#!/bin/bash
set -e
INSTALL_DIR="/volume1/docker/fancontrol-web"
mkdir -p "$INSTALL_DIR" && cd "$INSTALL_DIR"
mkdir -p templates/js data/logs

cat > app.py << 'ENDOFFILE'
import copy
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO
from werkzeug.exceptions import BadRequest

LOG_DIR = '/app/data/logs'
os.makedirs(LOG_DIR, exist_ok=True)

DATA_DIR = Path(os.getenv('FANCONTROL_DATA_DIR', '/app/data'))
HWMON_DIR = Path(os.getenv('FANCONTROL_HWMON_DIR', '/sys/class/hwmon'))
CONFIG_PATH = DATA_DIR / 'config.json'
CONFIG_FILE = str(CONFIG_PATH)
DB_FILE = DATA_DIR / 'fancontrol.db'
state_lock = threading.Lock()

logger = logging.getLogger('fancontrol')
logger.setLevel(logging.DEBUG)
fmt = logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
for h in [logging.StreamHandler(sys.stdout), RotatingFileHandler(f'{LOG_DIR}/fancontrol.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')]:
    h.setLevel(logging.INFO if isinstance(h, logging.StreamHandler) else logging.DEBUG)
    h.setFormatter(fmt)
    logger.addHandler(h)

app = Flask(__name__, static_folder='templates/js', static_url_path='/js')
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading', logger=True, engineio_logger=False, ping_timeout=120, ping_interval=25)

state = {
    'fans': {},
    'temp_sensors': {},
    'hdd_sensors': {},
    'max_hdd_temp': 0,
    'fan_enabled': True,
    'tested': False,
    'testing': False,
    'test_progress': {},
    '_pause_loop': False,
    'failsafe': False,
    'last_hdd_poll': 0,
    'initialized': False,
    'hardware_scanned': False,
    'discovered_fans': {},
    'discovered_temps': {},
    'discovered_disks': {}
}
executor = ThreadPoolExecutor(max_workers=6)
_failed_calibration_logged = False
_last_cleanup = 0

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_socket_connect():
    socketio.emit('update', get_state())

@socketio.on('get_state')
def handle_get_state():
    socketio.emit('update', get_state())


def get_state():
    with state_lock:
        return {
            'fans': copy.deepcopy(state['fans']),
            'temp_sensors': copy.deepcopy(state['temp_sensors']),
            'hdd_sensors': copy.deepcopy(state['hdd_sensors']),
            'max_hdd_temp': state.get('max_hdd_temp', 0),
            'fan_enabled': state.get('fan_enabled', True),
            'tested': state.get('tested', False),
            'testing': state.get('testing', False),
            'test_progress': copy.deepcopy(state.get('test_progress', {})),
            '_pause_loop': state.get('_pause_loop', False),
            'failsafe': state.get('failsafe', False),
            'initialized': state.get('initialized', False),
            'hardware_scanned': state.get('hardware_scanned', False),
            'discovered_fans': copy.deepcopy(state.get('discovered_fans', {})),
            'discovered_temps': copy.deepcopy(state.get('discovered_temps', {})),
            'discovered_disks': copy.deepcopy(state.get('discovered_disks', {}))
        }


def load_config():
    try:
        if not CONFIG_PATH.exists():
            return
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        with state_lock:
            if isinstance(cfg, dict):
                fans = cfg.get('fans', {})
                for key, fan_cfg in fans.items():
                    if key in state['fans']:
                        state['fans'][key].update(fan_cfg)
                    else:
                        state['fans'][key] = fan_cfg
                state['initialized'] = bool(cfg.get('initialized', False))
                state['tested'] = bool(cfg.get('tested', False))
                state['config'] = cfg
    except Exception as e:
        logger.error(f'Failed to load config: {e}')


@app.route('/api/discover', methods=['POST'])
def api_discover():
    try:
        fans, temps = discover_fans_and_sensors()
        disks = discover_disks()
        with state_lock:
            state['fans'] = fans
            state['temp_sensors'] = temps
            state['hdd_sensors'] = disks
            state['hardware_scanned'] = True
            state['discovered_fans'] = copy.deepcopy(fans)
            state['discovered_temps'] = copy.deepcopy(temps)
            state['discovered_disks'] = copy.deepcopy(disks)
        socketio.emit('hardware_discovered', {'fans': fans, 'temps': temps, 'disks': disks})
        return jsonify({'status': 'ok', 'fans': fans, 'temps': temps, 'disks': disks})
    except Exception as e:
        logger.error(f'Discovery error: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/initialize', methods=['POST'])
def api_initialize():
    try:
        if state.get('testing'):
            return jsonify({'status': 'error', 'message': 'Calibration already running'}), 409
        with state_lock:
            state['testing'] = True
            state['test_progress'] = {'status': 'Starting calibration...', 'step': 0, 'total': 0, 'current': ''}
        threading.Thread(target=test_fans, daemon=True).start()
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'Initialization error: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/test/start', methods=['POST'])
def api_test_start():
    try:
        data = request.get_json(silent=True) or {}
        fan_key = data.get('fan')
        if state.get('testing'):
            return jsonify({'status': 'error', 'message': 'Test already running'}), 409
        with state_lock:
            state['testing'] = True
            state['test_progress'] = {'status': 'Starting test...', 'step': 0, 'total': 0, 'current': ''}
        threading.Thread(target=test_fans, args=(fan_key,), daemon=True).start()
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'Test start error: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ================ DISK SUBSYSTEM ================

def is_physical_disk(dev_name):
    if re.match(r'^sata\d+$', dev_name): return True
    if dev_name.startswith('nvme') and re.match(r'^nvme\d+n\d+$', dev_name): return True
    if re.match(r'^sd[a-z]$', dev_name): return True
    if re.match(r'^sd[a-z]{2,}$', dev_name): return True
    if any(dev_name.startswith(p) for p in ['hd', 'xvd', 'vd']) and not re.search(r'\d$', dev_name): return True
    return False

def parse_smart_temp(output):
    for line in output.split('\n'):
        if 'Temperature_Celsius' in line:
            parts = line.split()
            try:
                idx = next((i for i, p in enumerate(parts) if 'Temperature_Celsius' in p), -1)
                if idx >= 0 and idx + 2 < len(parts):
                    temp = int(parts[idx + 2])
                    if 0 < temp < 100: return temp
            except: pass
            match = re.search(r'(\d+)\s*\(', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100: return temp
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70: return temp
    for line in output.split('\n'):
        if 'Airflow_Temperature_Cel' in line:
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70: return temp
    return None

def read_disk_temp(disk_identifier):
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()
        if not is_physical_disk(clean_name):
            return None, False
        cmd = ['smartctl', '-A', '-n', 'standby', f'/dev/{clean_name}']
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 2:
                return None, True
            if r.returncode != 0:
                cmd2 = ['smartctl', '-A', '-n', 'standby', '-d', 'sat', f'/dev/{clean_name}']
                r = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
                if r.returncode != 0 and r.returncode != 2:
                    return None, False
                if r.returncode == 2:
                    return None, True
            if clean_name.startswith('nvme'):
                for line in r.stdout.split('\n'):
                    if 'Temperature:' in line:
                        m = re.search(r'(\d+)\s*Celsius', line)
                        if m: return int(m.group(1)), False
            else:
                temp = parse_smart_temp(r.stdout)
                if temp: return temp, False
        except subprocess.TimeoutExpired:
            pass
    except Exception as e:
        logger.error(f'Error reading temp for {disk_identifier}: {e}')
    return None, False

def discover_disks():
    logger.info('='*40)
    logger.info('UNIVERSAL DISK SCANNING')
    disks = {}
    discovered_devices = set()
    try:
        result = subprocess.run(['lsblk', '-nd', '-o', 'NAME,TYPE,TRAN'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().split('\n'):
            if not line.strip(): continue
            parts = line.split()
            if len(parts) >= 2:
                name = parts[0].strip()
                dtype = parts[1].strip()
                if dtype == 'disk' and is_physical_disk(name):
                    if not any(name.startswith(x) for x in ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']):
                        discovered_devices.add(name)
    except Exception as e:
        logger.warning(f'lsblk failed: {e}')
        # Fallback to scanning /sys/block when lsblk is unavailable
        try:
            for dev in Path('/sys/block').iterdir():
                name = dev.name
                if is_physical_disk(name) and not any(name.startswith(x) for x in ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']):
                    discovered_devices.add(name)
        except Exception as e2:
            logger.warning(f'/sys/block fallback failed: {e2}')
    
    futures_map = {dev: executor.submit(read_disk_temp, dev) for dev in sorted(discovered_devices)}
    for dev, future in futures_map.items():
        try:
            result = future.result(timeout=15)
            t, standby = result if result else (None, False)
            disk_id = dev
            disk_label = f'SATA {dev.replace("sata", "")}' if dev.startswith('sata') else (f'NVMe {dev}' if dev.startswith('nvme') else f'Disk {dev}')
            disks[disk_id] = {
                'label': disk_label, 'device': f'/dev/{dev}', 'dev_name': dev,
                'temp': t if t else 0, 'standby': standby,
                'type': 'nvme' if dev.startswith('nvme') else 'sata'
            }
        except Exception as e:
            logger.error(f'Failed to poll disk {dev}: {e}')
    logger.info(f'  FINAL: disks={len(disks)}')
    return disks

# ================ FAN DISCOVERY ================

def discover_fans_and_sensors():
    logger.info('='*40)
    logger.info('SCANNING FANS AND SENSORS')
    fans, temps = {}, {}
    for hw in sorted(Path('/sys/class/hwmon').iterdir()):
        try:
            chip = (hw/'name').read_text().strip() if (hw/'name').exists() else '?'
        except (FileNotFoundError, PermissionError):
            logger.warning(f'  Skipped {hw.name} - hardware removed or access denied')
            continue
{"text": "        logger.info(f'  Chip: {hw.name} ({chip})')\r\n        for pwmf in sorted(hw.glob('pwm*')):"}
            if '_' in pwmf.name: continue
            n = re.search(r'\d+', pwmf.name).group()
            fanf = hw/f'fan{n}_input'
            if not fanf.exists(): continue
            try:
                lbl = (hw/f'fan{n}_label').read_text().strip() if (hw/f'fan{n}_label').exists() else f'Fan {n}'
            except (FileNotFoundError, PermissionError):
                lbl = f'Fan {n}'}
            w = os.access(str(pwmf), os.W_OK)
            try: current_rpm = int(Path(str(fanf)).read_text().strip())
            except: current_rpm = 0
            fans[f'{hw.name}/{pwmf.name}'] = dict(
                label=lbl, pwm_path=str(pwmf), fan_path=str(fanf),
                rpm=current_rpm, pwm_value=0, writable=w, inverted=False,
                min_rpm=0, max_rpm=0, manual_pct=50,
                sensors=[], sensor_mode='max', target_temp=31,
                fan_mode='manual', schedule=[], curve=[], calibration={}, status='not_tested',
                current_pct=50, raw_pwm=128, last_update=0
            )
        for tf in sorted(hw.glob('temp*_input')):
            tn = tf.name.replace('_input', '')
            lbl = (hw/f'{tn}_label').read_text().strip() if (hw/f'{tn}_label').exists() else 'Temp'
            try: current_temp = int(Path(str(tf)).read_text().strip()) // 1000
            except: current_temp = 0
            temps[f'{hw.name}/{tn}'] = dict(path=str(tf), label=lbl, value=current_temp)
    return fans, temps

# ================ PWM CONTROL ================

def set_pwm_raw(key, physical_pwm):
    with state_lock:
        f = state['fans'].get(key)
        if f and f.get('pwm_path', '').startswith('/sys/class/hwmon/'):
            val = max(0, min(255, int(physical_pwm)))
            try:
                Path(f['pwm_path']).write_text(str(val))
                f['pwm_value'] = val
            except Exception as e:
                logger.error(f'Raw PWM write error {key}: {e}')

def set_pwm(key, raw_pwm, from_curve=False):
    with state_lock:
        f = state['fans'].get(key)
        if not f or not f.get('pwm_path', '').startswith('/sys/class/hwmon/'):
            return
        val = max(0, min(255, int(raw_pwm)))
        f['raw_pwm'] = val
        physical_pwm = (255 - val) if (f.get('inverted') and not from_curve) else val
        try:
            Path(f['pwm_path']).write_text(str(physical_pwm))
            f['pwm_value'] = val
            rpm_raw = Path(f['fan_path']).read_text().strip()
            rpm_val = int(rpm_raw) if rpm_raw.isdigit() else 0
            if rpm_val > 0:
                f['rpm'] = rpm_val
            f['last_update'] = time.time()
        except Exception as e:
            logger.error(f'PWM write error {key}: {e}')

# ================ PARALLEL CALIBRATION ================

def test_fans(fan_key=None):
    global _failed_calibration_logged
    test_successful = True

    with state_lock:
        if fan_key:
            if fan_key not in state['fans']:
                raise ValueError(f'Fan key not found: {fan_key}')
            fans_to_test = {fan_key: copy.deepcopy(state['fans'][fan_key])}
        else:
            fans_to_test = {k: copy.deepcopy(v) for k, v in state['fans'].items()}

    writable_fans = {k: f for k, f in fans_to_test.items() if f.get('writable')}

    if len(writable_fans) == 0:
        logger.warning('No writable fans found for calibration')
        return

    for f in writable_fans.values():
        f['inverted'] = False
        f['status'] = 'calibrating'

    with state_lock:
        state['testing'] = True
        state['_pause_loop'] = True
        state['test_progress'] = dict(
            status='Starting parallel calibration...',
            step=0,
            total=11,
            current='All fans'
        )
    socketio.emit('test_progress', state['test_progress'])

    pwm_steps = [0, 26, 51, 77, 102, 128, 153, 179, 204, 230, 255]
    raw_data = {k: [] for k in writable_fans}

    try:
        for step_idx, p in enumerate(pwm_steps):
            pct = round(p * 100 / 255)
            with state_lock:
                state['test_progress'].update(
                    step=step_idx + 1,
                    status=f'Testing level {pct}%',
                    current='Parallel mode'
                )
            socketio.emit('test_progress', state['test_progress'])

            for k in writable_fans:
                set_pwm_raw(k, p)

            for _ in range(6):
                time.sleep(1)
                socketio.sleep(0)

            def read_one_rpm(item):
                key, fan = item
                try:
                    rpm = int(Path(fan['fan_path']).read_text().strip())
                except Exception:
                    rpm = 0
                return key, rpm

            futures = [executor.submit(read_one_rpm, (k, f)) for k, f in writable_fans.items()]
            for future in futures:
                try:
                    key, rpm = future.result(timeout=2)
                    raw_data[key].append(dict(pwm=p, rpm=rpm, pct=pct))
                    if rpm is not None:
                        with state_lock:
                            if key in state['fans']:
                                state['fans'][key]['rpm'] = rpm
                except Exception as ex:
                    logger.error(f'RPM read error: {ex}')

            socketio.emit('update', get_state())
            socketio.sleep(0)

        for k, fan in writable_fans.items():
            raw = raw_data.get(k, [])
            if not raw:
                logger.warning(f'Fan {fan.get("label", k)} ({k}): No RPM data collected')
                fan.update(status='not_connected', min_rpm=0, max_rpm=0, curve=[], calibration={})
                set_pwm_raw(k, 128)
                continue

            all_rpm = [pt['rpm'] for pt in raw]
            max_rpm = max(all_rpm)

            if max_rpm == 0:
                logger.warning(f"Fan {fan.get('label', k)} ({k}): No RPM detected")
                fan.update(status='not_connected', min_rpm=0, max_rpm=0, curve=[], calibration={})
                set_pwm_raw(k, 128)
                continue

            low_rpm_avg = sum(pt['rpm'] for pt in raw[:3]) / 3 if raw[:3] else 0
            high_rpm_avg = sum(pt['rpm'] for pt in raw[-3:]) / 3 if raw[-3:] else 0

            if high_rpm_avg > 0 and low_rpm_avg > high_rpm_avg * 1.2:
                fan['inverted'] = True
                fan['status'] = 'inverted'
                fan['curve'] = sorted(raw, key=lambda x: x['pwm'], reverse=True)
            else:
                fan['inverted'] = False
                fan['status'] = 'normal'
                fan['curve'] = sorted(raw, key=lambda x: x['pwm'])

            real_min = next((pt for pt in raw if pt['rpm'] > 150), raw[0])
            fan.update(
                min_rpm=real_min['rpm'],
                max_rpm=max_rpm,
                calibration=dict(min_rpm=real_min['rpm'], max_rpm=max_rpm, min_pct=real_min['pct'], inverted=fan['inverted'])
            )
            set_pwm(k, 128)
            logger.info(f'Fan {fan.get("label", k)}: status={fan["status"]}, min={fan["min_rpm"]}rpm, max={fan["max_rpm"]}rpm')

        with state_lock:
            for k, fan in writable_fans.items():
                if k in state['fans']:
                    state['fans'][k].update(fan)

    except Exception as e:
        logger.error(f'Test error: {e}')
        test_successful = False
    finally:
        with state_lock:
            state['testing'] = False
            state['tested'] = test_successful
            if test_successful:
                state['initialized'] = True
                save_config()
                logger.info('System initialized successfully')
            state['_pause_loop'] = False
            state['test_progress'] = dict(status='Ready!' if test_successful else 'Completed with errors', step=0, total=0, current='')
            current_initialized = state['initialized']
        socketio.emit('test_progress', state['test_progress'])
        socketio.emit('test_complete', {'success': test_successful, 'initialized': current_initialized})

# ================ MAIN CONTROL LOOP ================

def refresh():
    with state_lock:
        temp_items = [(k, v.copy()) for k, v in state['temp_sensors'].items()]
        fan_items = [(k, v.copy()) for k, v in state['fans'].items()]

    for key, sensor in temp_items:
        try:
            value = int(Path(sensor['path']).read_text().strip()) // 1000
        except Exception:
            continue
        with state_lock:
            if key in state['temp_sensors']:
                state['temp_sensors'][key]['value'] = value

    def poll_fan(item):
        k, fan = item
        try:
            rpm = int(Path(fan['fan_path']).read_text().strip())
        except Exception as e:
            logger.error(f'Fan poll error {k}: {e}')
            rpm = None
        return k, rpm

    futures = [executor.submit(poll_fan, item) for item in fan_items]
    for future in futures:
        try:
            key, rpm = future.result(timeout=2)
            if rpm is not None:
                with state_lock:
                    if key in state['fans']:
                        state['fans'][key]['rpm'] = rpm
        except Exception:
            pass

def refresh_disks():
    now = time.time()
    with state_lock:
        last_poll = state['last_hdd_poll']
        sensors_copy = {disk_id: info.copy() for disk_id, info in state['hdd_sensors'].items()}
    if last_poll > 0 and now - last_poll < 30:
        return

    futures_map = {disk_id: executor.submit(read_disk_temp, info.get('dev_name', disk_id)) for disk_id, info in sensors_copy.items()}
    updated_values = {}

    for disk_id, future in futures_map.items():
        try:
            result = future.result(timeout=15)
            t, standby = result if result else (None, False)
            if t is not None and t > 0:
                updated_values[disk_id] = {'temp': t, 'standby': standby}
        except Exception as e:
            logger.error(f'Poll error for {disk_id}: {e}')

    with state_lock:
        for disk_id, data in updated_values.items():
            if disk_id in state['hdd_sensors']:
                state['hdd_sensors'][disk_id].update(data)
        state['last_hdd_poll'] = time.time()
        active = [v['temp'] for v in state['hdd_sensors'].values() if v.get('temp', 0) > 0 and not v.get('standby')]
        if active:
            state['max_hdd_temp'] = max(active)
            state['failsafe'] = False
        else:
            state['max_hdd_temp'] = 0
            state['failsafe'] = True

def pwm_from_curve(fan, target_pct):
    curve, cal = fan.get('curve', []), fan.get('calibration', {})
    if not cal or len(curve) < 2:
        return int(target_pct * 255 // 100)
    target_pct = max(0, min(100, int(target_pct)))
    min_pct = cal.get('min_pct', 0)
    if 0 < target_pct < min_pct:
        target_pct = min_pct
    for i in range(len(curve)-1):
        a, b = curve[i], curve[i+1]
        if min(a['pct'], b['pct']) <= target_pct <= max(a['pct'], b['pct']):
            if a['pct'] == b['pct']:
                return a['pwm']
            ratio = (target_pct - a['pct']) / (b['pct'] - a['pct'])
            pwm = a['pwm'] + (b['pwm'] - a['pwm']) * ratio
            return max(0, min(255, int(pwm)))
    return curve[-1]['pwm']

def fan_temp(fan):
    sensors, mode = fan.get('sensors', []), fan.get('sensor_mode', 'max')
    temps = []
    with state_lock:
        hdd_copy = {k: v.copy() for k, v in state['hdd_sensors'].items()}
        temp_copy = {k: v.copy() for k, v in state['temp_sensors'].items()}
    for s in sensors:
        if s.startswith('hdd:'):
            t = hdd_copy.get(s.split(':', 1)[1], {}).get('temp', 0)
        elif s.startswith('temp:'):
            t = temp_copy.get(s.split(':', 1)[1], {}).get('value', 0)
        else:
            t = 0
        if t > 0:
            temps.append(t)
    if not temps:
        return 99
    if mode == 'max': return max(temps)
    if mode == 'min': return min(temps)
    return sum(temps) // len(temps)

def loop():
    global _failed_calibration_logged, _last_cleanup
    last_log = 0
    while True:
        try:
            if state.get('testing'):
                refresh()
                refresh_disks()
                socketio.emit('update', get_state())
                time.sleep(2)
                continue

            if state.get('_pause_loop'):
                time.sleep(1)
                continue
                
            if not state.get('initialized'):
                if state.get('hardware_scanned'):
                    refresh()
                    refresh_disks()
                    socketio.emit('update', get_state())
                
                if not state.get('tested', True):
                    if not _failed_calibration_logged:
                        logger.warning("Calibration failed. Fix hardware and restart.")
                        _failed_calibration_logged = True
                    time.sleep(10)
                    continue
                time.sleep(2)
                continue
                
            refresh()
            refresh_disks()
            with state_lock:
                fans_snapshot = copy.deepcopy(state['fans'])
            for k, f in fans_snapshot.items():
                fm = f.get('fan_mode', 'manual')
                if fm == 'manual':
                    raw_pct = f.get('manual_pct', 50)
                    calculated_pwm = int(raw_pct * 255 // 100)
                    set_pwm(k, calculated_pwm)
                    with state_lock:
                        if k in state['fans']:
                            state['fans'][k]['current_pct'] = raw_pct
                    continue
                if fm != 'auto':
                    continue
                sched = f.get('schedule', [])
                schedule_applied = False
                if sched:
                    now_dt = datetime.now()
                    cd = now_dt.strftime('%a').lower()
                    ct = now_dt.strftime('%H:%M')
                    for item in sched:
                        dg = []
                        if item['day'] == 'all': dg = ['mon','tue','wed','thu','fri','sat','sun']
                        elif item['day'] == 'weekday': dg = ['mon','tue','wed','thu','fri']
                        elif item['day'] == 'weekend': dg = ['sat','sun']
                        else: dg = [item['day']]
                        if cd in dg and item['time_start'] <= ct <= item['time_end']:
                            sm = item.get('mode', 'auto')
                            schedule_applied = True
                            if sm == 'off':
                                set_pwm(k, 0, from_curve=True)
                            elif sm == 'fixed':
                                set_pwm(k, pwm_from_curve(f, 50), from_curve=True)
                            elif sm == 'low':
                                set_pwm(k, pwm_from_curve(f, 20), from_curve=True)
                            else:
                                current_temp = fan_temp(f)
                                target = item.get('target_temp', 31)
                                delta = current_temp - target
                                target_pct = 20 if delta <= -2 else (100 if delta >= 6 else 20 + (delta+2)*80//8)
                                set_pwm(k, pwm_from_curve(f, target_pct), from_curve=True)
                            break
                if not schedule_applied:
                    current_temp = fan_temp(f)
                    if current_temp == 99 or state.get('failsafe'):
                        set_pwm(k, pwm_from_curve(f, 100), from_curve=True)
                    else:
                        target = f.get('target_temp', 31)
                        delta = current_temp - target
                        target_pct = 20 if delta <= -2 else (100 if delta >= 6 else 20 + (delta+2)*80//8)
                        set_pwm(k, pwm_from_curve(f, target_pct), from_curve=True)
            socketio.emit('update', get_state())
            current_time = time.time()
            if current_time - last_log > 300 or current_time < last_log:
                try:
                    with state_lock:
                        fc = len(state['fans'])
                        ap = sum(f.get('raw_pwm', f.get('pwm_value', 0)) for f in state['fans'].values()) // fc if fc > 0 else 0
                        ar = sum(f.get('rpm', 0) for f in state['fans'].values()) // fc if fc > 0 else 0
                        max_temp = state.get('max_hdd_temp', 0)
                    with sqlite3.connect(DB_FILE, timeout=30) as conn:
                        conn.execute('INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?, ?)',
                                   (datetime.now().isoformat(), 'auto', ap, ar, max_temp, fc, len(state['hdd_sensors'])))
                        conn.commit()
                except sqlite3.OperationalError as e:
                    logger.error(f'SQLite write error: {e}')
                last_log = current_time
            if current_time - _last_cleanup > 86400:
                cleanup_logs()
                _last_cleanup = current_time
            time.sleep(5)
        except Exception as e:
            logger.error(f'Loop error: {e}')
            time.sleep(5)

# ================ CONFIG MANAGEMENT ================

FAN_FIELDS = ['label', 'pwm_path', 'fan_path', 'inverted', 'min_rpm', 'max_rpm',
              'manual_pct', 'sensors', 'sensor_mode', 'target_temp', 'fan_mode',
              'schedule', 'curve', 'calibration', 'status', 'current_pct']

def save_config():
    with state_lock:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            state.setdefault('config', {})
            state['config']['initialized'] = state.get('initialized', False)
            state['config']['tested'] = state.get('tested', False)
            state['config']['fans'] = {
                k: {field: fan.get(field) for field in FAN_FIELDS if field in fan}
                for k, fan in state.get('fans', {}).items()
            }
            tmp_path = CONFIG_PATH.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state['config'], f, indent=4, ensure_ascii=False)
            tmp_path.replace(CONFIG_PATH)
            logger.info("Configuration saved atomically.")
        except Exception as e:
            logger.error(f"Failed to save config atomically: {e}")


def cleanup_logs(retention_days=30):
    try:
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with sqlite3.connect(DB_FILE, timeout=30) as conn:
            conn.execute('DELETE FROM logs WHERE ts < ?', (cutoff,))
            conn.commit()
    except sqlite3.OperationalError as e:
        logger.error(f'Failed to cleanup logs: {e}')


def validate_control_request(data):
    if not data or not isinstance(data, dict):
        raise BadRequest("Invalid JSON payload structure")

    action = data.get('action')
    if action not in ['set_fan_pwm', 'set_fan_config']:
        raise BadRequest(f"Unsupported action: {action}")

    fan_key = data.get('fan')
    with state_lock:
        if not fan_key or fan_key not in state.get('fans', {}):
            raise BadRequest(f"Fan key '{fan_key}' is missing or invalid")

    if action == 'set_fan_pwm':
        pwm_val = data.get('pwm')
        if pwm_val is None or not isinstance(pwm_val, int) or not (0 <= pwm_val <= 100):
            raise BadRequest("PWM value must be an integer between 0 and 100")
    elif action == 'set_fan_config':
        if 'schedule' in data:
            if not isinstance(data['schedule'], list):
                raise BadRequest("Schedule must be a list of rules")
            for rule in data['schedule']:
                if not isinstance(rule, dict) or 'mode' not in rule:
                    raise BadRequest("Invalid rule structure in schedule")
                if rule['mode'] in ['fixed', 'low'] and 'speed_pct' in rule:
                    if not (0 <= int(rule['speed_pct']) <= 100):
                        raise BadRequest("Schedule speed_pct must be between 0 and 100")

@app.route('/api/control', methods=['POST'])
def handle_control():
    try:
        data = request.get_json(force=True)
        validate_control_request(data)

        if data['action'] == 'set_fan_pwm':
            updated_fan = None
            pwm_val = int(data['pwm'] * 255 // 100)
            with state_lock:
                fan = state['fans'][data['fan']]
                updated_fan = fan.copy()
                updated_fan['manual_pct'] = data['pwm']
                updated_fan['fan_mode'] = fan.get('fan_mode', 'manual')
                state['fans'][data['fan']] = updated_fan
                save_config()

            if updated_fan['fan_mode'] == 'manual':
                set_pwm(data['fan'], pwm_val)
        elif data['action'] == 'set_fan_config':
            updated_fan = None
            with state_lock:
                fan = state['fans'][data['fan']]
                updated_fan = fan.copy()
                for key, value in data.items():
                    if key in ['schedule', 'sensors', 'sensor_mode', 'target_temp', 'fan_mode']:
                        updated_fan[key] = value
                state['fans'][data['fan']] = updated_fan
                save_config()

            if data.get('fan_mode') == 'manual':
                set_pwm(data['fan'], int(updated_fan.get('manual_pct', 50) * 255 // 100))

        return jsonify({"status": "success"})
    except BadRequest as br:
        return jsonify({"status": "error", "message": br.description}), 400
    except Exception:
        return jsonify({"status": "error", "message": "Internal server error"}), 500

@app.route('/api/history')
def api_history():
    since = (datetime.now() - timedelta(hours=request.args.get('hours', 24, type=int))).isoformat()
    try:
        with sqlite3.connect(DB_FILE, timeout=30) as conn:
            return jsonify([dict(ts=r[0], mode=r[1], pwm=r[2], rpm=r[3], max_temp=r[4]) for r in conn.execute('SELECT * FROM logs WHERE ts > ? ORDER BY ts', (since,)).fetchall()])
    except sqlite3.OperationalError as e:
        logger.error(f'SQLite read error: {e}')
        return jsonify([]), 503

# ================ ENTRY POINT ================

if __name__ == '__main__':
    logger.info('='*60)
    logger.info('STARTING FanControl Web v2.9 - Enhanced Live Updates')
    try:
        with sqlite3.connect(DB_FILE, timeout=30) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS logs (ts TEXT, mode TEXT, pwm INTEGER, rpm INTEGER, max_temp INTEGER, fan_count INTEGER, disk_count INTEGER)')
            conn.commit()
    except sqlite3.OperationalError as e:
        logger.error(f'Failed to initialize database: {e}')
        sys.exit(1)
    if os.path.exists(CONFIG_FILE):
        try:
            state['fans'], state['temp_sensors'] = discover_fans_and_sensors()
            state['hdd_sensors'] = discover_disks()
            refresh()
            load_config()
            if state['initialized']:
                logger.info('System restored from saved configuration')
            else:
                logger.warning('Configuration exists but initialization failed')
        except Exception as e:
            logger.error(f'Startup error: {e}')
    else:
        state['initialized'] = False
        logger.info('No configuration found - wizard mode')
    threading.Thread(target=loop, daemon=True).start()
    logger.info(f'Starting server on port 5059')
    socketio.run(app, host='0.0.0.0', port=5059, allow_unsafe_werkzeug=True)

ENDOFFILE

cat > requirements.txt << 'ENDOFFILE'
flask==2.3.3
flask-socketio==5.3.6
python-socketio==5.11.2

ENDOFFILE

cat > Dockerfile << 'ENDOFFILE'
FROM python:3.10-slim

# Install tools and update smartmontools database
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        lm-sensors \
        smartmontools \
        util-linux && \
    update-smart-drivedb && \
    rm -rf /var/lib/apt/lists/*

# Create a non-root user for safer container execution
RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser

WORKDIR /app
RUN mkdir -p /app/data /app/templates/js && chown -R appuser:appuser /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser . .

USER appuser
EXPOSE 5059
CMD ["python", "app.py"]

ENDOFFILE

cat > docker-compose.yml << 'ENDOFFILE'
version: '3.8'
services:
  fancontrol:
    build: .
    container_name: fancontrol-web
    restart: unless-stopped
    network_mode: host
    volumes:
      - /sys:/sys:rw
      - /dev:/dev:rw
      - ./data:/app/data
    environment:
      - TZ=Europe/Moscow
    privileged: true
    cap_add:
      - SYS_RAWIO
      - SYS_ADMIN

ENDOFFILE

cat > templates/index.html << 'ENDOFFILE'
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FanControl Web v2.9</title>
    <link rel="icon" href="data:,">
    <style>
        :root{--bg:#1a1a2e;--card:#16213e;--accent:#0f3460;--text:#e0e0e0}
        *{box-sizing:border-box}
        body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:.85rem;margin:0;padding:10px;line-height:1.4}
        .card{background:var(--card);border:1px solid var(--accent);margin-bottom:10px;border-radius:8px;transition:border-left .3s ease}
        .card.auto-mode{border-left:3px solid #4a90d9}
        .card.manual-mode{border-left:3px solid #555}
        .card-header{background:var(--accent);font-weight:bold;padding:10px 15px;font-size:14px;border-radius:8px 8px 0 0}
        .card-body{padding:10px}
        .fan-card{background:rgba(255,255,255,.03);margin:4px 0;padding:8px;border-radius:4px}
        .fan-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
        .fan-row .name{font-weight:bold;min-width:100px}
        .fan-row input[type=range]{flex:1;min-width:80px}
        .sensor-temp{font-weight:bold}
        .temp-good{color:#00ff88}
        .temp-warn{color:#ffaa00}
        .temp-bad{color:#ff4444}
        .sensor-popup{display:none;position:fixed;background:var(--card);border:2px solid var(--accent);padding:10px;z-index:9999;max-height:50vh;overflow-y:auto;min-width:220px;box-shadow:0 4px 12px rgba(0,0,0,.5);border-radius:6px}
        .sensor-popup.show{display:block}
        .sensor-tag{background:var(--accent);padding:2px 6px;border-radius:3px;font-size:11px;display:inline-flex;align-items:center;gap:4px;margin:1px}
        .sensor-tag .remove{cursor:pointer;color:#ff4444;font-weight:bold}
        .sensor-btn{font-size:12px;padding:2px 8px;background:#0a0a2e;color:var(--text);border:1px solid #333;border-radius:3px;cursor:pointer}
        
        .setup-card{background:var(--card);border:2px solid var(--accent);box-shadow:0 8px 32px rgba(15,52,96,.4);border-radius:12px;max-width:800px;margin:5% auto;padding:40px;text-align:center}
        .setup-title{color:#fff;font-weight:700;font-size:20px;margin-bottom:15px}
        .btn-init{background:linear-gradient(135deg,#0f3460,#1a1a2e);border:2px solid #4a90d9;color:#fff;font-weight:bold;padding:12px 30px;border-radius:6px;cursor:pointer;font-size:16px;transition:all .2s;margin:10px 0}
        .btn-init:hover:not(:disabled){border-color:#00ff88;color:#00ff88;box-shadow:0 0 15px rgba(0,255,136,.3)}
        .btn-init:disabled{opacity:.5;cursor:not-allowed}
        .pulse-loader{color:#4a90d9;margin-top:15px;animation:pulse 2s infinite}
        @keyframes pulse{0%,100%{opacity:.6}50%{opacity:1}}
        
        .wizard-layout { display: flex; gap: 20px; text-align: left; margin: 15px 0; }
        .wizard-block { flex: 1; background: rgba(0,0,0,0.15); padding: 15px; border-radius: 8px; border: 1px solid var(--accent); }
        .wizard-block h5 { margin-top: 0; color: #4a90d9; border-bottom: 1px solid var(--accent); padding-bottom: 5px; }
        
        .discovered-device{display:flex;justify-content:space-between;align-items:center;padding:12px;margin:6px 0;background:rgba(255,255,255,.03);border-left:3px solid var(--accent);border-radius:6px;font-size:13px}
        .badge-need-calib { background: #ff8c00; color: #000; padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; }
        
        .fan-icon-svg{width:32px;height:32px;vertical-align:middle;margin-right:8px;transition:all 0.3s ease;display:inline-block}
        .fan-spinning{animation:fanRotate linear infinite;transform-origin:center center}
        @keyframes fanRotate{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}
        @keyframes calibratePulse{0%,100%{opacity:1}50%{opacity:.5}}
        .calibrating-pulse{animation:calibratePulse 1.5s ease-in-out infinite}
        .fan-rpm-live{transition:color .3s ease;min-width:90px;text-align:right}
        .text-muted{color:#888}.text-info{color:#4a90d9}.text-warning{color:#ffaa00}.text-danger{color:#ff4444}
        button{cursor:pointer}
        #sync-status{display:inline-block;font-size:12px;margin-right:15px;transition:color .3s}
        .synced{color:#00ff88}.saving{color:#4a90d9;animation:pulse 1.5s infinite}
    </style>
</head>
<body>

<div id="setup-screen" style="display:none">
    <div class="setup-card">
        <div id="setup-step-intro">
            <div style="font-size:60px;margin-bottom:20px">РІС™в„ўРїС‘РЏ</div>
            <h4 class="setup-title">Initial System Setup</h4>
            <p class="text-muted" style="margin-bottom:25px">
                Configuration file not found. System needs to scan available 
                data buses to automatically detect fans and temperature sensors.
            </p>
            <button class="btn-init" id="discover-btn" onclick="runDiscovery()">
                СЂСџвЂќРЊ Start Hardware Scan
            </button>
            <div id="discover-loader" style="display:none" class="pulse-loader">
                Scanning sysfs bus and querying smartctl...
            </div>
        </div>
        
        <div id="setup-step-results" style="display:none">
            <h4 style="color:#00ff88;margin-top:0">РІСљвЂ¦ Hardware Detected</h4>
            <div id="discovered-devices" style="text-align:left;max-height:500px;overflow-y:auto;margin:15px 0"></div>
            <div id="setup-step-action" style="display:none">
                <p class="text-muted" style="margin:15px 0">
                    To complete setup, fans must be calibrated. 
                    This will take about 1-2 minutes.
                </p>
                <button class="btn-init" id="calibrate-btn" onclick="runCalibration()">
                    СЂСџР‹Р‡ Start Fan Calibration
                </button>
                <div id="calibrate-loader" style="display:none" class="pulse-loader">
                    Calibrating: determining PWM/RPM curves...
                </div>
            </div>
        </div>
    </div>
</div>

<div id="main-screen" style="display:none">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <h3 style="margin:0">FanControl Web v2.9</h3>
        <div>
            <span id="sync-status" class="text-muted synced">РІвЂ”РЏ Synced</span>
            <span id="test-warning" style="display:none;color:#ffaa00;margin-right:10px">Not tested</span>
            <button onclick="startTest()" style="background:#ff8c00;color:#000;border:none;padding:6px 12px;border-radius:4px;font-weight:bold">Test</button>
        </div>
    </div>

    <div id="test-progress" style="display:none;background:var(--card);padding:8px;margin-bottom:8px;border-radius:4px;border:1px solid #4a90d9">
        <small><span id="test-status"></span></small>
    </div>

    <div class="card">
        <div class="card-header">Fans <span id="fan-count-disp" style="background:#333;padding:2px 8px;border-radius:10px;font-size:12px">0</span></div>
        <div class="card-body" id="fan-container">Loading...</div>
    </div>

    <div style="display:flex;gap:10px">
        <div class="card" style="flex:1">
            <div class="card-header">History (24h)</div>
            <div class="card-body"><canvas id="chart" height="180"></canvas></div>
        </div>
        <div style="width:300px">
            <div class="card">
                <div class="card-header">Status</div>
                <div class="card-body" style="text-align:center;font-size:18px">Max HDD: <b id="max-temp-disp">--</b></div>
            </div>
            <div class="card">
                <div class="card-header">Disks</div>
                <div class="card-body" id="disks-container">Loading...</div>
            </div>
            <div class="card">
                <div class="card-header">Sensors</div>
                <div class="card-body" id="temps-container" style="max-height:150px;overflow-y:auto">Loading...</div>
            </div>
        </div>
    </div>
</div>

<div class="sensor-popup" id="sensor-popup"></div>

<script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="/js/main.js"></script>
</body>
</html>

ENDOFFILE

cat > templates/js/main.js << 'ENDOFFILE'
console.log("=== FanControl Web v2.9 - main.js LOADED ===");

var chart = null;
var allSensors = [];
var fanConfigs = {};
var currentData = null;
var fansBuilt = false;
var buildingConfig = false;
var activeSliders = new Set();

// Function for safe DOM element IDs
function safeId(key) {
    return key.replace(/[^a-zA-Z0-9]/g, '_');
}

var lastValidTemp = 30;
var wizardStep = 'intro';

console.log("=== Creating socket connection ===");
var socket = io();
console.log("=== Socket created ===");

socket.on("connect", function() {
    console.log("=== Socket CONNECTED ===");
});

socket.on("update", function(d) {
    console.log("=== Update received ===", d);
    updateValues(d);
});

socket.on("hardware_discovered", function(data) {
    console.log("=== Hardware discovered event ===", data);
    if (wizardStep === 'intro' && data) {
        renderDiscoveredHardware(data);
        wizardStep = 'results';
    }
});

socket.on("test_progress", function(p) {
    console.log("=== Test progress ===", p);
    
    if (wizardStep !== 'calibrating' && p.step > 0) {
        wizardStep = 'calibrating';
        var introScreen = document.getElementById("setup-step-intro");
        var resultsScreen = document.getElementById("setup-step-results");
        var actionBlock = document.getElementById("setup-step-action");
        
        if (introScreen) introScreen.style.display = "none";
        if (resultsScreen) resultsScreen.style.display = "block";
        if (actionBlock) actionBlock.style.display = "block";
        
        var btn = document.getElementById("calibrate-btn");
        var loader = document.getElementById("calibrate-loader");
        if (btn) btn.disabled = true;
        if (loader) loader.style.display = "block";
    }
    
    var loader = document.getElementById("calibrate-loader");
    if (loader && p.status) {
        loader.textContent = p.status + " (" + p.step + "/" + p.total + ")";
    }
    
    var tp = document.getElementById("test-progress");
    if (tp) tp.style.display = "block";
    var ts = document.getElementById("test-status");
    if (ts) ts.textContent = p.status + " (" + p.step + "/" + p.total + ")";
});

socket.on("test_complete", function(data) {
    console.log("=== Test complete ===", data);
    var tp = document.getElementById("test-progress");
    if (tp) tp.style.display = "none";

    if (data && data.success && data.initialized) {
        wizardStep = 'done';
        alert("рџЋ‰ РљР°Р»РёР±СЂРѕРІРєР° СѓСЃРїРµС€РЅРѕ Р·Р°РІРµСЂС€РµРЅР°!\nР’СЃРµ РєСЂРёРІС‹Рµ PWM/RPM РїРѕСЃС‚СЂРѕРµРЅС‹. РџРµСЂРµС…РѕРґРёРј РЅР° РіР»Р°РІРЅС‹Р№ СЌРєСЂР°РЅ СѓРїСЂР°РІР»РµРЅРёСЏ.");
        fansBuilt = false;
    } else if (!data || !data.success) {
        alert("Calibration completed with errors! Check server logs.");
        
        wizardStep = 'results';
        var intro = document.getElementById("setup-step-intro");
        var results = document.getElementById("setup-step-results");
        var action = document.getElementById("setup-step-action");
        
        if (intro) intro.style.display = "none";
        if (results) results.style.display = "block";
        if (action) action.style.display = "block";
        
        var btn = document.getElementById("calibrate-btn");
        var loader = document.getElementById("calibrate-loader");
        if (btn) btn.disabled = false;
        if (loader) loader.style.display = "none";
    }
});

// ====================== WIZARD FUNCTIONS ======================

function runDiscovery() {
    console.log("=== runDiscovery: Phase 1 - Hardware Scan ===");
    
    var btn = document.getElementById("discover-btn");
    var loader = document.getElementById("discover-loader");
    if (btn) btn.disabled = true;
    if (loader) loader.style.display = "block";
    
    wizardStep = 'scanning';
    
    fetch("/api/discover", { method: "POST" })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            console.log("=== Discovery result ===", data);
            
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
            
            if (data.status === "ok") {
                renderDiscoveredHardware(data);
                wizardStep = 'results';
                
                var intro = document.getElementById("setup-step-intro");
                var results = document.getElementById("setup-step-results");
                if (intro) intro.style.display = "none";
                if (results) results.style.display = "block";
            } else {
                alert("Scan error: " + data.message);
                wizardStep = 'intro';
            }
        })
        .catch(function(err) {
            console.error("=== Discovery error ===", err);
            alert("Connection error during scan");
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
            wizardStep = 'intro';
        });
}

function renderDiscoveredHardware(data) {
    var container = document.getElementById("discovered-devices");
    if (!container) return;
    
    var fanSvg = '<svg class="fan-icon-svg" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">' +
        '<defs>' +
            '<linearGradient id="bladeGrad" x1="0%" y1="0%" x2="100%" y2="100%">' +
                '<stop offset="0%" style="stop-color:#4a90d9;stop-opacity:1" />' +
                '<stop offset="100%" style="stop-color:#00ff88;stop-opacity:1" />' +
            '</linearGradient>' +
        '</defs>' +
        '<circle cx="50" cy="50" r="12" fill="#666" stroke="#4a90d9" stroke-width="2"/>' +
        '<circle cx="50" cy="50" r="6" fill="#333"/>' +
        '<g fill="url(#bladeGrad)" opacity="0.9">' +
            '<path d="M50,50 L50,8 Q55,15 60,10 Q58,25 65,20 L50,50Z" />' +
            '<path d="M50,50 L90,50 Q82,55 86,60 Q72,58 76,65 L50,50Z" />' +
            '<path d="M50,50 L50,92 Q45,85 40,90 Q42,75 35,80 L50,50Z" />' +
            '<path d="M50,50 L10,50 Q18,45 14,40 Q28,42 24,35 L50,50Z" />' +
        '</g>' +
    '</svg>';

    var html = '<div class="wizard-layout">';
    
    // пїЅ пїЅпїЅ вЂєпїЅ С›пїЅ С™ 1: пїЅ СћпїЅ вЂўпїЅ СљпїЅ СџпїЅ вЂўпїЅ пїЅ пїЅ С’пїЅ СћпїЅ Р€пїЅ пїЅ пїЅ СњпїЅ В«пїЅ вЂў пїЅ вЂќпїЅ С’пїЅ СћпїЅ В§пїЅ пїЅпїЅ С™пїЅ пїЅ пїЅ пїЅ пїЅ вЂќпїЅ пїЅпїЅ РЋпїЅ С™пїЅ пїЅ
    html += '<div class="wizard-block">';
    html += '<h5>СЂСџРЉРЋРїС‘РЏ Sensors & Drives</h5>';
    html += '<div id="wizard-sensors-list">';
    
    // пїЅ вЂ™РЎвЂ№пїЅ Р†пїЅ С•пїЅ Т‘ пїЅ Т‘пїЅ С‘РЎРѓпїЅ С”пїЅ С•пїЅ Р†
    if (data.disks && Object.keys(data.disks).length > 0) {
        for (var k in data.disks) {
            var d = data.disks[k];
            var diskId = safeId(k);
            html += '<div class="discovered-device" id="wdrive-' + diskId + '">' +
                    '<span>СЂСџвЂ™С• ' + d.label + ' <small class="text-muted">(' + d.type.toUpperCase() + ')</small></span>' +
                    '<span class="drive-temp-live" style="font-weight:bold; color:#ffaa00;">' + (d.standby ? 'Sleep' : (d.temp > 0 ? d.temp + 'Р’В°C' : '--')) + '</span>' +
                    '</div>';
        }
    }
    
    // пїЅ вЂ™РЎвЂ№пїЅ Р†пїЅ С•пїЅ Т‘ РЎРѓпїЅ ВµпїЅ Р…РЎРѓпїЅ С•РЎР‚пїЅ С•пїЅ Р† пїЅ пїЅпїЅ В°РЎвЂљпїЅ ВµРЎР‚пїЅ С‘пїЅ Р…РЎРѓпїЅ С”пїЅ С•пїЅ в„– пїЅ С—пїЅ В»пїЅ В°РЎвЂљРЎвЂ№ / CPU
    if (data.temps && Object.keys(data.temps).length > 0) {
        for (var tk in data.temps) {
            var t = data.temps[tk];
            var tempId = safeId(tk);
            html += '<div class="discovered-device" id="wtemp-' + tempId + '">' +
                    '<span>СЂСџРЉС— ' + t.label + '</span>' +
                    '<span class="sensor-temp-live" style="font-weight:bold; color:#ffaa00;">' + (t.value || 0) + 'Р’В°C</span>' +
                    '</div>';
        }
    }
    html += '</div></div>';
    
    // пїЅ пїЅпїЅ вЂєпїЅ С›пїЅ С™ 2: пїЅ вЂ™пїЅ вЂўпїЅ СњпїЅ СћпїЅ пїЅпїЅ вЂєпїЅ Р‡пїЅ СћпїЅ С›пїЅ пїЅ пїЅ В«
    html += '<div class="wizard-block">';
    html += '<h5>СЂСџРЉР‚ Fans</h5>';
    html += '<div id="wizard-fans-list">';
    
    if (data.fans && Object.keys(data.fans).length > 0) {
        for (var key in data.fans) {
            var fan = data.fans[key];
            var fanId = safeId(key);
            
            html += '<div class="discovered-device" id="device-' + fanId + '" style="display:flex;align-items:center;justify-content:space-between">' +
                    '<div style="display:flex;align-items:center;gap:10px;flex:1">' +
                    '<span id="icon-' + fanId + '" style="display:flex;align-items:center">' + fanSvg + '</span>' +
                    '<div>' +
                    '<div style="font-weight:bold;font-size:14px">' + fan.label + '</div>' +
                    '<div style="font-size:10px;color:#888">' + key + ' | ' + (fan.writable ? 'РІСљвЂ¦ Controllable' : 'РІС™пїЅ РїС‘РЏ Read-only') + '</div>' +
                    '</div>' +
                    '</div>' +
                    '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">' +
                    '<span class="fan-status-live"><span class="badge-need-calib">Not calibrated</span></span>' +
                    '<span class="fan-rpm-live" style="font-weight:bold;color:#00ff88;font-size:14px;">0 RPM</span>' +
                    '</div>' +
                    '</div>';
        }
    }
    html += '</div></div>';
    html += '</div>'; // пїЅ С™пїЅ С•пїЅ Р…пїЅ ВµРЎпїЅ  wizard-layout
    
    container.innerHTML = html;
    
    var intro = document.getElementById("setup-step-intro");
    var results = document.getElementById("setup-step-results");
    var action = document.getElementById("setup-step-action");
    
    if (intro) intro.style.display = "none";
    if (results) results.style.display = "block";
    
    if (data.fans && Object.keys(data.fans).length > 0 && action) {
        action.style.display = "block";
    }
}

function runCalibration() {
    console.log("=== runCalibration: Phase 2 - Fan Calibration ===");
    
    var btn = document.getElementById("calibrate-btn");
    var loader = document.getElementById("calibrate-loader");
    if (btn) btn.disabled = true;
    if (loader) loader.style.display = "block";
    
    wizardStep = 'calibrating';
    
    fetch("/api/initialize", { method: "POST" })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            console.log("=== Calibration started ===", data);
        })
        .catch(function(err) {
            console.error("=== Calibration error ===", err);
            alert("Calibration launch error");
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
            wizardStep = 'results';
        });
}

// ====================== MAIN SCREEN FUNCTIONS ======================

function showSyncingStatus() {
    var statusEl = document.getElementById("sync-status");
    if (statusEl) {
        statusEl.textContent = "РІСџС– Saving...";
        statusEl.className = "saving";
        clearTimeout(window._syncTimeout);
        window._syncTimeout = setTimeout(function() {
            if (statusEl) {
                statusEl.textContent = "РІвЂ”РЏ Synced";
                statusEl.className = "synced";
            }
        }, 3000);
    }
}

function updateValues(d) {
    currentData = d;
    
    // пїЅ вЂўРЎРѓпїЅ В»пїЅ С‘ пїЅ пїЅРЎвЂ№ пїЅ Р…пїЅ В° РЎРЊпїЅ С”РЎР‚пїЅ В°пїЅ Р…пїЅ Вµ пїЅ пїЅпїЅ В°РЎРѓРЎвЂљпїЅ ВµРЎР‚пїЅ В° пїЅ Р…пїЅ В°РЎРѓРЎвЂљРЎР‚пїЅ С•пїЅ в„–пїЅ С”пїЅ С‘ (пїЅ С”пїЅ В°пїЅ В»пїЅ С‘пїЅ В±РЎР‚пїЅ С•пїЅ Р†пїЅ С”пїЅ С‘)
    if (wizardStep === 'results' || wizardStep === 'calibrating') {
        
        // 1. пїЅ С›пїЅ В±пїЅ Р…пїЅ С•пїЅ Р†пїЅ В»пїЅ ВµпїЅ Р…пїЅ С‘пїЅ Вµ РЎвЂљпїЅ ВµпїЅ пїЅпїЅ С—пїЅ ВµРЎР‚пїЅ В°РЎвЂљРЎС“РЎР‚ пїЅ Т‘пїЅ С‘РЎРѓпїЅ С”пїЅ С•пїЅ Р†
        if (d.hdd_sensors) {
            for (var dk in d.hdd_sensors) {
                var disk = d.hdd_sensors[dk];
                var diskId = safeId(dk);
                var driveRow = document.getElementById("wdrive-" + diskId);
                if (driveRow) {
                    var dTempEl = driveRow.querySelector(".drive-temp-live");
                    if (dTempEl) {
                        dTempEl.textContent = disk.standby ? 'Sleep' : (disk.temp > 0 ? disk.temp + 'Р’В°C' : '--');
                    }
                }
            }
        }
        
        // 2. пїЅ С›пїЅ В±пїЅ Р…пїЅ С•пїЅ Р†пїЅ В»пїЅ ВµпїЅ Р…пїЅ С‘пїЅ Вµ пїЅ Т‘пїЅ В°РЎвЂљРЎвЂЎпїЅ С‘пїЅ С”пїЅ С•пїЅ Р† пїЅ пїЅпїЅ В°РЎвЂљпїЅ ВµРЎР‚пїЅ С‘пїЅ Р…РЎРѓпїЅ С”пїЅ С•пїЅ в„– пїЅ С—пїЅ В»пїЅ В°РЎвЂљРЎвЂ№ / CPU
        if (d.temp_sensors) {
            for (var tk in d.temp_sensors) {
                var sensor = d.temp_sensors[tk];
                var tempId = safeId(tk);
                var tempRow = document.getElementById("wtemp-" + tempId);
                if (tempRow) {
                    var sTempEl = tempRow.querySelector(".sensor-temp-live");
                    if (sTempEl) {
                        sTempEl.textContent = (sensor.value || 0) + 'Р’В°C';
                    }
                }
            }
        }
        
        // 3. пїЅ С›пїЅ В±пїЅ Р…пїЅ С•пїЅ Р†пїЅ В»пїЅ ВµпїЅ Р…пїЅ С‘пїЅ Вµ пїЅ Р†пїЅ ВµпїЅ Р…РЎвЂљпїЅ С‘пїЅ В»РЎРЏРЎвЂљпїЅ С•РЎР‚пїЅ С•пїЅ Р†
        if (d.fans) {
            for (var key in d.fans) {
                var fan = d.fans[key];
                var deviceId = safeId(key);
                var deviceRow = document.getElementById("device-" + deviceId);
                
                if (deviceRow) {
                    var rpmEl = deviceRow.querySelector(".fan-rpm-live");
                    if (rpmEl) rpmEl.textContent = fan.rpm + " RPM";
                    
                    var statusEl = deviceRow.querySelector(".fan-status-live");
                    if (statusEl) {
                        if (fan.status === "calibrating") {
                            statusEl.innerHTML = '<span class="text-info calibrating-pulse">РІС™РЋ Calibrating...</span>';
                        } else if (fan.status === "normal") {
                            statusEl.innerHTML = '<span style="color:#00ff88">РІСљвЂњ Normal</span>';
                        } else if (fan.status === "inverted") {
                            statusEl.innerHTML = '<span style="color:#ffaa00">РІвЂЎвЂћ Inverted</span>';
                        } else if (fan.status === "not_connected") {
                            statusEl.innerHTML = '<span style="color:#ff4444">РІСљвЂ” Not connected</span>';
                        }
                    }
                    
                    // пїЅ вЂ™РЎР‚пїЅ В°РЎвЂ°пїЅ ВµпїЅ Р…пїЅ С‘пїЅ Вµ пїЅ С‘пїЅ С”пїЅ С•пїЅ Р…пїЅ С”пїЅ С‘
                    var iconContainer = document.getElementById("icon-" + safeId);
                    if (iconContainer) {
                        var svgElement = iconContainer.querySelector(".fan-icon-svg");
                        if (svgElement) {
                            var currentRpm = fan.rpm || 0;
                            if (currentRpm > 0) {
                                var visualDuration = (60 / currentRpm) * 10;
                                if (visualDuration < 0.3) visualDuration = 0.3;
                                if (visualDuration > 5.0) visualDuration = 5.0;
                                
                                svgElement.style.animationDuration = visualDuration.toFixed(2) + "s";
                                svgElement.classList.add("fan-spinning");
                            } else {
                                svgElement.classList.remove("fan-spinning");
                                svgElement.style.animationDuration = "0s";
                            }
                        }
                    }
                }
            }
        }
    }

    // If system is not initialized, show setup screen
    if (!d.initialized) {
        var ss = document.getElementById("setup-screen");
        var ms = document.getElementById("main-screen");
        if (ss) ss.style.display = "block";
        if (ms) ms.style.display = "none";
        
        if (d.hardware_scanned && wizardStep === 'intro') {
            renderDiscoveredHardware({
                fans: d.fans,
                temps: d.temp_sensors,
                disks: d.hdd_sensors
            });
            wizardStep = 'results';
            
            var btn = document.getElementById("discover-btn");
            var loader = document.getElementById("discover-loader");
            if (btn) btn.disabled = false;
            if (loader) loader.style.display = "none";
        }
        return;
    }

    // Show main screen
    var ss = document.getElementById("setup-screen");
    var ms = document.getElementById("main-screen");
    if (ss) ss.style.display = "none";
    if (ms) ms.style.display = "block";

    var mt = document.getElementById("max-temp-disp");
    if (mt) {
        var tc = "temp-good";
        if (d.max_hdd_temp > 35) tc = "temp-bad";
        else if (d.max_hdd_temp > 31) tc = "temp-warn";
        
        var tempSpan = mt.querySelector('span') || document.createElement('span');
        tempSpan.className = tc;
        tempSpan.textContent = d.max_hdd_temp + 'Р’В°C';
        if (!mt.contains(tempSpan)) {
            mt.textContent = '';
            mt.appendChild(tempSpan);
        }
    }

    var tw = document.getElementById("test-warning");
    if (tw) tw.style.display = d.tested ? "none" : "inline";

    allSensors = [];
    for (var k in d.hdd_sensors) {
        var s = d.hdd_sensors[k];
        allSensors.push({id: "hdd:" + k, label: s.label, temp: s.temp, standby: s.standby, group: "Disks"});
    }
    for (k in d.temp_sensors) {
        allSensors.push({id: "temp:" + k, label: d.temp_sensors[k].label, temp: d.temp_sensors[k].value, standby: false, group: "Sensors"});
    }

    var dc = document.getElementById("disks-container");
    if (dc) {
        dc.innerHTML = '';
        for (k in d.hdd_sensors) {
            var v = d.hdd_sensors[k];
            var html = '<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px">' +
                       '<span>' + v.label + '</span>' +
                       '<span style="color:' + (v.standby ? '#4a90d9' : v.temp > 35 ? '#ff4444' : v.temp > 31 ? '#ffaa00' : '#00ff88') + '">' +
                       (v.standby ? 'Sleep' : v.temp > 0 ? v.temp + 'Р’В°C' : '--') + '</span></div>';
            dc.innerHTML += html;
        }
    }

    var tc2 = document.getElementById("temps-container");
    if (tc2) {
        tc2.innerHTML = '';
        for (k in d.temp_sensors) {
            var tv = d.temp_sensors[k];
            tc2.innerHTML += '<div style="display:flex;justify-content:space-between;padding:3px 0;font-size:13px">' +
                            '<span>' + tv.label + '</span>' +
                            '<span>' + (tv.value || 0) + 'Р’В°C</span></div>';
        }
    }

    if (!fansBuilt) {
        buildFans(d);
        fansBuilt = true;
    }

    for (k in d.fans) {
        var f = d.fans[k];
            var ks = safeId(k);
        if (rpmEl) {
            var rpmText = f.rpm + " RPM";
            if (f.rpm_stabilizing) rpmText += " РІРЏС–";
            rpmEl.textContent = rpmText;
        }

        var sliderEl = document.getElementById("slider-" + ks);
        if (sliderEl && !activeSliders.has(ks)) {
            sliderEl.disabled = (f.fan_mode === "auto");
            if (f.fan_mode !== "auto") {
                sliderEl.value = f.manual_pct || 50;
            }
        }

        var pwmEl = document.getElementById("pwm-" + ks);
        if (pwmEl && (!sliderEl || document.activeElement !== sliderEl)) {
            if (f.fan_mode === "auto") {
                pwmEl.textContent = (f.current_pct !== undefined ? f.current_pct : 50) + "%";
            } else {
                pwmEl.textContent = (f.manual_pct || 50) + "%";
            }
        }

        var stEl = document.getElementById("status-" + ks);
        if (stEl) {
            var st = f.status || "not_tested";
            var labels = {normal: "Normal", inverted: "Inverted", absent: "Absent", not_tested: "Not tested", not_connected: "Not connected"};
            var classes = {normal: "temp-good", inverted: "text-warning", absent: "text-muted", not_tested: "text-danger", not_connected: "text-muted"};
            stEl.textContent = labels[st] || st;
            stEl.className = classes[st] || "";
        }

        var card = document.getElementById("card-" + ks);
        if (card) {
            card.className = "card " + (f.fan_mode === "auto" ? "auto-mode" : "manual-mode");
        }
    }
}

function buildFans(d) {
    var fc = document.getElementById("fan-container");
    if (!fc) return;
    
    var html = "";
    for (var k in d.fans) {
        var f = d.fans[k];
        var ks = safeId(k);
        html += "<div class='card " + (f.fan_mode === "auto" ? "auto-mode" : "manual-mode") + "' id='card-" + ks + "'>";
        html += "<div class='card-header'>" + f.label + " <small id='status-" + ks + "'></small></div>";
        html += "<div class='card-body'>";
        html += "<div class='fan-row'>";
        html += "<span class='name'>" + f.label + "</span>";
        html += " <button class='sensor-btn' data-fan='" + k + "'>Test</button>";
        html += "<span class='rpm' id='rpm-" + ks + "'>0 RPM</span>";
        html += "<input type='range' min='0' max='100' value='" + (f.manual_pct || 50) + "' data-fan='" + k + "' " + (d.tested ? "" : "disabled") + " id='slider-" + ks + "'>";
        html += "<small id='pwm-" + ks + "'>" + (f.manual_pct || 50) + "%</small>";
        html += "</div><div id='config-" + ks + "' style='font-size:12px;margin-top:5px;display:flex;align-items:center;gap:5px;flex-wrap:wrap'></div>";
        html += "<div id='schedule-" + ks + "'></div>";
        html += "</div></div>";
    }
    fc.innerHTML = html;
    
    var fcd = document.getElementById("fan-count-disp");
    if (fcd) fcd.textContent = Object.keys(d.fans).length;

    var sliders = fc.querySelectorAll("input[type=range]");
    for (var i = 0; i < sliders.length; i++) {
        (function(slider) {
            var fanKey = slider.getAttribute("data-fan");
            var ks2 = safeId(fanKey);
            
            slider.addEventListener("input", function() {
                var pwmEl = document.getElementById("pwm-" + ks2);
                if (pwmEl) pwmEl.textContent = this.value + "%";
            });
            
            slider.addEventListener("change", function() {
                showSyncingStatus();
                setFan(fanKey, this.value);
            });
            
            slider.addEventListener("mousedown", function() { activeSliders.add(ks2); });
            slider.addEventListener("mouseup", function() { 
                setTimeout(function() { activeSliders.delete(ks2); }, 1500); 
            });
        })(sliders[i]);
    }

    var buttons = fc.querySelectorAll("button.sensor-btn");
    for (var j = 0; j < buttons.length; j++) {
        (function(btn) {
            btn.addEventListener("click", function() { testFan(this.getAttribute("data-fan")); });
        })(buttons[j]);
    }

    for (k in d.fans) {
        if (!fanConfigs[k]) fanConfigs[k] = {};
        var f2 = d.fans[k];
        fanConfigs[k].sensors = f2.sensors || [];
        fanConfigs[k].sensor_mode = f2.sensor_mode || "max";
        fanConfigs[k].target_temp = f2.target_temp || 31;
        fanConfigs[k].fan_mode = f2.fan_mode || "manual";
        fanConfigs[k].schedule = f2.schedule || [];
        buildFanConfig(k, d);
    }
}

function buildFanConfig(k, d) {
    buildingConfig = true;
    var f = d.fans[k];
    var cfg = fanConfigs[k] || {};
    var ks = safeId(k);
    var sensors = cfg.sensors || [];
    var smode = cfg.sensor_mode || "max";
    var target = cfg.target_temp || 31;
    var fm = cfg.fan_mode || "manual";

    var configDiv = document.getElementById("config-" + ks);
    if (!configDiv) { buildingConfig = false; return; }

    var addBtn = document.createElement("button");
    addBtn.className = "sensor-btn";
    addBtn.textContent = "+";
    addBtn.onclick = function(e) { e.stopPropagation(); togglePopup(k, this); };

    var tagsDiv = document.createElement("div");
    tagsDiv.style.cssText = "display:flex;flex-wrap:wrap;gap:2px";
    for (var i = 0; i < sensors.length; i++) {
        var s = sensors[i];
        var found = allSensors.find(function(y) { return y.id === s; });
        var tag = document.createElement("span");
        tag.className = "sensor-tag";
        tag.setAttribute("data-sid", s);
        tag.textContent = (found ? found.label : s) + " ";
        var rm = document.createElement("span");
        rm.className = "remove";
        rm.textContent = "x";
        rm.onclick = function(e) {
            e.stopPropagation();
            removeSensor(k, this.parentNode.getAttribute("data-sid"));
        };
        tag.appendChild(rm);
        tagsDiv.appendChild(tag);
    }

    var smodeSel = document.createElement("select");
    smodeSel.innerHTML = "<option value='max'>Max</option><option value='min'>Min</option><option value='avg'>Avg</option>";
    smodeSel.value = smode;
    smodeSel.onchange = function() { showSyncingStatus(); setFanConfig(k, "sensor_mode", this.value); };

    var targetInput = document.createElement("input");
    targetInput.type = "number"; targetInput.value = target; targetInput.min = 20; targetInput.max = 60;
    targetInput.style.width = "45px";
    targetInput.onchange = function() { showSyncingStatus(); setFanConfig(k, "target_temp", this.value); };

    var fmSel = document.createElement("select");
    fmSel.innerHTML = "<option value='manual'>Manual</option><option value='auto'>Auto</option>";
    fmSel.value = fm;
    fmSel.onchange = function() { showSyncingStatus(); setFanConfig(k, "fan_mode", this.value); };

    configDiv.innerHTML = "";
    configDiv.appendChild(addBtn);
    configDiv.appendChild(tagsDiv);
    configDiv.appendChild(smodeSel);
    configDiv.appendChild(document.createTextNode(" Target:"));
    configDiv.appendChild(targetInput);
    configDiv.appendChild(document.createTextNode("Р’В°C "));
    configDiv.appendChild(fmSel);

    buildSchedule(k, ks, fm, cfg.schedule || []);
    setTimeout(function() { buildingConfig = false; }, 100);
}

function buildSchedule(k, ks, fm, schedule) {
    var schedDiv = document.getElementById("schedule-" + ks);
    if (!schedDiv) return;
    schedDiv.innerHTML = "";
    if (fm !== "auto") return;

    var builder = document.createElement("div");
    builder.className = "timeline-builder";
    var header = document.createElement("div");
    header.style.cssText = "display:flex;justify-content:space-between;align-items:center;margin-bottom:8px";
    header.innerHTML = "<span style='color:#aaa;font-size:12px'>Flexible Schedule</span>";
    var addBtn = document.createElement("button");
    addBtn.textContent = "+ Add";
    addBtn.style.cssText = "font-size:11px;padding:2px 8px;background:#00aa00;color:#fff;border:none;border-radius:3px";
    addBtn.onclick = function() { addSchedule(k); };
    header.appendChild(addBtn);
    builder.appendChild(header);

    if (schedule.length === 0) {
        var empty = document.createElement("div");
        empty.style.cssText = "text-align:center;color:#888;font-size:11px;padding:10px";
        empty.textContent = "No schedule configured.";
        builder.appendChild(empty);
    }

    var days = ["mon","tue","wed","thu","fri","sat","sun"];
    var dayNames = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
    for (var i = 0; i < schedule.length; i++) {
        var sch = schedule[i];
        var row = document.createElement("div");
        row.className = "timeline-rule-row";
        row.style.cssText = "display:flex;align-items:center;gap:8px;background:rgba(0,0,0,.2);padding:6px;border-radius:4px;margin:4px 0;flex-wrap:wrap";
        
        var daySel = document.createElement("select");
        daySel.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        daySel.innerHTML = "<option value='all'>Every day</option><option value='weekday'>Weekdays</option><option value='weekend'>Weekend</option>";
        for (var di = 0; di < 7; di++) daySel.innerHTML += "<option value='" + days[di] + "'>" + dayNames[di] + "</option>";
        daySel.value = sch.day || "all";
        daySel.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "day", this.value); };
        
        var timeStart = document.createElement("input"); 
        timeStart.type = "time"; 
        timeStart.value = sch.time_start || "00:00";
        timeStart.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        timeStart.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "time_start", this.value); };
        
        var timeEnd = document.createElement("input"); 
        timeEnd.type = "time"; 
        timeEnd.value = sch.time_end || "23:59";
        timeEnd.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        timeEnd.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "time_end", this.value); };
        
        var modeSel = document.createElement("select");
        modeSel.style.cssText = "background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
        modeSel.innerHTML = "<option value='auto'>Auto</option><option value='fixed'>Fixed</option><option value='low'>Quiet</option><option value='off'>Off</option>";
        modeSel.value = sch.mode || "auto";
        modeSel.onchange = function() {
            sch.mode = this.value;
            showSyncingStatus();
            updateSchedule(k, i, "mode", sch.mode);
            rebuildFanConfig(k);
        };
        
        var delBtn = document.createElement("button");
        delBtn.textContent = "x";
        delBtn.style.cssText = "color:#ff4444;background:none;border:none;font-size:16px;cursor:pointer;margin-left:auto";
        delBtn.onclick = function() { removeSchedule(k, i); };
        
        row.appendChild(daySel);
        row.appendChild(document.createTextNode(" from ")); 
        row.appendChild(timeStart);
        row.appendChild(document.createTextNode(" to ")); 
        row.appendChild(timeEnd);
        row.appendChild(modeSel);
        if (sch.mode === "auto" || !sch.mode) {
            row.appendChild(document.createTextNode(" Р¦РµР»СЊ:"));
            var ti = document.createElement("input");
            ti.type = "number";
            ti.value = sch.target_temp || 31;
            ti.style.cssText = "width:45px;background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
            ti.min = 20; ti.max = 60;
            ti.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "target_temp", parseInt(this.value)); };
            row.appendChild(ti);
            row.appendChild(document.createTextNode("В°C"));
        } else if (sch.mode === "fixed" || sch.mode === "low") {
            row.appendChild(document.createTextNode(" РЎРєРѕСЂРѕСЃС‚СЊ:"));

            var pctInput = document.createElement("input");
            pctInput.type = "number";
            pctInput.value = sch.speed_pct !== undefined ? sch.speed_pct : (sch.mode === "low" ? 25 : 50);
            pctInput.style.cssText = "width:45px;background:#1a1a2e;color:#fff;border:1px solid #333;font-size:11px";
            pctInput.min = 0; pctInput.max = 100;

            var pctSlider = document.createElement("input");
            pctSlider.type = "range";
            pctSlider.value = pctInput.value;
            pctSlider.min = 0; pctSlider.max = 100;
            pctSlider.style.cssText = "width:80px; margin: 0 5px; vertical-align: middle;";

            pctSlider.oninput = function() { pctInput.value = this.value; };
            pctSlider.onchange = function() { showSyncingStatus(); updateSchedule(k, i, "speed_pct", parseInt(this.value)); };
            pctInput.onchange = function() {
                if (this.value < 0) this.value = 0;
                if (this.value > 100) this.value = 100;
                pctSlider.value = this.value;
                showSyncingStatus();
                updateSchedule(k, i, "speed_pct", parseInt(this.value));
            };

            row.appendChild(pctSlider);
            row.appendChild(pctInput);
            row.appendChild(document.createTextNode("%"));
        }
        row.appendChild(delBtn);
        builder.appendChild(row);
    }
    schedDiv.appendChild(builder);
}

function togglePopup(key, btn) {
    var popup = document.getElementById("sensor-popup");
    if (!popup) return;
    var rect = btn.getBoundingClientRect();
    popup.style.left = rect.left + "px";
    popup.style.top = (rect.bottom + 4) + "px";
    
    var groups = {};
    allSensors.forEach(function(s) { 
        if (!groups[s.group]) groups[s.group] = []; 
        groups[s.group].push(s); 
    });
    
    var sensors = (fanConfigs[key] || {}).sensors || [];
    popup.innerHTML = "";
    
    for (var g in groups) {
        var gTitle = document.createElement("div");
        gTitle.style.cssText = "font-weight:bold;padding:3px 0;color:#aaa;font-size:11px";
        gTitle.textContent = g;
        popup.appendChild(gTitle);
        
        for (var i = 0; i < groups[g].length; i++) {
            var s = groups[g][i];
            var label = document.createElement("label");
            label.style.cssText = "display:flex;align-items:center;gap:5px;padding:2px 5px;cursor:pointer;font-size:12px";
            var cb = document.createElement("input");
            cb.type = "checkbox"; 
            cb.value = s.id; 
            cb.checked = sensors.includes(s.id);
            cb.onchange = function() { showSyncingStatus(); toggleSensor(key, this); };
            label.appendChild(cb);
            label.appendChild(document.createTextNode(s.label + " (" + (s.standby ? "Sleep" : s.temp + "Р’В°C") + ")"));
            popup.appendChild(label);
        }
    }
    popup.classList.add("show");
}

function toggleSensor(key, cb) {
    var popup = document.getElementById("sensor-popup");
    if (!popup) return;
    var checks = popup.querySelectorAll("input[type=checkbox]:checked");
    var sensors = [];
    for (var i = 0; i < checks.length; i++) sensors.push(checks[i].value);
    if (sensors.length === 0) { cb.checked = true; sensors = [cb.value]; }
    if (!fanConfigs[key]) fanConfigs[key] = {};
    fanConfigs[key].sensors = sensors;
    setFanConfig(key, "sensors", sensors);
    rebuildFanConfig(key);
}

function removeSensor(key, id) {
    var cfg = fanConfigs[key] || {};
    var sensors = (cfg.sensors || []).filter(function(s) { return s !== id; });
    cfg.sensors = sensors;
    fanConfigs[key] = cfg;
    showSyncingStatus();
    setFanConfig(key, "sensors", sensors);
    rebuildFanConfig(key);
}

function addSchedule(key) {
    var cfg = fanConfigs[key] || {}, s = cfg.schedule || [];
    s.push({day: "all", time_start: "00:00", time_end: "23:59", mode: "auto", target_temp: 31});
    cfg.schedule = s; 
    fanConfigs[key] = cfg; 
    showSyncingStatus();
    setFanConfig(key, "schedule", s);
    rebuildFanConfig(key);
}

function removeSchedule(key, i) {
    var cfg = fanConfigs[key] || {}, s = cfg.schedule || [];
    s.splice(i, 1); 
    cfg.schedule = s; 
    fanConfigs[key] = cfg; 
    showSyncingStatus();
    setFanConfig(key, "schedule", s);
    rebuildFanConfig(key);
}

function updateSchedule(key, i, field, val) {
    var cfg = fanConfigs[key] || {}, s = cfg.schedule || [];
    if (!s[i]) return;
    s[i][field] = val; 
    cfg.schedule = s; 
    fanConfigs[key] = cfg; 
    setFanConfig(key, "schedule", s);
}

function rebuildFanConfig(k) { 
    if (currentData) buildFanConfig(k, currentData); 
}

function testFan(key) {
    fetch("/api/test/start", {
        method: "POST", 
        headers: {"Content-Type": "application/json"}, 
        body: JSON.stringify({fan: key})
    });
    var tp = document.getElementById("test-progress"); 
    if (tp) tp.style.display = "block";
}

function startTest() {
    fetch("/api/test/start", {method: "POST"});
    var tp = document.getElementById("test-progress"); 
    if (tp) tp.style.display = "block";
}

function setFan(k, v) {
    var ks = safeId(k);
    
    var sliderEl = document.getElementById("slider-" + ks);
    var pwmEl = document.getElementById("pwm-" + ks);
    if (pwmEl) pwmEl.textContent = v + "%";
    
    fetch("/api/control", {
        method: "POST", 
        headers: {"Content-Type": "application/json"}, 
        body: JSON.stringify({action: "set_fan_pwm", fan: k, pwm: parseInt(v)})
    })
    .then(function() {
        setTimeout(function() {
            socket.emit('get_state');
        }, 1500);
    })
    .catch(function(err) {
        console.error('Fan control error:', err);
        socket.emit('get_state');
    });
}

function setFanConfig(k, field, val) {
    if (buildingConfig) return;
    if (!fanConfigs[k]) fanConfigs[k] = {};
    var oldVal = fanConfigs[k][field];
    if (typeof val === "object" && val !== null) { 
        if (JSON.stringify(oldVal) === JSON.stringify(val)) return; 
    } else if (oldVal === val) return;
    
    fanConfigs[k][field] = val;
    var payload = {action: "set_fan_config", fan: k}; 
    payload[field] = val;
    fetch("/api/control", {
        method: "POST", 
        headers: {"Content-Type": "application/json"}, 
        body: JSON.stringify(payload)
    });
    if (field === "fan_mode") rebuildFanConfig(k);
}

// ====================== HISTORY CHART ======================

function loadChart() {
    var ctx = document.getElementById("chart");
    if (!ctx) return;

    fetch("/api/history?hours=24")
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (!d || d.length === 0) {
                console.log("Chart: no historical data");
                return;
            }

            var chartData = {labels: [], temps: [], rpm: []};
            d.forEach(function(x) {
                chartData.labels.push(new Date(x.ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}));
                if (x.max_temp > 0) lastValidTemp = x.max_temp;
                chartData.temps.push(lastValidTemp);
                chartData.rpm.push(x.rpm || 0);
            });

            if (!chart) {
                chart = new Chart(ctx, {
                    type: "line",
                    data: {
                        labels: chartData.labels,
                        datasets: [
                            {label: "Max HDD Р’В°C", data: chartData.temps, borderColor: "#ff4444", yAxisID: "y1", tension: 0.3},
                            {label: "RPM", data: chartData.rpm, borderColor: "#00ff88", yAxisID: "y2", tension: 0.3}
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {
                            y1: {type: "linear", position: "left", title: {display: true, text: "Р’В°C"}},
                            y2: {type: "linear", position: "right", title: {display: true, text: "RPM"}}
                        }
                    }
                });
            } else {
                chart.data.labels = chartData.labels;
                if (chart.data.datasets && chart.data.datasets[0]) 
                    chart.data.datasets[0].data = chartData.temps;
                if (chart.data.datasets && chart.data.datasets[1]) 
                    chart.data.datasets[1].data = chartData.rpm;
                chart.update();
            }
        })
        .catch(function(err) {
            console.error("Chart load error:", err);
        });
}

console.log("=== Calling loadChart ===");
loadChart();
setInterval(loadChart, 60000);

document.addEventListener('click', function(e) {
    var popup = document.getElementById('sensor-popup');
    if (popup && !popup.contains(e.target) && !e.target.classList.contains('sensor-btn')) {
        popup.classList.remove('show');
    }
});

console.log("=== FanControl Web v2.9 - main.js FULLY LOADED ===");

ENDOFFILE

docker-compose build --no-cache && docker-compose up -d
echo Done!

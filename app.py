import os, re, json, time, threading, sqlite3, subprocess, logging, sys, copy
from datetime import datetime, timedelta
from pathlib import Path
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO

LOG_DIR = '/app/data/logs'
os.makedirs(LOG_DIR, exist_ok=True)
DB_FILE = '/app/data/fancontrol.db'
CONFIG_FILE = '/app/data/fan_config.json'

DATA_DIR = Path(os.getenv('FANCONTROL_DATA_DIR', '/app/data'))
HWMON_DIR = Path(os.getenv('FANCONTROL_HWMON_DIR', '/sys/class/hwmon'))
CONFIG_PATH = DATA_DIR / 'config.json'
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
        chip = (hw/'name').read_text().strip() if (hw/'name').exists() else '?'
        logger.info(f'  Chip: {hw.name} ({chip})')
        for pwmf in sorted(hw.glob('pwm*')):
            if '_' in pwmf.name: continue
            n = re.search(r'\d+', pwmf.name).group()
            fanf = hw/f'fan{n}_input'
            if not fanf.exists(): continue
            lbl = (hw/f'fan{n}_label').read_text().strip() if (hw/f'fan{n}_label').exists() else f'Fan {n}'
            w = os.access(str(pwmf), os.W_OK)
            try: current_rpm = int(Path(str(fanf)).read_text().strip())
            except: current_rpm = 0
            fans[f'{hw.name}/{pwmf.name}'] = dict(
                label=lbl, pwm_path=str(pwmf), fan_path=str(fanf),
                rpm=current_rpm, pwm_value=0, writable=w, inverted=False,
                min_rpm=0, max_rpm=0, manual_pct=50,
                sensors=['hdd:sata1'], sensor_mode='max', target_temp=31,
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
            time.sleep(0.1)
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
    
    try:
        if fan_key:
            if fan_key not in state['fans']:
                raise ValueError(f'Fan key not found: {fan_key}')
            fans_to_test = {fan_key: state['fans'][fan_key]}
        else:
            fans_to_test = state['fans']
        
        writable_fans = {k: f for k, f in fans_to_test.items() if f['writable']}
        
        if len(writable_fans) == 0:
            logger.warning('No writable fans found for calibration')
            return
        
        for k, f in writable_fans.items():
            f['inverted'] = False
            f['status'] = 'calibrating'
        
        state['test_progress'] = dict(
            status='Starting parallel calibration...',
            step=0,
            total=11,
            current='All fans'
        )
        socketio.emit('test_progress', state['test_progress'])
        
        pwm_steps = [0, 26, 51, 77, 102, 128, 153, 179, 204, 230, 255]
        raw_data = {k: [] for k in writable_fans}
        
        for step_idx, p in enumerate(pwm_steps):
            pct = step_idx * 10
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
                except:
                    rpm = 0
                return key, rpm
            
            futures = [executor.submit(read_one_rpm, (k, f)) for k, f in writable_fans.items()]
            for future in futures:
                try:
                    key, rpm = future.result(timeout=2)
                    raw_data[key].append(dict(pwm=p, rpm=rpm, pct=pct))
                    state['fans'][key]['rpm'] = rpm
                except Exception as ex:
                    logger.error(f'RPM read error: {ex}')
            
            socketio.emit('update', get_state())
            socketio.sleep(0)
        
        for k, fan in writable_fans.items():
            raw = raw_data[k]
            all_rpm = [pt['rpm'] for pt in raw]
            max_rpm = max(all_rpm)
            
            if max_rpm == 0:
                logger.warning(f"Fan {fan['label']} ({k}): No RPM detected")
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
            logger.info(f'Fan {fan["label"]}: status={fan["status"]}, min={fan["min_rpm"]}rpm, max={fan["max_rpm"]}rpm')
        
    except Exception as e:
        logger.error(f'Test error: {e}')
        test_successful = False
    finally:
        state.update(testing=False, tested=test_successful)
        if test_successful:
            state['initialized'] = True
            save_config()
            logger.info('System initialized successfully')
        state['_pause_loop'] = False
        state['test_progress'] = dict(status='Ready!' if test_successful else 'Completed with errors', step=0, total=0, current='')
        socketio.emit('test_progress', state['test_progress'])
        socketio.emit('test_complete', {'success': test_successful, 'initialized': state['initialized']})

# ================ MAIN CONTROL LOOP ================

def refresh():
    for s in state['temp_sensors'].values():
        try: s['value'] = int(Path(s['path']).read_text().strip()) // 1000
        except: pass

    def poll_fan(item):
        k, f = item
        try: f['rpm'] = int(Path(f['fan_path']).read_text().strip())
        except Exception as e: logger.error(f'Fan poll error {k}: {e}')

    futures = [executor.submit(poll_fan, item) for item in state['fans'].items()]
    for future in futures:
        try: future.result(timeout=2)
        except: pass

def refresh_disks():
    now = time.time()
    if state['last_hdd_poll'] > 0 and now - state['last_hdd_poll'] < 30: return
    futures_map = {disk_id: executor.submit(read_disk_temp, info.get('dev_name', disk_id)) for disk_id, info in state['hdd_sensors'].items()}
    valid = []
    for disk_id, future in futures_map.items():
        try:
            result = future.result(timeout=15)
            t, standby = result if result else (None, False)
            if t is not None and t > 0:
                state['hdd_sensors'][disk_id].update(temp=t, standby=standby)
                valid.append(t)
            elif state['hdd_sensors'][disk_id].get('temp', 0) > 0:
                valid.append(state['hdd_sensors'][disk_id]['temp'])
        except Exception as e:
            logger.error(f'Poll error for {disk_id}: {e}')
    state['last_hdd_poll'] = time.time()
    active = [v['temp'] for v in state['hdd_sensors'].values() if v.get('temp', 0) > 0 and not v.get('standby')]
    state['max_hdd_temp'] = max(active) if active else (max(valid) if valid else 0)
    state['failsafe'] = not bool(active or (valid and state['max_hdd_temp'] > 0))

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
    sensors, mode = fan.get('sensors', ['hdd:sata1']), fan.get('sensor_mode', 'max')
    temps = []
    for s in sensors:
        if s.startswith('hdd:'):
            t = state['hdd_sensors'].get(s.split(':', 1)[1], {}).get('temp', 0)
        elif s.startswith('temp:'):
            t = state['temp_sensors'].get(s.split(':', 1)[1], {}).get('value', 0)
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
    global _failed_calibration_logged
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
                    with sqlite3.connect(DB_FILE, timeout=30) as conn:
                        fc = len(state['fans'])
                        ap = sum(f.get('raw_pwm', f.get('pwm_value', 0)) for f in state['fans'].values()) // fc if fc > 0 else 0
                        ar = sum(f.get('rpm', 0) for f in state['fans'].values()) // fc if fc > 0 else 0
                        conn.execute('INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?, ?)',
                                   (datetime.now().isoformat(), 'auto', ap, ar, state['max_hdd_temp'], fc, len(state['hdd_sensors'])))
                        conn.commit()
                except sqlite3.OperationalError as e:
                    logger.error(f'SQLite write error: {e}')
                last_log = current_time
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
            tmp_path = CONFIG_PATH.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state.get('config', {}), f, indent=4, ensure_ascii=False)
            tmp_path.replace(CONFIG_PATH)
            logger.info("Configuration saved atomically.")
        except Exception as e:
            logger.error(f"Failed to save config atomically: {e}")

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
            with state_lock:
                state['fans'][data['fan']]['manual_pct'] = data['pwm']
            # ... existing logic to write to sysfs ...
        elif data['action'] == 'set_fan_config':
            # ... existing config update logic ...

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

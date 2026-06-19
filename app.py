#!/usr/bin/env python3
"""
FanControl Web v3.3.6 - Neon Cyberpunk Edition
Modern fan control with real-time monitoring and intelligent thermal management
"""

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
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Any

from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO
from werkzeug.exceptions import BadRequest

from core.state import (
    state, state_lock, CONFIG_VERSION, get_state,
    invalidate_state_cache, _init_complete,
)
from core.hardware import (
    CALIBRATION_STEPS, CALIBRATION_SETTLE_TIME,
    executor,
    discover_fans_and_sensors, discover_disks,
    set_pwm, refresh,
)
from core.control import (
    SENSOR_FAILURE_TEMP, MIN_PWM_PCT, MAX_PWM_PCT,
    UNINITIALIZED_POLL_INTERVAL,
    get_db_connection,
    refresh_disks,
    loop, fan_temp, pwm_from_curve, process_auto_mode,
    _evaluate_fan_mode, log_telemetry, cleanup_logs,
)

# ============================================================================
# CONFIGURATION & INITIALIZATION
# ============================================================================

LOG_DIR = '/app/data/logs'
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

DATA_DIR = Path(os.getenv('FANCONTROL_DATA_DIR', '/app/data'))
CONFIG_PATH = DATA_DIR / 'config.json'
DB_FILE = DATA_DIR / 'fancontrol.db'

# Logger setup
logger = logging.getLogger('fancontrol')
logger.setLevel(logging.DEBUG)
fmt = logging.Formatter(
    '%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(fmt)
logger.addHandler(console_handler)

file_handler = RotatingFileHandler(
    f'{LOG_DIR}/fancontrol.log',
    maxBytes=10*1024*1024,
    backupCount=5,
    encoding='utf-8'
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(fmt)
logger.addHandler(file_handler)

# Flask & SocketIO
app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS_ORIGINS = os.getenv('FANCONTROL_CORS_ORIGINS', 'http://localhost:5059,http://127.0.0.1:5059').split(',')

socketio = SocketIO(
    app,
    cors_allowed_origins=CORS_ORIGINS,
    async_mode='threading',
    logger=False,
    engineio_logger=False,
    ping_timeout=120,
    ping_interval=25
)

# ============================================================================
# STATE MANAGEMENT
# ============================================================================

MAX_HISTORY_HOURS = 168

PWM_CURVE_POINTS = 11



# Rate limiting for control endpoints
_control_rate_limit: Dict[str, float] = {}
CONTROL_RATE_LIMIT_SECONDS = 0.1
_RATE_LIMIT_CLEANUP_INTERVAL = 600
_rate_limit_last_cleanup = time.monotonic()

# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    """Serve the main dashboard"""
    return render_template('index.html')


@app.route('/js/<path:filename>')
def serve_js(filename):
    """Serve JavaScript files from templates/js"""
    return send_from_directory(os.path.join(app.root_path, 'templates', 'js'), filename)


@app.route('/api/state')
def api_get_state():
    """REST endpoint for current state (debugging/health checks)"""
    return jsonify(get_state())


@app.route('/api/lang/<code>')
def api_get_lang(code):
    """Serve translation file"""
    lang_file = Path(app.static_folder) / 'lang' / f'{code}.json'
    if lang_file.exists():
        with open(lang_file, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    return jsonify({}), 404


@app.route('/api/language', methods=['POST'])
def api_set_language():
    """Save language preference to config"""
    try:
        data = request.get_json(force=True)
        lang = data.get('language', 'en')
        if lang not in ('en', 'ru'):
            return jsonify({"status": "error", "message": "Unsupported language"}), 400
        
        with state_lock:
            state['language'] = lang
        
        save_config()
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f'Language save error: {e}', exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/discover', methods=['POST'])
def api_discover():
    """Scan hardware for fans, sensors, and disks"""
    try:
        logger.info("Starting hardware discovery...")
        fans, temps = discover_fans_and_sensors()
        disks = discover_disks()
        
        with state_lock:
            state['fans'] = fans
            state['temp_sensors'] = temps
            state['hdd_sensors'] = disks
            state['hardware_scanned'] = True
        
        socketio.emit('hardware_discovered', {
            'fans': fans,
            'temps': temps,
            'disks': disks
        })
        
        logger.info(f"Discovery complete: {len(fans)} fans, {len(temps)} sensors, {len(disks)} disks")
        return jsonify({'status': 'ok', 'fans': fans, 'temps': temps, 'disks': disks})
        
    except Exception as e:
        logger.error(f'Discovery failed: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/initialize', methods=['POST'])
def api_initialize():
    """Start fan calibration"""
    try:
        if state.get('testing'):
            return jsonify({'status': 'error', 'message': 'Calibration already running'}), 409
        
        with state_lock:
            state['testing'] = True
            state['test_progress'] = {
                'status': 'Starting calibration...',
                'step': 0,
                'total': PWM_CURVE_POINTS,
                'current': ''
            }
        
        threading.Thread(target=test_fans, daemon=True).start()
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        logger.error(f'Initialization error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/test/start', methods=['POST'])
def api_test_start():
    """Start individual fan test"""
    try:
        data = request.get_json(silent=True) or {}
        fan_key = data.get('fan')
        
        if state.get('testing'):
            return jsonify({'status': 'error', 'message': 'Test already running'}), 409
        
        with state_lock:
            state['testing'] = True
            state['test_progress'] = {
                'status': 'Starting test...',
                'step': 0,
                'total': PWM_CURVE_POINTS,
                'current': ''
            }
        
        threading.Thread(target=test_fans, args=(fan_key,), daemon=True).start()
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        logger.error(f'Test start error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/history')
def api_history():
    """Return history data optimized for ApexCharts"""
    try:
        hours = max(1, min(request.args.get('hours', 24, type=int), MAX_HISTORY_HOURS))
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        with get_db_connection() as conn:
            cursor = conn.execute(
                'SELECT ts, mode, pwm, rpm, max_temp FROM logs WHERE ts > ? ORDER BY ts',
                (since,)
            )
            rows = cursor.fetchall()
        
        timestamps = []
        temps = []
        pwm_speeds = []
        
        for row in rows:
            timestamps.append(row[0])
            temps.append(row[4] if row[4] > 0 else None)
            pwm_speeds.append(row[2])
        
        return jsonify({
            'has_data': len(timestamps) > 0,
            'timestamps': timestamps,
            'temps': temps,
            'pwm': pwm_speeds
        })
        
    except sqlite3.OperationalError as e:
        logger.error(f'Database read error: {e}')
        return jsonify({'has_data': False, 'timestamps': [], 'temps': [], 'pwm': []}), 503


@app.route('/api/update/check')
def api_update_check():
    """Check for updates via GitHub API"""
    try:
        current_hash = os.getenv('FANCONTROL_GIT_HASH', '')
        current_version = CONFIG_VERSION

        # DNS resolution outside eventlet
        import http.client, ssl

        host = 'api.github.com'
        try:
            result = subprocess.run(
                ['python3', '-c', f'import socket; print(socket.getaddrinfo("{host}", 443, socket.AF_INET)[0][4][0])'],
                capture_output=True, text=True, timeout=10
            )
            ip = result.stdout.strip()
            if not ip:
                raise Exception(f'DNS failed: {result.stderr.strip()}')
        except Exception as e:
            logger.error(f'DNS resolution failed: {e}')
            return jsonify({'status': 'error', 'message': str(e)}), 500

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        repo = os.getenv('FANCONTROL_REPO', 'Biowolfx/fancontrol-web')
        headers = {
            'Host': host,
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'fancontrol-web'
        }

        # 1. Get latest commit info
        conn = http.client.HTTPSConnection(ip, 443, timeout=15, context=ctx)
        conn.request('GET', f'/repos/{repo}/commits/main', headers=headers)
        commit_data = json.loads(conn.getresponse().read())
        conn.close()

        remote_hash = commit_data['sha'][:8]
        commit_msg = commit_data['commit']['message'].split('\n')[0]

        remote_version = ''
        # Try to extract version from commit message (e.g. "v3.3.3" or "3.3.3")
        m_ver = re.search(r'[vV]?(\d+\.\d+\.\d+)', commit_msg)
        if m_ver:
            remote_version = m_ver.group(1)
        
        # Determine if update is available
        # If remote version extracted from commit message → compare versions
        # Otherwise → compare hashes
        if remote_version and current_version:
            has_update = remote_version != current_version
        else:
            has_update = current_hash != remote_hash and current_hash != ''

        return jsonify({
            'status': 'ok',
            'has_update': has_update,
            'current_version': current_version,
            'remote_version': remote_version or 'unknown',
            'current_hash': current_hash or 'unknown',
            'remote_hash': remote_hash,
            'commit_message': commit_msg
        })

    except Exception as e:
        logger.error(f'Update check error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Pull latest code and restart container. Entrypoint syncs code from /repo."""
    try:
        repo_dir = '/repo'

        # Git pull (public repo, no auth needed)
        pull = subprocess.run(
            ['git', '-C', repo_dir, 'pull', '--ff-only', 'origin', 'main'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )

        pull_output = pull.stdout.strip() + '\n' + pull.stderr.strip()
        already_up = 'Already up to date' in pull_output or 'Already up-to-date' in pull_output

        if pull.returncode != 0 and not already_up:
            logger.error(f'Git pull failed: {pull_output}')
            return jsonify({'status': 'error', 'message': pull_output.strip()}), 500

        logger.info(f'Git pull result: {pull_output.strip()}')

        # Check if requirements changed
        old_req = ''
        new_req = ''
        try:
            # Read pre-pull requirements from current running code
            with open('/app/requirements.txt', 'r') as f:
                old_req = f.read()
        except Exception:
            pass
        try:
            with open(os.path.join(repo_dir, 'requirements.txt'), 'r') as f:
                new_req = f.read()
        except Exception:
            pass
        deps_changed = old_req != new_req

        # Restart container — entrypoint will sync /repo → /app
        container_name = os.getenv('HOSTNAME', 'fancontrol-web')
        restart = subprocess.run(
            ['docker', 'restart', container_name],
            capture_output=True, text=True, timeout=30
        )
        logger.info(f'Docker restart: {restart.stdout.strip()} {restart.stderr.strip()}')

        return jsonify({
            'status': 'ok',
            'already_up_to_date': already_up,
            'deps_changed': deps_changed,
            'message': pull_output.strip()
        })

    except Exception as e:
        logger.error(f'Update apply error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/control', methods=['POST'])
def handle_control():
    """Handle fan control commands with validation"""
    global _rate_limit_last_cleanup
    try:
        data = request.get_json(force=True)
        validate_control_request(data)
        
        fan_key = data.get('fan', '')
        now = time.monotonic()
        last_time = _control_rate_limit.get(fan_key, 0)
        if now - last_time < CONTROL_RATE_LIMIT_SECONDS:
            return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
        _control_rate_limit[fan_key] = now
        
        if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
            expired = [k for k, t in _control_rate_limit.items() if now - t > 60]
            for k in expired:
                del _control_rate_limit[k]
            _rate_limit_last_cleanup = now
        
        if data['action'] == 'set_fan_pwm':
            return _handle_set_pwm(data)
        elif data['action'] == 'set_fan_config':
            return _handle_set_config(data)
        else:
            return jsonify({"status": "error", "message": f"Unknown action: {data['action']}"}), 400
            
    except BadRequest as br:
        return jsonify({"status": "error", "message": br.description}), 400
    except Exception as e:
        logger.error(f'Control error: {e}', exc_info=True)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


def _handle_set_pwm(data: dict) -> dict:
    """Handle PWM change request atomically"""
    fan_key = data['fan']
    pwm_val = int(data['pwm'])
    physical_pwm = int(pwm_val * 255 // 100)
    
    with state_lock:
        fan = state['fans'].get(fan_key)
        if not fan:
            raise BadRequest(f"Fan '{fan_key}' not found")
        
        updated_fan = fan.copy()
        updated_fan['manual_pct'] = pwm_val
        updated_fan['mode'] = 'manual'
        updated_fan['status'] = 'nominal'
        state['fans'][fan_key] = updated_fan
    
    set_pwm(fan_key, physical_pwm)
    save_config()
    return jsonify({"status": "success"})


def _handle_set_config(data: dict) -> dict:
    """Handle fan configuration change atomically"""
    fan_key = data['fan']
    
    with state_lock:
        fan = state['fans'].get(fan_key)
        if not fan:
            raise BadRequest(f"Fan '{fan_key}' not found")
        
        updated_fan = fan.copy()
        
        for key in ['schedule', 'sensors', 'sensor_mode', 'target_temp']:
            if key in data:
                updated_fan[key] = data[key]
        
        if 'fan_mode' in data:
            updated_fan['mode'] = data['fan_mode']
        
        state['fans'][fan_key] = updated_fan
        
        should_set_manual = (data.get('fan_mode') == 'manual')
        captured_manual_pct = updated_fan.get('manual_pct', 50)
    
    if should_set_manual:
        set_pwm(fan_key, int(captured_manual_pct * 255 // 100))
    
    save_config()
    return jsonify({"status": "success"})

# ============================================================================
# FAN CALIBRATION
# ============================================================================

def _detect_inversion(raw: List[Dict], fan_label: str) -> bool:
    """
    Detect if a fan has inverted PWM control using 3 methods.
    Returns True if inverted.
    """
    all_rpm = [pt['rpm'] for pt in raw]
    max_rpm = max(all_rpm)
    
    low_rpm_avg = sum(pt['rpm'] for pt in raw[:3]) / 3 if raw[:3] else 0
    high_rpm_avg = sum(pt['rpm'] for pt in raw[-3:]) / 3 if raw[-3:] else 0
    
    rpm_at_zero = raw[0]['rpm'] if raw else 0
    rpm_at_max = raw[-1]['rpm'] if raw else 0
    
    half = len(raw) // 2
    first_half_avg = sum(pt['rpm'] for pt in raw[:half]) / max(half, 1)
    second_half_avg = sum(pt['rpm'] for pt in raw[half:]) / max(len(raw) - half, 1)
    
    logger.info(
        f'Fan {fan_label} inversion check: '
        f'rpm@PWM0={rpm_at_zero}, rpm@PWM255={rpm_at_max}, '
        f'low3_avg={low_rpm_avg:.0f}, high3_avg={high_rpm_avg:.0f}, '
        f'first_half_avg={first_half_avg:.0f}, second_half_avg={second_half_avg:.0f}'
    )
    
    is_inverted = False
    if rpm_at_max > 0 and rpm_at_zero > rpm_at_max * 1.1:
        is_inverted = True
    elif high_rpm_avg > 0 and low_rpm_avg > high_rpm_avg * 1.1:
        is_inverted = True
    elif second_half_avg > 0 and first_half_avg > second_half_avg * 1.1:
        is_inverted = True
    
    logger.info(f'Fan {fan_label} inversion result: {"INVERTED" if is_inverted else "normal"}')
    return is_inverted


def _normalize_curve(raw: List[Dict], is_inverted: bool) -> List[Dict]:
    """
    Normalize calibration curve. If inverted, remap PWM so low pct = low RPM.
    Returns sorted curve.
    """
    if is_inverted:
        return sorted(
            [{'pwm': 255 - pt['pwm'], 'rpm': pt['rpm'], 'pct': 100 - pt['pct']}
             for pt in raw],
            key=lambda x: x['pwm']
        )
    return sorted(raw, key=lambda x: x['pwm'])


def test_fans(fan_key: Optional[str] = None):
    """
    Calibrate fans by testing PWM/RPM curve.
    Uses parallel testing for efficiency.
    """
    global _failed_calibration_logged
    test_successful = True
    
    with state_lock:
        if fan_key:
            if fan_key not in state['fans']:
                raise ValueError(f'Fan key not found: {fan_key}')
            fans_to_test = {fan_key: copy.deepcopy(state['fans'][fan_key])}
        else:
            fans_to_test = {
                k: copy.deepcopy(v)
                for k, v in state['fans'].items()
            }
    
    writable_fans = {k: f for k, f in fans_to_test.items() if f.get('writable')}
    
    if not writable_fans:
        logger.warning('No writable fans found for calibration')
        return
    
    for f in writable_fans.values():
        f['inverted'] = False
        f['status'] = 'calibrating'
    
    with state_lock:
        state['testing'] = True
        state['_pause_loop'] = True
        state['test_progress'] = {
            'status': 'Starting parallel calibration...',
            'step': 0,
            'total': PWM_CURVE_POINTS,
            'current': 'All fans'
        }
    
    socketio.emit('test_progress', state['test_progress'])
    
    pwm_steps = CALIBRATION_STEPS
    raw_data = {k: [] for k in writable_fans}
    
    try:
        for step_idx, pwm_value in enumerate(pwm_steps):
            pct = round(pwm_value * 100 / 255)
            
            with state_lock:
                state['test_progress'].update({
                    'step': step_idx + 1,
                    'status': f'Testing level {pct}%',
                    'current': 'Parallel mode'
                })
            socketio.emit('test_progress', state['test_progress'])
            
            for k in writable_fans:
                with state_lock:
                    set_pwm(k, pwm_value, raw=True)
            
            time.sleep(CALIBRATION_SETTLE_TIME)
            
            def read_rpm(item):
                key, fan = item
                try:
                    rpm = int(Path(fan['fan_path']).read_text().strip())
                except Exception:
                    rpm = 0
                return key, rpm
            
            futures = [
                executor.submit(read_rpm, (k, f))
                for k, f in writable_fans.items()
            ]
            
            for future in futures:
                try:
                    key, rpm = future.result(timeout=2)
                    raw_data[key].append({
                        'pwm': pwm_value,
                        'rpm': rpm,
                        'pct': pct
                    })
                    
                    if rpm is not None:
                        with state_lock:
                            if key in state['fans']:
                                state['fans'][key]['rpm'] = rpm
                                
                except Exception as ex:
                    logger.error(f'RPM read error: {ex}')
            
            socketio.emit('update', get_state())
        
        for k, fan in writable_fans.items():
            raw = raw_data.get(k, [])
            
            if not raw:
                logger.warning(f'Fan {fan.get("label", k)} ({k}): No RPM data')
                fan.update({
                    'status': 'not_connected',
                    'min_rpm': 0,
                    'max_rpm': 0,
                    'curve': [],
                    'calibration': {}
                })
                with state_lock:
                    set_pwm(k, 128, raw=True)
                continue
            
            all_rpm = [pt['rpm'] for pt in raw]
            max_rpm = max(all_rpm)
            
            if max_rpm == 0:
                logger.warning(f"Fan {fan.get('label', k)} ({k}): No RPM detected")
                fan.update({
                    'status': 'not_connected',
                    'min_rpm': 0,
                    'max_rpm': 0,
                    'curve': [],
                    'calibration': {}
                })
                with state_lock:
                    set_pwm(k, 128, raw=True)
                continue
            
            is_inverted = _detect_inversion(raw, fan.get('label', k))
            
            if is_inverted:
                fan['inverted'] = True
                fan['status'] = 'inverted'
            else:
                fan['inverted'] = False
                fan['status'] = 'normal'
            
            fan['curve'] = _normalize_curve(raw, is_inverted)
            
            min_threshold = max_rpm * 0.05
            
            # Find min_pct from the actual curve used (normalized or not)
            real_min = next(
                (pt for pt in fan['curve'] if pt['rpm'] > min_threshold),
                fan['curve'][0]
            )
            cal_min_pct = real_min['pct']
            
            fan.update({
                'min_rpm': real_min['rpm'],
                'max_rpm': max_rpm,
                'calibration': {
                    'min_rpm': real_min['rpm'],
                    'max_rpm': max_rpm,
                    'min_pct': cal_min_pct,
                    'inverted': fan['inverted']
                }
            })
            
            set_pwm(k, 128)
            fan['manual_pct'] = 50
            fan['current_pct'] = 50
            fan['target_pwm'] = 50
            logger.info(
                f'Fan {fan.get("label", k)}: status={fan["status"]}, '
                f'min={fan["min_rpm"]}rpm, max={fan["max_rpm"]}rpm'
            )
        
        with state_lock:
            for k, fan in writable_fans.items():
                if k in state['fans']:
                    state['fans'][k].update(fan)
    
    except Exception as e:
        logger.error(f'Test error: {e}', exc_info=True)
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
            state['test_progress'] = {
                'status': 'Ready!' if test_successful else 'Completed with errors',
                'step': 0,
                'total': 0,
                'current': ''
            }
            current_initialized = state['initialized']
        
        socketio.emit('test_progress', state['test_progress'])
        socketio.emit('test_complete', {
            'success': test_successful,
            'initialized': current_initialized
        })

# ============================================================================
# CONFIGURATION MANAGEMENT
# ============================================================================

FAN_FIELDS = [
    'id', 'label', 'hw_path', 'pwm_path', 'fan_path',
    'inverted', 'min_rpm', 'max_rpm', 'manual_pct',
    'sensors', 'sensor_mode', 'target_temp', 'mode',
    'status', 'target_pwm', 'current_pct',
    'schedule', 'curve', 'calibration'
]

SAVE_DEBOUNCE_SECONDS = 0.5
_save_timer: Optional[threading.Timer] = None
_save_lock = threading.Lock()


def migrate_config(cfg: Dict) -> Dict:
    """Migrate old config format to v3.0"""
    if 'config_version' not in cfg:
        logger.info('Migrating config to v3.0 format...')
        cfg['config_version'] = CONFIG_VERSION
        
        fans = cfg.get('fans', {})
        for fan_id, fan in fans.items():
            if 'fan_mode' in fan:
                fan['mode'] = fan.pop('fan_mode')
            fan.setdefault('mode', 'manual')
            fan.setdefault('id', fan_id)
            fan.setdefault('status', 'nominal')
            fan.setdefault('target_pwm', fan.get('manual_pct', 50))
            fan.setdefault('hw_path', '')
            
            schedule = fan.get('schedule', [])
            for item in schedule:
                if 'fan_mode' in item:
                    item['mode'] = item.pop('fan_mode')
                item.setdefault('mode', 'auto')
    
    return cfg


def _do_save_config():
    """Actually write config to disk."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        config = {
            'config_version': CONFIG_VERSION,
            'initialized': state.get('initialized', False),
            'tested': state.get('tested', False),
            'language': state.get('language', 'en'),
            'fans': {}
        }
        
        with state_lock:
            for fan_id, fan in state.get('fans', {}).items():
                config['fans'][fan_id] = {
                    field: fan.get(field)
                    for field in FAN_FIELDS
                    if field in fan
                }
        
        tmp_path = CONFIG_PATH.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        tmp_path.replace(CONFIG_PATH)
        
        logger.info('Configuration saved successfully')
        
    except Exception as e:
        logger.error(f'Failed to save config: {e}', exc_info=True)


def save_config():
    """Save configuration with debounce to avoid excessive disk writes."""
    global _save_timer
    with _save_lock:
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(SAVE_DEBOUNCE_SECONDS, _do_save_config)
        _save_timer.daemon = True
        _save_timer.start()


def load_config():
    """Load and migrate configuration"""
    try:
        if not CONFIG_PATH.exists():
            logger.info('No configuration found')
            return
        
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        
        cfg = migrate_config(cfg)
        
        # Fields that are runtime-only, should NOT be loaded from config
        RUNTIME_ONLY_FIELDS = {'rpm', 'pwm_value', 'raw_pwm', 'last_update', 'current_pct', 'target_pwm'}
        
        with state_lock:
            if isinstance(cfg, dict):
                fans = cfg.get('fans', {})
                
                for fan_id, fan_cfg in fans.items():
                    if fan_id in state['fans']:
                        for key, val in fan_cfg.items():
                            if key not in RUNTIME_ONLY_FIELDS:
                                state['fans'][fan_id][key] = val
                    else:
                        state['fans'][fan_id] = fan_cfg
                
                state['initialized'] = bool(cfg.get('initialized', False))
                state['tested'] = bool(cfg.get('tested', False))
                state['language'] = cfg.get('language', 'en')
                
            logger.info('Configuration loaded successfully')
            
    except Exception as e:
        logger.error(f'Failed to load config: {e}', exc_info=True)

# ============================================================================
# VALIDATION
# ============================================================================

def validate_control_request(data: Dict):
    """Validate incoming control request"""
    if not data or not isinstance(data, dict):
        raise BadRequest("Invalid JSON payload structure")
    
    action = data.get('action')
    if action not in ['set_fan_pwm', 'set_fan_config']:
        raise BadRequest(f"Unsupported action: {action}")
    
    fan_key = data.get('fan')
    if not fan_key:
        raise BadRequest("Missing fan identifier")
    
    with state_lock:
        if fan_key not in state.get('fans', {}):
            raise BadRequest(f"Fan '{fan_key}' not found")
    
    if action == 'set_fan_pwm':
        pwm_val = data.get('pwm')
        if pwm_val is None or not isinstance(pwm_val, (int, float)):
            raise BadRequest("PWM value must be a number")
        if not (0 <= pwm_val <= 100):
            raise BadRequest("PWM value must be between 0 and 100")
    
    elif action == 'set_fan_config':
        if 'target_temp' in data:
            t = data['target_temp']
            if not isinstance(t, (int, float)) or not (20 <= t <= 60):
                raise BadRequest("target_temp must be between 20 and 60")
        
        if 'sensor_mode' in data:
            if data['sensor_mode'] not in ('max', 'min', 'avg'):
                raise BadRequest("sensor_mode must be 'max', 'min', or 'avg'")
        
        if 'schedule' in data:
            if not isinstance(data['schedule'], list):
                raise BadRequest("Schedule must be a list of rules")
            valid_days = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'all', 'weekday', 'weekend'}
            valid_modes = {'auto', 'off', 'manual'}
            for rule in data['schedule']:
                if not isinstance(rule, dict) or 'mode' not in rule:
                    raise BadRequest("Invalid rule structure in schedule")
                if rule.get('mode') not in valid_modes:
                    raise BadRequest(f"Schedule mode must be one of: {valid_modes}")
                if rule.get('day') and rule['day'] not in valid_days:
                    raise BadRequest(f"Invalid day: {rule['day']}")
                if rule['mode'] == 'manual' and 'speed_pct' in rule:
                    speed = rule['speed_pct']
                    if not isinstance(speed, (int, float)) or not (0 <= speed <= 100):
                        raise BadRequest("Schedule speed_pct must be between 0 and 100")
                if rule['mode'] == 'auto' and 'target_temp' in rule:
                    t = rule['target_temp']
                    if not isinstance(t, (int, float)) or not (20 <= t <= 60):
                        raise BadRequest("Schedule target_temp must be between 20 and 60")
                if 'sensor_mode' in rule and rule['sensor_mode'] not in ('max', 'min', 'avg'):
                    raise BadRequest("Schedule sensor_mode must be 'max', 'min', or 'avg'")
                if 'sensors' in rule and not isinstance(rule['sensors'], list):
                    raise BadRequest("Schedule sensors must be a list")

# ============================================================================
# SOCKET.IO HANDLERS
# ============================================================================

@socketio.on('connect')
def handle_socket_connect():
    """Send initial state on client connection.
    Wait for init_hardware() to complete so the client always
    receives the correct 'initialized' flag (avoids wizard flash)."""
    _init_complete.wait(timeout=15)
    socketio.emit('update', get_state())


@socketio.on('get_state')
def handle_get_state():
    """Handle state request from client"""
    socketio.emit('update', get_state())

# ============================================================================
# ENTRY POINT
# ============================================================================
# INITIALIZATION
# ============================================================================

def init_database():
    """Initialize SQLite database and schema"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                ts TEXT,
                mode TEXT,
                pwm INTEGER,
                rpm INTEGER,
                max_temp INTEGER,
                fan_count INTEGER,
                disk_count INTEGER
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)')
        conn.commit()
    logger.info('Database initialized')


def init_hardware():
    """Discover hardware and load configuration"""
    if CONFIG_PATH.exists():
        try:
            state['fans'], state['temp_sensors'] = discover_fans_and_sensors()
            state['hdd_sensors'] = discover_disks()
            refresh()
            load_config()
            
            if state['initialized']:
                logger.info('System restored from saved configuration')
            else:
                logger.warning('Configuration exists but initialization incomplete')
        except Exception as e:
            logger.error(f'Startup error: {e}', exc_info=True)
    else:
        state['initialized'] = False
        logger.info('No configuration found - wizard mode')


_control_loop_started = False

def _ensure_control_loop():
    """Start control loop once (safe for gunicorn workers)"""
    global _control_loop_started
    if not _control_loop_started:
        _control_loop_started = True
        threading.Thread(target=loop, args=(socketio,), daemon=True).start()


@app.before_request
def _auto_init():
    """Auto-initialize on first request when running under gunicorn"""
    if not state.get('_gunicorn_initialized'):
        state['_gunicorn_initialized'] = True
        try:
            init_database()
        except Exception as e:
            logger.error(f'Database init error: {e}')
        init_hardware()
        _ensure_control_loop()
        _init_complete.set()
        # Invalidate cached state and push correct state to all connected clients
        invalidate_state_cache()
        socketio.emit('update', get_state())


if __name__ == '__main__':
    logger.info('=' * 60)
    logger.info(f'STARTING FanControl Web {CONFIG_VERSION} - Neon Cyberpunk Edition')
    logger.info('=' * 60)
    
    init_database()
    init_hardware()
    _init_complete.set()
    _ensure_control_loop()
    
    logger.info('Starting server on port 5059')
    socketio.run(app, host='0.0.0.0', port=5059)
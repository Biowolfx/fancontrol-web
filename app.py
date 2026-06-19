#!/usr/bin/env python3
"""
FanControl Web v3.3.6 - Neon Cyberpunk Edition
Modern fan control with real-time monitoring and intelligent thermal management
"""

import copy
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO
from werkzeug.exceptions import BadRequest

# ============================================================================
# CONFIGURATION & INITIALIZATION
# ============================================================================

LOG_DIR = '/app/data/logs'
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

DATA_DIR = Path(os.getenv('FANCONTROL_DATA_DIR', '/app/data'))
HWMON_DIR = Path(os.getenv('FANCONTROL_HWMON_DIR', '/sys/class/hwmon'))
CONFIG_PATH = DATA_DIR / 'config.json'
DB_FILE = DATA_DIR / 'fancontrol.db'

# Thread safety
state_lock = threading.RLock()

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

CONFIG_VERSION = "3.3.10"
MAX_HISTORY_HOURS = 168
SENSOR_FAILURE_TEMP = 99

state: Dict[str, Any] = {
    'fans': {},
    'temp_sensors': {},
    'hdd_sensors': {},
    'max_hdd_temp': 0,
    'tested': False,
    'testing': False,
    'test_progress': {},
    '_pause_loop': False,
    'failsafe': False,
    'standby_mode': False,
    'disks_polling': False,
    'last_hdd_poll': 0.0,
    'initialized': False,
    'hardware_scanned': False,
    'config_version': CONFIG_VERSION
}

executor = ThreadPoolExecutor(max_workers=8)
_last_cleanup = time.monotonic()
_failed_calibration_logged = False

PWM_CURVE_POINTS = 11
MIN_PWM_PCT = 20
MAX_PWM_PCT = 100

CALIBRATION_STEPS = [0, 26, 51, 77, 102, 128, 153, 179, 204, 230, 255]
CALIBRATION_SETTLE_TIME = 5
CONTROL_LOOP_INTERVAL = 5
UNINITIALIZED_POLL_INTERVAL = 10
TELEMETRY_LOG_INTERVAL = 300
LOG_CLEANUP_INTERVAL = 86400
DISK_POLL_COOLDOWN = 30


def get_db_connection() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_FILE, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn

# Rate limiting for control endpoints
_control_rate_limit: Dict[str, float] = {}
CONTROL_RATE_LIMIT_SECONDS = 0.1
_RATE_LIMIT_CLEANUP_INTERVAL = 600
_rate_limit_last_cleanup = time.monotonic()

# Cached state snapshot (refreshed every STATE_CACHE_TTL seconds)
STATE_CACHE_TTL = 2.0
_cached_state: Optional[Dict[str, Any]] = None
_cached_state_time: float = 0.0

# ============================================================================
# THREAD-SAFE STATE ACCESSOR
# ============================================================================

def _build_state_snapshot() -> Dict[str, Any]:
    """Build a fresh state snapshot (caller must hold state_lock)."""
    return {
        'fans': copy.deepcopy(state['fans']),
        'temp_sensors': copy.deepcopy(state['temp_sensors']),
        'hdd_sensors': copy.deepcopy(state['hdd_sensors']),
        'max_hdd_temp': state.get('max_hdd_temp', 0),
        'tested': state.get('tested', False),
        'testing': state.get('testing', False),
        'test_progress': copy.deepcopy(state.get('test_progress', {})),
        '_pause_loop': state.get('_pause_loop', False),
        'failsafe': state.get('failsafe', False),
        'standby_mode': state.get('standby_mode', False),
        'initialized': state.get('initialized', False),
        'hardware_scanned': state.get('hardware_scanned', False),
        'config_version': CONFIG_VERSION,
        'language': state.get('language', 'en')
    }


def get_state() -> Dict[str, Any]:
    """
    Thread-safe snapshot of global state for API and Socket.IO.
    Uses cached snapshot to reduce deepcopy overhead on frequent calls.
    """
    global _cached_state, _cached_state_time
    now = time.monotonic()
    
    with state_lock:
        if _cached_state is not None and (now - _cached_state_time) < STATE_CACHE_TTL:
            return _cached_state
        
        _cached_state = _build_state_snapshot()
        _cached_state_time = now
        return _cached_state

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
    """Pull latest code, rebuild if needed, and restart container"""
    try:
        repo_dir = '/repo'

        # Remember old requirements before pull
        old_req = ''
        try:
            with open(os.path.join(repo_dir, 'requirements.txt'), 'r') as f:
                old_req = f.read()
        except Exception:
            pass

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
        new_req = ''
        try:
            with open(os.path.join(repo_dir, 'requirements.txt'), 'r') as f:
                new_req = f.read()
        except Exception:
            pass

        deps_changed = old_req != new_req

        # Get the git hash from freshly pulled code
        hash_result = subprocess.run(
            ['git', '-C', repo_dir, 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=10
        )
        new_hash = hash_result.stdout.strip() or os.getenv('FANCONTROL_GIT_HASH', 'unknown')

        # Always rebuild image with new code (code is baked into image via COPY)
        logger.info('Rebuilding Docker image with updated code...')
        rebuild = subprocess.run(
            ['docker', 'compose', '-f', os.path.join(repo_dir, 'docker-compose.yml'),
             'build', '--no-cache', '--build-arg', f'GIT_HASH={new_hash}'],
            capture_output=True, text=True, timeout=300,
            cwd=repo_dir
        )
        rebuild_output = (rebuild.stdout + rebuild.stderr)[-500:]
        logger.info(f'Docker build: {rebuild_output}')
        if rebuild.returncode != 0:
            return jsonify({
                'status': 'error',
                'message': f'Docker build failed:\n{rebuild_output}'
            }), 500

        # Restart container with new image
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
            'rebuild_output': rebuild_output,
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
# HARDWARE DISCOVERY
# ============================================================================

def generate_stable_id(path: str) -> str:
    """Generate stable, safe ID from hardware path using SHA256 hash"""
    hash_obj = hashlib.sha256(path.encode())
    return f"dev-{hash_obj.hexdigest()[:12]}"


def discover_fans_and_sensors() -> Tuple[Dict, Dict]:
    """
    Scan /sys/class/hwmon for fans and temperature sensors.
    Returns (fans_dict, temp_sensors_dict)
    """
    logger.info('=' * 50)
    logger.info('SCANNING HARDWARE MONITORS')
    
    fans = {}
    temps = {}
    
    for hw_path in sorted(HWMON_DIR.iterdir()):
        try:
            chip_name = "unknown"
            name_file = hw_path / 'name'
            if name_file.exists():
                chip_name = name_file.read_text().strip()
            
            logger.info(f'  Chip: {hw_path.name} ({chip_name})')
            
            # Discover PWM fans
            for pwm_file in sorted(hw_path.glob('pwm*')):
                if '_' in pwm_file.name:
                    continue
                
                try:
                    pwm_num = re.search(r'\d+', pwm_file.name).group()
                    fan_input = hw_path / f'fan{pwm_num}_input'
                    
                    if not fan_input.exists():
                        continue
                    
                    label_file = hw_path / f'fan{pwm_num}_label'
                    if label_file.exists():
                        label = label_file.read_text().strip()
                    else:
                        label = f'Fan {pwm_num}'
                    
                    writable = os.access(str(pwm_file), os.W_OK)
                    
                    try:
                        current_rpm = int(fan_input.read_text().strip())
                    except (ValueError, OSError):
                        current_rpm = 0
                    
                    fan_path_str = f'{hw_path.name}/{pwm_file.name}'
                    fan_id = generate_stable_id(fan_path_str)
                    
                    fans[fan_id] = {
                        'id': fan_id,
                        'label': label,
                        'hw_path': fan_path_str,
                        'pwm_path': str(pwm_file),
                        'fan_path': str(fan_input),
                        'rpm': current_rpm,
                        'pwm_value': 0,
                        'writable': writable,
                        'inverted': False,
                        'min_rpm': 0,
                        'max_rpm': 0,
                        'manual_pct': 50,
                        'sensors': [],
                        'sensor_mode': 'max',
                        'target_temp': 31,
                        'mode': 'manual',
                        'status': 'not_tested',
                        'target_pwm': 50,
                        'current_pct': 50,
                        'raw_pwm': 128,
                        'last_update': 0.0,
                        'schedule': [],
                        'curve': [],
                        'calibration': {}
                    }
                    
                except Exception as e:
                    logger.warning(f'    Error reading fan {pwm_file}: {e}')
                    continue
            
            # Discover temperature sensors
            for temp_file in sorted(hw_path.glob('temp*_input')):
                try:
                    temp_name = temp_file.name.replace('_input', '')
                    
                    label_file = hw_path / f'{temp_name}_label'
                    if label_file.exists():
                        label = label_file.read_text().strip()
                    else:
                        label = 'Temp'
                    
                    try:
                        temp_value = int(temp_file.read_text().strip()) // 1000
                    except (ValueError, OSError):
                        temp_value = 0
                    
                    temp_path_str = f'{hw_path.name}/{temp_name}'
                    temp_id = generate_stable_id(temp_path_str)
                    
                    temps[temp_id] = {
                        'id': temp_id,
                        'path': str(temp_file),
                        'label': label,
                        'value': temp_value
                    }
                    
                except Exception as e:
                    logger.warning(f'    Error reading temp sensor {temp_file}: {e}')
                    continue
                    
        except Exception as e:
            logger.warning(f'  Skipped {hw_path.name}: {e}')
            continue
    
    logger.info(f'  Found: {len(fans)} fans, {len(temps)} temp sensors')
    return fans, temps

# ============================================================================
# DISK SUBSYSTEM
# ============================================================================

def is_physical_disk(dev_name: str) -> bool:
    """Check if device name represents a physical disk"""
    patterns = [
        r'^sata\d+$',
        r'^nvme\d+n\d+$',
        r'^sd[a-z]$',
        r'^sd[a-z]{2,}$',
    ]
    
    if any(re.match(p, dev_name) for p in patterns):
        return True
    
    if any(dev_name.startswith(p) for p in ['hd', 'xvd', 'vd']):
        if not re.search(r'\d$', dev_name):
            return True
    
    return False


def calculate_disk_health(temp: float) -> Dict[str, Any]:
    """Calculate disk health metrics for UI display"""
    if temp <= 0:
        return {'pct_fill': 0, 'color_zone': 'unknown', 'status': 'standby'}
    
    temp = max(10, min(80, temp))
    pct_fill = max(0, min(100, int((temp - 20) / (60 - 20) * 100)))
    
    if temp <= 35:
        color_zone = 'cyan'
    elif temp <= 45:
        color_zone = 'orange'
    elif temp <= 55:
        color_zone = 'red'
    else:
        color_zone = 'critical'
    
    return {'pct_fill': pct_fill, 'color_zone': color_zone, 'status': 'active'}


def parse_smart_temp(output: str) -> Optional[int]:
    """Parse temperature from smartctl output"""
    for line in output.split('\n'):
        if 'Temperature_Celsius' in line:
            match = re.search(r'(\d+)\s*\(', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100:
                    return temp
            
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70:
                    return temp
    
    for line in output.split('\n'):
        if 'Airflow_Temperature_Cel' in line:
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70:
                    return temp
    
    return None


def read_disk_temp(disk_identifier: str) -> Tuple[Optional[float], bool]:
    """
    Read temperature from a disk.
    Returns (temperature_celsius, is_standby)
    """
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()
        
        if not is_physical_disk(clean_name):
            return None, False
        
        cmd = ['smartctl', '-A', '-n', 'standby', f'/dev/{clean_name}']
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 2:
                return None, True
            
            if result.returncode != 0:
                cmd2 = ['smartctl', '-A', '-n', 'standby', '-d', 'sat', f'/dev/{clean_name}']
                result = subprocess.run(cmd2, capture_output=True, text=True, timeout=10)
                
                if result.returncode == 2:
                    return None, True
                if result.returncode != 0:
                    return None, False
            
            if clean_name.startswith('nvme'):
                for line in result.stdout.split('\n'):
                    if 'Temperature:' in line:
                        match = re.search(r'(\d+)\s*Celsius', line)
                        if match:
                            return float(match.group(1)), False
            
            temp = parse_smart_temp(result.stdout)
            if temp is not None:
                return float(temp), False
                
        except subprocess.TimeoutExpired:
            logger.debug(f'Timeout reading {disk_identifier}')
            return None, False
            
    except Exception as e:
        logger.error(f'Error reading disk {disk_identifier}: {e}')
    
    return None, False


def discover_disks() -> Dict[str, Dict]:
    """
    Discover physical disks in the system.
    Returns cached data if polling is already in progress.
    """
    logger.info('=' * 50)
    logger.info('DISK DISCOVERY')
    
    with state_lock:
        if state.get('disks_polling'):
            logger.warning('Disk polling already in progress, returning cached data')
            return copy.deepcopy(state['hdd_sensors'])
        state['disks_polling'] = True
    
    try:
        disks = {}
        discovered_devices = set()
        
        # Method 1: lsblk
        try:
            result = subprocess.run(
                ['lsblk', '-nd', '-o', 'NAME,TYPE,TRAN'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0].strip()
                    dtype = parts[1].strip()
                    
                    if dtype == 'disk' and is_physical_disk(name):
                        skip_prefixes = ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']
                        if not any(name.startswith(p) for p in skip_prefixes):
                            discovered_devices.add(name)
                            
        except Exception as e:
            logger.warning(f'lsblk failed: {e}')
            
            try:
                for dev_path in Path('/sys/block').iterdir():
                    name = dev_path.name
                    if is_physical_disk(name):
                        skip_prefixes = ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']
                        if not any(name.startswith(p) for p in skip_prefixes):
                            discovered_devices.add(name)
            except Exception as e2:
                logger.warning(f'/sys/block fallback failed: {e2}')
        
        # Read temperatures in parallel
        futures_map = {
            dev: executor.submit(read_disk_temp, dev)
            for dev in sorted(discovered_devices)
        }
        
        for dev, future in futures_map.items():
            try:
                result = future.result(timeout=10)
                temp, standby = result if result else (None, False)
                
                disk_id = generate_stable_id(f'/dev/{dev}')
                
                if dev.startswith('sata'):
                    disk_label = f'SATA {dev.replace("sata", "")}'
                    disk_type = 'sata'
                elif dev.startswith('nvme'):
                    disk_label = f'NVMe {dev}'
                    disk_type = 'nvme'
                else:
                    disk_label = f'Disk {dev}'
                    disk_type = 'sata'
                
                health = calculate_disk_health(temp if temp else 0)
                
                disks[disk_id] = {
                    'id': disk_id,
                    'label': disk_label,
                    'device': f'/dev/{dev}',
                    'dev_name': dev,
                    'temp': temp if temp else 0,
                    'standby': standby,
                    'type': disk_type,
                    'pct_fill': health['pct_fill'],
                    'color_zone': health['color_zone'],
                    'health_status': health['status']
                }
                
            except FutureTimeout:
                logger.warning(f'Timeout polling disk {dev}')
            except Exception as e:
                logger.error(f'Failed to poll disk {dev}: {e}')
        
        logger.info(f'  Discovered: {len(disks)} disks')
        return disks
    
    finally:
        with state_lock:
            state['disks_polling'] = False

# ============================================================================
# PWM CONTROL
# ============================================================================

def set_pwm(key: str, raw_pwm: int, raw: bool = False):
    """
    Set PWM value. When raw=True, writes physical value directly without
    inversion handling or RPM reading (used during calibration).
    """
    with state_lock:
        fan = state['fans'].get(key)
        if not fan or not fan.get('pwm_path', '').startswith('/sys/class/hwmon/'):
            return
        
        val = max(0, min(255, int(raw_pwm)))
        
        if not raw:
            fan['raw_pwm'] = val
        
        physical_pwm = (255 - val) if (not raw and fan.get('inverted')) else val
        
        try:
            Path(fan['pwm_path']).write_text(str(physical_pwm))
            fan['pwm_value'] = val
            
            if not raw:
                try:
                    rpm_raw = Path(fan['fan_path']).read_text().strip()
                    rpm_val = int(rpm_raw) if rpm_raw.isdigit() else 0
                    if rpm_val > 0:
                        fan['rpm'] = rpm_val
                except Exception:
                    pass
                
                fan['last_update'] = time.monotonic()
            
        except Exception as e:
            logger.error(f'PWM write error {key}: {e}')

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
# MAIN CONTROL LOOP
# ============================================================================

def refresh():
    """Update temperature and RPM readings"""
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
        except Exception:
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
    """
    Update disk temperatures with health metrics - FULLY ATOMIC VERSION.
    Skips if disks_polling flag is set (concurrent discovery).
    All calculations inside single state_lock to prevent race conditions.
    """
    global _last_cleanup
    now = time.monotonic()
    
    with state_lock:
        if state.get('disks_polling'):
            return
        
        last_poll = state['last_hdd_poll']
        if last_poll > 0 and now - last_poll < DISK_POLL_COOLDOWN:
            return
        
        sensors_copy = {
            disk_id: info.copy()
            for disk_id, info in state['hdd_sensors'].items()
        }
    
    futures_map = {
        disk_id: executor.submit(read_disk_temp, info.get('dev_name', disk_id))
        for disk_id, info in sensors_copy.items()
    }
    
    updated_values = {}
    for disk_id, future in futures_map.items():
        try:
            result = future.result(timeout=5)
            temp, standby = result if result else (None, False)
            
            if temp is not None:
                health = calculate_disk_health(temp)
                updated_values[disk_id] = {
                    'temp': temp,
                    'standby': standby,
                    'pct_fill': health['pct_fill'],
                    'color_zone': health['color_zone'],
                    'health_status': health['status']
                }
        except FutureTimeout:
            logger.debug(f'Timeout polling disk {disk_id}')
        except Exception as e:
            logger.error(f'Poll error for {disk_id}: {e}')
    
    # SINGLE ATOMIC BLOCK
    with state_lock:
        for disk_id, data in updated_values.items():
            if disk_id in state['hdd_sensors']:
                state['hdd_sensors'][disk_id].update(data)
        
        state['last_hdd_poll'] = time.monotonic()
        
        active_temps = [
            v['temp'] for v in state['hdd_sensors'].values()
            if v.get('temp', 0) > 0 and not v.get('standby')
        ]
        
        all_standby = (
            len(state['hdd_sensors']) > 0 and
            all(v.get('standby', False) for v in state['hdd_sensors'].values())
        )
        
        if active_temps:
            state['max_hdd_temp'] = max(active_temps)
            state['failsafe'] = False
            state['standby_mode'] = False
        elif all_standby:
            state['max_hdd_temp'] = 0
            state['failsafe'] = False
            state['standby_mode'] = True
        else:
            state['max_hdd_temp'] = 0
            state['failsafe'] = True
            state['standby_mode'] = False


def fan_temp(fan: Dict, override_sensors: Optional[List] = None, 
             override_sensor_mode: Optional[str] = None) -> float:
    """Calculate effective temperature for a fan based on assigned sensors"""
    sensors = override_sensors if override_sensors is not None else fan.get('sensors', [])
    mode = override_sensor_mode if override_sensor_mode is not None else fan.get('sensor_mode', 'max')
    
    if not sensors:
        return SENSOR_FAILURE_TEMP
    
    temps = []
    
    with state_lock:
        hdd_copy = {k: v.copy() for k, v in state['hdd_sensors'].items()}
        temp_copy = {k: v.copy() for k, v in state['temp_sensors'].items()}
    
    for sensor_id in sensors:
        if sensor_id.startswith('hdd:'):
            disk_id = sensor_id.split(':', 1)[1]
            disk = hdd_copy.get(disk_id, {})
            temp = disk.get('temp', 0)
        elif sensor_id.startswith('temp:'):
            temp_id = sensor_id.split(':', 1)[1]
            sensor = temp_copy.get(temp_id, {})
            temp = sensor.get('value', 0)
        else:
            if sensor_id in hdd_copy:
                temp = hdd_copy[sensor_id].get('temp', 0)
            elif sensor_id in temp_copy:
                temp = temp_copy[sensor_id].get('value', 0)
            else:
                temp = 0
        
        if temp > 0:
            temps.append(temp)
    
    if not temps:
        return SENSOR_FAILURE_TEMP
    
    if mode == 'max':
        return max(temps)
    elif mode == 'min':
        return min(temps)
    else:
        return sum(temps) / len(temps)


def pwm_from_curve(fan: Dict, target_pct: float) -> int:
    """
    Convert target percentage to PWM value using calibration curve.
    Keeps target_pct as float for smooth interpolation, rounds only at return.
    """
    curve = fan.get('curve', [])
    cal = fan.get('calibration', {})
    
    if not cal or len(curve) < 2:
        return int(target_pct * 255 // 100)
    
    target_pct = max(0.0, min(100.0, float(target_pct)))
    min_pct = float(cal.get('min_pct', 0))
    
    if 0.0 < target_pct < min_pct:
        target_pct = min_pct
    
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        if min(a['pct'], b['pct']) <= target_pct <= max(a['pct'], b['pct']):
            if a['pct'] == b['pct']:
                return int(a['pwm'])
            ratio = (target_pct - a['pct']) / (b['pct'] - a['pct'])
            pwm = a['pwm'] + (b['pwm'] - a['pwm']) * ratio
            return max(0, min(255, int(round(pwm))))
    
    return int(curve[-1]['pwm'])


def process_auto_mode(fan_id: str, fan: Dict, current_temp: float,
                      target_temp: float, schedule_item: Optional[Dict] = None,
                      failsafe: bool = False, standby_mode: bool = False) -> Tuple[int, str]:
    """
    PURE FUNCTION: Calculates target PWM percentage and status.
    Does NOT write to hardware or modify state.
    
    Args:
        fan_id: Fan identifier (for logging only)
        fan: Fan configuration dictionary
        current_temp: Current effective temperature
        target_temp: Target temperature
        schedule_item: Current schedule rule if applied (optional)
        failsafe: Whether the system is in failsafe mode
        standby_mode: Whether disks are in standby
    
    Returns:
        (target_pct: int, status: str)
    """
    if failsafe:
        return MAX_PWM_PCT, 'failsafe'

    if standby_mode:
        status = 'standby'
        
        if schedule_item:
            sm = schedule_item.get('mode', 'auto')
            if sm == 'manual':
                target_pct = schedule_item.get('speed_pct', 50)
            elif sm == 'off':
                target_pct = 0
            else:
                sched_target = schedule_item.get('target_temp', target_temp)
                target_pct = max(MIN_PWM_PCT, min(40, 
                    sched_target - current_temp + 30))
        else:
            target_pct = 25
        
        return int(target_pct), status

    # 3. Temperature sensor failure - MAXIMUM COOLING for hardware safety!
    if current_temp >= SENSOR_FAILURE_TEMP:
        return MAX_PWM_PCT, 'critical'

    # 4. Normal operation (Proportional curve)
    delta = current_temp - target_temp
    
    if delta <= -2:
        target_pct = MIN_PWM_PCT
    elif delta >= 6:
        target_pct = MAX_PWM_PCT
    else:
        target_pct = MIN_PWM_PCT + (delta + 2) * (MAX_PWM_PCT - MIN_PWM_PCT) // 8

    status = 'warning' if delta > 4 else 'nominal'
    return int(target_pct), status


def _evaluate_fan_mode(fan_id: str, fan: Dict, sys_failsafe: bool, sys_standby: bool) -> Tuple[int, str]:
    """
    Evaluate target_pct and status for a fan based on its mode and schedule.
    Returns (target_pct, status).
    """
    mode = fan.get('mode', 'manual')
    
    if mode == 'manual':
        raw_pct = fan.get('manual_pct', 50)
        status = fan.get('status', 'nominal')
        if status not in ['nominal', 'warning', 'critical', 'standby', 'failsafe', 'inverted', 'no_sensor']:
            status = 'nominal'
        return raw_pct, status
    
    if mode != 'auto':
        return 50, 'nominal'
    
    schedule = fan.get('schedule', [])
    schedule_applied = False
    
    if schedule:
        now_dt = datetime.now()
        current_day = now_dt.strftime('%a').lower()
        current_time = now_dt.strftime('%H:%M')
        
        for item in schedule:
            if item['day'] == 'all':
                days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
            elif item['day'] == 'weekday':
                days = ['mon', 'tue', 'wed', 'thu', 'fri']
            elif item['day'] == 'weekend':
                days = ['sat', 'sun']
            else:
                days = [item['day']]
            
            if current_day in days and item['time_start'] <= current_time <= item['time_end']:
                schedule_applied = True
                sm = item.get('mode', 'auto')
                
                if sm == 'off':
                    return 0, 'off'
                elif sm == 'manual':
                    return item.get('speed_pct', 50), 'manual'
                else:
                    item_sensors = item.get('sensors') or fan.get('sensors', [])
                    item_sensor_mode = item.get('sensor_mode') or fan.get('sensor_mode', 'max')
                    current_temp = fan_temp(fan, item_sensors, item_sensor_mode)
                    target_temp = item.get('target_temp', fan.get('target_temp', 31))
                    target_pct, status = process_auto_mode(
                        fan_id, fan, current_temp, target_temp, item,
                        failsafe=sys_failsafe, standby_mode=sys_standby
                    )
                    if status == 'critical' and not item_sensors:
                        status = 'no_sensor'
                    return target_pct, status
                break
    
    if not schedule_applied:
        current_temp = fan_temp(fan)
        target_temp = fan.get('target_temp', 31)
        target_pct, status = process_auto_mode(
            fan_id, fan, current_temp, target_temp,
            failsafe=sys_failsafe, standby_mode=sys_standby
        )
        if status == 'critical' and not fan.get('sensors'):
            status = 'no_sensor'
        return target_pct, status
    
    return 50, 'nominal'


def loop():
    """
    Main control loop.
    Architecture: 
    - Pure functions calculate target_pct and status
    - Loop applies PWM and updates state centrally
    """
    global _failed_calibration_logged, _last_cleanup
    last_log = time.monotonic()
    
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
                    time.sleep(UNINITIALIZED_POLL_INTERVAL)
                    continue
                
                time.sleep(2)
                continue
            
            refresh()
            refresh_disks()
            
            with state_lock:
                fans_snapshot = copy.deepcopy(state['fans'])
                sys_failsafe = state.get('failsafe', False)
                sys_standby = state.get('standby_mode', False)
            
            updated_fans_metrics = {}
            
            for fan_id, fan in fans_snapshot.items():
                if not fan.get('writable'):
                    continue
                
                target_pct, status = _evaluate_fan_mode(fan_id, fan, sys_failsafe, sys_standby)
                mode = fan.get('mode', 'manual')
                
                if mode not in ('manual', 'auto'):
                    continue
                
                calculated_pwm = pwm_from_curve(fan, float(target_pct))
                set_pwm(fan_id, calculated_pwm)
                
                updated_fans_metrics[fan_id] = {
                    'current_pct': target_pct,
                    'target_pwm': target_pct,
                    'mode': mode,
                    'status': status,
                    'inverted': fan.get('inverted', False)
                }
            
            with state_lock:
                for fan_id, metrics in updated_fans_metrics.items():
                    if fan_id in state['fans']:
                        state['fans'][fan_id].update(metrics)
            
            socketio.emit('update', get_state())
            
            current_time = time.monotonic()
            if current_time - last_log > TELEMETRY_LOG_INTERVAL:
                try:
                    log_telemetry()
                except Exception as e:
                    logger.error(f'Logging error: {e}')
                last_log = current_time
            
            if current_time - _last_cleanup > LOG_CLEANUP_INTERVAL:
                cleanup_logs()
                _last_cleanup = current_time
            
            time.sleep(CONTROL_LOOP_INTERVAL)
            
        except Exception as e:
            logger.error(f'Loop error: {e}', exc_info=True)
            time.sleep(CONTROL_LOOP_INTERVAL)


def log_telemetry():
    """Log telemetry data to SQLite"""
    try:
        with state_lock:
            fan_count = len(state['fans'])
            disk_count = len(state['hdd_sensors'])
            
            if fan_count > 0:
                avg_pwm = sum(
                    f.get('raw_pwm', f.get('pwm_value', 0))
                    for f in state['fans'].values()
                ) // fan_count
                avg_rpm = sum(
                    f.get('rpm', 0)
                    for f in state['fans'].values()
                ) // fan_count
            else:
                avg_pwm = 0
                avg_rpm = 0
            
            max_temp = state.get('max_hdd_temp', 0)
        
        with get_db_connection() as conn:
            conn.execute(
                'INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    datetime.now().isoformat(),
                    'auto',
                    avg_pwm,
                    avg_rpm,
                    max_temp,
                    fan_count,
                    disk_count
                )
            )
            conn.commit()
            
    except sqlite3.OperationalError as e:
        logger.error(f'SQLite write error: {e}')


def cleanup_logs(retention_days: int = 30):
    """Remove old log entries"""
    try:
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with get_db_connection() as conn:
            conn.execute('DELETE FROM logs WHERE ts < ?', (cutoff,))
            conn.commit()
        logger.info(f'Cleaned logs older than {retention_days} days')
    except sqlite3.OperationalError as e:
        logger.error(f'Failed to cleanup logs: {e}')

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
    """Send initial state on client connection"""
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
        threading.Thread(target=loop, daemon=True).start()


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
        # Invalidate cached state and push correct state to all connected clients
        global _cached_state
        _cached_state = None
        socketio.emit('update', get_state())


if __name__ == '__main__':
    logger.info('=' * 60)
    logger.info(f'STARTING FanControl Web {CONFIG_VERSION} - Neon Cyberpunk Edition')
    logger.info('=' * 60)
    
    init_database()
    init_hardware()
    _ensure_control_loop()
    
    logger.info('Starting server on port 5059')
    socketio.run(app, host='0.0.0.0', port=5059)
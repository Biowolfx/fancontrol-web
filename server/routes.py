"""Flask routes for FanControl Web."""

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

from flask import Blueprint, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import BadRequest

from core.state import state, state_lock, get_state, CONFIG_VERSION, invalidate_state_cache
from core.config import save_config, load_config, DATA_DIR, CONFIG_PATH
from core.hardware import discover_fans_and_sensors, discover_disks, set_pwm, refresh, read_disk_smart
from core.calibration import test_fans
from core.control import get_db_connection

logger = logging.getLogger('fancontrol')

routes = Blueprint('routes', __name__)

# Rate limiting for control endpoints
_control_rate_limit: Dict[str, float] = {}
CONTROL_RATE_LIMIT_SECONDS = 0.1
_RATE_LIMIT_CLEANUP_INTERVAL = 600
_rate_limit_last_cleanup = time.monotonic()

MAX_HISTORY_HOURS = 168
from core.hardware import CALIBRATION_STEPS
PWM_CURVE_POINTS = len(CALIBRATION_STEPS)


@routes.route('/')
def index():
    """Serve the main dashboard"""
    from core.state import CONFIG_VERSION
    resp = render_template('index.html', config_version=CONFIG_VERSION)
    from flask import make_response
    response = make_response(resp)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@routes.route('/js/<path:filename>')
def serve_js(filename):
    """Serve JavaScript files from templates/js"""
    return send_from_directory(os.path.join(os.path.dirname(__file__), '..', 'templates', 'js'), filename)


@routes.route('/api/state')
def api_get_state():
    """REST endpoint for current state (debugging/health checks)"""
    return jsonify(get_state())


@routes.route('/api/kernel')
def api_get_kernel():
    """Detect kernel type and capabilities for fan control."""
    from core.kernel_detect import get_kernel_info
    return jsonify(get_kernel_info())


@routes.route('/api/system')
def api_get_system():
    from core.hardware import get_system_info
    return jsonify(get_system_info())


@routes.route('/api/lang/<code>')
def api_get_lang(code):
    """Serve translation file"""
    if not re.match(r'^[a-z]{2}$', code):
        return jsonify({'error': 'Invalid language code'}), 400
    lang_file = Path(os.path.join(os.path.dirname(__file__), '..', 'static')) / 'lang' / f'{code}.json'
    if lang_file.exists():
        with open(lang_file, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    return jsonify({}), 404


@routes.route('/api/language', methods=['POST'])
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


@routes.route('/api/discover', methods=['POST'])
def api_discover():
    """Scan hardware for fans, sensors, and disks"""
    try:
        logger.info("Starting hardware discovery...")
        fans, temps = discover_fans_and_sensors()
        disks = discover_disks()
        
        from core.kernel_detect import get_kernel_info
        kernel_info = get_kernel_info()
        
        with state_lock:
            state['fans'] = fans
            state['temp_sensors'] = temps
            state['hdd_sensors'] = disks
            state['hardware_scanned'] = True
            state['kernel_type'] = kernel_info.get('type', 'unknown')
        
        from app import socketio
        socketio.emit('hardware_discovered', {
            'fans': fans,
            'temps': temps,
            'disks': disks,
            'kernel_info': kernel_info,
        })
        
        logger.info(f"Discovery complete: {len(fans)} fans, {len(temps)} sensors, {len(disks)} disks, kernel={kernel_info.get('type')}")
        return jsonify({'status': 'ok', 'fans': fans, 'temps': temps, 'disks': disks, 'kernel_info': kernel_info})
        
    except Exception as e:
        logger.error(f'Discovery failed: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/disks/<disk_id>/smart')
def api_get_disk_smart(disk_id):
    """Get full SMART data for a specific disk"""
    import time as _time
    from core.hardware import _smart_cache, _smart_cache_time, _smart_cache_lock, SMART_CACHE_TTL

    force_refresh = request.args.get('refresh', '0') == '1'
    now = _time.monotonic()
    with _smart_cache_lock:
        if not force_refresh and disk_id in _smart_cache and (now - _smart_cache_time.get(disk_id, 0)) < SMART_CACHE_TTL:
            return jsonify(_smart_cache[disk_id])

    with state_lock:
        disk = state.get('hdd_sensors', {}).get(disk_id)
        if not disk:
            return jsonify({'error': 'Disk not found'}), 404

        device = disk.get('device', '')
        if not device:
            return jsonify({'error': 'No device path'}), 404

    result = read_disk_smart(device)

    if 'error' not in result:
        with _smart_cache_lock:
            _smart_cache[disk_id] = result
            _smart_cache_time[disk_id] = now

    return jsonify(result)


@routes.route('/api/initialize', methods=['POST'])
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
        
        from app import socketio
        threading.Thread(
            target=test_fans,
            kwargs={'socketio': socketio, 'save_config_fn': save_config},
            daemon=True
        ).start()
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        logger.error(f'Initialization error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/skip-calibration', methods=['POST'])
def api_skip_calibration():
    """Mark setup complete without calibration (monitoring-only mode)."""
    with state_lock:
        state['initialized'] = True
        state['tested'] = True
    save_config()
    return jsonify({'status': 'ok'})


@routes.route('/api/dsm/fan-speed', methods=['POST'])
def api_set_dsm_fan_speed():
    """Set DSM fan speed via scemd.xml."""
    from core.dsm_fan import is_dsm_fan_available, set_dsm_fan_speed
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available (scemd.xml not found)'}), 400

    data = request.get_json(silent=True) or {}
    speed = data.get('speed', 50)
    speed = max(0, min(100, int(speed)))

    if set_dsm_fan_speed(speed):
        with state_lock:
            for fan_id, fan in state['fans'].items():
                if fan.get('control_method') == 'dsm_scemd':
                    fan['manual_pct'] = speed
                    fan['pwm_value'] = int(speed * 255 / 100)
        save_config()
        return jsonify({'status': 'ok', 'speed': speed})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to set fan speed'}), 500


# ============================================================================
# DSM Scheme Management
# ============================================================================

@routes.route('/api/dsm/schemes', methods=['GET'])
def api_get_dsm_schemes():
    """Return all fan_config schemes from scemd.xml."""
    from core.dsm_fan import is_dsm_fan_available, get_all_schemes
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    info = get_all_schemes()
    if info is None:
        return jsonify({'status': 'error', 'message': 'Failed to parse scemd.xml'}), 500
    return jsonify({'status': 'ok', **info})


@routes.route('/api/dsm/scheme/<scheme_type>', methods=['GET'])
def api_get_dsm_scheme(scheme_type):
    """Return a single scheme by type."""
    from core.dsm_fan import is_dsm_fan_available, get_scheme
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    scheme = get_scheme(scheme_type)
    if scheme is None:
        return jsonify({'status': 'error', 'message': f'Scheme {scheme_type} not found'}), 404
    return jsonify({'status': 'ok', 'scheme': scheme})


@routes.route('/api/dsm/scheme/<scheme_type>', methods=['PUT'])
def api_update_dsm_scheme(scheme_type):
    """Update a scheme's entries."""
    from core.dsm_fan import is_dsm_fan_available, update_scheme
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    data = request.get_json(silent=True) or {}
    entries = data.get('entries')
    if not entries or not isinstance(entries, list):
        return jsonify({'status': 'error', 'message': 'entries array required'}), 400

    if update_scheme(scheme_type, entries):
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'Failed to update scheme'}), 500


@routes.route('/api/dsm/scheme/<scheme_type>/entry/<int:index>', methods=['PUT'])
def api_update_dsm_entry(scheme_type, index):
    """Update a single entry in a scheme."""
    from core.dsm_fan import is_dsm_fan_available, update_scheme_entry
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    data = request.get_json(silent=True) or {}
    fan_speed = data.get('fan_speed_pct')
    action = data.get('action')
    threshold = data.get('threshold_temp')

    if update_scheme_entry(scheme_type, index,
                           fan_speed_pct=fan_speed,
                           action=action,
                           threshold_temp=threshold):
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'Failed to update entry'}), 500


@routes.route('/api/dsm/active', methods=['GET'])
def api_get_dsm_active():
    """Return the currently active scheme type."""
    from core.dsm_fan import is_dsm_fan_available, get_active_scheme_type
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    active = get_active_scheme_type()
    return jsonify({'status': 'ok', 'active_scheme': active})


@routes.route('/api/dsm/apply', methods=['POST'])
def api_apply_dsm_schemes():
    """Write pending changes and restart scemd service."""
    from core.dsm_fan import is_dsm_fan_available, get_all_schemes, _restart_scemd
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    if _restart_scemd():
        return jsonify({'status': 'ok', 'message': 'scemd service restarted'})
    return jsonify({'status': 'error', 'message': 'Failed to restart scemd service'}), 500


@routes.route('/api/test/start', methods=['POST'])
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
        
        from app import socketio
        threading.Thread(
            target=test_fans,
            args=(fan_key,),
            kwargs={'socketio': socketio, 'save_config_fn': save_config},
            daemon=True
        ).start()
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        logger.error(f'Test start error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/history')
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


@routes.route('/api/update/check')
def api_update_check():
    """Check for updates — compare local vs remote git hash."""
    import subprocess

    current_version = CONFIG_VERSION
    remote_version = ''
    remote_hash = ''
    commit_msg = ''
    local_hash = ''

    repo_dir = '/repo'

    # Get local hash from running code
    try:
        local_result = subprocess.run(
            ['git', '-C', repo_dir, 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        local_hash = local_result.stdout.strip()
    except Exception:
        pass

    # Fetch latest from remote and compare
    try:
        fetch = subprocess.run(
            ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        if fetch.returncode != 0:
            logger.error(f'Update check: git fetch failed: {fetch.stderr[:200]}')
    except Exception as e:
        logger.error(f'Update check: git fetch error: {e}')

    # Get remote hash
    try:
        remote_result = subprocess.run(
            ['git', '-C', repo_dir, 'rev-parse', '--short', 'origin/main'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        remote_hash = remote_result.stdout.strip()
    except Exception:
        pass

    # Get remote commit message
    try:
        msg_result = subprocess.run(
            ['git', '-C', repo_dir, 'log', '--oneline', '-1', 'origin/main'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        commit_msg = msg_result.stdout.strip()
    except Exception:
        pass

    # Read remote CONFIG_VERSION from fetched code
    try:
        ver_result = subprocess.run(
            ['git', '-C', repo_dir, 'show', f'origin/main:core/state.py'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        m = re.search(r"CONFIG_VERSION\s*=\s*['\"](.+?)['\"]", ver_result.stdout)
        if m:
            remote_version = m.group(1)
    except Exception:
        pass

    has_update = bool(remote_hash and local_hash and remote_hash != local_hash)
    logger.info(f'[CHECK] local={local_hash}, remote={remote_hash}, has_update={has_update}')

    return jsonify({
        'status': 'ok',
        'has_update': has_update,
        'current_version': current_version,
        'remote_version': remote_version or current_version,
        'current_hash': local_hash or 'N/A',
        'remote_hash': remote_hash or 'N/A',
        'commit_message': commit_msg
    })


@routes.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Pull latest code, sync to /app, then exit process."""
    update_token = os.environ.get('FANCONTROL_UPDATE_TOKEN')
    if update_token:
        provided = request.headers.get('X-Update-Token') or request.args.get('token')
        if provided != update_token:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401

    try:
        repo_dir = '/repo'
        app_dir = '/app'

        logger.info(f'[UPDATE] ====== START ====== PID={os.getpid()} VERSION={CONFIG_VERSION}')

        # Step 1: Check repo exists
        repo_exists = os.path.isdir(repo_dir)
        app_py = os.path.isfile(os.path.join(repo_dir, 'app.py'))
        logger.info(f'[UPDATE] Step 1: /repo exists={repo_exists}, app.py={app_py}')

        if not repo_exists or not app_py:
            logger.error(f'[UPDATE] /repo not ready!')
            return jsonify({'status': 'error', 'message': '/repo not ready'}), 500

        # Step 2: Git pull
        logger.info('[UPDATE] Step 2: git fetch + reset...')
        fetch = subprocess.run(
            ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        if fetch.returncode != 0:
            logger.error(f'[UPDATE] Git fetch FAILED: {fetch.stderr.strip()[:300]}')
            return jsonify({'status': 'error', 'message': fetch.stderr.strip()}), 500

        reset = subprocess.run(
            ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        pull_output = reset.stdout.strip() + '\n' + reset.stderr.strip()
        already_up = 'Already up to date' in pull_output or 'HEAD is now at' in pull_output
        logger.info(f'[UPDATE] Step 2 result: rc={reset.returncode}, output={pull_output.strip()[:300]}')

        # Step 3: Check what version /repo has after pull
        try:
            with open(os.path.join(repo_dir, 'core', 'state.py')) as f:
                for line in f:
                    if 'CONFIG_VERSION' in line:
                        logger.info(f'[UPDATE] Step 3: /repo version after pull: {line.strip()}')
                        break
        except Exception as e:
            logger.error(f'[UPDATE] Step 3: Cannot read /repo version: {e}')

        # Step 4: Sync /repo → /app
        logger.info('[UPDATE] Step 4: syncing files...')
        import shutil
        synced = []
        for f in os.listdir(repo_dir):
            if f.endswith('.py') or f.endswith('.txt') or f in ('Dockerfile', 'docker-compose.yml'):
                src = os.path.join(repo_dir, f)
                dst = os.path.join(app_dir, f)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                    synced.append(f)
        for d in ('templates', 'static', 'core', 'server', 'agent', 'installer', 'tests'):
            src = os.path.join(repo_dir, d)
            dst = os.path.join(app_dir, d)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                synced.append(f'{d}/')
        logger.info(f'[UPDATE] Step 4: synced {len(synced)} items: {", ".join(synced[:15])}')

        # Step 5: Verify /app version after sync
        try:
            with open(os.path.join(app_dir, 'core', 'state.py')) as f:
                for line in f:
                    if 'CONFIG_VERSION' in line:
                        logger.info(f'[UPDATE] Step 5: /app version after sync: {line.strip()}')
                        break
        except Exception as e:
            logger.error(f'[UPDATE] Step 5: Cannot read /app version: {e}')

        # Step 6: Schedule process exit
        logger.info('[UPDATE] Step 6: scheduling os._exit(0) in 1s...')
        import threading
        def delayed_exit():
            logger.info('[UPDATE] Step 6: os._exit(0) called!')
            os._exit(0)
        threading.Timer(1.0, delayed_exit).start()

        logger.info('[UPDATE] ====== DONE (waiting for timer) ======')
        return jsonify({'status': 'ok', 'message': f'Synced. Restarting in 1s...'})

    except Exception as e:
        logger.error(f'[UPDATE] ERROR: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/control', methods=['POST'])
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


@routes.route('/api/fan/<fan_id>/calibration', methods=['POST'])
def api_fan_calibration(fan_id):
    """Save calibration params (min_pwm, max_pwm, lambda) for a fan."""
    data = request.get_json(silent=True) or {}

    with state_lock:
        if fan_id not in state.get('fans', {}):
            return jsonify({'error': 'Fan not found'}), 404

        fan = state['fans'][fan_id]
        if 'calibration' not in fan:
            fan['calibration'] = {}

        for key in ('min_pwm', 'max_pwm', 'lambda'):
            if key in data:
                fan['calibration'][key] = data[key]

    save_config()
    return jsonify({'status': 'saved'})


@routes.route('/api/dashboard', methods=['GET'])
def api_get_dashboard():
    """Get dashboard layout."""
    return jsonify(state.get('dashboard', {'groups': [], 'cards': []}))


@routes.route('/api/dashboard', methods=['POST'])
def api_save_dashboard():
    """Save dashboard layout (cards, groups, positions, hidden sensors)."""
    data = request.get_json(silent=True) or {}
    with state_lock:
        old_hidden = state.get('dashboard', {}).get('hiddenSensors', [])
        state['dashboard'] = {
            'groups': data.get('groups', []),
            'cards': data.get('cards', []),
            'hiddenSensors': data.get('hiddenSensors', old_hidden)
        }
        new_hidden = state['dashboard']['hiddenSensors']
    save_config()
    if old_hidden != new_hidden:
        from app import socketio
        socketio.emit('hidden_sensors', {'hiddenSensors': new_hidden})
    return jsonify({'status': 'saved'})


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
# NODE MANAGEMENT API
# ============================================================================

@routes.route('/api/nodes')
def api_list_nodes():
    """List all registered nodes."""
    from server.node_registry import list_nodes
    return jsonify(list_nodes())


@routes.route('/api/nodes', methods=['POST'])
def api_add_node():
    """Add a new node."""
    from server.node_registry import add_node
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    node = add_node(name)
    return jsonify(node), 201


@routes.route('/api/nodes/<node_id>')
def api_get_node(node_id):
    """Get node details."""
    from server.node_registry import get_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    return jsonify(node)


@routes.route('/api/nodes/<node_id>', methods=['PUT'])
def api_update_node(node_id):
    """Update a node (name, ip, port, api_token)."""
    from server.node_registry import get_node, update_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    ip = data.get('ip', '').strip()
    port = data.get('port')
    api_token = data.get('api_token', '').strip()

    if update_node(node_id, name=name or None, ip=ip if ip is not None else None,
                   port=port, api_token=api_token or None):
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Update failed'}), 500


@routes.route('/api/nodes/<node_id>', methods=['DELETE'])
def api_delete_node(node_id):
    """Delete a node."""
    from server.node_registry import delete_node
    if delete_node(node_id):
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Node not found'}), 404


@routes.route('/api/nodes/<node_id>/config', methods=['POST'])
def api_push_config(node_id):
    """Push config to agent."""
    from server.node_registry import get_node, update_node_config
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json()
    update_node_config(node_id, data.get('config', {}))
    from app import socketio
    socketio.emit('server:config_push', {
        'config': data.get('config', {}),
    }, room=node_id)
    return jsonify({'status': 'pushed'})


@routes.route('/api/nodes/<node_id>/mode', methods=['POST'])
def api_set_node_mode(node_id):
    """Set agent control mode."""
    from server.node_registry import get_node, update_node_control_mode
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json()
    mode = data.get('mode', 'server')
    if mode not in ('server', 'manual'):
        return jsonify({'error': 'Invalid mode'}), 400
    update_node_control_mode(node_id, mode)
    from app import socketio
    socketio.emit('server:set_control_mode', {
        'mode': mode,
    }, room=node_id)
    return jsonify({'mode': mode})


@routes.route('/api/nodes/discover')
def api_discover_nodes():
    """Scan LAN for agents via SSDP + HTTP probe of offline nodes."""
    from server.discovery import scan_for_agents, probe_known_agents
    nodes = scan_for_agents(timeout=3)
    # Also probe offline nodes directly via HTTP
    probed = probe_known_agents(timeout=2)
    # Merge: SSDP results first, then newly-probed online nodes
    found_ids = {n['node_id'] for n in nodes}
    for p in probed:
        if p['node_id'] not in found_ids:
            nodes.append(p)
    return jsonify(nodes)


@routes.route('/api/nodes/probe', methods=['POST'])
def api_probe_ip():
    """Probe a specific IP for an agent."""
    from server.discovery import probe_agent
    from server.node_registry import list_nodes, get_node
    data = request.get_json()
    ip = (data.get('ip') or '').strip()
    port = int(data.get('port', 5059))
    if not ip:
        return jsonify({'error': 'IP required'}), 400

    info = probe_agent(ip, port=port, timeout=3)
    if not info:
        return jsonify({'error': 'Agent not reachable'}), 404

    # Check if this agent is already registered
    nodes = list_nodes()
    existing = None
    for n in nodes:
        if n.get('ip') == ip:
            existing = n
            break

    if existing:
        # Update status to online
        from server.node_registry import update_node_status
        update_node_status(existing['node_id'], 'online')
        info['node_id'] = existing['node_id']
        info['name'] = existing['name']
        info['already_registered'] = True
    else:
        info['already_registered'] = False

    return jsonify(info)


@routes.route('/api/nodes/add-by-ip', methods=['POST'])
def api_add_node_by_ip():
    """Add a node by IP address directly."""
    from server.node_registry import add_node, list_nodes
    from server.discovery import probe_agent
    data = request.get_json()
    ip = (data.get('ip') or '').strip()
    name = (data.get('name') or '').strip()
    port = int(data.get('port', 5059))
    if not ip:
        return jsonify({'error': 'IP required'}), 400
    if not name:
        name = ip

    # Check for duplicate IP
    for n in list_nodes():
        if n.get('ip') == ip:
            return jsonify({'error': 'Node with this IP already exists'}), 409

    info = probe_agent(ip, port=port, timeout=3)
    api_token = info.get('X-FANCONTROL-TOKEN', '') if info else ''

    node = add_node(name, api_token=api_token, ip=ip)

    from server.node_registry import update_node_status
    update_node_status(node['node_id'], 'online' if info else 'offline')

    return jsonify(node), 201


# ============================================================================
# DISCOVERED AGENTS API
# ============================================================================

@routes.route('/api/discovered')
def api_list_discovered():
    """List discovered but unregistered agents."""
    from server.discovery import _discovered_nodes, _lock
    with _lock:
        return jsonify(list(_discovered_nodes.values()))


@routes.route('/api/discovered/<node_id>/accept', methods=['POST'])
def api_accept_discovered(node_id):
    """Accept a discovered agent and register it."""
    from server.discovery import _discovered_nodes, _lock
    from server.node_registry import add_node

    with _lock:
        agent = _discovered_nodes.get(node_id)
        if not agent:
            return jsonify({'error': 'Agent not found'}), 404

        node = add_node(agent['name'], api_token=agent.get('api_token', ''), ip=agent.get('ip', ''))
        del _discovered_nodes[node_id]

    return jsonify(node), 201

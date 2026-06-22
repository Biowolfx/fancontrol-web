"""Flask routes for FanControl Web."""

import json
import logging
import os
import re
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
    resp = render_template('index.html')
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


@routes.route('/api/lang/<code>')
def api_get_lang(code):
    """Serve translation file"""
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
        
        with state_lock:
            state['fans'] = fans
            state['temp_sensors'] = temps
            state['hdd_sensors'] = disks
            state['hardware_scanned'] = True
        
        from app import socketio
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


@routes.route('/api/disks/<disk_id>/smart')
def api_get_disk_smart(disk_id):
    """Get full SMART data for a specific disk"""
    import time as _time
    from core.hardware import _smart_cache, _smart_cache_time, SMART_CACHE_TTL

    force_refresh = request.args.get('refresh', '0') == '1'
    now = _time.monotonic()
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
    """Check for updates — reads CONFIG_VERSION from remote core/state.py via GitHub API."""
    import urllib.request, ssl, base64

    current_version = CONFIG_VERSION
    repo = os.getenv('FANCONTROL_REPO', 'Biowolfx/fancontrol-web')

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    remote_version = ''
    remote_hash = ''
    commit_msg = ''

    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/commits/main',
            headers={'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'fancontrol-web'}
        )
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read())
            remote_hash = data['sha'][:8]
            commit_msg = data['commit']['message'].split('\n')[0]
    except Exception as e:
        logger.error(f'Update check: failed to fetch commit info: {e}')

    # Use GitHub Contents API (not raw URL — raw is CDN-cached and stale)
    try:
        req2 = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/contents/core/state.py?ref=main',
            headers={'Accept': 'application/vnd.github.v3+json', 'User-Agent': 'fancontrol-web'}
        )
        with urllib.request.urlopen(req2, timeout=10, context=ctx) as resp:
            file_data = json.loads(resp.read())
            content = base64.b64decode(file_data['content']).decode()
            m = re.search(r"CONFIG_VERSION\s*=\s*['\"](.+?)['\"]", content)
            if m:
                remote_version = m.group(1)
    except Exception as e:
        logger.error(f'Update check: failed to fetch remote state.py: {e}')

    has_update = bool(remote_version and current_version and remote_version != current_version)

    return jsonify({
        'status': 'ok',
        'has_update': has_update,
        'current_version': current_version,
        'remote_version': remote_version or 'unknown',
        'current_hash': 'N/A',
        'remote_hash': remote_hash,
        'commit_message': commit_msg
    })


@routes.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Pull latest code and restart container. Entrypoint syncs code from /repo."""
    try:
        repo_dir = '/repo'

        logger.info(f'[UPDATE] Starting update. Repo dir exists: {os.path.isdir(repo_dir)}, app.py exists: {os.path.isfile(os.path.join(repo_dir, "app.py"))}')

        # Git pull (public repo, no auth needed)
        pull = subprocess.run(
            ['git', '-C', repo_dir, 'pull', '--ff-only', 'origin', 'main'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )

        pull_output = pull.stdout.strip() + '\n' + pull.stderr.strip()
        already_up = 'Already up to date' in pull_output or 'Already up-to-date' in pull_output

        logger.info(f'[UPDATE] Git pull rc={pull.returncode}, already_up={already_up}')
        logger.info(f'[UPDATE] Git pull output: {pull_output.strip()[:500]}')

        if pull.returncode != 0 and not already_up:
            logger.error(f'[UPDATE] Git pull FAILED: {pull_output}')
            return jsonify({'status': 'error', 'message': pull_output.strip()}), 500

        # Check if requirements changed
        old_req = ''
        new_req = ''
        try:
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
        logger.info(f'[UPDATE] deps_changed={deps_changed}')

        # Schedule restart via detached subprocess (thread dies with request)
        container_name = os.getenv('CONTAINER_NAME', 'fancontrol-web')
        restart_script = """import time, os, signal
time.sleep(2)
os.kill(1, signal.SIGTERM)
"""
        try:
            proc = subprocess.Popen(
                ['python3', '-c', restart_script],
                stdout=open('/tmp/fancontrol_restart.log', 'w'),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            logger.info(f'[UPDATE] Restart subprocess spawned: pid={proc.pid}')
        except Exception as e:
            logger.error(f'[UPDATE] Failed to spawn restart subprocess: {e}')

        return jsonify({
            'status': 'ok',
            'already_up_to_date': already_up,
            'deps_changed': deps_changed,
            'message': pull_output.strip()
        })

    except Exception as e:
        logger.error(f'[UPDATE] Update apply error: {e}', exc_info=True)
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
    """Scan LAN for agents via SSDP."""
    from server.discovery import scan_for_agents
    nodes = scan_for_agents(timeout=5)
    return jsonify(nodes)

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
from core.config import cfg
from core.config import save_config, DATA_DIR
from core.hardware import discover_fans_and_sensors, discover_disks, set_pwm, read_disk_smart
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


@routes.route('/api/logging', methods=['GET'])
def api_get_logging():
    """Get current log level and retention."""
    from app import get_log_level
    return jsonify({
        'level': get_log_level(),
        'levels': ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        'retention_days': state.get('log_retention_days', 30),
        'retention_options': [7, 14, 30, 60, 90, 180, 365],
    })


@routes.route('/api/logging', methods=['POST'])
def api_set_logging():
    """Set log level and/or retention."""
    try:
        data = request.get_json(force=True)
        result = {}

        level = data.get('level')
        if level:
            from app import set_log_level
            if set_log_level(level):
                result['level'] = level
            else:
                return jsonify({'status': 'error', 'message': f'Invalid level: {level}'}), 400

        retention = data.get('retention_days')
        if retention is not None:
            retention = max(7, min(365, int(retention)))
            with state_lock:
                state['log_retention_days'] = retention
            result['retention_days'] = retention

        save_config()
        return jsonify({'status': 'ok', **result})
    except Exception as e:
        logger.error(f'Logging config error: {e}', exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ─── Telegram notifications ───────────────────────────────────────────

@routes.route('/api/telegram/config', methods=['POST'])
def api_telegram_config():
    """Save Telegram bot configuration and enable/disable notifications."""
    try:
        data = request.get_json(force=True)

        with state_lock:
            if 'bot_token' in data:
                state['telegram_bot_token'] = data['bot_token']
            if 'chat_id' in data:
                state['telegram_chat_id'] = data['chat_id']
            if 'enabled' in data:
                state['telegram_enabled'] = bool(data['enabled'])
            if 'events' in data:
                events = state.get('telegram_events', {})
                events.update(data['events'])
                state['telegram_events'] = events

        # Apply config to telegram module
        from core.telegram import configure
        configure(state.get('telegram_bot_token', ''), state.get('telegram_chat_id', ''))

        save_config()
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'Telegram config error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/telegram/test', methods=['POST'])
def api_telegram_test():
    """Send a test message to Telegram."""
    from core.telegram import send_message, is_configured
    if not is_configured():
        return jsonify({'status': 'error', 'message': 'Telegram not configured'}), 400
    ok = send_message('🧪 <b>FanControl</b>\nТестовое уведомление ✓')
    return jsonify({'status': 'ok' if ok else 'failed'})


@routes.route('/api/telegram/status')
def api_telegram_status():
    """Get Telegram configuration status."""
    from core.telegram import is_configured
    return jsonify({
        'configured': is_configured(),
        'enabled': bool(state.get('telegram_enabled', False)),
        'has_token': bool(state.get('telegram_bot_token')),
        'has_chat_id': bool(state.get('telegram_chat_id')),
        'events': state.get('telegram_events', {}),
    })


# ─── End Telegram ─────────────────────────────────────────────────────


@routes.route('/api/server-name', methods=['PUT'])
def api_update_server_name():
    """Update server name and push to all connected clients."""
    try:
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Name required'}), 400
        if len(name) > 64:
            return jsonify({'error': 'Name too long (max 64)'}), 400

        from core.config import save_config
        with state_lock:
            state['server_name'] = name

        save_config()
        invalidate_state_cache()

        # Push to all connected clients so UI updates instantly
        from app import socketio
        socketio.emit('server:name_changed', {'name': name})

        # Restart SSDP announcer with new name
        try:
            from server.socket_handlers import _restart_ssdp_announcer
            _restart_ssdp_announcer()
        except Exception as e:
            logger.warning(f'SSDP restart after rename failed: {e}')

        return jsonify({'status': 'ok', 'name': name})
    except Exception as e:
        logger.error(f'Server rename error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


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
            # Evict stale entries to prevent unbounded growth
            stale = [k for k, t in _smart_cache_time.items()
                     if (now - t) > SMART_CACHE_TTL * 2]
            for k in stale:
                _smart_cache.pop(k, None)
                _smart_cache_time.pop(k, None)

    return jsonify(result)


@routes.route('/api/nodes/<node_id>/disks/<disk_id>/smart')
def api_proxy_disk_smart(node_id, disk_id):
    """Proxy SMART request to a remote agent. Looks up by stable_id or node_id."""
    import logging
    logger = logging.getLogger('fancontrol')
    from server.node_registry import get_node, get_node_by_stable_id, list_nodes

    node = get_node_by_stable_id(node_id) or get_node(node_id)

    # Fallback: iterate all nodes (handles stale stable_id from frontend after delete/re-add)
    if not node:
        logger.warning(f'SMART proxy: node {node_id} not found by stable_id/node_id, scanning all nodes')
        for n in list_nodes():
            if n.get('stable_id') == node_id or n.get('node_id') == node_id:
                node = n
                break
        # Last resort: find node by disk ownership from telemetry
        if not node:
            for n in list_nodes():
                telemetry = n.get('telemetry') or {}
                hdds = telemetry.get('hdd_sensors') or {}
                if disk_id in hdds:
                    node = n
                    logger.info(f'SMART proxy: found node {n["node_id"]} by disk_id={disk_id}')
                    break

    if not node:
        logger.warning(f'SMART proxy: node {node_id} not found')
        return jsonify({'error': 'Node not found'}), 404
    ip = node.get('ip') or ''
    if not ip:
        logger.warning(f'SMART proxy: node {node_id} has no IP stored')
        return jsonify({'error': f'Node IP unknown for {node_id}'}), 400
    port = node.get('port', 5059)
    try:
        import urllib.request, json
        url = f'http://{ip}:{port}/api/agent/disks/{disk_id}/smart'
        logger.info(f'Proxying SMART request to {url}')
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'FanControl-Web')
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            logger.info(f'SMART proxy result for {disk_id}: has_attrs={bool(data.get("attributes"))}')
            return jsonify(data)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='ignore')
        logger.error(f'SMART proxy HTTP error: {e.code} {body[:200]}')
        return jsonify({'error': f'Agent returned {e.code}: {body[:200]}'}), e.code
    except Exception as e:
        logger.error(f'SMART proxy error: {e}')
        return jsonify({'error': str(e)}), 502


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
            temps.append(row[4] if row[4] is not None and row[4] > 0 else None)
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
    # Only require auth if FANCONTROL_UPDATE_TOKEN is explicitly configured
    if cfg.update_token:
        provided = request.headers.get('X-Update-Token') or request.args.get('token')
        if provided != cfg.update_token:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401

    try:
        from core.update_helper import do_git_pull, sync_repo_to_app, schedule_restart

        repo_dir = '/repo'
        app_dir = '/app'

        logger.info(f'[UPDATE] ====== START ====== PID={os.getpid()} VERSION={CONFIG_VERSION}')

        if not os.path.isdir(repo_dir) or not os.path.isfile(os.path.join(repo_dir, 'app.py')):
            return jsonify({'status': 'error', 'message': '/repo not ready'}), 500

        success, version = do_git_pull(repo_dir)
        if not success:
            return jsonify({'status': 'error', 'message': 'git pull failed'}), 500
        logger.info(f'[UPDATE] /repo version after pull: {version}')

        sync_repo_to_app(repo_dir, app_dir)

        schedule_restart(delay=1.0)

        return jsonify({'status': 'ok', 'message': 'Synced. Restarting in 1s...'})

    except Exception as e:
        logger.error(f'[UPDATE] ERROR: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/update/agents', methods=['POST'])
def api_update_agents():
    """Send update command to all online agents via WebSocket."""
    from server.agent_handlers import _emit_to_node, _node_to_sid
    from app import socketio
    from core.state import CONFIG_VERSION

    logger.info('[AGENTS-UPDATE] Endpoint called')

    data = request.get_json(silent=True) or {}
    node_ids = data.get('node_ids')  # Optional: specific nodes, or None for all

    with state_lock:
        nodes = dict(state.get('nodes', {}))

    logger.info(f'[AGENTS-UPDATE] state[nodes] has {len(nodes)} entries, '
                f'_node_to_sid has {len(_node_to_sid)} entries')

    updated = []
    skipped = []
    no_sid = []
    already_ok = []
    for nid, node in nodes.items():
        status = node.get('status', '?')
        has_sid = nid in _node_to_sid
        agent_ver = node.get('agent_version', '')
        logger.info(f'[AGENTS-UPDATE] node={nid} status={status} has_sid={has_sid} '
                    f'version={agent_ver}')
        if node_ids and nid not in node_ids:
            continue
        if status != 'online':
            skipped.append(nid)
            continue
        # Skip agents already at the correct version
        if agent_ver and agent_ver == CONFIG_VERSION:
            # Clear any stale pending_update flag
            with state_lock:
                state['nodes'].get(nid, {})['pending_update'] = False
                state['nodes'].get(nid, {})['update_started'] = None
            from server.node_registry import update_node_flags
            update_node_flags(nid, pending_update=False)
            already_ok.append(nid)
            logger.info(f'[AGENTS-UPDATE] Agent {nid} already at {CONFIG_VERSION} — skipped')
            continue
        # Set pending_update — agent polling will pick it up even if WebSocket fails
        import time as _time
        with state_lock:
            state['nodes'].get(nid, {})['pending_update'] = True
            state['nodes'].get(nid, {})['update_started'] = _time.time()
        from server.node_registry import update_node_flags
        update_node_flags(nid, pending_update=True)
        if not has_sid:
            no_sid.append(nid)
            logger.warning(f'[AGENTS-UPDATE] Agent {nid} has no SID — update via polling fallback')
            updated.append(nid)
            continue
        _emit_to_node(socketio, 'server:update', {}, nid)
        updated.append(nid)
        logger.info(f'[AGENTS-UPDATE] Sent update to {nid} ({node.get("name")})')

    if updated:
        logger.info(f'[AGENTS-UPDATE] Sent update to {len(updated)} agent(s)')
    if already_ok:
        logger.info(f'[AGENTS-UPDATE] {len(already_ok)} agent(s) already up to date')

    logger.info(f'[AGENTS-UPDATE] Result: updated={updated}, skipped={skipped}, '
                f'no_sid={no_sid}, already_ok={already_ok}')
    return jsonify({
        'status': 'ok',
        'updated': updated,
        'skipped': skipped,
        'no_sid': no_sid,
        'already_ok': already_ok,
        'message': f'Update sent to {len(updated)} agent(s), {len(skipped)} offline, '
                   f'{len(no_sid)} no SID, {len(already_ok)} already up to date'
    })


@routes.route('/api/nodes/<node_id>/request-logs', methods=['POST'])
def api_request_agent_logs(node_id):
    """Request log lines from a remote agent via WebSocket."""
    from server.agent_handlers import _emit_to_node, _node_to_sid
    from app import socketio

    if node_id not in state.get('nodes', {}):
        return jsonify({'error': 'Node not found'}), 404
    if node_id not in _node_to_sid:
        return jsonify({'error': 'Agent not connected'}), 503

    lines = (request.get_json(silent=True) or {}).get('lines', 100)
    _emit_to_node(socketio, 'server:request_logs', {'lines': lines}, node_id)
    return jsonify({'status': 'ok', 'message': 'Log request sent'})


@routes.route('/api/update/poll', methods=['POST'])
def api_update_poll():
    """Agent polls to check if an update is needed.

    Agent sends {agent_version, node_id}.
    Server responds with {update_available, server_version, should_update}.
    should_update = update_available AND (auto_update OR pending_update).
    """
    from core.state import CONFIG_VERSION
    data = request.get_json(silent=True) or {}
    agent_version = data.get('agent_version', '')
    node_id = data.get('node_id', '')

    with state_lock:
        node = state.get('nodes', {}).get(node_id, {})

    auto_update = node.get('auto_update', False)
    pending = node.get('pending_update', False)
    version_mismatch = agent_version and agent_version != CONFIG_VERSION
    should_update = version_mismatch and (auto_update or pending)

    # Don't consume pending_update here — clear it only when agent
    # reconnects with matching version (handled in agent:connect).
    # This allows retry on git fetch/reset failures.

    logger.info(f'[POLL] node={node_id} v={agent_version}→{CONFIG_VERSION} '
                f'mismatch={version_mismatch} auto={auto_update} pending={pending} '
                f'should_update={should_update}')
    return jsonify({
        'update_available': version_mismatch,
        'should_update': should_update,
        'server_version': CONFIG_VERSION,
    })


@routes.route('/api/nodes/<node_id>/auto-update', methods=['POST'])
def toggle_auto_update(node_id):
    """Toggle auto-update for a specific agent node."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', False)
    with state_lock:
        if node_id in state.get('nodes', {}):
            state['nodes'][node_id]['auto_update'] = enabled
    from server.node_registry import update_node_flags
    update_node_flags(node_id, auto_update=enabled)
    logger.info(f'[AUTO-UPDATE] node={node_id} auto_update={enabled}')
    return jsonify({'status': 'ok', 'node_id': node_id, 'auto_update': enabled})


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
            # Safety limit: if dict grows beyond 1000 entries, clear all but recent 100
            if len(_control_rate_limit) > 1000:
                sorted_items = sorted(_control_rate_limit.items(), key=lambda x: x[1], reverse=True)
                _control_rate_limit.clear()
                _control_rate_limit.update(sorted_items[:100])
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

        # Clear calibration_required after successful calibration
        health = fan.get('health', {})
        if health.get('calibration_required'):
            health['calibration_required'] = False
            health['status'] = 'healthy'
            health['rpm_baseline'] = 0

    save_config()
    return jsonify({'status': 'saved'})


@routes.route('/api/fan/<fan_id>/service', methods=['POST'])
def api_fan_service(fan_id):
    """Record fan replacement or service event."""
    data = request.get_json(force=True) or {}
    action = data.get('action', 'service')
    date = data.get('date', datetime.now().isoformat() if 'datetime' in dir() else '')

    with state_lock:
        fan = state.get('fans', {}).get(fan_id)
        if not fan:
            return jsonify({'error': 'Fan not found'}), 404

        health = fan.get('health', {})
        health['last_service_date'] = date
        health['calibration_required'] = True
        health['status'] = 'needs_calibration'
        health['rpm_baseline'] = 0
        health['slowdown_since'] = None
        health['stopped_since'] = None

    save_config()
    return jsonify({'status': 'ok', 'health': health})


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
    try:
        from server.node_registry import add_node
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Name required'}), 400
        node = add_node(name)
        return jsonify(node), 201
    except Exception as e:
        logger.error(f'api_add_node error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


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
        # Update in-memory state so next snapshot reflects the change immediately
        with state_lock:
            if node_id in state.get('nodes', {}):
                if name:
                    state['nodes'][node_id]['name'] = name
                if ip:
                    state['nodes'][node_id]['ip'] = ip
        invalidate_state_cache()
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Update failed'}), 500


@routes.route('/api/nodes/<node_id>', methods=['DELETE'])
def api_delete_node(node_id):
    """Delete a node — clean up DB, state, discovery cache, and disconnect agent."""
    from server.node_registry import delete_node, get_node
    from core.state import state, state_lock, invalidate_state_cache
    existing_node = get_node(node_id)
    deleted_ip = existing_node.get('ip', '') if existing_node else ''
    if delete_node(node_id):
        with state_lock:
            state.get('nodes', {}).pop(node_id, None)
        # Clean up any SID mapping (for backward compat with Socket.IO agents)
        from server.agent_handlers import _sid_to_node, _node_to_sid
        sid = _node_to_sid.pop(node_id, None)
        if sid:
            _sid_to_node.pop(sid, None)
        # Remove from SSDP discovered cache by both node_id and IP
        from server.discovery import _discovered_nodes, _lock as disc_lock
        with disc_lock:
            to_remove = [k for k, v in _discovered_nodes.items()
                         if v.get('node_id') == node_id or (deleted_ip and v.get('ip') == deleted_ip)]
            for k in to_remove:
                _discovered_nodes.pop(k, None)
        invalidate_state_cache()
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Node not found'}), 404


@routes.route('/api/nodes/<node_id>/config', methods=['POST'])
def api_push_config(node_id):
    """Push config to agent."""
    from server.node_registry import get_node, update_node_config
    from server.agent_handlers import _emit_to_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    update_node_config(node_id, data.get('config', {}))
    from app import socketio
    _emit_to_node(socketio, 'server:config_push', {
        'config': data.get('config', {}),
    }, node_id)
    return jsonify({'status': 'pushed'})


@routes.route('/api/nodes/<node_id>/mode', methods=['POST'])
def api_set_node_mode(node_id):
    """Set agent control mode."""
    from server.node_registry import get_node, update_node_control_mode
    from server.agent_handlers import _emit_to_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'server')
    if mode not in ('server', 'manual'):
        return jsonify({'error': 'Invalid mode'}), 400
    update_node_control_mode(node_id, mode)
    from app import socketio
    _emit_to_node(socketio, 'server:set_control_mode', {
        'mode': mode,
    }, node_id)
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


@routes.route('/api/nodes/scan-subnet', methods=['POST'])
def api_scan_subnet():
    """Fast TCP scan of local subnet for FanControl agents on port 5059."""
    from server.discovery import scan_subnet
    from server.node_registry import list_nodes
    try:
        data = request.get_json(silent=True) or {}
        port = int(data.get('port', 5059))
        results = scan_subnet(port=port)

        # Mark already-registered agents
        existing_nodes = list_nodes()
        existing_ips = {n['ip']: n for n in existing_nodes if n.get('ip')}
        for r in results:
            ip = r.get('ip', '')
            if ip in existing_ips:
                r['already_registered'] = True
                r['node_id'] = existing_ips[ip]['node_id']
                r['name'] = existing_ips[ip]['name']
            else:
                r['already_registered'] = False

        return jsonify(results)
    except Exception as e:
        logger.error(f'Subnet scan error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/nodes/probe', methods=['POST'])
def api_probe_ip():
    """Probe a specific IP for an agent."""
    from server.discovery import probe_agent
    from server.node_registry import list_nodes, get_node
    data = request.get_json(silent=True) or {}
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
    data = request.get_json(silent=True) or {}
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

    # Fetch api_token from agent via HTTP
    api_token = ''
    if info:
        try:
            import urllib.request
            import json
            resp = urllib.request.urlopen(f'http://{ip}:{port}/api/agent/status', timeout=5)
            status = json.loads(resp.read())
            api_token = status.get('api_token', '')
        except Exception:
            pass

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
    from server.node_registry import list_nodes
    existing_ips = {n['ip'] for n in list_nodes() if n.get('ip')}
    with _lock:
        agents = [a for a in _discovered_nodes.values() if a.get('ip') not in existing_ips]
        return jsonify(agents)


@routes.route('/api/discovered/<node_id>/accept', methods=['POST'])
def api_accept_discovered(node_id):
    """Accept a discovered agent and register it.

    Fetches the api_token from the agent's /api/agent/status endpoint
    over unicast HTTP (token is no longer broadcast via SSDP).

    Accepts optional ?ip= query param for agents found via subnet scan
    (not stored in SSDP _discovered_nodes).
    """
    try:
        from server.discovery import _discovered_nodes, _lock
        from server.node_registry import add_node, list_nodes
        import urllib.request
        from flask import request as flask_request

        agent_ip = ''
        agent_name = node_id

        with _lock:
            agent = _discovered_nodes.get(node_id)
            if agent:
                agent_ip = agent.get('ip', '')
                agent_name = agent.get('name', node_id)
            else:
                # Fallback: IP from query param (subnet scan) or already-registered check
                agent_ip = flask_request.args.get('ip', '').strip()
                if not agent_ip:
                    # Check if already registered by any existing node
                    return jsonify({'error': 'Agent not found — no IP provided'}), 404

        # Check if agent with this IP is already registered
        existing_ips = {n['ip']: n for n in list_nodes() if n.get('ip')}
        if agent_ip in existing_ips:
            existing = existing_ips[agent_ip]
            with _lock:
                _discovered_nodes.pop(node_id, None)
            return jsonify({'message': 'Agent already registered', 'node_id': existing['node_id']}), 200

        # Fetch api_token from agent via unicast HTTP
        api_token = ''
        try:
            url = f'http://{agent_ip}:5059/api/agent/status'
            req = urllib.request.urlopen(url, timeout=5)
            import json
            status = json.loads(req.read())
            api_token = status.get('api_token', '')
            agent_name = status.get('node_name', agent_name)
        except Exception as e:
            logger.warning(f'Could not fetch token from agent {agent_ip}: {e}')
            return jsonify({'error': f'Could not reach agent at {agent_ip}'}), 502

        node = add_node(agent_name, api_token=api_token, ip=agent_ip)

        # Populate state['nodes'] as 'pending' — will go 'online' on first telemetry
        from core.state import state, state_lock, invalidate_state_cache
        new_node = {
            'node_id': node['node_id'],
            'stable_id': node.get('stable_id', ''),
            'name': node['name'],
            'status': 'pending',
            'control_mode': 'server',
            'config': {},
            'dsm_schemes': [],
            'kernel_info': {},
            'agent_version': '',
            'auto_update': 0,
            'pending_update': 0,
            'update_started': None,
        }
        with state_lock:
            state['nodes'][node['node_id']] = new_node
        invalidate_state_cache()

        with _lock:
            _discovered_nodes.pop(node_id, None)

        logger.info(f'Accepted agent {node["name"]} ({agent_ip}) node_id={node["node_id"]}')
        return jsonify(node), 201
    except Exception as e:
        logger.error(f'api_accept_discovered error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ============================================================================
# HTTP Agent Protocol — replaces Socket.IO for agent↔server communication
# ============================================================================

@routes.route('/api/agent/telemetry', methods=['POST'])
def api_agent_telemetry_http():
    """Receive telemetry from agent via HTTP POST. Returns pending commands.

    This is the primary agent→server channel. Agent sends telemetry every ~5s.
    Server responds with any queued commands (config push, mode change, etc.).
    """
    try:
        data = request.get_json(silent=True) or {}
        api_token = data.get('api_token', '')
        telemetry = data.get('telemetry', {})
        agent_node_id = data.get('node_id', '')

        if not api_token:
            return jsonify({'error': 'Missing api_token'}), 400

        from server.node_registry import get_node_by_token, update_node_status
        from server.agent_handlers import drain_commands, _process_agent_data

        node = get_node_by_token(api_token)
        if not node:
            # Auto-register unknown agent
            from server.node_registry import add_node
            try:
                node = add_node(agent_node_id or 'Agent', api_token=api_token, ip='')
                logger.info(f'[HTTP] Auto-registered new agent: {agent_node_id} token={api_token[:8]}...')
            except Exception as e:
                logger.error(f'[HTTP] Auto-register failed: {e}')
                node = get_node_by_token(api_token)
                if not node:
                    return jsonify({'error': f'Registration failed: {e}'}), 500

        node_id = node['node_id']

        # Update state
        _process_agent_data(node_id, telemetry)

        # Update agent version if provided
        agent_version = data.get('version', '')
        if agent_version and agent_version != node.get('agent_version', ''):
            from server.node_registry import update_node_version
            update_node_version(node_id, agent_version)
            logger.info(f'[HTTP] Agent {node_id} version updated: {node.get("agent_version", "?")} → {agent_version}')

        # Drain command queue
        commands = drain_commands(node_id)
        return jsonify({'status': 'ok', 'commands': commands})
    except Exception as e:
        logger.error(f'api_agent_telemetry error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/agent/poll', methods=['GET'])
def api_agent_poll_http():
    """Agent polls for pending commands. Fallback for missed piggyback."""
    try:
        api_token = request.args.get('api_token', '')
        if not api_token:
            return jsonify({'error': 'Missing api_token'}), 400

        from server.node_registry import get_node_by_token, update_node_version
        from server.agent_handlers import drain_commands

        node = get_node_by_token(api_token)
        if not node:
            return jsonify({'error': 'Unknown agent'}), 401

        node_id = node['node_id']

        # Update last_seen and version if provided
        agent_version = request.args.get('version', '')
        if agent_version and agent_version != node.get('agent_version', ''):
            update_node_version(node_id, agent_version)
        from server.node_registry import update_node_status
        update_node_status(node_id, 'online')

        commands = drain_commands(node_id)
        return jsonify({'commands': commands})
    except Exception as e:
        logger.error(f'api_agent_poll error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/agent/update_result', methods=['POST'])
def api_agent_update_result_http():
    """Agent reports update progress via HTTP."""
    try:
        data = request.get_json(silent=True) or {}
        api_token = data.get('api_token', '')

        from server.node_registry import get_node_by_token
        node = get_node_by_token(api_token)
        if not node:
            return jsonify({'error': 'Unknown agent'}), 401

        node_id = node['node_id']

        # Process update result (same as Socket.IO handler)
        status = data.get('status', '')
        version = data.get('version', '')

        if status == 'synced' and version:
            from core.state import CONFIG_VERSION
            from server.node_registry import update_node_flags
            # Update is done — clear pending flag
            update_node_flags(node_id, pending_update=False)
            with state_lock:
                if node_id in state.get('nodes', {}):
                    state['nodes'][node_id]['pending_update'] = False
            invalidate_state_cache()
            logger.info(f'Agent {node_id} updated to {version}')

        # Broadcast to browsers
        from app import socketio
        socketio.emit('agent:update_result', {
            'node_id': node_id,
            'status': status,
            'version': version,
            'message': data.get('message', ''),
        })
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'api_agent_update_result error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/agent/command', methods=['POST'])
def api_agent_queue_command():
    """Browser queues a command for delivery to agent via HTTP poll."""
    try:
        data = request.get_json(silent=True) or {}
        node_id = data.get('node_id', '')
        command_type = data.get('type', '')
        payload = data.get('data', {})

        if not node_id or not command_type:
            return jsonify({'error': 'Missing node_id or type'}), 400

        from server.agent_handlers import queue_command
        queue_command(node_id, command_type, payload)
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'api_agent_queue_command error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/agent/logs', methods=['POST'])
def api_agent_logs_http():
    """Receive logs from agent via HTTP (response to request_logs command)."""
    try:
        data = request.get_json(silent=True) or {}
        api_token = data.get('api_token', '')

        from server.node_registry import get_node_by_token
        node = get_node_by_token(api_token)
        if not node:
            # Try by node_id
            node_id = data.get('node_id', '')
            node = get_node(node_id) if node_id else None
        if not node:
            return jsonify({'error': 'Unknown agent'}), 401

        node_id = node['node_id']
        lines = data.get('lines', [])

        # Forward logs to browsers via Socket.IO
        from app import socketio
        socketio.emit('agent:logs', {'node_id': node_id, 'lines': lines})
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'api_agent_logs error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/agent/ack', methods=['POST'])
def api_agent_ack():
    """Agent acknowledges command delivery."""
    try:
        data = request.get_json(silent=True) or {}
        api_token = data.get('api_token', '')
        command_id = data.get('command_id', '')
        status = data.get('status', 'delivered')

        if not api_token or not command_id:
            return jsonify({'error': 'Missing api_token or command_id'}), 400

        from server.agent_handlers import ack_command
        ack_command(command_id, status)
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'api_agent_ack error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Diagnostic endpoints
# ============================================================================

@routes.route('/api/health', methods=['GET'])
def api_health():
    """Quick health check — server version, agent versions, pending agents."""
    from core.state import CONFIG_VERSION
    from server.agent_handlers import _node_to_sid

    with state_lock:
        nodes = dict(state.get('nodes', {}))

    agents = []
    pending_agents = []
    for nid, node in nodes.items():
        info = {
            'node_id': nid,
            'version': node.get('agent_version', '?'),
            'status': node.get('status', '?'),
            'pending': bool(node.get('pending_update', 0)),
            'auto_update': bool(node.get('auto_update', 0)),
            'connected': nid in _node_to_sid,
        }
        agents.append(info)
        if info['pending']:
            pending_agents.append(nid)

    return jsonify({
        'server_version': CONFIG_VERSION,
        'agents': agents,
        'pending_agents': pending_agents,
        'total_agents': len(agents),
    })


@routes.route('/api/debug', methods=['GET'])
def api_debug():
    """Detailed diagnostic info — versions, state, config, recent logs."""
    from core.state import CONFIG_VERSION
    from server.agent_handlers import _node_to_sid, _sid_to_node

    with state_lock:
        nodes = dict(state.get('nodes', {}))

    agents = []
    for nid, node in nodes.items():
        info = {
            'node_id': nid,
            'name': node.get('name', '?'),
            'version': node.get('agent_version', '?'),
            'status': node.get('status', '?'),
            'pending_update': bool(node.get('pending_update', 0)),
            'auto_update': bool(node.get('auto_update', 0)),
            'control_mode': node.get('control_mode', '?'),
            'sid': _node_to_sid.get(nid, None),
            'last_seen': node.get('last_seen', '?'),
            'ip': node.get('ip', '?'),
        }
        agents.append(info)

    # Server state
    git_hash = ''
    try:
        result = subprocess.run(
            ['git', '-C', '/repo', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        git_hash = result.stdout.strip()
    except Exception:
        pass

    # Pending SQLite flags
    pending_db = {}
    try:
        from server.node_registry import list_nodes
        for n in list_nodes():
            if n.get('pending_update'):
                pending_db[n['node_id']] = True
    except Exception:
        pass

    return jsonify({
        'server': {
            'version': CONFIG_VERSION,
            'git_hash': git_hash,
            'data_dir': str(DATA_DIR),
            'uptime': _get_uptime(),
        },
        'agents': agents,
        'sid_map': {nid: sid[:8] + '...' for nid, sid in _node_to_sid.items()},
        'pending_in_db': pending_db,
        'state_keys': list(state.keys()),
    })


def _get_uptime():
    """Get process uptime."""
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF)
        return f'{r.ru_utime + r.ru_stime:.1f}s cpu'
    except Exception:
        return '?'



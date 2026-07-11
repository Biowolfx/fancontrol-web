"""Agent client — HTTP-first communication with server.

Primary channel: HTTP POST /api/agent/telemetry (every 5s, with api_token).
Commands returned in response body. Fallback: HTTP GET /api/agent/poll.
Socket.IO kept as secondary channel for backward compatibility.
"""

import json as _json
import logging
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
from typing import Optional

import socketio

from core.state import state, state_lock, invalidate_state_cache, CONFIG_VERSION
from core.config import cfg
from agent.config import init_agent_config, init_token
from agent.telemetry import get_telemetry

logger = logging.getLogger('fancontrol')

# Initialize agent identity from env/config.json
SERVER_URL, NODE_ID, NODE_NAME = init_agent_config()
API_TOKEN = init_token()
TELEMETRY_INTERVAL = cfg.telemetry_interval

# HTTP URL (convert ws:// → http://)
HTTP_URL = SERVER_URL.replace('ws://', 'http://').replace('wss://', 'https://')

# Populate state with agent identity
state['control_mode'] = 'server'
state['server_connected'] = False
state['server_url'] = SERVER_URL
state['node_id'] = NODE_ID
state['node_name'] = NODE_NAME
state['api_token'] = API_TOKEN
state['agent_config_snapshot'] = None

try:
    from core.kernel_detect import get_kernel_info
    state['kernel_info'] = get_kernel_info()
except Exception:
    state['kernel_info'] = {}

_sio: Optional[socketio.Client] = None
_telemetry_thread: Optional[threading.Thread] = None


# ============================================================================
# Command processor — handles commands received from server
# ============================================================================

def _process_command(cmd):
    """Process a command received from server (via HTTP response or Socket.IO)."""
    cmd_type = cmd.get('type', '')
    data = cmd.get('data', {})

    if cmd_type == 'config_push':
        from agent.telemetry import apply_server_config
        from agent.config import save_local_config
        apply_server_config(data.get('config', {}))
        save_local_config()
        logger.info('[cmd] Applied server config push')

    elif cmd_type == 'set_control_mode':
        mode = data.get('mode', 'server')
        state['control_mode'] = mode
        invalidate_state_cache()
        logger.info(f'[cmd] Control mode set to: {mode}')

    elif cmd_type == 'command':
        _handle_fan_command(data)

    elif cmd_type == 'dsm_apply':
        _handle_dsm_apply(data)

    elif cmd_type == 'update':
        _handle_update(data)

    elif cmd_type == 'request_logs':
        _handle_request_logs(data)

    elif cmd_type == 'node_id_push':
        new_id = data.get('node_id')
        if new_id and new_id != state.get('node_id'):
            logger.info(f'[cmd] Received node_id push: {state.get("node_id")} → {new_id}')
            state['node_id'] = new_id
            from agent.config import persist_node_id
            persist_node_id(new_id, state.get('api_token', ''))
    else:
        logger.warning(f'[cmd] Unknown command type: {cmd_type}')


def _handle_fan_command(data):
    """Handle fan control command."""
    command = data.get('command', '')
    fan_id = data.get('fan_id', '')
    value = data.get('value', 0)

    if command == 'set_fan':
        from core.hardware import set_pwm
        set_pwm(fan_id, value)
        logger.info(f'[cmd] Set fan {fan_id} to {value}%')
    else:
        logger.warning(f'[cmd] Unknown fan command: {command}')


def _handle_dsm_apply(data):
    """Handle DSM scheme apply command."""
    scheme_type = data.get('scheme_type', '')
    entries = data.get('entries', [])
    logger.info(f'[cmd] Apply DSM scheme: {scheme_type}, {len(entries)} entries')
    try:
        from core.dsm import update_scheme_entry
        for entry in entries:
            update_scheme_entry(
                scheme_type=scheme_type,
                index=entry.get('index', 0),
                fan_speed_pct=entry.get('fan_speed_pct', 50),
                action=entry.get('action', ''),
                threshold_temp=entry.get('threshold_temp', 0),
            )
    except Exception as e:
        logger.error(f'[cmd] DSM apply failed: {e}')


def _handle_update(data):
    """Handle server-pushed update command."""
    logger.info('[cmd] Server requests update')
    # Emit progress via Socket.IO if connected
    if _sio:
        _sio.emit('agent:update_result', {
            'node_id': state.get('node_id'),
            'status': 'pulling',
        })
    # Also report via HTTP
    _report_update_status('pulling')

    repo_dir = '/repo'
    git_dir = os.path.join(repo_dir, '.git')
    if not os.path.isdir(git_dir):
        logger.warning('[update] /repo has no .git')
        _report_update_status('error', 'No .git in /repo')
        return

    try:
        fetch = subprocess.run(
            ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if fetch.returncode != 0:
            _report_update_status('error', f'git fetch failed: {fetch.stderr[:200]}')
            return

        reset = subprocess.run(
            ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
        if reset.returncode != 0:
            _report_update_status('error', f'git reset failed: {reset.stderr[:200]}')
            return

        _report_update_status('synced', CONFIG_VERSION)
        logger.info(f'[update] Synced to latest, restarting...')
        time.sleep(1)
        threading.Timer(1.0, os._exit, args=[0]).start()
    except Exception as e:
        _report_update_status('error', str(e))


def _handle_request_logs(data):
    """Handle request for logs from server."""
    lines = data.get('lines', 100)
    log_file = cfg.log_dir / 'fancontrol.log'
    log_lines = []
    try:
        if log_file.exists():
            with open(log_file) as f:
                all_lines = f.readlines()
                log_lines = [l.rstrip() for l in all_lines[-lines:]]
    except Exception as e:
        log_lines = [f'Error reading logs: {e}']

    result = {'node_id': state.get('node_id'), 'lines': log_lines}
    if _sio:
        _sio.emit('agent:logs', result)

    # Also report via HTTP
    _http_post('/api/agent/logs', result)


def _report_update_status(status, version='', message=''):
    """Report update status to server via HTTP."""
    _http_post('/api/agent/update_result', {
        'api_token': API_TOKEN,
        'node_id': state.get('node_id'),
        'status': status,
        'version': version,
        'message': message,
    })


# ============================================================================
# HTTP helpers
# ============================================================================

def _http_post(path, data, timeout=10):
    """POST JSON to server. Returns parsed response or None."""
    try:
        payload = _json.dumps(data).encode()
        req = urllib.request.Request(
            f'{HTTP_URL}{path}',
            data=payload,
            headers={'Content-Type': 'application/json'},
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        return _json.loads(resp.read())
    except Exception as e:
        logger.debug(f'HTTP POST {path} failed: {e}')
        return None


def _http_get(path, timeout=5):
    """GET from server. Returns parsed response or None."""
    try:
        resp = urllib.request.urlopen(f'{HTTP_URL}{path}', timeout=timeout)
        return _json.loads(resp.read())
    except Exception as e:
        logger.debug(f'HTTP GET {path} failed: {e}')
        return None


# ============================================================================
# HTTP Telemetry Loop — primary agent→server channel
# ============================================================================

def _telemetry_http_loop():
    """Send telemetry via HTTP POST, receive commands in response."""
    logger.info('[http-telemetry] Started')
    while True:
        time.sleep(TELEMETRY_INTERVAL)
        try:
            telemetry = get_telemetry()
            logger.info(f'[telemetry] fans={list(telemetry["fans"].keys())} '
                        f'temps={list(telemetry["temp_sensors"].keys())} '
                        f'hdds={list(telemetry["hdd_sensors"].keys())} '
                        f'node_id={state.get("node_id")}')
            result = _http_post('/api/agent/telemetry', {
                'api_token': API_TOKEN,
                'node_id': state.get('node_id'),
                'telemetry': telemetry,
            })
            if result:
                # Mark as connected (server responded)
                if not state.get('server_connected'):
                    state['server_connected'] = True
                    invalidate_state_cache()
                    logger.info('[http-telemetry] Server connected')
                # Process piggybacked commands
                for cmd in result.get('commands', []):
                    _process_command(cmd)
            else:
                # Server didn't respond — might be offline
                if state.get('server_connected'):
                    state['server_connected'] = False
                    invalidate_state_cache()
                    logger.warning('[http-telemetry] Server unreachable')
        except Exception as e:
            logger.error(f'[http-telemetry] Error: {e}')


# ============================================================================
# HTTP Command Poll Loop — fallback for missed piggyback
# ============================================================================

def _command_poll_loop():
    """Poll server for pending commands every 15 seconds."""
    logger.info('[http-poll] Started')
    while True:
        time.sleep(15)
        try:
            result = _http_get(f'/api/agent/poll?api_token={API_TOKEN}')
            if result:
                commands = result.get('commands', [])
                if commands:
                    logger.info(f'[http-poll] Received {len(commands)} commands')
                for cmd in commands:
                    _process_command(cmd)
        except Exception:
            pass


# ============================================================================
# Socket.IO — kept for backward compatibility and initial handshake
# ============================================================================


def _update_check_loop():
    """Poll server for updates via HTTP."""
    POLL_INTERVAL = 15
    first_run = True
    while True:
        time.sleep(10 if first_run else POLL_INTERVAL)
        first_run = False
        try:
            if not state.get('server_connected'):
                continue

            current_node_id = state.get('node_id', NODE_ID)
            payload = _json.dumps({
                'agent_version': CONFIG_VERSION,
                'node_id': current_node_id,
            }).encode()

            req = urllib.request.Request(
                f'{HTTP_URL}/api/update/poll',
                data=payload,
                headers={'Content-Type': 'application/json'},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = _json.loads(resp.read())

            if result.get('should_update'):
                server_ver = result.get('server_version', '?')
                logger.info(f'[update-check] Server requests update: {CONFIG_VERSION} → {server_ver}')
                _report_update_status('pulling', server_ver)

                repo_dir = '/repo'
                git_dir = os.path.join(repo_dir, '.git')
                if not os.path.isdir(git_dir):
                    _report_update_status('error', 'No .git in /repo')
                    continue

                fetch = subprocess.run(
                    ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                )
                if fetch.returncode != 0:
                    _report_update_status('error', f'git fetch failed: {fetch.stderr[:200]}')
                    continue

                reset = subprocess.run(
                    ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                )
                if reset.returncode != 0:
                    _report_update_status('error', f'git reset failed: {reset.stderr[:200]}')
                    continue

                _report_update_status('synced', CONFIG_VERSION)
                time.sleep(1)
                threading.Timer(1.0, os._exit, args=[0]).start()
                break
            else:
                logger.debug(f'[update-check] Up to date: {CONFIG_VERSION}')

        except Exception as e:
            logger.warning(f'[update-check] Poll failed: {e}')


# ============================================================================
# Client startup
# ============================================================================

def start_client():
    """Start agent communication — HTTP telemetry + Socket.IO handshake."""
    global _sio, _telemetry_thread

    logger.info(f'[start_client] SERVER_URL={SERVER_URL}, NODE_ID={NODE_ID}')

    from agent.announcer import start_announcer, _handle_msearch
    start_announcer(NODE_ID, NODE_NAME)

    responder_thread = threading.Thread(
        target=_handle_msearch,
        args=(NODE_ID, NODE_NAME),
        daemon=True
    )
    responder_thread.start()
    logger.info('[agent] M-SEARCH responder started')

    if not SERVER_URL:
        logger.info('No SERVER_URL set — running standalone')
        return

    # Start HTTP telemetry loop (primary channel — no Socket.IO needed)
    _telemetry_thread = threading.Thread(target=_telemetry_http_loop, daemon=True)
    _telemetry_thread.start()
    logger.info('[agent] HTTP telemetry loop started')

    # Start HTTP command poll loop (fallback)
    poll_thread = threading.Thread(target=_command_poll_loop, daemon=True)
    poll_thread.start()
    logger.info('[agent] HTTP command poll loop started')

    # Start update check loop
    update_thread = threading.Thread(target=_update_check_loop, daemon=True)
    update_thread.start()
    logger.info('[agent] Update check loop started')

    # Socket.IO — kept for initial handshake + backward compatibility
    _sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=1,
        reconnection_delay_max=30,
    )

    from agent.handlers import make_handlers
    handlers = make_handlers(_sio)
    for event, handler in handlers.items():
        _sio.on(event, handler)
    logger.info(f'[agent] Registered Socket.IO handlers: {list(handlers.keys())}')

    try:
        _sio.connect(SERVER_URL)
    except Exception as e:
        logger.warning(f'Socket.IO connect failed (HTTP telemetry continues): {e}')

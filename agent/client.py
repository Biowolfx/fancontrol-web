"""WebSocket client — connects agent to server."""

import logging
import os
import threading
import time
from typing import Optional

import socketio

from core.state import state, state_lock, get_state, invalidate_state_cache

logger = logging.getLogger('fancontrol')

SERVER_URL = os.environ.get('SERVER_URL', '')
API_TOKEN = os.environ.get('API_TOKEN', '')
NODE_ID = os.environ.get('NODE_ID', 'agent-1')
NODE_NAME = os.environ.get('NODE_NAME', 'Agent 1')
TELEMETRY_INTERVAL = int(os.environ.get('TELEMETRY_INTERVAL', '5'))


def _init_agent_config():
    """Load agent config from config.json if not set via env vars.
    Auto-generates node_id if missing."""
    global SERVER_URL, NODE_ID, NODE_NAME
    import json
    from pathlib import Path

    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
    config = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass

    logger.info(f'[agent-config] config_path={config_path}, exists={config_path.exists()}, server_url_in_file={config.get("server_url", "NONE")}')

    if not SERVER_URL and config.get('server_url'):
        SERVER_URL = config['server_url']
    if NODE_ID == 'agent-1' and config.get('node_id'):
        NODE_ID = config['node_id']
    if NODE_NAME == 'Agent 1' and config.get('node_name'):
        NODE_NAME = config['node_name']

    # Auto-generate stable node_id if still default
    if NODE_ID == 'agent-1' and not config.get('node_id'):
        import uuid
        NODE_ID = f'agent-{uuid.uuid4().hex[:12]}'
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config['node_id'] = NODE_ID
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception:
            pass

    logger.info(f'[agent-config] SERVER_URL={SERVER_URL}, NODE_ID={NODE_ID}, NODE_NAME={NODE_NAME}')


_init_agent_config()


def _init_token():
    """Generate or load API token for this agent."""
    import json
    from pathlib import Path

    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'

    # If token provided via env, use it
    if API_TOKEN:
        return API_TOKEN

    # Try to load from config
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
                if config.get('api_token'):
                    return config['api_token']
        except Exception:
            pass

    # Generate new token
    import uuid
    new_token = uuid.uuid4().hex

    # Save to config (best-effort)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
            except Exception:
                pass

        config['api_token'] = new_token
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.warning(f'Could not persist api_token to config: {e}')

    logger.info(f'Generated new API token: {new_token[:8]}...')
    return new_token


API_TOKEN = _init_token()

# Agent-specific state fields
state['control_mode'] = 'server'  # 'server' or 'manual'
state['server_connected'] = False
state['server_url'] = SERVER_URL
state['node_id'] = NODE_ID
state['node_name'] = NODE_NAME
state['api_token'] = API_TOKEN
state['agent_config_snapshot'] = None

# Detect kernel info
try:
    from core.kernel_detect import get_kernel_info
    state['kernel_info'] = get_kernel_info()
except Exception:
    state['kernel_info'] = {}

_sio: Optional[socketio.Client] = None
_telemetry_thread: Optional[threading.Thread] = None


def _on_connect():
    logger.info(f'Connected to server: {SERVER_URL}')
    state['server_connected'] = True
    invalidate_state_cache()

    # Send agent info + local config
    _sio.emit('agent:connect', {
        'node_id': NODE_ID,
        'node_name': NODE_NAME,
        'api_token': API_TOKEN,
        'control_mode': state['control_mode'],
        'config': _get_local_config(),
    })


def _on_disconnect():
    logger.warning('Disconnected from server')
    state['server_connected'] = False
    invalidate_state_cache()


def _on_config_push(data):
    """Server pushes new config — apply and save locally."""
    with state_lock:
        if state['control_mode'] != 'server':
            logger.info('Config push ignored — in manual mode')
            return

        state['agent_config_snapshot'] = _get_local_config()
        _apply_config(data.get('config', {}))
        invalidate_state_cache()
        logger.info('Applied server config')

    _save_local_config()


def _on_set_control_mode(data):
    """Server requests mode change."""
    mode = data.get('mode', 'server')
    with state_lock:
        state['control_mode'] = mode
        invalidate_state_cache()
    logger.info(f'Control mode set to: {mode}')
    _save_local_config()


def _on_command(data):
    """Server sends a command (set_fan, etc.)."""
    cmd = data.get('command')
    if cmd == 'set_fan':
        fan_id = data.get('fan_id')
        value = data.get('value')
        with state_lock:
            if fan_id in state['fans']:
                state['fans'][fan_id]['manual_pct'] = value
                state['fans'][fan_id]['mode'] = 'manual'
        invalidate_state_cache()
        from core.hardware import set_pwm
        set_pwm(fan_id, int(value * 255 // 100))


def _on_dsm_apply(data):
    """Server pushes a DSM scheme change — apply locally."""
    scheme_type = data.get('scheme_type')
    entries = data.get('entries', [])
    logger.info(f'Received DSM scheme apply: {scheme_type} ({len(entries)} entries)')
    try:
        from core.dsm_fan import update_scheme_entry
        for entry in entries:
            idx = entry.get('index')
            field = entry.get('field', 'fan_speed')
            value = entry.get('value')
            if idx is not None and value is not None:
                update_scheme_entry(scheme_type, idx, field, value)
                logger.info(f'Updated {scheme_type}[{idx}].{field} = {value}')
        # Reload schemes in state
        with state_lock:
            try:
                from core.dsm_fan import is_dsm_fan_available, get_all_schemes
                if is_dsm_fan_available():
                    state['dsm_schemes'] = get_all_schemes()
            except Exception:
                pass
        invalidate_state_cache()
        logger.info(f'DSM scheme {scheme_type} applied successfully')
    except Exception as e:
        logger.error(f'Failed to apply DSM scheme: {e}')


def _telemetry_loop():
    """Send telemetry to server periodically."""
    while True:
        time.sleep(TELEMETRY_INTERVAL)
        if _sio and state['server_connected']:
            try:
                telemetry = _get_telemetry()
                logger.info(f'[telemetry] fans={list(telemetry["fans"].keys())} '
                            f'temps={list(telemetry["temp_sensors"].keys())} '
                            f'hdds={list(telemetry["hdd_sensors"].keys())} '
                            f'node_id={NODE_ID}')
                _sio.emit('agent:telemetry', {
                    'node_id': NODE_ID,
                    'telemetry': telemetry,
                })
            except Exception as e:
                logger.error(f'Telemetry send failed: {e}')


def _get_local_config():
    """Get current local fan config including kernel info and DSM schemes."""
    with state_lock:
        config = {
            'fans': {k: {kk: vv for kk, vv in v.items()
                         if kk not in ('rpm', 'pwm_value')}
                     for k, v in state['fans'].items()},
            'temp_sensors': state['temp_sensors'],
            'hdd_sensors': state['hdd_sensors'],
            'kernel_info': state.get('kernel_info', {}),
        }
        # Include DSM schemes if available
        try:
            from core.dsm_fan import is_dsm_fan_available, get_all_schemes
            if is_dsm_fan_available():
                config['dsm_schemes'] = get_all_schemes()
                logger.info(f'Including {len(config["dsm_schemes"])} DSM schemes in config')
        except Exception as e:
            logger.debug(f'Could not load DSM schemes: {e}')
        return config


def _get_telemetry():
    """Get current telemetry data."""
    with state_lock:
        return {
            'fans': {k: {'rpm': v.get('rpm', 0),
                         'pwm_value': v.get('pwm_value', 0),
                         'control_method': v.get('control_method', 'hwmon'),
                         'label': v.get('label', k)}
                     for k, v in state['fans'].items()},
            'temp_sensors': {k: {'value': v.get('value', 0),
                                 'label': v.get('label', k)}
                            for k, v in state['temp_sensors'].items()},
            'hdd_sensors': {k: {'temp': v.get('temp', 0),
                                'label': v.get('label', k)}
                           for k, v in state['hdd_sensors'].items()},
            'failsafe': state.get('failsafe', False),
            'standby_mode': state.get('standby_mode', False),
        }


def _save_local_config():
    """Save current config to local config.json, preserving wizard fields."""
    import json
    from pathlib import Path

    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # Read existing config to preserve wizard-set fields
        existing = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    existing = json.load(f)
            except Exception:
                pass

        with state_lock:
            # Update runtime fields, preserve mode/server_url/node_name etc.
            existing.update({
                'fans': {k: {kk: vv for kk, vv in v.items()
                             if kk not in ('rpm', 'pwm_value')}
                         for k, v in state['fans'].items()},
                'temp_sensors': state['temp_sensors'],
                'hdd_sensors': state['hdd_sensors'],
                'control_mode': state.get('control_mode', 'server'),
                'initialized': state.get('initialized', False),
                'api_token': state.get('api_token', existing.get('api_token', '')),
                'node_id': state.get('node_id', existing.get('node_id', '')),
                'node_name': state.get('node_name', existing.get('node_name', '')),
                'server_url': state.get('server_url', existing.get('server_url', '')),
            })
        with open(config_path, 'w') as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        logger.error(f'Failed to save local config: {e}')


def _apply_config(config):
    """Apply config received from server."""
    with state_lock:
        for fan_id, fan_cfg in config.get('fans', {}).items():
            if fan_id in state['fans']:
                for key in ('mode', 'target_temp', 'manual_pct', 'sensors',
                            'sensor_mode', 'schedule', 'inverted'):
                    if key in fan_cfg:
                        state['fans'][fan_id][key] = fan_cfg[key]


def _on_node_id_push(data):
    """Server pushes correct node_id and token — update and save locally."""
    new_node_id = data.get('node_id', '')
    new_token = data.get('token', '')

    global NODE_ID, API_TOKEN
    changed = False

    if new_node_id and new_node_id != NODE_ID:
        logger.info(f'Received node_id from server: {NODE_ID} → {new_node_id}')
        NODE_ID = new_node_id
        state['node_id'] = new_node_id
        changed = True

    if new_token and new_token != API_TOKEN:
        API_TOKEN = new_token
        state['api_token'] = new_token
        changed = True

    if changed:
        import json
        from pathlib import Path
        config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
        try:
            config = {}
            if config_path.exists():
                with open(config_path) as f:
                    config = json.load(f)
            if new_node_id:
                config['node_id'] = new_node_id
            if new_token:
                config['api_token'] = new_token
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            logger.warning(f'Could not persist node_id/token to config: {e}')


def _on_dsm_apply(data):
    """Server pushes DSM scheme changes — apply to local scemd.xml."""
    scheme_type = data.get('scheme_type')
    entries = data.get('entries', [])
    logger.info(f'Received DSM scheme apply: {scheme_type} ({len(entries)} entries)')

    try:
        from core.dsm_fan import update_scheme_entry
        for entry in entries:
            idx = entry.get('index')
            if idx is not None:
                update_scheme_entry(
                    scheme_type, idx,
                    fan_speed_pct=entry.get('fan_speed_pct'),
                    action=entry.get('action'),
                    threshold_temp=entry.get('threshold_temp'),
                )
        logger.info(f'DSM scheme {scheme_type} applied successfully')
    except Exception as e:
        logger.error(f'Failed to apply DSM scheme: {e}')


def start_client():
    """Start the WebSocket client connection to server."""
    global _sio, _telemetry_thread

    logger.info(f'[start_client] SERVER_URL={SERVER_URL}, NODE_ID={NODE_ID}')

    from agent.announcer import start_announcer, _handle_msearch
    start_announcer(NODE_ID, NODE_NAME)

    # Start M-SEARCH responder so server's active scan can find this agent
    import threading
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

    _sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,  # infinite
        reconnection_delay=1,
        reconnection_delay_max=30,
    )

    _sio.on('connect', _on_connect)
    _sio.on('disconnect', _on_disconnect)
    _sio.on('server:config_push', _on_config_push)
    _sio.on('server:set_control_mode', _on_set_control_mode)
    _sio.on('server:command', _on_command)
    _sio.on('server:node_id_push', _on_node_id_push)
    _sio.on('server:dsm:apply', _on_dsm_apply)

    try:
        _sio.connect(SERVER_URL)
    except Exception as e:
        logger.error(f'Failed to connect to server: {e}')

    _telemetry_thread = threading.Thread(target=_telemetry_loop, daemon=True)
    _telemetry_thread.start()

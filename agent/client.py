"""WebSocket client — connects agent to server. Thin wiring layer."""

import logging
import threading
import time
from typing import Optional

import socketio

from core.state import state, state_lock, invalidate_state_cache
from core.config import cfg
from agent.config import init_agent_config, init_token
from agent.telemetry import get_telemetry

logger = logging.getLogger('fancontrol')

# Initialize agent identity from env/config.json
SERVER_URL, NODE_ID, NODE_NAME = init_agent_config()
API_TOKEN = init_token()
TELEMETRY_INTERVAL = cfg.telemetry_interval

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


def _telemetry_loop():
    """Send telemetry to server periodically."""
    while True:
        time.sleep(TELEMETRY_INTERVAL)
        if _sio and state['server_connected']:
            try:
                telemetry = get_telemetry()
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


def start_client():
    """Start the WebSocket client connection to server."""
    global _sio, _telemetry_thread

    logger.info(f'[start_client] SERVER_URL={SERVER_URL}, NODE_ID={NODE_ID}')

    from agent.announcer import start_announcer, _handle_msearch
    start_announcer(NODE_ID, NODE_NAME)

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
        reconnection_attempts=0,
        reconnection_delay=1,
        reconnection_delay_max=30,
    )

    # Register event handlers
    from agent.handlers import make_handlers
    for event, handler in make_handlers(_sio).items():
        _sio.on(event, handler)

    try:
        _sio.connect(SERVER_URL)
    except Exception as e:
        logger.error(f'Failed to connect to server: {e}')

    _telemetry_thread = threading.Thread(target=_telemetry_loop, daemon=True)
    _telemetry_thread.start()

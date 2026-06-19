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

# Agent-specific state fields
state['control_mode'] = 'server'  # 'server' or 'manual'
state['server_connected'] = False
state['server_url'] = SERVER_URL
state['node_id'] = NODE_ID
state['node_name'] = NODE_NAME
state['agent_config_snapshot'] = None

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
    """Server pushes new config — apply if in server mode."""
    with state_lock:
        if state['control_mode'] != 'server':
            logger.info('Config push ignored — in manual mode')
            return

        # Save agent's current config for revert
        state['agent_config_snapshot'] = _get_local_config()

        # Apply server config
        _apply_config(data.get('config', {}))
        invalidate_state_cache()
        logger.info('Applied server config')


def _on_set_control_mode(data):
    """Server requests mode change."""
    mode = data.get('mode', 'server')
    with state_lock:
        state['control_mode'] = mode
        invalidate_state_cache()
    logger.info(f'Control mode set to: {mode}')


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


def _telemetry_loop():
    """Send telemetry to server periodically."""
    while True:
        time.sleep(TELEMETRY_INTERVAL)
        if _sio and state['server_connected']:
            try:
                _sio.emit('agent:telemetry', {
                    'node_id': NODE_ID,
                    'telemetry': _get_telemetry(),
                })
            except Exception as e:
                logger.error(f'Telemetry send failed: {e}')


def _get_local_config():
    """Get current local fan config."""
    with state_lock:
        return {
            'fans': {k: {kk: vv for kk, vv in v.items()
                         if kk not in ('rpm', 'pwm_value')}
                     for k, v in state['fans'].items()},
            'temp_sensors': state['temp_sensors'],
            'hdd_sensors': state['hdd_sensors'],
        }


def _get_telemetry():
    """Get current telemetry data."""
    with state_lock:
        return {
            'fans': {k: {'rpm': v.get('rpm', 0),
                         'pwm_value': v.get('pwm_value', 0)}
                     for k, v in state['fans'].items()},
            'temp_sensors': {k: {'value': v.get('value', 0)}
                            for k, v in state['temp_sensors'].items()},
            'hdd_sensors': {k: {'temp': v.get('temp', 0)}
                           for k, v in state['hdd_sensors'].items()},
            'failsafe': state.get('failsafe', False),
            'standby_mode': state.get('standby_mode', False),
        }


def _apply_config(config):
    """Apply config received from server."""
    with state_lock:
        for fan_id, fan_cfg in config.get('fans', {}).items():
            if fan_id in state['fans']:
                for key in ('mode', 'target_temp', 'manual_pct', 'sensors',
                            'sensor_mode', 'schedule', 'inverted'):
                    if key in fan_cfg:
                        state['fans'][fan_id][key] = fan_cfg[key]


def start_client():
    """Start the WebSocket client connection to server."""
    global _sio, _telemetry_thread

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

    try:
        _sio.connect(SERVER_URL)
    except Exception as e:
        logger.error(f'Failed to connect to server: {e}')

    _telemetry_thread = threading.Thread(target=_telemetry_loop, daemon=True)
    _telemetry_thread.start()

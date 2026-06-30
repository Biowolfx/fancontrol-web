"""Agent-specific routes — mode switch, status, config revert."""

import os
from flask import Blueprint, jsonify, request

from core.state import state, state_lock, get_state, invalidate_state_cache
from core.config import save_config

agent_routes = Blueprint('agent_routes', __name__)


@agent_routes.route('/api/agent/status')
def agent_status():
    """Get agent status including server connection."""
    return jsonify({
        'control_mode': state.get('control_mode', 'server'),
        'server_connected': state.get('server_connected', False),
        'server_url': state.get('server_url', ''),
        'node_id': state.get('node_id', ''),
        'has_agent_snapshot': state.get('agent_config_snapshot') is not None,
    })


@agent_routes.route('/api/agent/mode', methods=['POST'])
def set_control_mode():
    """Switch between server and manual control."""
    data = request.get_json()
    mode = data.get('mode', 'server')

    if mode not in ('server', 'manual'):
        return jsonify({'error': 'Invalid mode'}), 400

    with state_lock:
        old_mode = state.get('control_mode', 'server')
        state['control_mode'] = mode
        invalidate_state_cache()

    from agent.client import _sio
    if _sio and state.get('server_connected'):
        _sio.emit('agent:control_mode_changed', {
            'node_id': state.get('node_id'),
            'mode': mode,
        })

    save_config()
    return jsonify({'mode': mode, 'previous': old_mode})


@agent_routes.route('/api/agent/revert', methods=['POST'])
def revert_to_agent_config():
    """Revert to agent's local config (from snapshot)."""
    with state_lock:
        snapshot = state.get('agent_config_snapshot')
        if not snapshot:
            return jsonify({'error': 'No snapshot available'}), 400

        for fan_id, fan_cfg in snapshot.get('fans', {}).items():
            if fan_id in state['fans']:
                for key, val in fan_cfg.items():
                    state['fans'][fan_id][key] = val

        state['agent_config_snapshot'] = None
        invalidate_state_cache()

    save_config()
    return jsonify({'status': 'reverted'})

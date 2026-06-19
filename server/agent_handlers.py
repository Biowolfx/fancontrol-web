"""Socket.IO event handlers for agent (node) connections."""

import logging

from core.state import state, state_lock, invalidate_state_cache
from server.node_registry import (
    get_node_by_token,
    update_node_status,
    update_node_config,
    update_node_control_mode,
    get_node,
)

logger = logging.getLogger('fancontrol')


def register_agent_handlers(socketio):
    """Register Socket.IO event handlers for agent connections."""

    @socketio.on('agent:connect')
    def handle_agent_connect(data):
        node_id = data.get('node_id')
        node_name = data.get('node_name')
        api_token = data.get('api_token')
        control_mode = data.get('control_mode', 'server')
        config = data.get('config', {})

        if not api_token:
            logger.warning('agent:connect rejected — no api_token')
            return {'status': 'error', 'message': 'Missing api_token'}

        node = get_node_by_token(api_token)
        if not node:
            logger.warning(f'agent:connect rejected — invalid token')
            return {'status': 'error', 'message': 'Invalid token'}

        node_id = node['node_id']
        update_node_status(node_id, 'online', config)
        update_node_control_mode(node_id, control_mode)

        with state_lock:
            state['nodes'][node_id] = {
                'node_id': node_id,
                'name': node['name'],
                'status': 'online',
                'control_mode': control_mode,
                'config': config,
            }
        invalidate_state_cache()

        socketio.emit('update', {'nodes': dict(state['nodes'])})
        logger.info(f'Agent connected: {node_id} ({node["name"]})')
        return {'status': 'ok', 'node_id': node_id, 'name': node['name']}

    @socketio.on('agent:telemetry')
    def handle_agent_telemetry(data):
        node_id = data.get('node_id')
        telemetry = data.get('telemetry', {})

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:telemetry from unknown node: {node_id}')
            return

        update_node_status(node_id, 'online', telemetry)

        with state_lock:
            if node_id in state['nodes']:
                state['nodes'][node_id]['status'] = 'online'
                state['nodes'][node_id]['telemetry'] = telemetry
        invalidate_state_cache()

        socketio.emit('node_telemetry', {'node_id': node_id, 'telemetry': telemetry})

    @socketio.on('agent:config_changed')
    def handle_agent_config_changed(data):
        node_id = data.get('node_id')
        config = data.get('config', {})

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:config_changed from unknown node: {node_id}')
            return

        update_node_config(node_id, config)

        with state_lock:
            if node_id in state['nodes']:
                state['nodes'][node_id]['config'] = config
        invalidate_state_cache()

        socketio.emit('node_config_changed', {'node_id': node_id, 'config': config})
        logger.info(f'Agent config updated: {node_id}')

    @socketio.on('agent:control_mode_changed')
    def handle_agent_control_mode_changed(data):
        node_id = data.get('node_id')
        mode = data.get('mode', 'server')

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:control_mode_changed from unknown node: {node_id}')
            return

        update_node_control_mode(node_id, mode)

        with state_lock:
            if node_id in state['nodes']:
                state['nodes'][node_id]['control_mode'] = mode
        invalidate_state_cache()

        socketio.emit('node_mode_changed', {'node_id': node_id, 'mode': mode})
        logger.info(f'Agent mode changed: {node_id} -> {mode}')

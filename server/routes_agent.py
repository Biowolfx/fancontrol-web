"""Agent HTTP protocol endpoints — telemetry, poll, ack, update, commands."""

import logging
from flask import Blueprint, request, jsonify
from core.state import state, state_lock, CONFIG_VERSION, invalidate_state_cache
from core.config import save_config

logger = logging.getLogger('fancontrol')

agent_bp = Blueprint('agent', __name__)

@agent_bp.route('/api/agent/telemetry', methods=['POST'])
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
            # Check if auto-registration is enabled
            if not state.get('auto_register_agents', True):
                # Notify browser to show discovery toast
                from app import socketio as _sio
                agent_ip = request.remote_addr or ''
                _sio.emit('node:discovered', {
                    'node_id': agent_node_id,
                    'ip': agent_ip if agent_ip != '127.0.0.1' else '',
                    'name': agent_node_id,
                    'auto_registered': False,
                }) if _sio else None
                return jsonify({'error': 'Agent not registered. Use discovery to add.'}), 403
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

        # Update IP from HTTP request source
        agent_ip = request.remote_addr or ''
        if agent_ip and agent_ip != '127.0.0.1':
            from server.node_registry import update_node
            update_node(node_id, ip=agent_ip)
            with state_lock:
                if node_id in state.get('nodes', {}):
                    state['nodes'][node_id]['ip'] = agent_ip

        # Update agent version if provided
        agent_version = data.get('version', '')
        if agent_version:
            from server.node_registry import update_node_version
            update_node_version(node_id, agent_version)
            # Also update in-memory state so /api/health shows correct version
            with state_lock:
                if node_id in state.get('nodes', {}):
                    old_ver = state['nodes'][node_id].get('agent_version', '')
                    state['nodes'][node_id]['agent_version'] = agent_version
                    if agent_version != old_ver:
                        # Clear update timer — agent is now on latest version
                        state['nodes'][node_id]['update_started'] = None
                        # Notify browsers immediately so sidebar updates
                        try:
                            from app import socketio as _sio
                            _sio.emit('update', {
                                'nodes': dict(state.get('nodes', {})),
                                'config_version': CONFIG_VERSION,
                            }) if _sio else None
                        except Exception:
                            pass
                        logger.info(f'[HTTP] Agent {node_id} version: {old_ver} → {agent_version}')

        # Drain command queue
        commands = drain_commands(node_id)
        return jsonify({'status': 'ok', 'commands': commands})
    except Exception as e:
        logger.error(f'api_agent_telemetry error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/agent/poll', methods=['GET'])
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

        # Update IP from request source
        agent_ip = request.remote_addr or ''
        if agent_ip and agent_ip != '127.0.0.1':
            from server.node_registry import update_node
            update_node(node_id, ip=agent_ip)
            with state_lock:
                if node_id in state.get('nodes', {}):
                    state['nodes'][node_id]['ip'] = agent_ip

        commands = drain_commands(node_id)
        return jsonify({'commands': commands})
    except Exception as e:
        logger.error(f'api_agent_poll error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@agent_bp.route('/api/agent/update_result', methods=['POST'])
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


@agent_bp.route('/api/agent/command', methods=['POST'])
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


@agent_bp.route('/api/agent/logs', methods=['POST'])
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


@agent_bp.route('/api/agent/ack', methods=['POST'])
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


@agent_bp.route('/api/agent/control_mode', methods=['POST'])
def api_agent_control_mode():
    """Agent reports control mode change via HTTP."""
    try:
        data = request.get_json(silent=True) or {}
        api_token = data.get('api_token', '')
        mode = data.get('mode', '')

        if not api_token or not mode:
            return jsonify({'error': 'Missing api_token or mode'}), 400

        from server.node_registry import get_node_by_token, update_node_control_mode
        node = get_node_by_token(api_token)
        if not node:
            return jsonify({'error': 'Unknown agent'}), 401

        node_id = node['node_id']
        update_node_control_mode(node_id, mode)
        with state_lock:
            if node_id in state.get('nodes', {}):
                state['nodes'][node_id]['control_mode'] = mode
        logger.info(f'[HTTP] Agent {node_id} mode changed to {mode}')
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'api_agent_control_mode error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500
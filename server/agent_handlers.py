"""Socket.IO event handlers for agent (node) connections."""

import json
import logging
import threading
import time

from core.state import state, state_lock, invalidate_state_cache
from server.node_registry import (
    get_node_by_token,
    update_node_status,
    update_node_config,
    update_node_control_mode,
    get_node,
    save_agent_snapshot,
)

logger = logging.getLogger('fancontrol')

# Map agent SID → registry node_id, and reverse
_sid_to_node: dict = {}
_node_to_sid: dict = {}


def _emit_to_node(socketio, event, data, node_id):
    """Emit event to a specific agent by node_id via its SID."""
    sid = _node_to_sid.get(node_id)
    if sid:
        socketio.emit(event, data, room=sid)
    else:
        logger.debug(f'No SID for node {node_id}, emit {event} skipped')


def _start_ping_loop(socketio):
    """Ping all online agents every 30 seconds."""
    def _ping_loop():
        while True:
            time.sleep(30)
            try:
                from server.node_registry import list_nodes
                nodes = list_nodes()
                for node in nodes:
                    nid = node['node_id']
                    sid = _node_to_sid.get(nid)
                    if sid and node['status'] == 'online':
                        socketio.emit('server:ping', {'node_id': nid}, room=sid)
            except Exception as e:
                logger.error(f'Ping loop error: {e}')

    thread = threading.Thread(target=_ping_loop, daemon=True)
    thread.start()


def register_agent_handlers(socketio, on_connect=None, on_disconnect=None):
    """Register Socket.IO event handlers for agent connections."""

    _start_ping_loop(socketio)

    @socketio.on('agent:connect')
    def handle_agent_connect(data):
        from flask import request as flask_request
        agent_node_id = data.get('node_id')
        node_name = data.get('node_name')
        api_token = data.get('api_token')
        control_mode = data.get('control_mode', 'server')
        agent_config = data.get('config', {})
        agent_ip = flask_request.remote_addr if flask_request else ''
        agent_sid = flask_request.sid if flask_request else None

        if not api_token:
            logger.warning('agent:connect rejected — no api_token')
            return {'status': 'error', 'message': 'Missing api_token'}

        node = get_node_by_token(api_token)

        # Auto-register unknown agent — no manual setup needed
        if not node:
            from server.node_registry import add_node
            node = add_node(node_name or node_id or 'Agent', api_token=api_token,
                            ip=agent_ip if agent_ip != '127.0.0.1' else '')
            logger.info(f'Auto-registered new agent: {node_name} ({agent_ip}) token={api_token[:8]}...')
            # Notify browser — agent is already connected via WebSocket
            socketio.emit('node:discovered', {
                'node_id': node['node_id'],
                'name': node['name'],
                'ip': agent_ip,
                'auto_registered': True,
                'already_connected': True,
            })

        node_id = node['node_id']
        # Update IP from WebSocket connection
        if agent_ip and agent_ip != '127.0.0.1':
            from server.node_registry import update_node
            update_node(node_id, ip=agent_ip)

        # Track SID mapping for reliable delivery
        if agent_sid:
            _sid_to_node[agent_sid] = node_id
            _node_to_sid[node_id] = agent_sid
            logger.info(f'[connect] Agent SID mapped: {agent_sid} → {node_id} '
                        f'(agent_sent_node_id={data.get("node_id")})')
        else:
            logger.warning(f'[connect] No SID available for agent {node_id}')

        update_node_status(node_id, 'online', agent_config)
        update_node_control_mode(node_id, control_mode)

        if on_connect:
            on_connect(node_id)

        # Push node_id to agent so it uses the registry ID for telemetry
        _emit_to_node(socketio, 'server:node_id_push', {
            'node_id': node_id,
            'token': node['api_token'],
        }, node_id)

        with state_lock:
            state['nodes'][node_id] = {
                'node_id': node_id,
                'name': node['name'],
                'status': 'online',
                'control_mode': control_mode,
                'config': agent_config,
                'dsm_schemes': agent_config.get('dsm_schemes', []),
                'kernel_info': agent_config.get('kernel_info', {}),
            }
        invalidate_state_cache()

        # Push server config to agent if in server mode
        server_config = node.get('config', {})
        if server_config and control_mode == 'server':
            _emit_to_node(socketio, 'server:config_push', {
                'config': server_config,
            }, node_id)
            logger.info(f'Pushed config to {node["name"]}')

            # Check for conflict on reconnect
            if agent_config and server_config != agent_config:
                save_agent_snapshot(node_id, agent_config)
                socketio.emit('node:conflict', {
                    'node_id': node_id,
                    'name': node['name'],
                    'server_config': server_config,
                    'agent_config': agent_config,
                })
                logger.info(f'Config conflict on reconnect for {node["name"]}')

        socketio.emit('update', {'nodes': dict(state['nodes'])})
        logger.info(f'Agent connected: {node_id} ({node["name"]})')
        return {'status': 'ok', 'node_id': node_id, 'name': node['name']}

    @socketio.on('agent:telemetry')
    def handle_agent_telemetry(data):
        agent_node_id = data.get('node_id')
        telemetry = data.get('telemetry', {})

        # Resolve agent's node_id to registry node_id via SID mapping
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None

        logger.info(f'[telemetry-recv] agent_sent={agent_node_id} sid={agent_sid} '
                    f'resolved={node_id} fans={list(telemetry.get("fans", {}).keys())} '
                    f'temps={list(telemetry.get("temp_sensors", {}).keys())}')

        if not node_id:
            # Fallback: try direct lookup
            node_id = agent_node_id

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:telemetry DROPPED: resolved={node_id} '
                           f'nodes_keys={list(state.get("nodes", {}).keys())} '
                           f'sid_map_keys={list(_sid_to_node.keys())}')
            return

        update_node_status(node_id, 'online', telemetry)

        with state_lock:
            if node_id in state['nodes']:
                state['nodes'][node_id]['status'] = 'online'
                state['nodes'][node_id]['telemetry'] = telemetry
        invalidate_state_cache()

        socketio.emit('node:telemetry', {'node_id': node_id, 'telemetry': telemetry})

    @socketio.on('agent:config_changed')
    def handle_agent_config_changed(data):
        agent_node_id = data.get('node_id')
        agent_config = data.get('config', {})

        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            node_id = agent_node_id

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:config_changed from unknown node: {agent_node_id}')
            return

        # Get server's authoritative config for this node
        node = get_node(node_id)
        server_config = node.get('config', {}) if node else {}

        # Check for conflict: agent config differs from server config
        if server_config and agent_config and server_config != agent_config:
            # Save agent's config as snapshot for revert
            save_agent_snapshot(node_id, agent_config)

            # Update node config with agent's changes
            update_node_config(node_id, agent_config)

            with state_lock:
                if node_id in state['nodes']:
                    state['nodes'][node_id]['config'] = agent_config
            invalidate_state_cache()

            # Notify browsers of conflict
            socketio.emit('node:conflict', {
                'node_id': node_id,
                'name': state['nodes'].get(node_id, {}).get('name', node_id),
                'server_config': server_config,
                'agent_config': agent_config,
            })
            logger.info(f'Config conflict detected for {node_id}')
        else:
            # No conflict — just update
            update_node_config(node_id, agent_config)

            with state_lock:
                if node_id in state['nodes']:
                    state['nodes'][node_id]['config'] = agent_config
            invalidate_state_cache()

        socketio.emit('node_config_changed', {'node_id': node_id, 'config': agent_config})
        logger.info(f'Agent config updated: {node_id}')

    @socketio.on('agent:control_mode_changed')
    def handle_agent_control_mode_changed(data):
        agent_node_id = data.get('node_id')
        mode = data.get('mode', 'server')

        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            node_id = agent_node_id

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:control_mode_changed from unknown node: {agent_node_id}')
            return

        update_node_control_mode(node_id, mode)

        with state_lock:
            if node_id in state['nodes']:
                state['nodes'][node_id]['control_mode'] = mode
        invalidate_state_cache()

        socketio.emit('node_mode_changed', {'node_id': node_id, 'mode': mode})
        logger.info(f'Agent mode changed: {node_id} -> {mode}')

    @socketio.on('agent:pong')
    def handle_agent_pong(data):
        """Agent responds to ping — update last_seen."""
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            node_id = data.get('node_id', '')
        update_node_status(node_id, 'online')

    @socketio.on('disconnect')
    def handle_disconnect():
        """Clean up SID mapping on disconnect."""
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        if agent_sid and agent_sid in _sid_to_node:
            nid = _sid_to_node.pop(agent_sid)
            _node_to_sid.pop(nid, None)
            logger.info(f'Agent disconnected: {nid} (SID {agent_sid} released)')

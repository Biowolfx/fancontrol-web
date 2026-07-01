"""Socket.IO event handlers for FanControl Web."""

import logging
import threading
import time
from datetime import datetime

from core.state import get_state, _init_complete

logger = logging.getLogger('fancontrol')


def _start_heartbeat_checker(socketio):
    """Background thread that checks agent heartbeats and probes offline agents."""
    # Track which nodes connected via WebSocket (have real telemetry)
    _ws_connected = set()

    def _check_loop():
        nonlocal _ws_connected
        while True:
            time.sleep(10)
            try:
                from server.node_registry import list_nodes, update_node_status, update_node
                from core.state import state, state_lock, invalidate_state_cache

                nodes = list_nodes()
                now = datetime.utcnow()

                for node in nodes:
                    nid = node['node_id']

                    if node['status'] == 'online' and node.get('last_seen'):
                        try:
                            last_seen = datetime.fromisoformat(node['last_seen'])
                            age = (now - last_seen).total_seconds()
                            # Only mark offline if node was connected via WS and hasn't sent telemetry
                            if age > 15 and nid in _ws_connected:
                                update_node_status(nid, 'offline')
                                _ws_connected.discard(nid)
                                with state_lock:
                                    if 'nodes' in state and nid in state['nodes']:
                                        state['nodes'][nid]['status'] = 'offline'
                                invalidate_state_cache()

                                socketio.emit('node:update', {
                                    'node_id': nid,
                                    'status': 'offline',
                                    'name': node['name'],
                                })

                                logger.info(f'Agent {node["name"]} marked offline (no telemetry for 15s)')
                        except (ValueError, TypeError):
                            pass

                    # Probe offline agents via HTTP every 30s
                    elif node['status'] == 'offline' and node.get('ip'):
                        should_probe = True
                        if node.get('last_seen'):
                            try:
                                last = datetime.fromisoformat(node['last_seen'])
                                if (now - last).total_seconds() < 30:
                                    should_probe = False
                            except (ValueError, TypeError):
                                pass

                        if should_probe:
                            from server.discovery import probe_agent
                            info = probe_agent(node['ip'], timeout=2)
                            if info:
                                update_node_status(nid, 'online')
                                update_node(nid, ip=node['ip'])
                                with state_lock:
                                    if 'nodes' not in state:
                                        state['nodes'] = {}
                                    state['nodes'][nid] = {
                                        'node_id': nid,
                                        'name': node['name'],
                                        'status': 'online',
                                    }
                                invalidate_state_cache()

                                socketio.emit('node:update', {
                                    'node_id': nid,
                                    'status': 'online',
                                    'name': node['name'],
                                    'ip': node['ip'],
                                })

                                logger.info(f'Agent {node["name"]} ({node["ip"]}) came online via HTTP probe')
            except Exception as e:
                logger.error(f'Heartbeat check error: {e}')

    def on_ws_connect(node_id):
        """Called when agent connects via WebSocket."""
        _ws_connected.add(node_id)

    def on_ws_disconnect(node_id):
        """Called when agent disconnects from WebSocket."""
        _ws_connected.discard(node_id)

    thread = threading.Thread(target=_check_loop, daemon=True)
    thread.start()

    return on_ws_connect, on_ws_disconnect


def register_handlers(socketio):
    """Register Socket.IO event handlers."""

    # Start SSDP discovery listener
    from server.discovery import start_discovery_listener, on_agent_discovered

    def on_new_agent(agent_info):
        socketio.emit('node:discovered', agent_info)

    on_agent_discovered(on_new_agent)
    start_discovery_listener()

    # Start SSDP server announcer (so agents can discover this server)
    from core.state import state as _state
    if _state.get('ssdp_enabled', True):
        from server.announcer import start_announcer as _start_server_announcer
        _start_server_announcer(
            _state.get('server_name', 'FanControl Server'),
            _state.get('port', 5059),
        )

    @socketio.on('connect')
    def handle_socket_connect():
        """Send initial state on client connection.
        Wait for init_hardware() to complete so the client always
        receives the correct 'initialized' flag (avoids wizard flash)."""
        _init_complete.wait(timeout=15)
        socketio.emit('update', get_state())

    @socketio.on('get_state')
    def handle_get_state():
        """Handle state request from client"""
        socketio.emit('update', get_state())

    _on_ws_connect, _on_ws_disconnect = _start_heartbeat_checker(socketio)

    from server.agent_handlers import register_agent_handlers
    register_agent_handlers(socketio, on_connect=_on_ws_connect, on_disconnect=_on_ws_disconnect)

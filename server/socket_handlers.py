"""Socket.IO event handlers for FanControl Web."""

import logging
import threading
import time
from datetime import datetime

from core.state import get_state, _init_complete

logger = logging.getLogger('fancontrol')


def _start_heartbeat_checker(socketio):
    """Background thread that checks agent heartbeats."""
    def _check_loop():
        while True:
            time.sleep(10)
            try:
                from server.node_registry import list_nodes, update_node_status
                from core.state import state, state_lock, invalidate_state_cache

                nodes = list_nodes()
                now = datetime.utcnow()

                for node in nodes:
                    if node['status'] == 'online' and node.get('last_seen'):
                        try:
                            last_seen = datetime.fromisoformat(node['last_seen'])
                            if (now - last_seen).total_seconds() > 15:
                                update_node_status(node['node_id'], 'offline')
                                with state_lock:
                                    if 'nodes' in state and node['node_id'] in state['nodes']:
                                        state['nodes'][node['node_id']]['status'] = 'offline'
                                invalidate_state_cache()

                                socketio.emit('node:update', {
                                    'node_id': node['node_id'],
                                    'status': 'offline',
                                    'name': node['name'],
                                })

                                logger.info(f'Agent {node["name"]} marked offline (no telemetry for 15s)')
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.error(f'Heartbeat check error: {e}')

    thread = threading.Thread(target=_check_loop, daemon=True)
    thread.start()


def register_handlers(socketio):
    """Register Socket.IO event handlers."""

    # Start SSDP discovery listener
    from server.discovery import start_discovery_listener, on_agent_discovered

    def on_new_agent(agent_info):
        socketio.emit('node:discovered', agent_info)

    on_agent_discovered(on_new_agent)
    start_discovery_listener()

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

    _start_heartbeat_checker(socketio)

    from server.agent_handlers import register_agent_handlers
    register_agent_handlers(socketio)

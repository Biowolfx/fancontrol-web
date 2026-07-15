"""Server Socket.IO event handlers — browser communication + SSDP discovery."""

import logging
import threading
import time
from datetime import datetime

from core.state import state, state_lock, get_state, invalidate_state_cache, _init_complete

logger = logging.getLogger('fancontrol')

# Periodic heartbeat: check agent liveness and probe offline agents
# Runs every 30s (was 10s). HTTP-only agents — no WebSocket tracking needed.
HEARTBEAT_INTERVAL = 30
PROBE_INTERVAL = 120  # seconds between probes to offline agents


def _start_heartbeat_checker(socketio):
    """Background thread that checks agent liveness via last_seen timestamps."""
    _last_probe = {}  # nid → timestamp of last probe attempt

    def _check_loop():
        while True:
            time.sleep(HEARTBEAT_INTERVAL)
            try:
                from core.state import state, state_lock, invalidate_state_cache

                with state_lock:
                    nodes_snapshot = {k: dict(v) for k, v in state.get('nodes', {}).items()}

                now = datetime.utcnow()

                for nid, node in nodes_snapshot.items():
                    status = node.get('status', 'offline')
                    ip = node.get('ip', '')
                    last_seen_str = node.get('last_seen', '')

                    if not last_seen_str:
                        continue

                    try:
                        last_seen = datetime.fromisoformat(last_seen_str)
                        age = (now - last_seen).total_seconds()
                    except (ValueError, TypeError):
                        continue

                    if status == 'online' and age > 60:
                        # Agent hasn't sent telemetry in 60s — mark offline
                        _mark_offline(socketio, nid, node, 'No telemetry for 60s')

                    elif status == 'offline' and ip:
                        # Probe offline agents periodically
                        last_probe = _last_probe.get(nid, 0)
                        if (time.time() - last_probe) < PROBE_INTERVAL:
                            continue
                        _last_probe[nid] = time.time()

                        from server.discovery import probe_agent
                        info = probe_agent(ip, timeout=2)
                        if info:
                            from server.node_registry import update_node_status, update_node
                            update_node_status(nid, 'online')
                            update_node(nid, ip=ip)
                            with state_lock:
                                if nid not in state.get('nodes', {}):
                                    state.setdefault('nodes', {})[nid] = {
                                        'node_id': nid, 'name': node['name'],
                                        'ip': ip, 'port': node.get('port', 5059),
                                        'status': 'online',
                                    }
                                else:
                                    state['nodes'][nid]['status'] = 'online'
                            invalidate_state_cache()
                            socketio.emit('node:update', {
                                'node_id': nid, 'status': 'online',
                                'name': node['name'], 'ip': ip,
                            })
                            logger.info(f'Agent {node["name"]} ({ip}) came online via probe')
            except Exception as e:
                logger.error(f'Heartbeat check error: {e}', exc_info=True)

    thread = threading.Thread(target=_check_loop, daemon=True)
    thread.start()


def _mark_offline(socketio, nid, node, reason):
    """Mark a node as offline and notify browsers + Telegram."""
    try:
        from server.node_registry import update_node_status
        update_node_status(nid, 'offline')
        with state_lock:
            if nid in state.get('nodes', {}):
                state['nodes'][nid]['status'] = 'offline'
        invalidate_state_cache()
        socketio.emit('node:update', {
            'node_id': nid, 'status': 'offline', 'name': node['name'],
        })
        tg_enabled = state.get('telegram_enabled', False)
        tg_events = state.get('telegram_events', {})
        if tg_enabled and tg_events.get('agent_status', True):
            from core.telegram import send_message
            send_message(f'🔴 <b>Агент отключён</b>\n{node["name"]} ({nid})')
        logger.info(f'Agent {node["name"]} marked offline: {reason}')
    except Exception as e:
        logger.error(f'Error marking {nid} offline: {e}')


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
        """Send initial state on client connection."""
        _init_complete.wait(timeout=15)
        socketio.emit('update', get_state())

    @socketio.on('get_state')
    def handle_get_state():
        """Handle state request from client"""
        socketio.emit('update', get_state())

    _start_heartbeat_checker(socketio)

    from server.agent_handlers import register_agent_handlers
    register_agent_handlers(socketio)


def _restart_ssdp_announcer():
    """Stop old SSDP threads and start new ones with current server_name."""
    from server.announcer import stop_announcers, start_announcer
    from core.state import state, state_lock
    stop_announcers()
    if state.get('ssdp_enabled', True):
        with state_lock:
            name = state.get('server_name', 'FanControl Server')
            port = state.get('port', 5059)
        start_announcer(name, port)

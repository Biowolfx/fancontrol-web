"""Agent communication — Socket.IO handlers + HTTP command queue."""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime

from core.state import state, state_lock, invalidate_state_cache
from server.node_registry import (
    get_node_by_token,
    update_node_status,
    update_node_config,
    update_node_control_mode,
    update_node_version,
    get_node,
    save_agent_snapshot,
)

logger = logging.getLogger('fancontrol')

# Map agent SID → registry node_id, and reverse (for browser Socket.IO only)
_sid_to_node: dict = {}
_node_to_sid: dict = {}
_socketio_ref = None  # Set by register_agent_handlers

# HTTP command queue with delivery confirmation
_cmd_queue = defaultdict(list)
_cmd_lock = threading.Lock()
_cmd_counter = 0
_cmd_delivery = {}  # cmd_id → {node_id, type, status, time}


def queue_command(node_id, command_type, payload=None):
    """Queue a command for delivery to agent via HTTP poll."""
    global _cmd_counter
    with _cmd_lock:
        _cmd_counter += 1
        cmd_id = f'cmd-{_cmd_counter}'
        cmd = {
            'id': cmd_id,
            'type': command_type,
            'data': payload or {},
        }
        _cmd_queue[node_id].append(cmd)
        _cmd_delivery[cmd_id] = {
            'node_id': node_id,
            'type': command_type,
            'status': 'pending',
            'time': time.time(),
        }
    logger.info(f'[cmd-queue] Queued {command_type} ({cmd_id}) for {node_id}')


def drain_commands(node_id):
    """Return all pending commands for a node (mark as sent)."""
    with _cmd_lock:
        cmds = _cmd_queue.pop(node_id, [])
        for cmd in cmds:
            cmd_id = cmd.get('id')
            if cmd_id and cmd_id in _cmd_delivery:
                _cmd_delivery[cmd_id]['status'] = 'sent'
        return cmds


def ack_command(cmd_id, status='delivered'):
    """Mark a command as delivered/failed by the agent."""
    with _cmd_lock:
        if cmd_id in _cmd_delivery:
            _cmd_delivery[cmd_id]['status'] = status
            _cmd_delivery[cmd_id]['delivered_at'] = time.time()
            logger.info(f'[cmd-queue] Command {cmd_id} acknowledged: {status}')
        else:
            logger.warning(f'[cmd-queue] Unknown command {cmd_id} acknowledged')


def get_pending_commands(node_id):
    """Get pending (not yet delivered) commands for a node."""
    with _cmd_lock:
        return [c for c in _cmd_queue.get(node_id, [])]


def cleanup_stale_sids():
    """Remove SID mappings where the node no longer exists in state."""
    from core.state import state, state_lock
    with state_lock:
        nodes = set(state.get('nodes', {}).keys())
    stale_sids = [sid for sid, nid in _sid_to_node.items() if nid not in nodes]
    for sid in stale_sids:
        nid = _sid_to_node.pop(sid, None)
        if nid:
            _node_to_sid.pop(nid, None)
    if stale_sids:
        logger.info(f'[SID] Cleaned up {len(stale_sids)} stale mappings')


def _process_agent_data(node_id, telemetry):
    """Process telemetry data for a node — update state, DB, broadcast to browsers.
    Shared by both Socket.IO handler and HTTP endpoint."""
    if not isinstance(telemetry, dict):
        return

    update_node_status(node_id, 'online', telemetry)

    with state_lock:
        if node_id in state.get('nodes', {}):
            # Check for fan health status changes before updating telemetry
            prev_telemetry = state['nodes'][node_id].get('telemetry', {})
            prev_fans = prev_telemetry.get('fans', {})
            new_fans = telemetry.get('fans', {})

            for fan_id, new_fan in new_fans.items():
                new_health = new_fan.get('health', {})
                prev_health = prev_fans.get(fan_id, {}).get('health', {})
                new_h_status = new_health.get('status', 'healthy')
                prev_h_status = prev_health.get('status', 'healthy')

                if new_h_status != prev_h_status:
                    label = new_fan.get('label', fan_id)
                    if new_h_status in ('stopped', 'slowing', 'needs_calibration'):
                        _socketio_ref.emit('fan:health', {
                            'fan_id': fan_id, 'node_id': node_id,
                            'status': new_h_status, 'label': label,
                            'message': f'[{node_id}] Вентилятор {label}: {new_h_status}',
                        }) if _socketio_ref else None
                    elif new_h_status == 'healthy' and prev_h_status in ('stopped', 'slowing', 'needs_calibration'):
                        _socketio_ref.emit('fan:health:cleared', {
                            'fan_id': fan_id, 'node_id': node_id,
                        }) if _socketio_ref else None

            state['nodes'][node_id]['status'] = 'online'
            state['nodes'][node_id]['telemetry'] = telemetry
            state['nodes'][node_id]['last_seen'] = datetime.utcnow().isoformat()
        else:
            # Node exists in DB but not in state — populate from DB
            from server.node_registry import get_node
            db_node = get_node(node_id)
            if db_node:
                state['nodes'][node_id] = {
                    'node_id': node_id,
                    'stable_id': db_node.get('stable_id', ''),
                    'name': db_node.get('name', node_id),
                    'ip': db_node.get('ip', ''),
                    'port': db_node.get('port', 5059),
                    'status': 'online',
                    'control_mode': db_node.get('control_mode', 'server'),
                    'config': db_node.get('config') or {},
                    'dsm_schemes': (db_node.get('config') or {}).get('dsm_schemes', []),
                    'kernel_info': (db_node.get('config') or {}).get('kernel_info', {}),
                    'agent_version': db_node.get('agent_version', ''),
                    'auto_update': db_node.get('auto_update', 0),
                    'pending_update': db_node.get('pending_update', 0),
                    'update_started': None,
                    'telemetry': telemetry,
                    'last_seen': datetime.utcnow().isoformat(),
                }
                logger.info(f'[telemetry] Populated state for {node_id} from DB')

    invalidate_state_cache()

    if _socketio_ref:
        _socketio_ref.emit('node:telemetry', {'node_id': node_id, 'telemetry': telemetry})

# Grace period: skip conflict detection for 30s after server startup
_startup_time = time.monotonic()
_GRACE_PERIOD = 30

# Conflict comparison: only meaningful keys, strip runtime fields
_CMP_KEYS = {'fans', 'temp_sensors', 'hdd_sensors', 'kernel_info', 'dsm_schemes', 'control_mode'}
_RUNTIME_FAN_KEYS = {'rpm', 'pwm_value', 'raw_pwm', 'last_update', 'current_pct', 'target_pwm', 'health'}
_RUNTIME_SENSOR_KEYS = {'value', 'temp', 'standby', 'last_update', 'pct_fill', 'color_zone', 'health_status'}


def _strip_runtime(cfg):
    """Remove metadata and runtime-only fields for config comparison."""
    result = {}
    for k, v in (cfg or {}).items():
        if k not in _CMP_KEYS:
            continue
        if k == 'fans' and isinstance(v, dict):
            result[k] = {
                fid: {fk: fv for fk, fv in fval.items() if fk not in _RUNTIME_FAN_KEYS}
                for fid, fval in v.items()
            }
        elif k in ('temp_sensors', 'hdd_sensors') and isinstance(v, dict):
            result[k] = {
                sid: {sk: sv for sk, sv in sval.items() if sk not in _RUNTIME_SENSOR_KEYS}
                for sid, sval in v.items()
            }
        else:
            result[k] = v
    return result


def _emit_to_node(socketio, event, data, node_id):
    """Emit event to agent via Socket.IO AND queue for HTTP delivery."""
    # Try Socket.IO push (for old agents or immediate delivery)
    sid = _node_to_sid.get(node_id)
    if sid:
        logger.info(f'[_emit] {event} → node={node_id} sid={sid[:8]}...')
        socketio.emit(event, data, room=sid)

    # Also queue for HTTP poll (for new agents or as fallback)
    cmd_type = event.replace('server:', '')
    queue_command(node_id, cmd_type, data)


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
    global _socketio_ref
    _socketio_ref = socketio

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
            try:
                node = add_node(node_name or agent_node_id or 'Agent', api_token=api_token,
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
            except Exception as e:
                logger.error(f'agent:connect add_node failed: {e}')
                # Retry lookup — node may have been created by concurrent request
                node = get_node_by_token(api_token)
                if not node:
                    return {'status': 'error', 'message': f'Registration failed: {e}'}

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
        # Save agent config (incl. dsm_schemes) to config column
        # so telemetry updates don't overwrite it
        update_node_config(node_id, agent_config)

        agent_version = data.get('version', '') or agent_config.get('config_version', '')
        if agent_version:
            update_node_version(node_id, agent_version)

        if on_connect:
            on_connect(node_id)

        # Telegram notification for agent connect
        tg_enabled = state.get('telegram_enabled', False)
        tg_events = state.get('telegram_events', {})
        if tg_enabled and tg_events.get('agent_status', True):
            from core.telegram import send_message
            send_message(f'🟢 <b>Агент подключён</b>\n{node_name} ({agent_ip})')

        # Push node_id to agent so it uses the registry ID for telemetry
        _emit_to_node(socketio, 'server:node_id_push', {
            'node_id': node_id,
            'token': node['api_token'],
        }, node_id)

        with state_lock:
            prev = state['nodes'].get(node_id, {})
            from core.state import CONFIG_VERSION as _srv_ver
            try:
                db_pending = bool(node.get('pending_update', 0))
                db_auto = bool(node.get('auto_update', 0))
            except Exception as e:
                logger.error(f'[connect] Error reading flags: {e}')
                db_pending = False
                db_auto = False
            # If agent reconnects with matching server version, update is done
            # — clear pending_update. If version doesn't match, keep pending
            # so polling can retry.
            update_done = (agent_version and agent_version == _srv_ver)
            if update_done and db_pending:
                logger.info(f'[connect] Agent {node_id} updated successfully: '
                            f'{prev.get("agent_version", "?")} → {agent_version}')
            clear_pending = update_done or not db_pending
            if clear_pending and db_pending:
                from server.node_registry import update_node_flags
                update_node_flags(node_id, pending_update=False)
            new_node = {
                'node_id': node_id,
                'stable_id': node.get('stable_id', ''),
                'name': node['name'],
                'ip': node.get('ip', ''),
                'port': node.get('port', 5059),
                'status': 'online',
                'control_mode': control_mode,
                'config': agent_config,
                'dsm_schemes': agent_config.get('dsm_schemes', []),
                'kernel_info': agent_config.get('kernel_info', {}),
                'agent_version': agent_version,
                'auto_update': db_auto,
                'pending_update': False if clear_pending else db_pending,
                'update_started': None,
            }
            state['nodes'][node_id] = new_node
        invalidate_state_cache()

        # Push server config to agent if in server mode
        server_config = node.get('config', {})
        if server_config and control_mode == 'server':
            _emit_to_node(socketio, 'server:config_push', {
                'config': server_config,
            }, node_id)
            logger.info(f'Pushed config to {node["name"]}')

            # Check for conflict on reconnect (strip metadata + runtime fields)
            server_cmp = _strip_runtime(server_config)
            agent_cmp = _strip_runtime(agent_config)
            if server_cmp and agent_cmp and server_cmp != agent_cmp:
                diff_keys = [k for k in set(list(server_cmp) + list(agent_cmp))
                             if server_cmp.get(k) != agent_cmp.get(k)]
                logger.info(f'Config conflict on reconnect for {node["name"]}: {diff_keys}')
                for k in diff_keys:
                    logger.info(f'  field={k} server={repr(server_cmp.get(k))[:200]} '
                                f'agent={repr(agent_cmp.get(k))[:200]}')
                save_agent_snapshot(node_id, agent_config)
                socketio.emit('node:conflict', {
                    'node_id': node_id,
                    'name': node['name'],
                    'server_config': server_config,
                    'agent_config': agent_config,
                })

        socketio.emit('update', {'nodes': dict(state['nodes'])})
        logger.info(f'Agent connected: {node_id} ({node["name"]})')
        return {'status': 'ok', 'node_id': node_id, 'name': node['name']}

    @socketio.on('agent:telemetry')
    def handle_agent_telemetry(data):
        if not isinstance(data, dict):
            return
        agent_node_id = data.get('node_id')
        telemetry = data.get('telemetry', {})
        
        # Payload size limit (1MB)
        if len(str(data)) > 1_000_000:
            logger.warning(f'agent:telemetry DROPPED: payload too large ({len(str(data))} bytes)')
            return

        # Resolve agent's node_id to registry node_id via SID mapping
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None

        logger.info(f'[telemetry-recv] agent_sent={agent_node_id} sid={agent_sid} '
                    f'resolved={node_id} fans={list(telemetry.get("fans", {}).keys())} '
                    f'temps={list(telemetry.get("temp_sensors", {}).keys())}')

        if not node_id:
            # Fallback: try to resolve by agent IP from Socket.IO connection
            agent_ip = flask_request.remote_addr if flask_request else ''
            from server.node_registry import list_nodes
            for n in list_nodes():
                if agent_ip and n.get('ip') == agent_ip:
                    node_id = n['node_id']
                    if agent_sid:
                        _sid_to_node[agent_sid] = node_id
                        _node_to_sid[node_id] = agent_sid
                    # Populate state if missing (node added via scan but handle_agent_connect not called)
                    if node_id not in state.get('nodes', {}):
                        from core.state import invalidate_state_cache
                        new_node = {
                            'node_id': node_id,
                            'stable_id': n.get('stable_id', ''),
                            'name': n.get('name', node_id),
                            'status': 'online',
                            'control_mode': n.get('control_mode', 'server'),
                            'config': n.get('config') or {},
                            'dsm_schemes': (n.get('config') or {}).get('dsm_schemes', []),
                            'kernel_info': (n.get('config') or {}).get('kernel_info', {}),
                            'agent_version': n.get('agent_version', ''),
                            'auto_update': n.get('auto_update', 0),
                            'pending_update': n.get('pending_update', 0),
                            'update_started': None,
                        }
                        with state_lock:
                            state['nodes'][node_id] = new_node
                        invalidate_state_cache()
                        logger.info(f'[telemetry] Populated state for node {node_id} from DB')
                    logger.info(f'[telemetry] Resolved by IP fallback: {agent_ip} → {node_id}')
                    break

        if not node_id:
            logger.warning(f'agent:telemetry DROPPED: no SID mapping for sid={agent_sid}')
            return

        # If resolved node_id is stale (deleted from state), try IP fallback again
        if node_id not in state.get('nodes', {}):
            agent_ip = flask_request.remote_addr if flask_request else ''
            from server.node_registry import list_nodes
            found = False
            for n in list_nodes():
                if agent_ip and n.get('ip') == agent_ip:
                    node_id = n['node_id']
                    if agent_sid:
                        _sid_to_node[agent_sid] = node_id
                        _node_to_sid[node_id] = agent_sid
                    if node_id not in state.get('nodes', {}):
                        from core.state import invalidate_state_cache
                        with state_lock:
                            state['nodes'][node_id] = {
                                'node_id': node_id,
                                'stable_id': n.get('stable_id', ''),
                                'name': n.get('name', node_id),
                                'ip': n.get('ip', ''),
                                'port': n.get('port', 5059),
                                'status': 'online',
                                'control_mode': n.get('control_mode', 'server'),
                                'config': n.get('config') or {},
                                'dsm_schemes': (n.get('config') or {}).get('dsm_schemes', []),
                                'kernel_info': (n.get('config') or {}).get('kernel_info', {}),
                                'agent_version': n.get('agent_version', ''),
                                'auto_update': n.get('auto_update', 0),
                                'pending_update': n.get('pending_update', 0),
                                'update_started': None,
                            }
                        invalidate_state_cache()
                    logger.info(f'[telemetry] Stale SID resolved by IP: {agent_ip} → {node_id}')
                    found = True
                    break
            if not found:
                logger.warning(f'agent:telemetry DROPPED: stale node_id={node_id} not in state')
                return

        update_node_status(node_id, 'online', telemetry)

        with state_lock:
            if node_id in state['nodes']:
                # Check for fan health status changes before updating telemetry
                prev_telemetry = state['nodes'][node_id].get('telemetry', {})
                prev_fans = prev_telemetry.get('fans', {})
                new_fans = telemetry.get('fans', {})

                for fan_id, new_fan in new_fans.items():
                    new_health = new_fan.get('health', {})
                    prev_health = prev_fans.get(fan_id, {}).get('health', {})
                    new_h_status = new_health.get('status', 'healthy')
                    prev_h_status = prev_health.get('status', 'healthy')

                    if new_h_status != prev_h_status:
                        label = new_fan.get('label', fan_id)
                        if new_h_status in ('stopped', 'slowing', 'needs_calibration'):
                            socketio.emit('fan:health', {
                                'fan_id': fan_id, 'node_id': node_id,
                                'status': new_h_status, 'label': label,
                                'message': f'[{node_id}] Вентилятор {label}: {new_h_status}',
                            })
                        elif new_h_status == 'healthy' and prev_h_status in ('stopped', 'slowing', 'needs_calibration'):
                            socketio.emit('fan:health:cleared', {
                                'fan_id': fan_id, 'node_id': node_id,
                            })

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

        # Skip conflict detection during grace period after server startup
        if time.monotonic() - _startup_time < _GRACE_PERIOD:
            logger.debug(f'Grace period active, skipping conflict check for {node_id}')
            return

        # Check for conflict: agent config differs from server config
        server_cmp = _strip_runtime(server_config)
        agent_cmp = _strip_runtime(agent_config)

        if server_cmp and agent_cmp and server_cmp != agent_cmp:
            # Log what actually differs for debugging
            diff_keys = []
            all_keys = set(list(server_cmp.keys()) + list(agent_cmp.keys()))
            for k in all_keys:
                sv = server_cmp.get(k)
                av = agent_cmp.get(k)
                if sv != av:
                    diff_keys.append(k)
                    logger.info(f'[CONFLICT] field={k} server={repr(sv)[:200]} agent={repr(av)[:200]}')
            logger.warning(f'Config conflict for {node_id}: differing fields = {diff_keys}')
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
    @socketio.on('server:dsm:apply')
    def handle_server_dsm_apply(data):
        """Forward DSM scheme apply from UI to a remote agent."""
        node_id = data.get('node_id')
        if not node_id or node_id not in state.get('nodes', {}):
            return
        _emit_to_node(socketio, 'agent:dsm:apply', data, node_id)
        logger.info(f'DSM apply forwarded to agent {node_id}')

    @socketio.on('agent:update_result')
    def handle_agent_update_result(data):
        """Agent reports update progress or error."""
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            return
        status = data.get('status', 'unknown')
        message = data.get('message', '')
        version = data.get('version', '')
        logger.info(f'Agent update result: {node_id} status={status} version={version} msg={message}')
        socketio.emit('agent:update_progress', {
            'node_id': node_id,
            'status': status,
            'message': message,
            'version': version,
        })

    @socketio.on('agent:logs')
    def handle_agent_logs(data):
        """Agent sends log lines — forward to browser."""
        node_id = data.get('node_id', '')
        lines = data.get('lines', [])
        socketio.emit('agent:logs', {
            'node_id': node_id,
            'lines': lines,
        })

    @socketio.on('disconnect')
    def handle_disconnect():
        """Clean up SID mapping on disconnect."""
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        if agent_sid and agent_sid in _sid_to_node:
            nid = _sid_to_node.pop(agent_sid)
            # Only clean _node_to_sid if IT points back to THIS SID
            # (prevents old disconnect from corrupting a newer mapping)
            if _node_to_sid.get(nid) == agent_sid:
                _node_to_sid.pop(nid, None)
            if on_disconnect:
                on_disconnect(nid)
            logger.info(f'Agent disconnected: {nid} (SID {agent_sid[:8]}...)')

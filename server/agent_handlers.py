"""Agent communication — HTTP command queue + telemetry processing."""

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

_socketio_ref = None  # Set by register_agent_handlers (for browser broadcasts only)

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


# Runtime fields stripped from config comparison
_CMP_KEYS = {'fans', 'temp_sensors', 'hdd_sensors', 'kernel_info', 'dsm_schemes', 'control_mode'}
_RUNTIME_SENSOR_KEYS = {'rpm', 'pwm_value', 'raw_pwm', 'last_update', 'current_pct', 'target_pwm'}


def _strip_runtime(cfg):
    """Remove runtime-only fields from config for comparison."""
    if not isinstance(cfg, dict):
        return cfg
    result = {}
    for k, v in cfg.items():
        if k not in _CMP_KEYS:
            continue
        if isinstance(v, dict) and k in ('fans',):
            result[k] = {
                fid: {fk: fv for fk, fv in fval.items() if fk not in _RUNTIME_SENSOR_KEYS}
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
    """Queue command for HTTP delivery to agent."""
    cmd_type = event.replace('server:', '')
    queue_command(node_id, cmd_type, data)


# Grace period: skip conflict detection for 30s after server startup
_startup_time = time.monotonic()
_GRACE_PERIOD = 30


def _process_agent_data(node_id, telemetry):
    """Process telemetry data for a node — update state, DB, broadcast to browsers.
    Called by HTTP telemetry endpoint."""
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
            else:
                logger.warning(f'_process_agent_data: node {node_id} not found in DB')
                return

    invalidate_state_cache()
    _socketio_ref.emit('update', {'nodes': dict(state['nodes'])}) if _socketio_ref else None


def register_agent_handlers(socketio, on_connect=None, on_disconnect=None):
    """Register the socketio ref for browser broadcasts.
    
    No Socket.IO agent handlers — all agent communication is HTTP.
    Socket.IO is used only for browser→server real-time updates.
    """
    global _socketio_ref
    _socketio_ref = socketio

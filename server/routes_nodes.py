"""Node management endpoints — CRUD, discovery, config push, mode."""

from flask import Blueprint, request, jsonify

nodes_bp = Blueprint('nodes', __name__)

# ============================================================================
# NODE MANAGEMENT API
# ============================================================================

@nodes_bp.route('/api/nodes')
def api_list_nodes():
    """List all registered nodes."""
    from server.node_registry import list_nodes
    return jsonify(list_nodes())


@nodes_bp.route('/api/nodes', methods=['POST'])
def api_add_node():
    """Add a new node."""
    try:
        from server.node_registry import add_node
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Name required'}), 400
        node = add_node(name)
        return jsonify(node), 201
    except Exception as e:
        logger.error(f'api_add_node error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@nodes_bp.route('/api/nodes/<node_id>')
def api_get_node(node_id):
    """Get node details."""
    from server.node_registry import get_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    return jsonify(node)


@nodes_bp.route('/api/nodes/<node_id>', methods=['PUT'])
def api_update_node(node_id):
    """Update a node (name, ip, port, api_token)."""
    from server.node_registry import get_node, update_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    ip = data.get('ip', '').strip()
    port = data.get('port')
    api_token = data.get('api_token', '').strip()

    if update_node(node_id, name=name or None, ip=ip if ip is not None else None,
                   port=port, api_token=api_token or None):
        # Update in-memory state so next snapshot reflects the change immediately
        with state_lock:
            if node_id in state.get('nodes', {}):
                if name:
                    state['nodes'][node_id]['name'] = name
                if ip:
                    state['nodes'][node_id]['ip'] = ip
        invalidate_state_cache()
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Update failed'}), 500


@nodes_bp.route('/api/nodes/<node_id>', methods=['DELETE'])
def api_delete_node(node_id):
    """Delete a node — clean up DB, state, discovery cache, and disconnect agent."""
    from server.node_registry import delete_node, get_node
    from core.state import state, state_lock, invalidate_state_cache
    existing_node = get_node(node_id)
    deleted_ip = existing_node.get('ip', '') if existing_node else ''
    if delete_node(node_id):
        with state_lock:
            state.get('nodes', {}).pop(node_id, None)
            # Remove dashboard cards belonging to this agent
            dashboard = state.get('dashboard', {})
            cards = dashboard.get('cards', [])
            dashboard['cards'] = [c for c in cards if c.get('source') != node_id]
        # Remove from SSDP discovered cache by both node_id and IP
        from server.discovery import _discovered_nodes, _lock as disc_lock
        with disc_lock:
            to_remove = [k for k, v in _discovered_nodes.items()
                         if v.get('node_id') == node_id or (deleted_ip and v.get('ip') == deleted_ip)]
            for k in to_remove:
                _discovered_nodes.pop(k, None)
        invalidate_state_cache()
        # Persist dashboard changes
        from core.config import save_config
        save_config()
        # Notify browsers to refresh dashboard (cards were removed)
        try:
            from app import socketio
            socketio.emit('update', {
                'nodes': dict(state.get('nodes', {})),
                'dashboard': state.get('dashboard', {}),
                'config_version': CONFIG_VERSION,
            })
        except Exception:
            pass
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Node not found'}), 404


@nodes_bp.route('/api/nodes/<node_id>/config', methods=['POST'])
def api_push_config(node_id):
    """Push config to agent."""
    from server.node_registry import get_node, update_node_config
    from server.agent_handlers import _emit_to_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    update_node_config(node_id, data.get('config', {}))
    from app import socketio
    _emit_to_node(socketio, 'server:config_push', {
        'config': data.get('config', {}),
    }, node_id)
    return jsonify({'status': 'pushed'})


@nodes_bp.route('/api/nodes/<node_id>/mode', methods=['POST'])
def api_set_node_mode(node_id):
    """Set agent control mode."""
    from server.node_registry import get_node, update_node_control_mode
    from server.agent_handlers import _emit_to_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'server')
    if mode not in ('server', 'manual'):
        return jsonify({'error': 'Invalid mode'}), 400
    update_node_control_mode(node_id, mode)
    from app import socketio
    _emit_to_node(socketio, 'server:set_control_mode', {
        'mode': mode,
    }, node_id)
    return jsonify({'mode': mode})


@nodes_bp.route('/api/nodes/discover')
def api_discover_nodes():
    """Scan LAN for agents via SSDP + HTTP probe of offline nodes."""
    from server.discovery import scan_for_agents, probe_known_agents
    nodes = scan_for_agents(timeout=3)
    # Also probe offline nodes directly via HTTP
    probed = probe_known_agents(timeout=2)
    # Merge: SSDP results first, then newly-probed online nodes
    found_ids = {n['node_id'] for n in nodes}
    for p in probed:
        if p['node_id'] not in found_ids:
            nodes.append(p)
    return jsonify(nodes)


@nodes_bp.route('/api/nodes/scan-subnet', methods=['POST'])
def api_scan_subnet():
    """Fast TCP scan of local subnet for FanControl agents on port 5059."""
    from server.discovery import scan_subnet
    from server.node_registry import list_nodes
    try:
        data = request.get_json(silent=True) or {}
        port = int(data.get('port', 5059))
        results = scan_subnet(port=port)

        # Mark already-registered agents
        existing_nodes = list_nodes()
        existing_ips = {n['ip']: n for n in existing_nodes if n.get('ip')}
        for r in results:
            ip = r.get('ip', '')
            if ip in existing_ips:
                r['already_registered'] = True
                r['node_id'] = existing_ips[ip]['node_id']
                r['name'] = existing_ips[ip]['name']
            else:
                r['already_registered'] = False

        return jsonify(results)
    except Exception as e:
        logger.error(f'Subnet scan error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@nodes_bp.route('/api/nodes/probe', methods=['POST'])
def api_probe_ip():
    """Probe a specific IP for an agent."""
    from server.discovery import probe_agent
    from server.node_registry import list_nodes, get_node
    data = request.get_json(silent=True) or {}
    ip = (data.get('ip') or '').strip()
    port = int(data.get('port', 5059))
    if not ip:
        return jsonify({'error': 'IP required'}), 400

    info = probe_agent(ip, port=port, timeout=3)
    if not info:
        return jsonify({'error': 'Agent not reachable'}), 404

    # Check if this agent is already registered
    nodes = list_nodes()
    existing = None
    for n in nodes:
        if n.get('ip') == ip:
            existing = n
            break

    if existing:
        # Update status to online
        from server.node_registry import update_node_status
        update_node_status(existing['node_id'], 'online')
        info['node_id'] = existing['node_id']
        info['name'] = existing['name']
        info['already_registered'] = True
    else:
        info['already_registered'] = False

    return jsonify(info)


@nodes_bp.route('/api/nodes/add-by-ip', methods=['POST'])
def api_add_node_by_ip():
    """Add a node by IP address directly."""
    from server.node_registry import add_node, list_nodes
    from server.discovery import probe_agent
    data = request.get_json(silent=True) or {}
    ip = (data.get('ip') or '').strip()
    name = (data.get('name') or '').strip()
    port = int(data.get('port', 5059))
    if not ip:
        return jsonify({'error': 'IP required'}), 400
    if not name:
        name = ip

    # Check for duplicate IP
    for n in list_nodes():
        if n.get('ip') == ip:
            return jsonify({'error': 'Node with this IP already exists'}), 409

    info = probe_agent(ip, port=port, timeout=3)

    # Fetch api_token from agent via HTTP
    api_token = ''
    if info:
        try:
            import urllib.request
            import json
            resp = urllib.request.urlopen(f'http://{ip}:{port}/api/agent/status', timeout=5)
            status = json.loads(resp.read())
            api_token = status.get('api_token', '')
        except Exception:
            pass

    node = add_node(name, api_token=api_token, ip=ip)

    from server.node_registry import update_node_status
    update_node_status(node['node_id'], 'online' if info else 'offline')

    return jsonify(node), 201


# ============================================================================
# DISCOVERED AGENTS API
# ============================================================================

@nodes_bp.route('/api/discovered')
def api_list_discovered():
    """List discovered but unregistered agents."""
    from server.discovery import _discovered_nodes, _lock
    from server.node_registry import list_nodes
    existing_ips = {n['ip'] for n in list_nodes() if n.get('ip')}
    with _lock:
        agents = [a for a in _discovered_nodes.values() if a.get('ip') not in existing_ips]
        return jsonify(agents)


@nodes_bp.route('/api/discovered/<node_id>/accept', methods=['POST'])
def api_accept_discovered(node_id):
    """Accept a discovered agent and register it.

    Fetches the api_token from the agent's /api/agent/status endpoint
    over unicast HTTP (token is no longer broadcast via SSDP).

    Accepts optional ?ip= query param for agents found via subnet scan
    (not stored in SSDP _discovered_nodes).
    """
    try:
        from server.discovery import _discovered_nodes, _lock
        from server.node_registry import add_node, list_nodes
        import urllib.request
        from flask import request as flask_request

        agent_ip = ''
        agent_name = node_id

        with _lock:
            agent = _discovered_nodes.get(node_id)
            if agent:
                agent_ip = agent.get('ip', '')
                agent_name = agent.get('name', node_id)
            else:
                # Fallback: IP from query param (subnet scan) or already-registered check
                agent_ip = flask_request.args.get('ip', '').strip()
                if not agent_ip:
                    # Check if already registered by any existing node
                    return jsonify({'error': 'Agent not found — no IP provided'}), 404

        # Check if agent with this IP is already registered
        existing_ips = {n['ip']: n for n in list_nodes() if n.get('ip')}
        if agent_ip in existing_ips:
            existing = existing_ips[agent_ip]
            with _lock:
                _discovered_nodes.pop(node_id, None)
            return jsonify({'message': 'Agent already registered', 'node_id': existing['node_id']}), 200

        # Fetch api_token from agent via unicast HTTP
        api_token = ''
        try:
            url = f'http://{agent_ip}:5059/api/agent/status'
            req = urllib.request.urlopen(url, timeout=5)
            import json
            status = json.loads(req.read())
            api_token = status.get('api_token', '')
            agent_name = status.get('node_name', agent_name)
        except Exception as e:
            logger.warning(f'Could not fetch token from agent {agent_ip}: {e}')
            return jsonify({'error': f'Could not reach agent at {agent_ip}'}), 502

        node = add_node(agent_name, api_token=api_token, ip=agent_ip)

        # Populate state['nodes'] as 'pending' — will go 'online' on first telemetry
        from core.state import state, state_lock, invalidate_state_cache
        new_node = {
            'node_id': node['node_id'],
            'stable_id': node.get('stable_id', ''),
            'name': node['name'],
            'status': 'pending',
            'control_mode': 'server',
            'config': {},
            'dsm_schemes': [],
            'kernel_info': {},
            'agent_version': '',
            'auto_update': 0,
            'pending_update': 0,
            'update_started': None,
        }
        with state_lock:
            state['nodes'][node['node_id']] = new_node
        invalidate_state_cache()

        with _lock:
            _discovered_nodes.pop(node_id, None)

        logger.info(f'Accepted agent {node["name"]} ({agent_ip}) node_id={node["node_id"]}')
        return jsonify(node), 201
    except Exception as e:
        logger.error(f'api_accept_discovered error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500
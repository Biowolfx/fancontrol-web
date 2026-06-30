# Auto-Pairing Agent-Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement automatic agent-server pairing without manual token configuration.

**Architecture:** Agent generates token on first boot, broadcasts via SSDP. Server discovers agent, shows notification, user clicks "Add" to pair. Token exchange happens automatically.

**Tech Stack:** Python Flask+SocketIO, SQLite, vanilla JS

## Global Constraints

- Version bump required with every visible change
- Git identity: use `-c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev"` inline flags
- User communicates in Russian
- No new dependencies
- Backward compatible with existing token-based setup

---

### Task 1: Agent Token Auto-Generation

**Files:**
- Modify: `agent/client.py` (token generation on first boot)
- Modify: `agent/announcer.py` (include token in SSDP broadcast)

**Interfaces:**
- Consumes: `API_TOKEN` env var (optional)
- Produces: auto-generated token saved to `/data/config.json`

- [ ] **Step 1: Add token auto-generation in `agent/client.py`**

At the top of the file, after imports, add token initialization:

```python
def _init_token():
    """Generate or load API token for this agent."""
    import json
    from pathlib import Path
    
    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
    
    # If token provided via env, use it
    if API_TOKEN:
        return API_TOKEN
    
    # Try to load from config
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
                if config.get('api_token'):
                    return config['api_token']
        except Exception:
            pass
    
    # Generate new token
    import uuid
    new_token = uuid.uuid4().hex
    
    # Save to config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass
    
    config['api_token'] = new_token
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    
    logger.info(f'Generated new API token: {new_token[:8]}...')
    return new_token
```

Then modify the module-level variables:

```python
# Agent-specific state fields
API_TOKEN = _init_token()
state['control_mode'] = 'server'  # 'server' or 'manual'
state['server_connected'] = False
state['server_url'] = SERVER_URL
state['node_id'] = NODE_ID
state['node_name'] = NODE_NAME
state['api_token'] = API_TOKEN
state['agent_config_snapshot'] = None
```

- [ ] **Step 2: Update SSDP announcer to include token**

In `agent/announcer.py`, modify `_build_ssdp_response`:

```python
def _build_ssdp_response(node_id: str, node_name: str, port: int = 5059, api_token: str = '') -> str:
    ip = _get_local_ip()
    return (
        'HTTP/1.1 200 OK\r\n'
        'CACHE-CONTROL: max-age=60\r\n'
        'EXT: \r\n'
        f'LOCATION: http://{ip}:{port}\r\n'
        'SERVER: FanControl-Web/3.5.128\r\n'
        f'USN: urn:fancontrol-web:agent:{node_id}\r\n'
        'ST: urn:fancontrol-web:agent\r\n'
        f'X-FanControl-Name: {node_name}\r\n'
        f'X-FanControl-Id: {node_id}\r\n'
        f'X-FanControl-Token: {api_token}\r\n'
        '\r\n'
    )
```

And update `start_announcer` to accept and pass token:

```python
def start_announcer(node_id: str, node_name: str, port: int = 5059, api_token: str = '') -> Optional[threading.Thread]:
    def _announce_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            response = _build_ssdp_response(node_id, node_name, port, api_token)
            # ... rest of loop
```

- [ ] **Step 3: Update `start_client` to pass token**

In `agent/client.py`, update the call:

```python
def start_client():
    """Start the WebSocket client connection to server."""
    global _sio, _telemetry_thread

    from agent.announcer import start_announcer
    start_announcer(NODE_ID, NODE_NAME, api_token=API_TOKEN)  # Add token
    # ... rest of function
```

- [ ] **Step 4: Test and commit**

```bash
python3 -m pytest tests/ -q
git add agent/client.py agent/announcer.py
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "feat: agent auto-generates token on first boot, includes in SSDP broadcast"
```

---

### Task 2: Server SSDP Listener + Discovery Notification

**Files:**
- Modify: `server/discovery.py` (continuous listener)
- Modify: `server/socket_handlers.py` (emit discovery events)
- Modify: `server/routes.py` (add discovery endpoints)

**Interfaces:**
- Consumes: SSDP broadcasts from agents
- Produces: `node:discovered` SocketIO events, `/api/discovered` endpoint

- [ ] **Step 1: Add continuous SSDP listener in `server/discovery.py`**

Add a new function after `scan_for_agents`:

```python
_discovery_callbacks = []
_listener_running = False


def on_agent_discovered(callback):
    """Register callback for when new agent is discovered."""
    _discovery_callbacks.append(callback)


def start_discovery_listener():
    """Start continuous SSDP listener for agent broadcasts."""
    global _listener_running
    if _listener_running:
        return
    
    _listener_running = True
    
    def _listen_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('', SSDP_PORT))
            
            # Join multicast group
            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton('0.0.0.0')
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1)
            
            logger.info('SSDP discovery listener started')
            
            while _listener_running:
                try:
                    data, addr = sock.recvfrom(1024)
                    _parse_and_notify(data.decode(errors='ignore'), addr[0])
                except socket.timeout:
                    continue
                except Exception as e:
                    logger.debug(f'Discovery listener error: {e}')
            
            sock.close()
        except Exception as e:
            logger.error(f'Discovery listener failed: {e}')
    
    thread = threading.Thread(target=_listen_loop, daemon=True)
    thread.start()


def _parse_and_notify(data: str, source_ip: str):
    """Parse SSDP response and notify if new agent."""
    global _discovered_nodes
    
    headers = {}
    for line in data.split('\r\n'):
        if ':' in line:
            key, _, value = line.partition(':')
            headers[key.strip().upper()] = value.strip()
    
    if headers.get('ST') != 'urn:fancontrol-web:agent':
        return
    
    node_id = headers.get('X-FANCONTROL-ID', '')
    node_name = headers.get('X-FANCONTROL-NAME', '')
    api_token = headers.get('X-FANCONTROL-TOKEN', '')
    location = headers.get('LOCATION', '')
    
    if not node_id:
        return
    
    # Check if already discovered or registered
    with _lock:
        if node_id in _discovered_nodes:
            return
        
        from server.node_registry import get_node_by_token, get_node
        if get_node(node_id) or get_node_by_token(api_token):
            return  # Already registered
        
        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'api_token': api_token,
            'location': location,
            'discovered_at': datetime.utcnow().isoformat(),
        }
    
    # Notify callbacks
    for cb in _discovery_callbacks:
        try:
            cb(_discovered_nodes[node_id])
        except Exception as e:
            logger.error(f'Discovery callback error: {e}')
    
    logger.info(f'Discovered new agent: {node_name} ({source_ip})')
```

- [ ] **Step 2: Add SocketIO emission in `server/socket_handlers.py`**

In `register_handlers`, add discovery listener:

```python
def register_handlers(socketio):
    """Register Socket.IO event handlers."""
    
    # Start SSDP discovery listener
    from server.discovery import start_discovery_listener, on_agent_discovered
    
    def on_new_agent(agent_info):
        socketio.emit('node:discovered', agent_info)
    
    on_agent_discovered(on_new_agent)
    start_discovery_listener()
    
    # ... existing code
```

- [ ] **Step 3: Add API endpoints in `server/routes.py`**

```python
@routes.route('/api/discovered')
def api_list_discovered():
    """List discovered but unregistered agents."""
    from server.discovery import _discovered_nodes
    with _lock if hasattr(_discovered_nodes, '__class__') else DummyLock():
        return jsonify(list(_discovered_nodes.values()))


@routes.route('/api/discovered/<node_id>/accept', methods=['POST'])
def api_accept_discovered(node_id):
    """Accept a discovered agent and register it."""
    from server.discovery import _discovered_nodes
    from server.node_registry import add_node
    
    with _lock:
        agent = _discovered_nodes.get(node_id)
        if not agent:
            return jsonify({'error': 'Agent not found'}), 404
        
        # Register with the agent's self-generated token
        node = add_node(agent['name'], api_token=agent['api_token'])
        
        # Remove from discovered
        del _discovered_nodes[node_id]
    
    return jsonify(node), 201
```

- [ ] **Step 4: Test and commit**

```bash
python3 -m pytest tests/ -q
git add server/discovery.py server/socket_handlers.py server/routes.py
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "feat: server continuous SSDP listener with discovery notifications"
```

---

### Task 3: Frontend Toast Notifications

**Files:**
- Modify: `templates/index.html` (toast CSS)
- Modify: `templates/js/main.js` (toast display, accept button)

**Interfaces:**
- Consumes: `node:discovered` SocketIO events
- Produces: toast notification with "Add" button

- [ ] **Step 1: Add toast CSS in `index.html`**

Add to `<style>` section:

```css
.toast-container {
    position: fixed;
    top: 20px;
    right: 20px;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.toast {
    background: #1a1f2e;
    border: 1px solid #22d3ee;
    border-radius: 8px;
    padding: 12px 16px;
    color: #e5e7eb;
    box-shadow: 0 4px 20px rgba(34, 211, 238, 0.2);
    animation: toast-in 0.3s ease-out;
    max-width: 350px;
}
.toast-success { border-color: #4ade80; }
.toast-warning { border-color: #facc15; }
.toast-error { border-color: #f87171; }
@keyframes toast-in {
    from { opacity: 0; transform: translateX(100px); }
    to { opacity: 1; transform: translateX(0); }
}
.toast-btn {
    background: #22d3ee;
    color: #0f172a;
    border: none;
    border-radius: 4px;
    padding: 4px 12px;
    cursor: pointer;
    font-weight: 600;
    margin-left: 8px;
}
.toast-btn:hover { background: #06b6d4; }
.toast-btn-secondary {
    background: transparent;
    border: 1px solid #4b5563;
    color: #9ca3af;
}
.toast-btn-secondary:hover { border-color: #6b7280; }
```

Add container div before `</body>`:

```html
<div id="toast-container" class="toast-container"></div>
```

- [ ] **Step 2: Add toast function and discovery handler in `main.js`**

```javascript
function showToast(message, type = 'info', actions = []) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    
    let html = `<span>${message}</span>`;
    actions.forEach(action => {
        html += `<button class="toast-btn ${action.secondary ? 'toast-btn-secondary' : ''}" 
                    onclick="${action.onclick}">${action.label}</button>`;
    });
    
    toast.innerHTML = html;
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100px)';
        setTimeout(() => toast.remove(), 300);
    }, 8000);
}

// Handle agent discovery
socket.on('node:discovered', (data) => {
    const msg = `Новый агент: ${data.name} (${data.ip})`;
    showToast(msg, 'warning', [
        { label: 'Добавить', onclick: `acceptDiscoveredAgent('${data.node_id}')` },
        { label: 'Игнорировать', onclick: 'this.closest(".toast").remove()', secondary: true },
    ]);
});

async function acceptDiscoveredAgent(nodeId) {
    try {
        const resp = await fetch(`/api/discovered/${nodeId}/accept`, { method: 'POST' });
        if (resp.ok) {
            showToast('Агент добавлен! Переподключение...', 'success');
            loadNodes();
        }
    } catch (e) {
        showToast('Ошибка добавления агента', 'error');
    }
}
```

- [ ] **Step 3: Test and commit**

```bash
python3 -m pytest tests/ -q
git add templates/index.html templates/js/main.js
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "feat: toast notifications for agent discovery with accept button"
```

---

### Task 4: Agent Token Push Handler

**Files:**
- Modify: `agent/client.py` (handle token push from server)
- Modify: `server/agent_handlers.py` (emit token push after registration)

**Interfaces:**
- Consumes: `server:token_push` SocketIO event
- Produces: token saved locally, reconnection with new token

- [ ] **Step 1: Add token push handler in `agent/client.py`**

```python
def _on_token_push(data):
    """Server pushes new token after registration."""
    global API_TOKEN
    new_token = data.get('token')
    if not new_token:
        return
    
    API_TOKEN = new_token
    state['api_token'] = new_token
    
    # Save to config
    import json
    from pathlib import Path
    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
    config = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass
    
    config['api_token'] = new_token
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    
    logger.info(f'Received new token from server, reconnecting...')
    
    # Reconnect with new token
    if _sio and _sio.connected:
        _sio.disconnect()
```

Register the handler in `start_client`:

```python
_sio.on('server:token_push', _on_token_push)
```

- [ ] **Step 2: Emit token push after registration in `server/agent_handlers.py`**

In `handle_agent_connect`, after successful registration:

```python
# After update_node_status
socketio.emit('server:token_push', {
    'token': node['api_token'],
}, room=node_id)
```

- [ ] **Step 3: Test and commit**

```bash
python3 -m pytest tests/ -q
git add agent/client.py server/agent_handlers.py
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "feat: server pushes token to agent after registration, agent auto-reconnects"
```

---

### Task 5: Version Bump and Final Push

**Files:**
- Modify: `core/state.py` (CONFIG_VERSION)
- Modify: `templates/index.html` (cache buster)

- [ ] **Step 1: Bump version to 3.5.128**

```python
CONFIG_VERSION = "3.5.128"
```

- [ ] **Step 2: Update cache buster**

```html
<script src="/js/main.js?v=3.5.128"></script>
```

- [ ] **Step 3: Run tests**

```bash
python3 -m pytest tests/ -q
```

- [ ] **Step 4: Commit and push**

```bash
git add core/state.py templates/index.html
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "chore: bump version to 3.5.128"
git push
```

# Server Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-side node registry, agent Socket.IO handlers, and node management API to FanControl Web server.

**Architecture:** SQLite database for node storage. Socket.IO handlers for agent connections. REST API for node management. Runtime state in core/state.py.

**Tech Stack:** Python 3, Flask, Flask-SocketIO, SQLite, python-socketio

**Spec:** `docs/compose/specs/2026-06-19-phase3-6-installer-design.md` [S4]

---

## File Structure

```
server/
├── __init__.py
├── routes.py              # Existing (modify: add node API)
├── socket_handlers.py     # Existing (modify: add agent handlers)
├── node_registry.py       # NEW: SQLite node storage
└── agent_handlers.py      # NEW: Socket.IO agent event handlers
```

---

## Task 1: Create node registry module

**Covers:** [S4]

**Files:**
- Create: `server/node_registry.py`

- [ ] **Step 1: Create server/node_registry.py**

```python
"""Node registry — SQLite storage for connected agents."""

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.config import DATA_DIR

logger = logging.getLogger('fancontrol')

DB_PATH = DATA_DIR / 'fancontrol.db'
_db_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn


def init_nodes_table():
    """Create nodes table if not exists."""
    with _get_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                api_token TEXT UNIQUE NOT NULL,
                config TEXT DEFAULT '{}',
                telemetry TEXT DEFAULT '{}',
                control_mode TEXT DEFAULT 'server',
                status TEXT DEFAULT 'offline',
                last_seen TEXT,
                agent_config_snapshot TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    logger.info('Nodes table initialized')


def add_node(name: str, api_token: str = None) -> Dict:
    """Add a new node. Returns the created node."""
    if not api_token:
        api_token = uuid.uuid4().hex

    node_id = name.lower().replace(' ', '-').replace('/', '-')
    now = datetime.utcnow().isoformat()

    with _get_conn() as conn:
        conn.execute(
            'INSERT INTO nodes (node_id, name, api_token, created_at) VALUES (?, ?, ?, ?)',
            (node_id, name, api_token, now)
        )
        conn.commit()

    return get_node(node_id)


def get_node(node_id: str) -> Optional[Dict]:
    """Get a node by ID."""
    with _get_conn() as conn:
        row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
        if row:
            return _row_to_dict(row)
    return None


def get_node_by_token(api_token: str) -> Optional[Dict]:
    """Get a node by API token."""
    with _get_conn() as conn:
        row = conn.execute('SELECT * FROM nodes WHERE api_token = ?', (api_token,)).fetchone()
        if row:
            return _row_to_dict(row)
    return None


def list_nodes() -> List[Dict]:
    """List all nodes."""
    with _get_conn() as conn:
        rows = conn.execute('SELECT * FROM nodes ORDER BY name').fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_node(node_id: str) -> bool:
    """Delete a node."""
    with _get_conn() as conn:
        cursor = conn.execute('DELETE FROM nodes WHERE node_id = ?', (node_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_node_status(node_id: str, status: str, telemetry: Dict = None):
    """Update node status and optional telemetry."""
    now = datetime.utcnow().isoformat()
    with _get_conn() as conn:
        if telemetry:
            conn.execute(
                'UPDATE nodes SET status = ?, last_seen = ?, telemetry = ? WHERE node_id = ?',
                (status, now, json.dumps(telemetry), node_id)
            )
        else:
            conn.execute(
                'UPDATE nodes SET status = ?, last_seen = ? WHERE node_id = ?',
                (status, now, node_id)
            )
        conn.commit()


def update_node_config(node_id: str, config: Dict):
    """Update node config (authoritative server config)."""
    with _get_conn() as conn:
        conn.execute(
            'UPDATE nodes SET config = ? WHERE node_id = ?',
            (json.dumps(config), node_id)
        )
        conn.commit()


def update_node_control_mode(node_id: str, mode: str):
    """Update node control mode."""
    with _get_conn() as conn:
        conn.execute(
            'UPDATE nodes SET control_mode = ? WHERE node_id = ?',
            (mode, node_id)
        )
        conn.commit()


def save_agent_snapshot(node_id: str, snapshot: Dict):
    """Save agent's config snapshot for revert."""
    with _get_conn() as conn:
        conn.execute(
            'UPDATE nodes SET agent_config_snapshot = ? WHERE node_id = ?',
            (json.dumps(snapshot), node_id)
        )
        conn.commit()


def get_agent_snapshot(node_id: str) -> Optional[Dict]:
    """Get agent's config snapshot."""
    node = get_node(node_id)
    if node and node.get('agent_config_snapshot'):
        return node['agent_config_snapshot']
    return None


def _row_to_dict(row) -> Dict:
    """Convert sqlite3.Row to dict, parsing JSON fields."""
    d = dict(row)
    for field in ('config', 'telemetry', 'agent_config_snapshot'):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                d[field] = {}
    return d
```

- [ ] **Step 2: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "
from server.node_registry import init_nodes_table, add_node, list_nodes
init_nodes_table()
n = add_node('Test Node')
print(f'Created: {n[\"node_id\"]}')
nodes = list_nodes()
print(f'Count: {len(nodes)}')
print('OK')
"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add server/node_registry.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add node registry with SQLite storage"
```

---

## Task 2: Create agent Socket.IO handlers

**Covers:** [S4]

**Files:**
- Create: `server/agent_handlers.py`
- Modify: `server/socket_handlers.py`

- [ ] **Step 1: Create server/agent_handlers.py**

```python
"""Socket.IO handlers for agent connections."""

import json
import logging
from typing import Optional

from core.state import state, state_lock, get_state, invalidate_state_cache

logger = logging.getLogger('fancontrol')


def register_agent_handlers(socketio):
    """Register Socket.IO handlers for agent connections."""

    @socketio.on('agent:connect')
    def handle_agent_connect(data):
        """Agent connects with token + config."""
        api_token = data.get('api_token', '')
        node_id = data.get('node_id', '')
        node_name = data.get('node_name', '')
        control_mode = data.get('control_mode', 'server')
        agent_config = data.get('config', {})

        # Verify token
        from server.node_registry import get_node_by_token, update_node_status, get_node
        node = get_node_by_token(api_token)

        if not node:
            logger.warning(f'Agent rejected: invalid token for node_id={node_id}')
            socketio.emit('server:error', {'message': 'Invalid token'})
            return

        # Update node status
        update_node_status(node['node_id'], 'online')
        logger.info(f'Agent connected: {node["name"]} ({node["node_id"]})')

        # Store agent config for runtime access
        with state_lock:
            if 'nodes' not in state:
                state['nodes'] = {}
            state['nodes'][node['node_id']] = {
                'name': node['name'],
                'status': 'online',
                'control_mode': control_mode,
                'config': node.get('config', {}),
                'telemetry': node.get('telemetry', {}),
            }
        invalidate_state_cache()

        # Push server config to agent
        server_config = node.get('config', {})
        if server_config and control_mode == 'server':
            socketio.emit('server:config_push', {
                'config': server_config,
            })
            logger.info(f'Pushed config to {node["name"]}')

        # Notify browsers
        socketio.emit('node:update', {
            'node_id': node['node_id'],
            'status': 'online',
            'name': node['name'],
        })

    @socketio.on('agent:telemetry')
    def handle_agent_telemetry(data):
        """Agent sends telemetry data."""
        node_id = data.get('node_id', '')
        telemetry = data.get('telemetry', {})

        from server.node_registry import update_node_status
        update_node_status(node_id, 'online', telemetry)

        # Update runtime state
        with state_lock:
            if 'nodes' in state and node_id in state['nodes']:
                state['nodes'][node_id]['telemetry'] = telemetry
                state['nodes'][node_id]['status'] = 'online'
        invalidate_state_cache()

        # Forward to browsers
        socketio.emit('node:telemetry', {
            'node_id': node_id,
            'telemetry': telemetry,
        })

    @socketio.on('agent:config_changed')
    def handle_agent_config_changed(data):
        """Agent reports local config change."""
        node_id = data.get('node_id', '')
        agent_config = data.get('config', {})

        from server.node_registry import get_node, save_agent_snapshot
        node = get_node(node_id)
        if not node:
            return

        server_config = node.get('config', {})
        if server_config and agent_config != server_config:
            # Config conflict — save snapshot for revert
            save_agent_snapshot(node_id, agent_config)
            logger.info(f'Config conflict detected for {node["name"]}')

            # Notify browsers
            socketio.emit('node:conflict', {
                'node_id': node_id,
                'name': node['name'],
                'server_config': server_config,
                'agent_config': agent_config,
            })

    @socketio.on('agent:control_mode_changed')
    def handle_agent_mode_changed(data):
        """Agent reports mode change."""
        node_id = data.get('node_id', '')
        mode = data.get('mode', 'server')

        from server.node_registry import update_node_control_mode
        update_node_control_mode(node_id, mode)

        with state_lock:
            if 'nodes' in state and node_id in state['nodes']:
                state['nodes'][node_id]['control_mode'] = mode
        invalidate_state_cache()

        # Notify browsers
        socketio.emit('node:mode_changed', {
            'node_id': node_id,
            'mode': mode,
        })

        logger.info(f'Agent {node_id} mode changed to {mode}')
```

- [ ] **Step 2: Update server/socket_handlers.py**

```python
"""Socket.IO event handlers."""

from server.agent_handlers import register_agent_handlers


def register_handlers(socketio):
    """Register all Socket.IO handlers."""
    from core.state import get_state, _init_complete

    @socketio.on('connect')
    def handle_socket_connect():
        _init_complete.wait(timeout=15)
        socketio.emit('update', get_state())

    @socketio.on('get_state')
    def handle_get_state():
        socketio.emit('update', get_state())

    # Register agent handlers
    register_agent_handlers(socketio)
```

- [ ] **Step 3: Update app.py — init nodes table**

In app.py, add `init_nodes_table()` call in `_auto_init()`:

```python
from server.node_registry import init_nodes_table

@app.before_request
def _auto_init():
    if not state.get('_gunicorn_initialized'):
        state['_gunicorn_initialized'] = True
        try:
            init_database()
            init_nodes_table()
        except Exception as e:
            logger.error(f'Database init error: {e}')
        init_hardware()
        _ensure_control_loop()
        _init_complete.set()
        # ...
```

- [ ] **Step 4: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "
from server.socket_handlers import register_handlers
print('socket_handlers OK')
from server.agent_handlers import register_agent_handlers
print('agent_handlers OK')
"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add server/agent_handlers.py server/socket_handlers.py app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add agent Socket.IO handlers for node connections"
```

---

## Task 3: Add node management REST API

**Covers:** [S4]

**Files:**
- Modify: `server/routes.py`

- [ ] **Step 1: Add node management routes to server/routes.py**

Add these routes at the end of `server/routes.py`:

```python
# ============================================================================
# NODE MANAGEMENT API
# ============================================================================

@routes.route('/api/nodes')
def api_list_nodes():
    """List all registered nodes."""
    from server.node_registry import list_nodes
    return jsonify(list_nodes())


@routes.route('/api/nodes', methods=['POST'])
def api_add_node():
    """Add a new node."""
    from server.node_registry import add_node
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400

    node = add_node(name)
    return jsonify(node), 201


@routes.route('/api/nodes/<node_id>')
def api_get_node(node_id):
    """Get node details."""
    from server.node_registry import get_node
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    return jsonify(node)


@routes.route('/api/nodes/<node_id>', methods=['DELETE'])
def api_delete_node(node_id):
    """Delete a node."""
    from server.node_registry import delete_node
    if delete_node(node_id):
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Node not found'}), 404


@routes.route('/api/nodes/<node_id>/config', methods=['POST'])
def api_push_config(node_id):
    """Push config to agent."""
    from server.node_registry import get_node, update_node_config
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    data = request.get_json()
    update_node_config(node_id, data.get('config', {}))

    # Push via Socket.IO
    from app import socketio
    socketio.emit('server:config_push', {
        'config': data.get('config', {}),
    }, room=node_id)

    return jsonify({'status': 'pushed'})


@routes.route('/api/nodes/<node_id>/mode', methods=['POST'])
def api_set_node_mode(node_id):
    """Set agent control mode."""
    from server.node_registry import get_node, update_node_control_mode
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    data = request.get_json()
    mode = data.get('mode', 'server')
    if mode not in ('server', 'manual'):
        return jsonify({'error': 'Invalid mode'}), 400

    update_node_control_mode(node_id, mode)

    # Notify agent via Socket.IO
    from app import socketio
    socketio.emit('server:set_control_mode', {
        'mode': mode,
    }, room=node_id)

    return jsonify({'mode': mode})
```

- [ ] **Step 2: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "
from server.routes import routes
print('routes OK')
"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add server/routes.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add node management REST API endpoints"
```

---

## Task 4: Add node state to core/state.py

**Covers:** [S4]

**Files:**
- Modify: `core/state.py`

- [ ] **Step 1: Update core/state.py — add nodes to state dict**

Add `nodes` field to the state dict:

```python
state: Dict[str, Any] = {
    'fans': {},
    'temp_sensors': {},
    'hdd_sensors': {},
    'max_hdd_temp': 0,
    'tested': False,
    'testing': False,
    'test_progress': {},
    '_pause_loop': False,
    'failsafe': False,
    'standby_mode': False,
    'disks_polling': False,
    'last_hdd_poll': 0.0,
    'initialized': False,
    'hardware_scanned': False,
    'config_version': CONFIG_VERSION,
    'nodes': {},  # Runtime state for connected agents
}
```

Also update `_build_state_snapshot()` to include nodes:

```python
def _build_state_snapshot() -> Dict[str, Any]:
    return {
        # ... existing fields ...
        'nodes': dict(state.get('nodes', {})),  # shallow copy — agents dict
    }
```

- [ ] **Step 2: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "
from core.state import state, get_state
assert 'nodes' in state
s = get_state()
assert 'nodes' in s
print('OK')
"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add core/state.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add nodes runtime state to core/state.py"
```

# Phase 3-6 + Installer — Design Spec

## [S1] Problem

Phases 1-2 are complete (refactoring + agent mode). Remaining:
- Phase 3: Server multi-node dashboard
- Phase 4: Config sync + conflict manager
- Phase 5: SSDP discovery + manual node add
- Phase 6: Testing on 2+ nodes

Plus: unified installation script with language selection and component choice.

## [S2] Sub-projects

| ID | Name | Description | Dependencies |
|---|---|---|---|
| A | Web UI Installer | Standalone setup wizard | None |
| B | Server Foundation | Node registry + socket handlers | Phase 1-2 |
| C | Multi-node Dashboard | Sidebar + cards + detail views | B |
| D | Config Sync | Server pushes configs, conflict resolution | B |
| E | SSDP Discovery | LAN broadcast + manual add | B |

A and B can be built in parallel.

## [S3] Sub-project A: Web UI Installer

### Overview
Standalone Python script (`installer/install.py`) with Flask + Tailwind. Runs temporary web server on port 5060.

### Flow
1. User runs `python3 install.py`
2. Opens `http://localhost:5060`
3. Step 1: Language selection (EN/RU)
4. Step 2: Component selection (Server / Agent)
5. Step 3: Configuration:
   - Server: port, agent token path, data directory
   - Agent: server URL, API token, node name
6. Step 4: Installation:
   - Try Docker image from registry
   - Fallback: git clone + docker compose build
   - Configure docker-compose.yml
   - Start container
7. Step 5: Completion — link to dashboard

### Files
- `installer/install.py` — Flask server + installation logic
- `installer/templates/setup.html` — Setup wizard UI
- `installer/static/` — CSS/JS

### API Endpoints (installer)
| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Setup wizard page |
| `/api/config` | POST | Save configuration |
| `/api/install` | POST | Start installation |
| `/api/status` | GET | Installation progress (SSE) |

## [S4] Sub-project B: Server Foundation

### Node Registry (SQLite)

```sql
CREATE TABLE nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_token TEXT UNIQUE NOT NULL,
    server_url TEXT,
    config TEXT DEFAULT '{}',
    telemetry TEXT DEFAULT '{}',
    control_mode TEXT DEFAULT 'server',
    status TEXT DEFAULT 'offline',
    last_seen TIMESTAMP,
    agent_config_snapshot TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Server Socket.IO Handlers

Events agent → server:
| Event | Handler | Action |
|---|---|---|
| `agent:connect` | `_handle_agent_connect` | Verify token, register node, push config |
| `agent:telemetry` | `_handle_agent_telemetry` | Update telemetry, broadcast to browsers |
| `agent:config_changed` | `_handle_agent_config_changed` | Save config, detect conflict |
| `agent:control_mode_changed` | `_handle_agent_mode_changed` | Update mode, broadcast |

Events server → agent:
| Event | Purpose |
|---|---|
| `server:config_push` | Push authoritative config |
| `server:command` | set_fan, set_mode, restart |
| `server:set_control_mode` | Switch server/manual |

### REST API for Node Management

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/nodes` | GET | List all nodes |
| `/api/nodes` | POST | Add node (name, api_token) |
| `/api/nodes/<id>` | GET | Get node details |
| `/api/nodes/<id>` | DELETE | Remove node |
| `/api/nodes/<id>/config` | POST | Push config to agent |
| `/api/nodes/<id>/mode` | POST | Set agent control mode |
| `/api/nodes/discover` | GET | SSDP scan results |

### Files
- Create: `server/node_registry.py` — SQLite node storage
- Create: `server/agent_handlers.py` — Socket.IO agent event handlers
- Modify: `server/socket_handlers.py` — Add agent handlers
- Modify: `server/routes.py` — Add node management API
- Modify: `core/state.py` — Add nodes dict for runtime state

## [S5] Sub-project C: Multi-node Dashboard UI

### Sidebar "Nodes"
- List of connected agents: name, status (online/offline), IP
- Max temp, RPM summary
- Conflict icon / manual mode warning icon
- Click → detail view

### Nodes Overview Page
- Cards for all nodes: name, status, temps, fans, disk health
- Click → detailed dashboard (full fan control as current single-node view)

### Conflict Modal
- Shows server vs agent config side-by-side
- Buttons: "Apply Server" / "Keep Agent Config"

### Manual Mode Warning
- Banner when agent is in manual mode
- Button: "Switch to Server Control"

### Node Settings
- Add node form: name, API token
- Network discovery: "Discover" button → SSDP scan
- Delete node confirmation

### Files
- Modify: `templates/index.html` — Add sidebar, node cards, modals
- Modify: `templates/js/main.js` — Add node management logic
- Modify: `static/lang/en.json` — Add node-related translations
- Modify: `static/lang/ru.json` — Add node-related translations

## [S6] Sub-project D: Config Sync

### Sync Logic (server-side)
1. Agent connects → compare configs → if differs and agent in server mode → push
2. Server changes config → push to agent in real-time
3. Agent sends config_changed → save, detect conflict if agent in manual mode
4. Connection lost → mark offline, keep last config
5. Reconnection → full sync cycle

### Conflict Detection
- When agent sends config_changed and it differs from server's config
- Server stores both configs
- UI shows conflict modal with both options

### Files
- Modify: `server/node_registry.py` — Add config comparison
- Modify: `server/agent_handlers.py` — Add conflict detection
- Modify: `templates/js/main.js` — Add conflict resolution UI

## [S7] Sub-project E: SSDP Discovery

### Agent SSDP Announcer
- Broadcasts every 60 seconds
- UUID: `urn:fancontrol-web:agent:<node_id>`
- Location: `http://<ip>:5059`

### Server SSDP Listener
- Listens for SSDP M-SEARCH responses
- Shows discovered nodes in UI
- "Add" button → pre-fills form with discovered info

### Files
- Create: `agent/announcer.py` — SSDP broadcast
- Create: `server/discovery.py` — SSDP listener
- Modify: `server/routes.py` — Add discovery endpoint
- Modify: `templates/js/main.js` — Add discovery UI

## [S8] Implementation Order

| Step | Sub-project | Tasks | Est. Complexity |
|---|---|---|---|
| 1a | A: Installer | Create installer/ directory, install.py, setup.html | Medium |
| 1b | B: Server Foundation | node_registry.py, agent_handlers.py, socket handlers | High |
| 2 | C: Multi-node Dashboard | Sidebar, node cards, detail views, modals | Medium |
| 3 | D: Config Sync | Sync logic, conflict detection | Medium |
| 4 | E: SSDP Discovery | Announcer, listener, discovery UI | Low |
| 5 | Testing | Multi-node integration tests | Medium |

## [S9] Key Technical Decisions

1. **Web UI installer** — consistent with existing web UI pattern, works on any OS with Python
2. **SQLite for node registry** — already used for telemetry, consistent
3. **Unique token per agent** — more secure than global token
4. **Sidebar + Cards + Detail** — scalable UI pattern for 15+ nodes
5. **Hybrid install** — try Docker image first, fallback to git clone
6. **SSDP for LAN discovery** — standard protocol, works on most networks

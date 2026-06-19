# FanControl Multi-Node Architecture

## [S1] Problem

FanControl Web currently runs as a single-node application on one NAS/server. The user needs to monitor and control fans on 15+ servers across hybrid LAN + WAN networks from a single dashboard.

## [S2] Solution Overview

**One Docker image, two modes:**
- `--mode server` (default) — current FanControl Web + multi-node dashboard
- `--mode agent` — autonomous agent with local control + server connection

**Communication:** Socket.IO (WebSocket) — agent initiates connection to server. Works behind NAT (outbound only). Auto-reconnect with exponential backoff.

**Authentication:** Static API token, generated on server setup, passed in Socket.IO handshake.

**Discovery:** SSDP broadcast in LAN (port 5059) + manual add by IP/domain + token for WAN.

## [S3] Data Model

### Agent Local Storage
```json
{
  "mode": "agent",
  "server_url": "ws://192.168.1.100:5059",
  "api_token": "generated-token",
  "node_id": "server1",
  "node_name": "Server 1",
  "control_mode": "server|manual",
  "fans": { /* fan config */ },
  "schedule": { /* schedule */ },
  "telemetry_interval": 5
}
```

### Server Storage (per node)
```json
{
  "node_id": "server1",
  "name": "Server 1",
  "api_token": "...",
  "config": { /* authoritative config */ },
  "telemetry": { /* latest readings */ },
  "control_mode": "server|manual",
  "status": "online|offline",
  "last_seen": "2025-01-15T10:30:00Z",
  "agent_config_snapshot": { /* agent config at conflict time */ }
}
```

## [S4] Sync Model

1. **Agent connects** → sends local config + `control_mode`
2. **Server compares** configs:
   - If `control_mode == "server"` and configs differ → push server config to agent
   - If `control_mode == "manual"` → server shows warning, no config push
   - Agent's old config saved in `agent_config_snapshot` for revert
3. **Server changes config** → pushes to agent in real-time
4. **Agent changes locally** (in manual mode) → sends `agent:config_changed` to server
5. **Connection lost** → agent continues with last known config, server marks offline
6. **Reconnection** → full sync cycle again

## [S5] Protocol

### Agent → Server
| Event | Data | Frequency |
|---|---|---|
| `agent:connect` | node_id, token, local config, control_mode | on connect |
| `agent:telemetry` | fans RPM/PWM, temps, disks, status | every 5s |
| `agent:config_changed` | changed config | on local change |
| `agent:control_mode_changed` | new mode | on mode switch |

### Server → Agent
| Event | Data | When |
|---|---|---|
| `server:config_push` | full config | on connect + on change |
| `server:command` | set_fan, set_mode, restart | from UI |
| `server:set_control_mode` | server|manual | from UI |
| `server:ping` | — | every 30s |

**Heartbeat:** Agent sends telemetry every 5s. If server misses 3 ticks → status "offline".

## [S6] Server UI Changes

### Sidebar "Nodes"
- List of connected agents: name, status (online/offline), IP, max temp, RPM
- Conflict icon when config diverges
- Manual mode warning icon

### Nodes Overview Page
- Cards for all nodes: name, status, temps, fans, disk health
- Click → detailed dashboard (full fan control as current)

### Conflict Modal
```
┌──────────────────────────────────────────────┐
│ ⚠️ Config "Server 2" differs                 │
│                                              │
│ Server: target=55°C, mode=auto               │
│ Agent:  target=50°C, mode=manual             │
│                                              │
│ [Apply Server] [Keep Agent Config]           │
└──────────────────────────────────────────────┘
```

### Manual Mode Warning
```
┌──────────────────────────────────────────────┐
│ ⚠️ "Server 2" — manual control               │
│ Agent controls fans locally.                 │
│ Server does not control settings.            │
│                                              │
│ [Switch to Server Control]                   │
└──────────────────────────────────────────────┘
```

### Node Settings
- Manual add: IP/domain + token + name
- Network discovery: "Discover" button → SSDP scan → found nodes list

## [S7] Agent Architecture

### Launch
```bash
docker run --privileged --network=host \
  -e MODE=agent \
  -e SERVER_URL=ws://192.168.1.100:5059 \
  -e API_TOKEN=xxx \
  fancontrol-web
```

### Components
1. **Local controller** — existing `loop()` + `set_pwm()` + calibration (unchanged)
2. **WebSocket client** — connect to server, send telemetry, receive config
3. **SSDP announcer** — broadcast every 60s for LAN discovery
4. **Web interface** — full dashboard on port 5059 (local access)

### Agent Control Modes
- **Server mode** (default): follows server config, local editing locked
- **Manual mode**: full local control, server shows warning

### Autonomy
- Connection lost: agent continues with last known config
- Agent page shows "Server unavailable" notification
- On reconnect: full sync cycle (server config priority)

## [S8] Implementation Phases

| Phase | Description | Complexity |
|---|---|---|
| 1 | Refactor: extract agent code from app.py into modules | Medium |
| 2 | Agent: mode flag + WebSocket client + local dashboard | High |
| 3 | Server: multi-node dashboard + sidebar | Medium |
| 4 | Config sync + conflict manager | Medium |
| 5 | SSDP discovery + manual node add | Low |
| 6 | Testing on 2+ nodes | Medium |

## [S9] Key Technical Decisions

1. **Agent initiates connection** — works behind NAT, no port forwarding needed
2. **Socket.IO** — already in use, supports reconnection, rooms, binary
3. **Agent computes PWM locally** — minimizes network traffic, works offline
4. **Server config priority** — single source of truth, prevents drift
5. **Same Docker image** — reduces maintenance, same codebase
6. **Static API token** — simple auth, generated on server setup

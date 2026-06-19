# Setup Mode Design Spec

## [S1] Problem

Current installer runs on the host as a separate Python script. User wants installer to run INSIDE the Docker container — single image, setup wizard on first boot.

## [S2] Solution

One Docker image (`fancontrol-web`) with three modes:
- `MODE=setup` — setup wizard (first boot when no config.json)
- `MODE=server` — server mode
- `MODE=agent` — agent mode

**Auto-detection:** If `/data/config.json` doesn't exist → show wizard. If exists → load config and run.

## [S3] Installation Flow

1. User runs: `docker run -p 5059:5059 -v /path/data:/data fancontrol-web`
2. Container starts → checks `/data/config.json`
   - No config → MODE=setup → show wizard on port 5059
   - Has config → load → run as server/agent
3. Wizard steps:
   - Step 1: Language selection (EN/RU)
   - Step 2: Mode selection (Server / Agent)
   - Step 3: Configuration (fields depend on mode)
   - Step 4: Install → restart container
4. After restart: loads config.json, runs as server or agent

## [S4] Configuration Fields

### Server (extended)
| Field | Description | Default |
|---|---|---|
| Language | EN / RU | EN |
| Port | Web interface port | 5059 |
| Data path | Container path | /data |
| Server name | Display name | My Server |
| Description | Server description | (empty) |
| Admin password | Web interface protection | (empty = no auth) |
| SSDP | Enable LAN discovery | Enabled |

### Agent (extended)
| Field | Description | Default |
|---|---|---|
| Language | EN / RU | EN |
| Server URL | ws://ip:port | (required) |
| API Token | Server token | (required) |
| Node name | Display name | Agent 1 |
| Description | Node description | (empty) |
| Data path | Container path | /data |

## [S5] Config Format

### Server config.json
```json
{
  "mode": "server",
  "lang": "en",
  "port": 5059,
  "server_name": "My Server",
  "description": "",
  "admin_password": "",
  "ssdp_enabled": true,
  "initialized": true
}
```

### Agent config.json
```json
{
  "mode": "agent",
  "lang": "en",
  "server_url": "ws://192.168.1.100:5059",
  "api_token": "...",
  "node_name": "Agent 1",
  "description": "",
  "initialized": true
}
```

## [S6] UI Wizard

- Tailwind CSS + vanilla JS (no framework)
- 4-step wizard with progress indicator
- Field validation
- Descriptions for each field
- "Test Connection" button for agent URL
- Completion screen with restart countdown

## [S7] Container Restart

After saving config, wizard triggers container restart:
```python
import subprocess, socket
subprocess.run(['docker', 'restart', socket.gethostname()])
```

Or shows instruction if Docker socket not available.

## [S8] Implementation

| # | Task | Complexity |
|---|---|---|
| 1 | Update app.py — setup mode detection | Medium |
| 2 | Create installer/wizard.py | Medium |
| 3 | Create installer/templates/setup.html | Medium |
| 4 | Update Dockerfile | Low |
| 5 | Testing | Medium |

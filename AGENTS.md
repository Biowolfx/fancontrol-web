# AGENTS.md — FanControl Web

## Project overview

Python 3.10 Flask+SocketIO app for controlling server fans via sysfs hardware I/O. Runs in privileged Docker on Synology NAS. Multi-node architecture: one image, two modes (`--mode server|agent`). Agent↔server communication uses HTTP (primary) + Socket.IO (fallback).

## Quick start

```bash
# Run tests (import-based only)
pytest tests/

# Run locally (no Docker)
python app.py --mode server

# Docker build + run
docker compose up --build
```

## Architecture

### Server side
- `app.py` — Entrypoint. Flask app, SocketIO, 3 run modes (setup/server/agent). Auto-init on first HTTP request under gunicorn.
- `core/state.py` — Global `state` dict (thread-safe via `state_lock` RLock). **`CONFIG_VERSION` here — bump for every visible change.**
- `core/config.py` — JSON config at `/data/config.json`, debounced save (0.5s)
- `core/control.py` — Main loop (5s interval), PWM curve, fan health detection, SQLite telemetry
- `core/hardware.py` — sysfs discovery, SMART parsing, `ThreadPoolExecutor(max_workers=16)`
- `core/telegram.py` — Telegram Bot API notifications (urllib, zero deps)
- `server/routes.py` — All HTTP endpoints including agent protocol (`/api/agent/*`)
- `server/agent_handlers.py` — Socket.IO handlers + command queue + `_process_agent_data()`
- `server/node_registry.py` — SQLite storage for registered agents (nodes.db)
- `server/socket_handlers.py` — Browser Socket.IO events + heartbeat checker
- `server/discovery.py` — SSDP discovery + TCP subnet scan

### Agent side
- `agent/client.py` — HTTP telemetry loop (primary), HTTP command poll (fallback), update check
- `agent/telemetry.py` — `get_telemetry()`, `get_local_config()`
- `agent/config.py` — Agent identity init, token management
- `agent/announcer.py` — SSDP broadcast + M-SEARCH responder
- `agent/routes.py` — Agent local web API (status, mode, SMART)

### Frontend (no build step)
- `templates/index.html` — Single HTML + CSS + Jinja2 templates
- `templates/js/main.js` — Dashboard, fan cards, picker, calibration UI
- `templates/js/socket-handlers.js` — All Socket.IO event handlers
- `templates/js/store.js` — Centralized state store
- `templates/js/utils.js` — showToast, escapeHtml, format helpers
- `templates/js/i18n.js` — Internationalization
- `templates/js/render-helpers.js` — Fan health icons, sensor checkboxes
- `templates/js/charts.js` — Sparkline rendering
- `static/lang/en.json`, `static/lang/ru.json` — i18n translations

## Rules

- **No lint/typecheck/format tooling configured.** No pyproject.toml, Makefile, tox.ini, or CI pipeline.
- **No editable install.** Use `pip install -r requirements.txt` or run directly.
- **`CONFIG_VERSION` in `core/state.py` must be bumped** with every visible change (user rule).
- **Version bump policy**: PATCH=bugfix, MINOR=new feature, MAJOR=breaking.
- **Privileged Docker required** for hardware access (`/sys/class/hwmon`, `smartctl`, Docker socket).
- **`/repo` volume mount** — entrypoint syncs `/repo` → `/app` on container start. In-app updates use `git pull` + `docker restart`.
- **Global mutable state** — `core/state.py` holds a module-level `state` dict. Always acquire `state_lock` before read/write. `get_state()` returns a 2s-cached snapshot.
- **No `importlib.reload()` on core modules** — breaks config detection.
- **Browser cache busting** — `?v=VERSION` in index.html script tag. Only bump `CONFIG_VERSION`.
- **i18n required for all UI text** — add keys to both `en.json` and `ru.json`. Use `t()` or `data-i18n`.
- **Monolith update only on request** — do NOT auto-update `monolith.py`.
- **Docker restart required after code changes** — agent auto-updates via HTTP command queue; server needs manual restart or API trigger.

## Agent↔server protocol (v3.13+)

**Primary channel: HTTP** (stateless, reliable)
- `POST /api/agent/telemetry` — agent sends telemetry every 5s, receives commands in response
- `GET /api/agent/poll?api_token=X` — agent polls for pending commands (fallback)
- `POST /api/agent/update_result` — agent reports update progress
- `POST /api/agent/command` — browser queues command for agent delivery

**Socket.IO** — kept for browser real-time updates and backward compat with old agents.

**Command queue** — `queue_command()` / `drain_commands()` in agent_handlers.py. `_emit_to_node()` queues commands alongside Socket.IO push.

**Key benefit**: api_token is the stable identifier. No SID mapping, no force disconnect, no reconnect race conditions.

## Key gotchas

- `eventlet` in requirements.txt but SocketIO uses `async_mode='threading'`.
- `os._exit(0)` used in setup wizard and agent updates (cannot `docker restart` from inside container).
- `smartctl` subprocess calls require `smartmontools` installed in container.
- Fan IDs are SHA256 hashes of hardware paths (`dev-{hash[:12]}`) — stable across reboots.
- Config save is debounced (0.5s) — rapid changes may not persist immediately.
- Control loop runs in a daemon thread — non-blocking but must not crash silently.
- **`stable_id` column** — SQLite may not support `UNIQUE` in `ALTER TABLE`. Migration uses `DEFAULT ''` without UNIQUE.
- **Agent delete race condition** — old `handle_disconnect` can corrupt new `_node_to_sid` mapping. Disconnect handler checks SID before popping.
- **Fan health pulse** — uses picker cards (dashboard), not `fan-card-*` elements. Health classes: `fan-alert-stopped` (red), `fan-alert-slowing` (yellow), `fan-alert-needs-calibration` (yellow). Pulse via `startCardPulse()` (outline toggle).
- **Telegram** — `core.telegram.configure()` must be called on startup after `load_config()`. Token/chat_id saved in config.json.

## Testing

```bash
pytest tests/                    # All import-based tests
pytest tests/test_integration.py::test_core_state_import  # Single test
```

Only import smoke tests exist. No unit tests, no integration tests with real hardware.

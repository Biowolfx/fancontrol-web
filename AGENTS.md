# AGENTS.md — FanControl Web

## Project overview

Python 3.10 Flask+SocketIO app for controlling server fans via sysfs hardware I/O. Runs in privileged Docker on Synology NAS. Single-file frontend (`templates/index.html` + `templates/js/`). Multi-node architecture: one image, two modes (`--mode server|agent`).

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

- `app.py` — Entrypoint. Flask app, SocketIO, 3 run modes (setup/server/agent). Auto-init on first HTTP request under gunicorn.
- `core/` — Hardware I/O, control loop, config persistence, state management
  - `core/state.py` — Global `state` dict (thread-safe via `state_lock` RLock). `CONFIG_VERSION` here — bump for every visible change.
  - `core/config.py` — JSON config at `/data/config.json`, debounced save (0.5s)
  - `core/control.py` — Main loop (5s interval), PWM curve, SQLite telemetry
  - `core/hardware.py` — sysfs discovery, SMART parsing, `ThreadPoolExecutor(max_workers=8)`
- `server/` — HTTP routes + SocketIO event handlers + multi-node registry
- `agent/` — WebSocket client for agent mode, SSDP announcer
- `installer/` — Setup wizard (Flask on port 5059)
- `templates/` — Single `index.html` + JS bundle (no build step)
- `static/lang/` — i18n translation files
- `tests/test_integration.py` — Import-only smoke tests

## Rules

- **No lint/typecheck/format tooling configured.** No pyproject.toml, Makefile, tox.ini, or CI pipeline.
- **No editable install.** Use `pip install -r requirements.txt` or run directly.
- **CONFIG_VERSION in `core/state.py` must be bumped** with every visible change (user rule).
- **Version bump policy**: Agent determines importance — PATCH=bugfix, MINOR=feature, MAJOR=breaking.
- **Privileged Docker required** for hardware access (`/sys/class/hwmon`, `smartctl`, Docker socket).
- **`/repo` volume mount** — entrypoint syncs `/repo` → `/app` on container start. In-app updates use `git pull` + `docker restart`.
- **Global mutable state** — `core/state.py` holds a module-level `state` dict. Always acquire `state_lock` before read/write. `get_state()` returns a 2s-cached snapshot.
- **Circular imports avoided** via late imports (e.g., `from app import socketio` inside functions) and re-export shims (`core/sensors.py` wraps `core/hardware.py`).
- **No `importlib.reload()` on core modules** — breaks config detection (`is_setup_needed()` returns True even when config exists).
- **Browser cache busting** — static assets in `index.html` use `?v=VERSION` suffix. Must bump both `CONFIG_VERSION` and the `?v=` value together.

## Environment variables

- `FANCONTROL_DATA_DIR` — Config/data directory (default: `/data`)
- `FANCONTROL_LOG_DIR` — Log directory (default: `{DATA_DIR}/logs`)
- `FANCONTROL_HWMON_DIR` — Hardware monitor path (default: `/sys/class/hwmon`)
- `FANCONTROL_CORS_ORIGINS` — Comma-separated CORS origins (default: `http://localhost:5059,...`)
- `MODE` — Run mode: `setup`, `server`, `agent`
- `CONTAINER_NAME` — Required in Docker (host network mode breaks `gethostname()`)

## Testing

```bash
pytest tests/                    # All import-based tests
pytest tests/test_integration.py::test_core_state_import  # Single test
```

Only import smoke tests exist. No unit tests, no integration tests with real hardware.

## Key gotchas

- `eventlet` in requirements.txt but SocketIO uses `async_mode='threading'`.
- `os._exit(0)` used in setup wizard (cannot `docker restart` from inside container — deadlock).
- `smartctl` subprocess calls require `smartmontools` installed in container.
- Fan IDs are SHA256 hashes of hardware paths (`dev-{hash[:12]}`) — stable across reboots.
- Config save is debounced (0.5s) — rapid changes may not persist immediately.
- Control loop runs in a daemon thread — non-blocking but must not crash silently.

# Multi-Node Agent Mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add agent mode to FanControl Web — same Docker image, `--mode agent` flag, WebSocket connection to server, autonomous local control.

**Architecture:** Split monolithic `app.py` into modules. Add agent entry point with WebSocket client. Agent runs full local dashboard + connects to server for centralized management.

**Tech Stack:** Python 3.10, Flask, Flask-SocketIO, python-socketio (client), gunicorn, eventlet

**Spec:** `docs/compose/specs/2026-06-19-multi-node-architecture-design.md`

---

## File Structure (after refactoring)

```
fancontrol-web/
├── app.py                     # Entry point (thin: mode selection + startup)
├── core/
│   ├── __init__.py
│   ├── state.py               # Global state dict, lock, get_state, _build_state_snapshot
│   ├── hardware.py            # discover_fans_and_sensors, discover_disks, set_pwm, refresh
│   ├── control.py             # loop, process_auto_mode, pwm_from_curve, fan_temp
│   ├── calibration.py         # test_fans, _detect_inversion, _normalize_curve
│   ├── config.py              # load_config, save_config, CONFIG_PATH
│   ├── schedule.py            # _evaluate_fan_mode, schedule evaluation logic
│   └── sensors.py             # read_disk_temp, parse_smart_temp, refresh_disks
├── server/
│   ├── __init__.py
│   ├── routes.py              # Flask routes (API endpoints)
│   └── socket_handlers.py     # Socket.IO event handlers
├── agent/
│   ├── __init__.py
│   ├── client.py              # WebSocket client (connects to server)
│   └── routes.py              # Agent-specific routes (mode switch, status)
├── templates/
│   ├── index.html             # Main dashboard (shared)
│   └── js/
│       └── main.js            # Frontend (shared, mode-aware)
├── tests/
│   ├── __init__.py
│   ├── test_state.py
│   ├── test_hardware.py
│   ├── test_control.py
│   ├── test_config.py
│   ├── test_calibration.py
│   └── test_agent_client.py
├── static/lang/
│   ├── en.json
│   └── ru.json
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## Task 1: Extract state module

**Covers:** [S2, S3]

**Files:**
- Create: `core/__init__.py`
- Create: `core/state.py`
- Modify: `app.py` (remove state code, import from core.state)

- [ ] **Step 1: Create core package**

```bash
mkdir -p core
touch core/__init__.py
```

- [ ] **Step 2: Create core/state.py**

Move from `app.py`:
- `state` dict definition (lines 88-104)
- `state_lock` (line 88)
- `STATE_CACHE_TTL`, `_cached_state`, `_cached_state_time` (lines 136-138)
- `_build_state_snapshot()` (line 144)
- `get_state()` (line 164)
- `_init_complete` event (line 1924)
- `CONFIG_VERSION` (line 84)

```python
"""Global state management — thread-safe state dict with caching."""

import copy
import threading
import time
from typing import Any, Dict, Optional

CONFIG_VERSION = "3.4.1"

state_lock = threading.RLock()

state: Dict[str, Any] = {
    'fans': {},
    'temp_sensors': {},
    'hdd_sensors': {},
    'max_hdd_temp': 0,
    'tested': False,
    'testing': False,
    '_pause_loop': False,
    'failsafe': False,
    'standby_mode': False,
    'initialized': False,
    'hardware_scanned': False,
    'test_progress': None,
    'test_complete': False,
    'language': 'en',
    '_gunicorn_initialized': False,
}

STATE_CACHE_TTL = 2.0
_cached_state: Optional[Dict[str, Any]] = None
_cached_state_time: float = 0.0

_init_complete = threading.Event()


def _build_state_snapshot() -> Dict[str, Any]:
    """Build a fresh state snapshot (caller must hold state_lock)."""
    return {
        'fans': copy.deepcopy(state['fans']),
        'temp_sensors': copy.deepcopy(state['temp_sensors']),
        'hdd_sensors': copy.deepcopy(state['hdd_sensors']),
        'max_hdd_temp': state['max_hdd_temp'],
        'tested': state['tested'],
        'testing': state['testing'],
        'failsafe': state['failsafe'],
        'standby_mode': state['standby_mode'],
        'initialized': state['initialized'],
        'hardware_scanned': state['hardware_scanned'],
        'test_progress': state['test_progress'],
        'test_complete': state['test_complete'],
        'config_version': CONFIG_VERSION,
        'language': state.get('language', 'en')
    }


def get_state() -> Dict[str, Any]:
    """Thread-safe snapshot of global state for API and Socket.IO."""
    global _cached_state, _cached_state_time
    now = time.monotonic()

    with state_lock:
        if _cached_state is not None and (now - _cached_state_time) < STATE_CACHE_TTL:
            return _cached_state

        _cached_state = _build_state_snapshot()
        _cached_state_time = now
        return _cached_state


def invalidate_state_cache():
    """Force next get_state() to rebuild snapshot."""
    global _cached_state
    _cached_state = None
```

- [ ] **Step 3: Update app.py imports**

Replace the state-related code in `app.py` with imports:

```python
from core.state import (
    state, state_lock, CONFIG_VERSION, get_state,
    invalidate_state_cache, _init_complete,
)
```

Remove the following from `app.py`:
- `state = { ... }` dict (lines 88-104)
- `state_lock` definition
- `CONFIG_VERSION` (line 84)
- `STATE_CACHE_TTL`, `_cached_state`, `_cached_state_time`
- `_build_state_snapshot()`
- `get_state()`
- `_init_complete`

- [ ] **Step 4: Verify app.py still works**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add core/__init__.py core/state.py app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "refactor: extract state management to core/state.py"
```

---

## Task 2: Extract hardware module

**Covers:** [S2, S7]

**Files:**
- Create: `core/hardware.py`
- Modify: `app.py`

- [ ] **Step 1: Create core/hardware.py**

Move from `app.py`:
- `FANCONTROL_HWMON_DIR` env var
- `generate_stable_id()` (line 565)
- `discover_fans_and_sensors()` (line 571-685)
- `is_physical_disk()` (line 691)
- `discover_disks()` (line 806-908)
- `set_pwm_raw()` (line 910-912)
- `set_pwm()` (line 914-948)
- `refresh()` (line 1208-1242)
- `CALIBRATION_STEPS`, `CALIBRATION_SETTLE_TIME`

```python
"""Hardware discovery and control — sysfs + smartctl."""

import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

from core.state import state, state_lock

logger = logging.getLogger('fancontrol')

FANCONTROL_HWMON_DIR = os.environ.get('FANCONTROL_HWMON_DIR', '/sys/class/hwmon')

CALIBRATION_STEPS = [0, 26, 51, 77, 102, 128, 153, 179, 204, 230, 255]
CALIBRATION_SETTLE_TIME = 5


def generate_stable_id(hwmon_name: str, pwm_file: str) -> str:
    raw = f"{hwmon_name}/{pwm_file}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def discover_fans_and_sensors() -> Tuple[Dict, Dict]:
    """... (full implementation from app.py lines 571-685) ..."""
    # [Move exact code here]


def is_physical_disk(name: str) -> bool:
    """... (full implementation from app.py line 691) ..."""
    # [Move exact code here]


def discover_disks() -> Dict:
    """... (full implementation from app.py lines 806-908) ..."""
    # [Move exact code here]


def set_pwm_raw(pwm_path: str, value: int):
    """... (full implementation from app.py line 910) ..."""
    # [Move exact code here]


def set_pwm(fan: dict, value: int, raw: bool = False):
    """... (full implementation from app.py lines 914-948) ..."""
    # [Move exact code here]


def refresh():
    """... (full implementation from app.py lines 1208-1242) ..."""
    # [Move exact code here]
```

- [ ] **Step 2: Update app.py imports**

```python
from core.hardware import (
    discover_fans_and_sensors, discover_disks, set_pwm, set_pwm_raw,
    refresh, CALIBRATION_STEPS, CALIBRATION_SETTLE_TIME,
    FANCONTROL_HWMON_DIR,
)
```

Remove moved functions from `app.py`.

- [ ] **Step 3: Verify**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add core/hardware.py app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "refactor: extract hardware discovery/control to core/hardware.py"
```

---

## Task 3: Extract control loop module

**Covers:** [S2, S7]

**Files:**
- Create: `core/control.py`
- Modify: `app.py`

- [ ] **Step 1: Create core/control.py**

Move from `app.py`:
- `MIN_PWM_PCT`, `MAX_PWM_PCT`, `SENSOR_FAILURE_TEMP`
- `CONTROL_LOOP_INTERVAL`, `TELEMETRY_LOG_INTERVAL`, `DISK_POLL_COOLDOWN`
- `fan_temp()` (line 1323-1366)
- `pwm_from_curve()` (line 1369-1396)
- `process_auto_mode()` (line 1398-1453)
- `_evaluate_fan_mode()` (line 1456-1525)
- `loop()` (line 1527-1620)

Note: `loop()` needs `socketio.emit` — pass as parameter or use late import.

```python
"""Fan control loop and mode evaluation."""

import logging
import time
from typing import Tuple

from core.state import state, state_lock, get_state

logger = logging.getLogger('fancontrol')

MIN_PWM_PCT = 20
MAX_PWM_PCT = 100
SENSOR_FAILURE_TEMP = 99
CONTROL_LOOP_INTERVAL = 5
TELEMETRY_LOG_INTERVAL = 300
DISK_POLL_COOLDOWN = 30


def fan_temp(fan: dict) -> float:
    """... (full implementation from app.py lines 1323-1366) ..."""
    # [Move exact code here]


def pwm_from_curve(fan: dict, target_pct: float) -> int:
    """... (full implementation from app.py lines 1369-1396) ..."""
    # [Move exact code here]


def process_auto_mode(fan: dict, current_temp: float) -> Tuple[float, str]:
    """... (full implementation from app.py lines 1398-1453) ..."""
    # [Move exact code here]


def _evaluate_fan_mode(fan: dict) -> Tuple[float, str]:
    """... (full implementation from app.py lines 1456-1525) ..."""
    # [Move exact code here]


def loop(socketio=None):
    """Main control loop. socketio needed for emit."""
    # [Move exact code from app.py lines 1527-1620]
    # Replace direct socketio references with parameter
```

- [ ] **Step 2: Update app.py**

```python
from core.control import loop, process_auto_mode, pwm_from_curve, fan_temp
```

Remove moved functions. Update `_ensure_control_loop()` to pass `socketio`:

```python
def _ensure_control_loop():
    global _control_loop_started
    if not _control_loop_started:
        _control_loop_started = True
        threading.Thread(target=loop, args=(socketio,), daemon=True).start()
```

- [ ] **Step 3: Verify**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add core/control.py app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "refactor: extract control loop to core/control.py"
```

---

## Task 4: Extract calibration module

**Covers:** [S2, S7]

**Files:**
- Create: `core/calibration.py`
- Modify: `app.py`

- [ ] **Step 1: Create core/calibration.py**

Move from `app.py`:
- `_detect_inversion()` (line 953-988)
- `_normalize_curve()` (line 990-1002)
- `test_fans()` (line 1004-1202)

```python
"""Fan calibration — detection, inversion, curve normalization."""

import logging
import time
from typing import Dict, Tuple

from core.state import state, state_lock
from core.hardware import set_pwm_raw, CALIBRATION_STEPS, CALIBRATION_SETTLE_TIME

logger = logging.getLogger('fancontrol')


def _detect_inversion(results: Dict) -> bool:
    """... (full implementation from app.py lines 953-988) ..."""
    # [Move exact code here]


def _normalize_curve(curve: list, inverted: bool) -> list:
    """... (full implementation from app.py lines 990-1002) ..."""
    # [Move exact code here]


def test_fans(socketio=None):
    """Full calibration. socketio needed for emit."""
    # [Move exact code from app.py lines 1004-1202]
    # Replace direct socketio references with parameter
```

- [ ] **Step 2: Update app.py**

```python
from core.calibration import test_fans
```

Remove moved functions. Update any calls to `test_fans()` to pass `socketio`.

- [ ] **Step 3: Verify**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add core/calibration.py app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "refactor: extract calibration to core/calibration.py"
```

---

## Task 5: Extract config and sensors modules

**Covers:** [S2, S7]

**Files:**
- Create: `core/config.py`
- Create: `core/sensors.py`
- Modify: `app.py`

- [ ] **Step 1: Create core/config.py**

Move from `app.py`:
- `DATA_DIR`, `CONFIG_PATH`, `DB_PATH`
- `save_config()` (with debounce)
- `load_config()`

```python
"""Configuration persistence — JSON config + save debounce."""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from core.state import state, state_lock

logger = logging.getLogger('fancontrol')

DATA_DIR = Path(os.environ.get('FANCONTROL_DATA_DIR', '/app/data'))
CONFIG_PATH = DATA_DIR / 'config.json'
DB_PATH = DATA_DIR / 'fancontrol.db'

_save_timer: Optional[threading.Timer] = None
SAVE_DEBOUNCE = 0.5


def save_config(immediate: bool = False):
    """... (full implementation from app.py) ..."""
    # [Move exact code here]


def load_config():
    """... (full implementation from app.py) ..."""
    # [Move exact code here]
```

- [ ] **Step 2: Create core/sensors.py**

Move from `app.py`:
- `read_disk_temp()` (line 757-803)
- `parse_smart_temp()` (line 730-755)
- `refresh_disks()` (line 1244-1321)

```python
"""Disk temperature reading — smartctl + sysfs fallback."""

import logging
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Dict

from core.state import state, state_lock

logger = logging.getLogger('fancontrol')


def parse_smart_temp(output: str) -> int:
    """... (full implementation from app.py lines 730-755) ..."""
    # [Move exact code here]


def read_disk_temp(device: str) -> int:
    """... (full implementation from app.py lines 757-803) ..."""
    # [Move exact code here]


def refresh_disks():
    """... (full implementation from app.py lines 1244-1321) ..."""
    # [Move exact code here]
```

- [ ] **Step 3: Update app.py**

```python
from core.config import save_config, load_config, DATA_DIR, CONFIG_PATH, DB_PATH
from core.sensors import read_disk_temp, refresh_disks
```

Remove moved functions from `app.py`.

- [ ] **Step 4: Verify**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add core/config.py core/sensors.py app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "refactor: extract config and sensors to core modules"
```

---

## Task 6: Extract server routes

**Covers:** [S2, S7]

**Files:**
- Create: `server/__init__.py`
- Create: `server/routes.py`
- Create: `server/socket_handlers.py`
- Modify: `app.py`

- [ ] **Step 1: Create server package**

```bash
mkdir -p server
touch server/__init__.py
```

- [ ] **Step 2: Create server/routes.py**

Move all Flask `@app.route` handlers from `app.py`:
- `index()` (line 184)
- `serve_js()` (line 190)
- `api_get_state()` (line 196)
- `api_get_lang()` (line 202)
- `api_set_language()` (line 212)
- `api_discover()` (line 231)
- `api_initialize()` (line 259)
- `api_test_single()` (line 283)
- `api_history()` (line 310)
- `api_update_check()` (line 345)
- `api_update_apply()` (line 417)
- `api_control()` (line 475)

```python
"""Flask routes — REST API endpoints."""

from flask import Blueprint, jsonify, render_template, request, send_from_directory
# ... imports ...

routes = Blueprint('routes', __name__)


@routes.route('/')
def index():
    return render_template('index.html')


@routes.route('/js/<path:filename>')
def serve_js(filename):
    # ...

# ... all other routes ...
```

- [ ] **Step 3: Create server/socket_handlers.py**

Move Socket.IO event handlers:
- `handle_socket_connect()` (line 1863)
- `handle_get_state()` (line 1872)

```python
"""Socket.IO event handlers."""

def register_handlers(socketio):
    @socketio.on('connect')
    def handle_socket_connect():
        _init_complete.wait(timeout=15)
        socketio.emit('update', get_state())

    @socketio.on('get_state')
    def handle_get_state():
        socketio.emit('update', get_state())
```

- [ ] **Step 4: Update app.py**

```python
from server.routes import routes
from server.socket_handlers import register_handlers

app.register_blueprint(routes)
register_handlers(socketio)
```

Remove all route and socket handler code from `app.py`.

- [ ] **Step 5: Verify**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add server/ app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "refactor: extract server routes and socket handlers"
```

---

## Task 7: Create agent module — WebSocket client

**Covers:** [S2, S4, S5, S7]

**Files:**
- Create: `agent/__init__.py`
- Create: `agent/client.py`
- Modify: `app.py` (add mode selection)

- [ ] **Step 1: Create agent package**

```bash
mkdir -p agent
touch agent/__init__.py
```

- [ ] **Step 2: Create agent/client.py**

```python
"""WebSocket client — connects agent to server."""

import logging
import os
import threading
import time
from typing import Optional

import socketio

from core.state import state, state_lock, get_state, invalidate_state_cache

logger = logging.getLogger('fancontrol')

SERVER_URL = os.environ.get('SERVER_URL', '')
API_TOKEN = os.environ.get('API_TOKEN', '')
NODE_ID = os.environ.get('NODE_ID', 'agent-1')
NODE_NAME = os.environ.get('NODE_NAME', 'Agent 1')
TELEMETRY_INTERVAL = int(os.environ.get('TELEMETRY_INTERVAL', '5'))

# Agent states
state['control_mode'] = 'server'  # 'server' or 'manual'
state['server_connected'] = False
state['server_url'] = SERVER_URL

_sio: Optional[socketio.Client] = None
_telemetry_thread: Optional[threading.Thread] = None


def _on_connect():
    logger.info(f'Connected to server: {SERVER_URL}')
    state['server_connected'] = True
    invalidate_state_cache()

    # Send agent info + local config
    _sio.emit('agent:connect', {
        'node_id': NODE_ID,
        'node_name': NODE_NAME,
        'api_token': API_TOKEN,
        'control_mode': state['control_mode'],
        'config': _get_local_config(),
    })


def _on_disconnect():
    logger.warning('Disconnected from server')
    state['server_connected'] = False
    invalidate_state_cache()


def _on_config_push(data):
    """Server pushes new config — apply if in server mode."""
    with state_lock:
        if state['control_mode'] != 'server':
            logger.info('Config push ignored — in manual mode')
            return

        # Save agent's current config for revert
        state['agent_config_snapshot'] = _get_local_config()

        # Apply server config
        _apply_config(data.get('config', {}))
        invalidate_state_cache()
        logger.info('Applied server config')


def _on_set_control_mode(data):
    """Server requests mode change."""
    mode = data.get('mode', 'server')
    with state_lock:
        state['control_mode'] = mode
        invalidate_state_cache()
    logger.info(f'Control mode set to: {mode}')


def _on_command(data):
    """Server sends a command (set_fan, etc.)."""
    cmd = data.get('command')
    if cmd == 'set_fan':
        fan_id = data.get('fan_id')
        value = data.get('value')
        with state_lock:
            if fan_id in state['fans']:
                state['fans'][fan_id]['manual_pct'] = value
                state['fans'][fan_id]['mode'] = 'manual'
        invalidate_state_cache()


def _telemetry_loop():
    """Send telemetry to server periodically."""
    while True:
        time.sleep(TELEMETRY_INTERVAL)
        if _sio and state['server_connected']:
            try:
                _sio.emit('agent:telemetry', {
                    'node_id': NODE_ID,
                    'telemetry': _get_telemetry(),
                })
            except Exception as e:
                logger.error(f'Telemetry send failed: {e}')


def _get_local_config():
    """Get current local fan config."""
    with state_lock:
        return {
            'fans': {k: {kk: vv for kk, vv in v.items()
                         if kk not in ('rpm', 'pwm_value')}
                     for k, v in state['fans'].items()},
            'temp_sensors': state['temp_sensors'],
            'hdd_sensors': state['hdd_sensors'],
        }


def _get_telemetry():
    """Get current telemetry data."""
    with state_lock:
        return {
            'fans': {k: {'rpm': v.get('rpm', 0),
                         'pwm_value': v.get('pwm_value', 0)}
                     for k, v in state['fans'].items()},
            'temp_sensors': {k: {'value': v.get('value', 0)}
                            for k, v in state['temp_sensors'].items()},
            'hdd_sensors': {k: {'temp': v.get('temp', 0)}
                           for k, v in state['hdd_sensors'].items()},
            'failsafe': state.get('failsafe', False),
            'standby_mode': state.get('standby_mode', False),
        }


def _apply_config(config):
    """Apply config received from server."""
    with state_lock:
        for fan_id, fan_cfg in config.get('fans', {}).items():
            if fan_id in state['fans']:
                for key in ('mode', 'target_temp', 'manual_pct', 'sensors',
                            'sensor_mode', 'schedule', 'inverted'):
                    if key in fan_cfg:
                        state['fans'][fan_id][key] = fan_cfg[key]


def start_client():
    """Start the WebSocket client connection to server."""
    global _sio, _telemetry_thread

    if not SERVER_URL:
        logger.info('No SERVER_URL set — running standalone')
        return

    _sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,  # infinite
        reconnection_delay=1,
        reconnection_delay_max=30,
    )

    _sio.on('connect', _on_connect)
    _sio.on('disconnect', _on_disconnect)
    _sio.on('server:config_push', _on_config_push)
    _sio.on('server:set_control_mode', _on_set_control_mode)
    _sio.on('server:command', _on_command)

    try:
        _sio.connect(SERVER_URL)
    except Exception as e:
        logger.error(f'Failed to connect to server: {e}')

    _telemetry_thread = threading.Thread(target=_telemetry_loop, daemon=True)
    _telemetry_thread.start()
```

- [ ] **Step 3: Update app.py — add mode selection**

Replace the entry point section:

```python
import argparse

def main():
    parser = argparse.ArgumentParser(description='FanControl Web')
    parser.add_argument('--mode', choices=['server', 'agent'],
                       default=os.environ.get('MODE', 'server'),
                       help='Run mode: server (default) or agent')
    args = parser.parse_args()

    if args.mode == 'agent':
        from agent.client import start_client
        init_database()
        init_hardware()
        _init_complete.set()
        _ensure_control_loop()
        start_client()
    else:
        init_database()
        init_hardware()
        _init_complete.set()
        _ensure_control_loop()

    logger.info(f'Starting FanControl Web {CONFIG_VERSION} in {args.mode} mode')
    socketio.run(app, host='0.0.0.0', port=5059)


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: Add python-socketio client to requirements.txt**

Append to `requirements.txt`:
```
python-socketio[client]
```

- [ ] **Step 5: Verify**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add agent/ app.py requirements.txt
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add agent mode with WebSocket client"
```

---

## Task 8: Agent mode in Docker

**Covers:** [S7]

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Update Dockerfile CMD**

The Dockerfile CMD should remain the same (gunicorn). Mode selection is via `MODE` env var.

No changes needed — the `main()` function in `app.py` reads `MODE` env var.

- [ ] **Step 2: Create docker-compose.agent.yml**

```yaml
services:
  fancontrol-agent:
    image: fancontrol-web
    container_name: fancontrol-agent
    restart: unless-stopped
    network_mode: host
    privileged: true
    cap_add:
      - SYS_RAWIO
      - SYS_ADMIN
    volumes:
      - /sys:/sys:rw
      - /dev:/dev:rw
      - ./data-agent:/app/data
    environment:
      - MODE=agent
      - SERVER_URL=ws://192.168.1.100:5059
      - API_TOKEN=your-token-here
      - NODE_ID=server1
      - NODE_NAME=Server 1
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.agent.yml Dockerfile
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add agent Docker Compose config"
```

---

## Task 9: Agent routes — mode switch + status

**Covers:** [S2, S7]

**Files:**
- Create: `agent/routes.py`
- Modify: `app.py`

- [ ] **Step 1: Create agent/routes.py**

```python
"""Agent-specific routes — mode switch, status, config revert."""

from flask import Blueprint, jsonify, request

from core.state import state, state_lock, get_state, invalidate_state_cache
from core.config import save_config

agent_routes = Blueprint('agent_routes', __name__)


@agent_routes.route('/api/agent/status')
def agent_status():
    """Get agent status including server connection."""
    return jsonify({
        'control_mode': state.get('control_mode', 'server'),
        'server_connected': state.get('server_connected', False),
        'server_url': state.get('server_url', ''),
        'node_id': state.get('node_id', ''),
        'has_agent_snapshot': state.get('agent_config_snapshot') is not None,
    })


@agent_routes.route('/api/agent/mode', methods=['POST'])
def set_control_mode():
    """Switch between server and manual control."""
    data = request.get_json()
    mode = data.get('mode', 'server')

    if mode not in ('server', 'manual'):
        return jsonify({'error': 'Invalid mode'}), 400

    with state_lock:
        old_mode = state.get('control_mode', 'server')
        state['control_mode'] = mode
        invalidate_state_cache()

    # Notify server of mode change
    from agent.client import _sio
    if _sio and state.get('server_connected'):
        _sio.emit('agent:control_mode_changed', {
            'node_id': state.get('node_id'),
            'mode': mode,
        })

    save_config()
    return jsonify({'mode': mode, 'previous': old_mode})


@agent_routes.route('/api/agent/revert', methods=['POST'])
def revert_to_agent_config():
    """Revert to agent's local config (from snapshot)."""
    with state_lock:
        snapshot = state.get('agent_config_snapshot')
        if not snapshot:
            return jsonify({'error': 'No snapshot available'}), 400

        # Apply snapshot to current state
        for fan_id, fan_cfg in snapshot.get('fans', {}).items():
            if fan_id in state['fans']:
                for key, val in fan_cfg.items():
                    state['fans'][fan_id][key] = val

        state['agent_config_snapshot'] = None
        invalidate_state_cache()

    save_config()
    return jsonify({'status': 'reverted'})
```

- [ ] **Step 2: Update app.py — register agent routes**

```python
from agent.routes import agent_routes

app.register_blueprint(agent_routes)
```

- [ ] **Step 3: Verify**

Run: `python3 -c "from app import app; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add agent/routes.py app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add agent routes for mode switch and config revert"
```

---

## Task 10: Integration test — verify refactoring

**Covers:** All

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Create tests/test_integration.py**

```python
"""Integration tests — verify all modules import and work together."""

import os
import sys
import pytest

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_core_state_import():
    from core.state import state, get_state, CONFIG_VERSION
    assert CONFIG_VERSION == "3.4.1"
    s = get_state()
    assert 'fans' in s
    assert 'initialized' in s


def test_core_hardware_import():
    from core.hardware import (
        discover_fans_and_sensors, discover_disks,
        set_pwm, refresh, CALIBRATION_STEPS
    )
    assert len(CALIBRATION_STEPS) == 11


def test_core_control_import():
    from core.control import loop, process_auto_mode, pwm_from_curve
    assert callable(loop)
    assert callable(process_auto_mode)


def test_core_calibration_import():
    from core.calibration import test_fans, _detect_inversion
    assert callable(test_fans)


def test_core_config_import():
    from core.config import save_config, load_config, CONFIG_PATH
    assert callable(save_config)


def test_core_sensors_import():
    from core.sensors import read_disk_temp, refresh_disks
    assert callable(read_disk_temp)


def test_server_routes_import():
    from server.routes import routes
    assert routes is not None


def test_agent_client_import():
    from agent.client import start_client, _telemetry_loop
    assert callable(start_client)


def test_agent_routes_import():
    from agent.routes import agent_routes
    assert agent_routes is not None


def test_app_import():
    from app import app
    assert app is not None
    assert app.name == 'app'
```

- [ ] **Step 2: Run tests**

Run: `cd /home/impulse/fancontrol-web && python3 -m pytest tests/test_integration.py -v`
Expected: All 10 tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "test: add integration tests for module imports"
```

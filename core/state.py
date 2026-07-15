"""Global state management — thread-safe state dict with caching."""

import secrets
import threading
import time
from typing import Any, Dict, Optional

CONFIG_VERSION = "3.14.9"

# Auto-generated update token if FANCONTROL_UPDATE_TOKEN is not set
# Import cfg lazily to avoid circular imports
_auto_update_token = None

def _ensure_update_token():
    global _auto_update_token
    if _auto_update_token is None:
        from core.config import cfg
        _auto_update_token = cfg.update_token or secrets.token_urlsafe(32)
    return _auto_update_token

state_lock = threading.RLock()

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
    'server_name': 'FanControl Server',
    'nodes': {},  # Runtime state for connected agents
    'dashboard': {'groups': [], 'cards': [], 'hiddenSensors': []},
    'smart_monitored_disks': set(),  # runtime-only; persisted via card.monitoring in config.json
    'auto_register_agents': True,  # auto-register unknown agents on first telemetry
}

STATE_CACHE_TTL = 2.0
_cached_state: Optional[Dict[str, Any]] = None
_cached_state_time: float = 0.0

_init_complete = threading.Event()


def _build_state_snapshot() -> Dict[str, Any]:
    """Build a fresh state snapshot (caller must hold state_lock).
    
    Uses shallow copy for dicts — fan/sensor/disk dicts are flat
    except for the nested 'health' dict in fans, which is copied separately.
    """
    fans_snap = {}
    for k, v in state['fans'].items():
        fan_copy = v.copy()
        if 'health' in v:
            fan_copy['health'] = v['health'].copy()
        fans_snap[k] = fan_copy
    return {
        'fans': fans_snap,
        'temp_sensors': {k: v.copy() for k, v in state['temp_sensors'].items()},
        'hdd_sensors': {k: v.copy() for k, v in state['hdd_sensors'].items()},
        'max_hdd_temp': state.get('max_hdd_temp', 0),
        'tested': state.get('tested', False),
        'testing': state.get('testing', False),
        'test_progress': (state.get('test_progress') or {}).copy(),
        '_pause_loop': state.get('_pause_loop', False),
        'failsafe': state.get('failsafe', False),
        'standby_mode': state.get('standby_mode', False),
        'initialized': state.get('initialized', False),
        'hardware_scanned': state.get('hardware_scanned', False),
        'config_version': CONFIG_VERSION,
        'language': state.get('language', 'en'),
        'server_name': state.get('server_name', 'FanControl Server'),
        'nodes': {k: v.copy() for k, v in state.get('nodes', {}).items()},
        'agent_mode': state.get('server_url') is not None,
        'api_token': state.get('api_token', ''),
        'auto_register_agents': state.get('auto_register_agents', True),
        'dashboard': state.get('dashboard', {'groups': [], 'cards': [], 'hiddenSensors': []}),
    }


def get_state() -> Dict[str, Any]:
    """Thread-safe snapshot of global state for API and Socket.IO."""
    global _cached_state, _cached_state_time
    now = time.monotonic()

    with state_lock:
        if _cached_state is not None and (now - _cached_state_time) < STATE_CACHE_TTL:
            return dict(_cached_state)

        _cached_state = _build_state_snapshot()
        _cached_state_time = now
        return dict(_cached_state)


def invalidate_state_cache():
    """Force next get_state() to rebuild snapshot."""
    global _cached_state
    _cached_state = None

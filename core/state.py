"""Global state management — thread-safe state dict with caching."""

import copy
import threading
import time
from typing import Any, Dict, Optional

CONFIG_VERSION = "3.9.6"

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
    'nodes': {},  # Runtime state for connected agents
    'dashboard': {'groups': [], 'cards': [], 'hiddenSensors': []},
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
        'max_hdd_temp': state.get('max_hdd_temp', 0),
        'tested': state.get('tested', False),
        'testing': state.get('testing', False),
        'test_progress': copy.deepcopy(state.get('test_progress', {})),
        '_pause_loop': state.get('_pause_loop', False),
        'failsafe': state.get('failsafe', False),
        'standby_mode': state.get('standby_mode', False),
        'initialized': state.get('initialized', False),
        'hardware_scanned': state.get('hardware_scanned', False),
        'config_version': CONFIG_VERSION,
        'language': state.get('language', 'en'),
        'nodes': dict(state.get('nodes', {})),
        'agent_mode': state.get('server_url') is not None,
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

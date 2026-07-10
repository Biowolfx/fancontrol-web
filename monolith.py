#!/usr/bin/env python3
"""
FanControl Web - Monolith (single-file version)
All Python modules, HTML template, JS, and lang files merged into one file.

CONFIG_VERSION: 3.12.87
Auto-generated build - do not edit manually.
"""

import copy
import fcntl
import glob as _glob
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import (Flask, Blueprint, jsonify, make_response, render_template,
                   request, send_from_directory)
from flask_socketio import SocketIO
from werkzeug.exceptions import BadRequest

try:
    import socketio as _sio_client
    HAS_SIO_CLIENT = True
except ImportError:
    HAS_SIO_CLIENT = False

# ==============================================================================
# MODULE: core.state
# ==============================================================================

"""Global state management — thread-safe state dict with caching."""

import secrets
import threading
import time
from typing import Any, Dict, Optional

CONFIG_VERSION = "3.12.92"

# Auto-generated update token if FANCONTROL_UPDATE_TOKEN is not set
# Import cfg lazily to avoid circular imports
_auto_update_token = None

def _ensure_update_token():
    global _auto_update_token
    if _auto_update_token is None:
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


# ==============================================================================
# MODULE: core.kernel_detect
# ==============================================================================

"""Detect kernel type: official Synology vs custom ARC loader."""

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger('fancontrol')

KERNEL_UNKNOWN = 'unknown'
KERNEL_OFFICIAL = 'official'
KERNEL_CUSTOM = 'custom'


def detect_kernel_type():
    """
    Detect whether running on official Synology kernel or custom ARC kernel.
    Returns one of: 'official', 'custom', 'unknown'
    """
    # Method 1: Check /proc/version for ARC or custom kernel markers
    try:
        with open('/proc/version', 'r') as f:
            version_str = f.read().strip()
        logger.info(f'[kernel] /proc/version: {version_str}')

        # Custom ARC kernels often contain specific markers
        if any(marker in version_str.lower() for marker in ['arc', 'junior', 'arpl', 'rr']):
            logger.info('[kernel] Detected custom kernel via /proc/version markers')
            return KERNEL_CUSTOM
    except Exception as e:
        logger.warning(f'[kernel] Cannot read /proc/version: {e}')

    # Method 2: Check if Synology proprietary fan modules exist
    try:
        result = subprocess.run(
            ['lsmod'], capture_output=True, text=True, timeout=5
        )
        modules = result.stdout.lower()
        # syno_hddtemp or syno_fan indicate official kernel with Synology drivers
        if 'syno_fan' in modules or 'syno_hddtemp' in modules:
            logger.info('[kernel] Detected official kernel via Synology modules')
            return KERNEL_OFFICIAL
    except Exception:
        pass

    # Method 3: Check /sys/module for Synology-specific modules
    syno_modules = list(Path('/sys/module').glob('syno_*'))
    if syno_modules:
        logger.info(f'[kernel] Detected official kernel via /sys/module/syno_*: {[m.name for m in syno_modules]}')
        return KERNEL_OFFICIAL

    # Method 4: Check for hwmon pwm* files (custom kernel usually has them)
    try:
        hwmon_dir = Path('/sys/class/hwmon')
        for hw in hwmon_dir.iterdir():
            if list(hw.glob('pwm*')):
                logger.info('[kernel] Detected custom kernel via hwmon pwm* files')
                return KERNEL_CUSTOM
    except Exception:
        pass

    # Method 5: Check DSM version + architecture
    try:
        result = subprocess.run(
            ['uname', '-r'], capture_output=True, text=True, timeout=5
        )
        kernel_release = result.stdout.strip()
        logger.info(f'[kernel] uname -r: {kernel_release}')

        # Official Synology kernels have specific version patterns
        # e.g., 4.4.302+ (DSM 7.1) or 4.4.180+ (DSM 6.2)
        # Custom kernels may differ
        if '+' in kernel_release:
            # Could be either — need more signals
            pass
    except Exception:
        pass

    # Method 6: Check if scemd.xml exists (always present on official DSM)
    scemd_exists = Path('/usr/syno/etc.defaults/scemd.xml').exists()
    pwm_exists = any(Path('/sys/class/hwmon').glob('*/pwm*')) if Path('/sys/class/hwmon').exists() else False

    if scemd_exists and not pwm_exists:
        logger.info('[kernel] Detected official kernel: scemd.xml present, no hwmon pwm*')
        return KERNEL_OFFICIAL
    elif pwm_exists:
        logger.info('[kernel] Detected custom kernel: hwmon pwm* files present')
        return KERNEL_CUSTOM
    elif scemd_exists:
        logger.info('[kernel] Detected official kernel: scemd.xml present')
        return KERNEL_OFFICIAL

    logger.warning('[kernel] Could not determine kernel type, assuming unknown')
    return KERNEL_UNKNOWN


def get_kernel_info():
    """Get detailed kernel information."""
    info = {
        'type': detect_kernel_type(),
        'version': '',
        'has_hwmon_pwm': False,
        'has_scemd': False,
        'has_ipmi': False,
        'syno_modules': [],
    }

    try:
        result = subprocess.run(['uname', '-a'], capture_output=True, text=True, timeout=5)
        info['version'] = result.stdout.strip()
    except Exception:
        pass

    info['has_hwmon_pwm'] = bool(list(Path('/sys/class/hwmon').glob('*/pwm*'))) if Path('/sys/class/hwmon').exists() else False
    info['has_scemd'] = Path('/usr/syno/etc.defaults/scemd.xml').exists()
    info['has_ipmi'] = bool(list(Path('/dev').glob('ipmi*')))

    info['syno_modules'] = [m.name for m in Path('/sys/module').glob('syno_*')] if Path('/sys/module').exists() else []

    return info


# ==============================================================================
# MODULE: core.config
# ==============================================================================

"""Configuration persistence — JSON config load/save with debounced writes."""

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


logger = logging.getLogger('fancontrol')


# ============================================================================
# Centralized environment configuration
# ============================================================================

@dataclass(frozen=True)
class Config:
    """Single source of truth for all environment variables.

    Read once at import time. Immutable — no runtime mutation.
    For tests: Config(**overrides) or monkeypatch the module-level `cfg`.
    """
    # Paths
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('FANCONTROL_DATA_DIR', '/data')))
    hwmon_dir: Path = field(default_factory=lambda: Path(os.getenv('FANCONTROL_HWMON_DIR', '/sys/class/hwmon')))
    log_dir: str = field(default_factory=lambda: os.getenv('FANCONTROL_LOG_DIR', ''))

    # App
    mode: str = field(default_factory=lambda: os.getenv('MODE', 'server'))
    cors_origins: List[str] = field(default_factory=lambda: os.getenv('FANCONTROL_CORS_ORIGINS', '*').split(','))

    # Agent connection
    server_url: str = field(default_factory=lambda: os.getenv('SERVER_URL', ''))
    api_token: str = field(default_factory=lambda: os.getenv('API_TOKEN', ''))
    node_id: str = field(default_factory=lambda: os.getenv('NODE_ID', 'agent-1'))
    node_name: str = field(default_factory=lambda: os.getenv('NODE_NAME', 'Agent 1'))
    telemetry_interval: int = field(default_factory=lambda: int(os.getenv('TELEMETRY_INTERVAL', '5')))

    # Server
    update_token: str = field(default_factory=lambda: os.getenv('FANCONTROL_UPDATE_TOKEN', ''))

    def __post_init__(self):
        # Resolve log_dir default after data_dir is known
        if not self.log_dir:
            object.__setattr__(self, 'log_dir', str(self.data_dir / 'logs'))
        # Normalize cors_origins: accept "a,b" string from constructor
        if isinstance(self.cors_origins, str):
            object.__setattr__(self, 'cors_origins', self.cors_origins.split(','))


cfg = Config()

DATA_DIR = cfg.data_dir
CONFIG_PATH = DATA_DIR / 'config.json'
DB_FILE = DATA_DIR / 'fancontrol.db'

FAN_FIELDS = [
    'id', 'label', 'hw_path', 'pwm_path', 'fan_path',
    'inverted', 'min_rpm', 'max_rpm', 'manual_pct',
    'sensors', 'sensor_mode', 'target_temp', 'mode',
    'status', 'target_pwm', 'current_pct',
    'schedule', 'curve', 'calibration', 'health'
]

SAVE_DEBOUNCE_SECONDS = 0.5
_save_timer: Optional[threading.Timer] = None
_save_lock = threading.Lock()

# Cache of config.json content to avoid re-reading disk on every debounced save.
# Invalidated on write and when wizard modifies the file externally.
_cached_config_json: Optional[Dict] = None
_cached_config_mtime: float = 0.0


def migrate_config(cfg: Dict) -> Dict:
    """Migrate old config format to v3.0"""
    if 'config_version' not in cfg:
        logger.info('Migrating config to v3.0 format...')
        cfg['config_version'] = CONFIG_VERSION

        fans = cfg.get('fans', {})
        for fan_id, fan in fans.items():
            if 'fan_mode' in fan:
                fan['mode'] = fan.pop('fan_mode')
            fan.setdefault('mode', 'manual')
            fan.setdefault('id', fan_id)
            fan.setdefault('status', 'nominal')
            fan.setdefault('target_pwm', fan.get('manual_pct', 50))
            fan.setdefault('hw_path', '')

            schedule = fan.get('schedule', [])
            for item in schedule:
                if 'fan_mode' in item:
                    item['mode'] = item.pop('fan_mode')
                item.setdefault('mode', 'auto')

    return cfg


def _do_save_config():
    """Actually write config to disk, preserving all existing fields.
    
    Caches the on-disk config.json to avoid re-reading it on every
    debounced save. Cache invalidated on write; external modifications
    (by wizard) detected via mtime check.
    """
    global _cached_config_json, _cached_config_mtime
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Read existing config (cached if unchanged on disk)
        existing = {}
        try:
            current_mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
        except OSError:
            current_mtime = 0.0

        if _cached_config_json is not None and abs(current_mtime - _cached_config_mtime) < 0.01:
            existing = _cached_config_json.copy()
        elif CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                _cached_config_json = existing.copy()
                _cached_config_mtime = current_mtime
            except Exception as e:
                logger.error(f'Failed to read config.json: {e}')
                # Use cached config if available, otherwise abort to prevent data loss
                if _cached_config_json is not None:
                    existing = _cached_config_json.copy()
                else:
                    logger.error('No cached config available — aborting save to prevent data loss')
                    return

        existing['config_version'] = CONFIG_VERSION
        existing['initialized'] = state.get('initialized', False)
        existing['tested'] = state.get('tested', False)
        existing['language'] = state.get('language', 'en')
        existing['server_name'] = state.get('server_name', 'FanControl Server')
        existing['log_level'] = state.get('log_level', 'INFO')
        existing['log_retention_days'] = state.get('log_retention_days', 30)
        existing['telegram_bot_token'] = state.get('telegram_bot_token', '')
        existing['telegram_chat_id'] = state.get('telegram_chat_id', '')
        existing['telegram_enabled'] = state.get('telegram_enabled', False)
        existing['telegram_events'] = state.get('telegram_events', {
            'fan_health': True, 'agent_status': True,
            'updates': True, 'temperature': True,
        })

        fans_data = {}
        with state_lock:
            for fan_id, fan in state.get('fans', {}).items():
                fans_data[fan_id] = {
                    field: fan.get(field)
                    for field in FAN_FIELDS
                    if field in fan
                }
        existing['fans'] = fans_data
        existing['dashboard'] = state.get('dashboard', {'groups': [], 'cards': []})

        tmp_path = CONFIG_PATH.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        tmp_path.replace(CONFIG_PATH)

        _cached_config_json = existing.copy()
        try:
            _cached_config_mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            pass

        logger.info('Configuration saved successfully')

    except Exception as e:
        logger.error(f'Failed to save config: {e} — changes may be lost, will retry on next save', exc_info=True)


def save_config():
    """Save configuration with debounce to avoid excessive disk writes."""
    global _save_timer
    with _save_lock:
        if _save_timer is not None:
            _save_timer.cancel()
        _save_timer = threading.Timer(SAVE_DEBOUNCE_SECONDS, _do_save_config)
        _save_timer.daemon = True
        _save_timer.start()


def load_config():
    """Load and migrate configuration"""
    try:
        if not CONFIG_PATH.exists():
            logger.info('No configuration found')
            return

        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)

        cfg = migrate_config(cfg)

        RUNTIME_ONLY_FIELDS = {'rpm', 'pwm_value', 'raw_pwm', 'last_update', 'current_pct', 'target_pwm'}

        with state_lock:
            if isinstance(cfg, dict):
                fans = cfg.get('fans', {})

                for fan_id, fan_cfg in fans.items():
                    if fan_id in state['fans']:
                        for key, val in fan_cfg.items():
                            if key not in RUNTIME_ONLY_FIELDS:
                                state['fans'][fan_id][key] = val
                    else:
                        state['fans'][fan_id] = fan_cfg

                state['initialized'] = bool(cfg.get('initialized', False))
                state['tested'] = bool(cfg.get('tested', False))
                state['language'] = cfg.get('language', 'en')
                state['server_name'] = cfg.get('server_name', 'FanControl Server')
                state['log_level'] = cfg.get('log_level', 'INFO')
                state['log_retention_days'] = cfg.get('log_retention_days', 30)
                state['telegram_bot_token'] = cfg.get('telegram_bot_token', '')
                state['telegram_chat_id'] = cfg.get('telegram_chat_id', '')
                state['telegram_enabled'] = cfg.get('telegram_enabled', False)
                state['telegram_events'] = cfg.get('telegram_events', {
                    'fan_health': True, 'agent_status': True,
                    'updates': True, 'temperature': True,
                })
                state['dashboard'] = cfg.get('dashboard', {'groups': [], 'cards': []})

            logger.info('Configuration loaded successfully')

    except Exception as e:
        logger.error(f'Failed to load config: {e}', exc_info=True)


# ==============================================================================
# MODULE: core.hardware
# ==============================================================================

import copy
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple


logger = logging.getLogger('fancontrol')

_has_smartctl = shutil.which('smartctl') is not None
if not _has_smartctl:
    logger.warning('smartctl not found — SMART data and smartctl-based temp reading unavailable')

executor = ThreadPoolExecutor(max_workers=16)

HWMON_DIR = cfg.hwmon_dir

CALIBRATION_STEPS = [
    0, 25, 51, 76, 102, 127, 153, 178, 204, 229, 255
]
CALIBRATION_SETTLE_TIME = 5


def generate_stable_id(path: str) -> str:
    """Generate stable, safe ID from hardware path using SHA256 hash"""
    hash_obj = hashlib.sha256(path.encode())
    return f"dev-{hash_obj.hexdigest()[:12]}"


def discover_fans_and_sensors() -> Tuple[Dict, Dict]:
    """
    Scan /sys/class/hwmon for fans and temperature sensors.
    Returns (fans_dict, temp_sensors_dict)
    """
    logger.info('=' * 50)
    logger.info('SCANNING HARDWARE MONITORS')
    
    fans = {}
    temps = {}
    
    for hw_path in sorted(HWMON_DIR.iterdir()):
        try:
            chip_name = "unknown"
            name_file = hw_path / 'name'
            if name_file.exists():
                chip_name = name_file.read_text().strip()
            
            logger.info(f'  Chip: {hw_path.name} ({chip_name})')
            
            # Discover PWM fans
            for pwm_file in sorted(hw_path.glob('pwm*')):
                if '_' in pwm_file.name:
                    continue
                
                try:
                    pwm_num = re.search(r'\d+', pwm_file.name).group()
                    fan_input = hw_path / f'fan{pwm_num}_input'
                    
                    if not fan_input.exists():
                        continue
                    
                    label_file = hw_path / f'fan{pwm_num}_label'
                    if label_file.exists():
                        label = label_file.read_text().strip()
                    else:
                        label = f'Fan {pwm_num}'
                    
                    writable = os.access(str(pwm_file), os.W_OK)
                    
                    try:
                        current_rpm = int(fan_input.read_text().strip())
                    except (ValueError, OSError):
                        current_rpm = 0
                    
                    fan_path_str = f'{hw_path.name}/{pwm_file.name}'
                    fan_id = generate_stable_id(fan_path_str)
                    
                    fans[fan_id] = {
                        'id': fan_id,
                        'label': label,
                        'hw_path': fan_path_str,
                        'pwm_path': str(pwm_file),
                        'fan_path': str(fan_input),
                        'rpm': current_rpm,
                        'pwm_value': 0,
                        'writable': writable,
                        'inverted': False,
                        'min_rpm': 0,
                        'max_rpm': 0,
                        'manual_pct': 50,
                        'sensors': [],
                        'sensor_mode': 'max',
                        'target_temp': 31,
                        'mode': 'manual',
                        'status': 'not_tested',
                        'target_pwm': 50,
                        'current_pct': 50,
                        'raw_pwm': 128,
                        'last_update': 0.0,
                        'schedule': [],
                        'curve': [],
                        'calibration': {},
                        'health': {
                            'status': 'healthy',
                            'rpm_baseline': 0,
                            'slowdown_since': None,
                            'stopped_since': None,
                            'last_service_date': None,
                            'calibration_required': False,
                        }
                    }
                    
                except Exception as e:
                    logger.warning(f'    Error reading fan {pwm_file}: {e}')
                    continue
            
            # Discover temperature sensors
            for temp_file in sorted(hw_path.glob('temp*_input')):
                try:
                    temp_name = temp_file.name.replace('_input', '')
                    
                    label_file = hw_path / f'{temp_name}_label'
                    if label_file.exists():
                        label = label_file.read_text().strip()
                    else:
                        label = 'Temp'
                    
                    try:
                        temp_value = int(temp_file.read_text().strip()) // 1000
                    except (ValueError, OSError):
                        temp_value = 0
                    
                    temp_path_str = f'{hw_path.name}/{temp_name}'
                    temp_id = generate_stable_id(temp_path_str)
                    
                    temps[temp_id] = {
                        'id': temp_id,
                        'path': str(temp_file),
                        'label': label,
                        'value': temp_value
                    }
                    
                except Exception as e:
                    logger.warning(f'    Error reading temp sensor {temp_file}: {e}')
                    continue
                    
        except Exception as e:
            logger.warning(f'  Skipped {hw_path.name}: {e}')
            continue
    
    logger.info(f'  Found: {len(fans)} fans, {len(temps)} temp sensors')

    # Fallback: DSM scemd.xml fan control (official kernel xpenology)
    if not fans:
        if is_dsm_fan_available():
            logger.info('  No hwmon fans found, trying DSM scemd.xml...')
            dsm_info = get_dsm_fan_info()
            if dsm_info:
                fan_id = generate_stable_id('dsm-fan-0')
                fans[fan_id] = {
                    'id': fan_id,
                    'label': f'DSM Fan ({dsm_info.get("hw_version", "unknown")})',
                    'hw_path': 'dsm-scemd',
                    'pwm_path': '',
                    'fan_path': '',
                    'rpm': 0,
                    'pwm_value': 0,
                    'writable': True,
                    'inverted': False,
                    'min_rpm': 0,
                    'max_rpm': 0,
                    'manual_pct': 50,
                    'sensors': [],
                    'sensor_mode': 'max',
                    'target_temp': 31,
                    'mode': 'manual',
                    'status': 'not_tested',
                    'target_pwm': 50,
                    'current_pct': 50,
                    'raw_pwm': 128,
                    'last_update': 0.0,
                    'schedule': [],
                    'curve': [],
                    'calibration': {},
                    'control_method': 'dsm_scemd',
                    'health': {
                        'status': 'healthy',
                        'rpm_baseline': 0,
                        'slowdown_since': None,
                        'stopped_since': None,
                        'last_service_date': None,
                        'calibration_required': False,
                    }
                }
                modes = dsm_info.get('modes', [])
                current_speed = modes[0].get('fan_speed', 0) if modes else 0
                logger.info(f'  DSM fan detected: {fan_id}, current speed: {current_speed}%')
            else:
                logger.info('  DSM scemd.xml found but could not parse fan info')

    return fans, temps


def is_physical_disk(dev_name: str) -> bool:
    """Check if device name represents a physical disk"""
    patterns = [
        r'^sata\d+$',
        r'^nvme\d+n\d+$',
        r'^sd[a-z]$',
        r'^sd[a-z]{2,}$',
    ]
    
    if any(re.match(p, dev_name) for p in patterns):
        return True
    
    if any(dev_name.startswith(p) for p in ['hd', 'xvd', 'vd']):
        if not re.search(r'\d$', dev_name):
            return True
    
    return False


def calculate_disk_health(temp: float) -> Dict[str, Any]:
    """Calculate disk health metrics for UI display"""
    if temp <= 0:
        return {'pct_fill': 0, 'color_zone': 'unknown', 'status': 'standby'}
    
    temp = max(10, min(80, temp))
    pct_fill = max(0, min(100, int((temp - 20) / (60 - 20) * 100)))
    
    if temp <= 35:
        color_zone = 'cyan'
    elif temp <= 45:
        color_zone = 'orange'
    elif temp <= 55:
        color_zone = 'red'
    else:
        color_zone = 'critical'
    
    return {'pct_fill': pct_fill, 'color_zone': color_zone, 'status': 'active'}


def parse_smart_temp(output: str) -> Optional[int]:
    """Parse temperature from smartctl output"""
    for line in output.split('\n'):
        if 'Temperature_Celsius' in line:
            match = re.search(r'(\d+)\s*\(', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100:
                    return temp
            
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70:
                    return temp
    
    for line in output.split('\n'):
        if 'Airflow_Temperature_Cel' in line:
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70:
                    return temp
    
    return None


SMART_ATTRIBUTE_META = {
    1: {"name": "Raw_Read_Error_Rate", "criticality": "important", "description": "Частота ошибок чтения", "tooltip": "Рост указывает на деградацию поверхности диска или проблемы с головками."},
    2: {"name": "Throughput_Performance", "criticality": "info", "description": "Производительность", "tooltip": "Общая производительность диска. Снижение может указывать на фрагментацию."},
    3: {"name": "Spin_Up_Time", "criticality": "info", "description": "Время раскрутки", "tooltip": "Время запуска шпинделя. Рост может указывать на износ механики."},
    4: {"name": "Start_Stop_Count", "criticality": "info", "description": "Количество запусков", "tooltip": "Сколько раз диск включался/выключался. Нормальный износ."},
    5: {"name": "Reallocated_Sector_Ct", "criticality": "critical", "description": "Переназначенные сектора", "tooltip": "Количество переназначенных секторов. Рост означает физическую деградацию поверхности диска. Рост > 0 требует замены диска."},
    7: {"name": "Seek_Error_Rate", "criticality": "important", "description": "Частота ошибок позиционирования", "tooltip": "Рост указывает на проблемы с блоком головок или фрагментацией."},
    8: {"name": "Seek_Time_Performance", "criticality": "info", "description": "Время позиционирования", "tooltip": "Среднее время поиска. Снижение = механический износ."},
    9: {"name": "Power_On_Hours", "criticality": "info", "description": "Часы работы", "tooltip": "Общее время работы диска. Нормальный износ, ресурс 30000-50000 часов.", "unit": "hours", "unit_divisor": 1},
    10: {"name": "Spin_Retry_Count", "criticality": "critical", "description": "Повторы раскрутки", "tooltip": "Количество повторных попыток раскрутки шпинделя. Рост = механическая проблема, замена обязательна."},
    11: {"name": "Calibration_Retry_Count", "criticality": "important", "description": "Повторы калибровки", "tooltip": "Неудачные попытки калибровки головок. Рост может привести к ошибкам чтения."},
    12: {"name": "Power_Cycle_Count", "criticality": "info", "description": "Циклы включения", "tooltip": "Количество включений/выключений питания."},
    13: {"name": "Read_Soft_Error_Rate", "criticality": "info", "description": "Программные ошибки чтения", "tooltip": "Ошибки, исправленные ECC. Временные ошибки, обычно не критичны."},
    15: {"name": "Seek_Time_Performance", "criticality": "info", "description": "Время позиционирования", "tooltip": "Среднее время поиска. Снижение = механический износ."},
    17: {"name": "Power_On_Hours", "criticality": "info", "description": "Часы работы", "tooltip": "Общее время работы диска в часах.", "unit": "hours", "unit_divisor": 1},
    18: {"name": "Available_Spare_Threshold", "criticality": "info", "description": "Порог доступного запаса", "tooltip": "Минимально допустимый процент резервных блоков. При достижении — замена обязательна."},
    19: {"name": "Available_Spare", "criticality": "critical", "description": "Доступный запас", "tooltip": "Текущий процент резервных блоков. 0% = ресурс исчерпан, замена обязательна."},
    22: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки", "tooltip": "Количество перемещений головок."},
    170: {"name": "Grown_Failing_Block_Ct", "criticality": "critical", "description": "Выросшие坏块", "tooltip": "Блоки, отмеченные как坏块 после изготовления. Рост = деградация поверхности."},
    171: {"name": "Program_Fail_Count", "criticality": "important", "description": "Ошибки записи", "tooltip": "Неудачные попытки записи. Рост может указывать на проблемы с NAND (SSD)."},
    172: {"name": "Erase_Fail_Count", "criticality": "important", "description": "Ошибки стирания", "tooltip": "Неудачные попытки стирания. Рост = проблема с ячейками памяти (SSD)."},
    173: {"name": "Wear_Leveling_Count", "criticality": "important", "description": "Уровень износа", "tooltip": "Минимальный износ блоков. Для SSD: рост = приближение к концу ресурса."},
    175: {"name": "Program_Fail_Count_Chip", "criticality": "important", "description": "Ошибки программы (чип)", "tooltip": "Ошибки записи на уровне чипа. Рост = проблема с ячейками."},
    176: {"name": "Erase_Fail_Count_Chip", "criticality": "important", "description": "Ошибки стирания (чип)", "tooltip": "Ошибки стирания на уровне чипа. Рост = проблема с ячейками."},
    177: {"name": "Wear_Leveling_Count", "criticality": "important", "description": "Износ блоков", "tooltip": "Количество перераспределенных блоков. Для SSD."},
    178: {"name": "Used_Rsvd_Blk_Ct_Chip", "criticality": "critical", "description": "Использовано резервных блоков", "tooltip": "Резервные блоки исчерпываются. 0 резерва = замена обязательна."},
    179: {"name": "Used_Rsvd_Blk_Ct_Tot", "criticality": "critical", "description": "Всего использовано резервных", "tooltip": "Общее число использованных резервных блоков."},
    180: {"name": "Unused_Rsvd_Blk_Ct_Chip", "criticality": "info", "description": "Свободных резервных блоков", "tooltip": "Остаток резервных блоков. Чем меньше — тем ближе замена."},
    181: {"name": "Program_Fail_Cnt_Total", "criticality": "important", "description": "Всего ошибок записи", "tooltip": "Суммарные ошибки записи за весь срок службы."},
    182: {"name": "Erase_Fail_Count_Total", "criticality": "important", "description": "Всего ошибок стирания", "tooltip": "Суммарные ошибки стирания за весь срок службы."},
    183: {"name": "Runtime_Bad_Block", "criticality": "critical", "description": "坏块 при работе", "tooltip": "坏块, обнаруженные во время работы. Рост = деградация."},
    184: {"name": "End_Ecc_Error", "criticality": "critical", "description": "Исправленные ECC ошибки", "tooltip": "ECC-исправленные ошибки. Рост = проблема с памятью."},
    187: {"name": "Airflow_Temperature_Cel", "criticality": "important", "description": "Температура воздуха", "tooltip": "Температура воздушного потока у диска. Оптимально: 25-45°C."},
    188: {"name": "G_Sense_Error_Rate", "criticality": "important", "description": "Ошибки от вибрации", "tooltip": "Ошибки, вызванные ударами/вибрацией. Рост = физическое повреждение."},
    190: {"name": "Airflow_Temperature_Cel", "criticality": "important", "description": "Температура воздушного потока", "tooltip": "Температура воздуха у диска. Оптимально: 25-45°C. Выше 50°C — перегрев."},
    191: {"name": "G_Sense_Error_Rate", "criticality": "important", "description": "Ошибки от удара", "tooltip": "Ошибки, вызванные ударами/вибрацией. Рост = физическое повреждение."},
    192: {"name": "Power-Off_Retract_Count", "criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество аварийных отключений питания. Рост = риск повреждения головок."},
    193: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки", "tooltip": "Количество перемещений головок. Нормальный износ."},
    194: {"name": "Temperature_Celsius", "criticality": "important", "description": "Температура", "tooltip": "Текущая температура диска. Оптимально: 25-45°C. Выше 50°C — перегрев."},
    195: {"name": "Hardware_ECC_Recovered", "criticality": "info", "description": "ECC восстановления", "tooltip": "Ошибки, исправленные аппаратным ECC. Временные, обычно не критичны."},
    196: {"name": "Reallocated_Event_Count", "criticality": "critical", "description": "События переназначения", "tooltip": "Количество событий переназначения секторов. Рост = деградация."},
    197: {"name": "Current_Pending_Sector", "criticality": "critical", "description": "Ожидающие сектора", "tooltip": "Сектора, ожидающие перераспределения. Рост может привести к потере данных."},
    198: {"name": "Offline_Uncorrectable", "criticality": "critical", "description": "Неисправимые сектора", "tooltip": "Сектора, которые невозможно прочитать/исправить. Рост = немедленная замена диска."},
    199: {"name": "UDMA_CRC_Error_Count", "criticality": "important", "description": "CRC ошибки интерфейса", "tooltip": "Ошибки checksum интерфейса SATA. Проверьте кабель."},
    200: {"name": "Multi_Zone_Error_Rate", "criticality": "important", "description": "Ошибки по зонам", "tooltip": "Ошибки записи в несколько зон. Рост = деградация поверхности."},
    201: {"name": "Soft_Read_Error_Rate", "criticality": "info", "description": "Программные ошибки чтения", "tooltip": "Ошибки чтения, требующие повтора. Временные."},
    202: {"name": "High_Fly_Writes", "criticality": "critical", "description": "Записи на высоте", "tooltip": "Записи, выполненные вне зоны контакта головки. Рост = риск потери данных."},
    203: {"name": "Run_Out_Cancel", "criticality": "critical", "description": "Отмена из-за нехватки ресурса", "tooltip": "Операции отменены из-за нехватки резервных блоков. Замена обязательна."},
    204: {"name": "Soft_ECC_Correction", "criticality": "info", "description": "Программные ECC исправления", "tooltip": "ECC-исправленные ошибки. Временные, обычно не критичны."},
    205: {"name": "Thermal_Asperity_Rate", "criticality": "important", "description": "Термические помехи", "tooltip": "Ошибки из-за температуры. Рост = перегрев."},
    206: {"name": "Flying_Height", "criticality": "important", "description": "Высота полёта головки", "tooltip": "Расстояние головки от пластин. Снижение = риск контакта."},
    207: {"name": "Spin_Try_Count", "criticality": "critical", "description": "Попытки раскрутки", "tooltip": "Количество попыток раскрутки шпинделя. Рост = механическая проблема."},
    208: {"name": "Spin_Retry_Count", "criticality": "critical", "description": "Повторы раскрутки", "tooltip": "Неудачные попытки раскрутки. Замена обязательна."},
    209: {"name": "Offline_Seek_Perform", "criticality": "info", "description": "Offline позиционирование", "tooltip": "Производительность позиционирования в offline."},
    210: {"name": "Tap_Retry_Count", "criticality": "critical", "description": "Повторы поиска", "tooltip": "Неудачные попытки позиционирования. Рост = деградация."},
    220: {"name": "Power-Off_Retract_Count", "criticality": "important", "description": "Аварийные выключения (head)", "tooltip": "Количество аварийных уборок головок."},
    222: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки (head)", "tooltip": "Количество циклов загрузки/выгрузки головок."},
    223: {"name": "Temperature_Celsius", "criticality": "important", "description": "Температура", "tooltip": "Текущая температура диска. Оптимально: 25-45°C."},
    224: {"name": "G_Sense_Error_Rate", "criticality": "important", "description": "Ошибки от вибрации", "tooltip": "Ошибки, вызванные ударами/вибрацией."},
    225: {"name": "Power-Off_Retract_Count", "criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество аварийных отключений питания."},
    226: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки", "tooltip": "Количество перемещений головок."},
    227: {"name": "Temperature_Celsius", "criticality": "important", "description": "Температура (extended)", "tooltip": "Текущая температура диска."},
    230: {"name": "Head_Flying_Hours", "criticality": "info", "description": "Часы работы головок", "tooltip": "Общее время полёта головок над пластинами. Нормальный износ.", "unit": "hours", "unit_divisor": 1},
    231: {"name": "Head_Flying_Hours", "criticality": "info", "description": "Часы работы головок", "tooltip": "Общее время полёта головок над пластинами.", "unit": "hours", "unit_divisor": 1},
    232: {"name": "Total_LBAs_Written", "criticality": "info", "description": "Всего записано (LBA)", "tooltip": "Общее количество записанных блоков. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    233: {"name": "Total_LBAs_Read", "criticality": "info", "description": "Всего прочитано (LBA)", "tooltip": "Общее количество прочитанных блоков. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    234: {"name": "Read_Error_Retry_Rate", "criticality": "important", "description": "Повторы чтения", "tooltip": "Количество повторных попыток чтения. Рост = деградация."},
    235: {"name": "Hardware_ECC_Recovered", "criticality": "info", "description": "ECC восстановления (v2)", "tooltip": "Ошибки, исправленные аппаратным ECC."},
    240: {"name": "Head_Flying_Hours", "criticality": "info", "description": "Часы полёта головок", "tooltip": "Общее время работы головок над пластинами в часах. Нормальный износ, ресурс 30000-50000 часов.", "unit": "hours", "unit_divisor": 1},
    241: {"name": "Total_LBAs_Written", "criticality": "info", "description": "Всего записано данных", "tooltip": "Общий объём записанных данных на диск. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    242: {"name": "Total_LBAs_Read", "criticality": "info", "description": "Всего прочитано данных", "tooltip": "Общий объём прочитанных данных с диска. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    243: {"name": "Read_Error_Retry_Rate", "criticality": "important", "description": "Повторы чтения (v2)", "tooltip": "Количество повторных попыток чтения. Рост = деградация."},
    244: {"name": "Free_Fall_Sector_Count", "criticality": "important", "description": "Сектора при падении", "tooltip": "Количество ошибок при свободном падении. Рост = физическое повреждение."},
}

_smart_cache: Dict[str, Dict] = {}
_smart_cache_time: Dict[str, float] = {}
_smart_cache_lock = threading.Lock()
SMART_CACHE_TTL = 60  # seconds


def parse_smart_attributes(output: str) -> list:
    """Parse all SMART attributes from smartctl -A output."""
    attributes = []
    in_attribute_section = False

    for line in output.split('\n'):
        line = line.strip()

        if 'ID# ATTRIBUTE_NAME' in line or 'ATTRIBUTE_NAME' in line:
            in_attribute_section = True
            continue

        if not in_attribute_section:
            continue

        if not line or line.startswith('===') or line.startswith('SMART'):
            if attributes:
                break
            continue

        parts = line.split()
        if len(parts) < 10:
            continue

        try:
            attr_id = int(parts[0])
        except ValueError:
            continue

        attr_name = parts[1]
        try:
            flag = int(parts[2], 16) if parts[2].startswith('0x') else int(parts[2])
        except (ValueError, IndexError):
            flag = 0

        try:
            value = int(parts[3])
            worst = int(parts[4])
            thresh = int(parts[5])
        except (ValueError, IndexError):
            continue

        raw_value = parts[9] if len(parts) > 9 else '0'
        try:
            raw_num = int(re.sub(r'[^0-9]', '', raw_value) or '0')
        except ValueError:
            raw_num = 0

        meta = SMART_ATTRIBUTE_META.get(attr_id, {})
        criticality = meta.get('criticality', 'info')

        if thresh > 0 and value <= thresh:
            status = 'critical'
        elif thresh > 0 and value <= thresh * 1.5:
            status = 'warning'
        else:
            status = 'ok'

        attributes.append({
            'id': attr_id,
            'name': attr_name,
            'flag': flag,
            'value': value,
            'worst': worst,
            'threshold': thresh,
            'raw': raw_value,
            'raw_num': raw_num,
            'criticality': criticality,
            'description': meta.get('description', attr_name),
            'tooltip': meta.get('tooltip', f'SMART атрибут #{attr_id}'),
            'status': status,
            'unit': meta.get('unit'),
            'unit_divisor': meta.get('unit_divisor'),
        })

    return attributes


def parse_nvme_smart(output: str) -> dict:
    """Parse NVMe SMART attributes from smartctl output."""
    attributes = {}
    patterns = {
        'critical_warning': r'Critical Warning\s*:\s*(.+)',
        'temperature': r'Temperature:\s+(\d+)\s+Celsius',
        'available_spare': r'Available Spare:\s+(\d+)%',
        'available_spare_threshold': r'Available Spare Threshold:\s+(\d+)%',
        'percentage_used': r'Percentage Used:\s+(\d+)%',
        'data_units_read': r'Data Units Read:\s+([\d,]+)',
        'data_units_written': r'Data Units Written:\s+([\d,]+)',
        'host_reads': r'Host Read Commands:\s+([\d,]+)',
        'host_writes': r'Host Write Commands:\s+([\d,]+)',
        'controller_busy_time': r'Controller Busy Time:\s+([\d,]+)',
        'power_cycles': r'Power Cycles:\s+([\d,]+)',
        'power_on_hours': r'Power On Hours:\s+([\d,]+)',
        'unsafe_shutdowns': r'Unsafe Shutdowns:\s+(\d+)',
        'media_errors': r'Media and Data Integrity Errors:\s+(\d+)',
        'error_log_entries': r'Error Information Log Entries:\s+(\d+)',
        'warning_temp_time': r'Warning Comp\. Temp\. Time:\s+(\d+)',
        'critical_comp_time': r'Critical Comp\. Temp\. Time:\s+(\d+)',
    }

    nvme_meta = {
        'critical_warning': {"criticality": "critical", "description": "Критические ошибки", "tooltip": "Критические ошибки в работе накопителя. Любое ненулевое значение = замена обязательна. Может указывать на перегрев, критический износ или ошибки питания."},
        'temperature': {"criticality": "important", "description": "Температура", "tooltip": "Текущая температура NVMe диска. Оптимально: 25-45°C. Выше 50°C — перегрев, снижение производительности."},
        'available_spare': {"criticality": "critical", "description": "Доступный запас", "tooltip": "Процент резервных блоков для подмены вышедших из строя ячеек. При приближении к порогу — задумайтесь о замене. 0% = ресурс исчерпан, замена обязательна."},
        'available_spare_threshold': {"criticality": "critical", "description": "Порог запаса", "tooltip": "Пороговое значение Available Spare. При достижении этого значения состояние диска считается критическим."},
        'percentage_used': {"criticality": "critical", "description": "Износ NAND", "tooltip": "Уровень износа накопителя в процентах. Зависит от Available Spare и Available Spare Threshold. 100% = ресурс исчерпан."},
        'data_units_read': {"criticality": "info", "description": "Прочитано данных", "tooltip": "Количество прочитанных блоков (1 блок = 512 байт). Информационный параметр. Конвертируется в ГБ.", "unit": "nvme_blocks", "unit_divisor": 512 * 1000},
        'data_units_written': {"criticality": "info", "description": "Записано данных", "tooltip": "Количество записанных блоков (1 блок = 512 байт). Информационный параметр. Конвертируется в ГБ.", "unit": "nvme_blocks", "unit_divisor": 512 * 1000},
        'host_reads': {"criticality": "info", "description": "Операций чтения", "tooltip": "Количество выполненных операций чтения (1 единица ≈ 1 МБ). Информационный параметр."},
        'host_writes': {"criticality": "info", "description": "Операций записи", "tooltip": "Количество выполненных операций записи (1 единица ≈ 1 МБ). Информационный параметр."},
        'controller_busy_time': {"criticality": "info", "description": "Время контроллера", "tooltip": "Время в минутах, когда контроллер был занят обслуживанием запросов системы."},
        'power_cycles': {"criticality": "info", "description": "Циклы включения", "tooltip": "Количество циклов включения/выключения. Нормальный износ."},
        'power_on_hours': {"criticality": "info", "description": "Часы работы", "tooltip": "Общее наработанное время в часах. Нормальный износ, ресурс 30000-50000 часов.", "unit": "hours", "unit_divisor": 1},
        'unsafe_shutdowns': {"criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество небезопасных отключений питания. Рост = риск повреждения данных и NAND-ячеек."},
        'media_errors': {"criticality": "critical", "description": "Ошибки носителя", "tooltip": "Ошибки целостности данных. Рост = проблема с NAND, замена обязательна."},
        'error_log_entries': {"criticality": "important", "description": "Записи журнала ошибок", "tooltip": "Количество записей в журнале ошибок. Рост = повторяющиеся проблемы."},
        'warning_temp_time': {"criticality": "info", "description": "Время при перегреве", "tooltip": "Время работы (в минутах) при высокой температуре. Рост = перегрев."},
        'critical_comp_time': {"criticality": "critical", "description": "Время при крит. темп.", "tooltip": "Время работы (в минутах) при критической температуре. Рост = сильный перегрев, замена обязательна."},
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            value_str = match.group(1).replace(',', '')
            try:
                value = int(value_str)
            except ValueError:
                value = 0

            meta = nvme_meta.get(key, {})
            attributes[key] = {
                'value': value,
                'criticality': meta.get('criticality', 'info'),
                'description': meta.get('description', key),
                'tooltip': meta.get('tooltip', ''),
                'unit': meta.get('unit'),
                'unit_divisor': meta.get('unit_divisor'),
            }

    return attributes


def read_disk_smart(disk_identifier: str) -> dict:
    """
    Read full SMART data for a disk.
    Returns dict with device info, attributes, and metadata.
    Tries multiple access methods: direct, SAT, MegaRAID.
    """
    if not _has_smartctl:
        return {'error': 'smartctl not installed'}
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()

        if not is_physical_disk(clean_name):
            return {'error': 'Not a physical disk'}

        is_nvme = clean_name.startswith('nvme')

        # Extract disk index from name (e.g., sda=0, sdb=1)
        disk_index = -1
        if clean_name.startswith('sd'):
            disk_index = ord(clean_name[2]) - ord('a')
        elif clean_name.startswith('nvme'):
            try:
                disk_index = int(clean_name.split('n')[0].replace('nvme', ''))
            except (ValueError, IndexError):
                pass

        # Try multiple access methods in order
        attempts = []

        if is_nvme:
            attempts.append(['smartctl', '-A', '-i', f'/dev/{clean_name}'])
            attempts.append(['smartctl', '-A', '-i', '-d', 'nvme', f'/dev/{clean_name}'])
        else:
            # 1. Direct access
            attempts.append(['smartctl', '-A', '-i', f'/dev/{clean_name}'])
            # 2. SAT passthrough
            attempts.append(['smartctl', '-A', '-i', '-d', 'sat', f'/dev/{clean_name}'])
            # 3-4. RAID controllers only for sdX devices
            if clean_name.startswith('sd') and disk_index >= 0:
                attempts.append(['smartctl', '-A', '-i', '-d', f'megaraid,{disk_index}', f'/dev/sda'])
                attempts.append(['smartctl', '-A', '-i', '-d', f'areca,{disk_index + 1}', '/dev/arcmsr0'])

        output = ''
        used_cmd = None
        for cmd in attempts:
            try:
                logger.info(f'SMART attempt: {" ".join(cmd)}')
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                stdout = result.stdout or ''

                # Check if output has actual SMART data (not just device info)
                has_device_info = 'Model Family' in stdout or 'Device Model' in stdout or 'Serial Number' in stdout
                has_smart_attrs = 'SMART Attributes' in stdout or 'SMART overall-health' in stdout or 'Raw_Read_Error_Rate' in stdout
                has_nvme_smart = 'SMART/Health' in stdout or 'Available Spare' in stdout

                if has_device_info and (has_smart_attrs or has_nvme_smart):
                    output = stdout
                    used_cmd = cmd
                    logger.info(f'SMART success with attrs: {" ".join(cmd)}')
                    break
                elif has_device_info:
                    logger.info(f'SMART: device info found but no attributes with {" ".join(cmd)}, trying next...')
                elif result.returncode == 0 and stdout.strip():
                    logger.debug(f'SMART: some output but no recognized data: {" ".join(cmd)}')
                if result.stderr:
                    logger.debug(f'SMART stderr: {result.stderr[:200]}')
            except subprocess.TimeoutExpired:
                logger.debug(f'SMART timeout: {" ".join(cmd)}')
                continue

        # If no method found attributes, try one more time with -a (all SMART data)
        if not output:
            for base_dev in [f'/dev/{clean_name}', '/dev/sda']:
                try:
                    cmd = ['smartctl', '-a', base_dev]
                    logger.info(f'SMART fallback -a: {" ".join(cmd)}')
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                    stdout = result.stdout or ''
                    if 'SMART Attributes' in stdout or 'SMART overall-health' in stdout or 'SMART/Health' in stdout:
                        output = stdout
                        used_cmd = cmd
                        logger.info(f'SMART -a success: {" ".join(cmd)}')
                        break
                except Exception:
                    pass

        if not output:
            return {'error': 'smartctl failed — no SMART data available (disk may be behind RAID controller)'}

        device_info = {}
        for line in output.split('\n'):
            if 'Device Model:' in line or 'Model Family:' in line:
                device_info['model'] = line.split(':', 1)[1].strip()
            if 'Serial Number:' in line:
                device_info['serial'] = line.split(':', 1)[1].strip()
            if 'Firmware Version:' in line:
                device_info['firmware'] = line.split(':', 1)[1].strip()
            if 'User Capacity:' in line:
                device_info['capacity'] = line.split(':', 1)[1].strip()

        if is_nvme:
            attributes = parse_nvme_smart(output)
            attr_type = 'nvme'
        else:
            attributes = parse_smart_attributes(output)
            attr_type = 'sata'

        return {
            'device': f'/dev/{clean_name}',
            'device_info': device_info,
            'attributes': attributes,
            'attr_type': attr_type,
            'access_method': ' '.join(used_cmd) if used_cmd else 'unknown',
        }

    except Exception as e:
        logger.error(f'Error reading SMART for {disk_identifier}: {e}')
        return {'error': str(e)}


def read_disk_temp(disk_identifier: str) -> Tuple[Optional[float], bool]:
    """
    Read temperature from a disk.
    Returns (temperature_celsius, is_standby)
    Tries multiple access methods: smartctl direct, SAT, sysfs, SMART attributes.
    Prefers Airflow_Temperature_Cel over Temperature_Celsius for accuracy.
    """
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()

        if not is_physical_disk(clean_name):
            return None, False

        # Method 1: smartctl (skipped if not installed — falls through to hdparm/sysfs)
        if _has_smartctl:
            attempts = []
            attempts.append(['smartctl', '-a', '-n', 'standby', f'/dev/{clean_name}'])
            attempts.append(['smartctl', '-a', '-n', 'standby', '-d', 'sat', f'/dev/{clean_name}'])
            # MegaRAID/Areca only for sdX devices (behind RAID controllers)
            # Skip for Synology proprietary sataX/nvmeX — never behind RAID
            if clean_name.startswith('sd'):
                disk_index = ord(clean_name[2]) - ord('a')
                attempts.append(['smartctl', '-a', '-n', 'standby', '-d', f'megaraid,{disk_index}', '/dev/sda'])
                attempts.append(['smartctl', '-a', '-n', 'standby', '-d', f'areca,{disk_index + 1}', '/dev/arcmsr0'])

            for cmd in attempts:
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

                    if result.returncode == 2:
                        logger.info(f'DISK TEMP: {clean_name} standby via {" ".join(cmd)}')
                        return None, True  # standby

                    stdout = result.stdout or ''
                    if not stdout:
                        continue

                    temp, source = _parse_disk_temp_preferred(stdout)
                    if temp is not None:
                        logger.info(f'DISK TEMP: {clean_name} = {temp}°C (source={source}) via {" ".join(cmd)}')
                        for line in stdout.split('\n'):
                            if any(kw in line for kw in ['Airflow', 'HDA_Temp', 'Temperature_Celsius',
                                                          'Current Drive Temperature', 'temperature',
                                                          '190 ', '194 ', '194\t', 'SMART Attributes']):
                                logger.info(f'DISK TEMP ATTR: {clean_name} — {line.strip()[:150]}')
                        return float(temp), False
                    else:
                        for line in stdout.split('\n'):
                            if any(kw in line for kw in ['Temperature', 'Airflow', 'temperature', 'temp', 'Celsius', '190', '194']):
                                logger.info(f'DISK TEMP DEBUG: {clean_name} — {line.strip()[:150]}')

                except subprocess.TimeoutExpired:
                    continue

    except Exception as e:
        logger.debug(f'Error reading disk temp for {disk_identifier}: {e}')

    # Method 2: hdparm (some drives expose temp via ATA IDENTIFY)
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()
        result = subprocess.run(
            ['hdparm', '-I', f'/dev/{clean_name}'],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.split('\n'):
                low = line.lower()
                if 'temperature' in low:
                    match = re.search(r'(-?\d{1,3})\s*(?:°|deg|C)', line, re.IGNORECASE)
                    if not match:
                        match = re.search(r':\s*(-?\d{1,3})', line)
                    if match:
                        temp = int(match.group(1))
                        if 10 < temp < 80:
                            logger.info(f'DISK TEMP: {clean_name} = {temp}°C via hdparm')
                            return float(temp), False
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    # Method 3: sysfs temperature
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()
        sysfs_temp = _read_sysfs_temp(clean_name)
        if sysfs_temp is not None:
            logger.info(f'DISK TEMP: {clean_name} = {sysfs_temp}°C via sysfs')
            return sysfs_temp, False
    except Exception:
        pass

    return None, False


def _extract_smart_raw_value(line: str) -> Optional[int]:
    """Extract the RAW_VALUE from a smartctl SMART attribute line.

    Format: ID# ATTRIBUTE_NAME FLAGS VALUE WORST THRESH TYPE UPDATED FAILING_NOW RAW_VALUE
    Example: 190 Airflow_Temperature_Cel 0x0022 065 053 000 Old_age Always - 35 (Min/Max 26/45)

    The RAW_VALUE (35) is the actual temperature. The VALUE field (065) is a
    normalized 0-253 scale — NOT the temperature. Previous code used findall()
    which grabbed 065 first, reporting 65°C instead of 35°C.
    """
    # The raw value comes after the FAILING_NOW column (always `-` or a flag)
    match = re.search(r'\s-\s+(\d{1,3})\b', line)
    if match:
        return int(match.group(1))
    return None


def _parse_disk_temp_preferred(output: str) -> Tuple[Optional[int], str]:
    """Parse temperature from smartctl output.
    Priority:
    1. Airflow_Temperature_Cel — actual air temp near disk (best)
    2. HDA_Temperature — head/disk assembly temp
    3. Current Drive Temperature header
    4. Temperature_Celsius attribute — last resort (may be controller/IC temp)
    Returns (temperature, source_label)."""
    lines = output.split('\n')

    for line in lines:
        if 'Airflow_Temperature_Cel' in line:
            raw = _extract_smart_raw_value(line)
            if raw and 10 < raw < 80:
                return raw, 'airflow'

    for line in lines:
        if 'HDA_Temperature' in line:
            raw = _extract_smart_raw_value(line)
            if raw and 10 < raw < 80:
                return raw, 'hda'

    for line in lines:
        if 'Current Drive Temperature' in line:
            match = re.search(r':\s*(\d+)', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100:
                    return temp, 'smartctl_header'

    for line in lines:
        if 'Temperature_Celsius' in line:
            raw = _extract_smart_raw_value(line)
            if raw and 0 < raw < 100:
                return raw, 'celsius'

    # Pass 5: NVMe "Temperature: 37 Celsius" (non-SMART-attribute format)
    for line in lines:
        if 'Temperature:' in line and 'Celsius' in line:
            match = re.search(r'Temperature:\s+(\d+)', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100:
                    return temp, 'nvme'

    return None, ''


def _read_sysfs_temp(dev_name: str) -> Optional[float]:
    """Try to read temperature from sysfs."""
    import glob as _glob
    # Try common sysfs temperature paths
    patterns = [
        f'/sys/block/{dev_name}/device/scsi_disk/*/temperature',
        f'/sys/block/{dev_name}/device/scsi_disk/*/hwmon/hwmon*/temp1_input',
        f'/sys/block/{dev_name}/hwmon/hwmon*/temp1_input',
    ]
    for pattern in patterns:
        try:
            for path in _glob.glob(pattern):
                with open(path) as f:
                    val = int(f.read().strip())
                    if val > 0:
                        return val / 1000.0 if val > 200 else float(val)
        except Exception:
            continue
    return None


def discover_disks() -> Dict[str, Dict]:
    """
    Discover physical disks in the system.
    Returns cached data if polling is already in progress.
    """
    logger.info('=' * 50)
    logger.info('DISK DISCOVERY')
    
    with state_lock:
        if state.get('disks_polling'):
            logger.warning('Disk polling already in progress, returning cached data')
            return copy.deepcopy(state['hdd_sensors'])
        state['disks_polling'] = True
    
    try:
        disks = {}
        discovered_devices = set()
        
        # Method 1: lsblk
        try:
            result = subprocess.run(
                ['lsblk', '-nd', '-o', 'NAME,TYPE,TRAN'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0].strip()
                    dtype = parts[1].strip()
                    
                    if dtype == 'disk' and is_physical_disk(name):
                        skip_prefixes = ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']
                        if not any(name.startswith(p) for p in skip_prefixes):
                            discovered_devices.add(name)
                            
        except Exception as e:
            logger.warning(f'lsblk failed: {e}')
            
            try:
                for dev_path in Path('/sys/block').iterdir():
                    name = dev_path.name
                    if is_physical_disk(name):
                        skip_prefixes = ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']
                        if not any(name.startswith(p) for p in skip_prefixes):
                            discovered_devices.add(name)
            except Exception as e2:
                logger.warning(f'/sys/block fallback failed: {e2}')
        
        # Read temperatures in parallel
        futures_map = {
            dev: executor.submit(read_disk_temp, dev)
            for dev in sorted(discovered_devices)
        }
        
        for dev, future in futures_map.items():
            try:
                result = future.result(timeout=10)
                temp, standby = result if result else (None, False)
                
                disk_id = generate_stable_id(f'/dev/{dev}')
                
                if dev.startswith('sata'):
                    disk_label = f'SATA {dev.replace("sata", "")}'
                    disk_type = 'sata'
                elif dev.startswith('nvme'):
                    disk_label = f'NVMe {dev}'
                    disk_type = 'nvme'
                else:
                    disk_label = f'Disk {dev}'
                    disk_type = 'sata'
                
                health = calculate_disk_health(temp if temp else 0)
                
                disks[disk_id] = {
                    'id': disk_id,
                    'label': disk_label,
                    'device': f'/dev/{dev}',
                    'dev_name': dev,
                    'temp': temp if temp else 0,
                    'standby': standby,
                    'type': disk_type,
                    'pct_fill': health['pct_fill'],
                    'color_zone': health['color_zone'],
                    'health_status': health['status']
                }
                
            except FutureTimeout:
                logger.warning(f'Timeout polling disk {dev}')
            except Exception as e:
                logger.error(f'Failed to poll disk {dev}: {e}')
        
        logger.info(f'  Discovered: {len(disks)} disks')
        return disks
    
    finally:
        with state_lock:
            state['disks_polling'] = False


def set_pwm(key: str, raw_pwm: int, raw: bool = False):
    """
    Set PWM value. When raw=True, writes physical value directly without
    inversion handling or RPM reading (used during calibration).
    """
    with state_lock:
        fan = state['fans'].get(key)
        if not fan:
            return

        # Check if fan has hwmon path (standard Linux PWM)
        pwm_path = fan.get('pwm_path', '')
        if pwm_path.startswith('/sys/class/hwmon/'):
            _set_pwm_hwmon(fan, raw_pwm, raw, key)
        elif fan.get('control_method') == 'dsm_scemd':
            _set_pwm_dsm(fan, raw_pwm, raw)
        else:
            return


def _set_pwm_hwmon(fan, raw_pwm, raw, key):
    """Set PWM via standard Linux hwmon sysfs."""
    val = max(0, min(255, int(raw_pwm)))

    if not raw:
        fan['raw_pwm'] = val

    physical_pwm = (255 - val) if (not raw and fan.get('inverted')) else val

    try:
        Path(fan['pwm_path']).write_text(str(physical_pwm))
        fan['pwm_value'] = val

        if not raw:
            try:
                rpm_raw = Path(fan['fan_path']).read_text().strip()
                rpm_val = int(rpm_raw) if rpm_raw.isdigit() else 0
                if rpm_val > 0:
                    fan['rpm'] = rpm_val
            except Exception:
                pass

            fan['last_update'] = time.monotonic()

    except Exception as e:
        logger.error(f'PWM write error {key}: {e}')


def _set_pwm_dsm(fan, raw_pwm, raw):
    """Set fan speed via DSM scemd.xml (0-255 maps to 0-100%)."""

    val = max(0, min(255, int(raw_pwm)))
    if not raw:
        fan['raw_pwm'] = val

    percent = int(val * 100 / 255)
    set_dsm_fan_speed(percent)
    fan['pwm_value'] = val
    fan['last_update'] = time.monotonic()


def refresh():
    """Update temperature and RPM readings.
    
    Reads sysfs without holding state_lock, then batch-updates under a
    single lock acquisition instead of per-sensor locks.
    """
    with state_lock:
        temp_paths = [(k, v['path']) for k, v in state['temp_sensors'].items()]
        fan_paths = [(k, v['fan_path']) for k, v in state['fans'].items()]

    # Read all sysfs without holding lock
    temp_updates = {}
    for key, path in temp_paths:
        try:
            temp_updates[key] = int(Path(path).read_text().strip()) // 1000
        except Exception:
            pass

    def poll_fan(item):
        k, path = item
        try:
            return k, int(Path(path).read_text().strip())
        except Exception:
            return k, None

    futures = [executor.submit(poll_fan, item) for item in fan_paths]
    fan_updates = {}
    for future in futures:
        try:
            key, rpm = future.result(timeout=2)
            if rpm is not None:
                fan_updates[key] = rpm
        except Exception:
            pass

    # Single lock for all writes
    with state_lock:
        for k, v in temp_updates.items():
            if k in state['temp_sensors']:
                state['temp_sensors'][k]['value'] = v
        for k, rpm in fan_updates.items():
            if k in state['fans']:
                state['fans'][k]['rpm'] = rpm


def get_system_info():
    """Get system info: uptime, CPU, memory."""
    info = {}

    # Uptime
    try:
        with open('/proc/uptime') as f:
            uptime_sec = float(f.read().split()[0])
        days = int(uptime_sec // 86400)
        hours = int((uptime_sec % 86400) // 3600)
        mins = int((uptime_sec % 3600) // 60)
        info['uptime'] = f"{days}d {hours}h {mins}m"
        info['uptime_seconds'] = uptime_sec
    except Exception:
        info['uptime'] = '--'
        info['uptime_seconds'] = 0

    # CPU load
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        info['cpu_load'] = round(load1 / cpu_count * 100, 1)
    except Exception:
        info['cpu_load'] = 0

    # Memory
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ('MemTotal:', 'MemAvailable:'):
                    mem[parts[0]] = int(parts[1])
        total = mem.get('MemTotal:', 1)
        avail = mem.get('MemAvailable:', 0)
        info['mem_total_mb'] = round(total / 1024)
        info['mem_used_mb'] = round((total - avail) / 1024)
        info['mem_percent'] = round((total - avail) / total * 100, 1)
    except Exception:
        info['mem_percent'] = 0

    return info

# ==============================================================================
# MODULE: core.dsm_fan
# ==============================================================================

"""DSM fan control via scemd.xml — fallback for official kernel xpenology.

Supports two scemd.xml formats:
1. Official Synology: <fan_config type="DUAL_MODE_LOW" ...> wrapping child elements
2. Flat format: <disk_temperature fan_speed="..." action="..."> directly under <scemd>

Fan speed values can be:
- "20%40hz" (percentage + Hz notation)
- "255" (raw 0-255 PWM value)
- "20" (plain percentage)
"""

import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger('fancontrol')

SCEMD_PATH = '/usr/syno/etc.defaults/scemd.xml'
SCEMD_BACKUP = '/usr/syno/etc.defaults/scemd.xml.bak'

# Known fan_config types in priority order
KNOWN_SCHEME_TYPES = [
    'DUAL_MODE_HIGH',
    'DUAL_MODE_LOW',
    'FULL_SPEED',
    'STOP',
]

# Module-level cache for scemd.xml parsed tree
_scemd_cache = None
_scemd_cache_mtime = 0.0


def is_dsm_fan_available():
    """Check if scemd.xml exists and can be used for fan control."""
    return Path(SCEMD_PATH).exists() and os.access(SCEMD_PATH, os.R_OK | os.W_OK)


def _invalidate_scemd_cache():
    """Invalidate cached scemd.xml tree. Called after writes."""
    global _scemd_cache, _scemd_cache_mtime
    _scemd_cache = None
    _scemd_cache_mtime = 0.0


def _parse_scemd():
    """Parse scemd.xml and return the tree (cached by file mtime)."""
    global _scemd_cache, _scemd_cache_mtime
    try:
        mtime = Path(SCEMD_PATH).stat().st_mtime
    except OSError:
        return None

    if _scemd_cache is not None and mtime == _scemd_cache_mtime:
        import copy
        return copy.deepcopy(_scemd_cache)

    try:
        tree = ET.parse(SCEMD_PATH)
        _scemd_cache = tree
        _scemd_cache_mtime = mtime
        return tree
    except ET.ParseError as e:
        logger.error(f'Failed to parse {SCEMD_PATH}: {e}')
        return None
    except Exception as e:
        logger.error(f'Error reading {SCEMD_PATH}: {e}')
        return None


def _parse_fan_speed(raw):
    """Parse fan_speed string to integer percentage (0-100).

    Handles formats: '20%40hz', '99%40hz', '255', '20', 'UNKNOWN', etc.
    """
    if raw is None or raw == 'UNKNOWN':
        return None
    # "20%40hz" or "99%40hz" format
    m = re.match(r'^(\d+)%', str(raw))
    if m:
        return int(m.group(1))
    # Plain integer (0-255 raw or 0-100 percentage)
    try:
        val = int(raw)
        if val > 100:
            return int(val * 100 / 255)
        return val
    except (ValueError, TypeError):
        return None


def _parse_entry(elem):
    """Parse a single disk_temperature or cpu_temperature element into a dict."""
    fan_speed_raw = elem.attrib.get('fan_speed', '')
    return {
        'sensor_type': elem.tag,
        'fan_speed': fan_speed_raw,
        'fan_speed_pct': _parse_fan_speed(fan_speed_raw),
        'action': elem.attrib.get('action', 'NONE'),
        'threshold_temp': elem.text.strip() if elem.text else '0',
        'index': None,  # set by caller
    }


def _parse_fan_config(fc):
    """Parse a <fan_config> element into a scheme dict."""
    entries = []
    for child in fc:
        if child.tag in ('disk_temperature', 'cpu_temperature'):
            entries.append(_parse_entry(child))

    for i, e in enumerate(entries):
        e['index'] = i

    return {
        'type': fc.attrib.get('type', 'UNKNOWN'),
        'period': fc.attrib.get('period', ''),
        'threshold': fc.attrib.get('threshold', ''),
        'hibernation_speed': fc.attrib.get('hibernation_speed', 'UNKNOWN'),
        'entries': entries,
    }


def get_all_schemes():
    """Parse scemd.xml and return all fan_config schemes.

    Returns dict with 'schemes' list and 'hw_version'.
    Handles both official format (<fan_config> wrappers) and flat format.
    """
    tree = _parse_scemd()
    if tree is None:
        return None

    root = tree.getroot()
    result = {'schemes': [], 'hw_version': None}

    # Check for hw_version on root or child elements
    hw = root.attrib.get('hw_version')
    if hw:
        result['hw_version'] = hw

    # Format 1: Official Synology — <fan_config type="..."> wrapping children
    fan_configs = list(root.iter('fan_config'))
    if fan_configs:
        for fc in fan_configs:
            fc_type = fc.attrib.get('type', 'UNKNOWN')
            hw = fc.attrib.get('hw_version')
            if hw:
                result['hw_version'] = hw
            scheme = _parse_fan_config(fc)
            result['schemes'].append(scheme)
        return result

    # Format 2: Flat — disk_temperature/cpu_temperature directly under root
    flat_entries = []
    for child in root:
        if child.tag in ('disk_temperature', 'cpu_temperature'):
            flat_entries.append(_parse_entry(child))

    if flat_entries:
        for i, e in enumerate(flat_entries):
            e['index'] = i
        result['schemes'].append({
            'type': 'FLAT',
            'period': '',
            'threshold': '',
            'hibernation_speed': 'UNKNOWN',
            'entries': flat_entries,
        })

    return result


def get_dsm_fan_info():
    """Get simplified info about DSM fan control (backward-compatible).

    Returns dict with 'modes' list for legacy callers.
    """
    info = get_all_schemes()
    if info is None:
        return None

    modes = []
    for scheme in info.get('schemes', []):
        for entry in scheme.get('entries', []):
            modes.append({
                'type': entry['sensor_type'],
                'mode': scheme['type'].lower(),
                'fan_speed': entry.get('fan_speed_pct', 0) or 0,
            })

    return {'modes': modes, 'hw_version': info.get('hw_version')}


def get_scheme(scheme_type):
    """Get a single scheme by type (e.g. 'DUAL_MODE_LOW')."""
    info = get_all_schemes()
    if info is None:
        return None
    for s in info['schemes']:
        if s['type'] == scheme_type:
            return s
    return None


def get_active_scheme_type():
    """Determine which fan_config scheme is currently active.

    Reads current CPU and disk temperatures and finds which scheme's
    thresholds match. Falls back to DUAL_MODE_LOW if unknown.
    """
    info = get_all_schemes()
    if not info or not info['schemes']:
        return None

    # Read current temperatures
    cpu_temp = _read_current_cpu_temp()
    disk_temp = _read_current_max_disk_temp()

    best_match = None
    for scheme in info['schemes']:
        if _scheme_matches_temps(scheme, cpu_temp, disk_temp):
            best_match = scheme['type']
            break

    return best_match or 'DUAL_MODE_LOW'


def _read_current_cpu_temp():
    """Read current CPU temperature from hwmon or thermal zone."""
    try:
        for tz in Path('/sys/class/thermal').glob('thermal_zone*'):
            try:
                temp = int(tz.read_text().strip()) / 1000
                return temp
            except (ValueError, OSError):
                continue
    except Exception:
        pass
    return 40  # fallback guess


def _read_current_max_disk_temp():
    """Read max disk temperature from hwmon sensors."""
    try:
        for hw in Path('/sys/class/hwmon').iterdir():
            for temp_file in hw.glob('temp*_input'):
                try:
                    val = int(temp_file.read_text().strip()) / 1000
                    if 20 < val < 80:
                        return val
                except (ValueError, OSError):
                    continue
    except Exception:
        pass
    return 35  # fallback guess


def _scheme_matches_temps(scheme, cpu_temp, disk_temp):
    """Check if current temps match this scheme's temperature thresholds."""
    for entry in scheme.get('entries', []):
        try:
            threshold = float(entry.get('threshold_temp', '0'))
        except (ValueError, TypeError):
            continue

        if entry['sensor_type'] == 'cpu_temperature' and cpu_temp >= threshold:
            return True
        if entry['sensor_type'] == 'disk_temperature' and disk_temp >= threshold:
            return True
    return False


def update_scheme_entry(scheme_type, index, fan_speed_pct=None, action=None, threshold_temp=None):
    """Update a single entry in a scheme.

    Args:
        scheme_type: e.g. 'DUAL_MODE_LOW'
        index: entry index within the scheme
        fan_speed_pct: new fan speed percentage (0-100)
        action: new action ('NONE', 'SHUTDOWN')
        threshold_temp: new threshold temperature
    """
    tree = _parse_scemd()
    if tree is None:
        return False

    root = tree.getroot()
    changed = False

    # Backup on first write
    _ensure_backup()

    # Find the fan_config element
    fc = _find_fan_config(root, scheme_type)
    if fc is None:
        logger.error(f'Scheme {scheme_type} not found in scemd.xml')
        return False

    # Find the entry by index
    entries = [c for c in fc if c.tag in ('disk_temperature', 'cpu_temperature')]
    if index < 0 or index >= len(entries):
        logger.error(f'Entry index {index} out of range (0-{len(entries)-1})')
        return False

    elem = entries[index]

    if fan_speed_pct is not None:
        new_speed = str(max(0, min(100, int(fan_speed_pct))))
        old_speed = elem.attrib.get('fan_speed', '')
        if old_speed != new_speed:
            elem.attrib['fan_speed'] = new_speed
            changed = True
            logger.info(f'{scheme_type}[{index}] fan_speed: {old_speed} -> {new_speed}')

    if action is not None:
        old_action = elem.attrib.get('action', 'NONE')
        if old_action != action:
            elem.attrib['action'] = action
            changed = True
            logger.info(f'{scheme_type}[{index}] action: {old_action} -> {action}')

    if threshold_temp is not None:
        old_temp = elem.text.strip() if elem.text else '0'
        new_temp = str(int(threshold_temp))
        if old_temp != new_temp:
            elem.text = new_temp
            changed = True
            logger.info(f'{scheme_type}[{index}] threshold: {old_temp} -> {new_temp}')

    if not changed:
        return True

    return _write_and_restart(tree)


def update_scheme(scheme_type, entries):
    """Replace all entries in a scheme.

    Args:
        scheme_type: e.g. 'DUAL_MODE_LOW'
        entries: list of dicts with keys: sensor_type, fan_speed, action, threshold_temp
    """
    tree = _parse_scemd()
    if tree is None:
        return False

    root = tree.getroot()
    _ensure_backup()

    fc = _find_fan_config(root, scheme_type)
    if fc is None:
        logger.error(f'Scheme {scheme_type} not found')
        return False

    # Remove existing temperature children
    for child in list(fc):
        if child.tag in ('disk_temperature', 'cpu_temperature'):
            fc.remove(child)

    # Add new entries
    for entry in entries:
        tag = entry.get('sensor_type', 'disk_temperature')
        elem = ET.SubElement(fc, tag)
        elem.attrib['fan_speed'] = str(entry.get('fan_speed', '20'))
        elem.attrib['action'] = entry.get('action', 'NONE')
        elem.text = str(entry.get('threshold_temp', '0'))

    logger.info(f'Updated scheme {scheme_type} with {len(entries)} entries')
    return _write_and_restart(tree)


def set_dsm_fan_speed(percent):
    """Set fan speed via scemd.xml for ALL schemes. Restarts scemd service."""
    tree = _parse_scemd()
    if tree is None:
        logger.error('Cannot set DSM fan speed: scemd.xml not parseable')
        return False

    root = tree.getroot()
    percent = max(0, min(100, int(percent)))
    _ensure_backup()

    changed = False

    # Update fan_config children (official format)
    for fc in root.iter('fan_config'):
        for child in fc:
            if child.tag in ('disk_temperature', 'cpu_temperature'):
                old = child.attrib.get('fan_speed', '')
                child.attrib['fan_speed'] = str(percent)
                if old != str(percent):
                    changed = True

    # Update flat format (no fan_config wrapper)
    for child in root:
        if child.tag in ('disk_temperature', 'cpu_temperature') and child.getparent() is root:
            old = child.attrib.get('fan_speed', '')
            child.attrib['fan_speed'] = str(percent)
            if old != str(percent):
                changed = True

    if not changed:
        logger.info(f'DSM fan speed already at {percent}%')
        return True

    return _write_and_restart(tree)


def _find_fan_config(root, scheme_type):
    """Find a <fan_config> element by its type attribute."""
    for fc in root.iter('fan_config'):
        if fc.attrib.get('type') == scheme_type:
            return fc
    return None


def _ensure_backup():
    """Create backup of scemd.xml if not already done."""
    if not Path(SCEMD_BACKUP).exists():
        try:
            import shutil
            shutil.copy2(SCEMD_PATH, SCEMD_BACKUP)
            logger.info(f'Backed up {SCEMD_PATH} to {SCEMD_BACKUP}')
        except Exception as e:
            logger.warning(f'Failed to backup scemd.xml: {e}')


def _write_and_restart(tree):
    """Write tree back to scemd.xml, verify integrity, then restart service."""
    try:
        tree.write(SCEMD_PATH, encoding='unicode', xml_declaration=False)
        logger.info(f'Wrote {SCEMD_PATH}')
        _invalidate_scemd_cache()
    except Exception as e:
        logger.error(f'Failed to write {SCEMD_PATH}: {e}')
        return False

    # Verify written XML is parseable before restarting scemd
    try:
        from xml.etree import ElementTree
        ElementTree.parse(SCEMD_PATH)
    except Exception as e:
        logger.error(f'Written XML is corrupt, restoring backup: {e}')
        _restore_backup()
        return False

    return _restart_scemd()


def _restore_backup():
    """Restore scemd.xml from backup."""
    import shutil
    backup = Path(SCEMD_BACKUP)
    if backup.exists():
        try:
            shutil.copy2(backup, SCEMD_PATH)
            _invalidate_scemd_cache()
            logger.info(f'Restored {SCEMD_PATH} from backup')
        except Exception as e:
            logger.error(f'Failed to restore backup: {e}')
    else:
        logger.error('No backup available to restore')


def _restart_scemd():
    """Restart the scemd service to apply fan settings."""
    for cmd in [
        ['systemctl', 'restart', 'scemd'],
        ['synoservice', '--restart', 'scemd'],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f'scemd restarted successfully via {cmd[0]}')
                return True
            else:
                logger.warning(f'{cmd[0]} failed: {result.stderr}')
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f'{cmd[0]} error: {e}')
            continue

    logger.error('Failed to restart scemd service')
    return False


# ==============================================================================
# MODULE: core.calibration
# ==============================================================================

"""Fan calibration — PWM/RPM curve detection and inversion handling."""

import copy
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional


logger = logging.getLogger('fancontrol')


def _detect_inversion(raw: List[Dict], fan_label: str) -> bool:
    """
    Detect if a fan has inverted PWM control using 3 methods.
    Returns True if inverted.
    """
    all_rpm = [pt['rpm'] for pt in raw]
    max_rpm = max(all_rpm)

    low_rpm_avg = sum(pt['rpm'] for pt in raw[:3]) / 3 if raw[:3] else 0
    high_rpm_avg = sum(pt['rpm'] for pt in raw[-3:]) / 3 if raw[-3:] else 0

    rpm_at_zero = raw[0]['rpm'] if raw else 0
    rpm_at_max = raw[-1]['rpm'] if raw else 0

    half = len(raw) // 2
    first_half_avg = sum(pt['rpm'] for pt in raw[:half]) / max(half, 1)
    second_half_avg = sum(pt['rpm'] for pt in raw[half:]) / max(len(raw) - half, 1)

    logger.info(
        f'Fan {fan_label} inversion check: '
        f'rpm@PWM0={rpm_at_zero}, rpm@PWM255={rpm_at_max}, '
        f'low3_avg={low_rpm_avg:.0f}, high3_avg={high_rpm_avg:.0f}, '
        f'first_half_avg={first_half_avg:.0f}, second_half_avg={second_half_avg:.0f}'
    )

    is_inverted = False
    if rpm_at_max > 0 and rpm_at_zero > rpm_at_max * 1.1:
        is_inverted = True
    elif high_rpm_avg > 0 and low_rpm_avg > high_rpm_avg * 1.1:
        is_inverted = True
    elif second_half_avg > 0 and first_half_avg > second_half_avg * 1.1:
        is_inverted = True

    logger.info(f'Fan {fan_label} inversion result: {"INVERTED" if is_inverted else "normal"}')
    return is_inverted


def _normalize_curve(raw: List[Dict], is_inverted: bool) -> List[Dict]:
    """
    Normalize calibration curve. If inverted, remap PWM so low pct = low RPM.
    Returns sorted curve.
    """
    if is_inverted:
        return sorted(
            [{'pwm': 255 - pt['pwm'], 'rpm': pt['rpm'], 'pct': 100 - pt['pct']}
             for pt in raw],
            key=lambda x: x['pwm']
        )
    return sorted(raw, key=lambda x: x['pwm'])


def _detect_dead_zones(raw: List[Dict], max_rpm: int) -> tuple:
    """
    Detect min_pwm (fan start) and max_pwm (saturation) from calibration data.
    Returns (min_pwm, max_pwm).
    """
    if not raw or max_rpm == 0:
        return 0, 255

    min_threshold = max_rpm * 0.05

    min_pwm = 0
    for pt in raw:
        if pt['rpm'] > min_threshold:
            min_pwm = pt['pwm']
            break

    max_pwm = 255
    for i in range(len(raw) - 1, 0, -1):
        if raw[i]['rpm'] > raw[i - 1]['rpm'] * 1.01:
            max_pwm = raw[i]['pwm']
            break

    logger.info(f'Dead zones: min_pwm={min_pwm}, max_pwm={max_pwm}')
    return min_pwm, max_pwm


def test_fans(fan_key: Optional[str] = None, socketio=None, save_config_fn=None):
    """
    Calibrate fans by testing PWM/RPM curve.
    Uses parallel testing for efficiency.
    socketio: needed for emit during calibration.
    save_config_fn: callable to persist config on success.
    """
    test_successful = True

    with state_lock:
        if fan_key:
            if fan_key not in state['fans']:
                raise ValueError(f'Fan key not found: {fan_key}')
            fans_to_test = {fan_key: copy.deepcopy(state['fans'][fan_key])}
        else:
            fans_to_test = {
                k: copy.deepcopy(v)
                for k, v in state['fans'].items()
            }

    writable_fans = {k: f for k, f in fans_to_test.items() if f.get('writable')}

    if not writable_fans:
        logger.warning('No writable fans found for calibration')
        return

    for f in writable_fans.values():
        f['inverted'] = False
        f['status'] = 'calibrating'

    with state_lock:
        state['testing'] = True
        state['_pause_loop'] = True
        state['test_progress'] = {
            'status': 'Starting parallel calibration...',
            'step': 0,
            'total': len(CALIBRATION_STEPS),
            'current': 'All fans'
        }

    if socketio:
        socketio.emit('test_progress', state['test_progress'])

    pwm_steps = CALIBRATION_STEPS
    raw_data = {k: [] for k in writable_fans}

    try:
        for step_idx, pwm_value in enumerate(pwm_steps):
            pct = round(pwm_value * 100 / 255)

            with state_lock:
                state['test_progress'].update({
                    'step': step_idx + 1,
                    'status': f'Testing level {pct}%',
                    'current': 'Parallel mode'
                })
            if socketio:
                socketio.emit('test_progress', state['test_progress'])

            for k in writable_fans:
                with state_lock:
                    set_pwm(k, pwm_value, raw=True)

            time.sleep(CALIBRATION_SETTLE_TIME)

            def read_rpm(item):
                key, fan = item
                try:
                    rpm = int(Path(fan['fan_path']).read_text().strip())
                except Exception:
                    rpm = 0
                return key, rpm

            futures = [
                executor.submit(read_rpm, (k, f))
                for k, f in writable_fans.items()
            ]

            for future in futures:
                try:
                    key, rpm = future.result(timeout=2)
                    raw_data[key].append({
                        'pwm': pwm_value,
                        'rpm': rpm,
                        'pct': pct
                    })

                    if rpm is not None:
                        with state_lock:
                            if key in state['fans']:
                                state['fans'][key]['rpm'] = rpm

                except Exception as ex:
                    logger.error(f'RPM read error: {ex}')

            if socketio:
                socketio.emit('update', get_state())

        for k, fan in writable_fans.items():
            raw = raw_data.get(k, [])

            if not raw:
                logger.warning(f'Fan {fan.get("label", k)} ({k}): No RPM data')
                fan.update({
                    'status': 'not_connected',
                    'min_rpm': 0,
                    'max_rpm': 0,
                    'curve': [],
                    'calibration': {}
                })
                with state_lock:
                    set_pwm(k, 128, raw=True)
                continue

            all_rpm = [pt['rpm'] for pt in raw]
            max_rpm = max(all_rpm)

            if max_rpm == 0:
                logger.warning(f"Fan {fan.get('label', k)} ({k}): No RPM detected")
                fan.update({
                    'status': 'not_connected',
                    'min_rpm': 0,
                    'max_rpm': 0,
                    'curve': [],
                    'calibration': {}
                })
                with state_lock:
                    set_pwm(k, 128, raw=True)
                continue

            is_inverted = _detect_inversion(raw, fan.get('label', k))

            if is_inverted:
                fan['inverted'] = True
                fan['status'] = 'inverted'
            else:
                fan['inverted'] = False
                fan['status'] = 'normal'

            fan['curve'] = _normalize_curve(raw, is_inverted)

            min_threshold = max_rpm * 0.05

            real_min = next(
                (pt for pt in fan['curve'] if pt['rpm'] > min_threshold),
                fan['curve'][0]
            )
            cal_min_pct = real_min['pct']

            detected_min_pwm, detected_max_pwm = _detect_dead_zones(raw, max_rpm)

            existing_cal = state.get('fans', {}).get(k, {}).get('calibration', {})
            fan.update({
                'min_rpm': real_min['rpm'],
                'max_rpm': max_rpm,
                'calibration': {
                    'min_rpm': real_min['rpm'],
                    'max_rpm': max_rpm,
                    'min_pct': cal_min_pct,
                    'inverted': fan['inverted'],
                    'min_pwm': existing_cal.get('min_pwm', detected_min_pwm),
                    'max_pwm': existing_cal.get('max_pwm', detected_max_pwm),
                    'lambda': existing_cal.get('lambda', 1.0),
                }
            })

            set_pwm(k, 128)
            fan['manual_pct'] = 50
            fan['current_pct'] = 50
            fan['target_pwm'] = 50
            logger.info(
                f'Fan {fan.get("label", k)}: status={fan["status"]}, '
                f'min={fan["min_rpm"]}rpm, max={fan["max_rpm"]}rpm'
            )

        with state_lock:
            for k, fan in writable_fans.items():
                if k in state['fans']:
                    state['fans'][k].update(fan)

    except Exception as e:
        logger.error(f'Test error: {e}', exc_info=True)
        test_successful = False

    finally:
        with state_lock:
            state['testing'] = False
            state['tested'] = test_successful

            if test_successful:
                state['initialized'] = True
                if save_config_fn:
                    save_config_fn()
                logger.info('System initialized successfully')

            state['_pause_loop'] = False
            state['test_progress'] = {
                'status': 'Ready!' if test_successful else 'Completed with errors',
                'step': 0,
                'total': 0,
                'current': ''
            }
            current_initialized = state['initialized']

        if socketio:
            socketio.emit('test_progress', state['test_progress'])
            socketio.emit('test_complete', {
                'success': test_successful,
                'initialized': current_initialized
            })


# ==============================================================================
# MODULE: core.sensors
# ==============================================================================

"""Disk temperature sensors — re-exports from hardware module.

read_disk_temp() and parse_smart_temp() live in core/hardware.py
to avoid circular imports. This module re-exports them for
consumers that think of these as sensor operations.
"""


# ==============================================================================
# MODULE: core.control
# ==============================================================================

"""Control loop — fan temperature evaluation, PWM calculation, and main loop."""

import logging
import sqlite3
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any


logger = logging.getLogger('fancontrol')

SENSOR_FAILURE_TEMP = 99

MIN_PWM_PCT = 20
MAX_PWM_PCT = 100

CONTROL_LOOP_INTERVAL = 5
UNINITIALIZED_POLL_INTERVAL = 10
TELEMETRY_LOG_INTERVAL = 300
LOG_CLEANUP_INTERVAL = 86400
DISK_POLL_COOLDOWN = 15


_db_local = threading.local()


def get_db_connection() -> sqlite3.Connection:
    """Thread-local persistent SQLite connection with WAL mode."""
    conn = getattr(_db_local, 'conn', None)
    if conn is not None:
        # Check if connection is still valid (detect stale connections from recycled threads)
        try:
            conn.execute('SELECT 1')
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _db_local.conn = None
            conn = None
    if conn is None:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA journal_size_limit=10485760')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=5000')
        _db_local.conn = conn
    return conn


def refresh_disks():
    """
    Update disk temperatures with health metrics - FULLY ATOMIC VERSION.
    Skips if disks_polling flag is set (concurrent discovery).
    All calculations inside single state_lock to prevent race conditions.
    """
    now = time.monotonic()
    
    with state_lock:
        if state.get('disks_polling'):
            return
        
        last_poll = state['last_hdd_poll']
        if last_poll > 0 and now - last_poll < DISK_POLL_COOLDOWN:
            return
        
        sensors_copy = {
            disk_id: info.copy()
            for disk_id, info in state['hdd_sensors'].items()
        }
    
    futures_map = {
        disk_id: executor.submit(read_disk_temp, info.get('dev_name', disk_id))
        for disk_id, info in sensors_copy.items()
    }
    
    updated_values = {}
    for disk_id, future in futures_map.items():
        try:
            result = future.result(timeout=3)
            temp, standby = result if isinstance(result, tuple) and len(result) == 2 else (None, False)
            
            if temp is not None:
                health = calculate_disk_health(temp)
                updated_values[disk_id] = {
                    'temp': temp,
                    'standby': standby,
                    'pct_fill': health['pct_fill'],
                    'color_zone': health['color_zone'],
                    'health_status': health['status']
                }
        except FutureTimeout:
            logger.debug(f'Timeout polling disk {disk_id}')
        except Exception as e:
            logger.error(f'Poll error for {disk_id}: {e}')
    
    # SINGLE ATOMIC BLOCK
    with state_lock:
        for disk_id, data in updated_values.items():
            if disk_id in state['hdd_sensors']:
                state['hdd_sensors'][disk_id].update(data)
        
        state['last_hdd_poll'] = time.monotonic()
        
        active_temps = [
            v['temp'] for v in state['hdd_sensors'].values()
            if v.get('temp', 0) > 0 and not v.get('standby')
        ]
        
        all_standby = (
            len(state['hdd_sensors']) > 0 and
            all(v.get('standby', False) for v in state['hdd_sensors'].values())
        )
        
        if active_temps:
            state['max_hdd_temp'] = max(active_temps)
            state['failsafe'] = False
            state['standby_mode'] = False
        elif all_standby:
            state['max_hdd_temp'] = 0
            state['failsafe'] = False
            state['standby_mode'] = True
        else:
            state['max_hdd_temp'] = 0
            state['failsafe'] = True
            state['standby_mode'] = False


def fan_temp(fan: Dict, override_sensors: Optional[List] = None, 
             override_sensor_mode: Optional[str] = None) -> float:
    """Calculate effective temperature for a fan based on assigned sensors.
    
    Reads sensor values under lock without copying entire dicts —
    only accesses the specific sensor_ids needed.
    """
    sensors = override_sensors if override_sensors is not None else fan.get('sensors', [])
    mode = override_sensor_mode if override_sensor_mode is not None else fan.get('sensor_mode', 'max')
    
    if not sensors:
        return SENSOR_FAILURE_TEMP
    
    temps = []
    
    with state_lock:
        for sensor_id in sensors:
            temp = _extract_sensor_temp(sensor_id)
            if temp > 0:
                temps.append(temp)
    
    if not temps:
        return SENSOR_FAILURE_TEMP
    
    if mode == 'max':
        return max(temps)
    elif mode == 'min':
        return min(temps)
    else:
        return sum(temps) / len(temps)


def _extract_sensor_temp(sensor_id: str) -> float:
    """Extract temperature for a single sensor_id. Must be called under state_lock."""
    if sensor_id.startswith('hdd:'):
        disk_id = sensor_id.split(':', 1)[1]
        return state['hdd_sensors'].get(disk_id, {}).get('temp', 0)
    if sensor_id.startswith('temp:'):
        temp_id = sensor_id.split(':', 1)[1]
        return state['temp_sensors'].get(temp_id, {}).get('value', 0)
    # Legacy format — try both
    if sensor_id in state['hdd_sensors']:
        return state['hdd_sensors'][sensor_id].get('temp', 0)
    if sensor_id in state['temp_sensors']:
        return state['temp_sensors'][sensor_id].get('value', 0)
    return 0


def pwm_from_curve(fan: Dict, target_pct: float) -> int:
    """
    Convert target percentage to PWM value using calibration curve.
    Applies dead zone offset and lambda curve shape.
    """
    curve = fan.get('curve', [])
    cal = fan.get('calibration', {})
    
    if not cal or len(curve) < 2:
        return int(target_pct * 255 // 100)
    
    target_pct = max(0.0, min(100.0, float(target_pct)))
    min_pct = float(cal.get('min_pct', 0))
    
    if 0.0 < target_pct < min_pct:
        target_pct = min_pct
    
    raw_pwm = None
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        if min(a['pct'], b['pct']) <= target_pct <= max(a['pct'], b['pct']):
            if a['pct'] == b['pct']:
                raw_pwm = a['pwm']
                break
            ratio = (target_pct - a['pct']) / (b['pct'] - a['pct'])
            raw_pwm = a['pwm'] + (b['pwm'] - a['pwm']) * ratio
            break
    
    if raw_pwm is None:
        raw_pwm = curve[-1]['pwm']

    min_pwm = cal.get('min_pwm', 0)
    max_pwm = cal.get('max_pwm', 255)
    lam = cal.get('lambda', 1.0)

    if lam != 1.0 and max_pwm > min_pwm:
        span = max_pwm - min_pwm
        normalized = (raw_pwm - min_pwm) / span
        normalized = max(0.0, min(1.0, normalized))
        normalized = normalized ** lam
        raw_pwm = normalized * span + min_pwm

    return max(min_pwm, min(max_pwm, int(round(raw_pwm))))


def process_auto_mode(fan_id: str, fan: Dict, current_temp: float,
                      target_temp: float, schedule_item: Optional[Dict] = None,
                      failsafe: bool = False, standby_mode: bool = False) -> Tuple[int, str]:
    """
    PURE FUNCTION: Calculates target PWM percentage and status.
    Does NOT write to hardware or modify state.
    
    Args:
        fan_id: Fan identifier (for logging only)
        fan: Fan configuration dictionary
        current_temp: Current effective temperature
        target_temp: Target temperature
        schedule_item: Current schedule rule if applied (optional)
        failsafe: Whether the system is in failsafe mode
        standby_mode: Whether disks are in standby
    
    Returns:
        (target_pct: int, status: str)
    """
    if failsafe:
        return MAX_PWM_PCT, 'failsafe'

    if standby_mode:
        status = 'standby'
        
        if schedule_item:
            sm = schedule_item.get('mode', 'auto')
            if sm == 'manual':
                target_pct = schedule_item.get('speed_pct', 50)
            elif sm == 'off':
                target_pct = 0
            else:
                sched_target = schedule_item.get('target_temp', target_temp)
                target_pct = max(MIN_PWM_PCT, min(40, 
                    sched_target - current_temp + 30))
        else:
            target_pct = 25
        
        return int(target_pct), status

    # 3. Temperature sensor failure - MAXIMUM COOLING for hardware safety!
    if current_temp >= SENSOR_FAILURE_TEMP:
        return MAX_PWM_PCT, 'critical'

    # 4. Normal operation (Proportional curve)
    delta = current_temp - target_temp
    
    if delta <= -2:
        target_pct = MIN_PWM_PCT
    elif delta >= 6:
        target_pct = MAX_PWM_PCT
    else:
        target_pct = MIN_PWM_PCT + (delta + 2) * (MAX_PWM_PCT - MIN_PWM_PCT) // 8

    status = 'warning' if delta > 4 else 'nominal'
    return int(target_pct), status


def _evaluate_fan_mode(fan_id: str, fan: Dict, sys_failsafe: bool, sys_standby: bool) -> Tuple[int, str]:
    """
    Evaluate target_pct and status for a fan based on its mode and schedule.
    Returns (target_pct, status).
    """
    mode = fan.get('mode', 'manual')
    
    if mode == 'manual':
        raw_pct = fan.get('manual_pct', 50)
        status = fan.get('status', 'nominal')
        if status not in ['nominal', 'warning', 'critical', 'standby', 'failsafe', 'inverted', 'no_sensor']:
            status = 'nominal'
        return raw_pct, status
    
    if mode != 'auto':
        return 50, 'nominal'
    
    schedule = fan.get('schedule', [])
    schedule_applied = False
    
    if schedule:
        now_dt = datetime.now()
        current_day = now_dt.strftime('%a').lower()
        current_time = now_dt.strftime('%H:%M')
        
        for item in schedule:
            day = item.get('day', 'all')
            if day == 'all':
                days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
            elif day == 'weekday':
                days = ['mon', 'tue', 'wed', 'thu', 'fri']
            elif day == 'weekend':
                days = ['sat', 'sun']
            else:
                days = [day]
            
            if current_day in days and item['time_start'] <= current_time <= item['time_end']:
                schedule_applied = True
                sm = item.get('mode', 'auto')
                
                if sm == 'off':
                    return 0, 'off'
                elif sm == 'manual':
                    return item.get('speed_pct', 50), 'manual'
                else:
                    item_sensors = item.get('sensors') or fan.get('sensors', [])
                    item_sensor_mode = item.get('sensor_mode') or fan.get('sensor_mode', 'max')
                    current_temp = fan_temp(fan, item_sensors, item_sensor_mode)
                    target_temp = item.get('target_temp', fan.get('target_temp', 31))
                    target_pct, status = process_auto_mode(
                        fan_id, fan, current_temp, target_temp, item,
                        failsafe=sys_failsafe, standby_mode=sys_standby
                    )
                    if status == 'critical' and not item_sensors:
                        status = 'no_sensor'
                    return target_pct, status
                break
    
    if not schedule_applied:
        current_temp = fan_temp(fan)
        target_temp = fan.get('target_temp', 31)
        target_pct, status = process_auto_mode(
            fan_id, fan, current_temp, target_temp,
            failsafe=sys_failsafe, standby_mode=sys_standby
        )
        if status == 'critical' and not fan.get('sensors'):
            status = 'no_sensor'
        return target_pct, status
    
    return 50, 'nominal'


def log_telemetry():
    """Log telemetry data to SQLite.
    
    Reads fan/disk counts directly without state_lock — GIL protects
    dict iteration. Values may be 1 cycle stale, acceptable for logging.
    """
    try:
        with state_lock:
            fans = dict(state.get('fans', {}))
            disk_count = len(state.get('hdd_sensors', {}))
            max_temp = state.get('max_hdd_temp', 0)

        fan_count = len(fans)
        if fan_count > 0:
            avg_pwm = sum(
                f.get('raw_pwm', f.get('pwm_value', 0))
                for f in fans.values()
            ) // fan_count
            avg_rpm = sum(
                f.get('rpm', 0)
                for f in fans.values()
            ) // fan_count
        else:
            avg_pwm = 0
            avg_rpm = 0

        with get_db_connection() as conn:
            conn.execute(
                'INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    datetime.now().isoformat(),
                    'auto',
                    avg_pwm,
                    avg_rpm,
                    max_temp,
                    fan_count,
                    disk_count
                )
            )
            conn.commit()

    except sqlite3.OperationalError as e:
        logger.error(f'SQLite write error: {e}')


def cleanup_logs(retention_days: int = 30):
    """Remove old log entries"""
    try:
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with get_db_connection() as conn:
            conn.execute('DELETE FROM logs WHERE ts < ?', (cutoff,))
            conn.commit()
        logger.info(f'Cleaned logs older than {retention_days} days')
    except sqlite3.OperationalError as e:
        logger.error(f'Failed to cleanup logs: {e}')


def check_fan_health(socketio=None):
    """Detect fan slowdown (bearing wear) and stop, emit alerts on status changes."""
    now = time.time()
    changes = []

    with state_lock:
        fans = dict(state.get('fans', {}))
        for fan_id, fan in fans.items():
            if not fan.get('writable'):
                continue
            if fan.get('mode') == 'off':
                continue

            health = fan.setdefault('health', {
                'status': 'healthy',
                'rpm_baseline': 0,
                'slowdown_since': None,
                'stopped_since': None,
                'last_service_date': None,
                'calibration_required': False,
            })
            old_status = health.get('status', 'healthy')
            rpm = fan.get('rpm', 0) or 0
            pwm_from_pct = fan.get('current_pct', 0) or 0
            pwm_from_manual = fan.get('manual_pct', 0) or 0
            pwm_from_target = fan.get('target_pwm', 0) or 0
            pwm = max(pwm_from_pct, pwm_from_manual, pwm_from_target)
            cal = fan.get('calibration', {})
            max_rpm = cal.get('max_rpm', 0)
            new_status = old_status

            logger.debug(f'[health] {fan.get("label", fan_id)}: rpm={rpm} pwm={pwm} '
                         f'pct={pwm_from_pct} manual={pwm_from_manual} target={pwm_from_target} '
                         f'baseline={health.get("rpm_baseline", 0):.0f} status={old_status} '
                         f'stopped_since={health.get("stopped_since")}')

            should_be_spinning = pwm > 5 or health.get('rpm_baseline', 0) > 0
            if rpm < 10 and should_be_spinning:
                if health.get('stopped_since') is None:
                    health['stopped_since'] = now
                if now - health['stopped_since'] >= 10:
                    new_status = 'stopped'
            else:
                if health.get('stopped_since') is not None:
                    health['stopped_since'] = None
                if old_status == 'stopped':
                    new_status = 'healthy'

            if new_status != 'stopped' and rpm > 0 and pwm > 0:
                if max_rpm > 0:
                    expected_rpm = max_rpm * (pwm / 100)
                else:
                    if old_status in ('healthy', 'slowing'):
                        if health.get('rpm_baseline', 0) == 0:
                            health['rpm_baseline'] = rpm
                        else:
                            health['rpm_baseline'] = 0.9 * health['rpm_baseline'] + 0.1 * rpm
                    expected_rpm = health.get('rpm_baseline', 0)

                if expected_rpm > 0 and rpm < expected_rpm * 0.5:
                    if health.get('slowing_since') is None:
                        health['slowing_since'] = now
                    if now - health['slowing_since'] >= 15:
                        new_status = 'slowing'
                else:
                    if health.get('slowing_since') is not None:
                        health['slowing_since'] = None
                    if old_status == 'slowing':
                        new_status = 'healthy'

            if health.get('calibration_required') and new_status != 'stopped':
                new_status = 'needs_calibration'

            health['status'] = new_status

            if new_status != old_status:
                changes.append((fan_id, fan.get('label', fan_id), old_status, new_status))

    # Emit alerts outside of the lock
    if changes:
        # Telegram notifications
        tg_enabled = state.get('telegram_enabled', False)
        tg_events = state.get('telegram_events', {})
        tg_fan = tg_enabled and tg_events.get('fan_health', True)

        for fan_id, label, old_s, new_s in changes:
            if new_s == 'stopped':
                if socketio:
                    socketio.emit('fan:health', {
                        'fan_id': fan_id, 'node_id': 'local',
                        'status': 'stopped', 'label': label,
                        'message': f'Вентилятор {label} остановлен!',
                    })
                if tg_fan:
                    send_message(f'⛔ <b>Вентилятор остановлен!</b>\n{label} ({fan_id})')
                logger.warning(f'Fan STOPPED: {label} ({fan_id})')
            elif new_s == 'slowing':
                if socketio:
                    socketio.emit('fan:health', {
                        'fan_id': fan_id, 'node_id': 'local',
                        'status': 'slowing', 'label': label,
                        'message': f'Вентилятор {label} замедляется (износ подшипника)',
                    })
                if tg_fan:
                    send_message(f'⚠️ <b>Вентилятор замедляется</b>\n{label} — износ подшипника')
                logger.warning(f'Fan SLOWING: {label} ({fan_id})')
            elif new_s == 'needs_calibration':
                if socketio:
                    socketio.emit('fan:health', {
                        'fan_id': fan_id, 'node_id': 'local',
                        'status': 'needs_calibration', 'label': label,
                        'message': f'Вентилятор {label} требует калибровки',
                    })
                if tg_fan:
                    send_message(f'🔧 <b>Требуется калибровка</b>\n{label}')
            elif new_s == 'healthy' and old_s in ('stopped', 'slowing', 'needs_calibration'):
                if socketio:
                    socketio.emit('fan:health:cleared', {
                        'fan_id': fan_id, 'node_id': 'local',
                    })
                if tg_fan:
                    send_message(f'✅ <b>Вентилятор восстановлен</b>\n{label}')
                logger.info(f'Fan recovered: {label} ({fan_id}) → healthy')


_failed_calibration_logged = False
_last_cleanup = time.monotonic()


def loop(socketio=None):
    """
    Main control loop.
    Architecture: 
    - Pure functions calculate target_pct and status
    - Loop applies PWM and updates state centrally
    """
    global _failed_calibration_logged, _last_cleanup
    last_log = time.monotonic()
    
    while True:
        try:
            if state.get('testing'):
                refresh()
                refresh_disks()
                if socketio:
                    socketio.emit('update', get_state())
                time.sleep(2)
                continue
            
            if state.get('_pause_loop'):
                time.sleep(1)
                continue
            
            if not state.get('initialized'):
                if state.get('hardware_scanned'):
                    refresh()
                    refresh_disks()
                    if socketio:
                        socketio.emit('update', get_state())
                
                if not state.get('tested', True):
                    if not _failed_calibration_logged:
                        logger.warning("Calibration failed. Fix hardware and restart.")
                        _failed_calibration_logged = True
                    time.sleep(UNINITIALIZED_POLL_INTERVAL)
                    continue
                
                time.sleep(2)
                continue
            
            refresh()
            refresh_disks()

            check_fan_health(socketio)

            with state_lock:
                fans_snapshot = {k: v.copy() for k, v in state['fans'].items()}
                sys_failsafe = state.get('failsafe', False)
                sys_standby = state.get('standby_mode', False)
            
            updated_fans_metrics = {}
            
            for fan_id, fan in fans_snapshot.items():
                if not fan.get('writable'):
                    continue
                
                target_pct, status = _evaluate_fan_mode(fan_id, fan, sys_failsafe, sys_standby)
                mode = fan.get('mode', 'manual')
                
                if mode not in ('manual', 'auto'):
                    continue
                
                calculated_pwm = pwm_from_curve(fan, float(target_pct))
                set_pwm(fan_id, calculated_pwm)
                
                updated_fans_metrics[fan_id] = {
                    'current_pct': target_pct,
                    'target_pwm': target_pct,
                    'mode': mode,
                    'status': status,
                    'inverted': fan.get('inverted', False)
                }
            
            with state_lock:
                for fan_id, metrics in updated_fans_metrics.items():
                    if fan_id in state['fans']:
                        state['fans'][fan_id].update(metrics)
            
            if socketio:
                socketio.emit('update', get_state())
                try:
                    check_alerts(socketio)
                except Exception:
                    logger.debug('check_alerts raised', exc_info=True)
            
            current_time = time.monotonic()
            if current_time - last_log > TELEMETRY_LOG_INTERVAL:
                try:
                    log_telemetry()
                except Exception as e:
                    logger.error(f'Logging error: {e}')
                last_log = current_time
            
            if current_time - _last_cleanup > LOG_CLEANUP_INTERVAL:
                cleanup_logs(state.get('log_retention_days', 30))
                _last_cleanup = current_time
            
            time.sleep(CONTROL_LOOP_INTERVAL)
            
        except Exception as e:
            logger.error(f'Loop error: {e}', exc_info=True)
            time.sleep(CONTROL_LOOP_INTERVAL)


def _compare(val, cmp, threshold):
    try:
        if cmp == '>=':
            return val >= threshold
        if cmp == '>':
            return val > threshold
        if cmp == '<=':
            return val <= threshold
        if cmp == '<':
            return val < threshold
        return False
    except Exception:
        return False


def check_alerts(socketio):
    """Evaluate registered alerts in `state['alerts']` and emit 'alert' events."""
    with state_lock:
        alerts = dict(state.get('alerts', {}))
        fired = state.setdefault('_alerts_fired', {})
        fans = dict(state.get('fans', {}))
        max_temp = state.get('max_hdd_temp', 0)
        hdds = dict(state.get('hdd_sensors', {}))

    for key, a in alerts.items():
        a_type = a.get('type')
        target = a.get('target')
        threshold = a.get('threshold')
        cmp = a.get('cmp', '>=')
        level = a.get('level', 'warning')
        message = a.get('message') or f'Alert {key}'

        if threshold is None:
            continue

        value = None
        if a_type == 'fan' and target:
            f = fans.get(target)
            if f:
                value = f.get('rpm') or f.get('current_pct') or 0
        elif a_type in ('overview', 'max_temp'):
            value = max_temp
        elif a_type == 'disk' and target:
            d = hdds.get(target)
            if d:
                value = d.get('temp')
        else:
            with state_lock:
                value = state.get(target)

        if value is None:
            continue

        triggered = _compare(value, cmp, threshold)
        previously = fired.get(key, False)

        if triggered and not previously:
            payload = {
                'key': key,
                'level': level,
                'message': message,
                'value': value,
                'threshold': threshold
            }
            socketio.emit('alert', payload)
            with state_lock:
                fired[key] = True
        elif not triggered and previously:
            payload = {'key': key, 'cleared': True}
            socketio.emit('alert:cleared', payload)
            with state_lock:
                fired[key] = False


# ==============================================================================
# MODULE: core.telegram
# ==============================================================================

"""Telegram notifications — send alerts via Bot API.

Zero new dependencies — uses urllib.request (stdlib).
Rate-limited to 1 msg/sec to avoid Telegram flood limits.
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error

logger = logging.getLogger('fancontrol')

_api_url = 'https://api.telegram.org/bot{token}/sendMessage'
_bot_token = ''
_chat_id = ''
_last_send = 0.0
_lock = threading.Lock()
_min_interval = 1.0


def configure(bot_token, chat_id):
    """Set bot token and chat ID at runtime."""
    global _bot_token, _chat_id
    _bot_token = (bot_token or '').strip()
    _chat_id = (chat_id or '').strip()
    if _bot_token and _chat_id:
        logger.info(f'[TG] Configured: chat_id={_chat_id}')
    else:
        logger.info('[TG] Not configured (missing token or chat_id)')


def is_configured():
    """Check if Telegram is properly configured."""
    return bool(_bot_token and _chat_id)


def send_message(text, parse_mode='HTML'):
    """Send message to Telegram. Thread-safe, rate-limited.

    Returns True on success, False on failure.
    """
    global _last_send
    if not is_configured():
        return False

    with _lock:
        elapsed = time.time() - _last_send
        if elapsed < _min_interval:
            time.sleep(_min_interval - elapsed)
        _last_send = time.time()

    try:
        url = _api_url.format(token=_bot_token)
        payload = json.dumps({
            'chat_id': _chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': True,
        }).encode('utf-8')
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get('ok'):
                logger.info(f'[TG] Sent: {text[:80]}...')
                return True
            else:
                error_code = result.get('error_code', '?')
                description = result.get('description', '')
                logger.warning(f'[TG] API error {error_code}: {description}')
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='ignore')[:200]
        logger.error(f'[TG] HTTP {e.code}: {body}')
        return False
    except Exception as e:
        logger.error(f'[TG] Send failed: {e}')
        return False


# MODULE: core.update_helper
# ==============================================================================

"""Shared update logic — git pull, sync /repo to /app, restart.

Used by both server (routes.py) and agent (handlers.py) to avoid
~100 lines of duplicated code.
"""

import logging
import os
import shutil
import subprocess
import threading

logger = logging.getLogger('fancontrol')

GIT_ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}


def do_git_pull(repo_dir='/repo'):
    """Fetch + reset --hard origin/main.

    Returns (success, version_string) where version_string is the
    CONFIG_VERSION read from the repo after pull (or '' on failure).
    """
    # Step 1: fetch
    fetch = subprocess.run(
        ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
        capture_output=True, text=True, timeout=60, env=GIT_ENV,
    )
    if fetch.returncode != 0:
        logger.error(f'[update] git fetch failed: {fetch.stderr.strip()[:300]}')
        return False, ''

    # Step 2: reset
    reset = subprocess.run(
        ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
        capture_output=True, text=True, timeout=60, env=GIT_ENV,
    )
    output = (reset.stdout + '\n' + reset.stderr).strip()
    logger.info(f'[update] git reset: rc={reset.returncode}, output={output[:300]}')
    if reset.returncode != 0:
        return False, ''

    # Step 3: read version
    version = _read_version_from_repo(repo_dir)
    return True, version


def sync_repo_to_app(repo_dir='/repo', app_dir='/app'):
    """Copy changed files from /repo to /app.

    Returns list of synced item names for logging.
    """
    synced = []

    # Root-level files
    for f in os.listdir(repo_dir):
        if f.endswith('.py') or f.endswith('.txt') or f in ('Dockerfile', 'docker-compose.yml'):
            src = os.path.join(repo_dir, f)
            dst = os.path.join(app_dir, f)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                synced.append(f)

    # Subdirectories
    for d in ('templates', 'static', 'core', 'server', 'agent', 'installer', 'tests'):
        src = os.path.join(repo_dir, d)
        dst = os.path.join(app_dir, d)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            synced.append(f'{d}/')

    logger.info(f'[update] synced {len(synced)} items: {", ".join(synced[:15])}')
    return synced


def schedule_restart(delay=1.0):
    """Schedule os._exit(0) after delay. Triggers Docker restart: unless-stopped."""
    def _exit():
        logger.info('[update] os._exit(0) called')
        os._exit(0)
    threading.Timer(delay, _exit).start()
    logger.info(f'[update] restart scheduled in {delay}s')


def _read_version_from_repo(repo_dir):
    """Extract CONFIG_VERSION string from repo's core/state.py."""
    try:
        with open(os.path.join(repo_dir, 'core', 'state.py')) as f:
            for line in f:
                if 'CONFIG_VERSION' in line:
                    return line.strip()
    except Exception:
        pass
    return ''


# ==============================================================================
# MODULE: server.node_registry
# ==============================================================================

"""Node registry — SQLite storage for registered agents."""

import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional


logger = logging.getLogger('fancontrol')

_db_path = DATA_DIR / 'nodes.db'
_lock = threading.Lock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Thread-local persistent connection with WAL pragmas."""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA journal_size_limit=10485760')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=5000')
        _local.conn = conn
    return conn


def _row_to_dict(row: sqlite3.Row) -> Dict:
    d = dict(row)
    for field in ('config', 'telemetry', 'agent_config_snapshot'):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                logger.warning(f'Corrupted JSON in {field} for node {d.get("node_id", "?")}, using empty dict')
                d[field] = {}
    return d


def init_nodes_table():
    with _lock:
        conn = _get_conn()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                stable_id TEXT UNIQUE,
                name TEXT NOT NULL,
                api_token TEXT UNIQUE NOT NULL,
                ip TEXT DEFAULT '',
                port INTEGER DEFAULT 5059,
                config TEXT DEFAULT '{}',
                telemetry TEXT DEFAULT '{}',
                control_mode TEXT DEFAULT 'server',
                status TEXT DEFAULT 'offline',
                last_seen TEXT,
                agent_config_snapshot TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cols = [r[1] for r in conn.execute('PRAGMA table_info(nodes)').fetchall()]
        logger.info(f'[init_nodes_table] Existing columns: {cols}')
        
        migrations = [
            ('ip', "ALTER TABLE nodes ADD COLUMN ip TEXT DEFAULT ''"),
            ('port', "ALTER TABLE nodes ADD COLUMN port INTEGER DEFAULT 5059"),
            ('agent_version', "ALTER TABLE nodes ADD COLUMN agent_version TEXT DEFAULT ''"),
            ('pending_update', "ALTER TABLE nodes ADD COLUMN pending_update INTEGER DEFAULT 0"),
            ('auto_update', "ALTER TABLE nodes ADD COLUMN auto_update INTEGER DEFAULT 0"),
            # UNIQUE removed from ALTER TABLE — SQLite may not support it.
            # Uniqueness enforced by add_node() IntegrityError handling.
            ('stable_id', "ALTER TABLE nodes ADD COLUMN stable_id TEXT DEFAULT ''"),
        ]
        for col_name, sql in migrations:
            if col_name not in cols:
                try:
                    conn.execute(sql)
                    conn.commit()
                    logger.info(f'[init_nodes_table] Added column: {col_name}')
                except Exception as e:
                    logger.warning(f'[init_nodes_table] Column {col_name} migration failed: {e}')
        
        # Generate stable_id for existing nodes that don't have one
        try:
            for row in conn.execute("SELECT node_id FROM nodes WHERE stable_id IS NULL OR stable_id = ''").fetchall():
                sid = uuid.uuid4().hex[:12]
                conn.execute('UPDATE nodes SET stable_id = ? WHERE node_id = ?', (sid, row[0]))
                logger.info(f'[init_nodes_table] Generated stable_id={sid} for node {row[0]}')
        except Exception as e:
            logger.warning(f'[init_nodes_table] stable_id generation failed: {e}')
        
        conn.commit()


def add_node(name: str, api_token: Optional[str] = None, ip: str = '', port: int = 5059) -> Dict:
    if not api_token:
        api_token = uuid.uuid4().hex
    # Sanitize node_id: lowercase, replace spaces with hyphens, remove special chars
    node_id = re.sub(r'[^a-z0-9\-]', '', name.lower().replace(' ', '-'))
    if not node_id:
        node_id = f'node-{uuid.uuid4().hex[:8]}'
    
    with _lock:
        conn = _get_conn()
        # Try up to 3 times with unique stable_id
        for attempt in range(3):
            stable_id = uuid.uuid4().hex[:12]
            try:
                conn.execute(
                    'INSERT INTO nodes (node_id, stable_id, name, api_token, ip, port) VALUES (?, ?, ?, ?, ?, ?)',
                    (node_id, stable_id, name, api_token, ip, port)
                )
                conn.commit()
                row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
                return _row_to_dict(row)
            except sqlite3.IntegrityError as e:
                if 'node_id' in str(e):
                    # node_id collision — add suffix
                    node_id = f'{node_id}-{uuid.uuid4().hex[:4]}'
                elif 'stable_id' in str(e):
                    # stable_id collision — retry with new one
                    continue
                else:
                    logger.error(f'add_node IntegrityError: {e}')
                    raise
            except Exception as e:
                logger.error(f'add_node failed: {e}')
                raise
        raise Exception('Failed to add node after 3 attempts')


def get_node(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_node_by_token(api_token: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT * FROM nodes WHERE api_token = ?', (api_token,)).fetchone()
        return _row_to_dict(row) if row else None


def get_node_by_stable_id(stable_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT * FROM nodes WHERE stable_id = ?', (stable_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_nodes() -> List[Dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute('SELECT * FROM nodes ORDER BY created_at DESC').fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_node(node_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        cursor = conn.execute('DELETE FROM nodes WHERE node_id = ?', (node_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_node(node_id: str, name: Optional[str] = None, ip: Optional[str] = None,
                port: Optional[int] = None, api_token: Optional[str] = None) -> bool:
    with _lock:
        conn = _get_conn()
        updates = []
        params = []
        if name is not None:
            updates.append('name = ?')
            params.append(name)
        if ip is not None:
            updates.append('ip = ?')
            params.append(ip)
        if port is not None:
            updates.append('port = ?')
            params.append(port)
        if api_token is not None:
            updates.append('api_token = ?')
            params.append(api_token)
        if not updates:
            return False
        params.append(node_id)
        cursor = conn.execute(
            f'UPDATE nodes SET {", ".join(updates)} WHERE node_id = ?',
            params
        )
        conn.commit()
        return cursor.rowcount > 0


def update_node_status(node_id: str, status: str, telemetry: Optional[Dict] = None) -> bool:
    with _lock:
        conn = _get_conn()
        now = datetime.utcnow().isoformat()
        if telemetry is not None:
            conn.execute(
                'UPDATE nodes SET status = ?, telemetry = ?, last_seen = ? WHERE node_id = ?',
                (status, json.dumps(telemetry), now, node_id)
            )
        else:
            conn.execute(
                'UPDATE nodes SET status = ?, last_seen = ? WHERE node_id = ?',
                (status, now, node_id)
            )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def update_node_config(node_id: str, config: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET config = ? WHERE node_id = ?',
            (json.dumps(config), node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def update_node_control_mode(node_id: str, mode: str) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET control_mode = ? WHERE node_id = ?',
            (mode, node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def update_node_flags(node_id: str, pending_update: Optional[bool] = None,
                      auto_update: Optional[bool] = None) -> bool:
    with _lock:
        conn = _get_conn()
        updates = []
        params = []
        if pending_update is not None:
            updates.append('pending_update = ?')
            params.append(1 if pending_update else 0)
        if auto_update is not None:
            updates.append('auto_update = ?')
            params.append(1 if auto_update else 0)
        if not updates:
            return False
        params.append(node_id)
        conn.execute(
            f'UPDATE nodes SET {", ".join(updates)} WHERE node_id = ?',
            params
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def update_node_version(node_id: str, version: str) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET agent_version = ? WHERE node_id = ?',
            (version, node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def save_agent_snapshot(node_id: str, snapshot: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET agent_config_snapshot = ? WHERE node_id = ?',
            (json.dumps(snapshot), node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def get_agent_snapshot(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            'SELECT agent_config_snapshot FROM nodes WHERE node_id = ?',
            (node_id,)
        ).fetchone()
        if row and row['agent_config_snapshot']:
            try:
                return json.loads(row['agent_config_snapshot'])
            except (json.JSONDecodeError, TypeError):
                logger.warning(f'Corrupted agent snapshot for {node_id}')
                return None
        return None


# ==============================================================================
# MODULE: server.announcer
# ==============================================================================

"""SSDP announcer — broadcasts server presence on LAN + responds to M-SEARCH."""

import logging
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
SSDP_INTERVAL = 60

# Track active stop events so we can restart announcer
_active_stop_events: list[threading.Event] = []


def _build_ssdp_response(server_name: str, port: int = 5059) -> str:
    ip = _get_local_ip()
    return (
        'HTTP/1.1 200 OK\r\n'
        'CACHE-CONTROL: max-age=60\r\n'
        'EXT: \r\n'
        f'LOCATION: http://{ip}:{port}\r\n'
        'SERVER: FanControl-Web/3.7.1\r\n'
        f'USN: urn:fancontrol-web:server:{ip}\r\n'
        'ST: urn:fancontrol-web:server\r\n'
        f'X-FanControl-Name: {server_name}\r\n'
        f'X-FanControl-Port: {port}\r\n'
        '\r\n'
    )


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((SSDP_ADDR, SSDP_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def stop_announcers():
    """Signal all active announcer threads to stop."""
    for evt in _active_stop_events:
        evt.set()
    _active_stop_events.clear()


def start_announcer(server_name: str, port: int = 5059) -> Optional[threading.Thread]:
    """Start SSDP broadcast for server discovery by agents."""
    stop_event = threading.Event()
    _active_stop_events.append(stop_event)

    def _announce_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            response = _build_ssdp_response(server_name, port)

            logger.info(f'SSDP server announcer started: {server_name}')

            while not stop_event.is_set():
                try:
                    sock.sendto(response.encode(), (SSDP_ADDR, SSDP_PORT))
                except Exception as e:
                    logger.debug(f'SSDP server announce failed: {e}')
                stop_event.wait(SSDP_INTERVAL)
        except Exception as e:
            logger.error(f'SSDP server announcer error: {e}')

    thread = threading.Thread(target=_announce_loop, daemon=True)
    thread.start()

    # Also start M-SEARCH responder so wizard/agents can actively discover this server
    _start_msearch_responder(server_name, port, stop_event)

    return thread


def _start_msearch_responder(server_name: str, port: int = 5059, stop_event: Optional[threading.Event] = None):
    """Listen for M-SEARCH queries and respond with server info."""
    if stop_event is None:
        stop_event = threading.Event()

    def _respond_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            sock.bind(('', SSDP_PORT))

            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton('0.0.0.0')
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1)

            response = _build_ssdp_response(server_name, port)
            logger.info('SSDP M-SEARCH responder started for server')

            while not stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                    decoded = data.decode(errors='ignore')
                    if 'M-SEARCH' in decoded:
                        # Check if the search is for our type
                        if 'urn:fancontrol-web:server' in decoded:
                            logger.debug(f'M-SEARCH from {addr[0]} — responding')
                            sock.sendto(response.encode(), addr)
                except socket.timeout:
                    continue
        except Exception as e:
            logger.error(f'SSDP M-SEARCH responder error: {e}')

    thread = threading.Thread(target=_respond_loop, daemon=True)
    thread.start()


# ==============================================================================
# MODULE: server.discovery
# ==============================================================================

"""SSDP discovery — listens for agent broadcasts on LAN."""

import logging
import socket
import struct
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable, Dict, List

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
DISCOVERY_TIMEOUT = 5

_discovered_nodes: Dict[str, Dict] = {}
_lock = threading.Lock()


def scan_for_agents(timeout: int = DISCOVERY_TIMEOUT) -> List[Dict]:
    """Send M-SEARCH and collect responses. Preserves existing discovered nodes."""
    logger.info('Starting SSDP M-SEARCH scan...')

    found = []

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.settimeout(timeout)

        msearch = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST: 239.255.255.250:1900\r\n'
            'MAN: "ssdp:discover"\r\n'
            'ST: urn:fancontrol-web:agent\r\n'
            'MX: 3\r\n'
            '\r\n'
        )
        sock.sendto(msearch.encode(), (SSDP_ADDR, SSDP_PORT))
        logger.info('M-SEARCH sent to 239.255.255.250:1900')

        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(1024)
                decoded = data.decode(errors='ignore')
                logger.debug(f'SSDP response from {addr[0]}: {decoded[:100]}')
                _parse_response(decoded, addr[0])
            except socket.timeout:
                break

        sock.close()
    except Exception as e:
        logger.error(f'Discovery scan failed: {e}')

    with _lock:
        found = list(_discovered_nodes.values())

    logger.info(f'SSDP scan complete: {len(found)} agents found')
    return found


def _parse_response(data: str, source_ip: str):
    global _discovered_nodes

    headers = {}
    for line in data.split('\r\n'):
        if ':' in line:
            key, _, value = line.partition(':')
            headers[key.strip().upper()] = value.strip()

    usn = headers.get('USN', '')
    if 'urn:fancontrol-web:agent:' not in usn:
        return

    node_id = usn.split('urn:fancontrol-web:agent:')[-1]
    node_name = headers.get('X-FANCONTROL-NAME', node_id)
    location = headers.get('LOCATION', f'http://{source_ip}:5059')

    logger.info(f'SSDP scan found agent: {node_name} ({source_ip})')

    with _lock:
        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'location': location,
        }


def get_discovered_nodes() -> List[Dict]:
    with _lock:
        return list(_discovered_nodes.values())


# ============================================================================
# Continuous SSDP Listener
# ============================================================================

_discovery_callbacks: List[Callable] = []
_listener_running = False


def on_agent_discovered(callback: Callable):
    """Register callback for when new agent is discovered."""
    if callback not in _discovery_callbacks:
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
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass  # SO_REUSEPORT not available on all platforms
            sock.bind(('', SSDP_PORT))

            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton('0.0.0.0')
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(1)

            logger.info('SSDP discovery listener started on port %d', SSDP_PORT)

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

    # Accept both ST and USN matching for agent detection
    st = headers.get('ST', '')
    usn = headers.get('USN', '')
    is_agent = (st == 'urn:fancontrol-web:agent' or 'urn:fancontrol-web:agent:' in usn)

    if not is_agent:
        return

    node_id = headers.get('X-FANCONTROL-ID', '')
    # Fallback: extract from USN if X-FanControl-Id header missing
    if not node_id and 'urn:fancontrol-web:agent:' in usn:
        node_id = usn.split('urn:fancontrol-web:agent:')[-1]

    node_name = headers.get('X-FANCONTROL-NAME', node_id)
    location = headers.get('LOCATION', '')

    if not node_id:
        return

    with _lock:
        if node_id in _discovered_nodes:
            return

        if get_node(node_id):
            return

        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'location': location,
            'discovered_at': datetime.utcnow().isoformat(),
        }

    logger.info(f'Discovered new agent: {node_name} ({source_ip})')

    for cb in _discovery_callbacks:
        try:
            cb(_discovered_nodes[node_id])
        except Exception as e:
            logger.error(f'Discovery callback error: {e}')


# ============================================================================
# HTTP Probe — fallback when SSDP multicast doesn't work (Docker/VM)
# ============================================================================

def probe_agent(ip: str, port: int = 5059, timeout: int = 3) -> dict:
    """Try to reach an agent directly via HTTP /api/agent/status."""
    try:
        url = f'http://{ip}:{port}/api/agent/status'
        req = urllib.request.Request(url, method='GET')
        req.add_header('User-Agent', 'FanControl-Web')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode()
            import json
            info = json.loads(data)
            info['ip'] = ip
            info['port'] = port
            return info
    except Exception as e:
        logger.debug(f'Probe {ip}:{port} failed: {e}')
        return None


def probe_known_agents(timeout: int = 2) -> List[Dict]:
    """Probe all registered nodes that are offline via HTTP."""
    results = []
    nodes = list_nodes()
    for node in nodes:
        if node.get('status') == 'online':
            continue
        ip = node.get('ip', '')
        if not ip:
            continue
        port = node.get('port', 5059)
        info = probe_agent(ip, port=port, timeout=timeout)
        if info:
            results.append({
                'node_id': node['node_id'],
                'name': node['name'],
                'ip': ip,
                'status': 'online',
                'info': info,
            })
    return results


# ============================================================================
# Subnet Scan — fast TCP probe of all IPs in local subnet
# ============================================================================

def _get_local_subnet() -> tuple:
    """Detect local IP and calculate subnet CIDR. Returns (ip, mask, prefix_len)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        return '127.0.0.1', '255.255.255.0', 24

    # Try to read netmask from /proc/net/if_inet6 or ip addr
    try:
        import fcntl
        import struct
        SIOCGIFNETMASK = 0x891b
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Get first non-loopback interface name
        with open('/proc/net/dev') as f:
            for line in f:
                if ':' in line and not line.strip().startswith('lo'):
                    iface = line.split(':')[0].strip()
                    break
            else:
                iface = 'eth0'
        mask_bytes = fcntl.ioctl(sock.fileno(), SIOCGIFNETMASK, struct.pack('256s', iface.encode()[:15]))
        mask = socket.inet_ntoa(mask_bytes[20:24])
        sock.close()
        prefix = sum(bin(int(b)).count('1') for b in mask.split('.'))
        return ip, mask, prefix
    except Exception:
        # Fallback: assume /24
        parts = ip.split('.')
        mask = f'{parts[0]}.{parts[1]}.{parts[2]}.0'
        return ip, mask, 24


def _tcp_probe(ip: str, port: int = 5059, timeout: float = 0.3) -> bool:
    """Quick TCP connect check — returns True if port is open."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((ip, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def scan_subnet(port: int = 5059, timeout: float = 0.3, probe_timeout: int = 2) -> List[Dict]:
    """Scan local subnet for FanControl agents via TCP connect + HTTP probe.

    Returns list of dicts: {ip, name, node_id, api_token, ...}
    """
    ip, mask, prefix = _get_local_subnet()
    logger.info(f'Subnet scan: local IP={ip}, mask={mask}, /{prefix}')

    # Generate all IPs in subnet
    ip_int = struct.unpack('!I', socket.inet_aton(ip))[0]
    mask_int = struct.unpack('!I', socket.inet_aton(mask))[0]
    network = ip_int & mask_int
    broadcast = network | (~mask_int & 0xFFFFFFFF)

    # For /24, skip network and broadcast addresses. For larger subnets, limit scan.
    hosts = []
    addr = network + 1
    while addr < broadcast:
        if addr != ip_int:  # skip self
            hosts.append(socket.inet_ntoa(struct.pack('!I', addr)))
        addr += 1
        if len(hosts) > 1024:  # safety limit
            break

    logger.info(f'Scanning {len(hosts)} hosts on port {port}...')

    # Parallel TCP connect scan
    found_ips = []
    with ThreadPoolExecutor(max_workers=64) as pool:
        futures = {pool.submit(_tcp_probe, h, port, timeout): h for h in hosts}
        for future in as_completed(futures):
            ip_addr = futures[future]
            try:
                if future.result():
                    found_ips.append(ip_addr)
            except Exception:
                pass

    logger.info(f'TCP scan found {len(found_ips)} hosts with open port {port}: {found_ips}')

    # HTTP probe each found IP
    results = []
    for ip_addr in found_ips:
        info = probe_agent(ip_addr, port=port, timeout=probe_timeout)
        if info:
            results.append(info)

    logger.info(f'Subnet scan complete: {len(results)} agents found')
    return results


# ==============================================================================
# MODULE: server.socket_handlers
# ==============================================================================

"""Socket.IO event handlers for FanControl Web."""

import logging
import threading
import time
from datetime import datetime


logger = logging.getLogger('fancontrol')


def _start_heartbeat_checker(socketio):
    """Background thread that checks agent heartbeats and probes offline agents."""
    # Track which nodes connected via WebSocket (have real telemetry)
    _ws_connected = set()

    def _check_loop():
        nonlocal _ws_connected
        while True:
            time.sleep(10)
            try:
                cleanup_stale_sids()

                # Read from in-memory state instead of SQLite every 10s
                with state_lock:
                    nodes_snapshot = {k: v.copy() for k, v in state.get('nodes', {}).items()}

                now = datetime.utcnow()

                for nid, node in nodes_snapshot.items():

                    if node['status'] == 'online' and node.get('last_seen'):
                        try:
                            last_seen = datetime.fromisoformat(node['last_seen'])
                            age = (now - last_seen).total_seconds()
                            if nid in _ws_connected:
                                # WS-connected: offline after 15s no telemetry
                                if age > 15:
                                    update_node_status(nid, 'offline')
                                    _ws_connected.discard(nid)
                                    with state_lock:
                                        if 'nodes' in state and nid in state['nodes']:
                                            state['nodes'][nid]['status'] = 'offline'
                                    invalidate_state_cache()
                                    socketio.emit('node:update', {
                                        'node_id': nid,
                                        'status': 'offline',
                                        'name': node['name'],
                                    })
                                    # Telegram notification
                                    tg_enabled = state.get('telegram_enabled', False)
                                    tg_events = state.get('telegram_events', {})
                                    if tg_enabled and tg_events.get('agent_status', True):
                                        send_message(f'🔴 <b>Агент отключён</b>\n{node["name"]} ({nid})')
                                    logger.info(f'Agent {node["name"]} marked offline (no telemetry)')
                            elif node.get('ip') and age > 60:
                                # Probe-only: re-probe every 60s, mark offline if unreachable
                                info = probe_agent(node['ip'], timeout=2)
                                if not info:
                                    update_node_status(nid, 'offline')
                                    with state_lock:
                                        if 'nodes' in state and nid in state['nodes']:
                                            state['nodes'][nid]['status'] = 'offline'
                                    invalidate_state_cache()
                                    socketio.emit('node:update', {
                                        'node_id': nid,
                                        'status': 'offline',
                                        'name': node['name'],
                                    })
                                    # Telegram notification
                                    tg_enabled = state.get('telegram_enabled', False)
                                    tg_events = state.get('telegram_events', {})
                                    if tg_enabled and tg_events.get('agent_status', True):
                                        send_message(f'🔴 <b>Агент отключён</b>\n{node["name"]} ({node["ip"]})')
                                    logger.info(f'Agent {node["name"]} ({node["ip"]}) marked offline (probe failed)')
                                else:
                                    # Still reachable — refresh last_seen
                                    update_node_status(nid, 'online')
                        except (ValueError, TypeError):
                            pass

                    elif node['status'] == 'offline' and node.get('ip'):
                        should_probe = True
                        if node.get('last_seen'):
                            try:
                                last = datetime.fromisoformat(node['last_seen'])
                                if (now - last).total_seconds() < 30:
                                    should_probe = False
                            except (ValueError, TypeError):
                                pass

                        if should_probe:
                            info = probe_agent(node['ip'], timeout=2)
                            if info:
                                update_node_status(nid, 'online')
                                update_node(nid, ip=node['ip'])
                                with state_lock:
                                    if 'nodes' not in state:
                                        state['nodes'] = {}
                                    state['nodes'][nid] = {
                                        'node_id': nid,
                                        'name': node['name'],
                                        'status': 'online',
                                    }
                                invalidate_state_cache()

                                socketio.emit('node:update', {
                                    'node_id': nid,
                                    'status': 'online',
                                    'name': node['name'],
                                    'ip': node['ip'],
                                })

                                logger.info(f'Agent {node["name"]} ({node["ip"]}) came online via HTTP probe')
            except Exception as e:
                logger.error(f'Heartbeat check error: {e}')

    def on_ws_connect(node_id):
        """Called when agent connects via WebSocket."""
        _ws_connected.add(node_id)

    def on_ws_disconnect(node_id):
        """Called when agent disconnects from WebSocket."""
        _ws_connected.discard(node_id)

    thread = threading.Thread(target=_check_loop, daemon=True)
    thread.start()

    return on_ws_connect, on_ws_disconnect


def register_handlers(socketio):
    """Register Socket.IO event handlers."""

    # Start SSDP discovery listener

    def on_new_agent(agent_info):
        socketio.emit('node:discovered', agent_info)

    on_agent_discovered(on_new_agent)
    start_discovery_listener()

    # Start SSDP server announcer (so agents can discover this server)
    if _state.get('ssdp_enabled', True):
        _start_server_announcer(
            _state.get('server_name', 'FanControl Server'),
            _state.get('port', 5059),
        )

    @socketio.on('connect')
    def handle_socket_connect():
        """Send initial state on client connection.
        Wait for init_hardware() to complete so the client always
        receives the correct 'initialized' flag (avoids wizard flash)."""
        _init_complete.wait(timeout=15)
        socketio.emit('update', get_state())

    @socketio.on('get_state')
    def handle_get_state():
        """Handle state request from client"""
        socketio.emit('update', get_state())

    _on_ws_connect, _on_ws_disconnect = _start_heartbeat_checker(socketio)

    register_agent_handlers(socketio, on_connect=_on_ws_connect, on_disconnect=_on_ws_disconnect)


def _restart_ssdp_announcer():
    """Stop old SSDP threads and start new ones with current server_name."""
    stop_announcers()
    if state.get('ssdp_enabled', True):
        with state_lock:
            name = state.get('server_name', 'FanControl Server')
            port = state.get('port', 5059)
        start_announcer(name, port)
        logger.info(f'SSDP announcer restarted with name: {name}')


# ==============================================================================
# MODULE: server.agent_handlers
# ==============================================================================

"""Socket.IO event handlers for agent (node) connections."""

import json
import logging
import threading
import time


logger = logging.getLogger('fancontrol')

# Map agent SID → registry node_id, and reverse
_sid_to_node: dict = {}
_node_to_sid: dict = {}


def cleanup_stale_sids():
    """Remove SID mappings where the node no longer exists in state."""
    with state_lock:
        nodes = set(state.get('nodes', {}).keys())
    stale_sids = [sid for sid, nid in _sid_to_node.items() if nid not in nodes]
    for sid in stale_sids:
        nid = _sid_to_node.pop(sid, None)
        if nid:
            _node_to_sid.pop(nid, None)
    if stale_sids:
        logger.info(f'[SID] Cleaned up {len(stale_sids)} stale mappings')

# Grace period: skip conflict detection for 30s after server startup
# to avoid false conflicts when agents reconnect during restart
import time as _time
_startup_time = _time.monotonic()
_GRACE_PERIOD = 30

# Conflict comparison: only meaningful keys, strip runtime fields
_CMP_KEYS = {'fans', 'temp_sensors', 'hdd_sensors', 'kernel_info', 'dsm_schemes', 'control_mode'}
_RUNTIME_FAN_KEYS = {'rpm', 'pwm_value', 'raw_pwm', 'last_update', 'current_pct', 'target_pwm', 'health'}
_RUNTIME_SENSOR_KEYS = {'value', 'temp', 'standby', 'last_update', 'pct_fill', 'color_zone', 'health_status'}


def _strip_runtime(cfg):
    """Remove metadata and runtime-only fields for config comparison."""
    result = {}
    for k, v in (cfg or {}).items():
        if k not in _CMP_KEYS:
            continue
        if k == 'fans' and isinstance(v, dict):
            result[k] = {
                fid: {fk: fv for fk, fv in fval.items() if fk not in _RUNTIME_FAN_KEYS}
                for fid, fval in v.items()
            }
        elif k in ('temp_sensors', 'hdd_sensors') and isinstance(v, dict):
            result[k] = {
                sid: {sk: sv for sk, sv in sval.items() if sk not in _RUNTIME_SENSOR_KEYS}
                for sid, sval in v.items()
            }
        else:
            result[k] = v
    return result


def _emit_to_node(socketio, event, data, node_id):
    """Emit event to a specific agent by node_id via its SID."""
    sid = _node_to_sid.get(node_id)
    if sid:
        logger.info(f'[_emit] {event} → node={node_id} sid={sid[:8]}...')
        socketio.emit(event, data, room=sid)
    else:
        logger.warning(f'[_emit] No SID for node {node_id}, emit {event} skipped')


def _start_ping_loop(socketio):
    """Ping all online agents every 30 seconds."""
    def _ping_loop():
        while True:
            time.sleep(30)
            try:
                nodes = list_nodes()
                for node in nodes:
                    nid = node['node_id']
                    sid = _node_to_sid.get(nid)
                    if sid and node['status'] == 'online':
                        socketio.emit('server:ping', {'node_id': nid}, room=sid)
            except Exception as e:
                logger.error(f'Ping loop error: {e}')

    thread = threading.Thread(target=_ping_loop, daemon=True)
    thread.start()


def register_agent_handlers(socketio, on_connect=None, on_disconnect=None):
    """Register Socket.IO event handlers for agent connections."""

    _start_ping_loop(socketio)

    @socketio.on('agent:connect')
    def handle_agent_connect(data):
        from flask import request as flask_request
        agent_node_id = data.get('node_id')
        node_name = data.get('node_name')
        api_token = data.get('api_token')
        control_mode = data.get('control_mode', 'server')
        agent_config = data.get('config', {})
        agent_ip = flask_request.remote_addr if flask_request else ''
        agent_sid = flask_request.sid if flask_request else None

        if not api_token:
            logger.warning('agent:connect rejected — no api_token')
            return {'status': 'error', 'message': 'Missing api_token'}

        node = get_node_by_token(api_token)

        # Auto-register unknown agent — no manual setup needed
        if not node:
            node = add_node(node_name or agent_node_id or 'Agent', api_token=api_token,
                            ip=agent_ip if agent_ip != '127.0.0.1' else '')
            logger.info(f'Auto-registered new agent: {node_name} ({agent_ip}) token={api_token[:8]}...')
            # Notify browser — agent is already connected via WebSocket
            socketio.emit('node:discovered', {
                'node_id': node['node_id'],
                'name': node['name'],
                'ip': agent_ip,
                'auto_registered': True,
                'already_connected': True,
            })

        node_id = node['node_id']
        # Update IP from WebSocket connection
        if agent_ip and agent_ip != '127.0.0.1':
            update_node(node_id, ip=agent_ip)

        # Track SID mapping for reliable delivery
        if agent_sid:
            _sid_to_node[agent_sid] = node_id
            _node_to_sid[node_id] = agent_sid
            logger.info(f'[connect] Agent SID mapped: {agent_sid} → {node_id} '
                        f'(agent_sent_node_id={data.get("node_id")})')
        else:
            logger.warning(f'[connect] No SID available for agent {node_id}')

        update_node_status(node_id, 'online', agent_config)
        update_node_control_mode(node_id, control_mode)
        # Save agent config (incl. dsm_schemes) to config column
        # so telemetry updates don't overwrite it
        update_node_config(node_id, agent_config)

        agent_version = data.get('version', '') or agent_config.get('config_version', '')
        if agent_version:
            update_node_version(node_id, agent_version)

        if on_connect:
            on_connect(node_id)

        # Telegram notification for agent connect
        tg_enabled = state.get('telegram_enabled', False)
        tg_events = state.get('telegram_events', {})
        if tg_enabled and tg_events.get('agent_status', True):
            send_message(f'🟢 <b>Агент подключён</b>\n{node_name} ({agent_ip})')

        # Push node_id to agent so it uses the registry ID for telemetry
        _emit_to_node(socketio, 'server:node_id_push', {
            'node_id': node_id,
            'token': node['api_token'],
        }, node_id)

        with state_lock:
            prev = state['nodes'].get(node_id, {})
            try:
                db_pending = bool(node.get('pending_update', 0))
                db_auto = bool(node.get('auto_update', 0))
            except Exception as e:
                logger.error(f'[connect] Error reading flags: {e}')
                db_pending = False
                db_auto = False
            # If agent reconnects with matching server version, update is done
            # — clear pending_update. If version doesn't match, keep pending
            # so polling can retry.
            update_done = (agent_version and agent_version == _srv_ver)
            if update_done and db_pending:
                logger.info(f'[connect] Agent {node_id} updated successfully: '
                            f'{prev.get("agent_version", "?")} → {agent_version}')
            clear_pending = update_done or not db_pending
            if clear_pending and db_pending:
                update_node_flags(node_id, pending_update=False)
            new_node = {
                'node_id': node_id,
                'stable_id': node.get('stable_id', ''),
                'name': node['name'],
                'status': 'online',
                'control_mode': control_mode,
                'config': agent_config,
                'dsm_schemes': agent_config.get('dsm_schemes', []),
                'kernel_info': agent_config.get('kernel_info', {}),
                'agent_version': agent_version,
                'auto_update': db_auto,
                'pending_update': False if clear_pending else db_pending,
                'update_started': None,
            }
            state['nodes'][node_id] = new_node
        invalidate_state_cache()

        # Push server config to agent if in server mode
        server_config = node.get('config', {})
        if server_config and control_mode == 'server':
            _emit_to_node(socketio, 'server:config_push', {
                'config': server_config,
            }, node_id)
            logger.info(f'Pushed config to {node["name"]}')

            # Check for conflict on reconnect (strip metadata + runtime fields)
            server_cmp = _strip_runtime(server_config)
            agent_cmp = _strip_runtime(agent_config)
            if server_cmp and agent_cmp and server_cmp != agent_cmp:
                diff_keys = [k for k in set(list(server_cmp) + list(agent_cmp))
                             if server_cmp.get(k) != agent_cmp.get(k)]
                logger.info(f'Config conflict on reconnect for {node["name"]}: {diff_keys}')
                for k in diff_keys:
                    logger.info(f'  field={k} server={repr(server_cmp.get(k))[:200]} '
                                f'agent={repr(agent_cmp.get(k))[:200]}')
                save_agent_snapshot(node_id, agent_config)
                socketio.emit('node:conflict', {
                    'node_id': node_id,
                    'name': node['name'],
                    'server_config': server_config,
                    'agent_config': agent_config,
                })

        socketio.emit('update', {'nodes': dict(state['nodes'])})
        logger.info(f'Agent connected: {node_id} ({node["name"]})')
        return {'status': 'ok', 'node_id': node_id, 'name': node['name']}

    @socketio.on('agent:telemetry')
    def handle_agent_telemetry(data):
        if not isinstance(data, dict):
            return
        agent_node_id = data.get('node_id')
        telemetry = data.get('telemetry', {})
        
        # Payload size limit (1MB)
        if len(str(data)) > 1_000_000:
            logger.warning(f'agent:telemetry DROPPED: payload too large ({len(str(data))} bytes)')
            return

        # Resolve agent's node_id to registry node_id via SID mapping
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None

        logger.info(f'[telemetry-recv] agent_sent={agent_node_id} sid={agent_sid} '
                    f'resolved={node_id} fans={list(telemetry.get("fans", {}).keys())} '
                    f'temps={list(telemetry.get("temp_sensors", {}).keys())}')

        if not node_id:
            # No fallback — if SID mapping fails, drop the telemetry
            # Agent should reconnect to get proper SID mapping
            logger.warning(f'agent:telemetry DROPPED: no SID mapping for sid={agent_sid}')
            return

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:telemetry DROPPED: resolved={node_id} '
                           f'nodes_keys={list(state.get("nodes", {}).keys())} '
                           f'sid_map_keys={list(_sid_to_node.keys())}')
            return

        update_node_status(node_id, 'online', telemetry)

        with state_lock:
            if node_id in state['nodes']:
                # Check for fan health status changes before updating telemetry
                prev_telemetry = state['nodes'][node_id].get('telemetry', {})
                prev_fans = prev_telemetry.get('fans', {})
                new_fans = telemetry.get('fans', {})

                for fan_id, new_fan in new_fans.items():
                    new_health = new_fan.get('health', {})
                    prev_health = prev_fans.get(fan_id, {}).get('health', {})
                    new_h_status = new_health.get('status', 'healthy')
                    prev_h_status = prev_health.get('status', 'healthy')

                    if new_h_status != prev_h_status:
                        label = new_fan.get('label', fan_id)
                        if new_h_status in ('stopped', 'slowing', 'needs_calibration'):
                            socketio.emit('fan:health', {
                                'fan_id': fan_id, 'node_id': node_id,
                                'status': new_h_status, 'label': label,
                                'message': f'[{node_id}] Вентилятор {label}: {new_h_status}',
                            })
                        elif new_h_status == 'healthy' and prev_h_status in ('stopped', 'slowing', 'needs_calibration'):
                            socketio.emit('fan:health:cleared', {
                                'fan_id': fan_id, 'node_id': node_id,
                            })

                state['nodes'][node_id]['status'] = 'online'
                state['nodes'][node_id]['telemetry'] = telemetry
        invalidate_state_cache()

        socketio.emit('node:telemetry', {'node_id': node_id, 'telemetry': telemetry})

    @socketio.on('agent:config_changed')
    def handle_agent_config_changed(data):
        agent_node_id = data.get('node_id')
        agent_config = data.get('config', {})

        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            node_id = agent_node_id

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:config_changed from unknown node: {agent_node_id}')
            return

        # Get server's authoritative config for this node
        node = get_node(node_id)
        server_config = node.get('config', {}) if node else {}

        # Skip conflict detection during grace period after server startup
        if _time.monotonic() - _startup_time < _GRACE_PERIOD:
            logger.debug(f'Grace period active, skipping conflict check for {node_id}')
            return

        # Check for conflict: agent config differs from server config
        server_cmp = _strip_runtime(server_config)
        agent_cmp = _strip_runtime(agent_config)

        if server_cmp and agent_cmp and server_cmp != agent_cmp:
            # Log what actually differs for debugging
            diff_keys = []
            all_keys = set(list(server_cmp.keys()) + list(agent_cmp.keys()))
            for k in all_keys:
                sv = server_cmp.get(k)
                av = agent_cmp.get(k)
                if sv != av:
                    diff_keys.append(k)
                    logger.info(f'[CONFLICT] field={k} server={repr(sv)[:200]} agent={repr(av)[:200]}')
            logger.warning(f'Config conflict for {node_id}: differing fields = {diff_keys}')
            # Save agent's config as snapshot for revert
            save_agent_snapshot(node_id, agent_config)

            # Update node config with agent's changes
            update_node_config(node_id, agent_config)

            with state_lock:
                if node_id in state['nodes']:
                    state['nodes'][node_id]['config'] = agent_config
            invalidate_state_cache()

            # Notify browsers of conflict
            socketio.emit('node:conflict', {
                'node_id': node_id,
                'name': state['nodes'].get(node_id, {}).get('name', node_id),
                'server_config': server_config,
                'agent_config': agent_config,
            })
            logger.info(f'Config conflict detected for {node_id}')
        else:
            # No conflict — just update
            update_node_config(node_id, agent_config)

            with state_lock:
                if node_id in state['nodes']:
                    state['nodes'][node_id]['config'] = agent_config
            invalidate_state_cache()

        socketio.emit('node_config_changed', {'node_id': node_id, 'config': agent_config})
        logger.info(f'Agent config updated: {node_id}')

    @socketio.on('agent:control_mode_changed')
    def handle_agent_control_mode_changed(data):
        agent_node_id = data.get('node_id')
        mode = data.get('mode', 'server')

        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            node_id = agent_node_id

        if not node_id or node_id not in state.get('nodes', {}):
            logger.warning(f'agent:control_mode_changed from unknown node: {agent_node_id}')
            return

        update_node_control_mode(node_id, mode)

        with state_lock:
            if node_id in state['nodes']:
                state['nodes'][node_id]['control_mode'] = mode
        invalidate_state_cache()

        socketio.emit('node_mode_changed', {'node_id': node_id, 'mode': mode})
        logger.info(f'Agent mode changed: {node_id} -> {mode}')

    @socketio.on('agent:pong')
    def handle_agent_pong(data):
        """Agent responds to ping — update last_seen."""
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            node_id = data.get('node_id', '')
        update_node_status(node_id, 'online')

    @socketio.on('server:dsm:apply')
    def handle_server_dsm_apply(data):
        """Forward DSM scheme apply from UI to a remote agent."""
        node_id = data.get('node_id')
        if not node_id or node_id not in state.get('nodes', {}):
            return
        _emit_to_node(socketio, 'agent:dsm:apply', data, node_id)
        logger.info(f'DSM apply forwarded to agent {node_id}')

    @socketio.on('agent:update_result')
    def handle_agent_update_result(data):
        """Agent reports update progress or error."""
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None
        if not node_id:
            return
        status = data.get('status', 'unknown')
        message = data.get('message', '')
        version = data.get('version', '')
        logger.info(f'Agent update result: {node_id} status={status} version={version} msg={message}')
        socketio.emit('agent:update_progress', {
            'node_id': node_id,
            'status': status,
            'message': message,
            'version': version,
        })

    @socketio.on('agent:logs')
    def handle_agent_logs(data):
        """Agent sends log lines — forward to browser."""
        node_id = data.get('node_id', '')
        lines = data.get('lines', [])
        socketio.emit('agent:logs', {
            'node_id': node_id,
            'lines': lines,
        })

    @socketio.on('disconnect')
    def handle_disconnect():
        """Clean up SID mapping on disconnect."""
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        if agent_sid and agent_sid in _sid_to_node:
            nid = _sid_to_node.pop(agent_sid)
            _node_to_sid.pop(nid, None)
            logger.info(f'Agent disconnected: {nid} (SID {agent_sid} released)')


# ==============================================================================
# MODULE: server.routes
# ==============================================================================

"""Flask routes for FanControl Web."""

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict

from flask import Blueprint, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import BadRequest


logger = logging.getLogger('fancontrol')

routes = Blueprint('routes', __name__)

# Rate limiting for control endpoints
_control_rate_limit: Dict[str, float] = {}
CONTROL_RATE_LIMIT_SECONDS = 0.1
_RATE_LIMIT_CLEANUP_INTERVAL = 600
_rate_limit_last_cleanup = time.monotonic()

MAX_HISTORY_HOURS = 168
PWM_CURVE_POINTS = len(CALIBRATION_STEPS)


@routes.route('/')
def index():
    """Serve the main dashboard"""
    resp = render_template('index.html', config_version=CONFIG_VERSION)
    from flask import make_response
    response = make_response(resp)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@routes.route('/js/<path:filename>')
def serve_js(filename):
    """Serve JavaScript files from templates/js"""
    return send_from_directory(os.path.join(os.path.dirname(__file__), '..', 'templates', 'js'), filename)


@routes.route('/api/state')
def api_get_state():
    """REST endpoint for current state (debugging/health checks)"""
    return jsonify(get_state())


@routes.route('/api/kernel')
def api_get_kernel():
    """Detect kernel type and capabilities for fan control."""
    return jsonify(get_kernel_info())


@routes.route('/api/system')
def api_get_system():
    return jsonify(get_system_info())


@routes.route('/api/lang/<code>')
def api_get_lang(code):
    """Serve translation file"""
    if not re.match(r'^[a-z]{2}$', code):
        return jsonify({'error': 'Invalid language code'}), 400
    lang_file = Path(os.path.join(os.path.dirname(__file__), '..', 'static')) / 'lang' / f'{code}.json'
    if lang_file.exists():
        with open(lang_file, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
    return jsonify({}), 404


@routes.route('/api/language', methods=['POST'])
def api_set_language():
    """Save language preference to config"""
    try:
        data = request.get_json(force=True)
        lang = data.get('language', 'en')
        if lang not in ('en', 'ru'):
            return jsonify({"status": "error", "message": "Unsupported language"}), 400
        
        with state_lock:
            state['language'] = lang
        
        save_config()
        return jsonify({"status": "success"})
    except Exception as e:
        logger.error(f'Language save error: {e}', exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


@routes.route('/api/logging', methods=['GET'])
def api_get_logging():
    """Get current log level and retention."""
    from app import get_log_level
    return jsonify({
        'level': get_log_level(),
        'levels': ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        'retention_days': state.get('log_retention_days', 30),
        'retention_options': [7, 14, 30, 60, 90, 180, 365],
    })


@routes.route('/api/logging', methods=['POST'])
def api_set_logging():
    """Set log level and/or retention."""
    try:
        data = request.get_json(force=True)
        result = {}

        level = data.get('level')
        if level:
            from app import set_log_level
            if set_log_level(level):
                result['level'] = level
            else:
                return jsonify({'status': 'error', 'message': f'Invalid level: {level}'}), 400

        retention = data.get('retention_days')
        if retention is not None:
            retention = max(7, min(365, int(retention)))
            with state_lock:
                state['log_retention_days'] = retention
            result['retention_days'] = retention

        save_config()
        return jsonify({'status': 'ok', **result})
    except Exception as e:
        logger.error(f'Logging config error: {e}', exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500


# ─── Telegram notifications ───────────────────────────────────────────

@routes.route('/api/telegram/config', methods=['POST'])
def api_telegram_config():
    """Save Telegram bot configuration and enable/disable notifications."""
    try:
        data = request.get_json(force=True)

        with state_lock:
            if 'bot_token' in data:
                state['telegram_bot_token'] = data['bot_token']
            if 'chat_id' in data:
                state['telegram_chat_id'] = data['chat_id']
            if 'enabled' in data:
                state['telegram_enabled'] = bool(data['enabled'])
            if 'events' in data:
                events = state.get('telegram_events', {})
                events.update(data['events'])
                state['telegram_events'] = events

        # Apply config to telegram module
        configure(state.get('telegram_bot_token', ''), state.get('telegram_chat_id', ''))

        save_config()
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f'Telegram config error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/telegram/test', methods=['POST'])
def api_telegram_test():
    """Send a test message to Telegram."""
    if not is_configured():
        return jsonify({'status': 'error', 'message': 'Telegram not configured'}), 400
    ok = send_message('🧪 <b>FanControl</b>\nТестовое уведомление ✓')
    return jsonify({'status': 'ok' if ok else 'failed'})


@routes.route('/api/telegram/status')
def api_telegram_status():
    """Get Telegram configuration status."""
    return jsonify({
        'configured': is_configured(),
        'enabled': bool(state.get('telegram_enabled', False)),
        'has_token': bool(state.get('telegram_bot_token')),
        'has_chat_id': bool(state.get('telegram_chat_id')),
        'events': state.get('telegram_events', {}),
    })


# ─── End Telegram ─────────────────────────────────────────────────────


@routes.route('/api/server-name', methods=['PUT'])
def api_update_server_name():
    """Update server name and push to all connected clients."""
    try:
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Name required'}), 400
        if len(name) > 64:
            return jsonify({'error': 'Name too long (max 64)'}), 400

        with state_lock:
            state['server_name'] = name

        save_config()
        invalidate_state_cache()

        # Push to all connected clients so UI updates instantly
        from app import socketio
        socketio.emit('server:name_changed', {'name': name})

        # Restart SSDP announcer with new name
        try:
            _restart_ssdp_announcer()
        except Exception as e:
            logger.warning(f'SSDP restart after rename failed: {e}')

        return jsonify({'status': 'ok', 'name': name})
    except Exception as e:
        logger.error(f'Server rename error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/discover', methods=['POST'])
def api_discover():
    """Scan hardware for fans, sensors, and disks"""
    try:
        logger.info("Starting hardware discovery...")
        fans, temps = discover_fans_and_sensors()
        disks = discover_disks()
        
        kernel_info = get_kernel_info()
        
        with state_lock:
            state['fans'] = fans
            state['temp_sensors'] = temps
            state['hdd_sensors'] = disks
            state['hardware_scanned'] = True
            state['kernel_type'] = kernel_info.get('type', 'unknown')
        
        from app import socketio
        socketio.emit('hardware_discovered', {
            'fans': fans,
            'temps': temps,
            'disks': disks,
            'kernel_info': kernel_info,
        })
        
        logger.info(f"Discovery complete: {len(fans)} fans, {len(temps)} sensors, {len(disks)} disks, kernel={kernel_info.get('type')}")
        return jsonify({'status': 'ok', 'fans': fans, 'temps': temps, 'disks': disks, 'kernel_info': kernel_info})
        
    except Exception as e:
        logger.error(f'Discovery failed: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/disks/<disk_id>/smart')
def api_get_disk_smart(disk_id):
    """Get full SMART data for a specific disk"""
    import time as _time

    force_refresh = request.args.get('refresh', '0') == '1'
    now = _time.monotonic()
    with _smart_cache_lock:
        if not force_refresh and disk_id in _smart_cache and (now - _smart_cache_time.get(disk_id, 0)) < SMART_CACHE_TTL:
            return jsonify(_smart_cache[disk_id])

    with state_lock:
        disk = state.get('hdd_sensors', {}).get(disk_id)
        if not disk:
            return jsonify({'error': 'Disk not found'}), 404

        device = disk.get('device', '')
        if not device:
            return jsonify({'error': 'No device path'}), 404

    result = read_disk_smart(device)

    if 'error' not in result:
        with _smart_cache_lock:
            _smart_cache[disk_id] = result
            _smart_cache_time[disk_id] = now
            # Evict stale entries to prevent unbounded growth
            stale = [k for k, t in _smart_cache_time.items()
                     if (now - t) > SMART_CACHE_TTL * 2]
            for k in stale:
                _smart_cache.pop(k, None)
                _smart_cache_time.pop(k, None)

    return jsonify(result)


@routes.route('/api/nodes/<node_id>/disks/<disk_id>/smart')
def api_proxy_disk_smart(node_id, disk_id):
    """Proxy SMART request to a remote agent. Looks up by stable_id or node_id."""
    import logging
    logger = logging.getLogger('fancontrol')
    node = get_node_by_stable_id(node_id) or get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    ip = node.get('ip') or ''
    if not ip:
        # Try to get IP from the agent's last known connection
        logger.warning(f'SMART proxy: node {node_id} has no IP stored')
        return jsonify({'error': f'Node IP unknown for {node_id}'}), 400
        return jsonify({'error': 'Node IP unknown'}), 400
    port = node.get('port', 5059)
    try:
        import urllib.request, json
        url = f'http://{ip}:{port}/api/agent/disks/{disk_id}/smart'
        logger.info(f'Proxying SMART request to {url}')
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'FanControl-Web')
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            logger.info(f'SMART proxy result for {disk_id}: has_attrs={bool(data.get("attributes"))}')
            return jsonify(data)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='ignore')
        logger.error(f'SMART proxy HTTP error: {e.code} {body[:200]}')
        return jsonify({'error': f'Agent returned {e.code}: {body[:200]}'}), e.code
    except Exception as e:
        logger.error(f'SMART proxy error: {e}')
        return jsonify({'error': str(e)}), 502


@routes.route('/api/initialize', methods=['POST'])
def api_initialize():
    """Start fan calibration"""
    try:
        if state.get('testing'):
            return jsonify({'status': 'error', 'message': 'Calibration already running'}), 409
        
        with state_lock:
            state['testing'] = True
            state['test_progress'] = {
                'status': 'Starting calibration...',
                'step': 0,
                'total': PWM_CURVE_POINTS,
                'current': ''
            }
        
        from app import socketio
        threading.Thread(
            target=test_fans,
            kwargs={'socketio': socketio, 'save_config_fn': save_config},
            daemon=True
        ).start()
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        logger.error(f'Initialization error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/skip-calibration', methods=['POST'])
def api_skip_calibration():
    """Mark setup complete without calibration (monitoring-only mode)."""
    with state_lock:
        state['initialized'] = True
        state['tested'] = True
    save_config()
    return jsonify({'status': 'ok'})


@routes.route('/api/dsm/fan-speed', methods=['POST'])
def api_set_dsm_fan_speed():
    """Set DSM fan speed via scemd.xml."""
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available (scemd.xml not found)'}), 400

    data = request.get_json(silent=True) or {}
    speed = data.get('speed', 50)
    speed = max(0, min(100, int(speed)))

    if set_dsm_fan_speed(speed):
        with state_lock:
            for fan_id, fan in state['fans'].items():
                if fan.get('control_method') == 'dsm_scemd':
                    fan['manual_pct'] = speed
                    fan['pwm_value'] = int(speed * 255 / 100)
        save_config()
        return jsonify({'status': 'ok', 'speed': speed})
    else:
        return jsonify({'status': 'error', 'message': 'Failed to set fan speed'}), 500


# ============================================================================
# DSM Scheme Management
# ============================================================================

@routes.route('/api/dsm/schemes', methods=['GET'])
def api_get_dsm_schemes():
    """Return all fan_config schemes from scemd.xml."""
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    info = get_all_schemes()
    if info is None:
        return jsonify({'status': 'error', 'message': 'Failed to parse scemd.xml'}), 500
    return jsonify({'status': 'ok', **info})


@routes.route('/api/dsm/scheme/<scheme_type>', methods=['GET'])
def api_get_dsm_scheme(scheme_type):
    """Return a single scheme by type."""
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    scheme = get_scheme(scheme_type)
    if scheme is None:
        return jsonify({'status': 'error', 'message': f'Scheme {scheme_type} not found'}), 404
    return jsonify({'status': 'ok', 'scheme': scheme})


@routes.route('/api/dsm/scheme/<scheme_type>', methods=['PUT'])
def api_update_dsm_scheme(scheme_type):
    """Update a scheme's entries."""
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    data = request.get_json(silent=True) or {}
    entries = data.get('entries')
    if not entries or not isinstance(entries, list):
        return jsonify({'status': 'error', 'message': 'entries array required'}), 400

    if update_scheme(scheme_type, entries):
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'Failed to update scheme'}), 500


@routes.route('/api/dsm/scheme/<scheme_type>/entry/<int:index>', methods=['PUT'])
def api_update_dsm_entry(scheme_type, index):
    """Update a single entry in a scheme."""
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    data = request.get_json(silent=True) or {}
    fan_speed = data.get('fan_speed_pct')
    action = data.get('action')
    threshold = data.get('threshold_temp')

    if update_scheme_entry(scheme_type, index,
                           fan_speed_pct=fan_speed,
                           action=action,
                           threshold_temp=threshold):
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'Failed to update entry'}), 500


@routes.route('/api/dsm/active', methods=['GET'])
def api_get_dsm_active():
    """Return the currently active scheme type."""
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    active = get_active_scheme_type()
    return jsonify({'status': 'ok', 'active_scheme': active})


@routes.route('/api/dsm/apply', methods=['POST'])
def api_apply_dsm_schemes():
    """Write pending changes and restart scemd service."""
    if not is_dsm_fan_available():
        return jsonify({'status': 'error', 'message': 'DSM fan control not available'}), 400

    if _restart_scemd():
        return jsonify({'status': 'ok', 'message': 'scemd service restarted'})
    return jsonify({'status': 'error', 'message': 'Failed to restart scemd service'}), 500


@routes.route('/api/test/start', methods=['POST'])
def api_test_start():
    """Start individual fan test"""
    try:
        data = request.get_json(silent=True) or {}
        fan_key = data.get('fan')
        
        if state.get('testing'):
            return jsonify({'status': 'error', 'message': 'Test already running'}), 409
        
        with state_lock:
            state['testing'] = True
            state['test_progress'] = {
                'status': 'Starting test...',
                'step': 0,
                'total': PWM_CURVE_POINTS,
                'current': ''
            }
        
        from app import socketio
        threading.Thread(
            target=test_fans,
            args=(fan_key,),
            kwargs={'socketio': socketio, 'save_config_fn': save_config},
            daemon=True
        ).start()
        return jsonify({'status': 'ok'})
        
    except Exception as e:
        logger.error(f'Test start error: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/history')
def api_history():
    """Return history data optimized for ApexCharts"""
    try:
        hours = max(1, min(request.args.get('hours', 24, type=int), MAX_HISTORY_HOURS))
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        with get_db_connection() as conn:
            cursor = conn.execute(
                'SELECT ts, mode, pwm, rpm, max_temp FROM logs WHERE ts > ? ORDER BY ts',
                (since,)
            )
            rows = cursor.fetchall()
        
        timestamps = []
        temps = []
        pwm_speeds = []
        
        for row in rows:
            timestamps.append(row[0])
            temps.append(row[4] if row[4] is not None and row[4] > 0 else None)
            pwm_speeds.append(row[2])
        
        return jsonify({
            'has_data': len(timestamps) > 0,
            'timestamps': timestamps,
            'temps': temps,
            'pwm': pwm_speeds
        })
        
    except sqlite3.OperationalError as e:
        logger.error(f'Database read error: {e}')
        return jsonify({'has_data': False, 'timestamps': [], 'temps': [], 'pwm': []}), 503


@routes.route('/api/update/check')
def api_update_check():
    """Check for updates — compare local vs remote git hash."""

    current_version = CONFIG_VERSION
    remote_version = ''
    remote_hash = ''
    commit_msg = ''
    local_hash = ''

    repo_dir = '/repo'

    # Get local hash from running code
    try:
        local_result = subprocess.run(
            ['git', '-C', repo_dir, 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        local_hash = local_result.stdout.strip()
    except Exception:
        pass

    # Fetch latest from remote and compare
    try:
        fetch = subprocess.run(
            ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        if fetch.returncode != 0:
            logger.error(f'Update check: git fetch failed: {fetch.stderr[:200]}')
    except Exception as e:
        logger.error(f'Update check: git fetch error: {e}')

    # Get remote hash
    try:
        remote_result = subprocess.run(
            ['git', '-C', repo_dir, 'rev-parse', '--short', 'origin/main'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        remote_hash = remote_result.stdout.strip()
    except Exception:
        pass

    # Get remote commit message
    try:
        msg_result = subprocess.run(
            ['git', '-C', repo_dir, 'log', '--oneline', '-1', 'origin/main'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        commit_msg = msg_result.stdout.strip()
    except Exception:
        pass

    # Read remote CONFIG_VERSION from fetched code
    try:
        ver_result = subprocess.run(
            ['git', '-C', repo_dir, 'show', f'origin/main:core/state.py'],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        m = re.search(r"CONFIG_VERSION\s*=\s*['\"](.+?)['\"]", ver_result.stdout)
        if m:
            remote_version = m.group(1)
    except Exception:
        pass

    has_update = bool(remote_hash and local_hash and remote_hash != local_hash)
    logger.info(f'[CHECK] local={local_hash}, remote={remote_hash}, has_update={has_update}')

    return jsonify({
        'status': 'ok',
        'has_update': has_update,
        'current_version': current_version,
        'remote_version': remote_version or current_version,
        'current_hash': local_hash or 'N/A',
        'remote_hash': remote_hash or 'N/A',
        'commit_message': commit_msg
    })


@routes.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Pull latest code, sync to /app, then exit process."""
    # Only require auth if FANCONTROL_UPDATE_TOKEN is explicitly configured
    if cfg.update_token:
        provided = request.headers.get('X-Update-Token') or request.args.get('token')
        if provided != cfg.update_token:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401

    try:

        repo_dir = '/repo'
        app_dir = '/app'

        logger.info(f'[UPDATE] ====== START ====== PID={os.getpid()} VERSION={CONFIG_VERSION}')

        if not os.path.isdir(repo_dir) or not os.path.isfile(os.path.join(repo_dir, 'app.py')):
            return jsonify({'status': 'error', 'message': '/repo not ready'}), 500

        success, version = do_git_pull(repo_dir)
        if not success:
            return jsonify({'status': 'error', 'message': 'git pull failed'}), 500
        logger.info(f'[UPDATE] /repo version after pull: {version}')

        sync_repo_to_app(repo_dir, app_dir)

        schedule_restart(delay=1.0)

        return jsonify({'status': 'ok', 'message': 'Synced. Restarting in 1s...'})

    except Exception as e:
        logger.error(f'[UPDATE] ERROR: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@routes.route('/api/update/agents', methods=['POST'])
def api_update_agents():
    """Send update command to all online agents via WebSocket."""
    from app import socketio

    logger.info('[AGENTS-UPDATE] Endpoint called')

    data = request.get_json(silent=True) or {}
    node_ids = data.get('node_ids')  # Optional: specific nodes, or None for all

    with state_lock:
        nodes = dict(state.get('nodes', {}))

    logger.info(f'[AGENTS-UPDATE] state[nodes] has {len(nodes)} entries, '
                f'_node_to_sid has {len(_node_to_sid)} entries')

    updated = []
    skipped = []
    no_sid = []
    already_ok = []
    for nid, node in nodes.items():
        status = node.get('status', '?')
        has_sid = nid in _node_to_sid
        agent_ver = node.get('agent_version', '')
        logger.info(f'[AGENTS-UPDATE] node={nid} status={status} has_sid={has_sid} '
                    f'version={agent_ver}')
        if node_ids and nid not in node_ids:
            continue
        if status != 'online':
            skipped.append(nid)
            continue
        # Skip agents already at the correct version
        if agent_ver and agent_ver == CONFIG_VERSION:
            # Clear any stale pending_update flag
            with state_lock:
                state['nodes'].get(nid, {})['pending_update'] = False
                state['nodes'].get(nid, {})['update_started'] = None
            update_node_flags(nid, pending_update=False)
            already_ok.append(nid)
            logger.info(f'[AGENTS-UPDATE] Agent {nid} already at {CONFIG_VERSION} — skipped')
            continue
        # Set pending_update — agent polling will pick it up even if WebSocket fails
        import time as _time
        with state_lock:
            state['nodes'].get(nid, {})['pending_update'] = True
            state['nodes'].get(nid, {})['update_started'] = _time.time()
        update_node_flags(nid, pending_update=True)
        if not has_sid:
            no_sid.append(nid)
            logger.warning(f'[AGENTS-UPDATE] Agent {nid} has no SID — update via polling fallback')
            updated.append(nid)
            continue
        _emit_to_node(socketio, 'server:update', {}, nid)
        updated.append(nid)
        logger.info(f'[AGENTS-UPDATE] Sent update to {nid} ({node.get("name")})')

    if updated:
        logger.info(f'[AGENTS-UPDATE] Sent update to {len(updated)} agent(s)')
    if already_ok:
        logger.info(f'[AGENTS-UPDATE] {len(already_ok)} agent(s) already up to date')

    logger.info(f'[AGENTS-UPDATE] Result: updated={updated}, skipped={skipped}, '
                f'no_sid={no_sid}, already_ok={already_ok}')
    return jsonify({
        'status': 'ok',
        'updated': updated,
        'skipped': skipped,
        'no_sid': no_sid,
        'already_ok': already_ok,
        'message': f'Update sent to {len(updated)} agent(s), {len(skipped)} offline, '
                   f'{len(no_sid)} no SID, {len(already_ok)} already up to date'
    })


@routes.route('/api/nodes/<node_id>/request-logs', methods=['POST'])
def api_request_agent_logs(node_id):
    """Request log lines from a remote agent via WebSocket."""
    from app import socketio

    if node_id not in state.get('nodes', {}):
        return jsonify({'error': 'Node not found'}), 404
    if node_id not in _node_to_sid:
        return jsonify({'error': 'Agent not connected'}), 503

    lines = (request.get_json(silent=True) or {}).get('lines', 100)
    _emit_to_node(socketio, 'server:request_logs', {'lines': lines}, node_id)
    return jsonify({'status': 'ok', 'message': 'Log request sent'})


@routes.route('/api/update/poll', methods=['POST'])
def api_update_poll():
    """Agent polls to check if an update is needed.

    Agent sends {agent_version, node_id}.
    Server responds with {update_available, server_version, should_update}.
    should_update = update_available AND (auto_update OR pending_update).
    """
    data = request.get_json(silent=True) or {}
    agent_version = data.get('agent_version', '')
    node_id = data.get('node_id', '')

    with state_lock:
        node = state.get('nodes', {}).get(node_id, {})

    auto_update = node.get('auto_update', False)
    pending = node.get('pending_update', False)
    version_mismatch = agent_version and agent_version != CONFIG_VERSION
    should_update = version_mismatch and (auto_update or pending)

    # Don't consume pending_update here — clear it only when agent
    # reconnects with matching version (handled in agent:connect).
    # This allows retry on git fetch/reset failures.

    logger.info(f'[POLL] node={node_id} v={agent_version}→{CONFIG_VERSION} '
                f'mismatch={version_mismatch} auto={auto_update} pending={pending} '
                f'should_update={should_update}')
    return jsonify({
        'update_available': version_mismatch,
        'should_update': should_update,
        'server_version': CONFIG_VERSION,
    })


@routes.route('/api/nodes/<node_id>/auto-update', methods=['POST'])
def toggle_auto_update(node_id):
    """Toggle auto-update for a specific agent node."""
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled', False)
    with state_lock:
        if node_id in state.get('nodes', {}):
            state['nodes'][node_id]['auto_update'] = enabled
    update_node_flags(node_id, auto_update=enabled)
    logger.info(f'[AUTO-UPDATE] node={node_id} auto_update={enabled}')
    return jsonify({'status': 'ok', 'node_id': node_id, 'auto_update': enabled})


@routes.route('/api/control', methods=['POST'])
def handle_control():
    """Handle fan control commands with validation"""
    global _rate_limit_last_cleanup
    try:
        data = request.get_json(force=True)
        validate_control_request(data)
        
        fan_key = data.get('fan', '')
        now = time.monotonic()
        last_time = _control_rate_limit.get(fan_key, 0)
        if now - last_time < CONTROL_RATE_LIMIT_SECONDS:
            return jsonify({"status": "error", "message": "Rate limit exceeded"}), 429
        _control_rate_limit[fan_key] = now
        
        if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
            expired = [k for k, t in _control_rate_limit.items() if now - t > 60]
            for k in expired:
                del _control_rate_limit[k]
            # Safety limit: if dict grows beyond 1000 entries, clear all but recent 100
            if len(_control_rate_limit) > 1000:
                sorted_items = sorted(_control_rate_limit.items(), key=lambda x: x[1], reverse=True)
                _control_rate_limit.clear()
                _control_rate_limit.update(sorted_items[:100])
            _rate_limit_last_cleanup = now
        
        if data['action'] == 'set_fan_pwm':
            return _handle_set_pwm(data)
        elif data['action'] == 'set_fan_config':
            return _handle_set_config(data)
        else:
            return jsonify({"status": "error", "message": f"Unknown action: {data['action']}"}), 400
            
    except BadRequest as br:
        return jsonify({"status": "error", "message": br.description}), 400
    except Exception as e:
        logger.error(f'Control error: {e}', exc_info=True)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


def _handle_set_pwm(data: dict) -> dict:
    """Handle PWM change request atomically"""
    fan_key = data['fan']
    pwm_val = int(data['pwm'])
    physical_pwm = int(pwm_val * 255 // 100)
    
    with state_lock:
        fan = state['fans'].get(fan_key)
        if not fan:
            raise BadRequest(f"Fan '{fan_key}' not found")
        
        updated_fan = fan.copy()
        updated_fan['manual_pct'] = pwm_val
        updated_fan['mode'] = 'manual'
        updated_fan['status'] = 'nominal'
        state['fans'][fan_key] = updated_fan
    
    set_pwm(fan_key, physical_pwm)
    save_config()
    return jsonify({"status": "success"})


@routes.route('/api/fan/<fan_id>/calibration', methods=['POST'])
def api_fan_calibration(fan_id):
    """Save calibration params (min_pwm, max_pwm, lambda) for a fan."""
    data = request.get_json(silent=True) or {}

    with state_lock:
        if fan_id not in state.get('fans', {}):
            return jsonify({'error': 'Fan not found'}), 404

        fan = state['fans'][fan_id]
        if 'calibration' not in fan:
            fan['calibration'] = {}

        for key in ('min_pwm', 'max_pwm', 'lambda'):
            if key in data:
                fan['calibration'][key] = data[key]

        # Clear calibration_required after successful calibration
        health = fan.get('health', {})
        if health.get('calibration_required'):
            health['calibration_required'] = False
            health['status'] = 'healthy'
            health['rpm_baseline'] = 0

    save_config()
    return jsonify({'status': 'saved'})


@routes.route('/api/fan/<fan_id>/service', methods=['POST'])
def api_fan_service(fan_id):
    """Record fan replacement or service event."""
    data = request.get_json(force=True) or {}
    action = data.get('action', 'service')
    date = data.get('date', datetime.now().isoformat() if 'datetime' in dir() else '')

    with state_lock:
        fan = state.get('fans', {}).get(fan_id)
        if not fan:
            return jsonify({'error': 'Fan not found'}), 404

        health = fan.get('health', {})
        health['last_service_date'] = date
        health['calibration_required'] = True
        health['status'] = 'needs_calibration'
        health['rpm_baseline'] = 0
        health['slowdown_since'] = None
        health['stopped_since'] = None

    save_config()
    return jsonify({'status': 'ok', 'health': health})


@routes.route('/api/dashboard', methods=['GET'])
def api_get_dashboard():
    """Get dashboard layout."""
    return jsonify(state.get('dashboard', {'groups': [], 'cards': []}))


@routes.route('/api/dashboard', methods=['POST'])
def api_save_dashboard():
    """Save dashboard layout (cards, groups, positions, hidden sensors)."""
    data = request.get_json(silent=True) or {}
    with state_lock:
        old_hidden = state.get('dashboard', {}).get('hiddenSensors', [])
        state['dashboard'] = {
            'groups': data.get('groups', []),
            'cards': data.get('cards', []),
            'hiddenSensors': data.get('hiddenSensors', old_hidden)
        }
        new_hidden = state['dashboard']['hiddenSensors']
    save_config()
    if old_hidden != new_hidden:
        from app import socketio
        socketio.emit('hidden_sensors', {'hiddenSensors': new_hidden})
    return jsonify({'status': 'saved'})


def _handle_set_config(data: dict) -> dict:
    """Handle fan configuration change atomically"""
    fan_key = data['fan']
    
    with state_lock:
        fan = state['fans'].get(fan_key)
        if not fan:
            raise BadRequest(f"Fan '{fan_key}' not found")
        
        updated_fan = fan.copy()
        
        for key in ['schedule', 'sensors', 'sensor_mode', 'target_temp']:
            if key in data:
                updated_fan[key] = data[key]
        
        if 'fan_mode' in data:
            updated_fan['mode'] = data['fan_mode']
        
        state['fans'][fan_key] = updated_fan
        
        should_set_manual = (data.get('fan_mode') == 'manual')
        captured_manual_pct = updated_fan.get('manual_pct', 50)
    
    if should_set_manual:
        set_pwm(fan_key, int(captured_manual_pct * 255 // 100))
    
    save_config()
    return jsonify({"status": "success"})


def validate_control_request(data: Dict):
    """Validate incoming control request"""
    if not data or not isinstance(data, dict):
        raise BadRequest("Invalid JSON payload structure")
    
    action = data.get('action')
    if action not in ['set_fan_pwm', 'set_fan_config']:
        raise BadRequest(f"Unsupported action: {action}")
    
    fan_key = data.get('fan')
    if not fan_key:
        raise BadRequest("Missing fan identifier")

    if fan_key not in state.get('fans', {}):
        raise BadRequest(f"Fan '{fan_key}' not found")
    
    if action == 'set_fan_pwm':
        pwm_val = data.get('pwm')
        if pwm_val is None or not isinstance(pwm_val, (int, float)):
            raise BadRequest("PWM value must be a number")
        if not (0 <= pwm_val <= 100):
            raise BadRequest("PWM value must be between 0 and 100")
    
    elif action == 'set_fan_config':
        if 'target_temp' in data:
            t = data['target_temp']
            if not isinstance(t, (int, float)) or not (20 <= t <= 60):
                raise BadRequest("target_temp must be between 20 and 60")
        
        if 'sensor_mode' in data:
            if data['sensor_mode'] not in ('max', 'min', 'avg'):
                raise BadRequest("sensor_mode must be 'max', 'min', or 'avg'")
        
        if 'schedule' in data:
            if not isinstance(data['schedule'], list):
                raise BadRequest("Schedule must be a list of rules")
            valid_days = {'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun', 'all', 'weekday', 'weekend'}
            valid_modes = {'auto', 'off', 'manual'}
            for rule in data['schedule']:
                if not isinstance(rule, dict) or 'mode' not in rule:
                    raise BadRequest("Invalid rule structure in schedule")
                if rule.get('mode') not in valid_modes:
                    raise BadRequest(f"Schedule mode must be one of: {valid_modes}")
                if rule.get('day') and rule['day'] not in valid_days:
                    raise BadRequest(f"Invalid day: {rule['day']}")
                if rule['mode'] == 'manual' and 'speed_pct' in rule:
                    speed = rule['speed_pct']
                    if not isinstance(speed, (int, float)) or not (0 <= speed <= 100):
                        raise BadRequest("Schedule speed_pct must be between 0 and 100")
                if rule['mode'] == 'auto' and 'target_temp' in rule:
                    t = rule['target_temp']
                    if not isinstance(t, (int, float)) or not (20 <= t <= 60):
                        raise BadRequest("Schedule target_temp must be between 20 and 60")
                if 'sensor_mode' in rule and rule['sensor_mode'] not in ('max', 'min', 'avg'):
                    raise BadRequest("Schedule sensor_mode must be 'max', 'min', or 'avg'")
                if 'sensors' in rule and not isinstance(rule['sensors'], list):
                    raise BadRequest("Schedule sensors must be a list")


# ============================================================================
# NODE MANAGEMENT API
# ============================================================================

@routes.route('/api/nodes')
def api_list_nodes():
    """List all registered nodes."""
    return jsonify(list_nodes())


@routes.route('/api/nodes', methods=['POST'])
def api_add_node():
    """Add a new node."""
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'Name required'}), 400
        node = add_node(name)
        return jsonify(node), 201
    except Exception as e:
        logger.error(f'api_add_node error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/nodes/<node_id>')
def api_get_node(node_id):
    """Get node details."""
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    return jsonify(node)


@routes.route('/api/nodes/<node_id>', methods=['PUT'])
def api_update_node(node_id):
    """Update a node (name, ip, port, api_token)."""
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404

    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    ip = data.get('ip', '').strip()
    port = data.get('port')
    api_token = data.get('api_token', '').strip()

    if update_node(node_id, name=name or None, ip=ip if ip is not None else None,
                   port=port, api_token=api_token or None):
        # Update in-memory state so next snapshot reflects the change immediately
        with state_lock:
            if node_id in state.get('nodes', {}):
                if name:
                    state['nodes'][node_id]['name'] = name
                if ip:
                    state['nodes'][node_id]['ip'] = ip
        invalidate_state_cache()
        return jsonify({'status': 'ok'})
    return jsonify({'error': 'Update failed'}), 500


@routes.route('/api/nodes/<node_id>', methods=['DELETE'])
def api_delete_node(node_id):
    """Delete a node."""
    if delete_node(node_id):
        with state_lock:
            state.get('nodes', {}).pop(node_id, None)
        invalidate_state_cache()
        return jsonify({'status': 'deleted'})
    return jsonify({'error': 'Node not found'}), 404


@routes.route('/api/nodes/<node_id>/config', methods=['POST'])
def api_push_config(node_id):
    """Push config to agent."""
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    update_node_config(node_id, data.get('config', {}))
    from app import socketio
    _emit_to_node(socketio, 'server:config_push', {
        'config': data.get('config', {}),
    }, node_id)
    return jsonify({'status': 'pushed'})


@routes.route('/api/nodes/<node_id>/mode', methods=['POST'])
def api_set_node_mode(node_id):
    """Set agent control mode."""
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'server')
    if mode not in ('server', 'manual'):
        return jsonify({'error': 'Invalid mode'}), 400
    update_node_control_mode(node_id, mode)
    from app import socketio
    _emit_to_node(socketio, 'server:set_control_mode', {
        'mode': mode,
    }, node_id)
    return jsonify({'mode': mode})


@routes.route('/api/nodes/discover')
def api_discover_nodes():
    """Scan LAN for agents via SSDP + HTTP probe of offline nodes."""
    nodes = scan_for_agents(timeout=3)
    # Also probe offline nodes directly via HTTP
    probed = probe_known_agents(timeout=2)
    # Merge: SSDP results first, then newly-probed online nodes
    found_ids = {n['node_id'] for n in nodes}
    for p in probed:
        if p['node_id'] not in found_ids:
            nodes.append(p)
    return jsonify(nodes)


@routes.route('/api/nodes/scan-subnet', methods=['POST'])
def api_scan_subnet():
    """Fast TCP scan of local subnet for FanControl agents on port 5059."""
    try:
        data = request.get_json(silent=True) or {}
        port = int(data.get('port', 5059))
        results = scan_subnet(port=port)

        # Mark already-registered agents
        existing_nodes = list_nodes()
        existing_ips = {n['ip']: n for n in existing_nodes if n.get('ip')}
        for r in results:
            ip = r.get('ip', '')
            if ip in existing_ips:
                r['already_registered'] = True
                r['node_id'] = existing_ips[ip]['node_id']
                r['name'] = existing_ips[ip]['name']
            else:
                r['already_registered'] = False

        return jsonify(results)
    except Exception as e:
        logger.error(f'Subnet scan error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


@routes.route('/api/nodes/probe', methods=['POST'])
def api_probe_ip():
    """Probe a specific IP for an agent."""
    data = request.get_json(silent=True) or {}
    ip = (data.get('ip') or '').strip()
    port = int(data.get('port', 5059))
    if not ip:
        return jsonify({'error': 'IP required'}), 400

    info = probe_agent(ip, port=port, timeout=3)
    if not info:
        return jsonify({'error': 'Agent not reachable'}), 404

    # Check if this agent is already registered
    nodes = list_nodes()
    existing = None
    for n in nodes:
        if n.get('ip') == ip:
            existing = n
            break

    if existing:
        # Update status to online
        update_node_status(existing['node_id'], 'online')
        info['node_id'] = existing['node_id']
        info['name'] = existing['name']
        info['already_registered'] = True
    else:
        info['already_registered'] = False

    return jsonify(info)


@routes.route('/api/nodes/add-by-ip', methods=['POST'])
def api_add_node_by_ip():
    """Add a node by IP address directly."""
    data = request.get_json(silent=True) or {}
    ip = (data.get('ip') or '').strip()
    name = (data.get('name') or '').strip()
    port = int(data.get('port', 5059))
    if not ip:
        return jsonify({'error': 'IP required'}), 400
    if not name:
        name = ip

    # Check for duplicate IP
    for n in list_nodes():
        if n.get('ip') == ip:
            return jsonify({'error': 'Node with this IP already exists'}), 409

    info = probe_agent(ip, port=port, timeout=3)

    # Fetch api_token from agent via HTTP
    api_token = ''
    if info:
        try:
            import urllib.request
            import json
            resp = urllib.request.urlopen(f'http://{ip}:{port}/api/agent/status', timeout=5)
            status = json.loads(resp.read())
            api_token = status.get('api_token', '')
        except Exception:
            pass

    node = add_node(name, api_token=api_token, ip=ip)

    update_node_status(node['node_id'], 'online' if info else 'offline')

    return jsonify(node), 201


# ============================================================================
# DISCOVERED AGENTS API
# ============================================================================

@routes.route('/api/discovered')
def api_list_discovered():
    """List discovered but unregistered agents."""
    with _lock:
        return jsonify(list(_discovered_nodes.values()))


@routes.route('/api/discovered/<node_id>/accept', methods=['POST'])
def api_accept_discovered(node_id):
    """Accept a discovered agent and register it.

    Fetches the api_token from the agent's /api/agent/status endpoint
    over unicast HTTP (token is no longer broadcast via SSDP).
    """
    try:
        import urllib.request

        with _lock:
            agent = _discovered_nodes.get(node_id)
            if not agent:
                return jsonify({'error': 'Agent not found'}), 404

            agent_ip = agent.get('ip', '')
            agent_name = agent.get('name', node_id)

        # Fetch api_token from agent via unicast HTTP
        api_token = ''
        try:
            url = f'http://{agent_ip}:5059/api/agent/status'
            req = urllib.request.urlopen(url, timeout=5)
            import json
            status = json.loads(req.read())
            api_token = status.get('api_token', '')
        except Exception as e:
            logger.warning(f'Could not fetch token from agent {agent_ip}: {e}')
            return jsonify({'error': f'Could not reach agent at {agent_ip}'}), 502

        node = add_node(agent_name, api_token=api_token, ip=agent_ip)

        with _lock:
            _discovered_nodes.pop(node_id, None)

        return jsonify(node), 201
    except Exception as e:
        logger.error(f'api_accept_discovered error: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Diagnostic endpoints
# ============================================================================

@routes.route('/api/health', methods=['GET'])
def api_health():
    """Quick health check — server version, agent versions, pending agents."""

    with state_lock:
        nodes = dict(state.get('nodes', {}))

    agents = []
    pending_agents = []
    for nid, node in nodes.items():
        info = {
            'node_id': nid,
            'version': node.get('agent_version', '?'),
            'status': node.get('status', '?'),
            'pending': bool(node.get('pending_update', 0)),
            'auto_update': bool(node.get('auto_update', 0)),
            'connected': nid in _node_to_sid,
        }
        agents.append(info)
        if info['pending']:
            pending_agents.append(nid)

    return jsonify({
        'server_version': CONFIG_VERSION,
        'agents': agents,
        'pending_agents': pending_agents,
        'total_agents': len(agents),
    })


@routes.route('/api/debug', methods=['GET'])
def api_debug():
    """Detailed diagnostic info — versions, state, config, recent logs."""

    with state_lock:
        nodes = dict(state.get('nodes', {}))

    agents = []
    for nid, node in nodes.items():
        info = {
            'node_id': nid,
            'name': node.get('name', '?'),
            'version': node.get('agent_version', '?'),
            'status': node.get('status', '?'),
            'pending_update': bool(node.get('pending_update', 0)),
            'auto_update': bool(node.get('auto_update', 0)),
            'control_mode': node.get('control_mode', '?'),
            'sid': _node_to_sid.get(nid, None),
            'last_seen': node.get('last_seen', '?'),
            'ip': node.get('ip', '?'),
        }
        agents.append(info)

    # Server state
    git_hash = ''
    try:
        result = subprocess.run(
            ['git', '-C', '/repo', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5,
        )
        git_hash = result.stdout.strip()
    except Exception:
        pass

    # Pending SQLite flags
    pending_db = {}
    try:
        for n in list_nodes():
            if n.get('pending_update'):
                pending_db[n['node_id']] = True
    except Exception:
        pass

    return jsonify({
        'server': {
            'version': CONFIG_VERSION,
            'git_hash': git_hash,
            'data_dir': str(DATA_DIR),
            'uptime': _get_uptime(),
        },
        'agents': agents,
        'sid_map': {nid: sid[:8] + '...' for nid, sid in _node_to_sid.items()},
        'pending_in_db': pending_db,
        'state_keys': list(state.keys()),
    })


def _get_uptime():
    """Get process uptime."""
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF)
        return f'{r.ru_utime + r.ru_stime:.1f}s cpu'
    except Exception:
        return '?'




# ==============================================================================
# MODULE: agent.announcer
# ==============================================================================

"""SSDP announcer — broadcasts agent presence on LAN."""

import logging
import socket
import threading
import time
from typing import Optional

logger = logging.getLogger('fancontrol')

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
SSDP_INTERVAL = 60


def _build_ssdp_response(node_id: str, node_name: str, port: int = 5059) -> str:
    ip = _get_local_ip()
    return (
        'HTTP/1.1 200 OK\r\n'
        'CACHE-CONTROL: max-age=60\r\n'
        'EXT: \r\n'
        f'LOCATION: http://{ip}:{port}\r\n'
        'SERVER: FanControl-Web/3.4.1\r\n'
        f'USN: urn:fancontrol-web:agent:{node_id}\r\n'
        'ST: urn:fancontrol-web:agent\r\n'
        f'X-FanControl-Name: {node_name}\r\n'
        f'X-FanControl-Id: {node_id}\r\n'
        '\r\n'
    )


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((SSDP_ADDR, SSDP_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def start_announcer(node_id: str, node_name: str, port: int = 5059) -> Optional[threading.Thread]:
    def _announce_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            response = _build_ssdp_response(node_id, node_name, port)

            logger.info(f'SSDP announcer started for {node_name}')

            while True:
                try:
                    sock.sendto(response.encode(), (SSDP_ADDR, SSDP_PORT))
                except Exception as e:
                    logger.debug(f'SSDP send failed: {e}')
                time.sleep(SSDP_INTERVAL)
        except Exception as e:
            logger.error(f'SSDP announcer error: {e}')

    thread = threading.Thread(target=_announce_loop, daemon=True)
    thread.start()
    return thread


def _handle_msearch(node_id: str, node_name: str, port: int = 5059):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(('', SSDP_PORT))

        mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton('0.0.0.0')
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        response = _build_ssdp_response(node_id, node_name, port)
        logger.info(f'M-SEARCH responder started on port {SSDP_PORT} for {node_name}')

        while True:
            data, addr = sock.recvfrom(1024)
            if b'M-SEARCH' in data:
                logger.debug(f'Received M-SEARCH from {addr[0]}, responding')
                sock.sendto(response.encode(), addr)
    except Exception as e:
        logger.error(f'M-SEARCH responder error: {e}')


# ==============================================================================
# MODULE: agent.client
# ==============================================================================

"""WebSocket client — connects agent to server. Thin wiring layer."""

import logging
import threading
import time
from typing import Optional

import socketio


logger = logging.getLogger('fancontrol')

# Initialize agent identity from env/config.json
SERVER_URL, NODE_ID, NODE_NAME = init_agent_config()
API_TOKEN = init_token()
TELEMETRY_INTERVAL = cfg.telemetry_interval

# Populate state with agent identity
state['control_mode'] = 'server'
state['server_connected'] = False
state['server_url'] = SERVER_URL
state['node_id'] = NODE_ID
state['node_name'] = NODE_NAME
state['api_token'] = API_TOKEN
state['agent_config_snapshot'] = None

try:
    state['kernel_info'] = get_kernel_info()
except Exception:
    state['kernel_info'] = {}

_sio: Optional[socketio.Client] = None
_telemetry_thread: Optional[threading.Thread] = None


def _telemetry_loop():
    """Send telemetry to server periodically."""
    while True:
        time.sleep(TELEMETRY_INTERVAL)
        if _sio and state['server_connected']:
            try:
                telemetry = get_telemetry()
                logger.info(f'[telemetry] fans={list(telemetry["fans"].keys())} '
                            f'temps={list(telemetry["temp_sensors"].keys())} '
                            f'hdds={list(telemetry["hdd_sensors"].keys())} '
                            f'node_id={NODE_ID}')
                _sio.emit('agent:telemetry', {
                    'node_id': NODE_ID,
                    'telemetry': telemetry,
                })
            except Exception as e:
                logger.error(f'Telemetry send failed: {e}')


def _update_check_loop():
    """Poll server for updates via HTTP."""
    import urllib.request
    import json as _json
    import os
    import subprocess

    POLL_INTERVAL = 15  # check every 15 seconds

    # Convert ws:// to http:// for HTTP requests
    http_url = SERVER_URL.replace('ws://', 'http://').replace('wss://', 'https://')

    first_run = True
    while True:
        # First poll 10s after connect, then every POLL_INTERVAL
        time.sleep(10 if first_run else POLL_INTERVAL)
        first_run = False
        try:
            if not state.get('server_connected'):
                continue

            # Use state['node_id'] (may differ from module-level NODE_ID after server push)
            current_node_id = state.get('node_id', NODE_ID)
            payload = _json.dumps({
                'agent_version': CONFIG_VERSION,
                'node_id': current_node_id,
            }).encode()

            req = urllib.request.Request(
                f'{http_url}/api/update/poll',
                data=payload,
                headers={'Content-Type': 'application/json'},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = _json.loads(resp.read())

            if result.get('should_update'):
                server_ver = result.get('server_version', '?')
                logger.info(f'[update-check] Server requests update: {CONFIG_VERSION} → {server_ver}')

                # Notify browser of update progress
                if _sio:
                    _sio.emit('agent:update_result', {
                        'node_id': current_node_id,
                        'status': 'pulling',
                        'version': server_ver,
                    })

                repo_dir = '/repo'
                git_dir = os.path.join(repo_dir, '.git')
                if not os.path.isdir(git_dir):
                    logger.warning('[update-check] /repo has no .git — cannot auto-update')
                    if _sio:
                        _sio.emit('agent:update_result', {
                            'node_id': current_node_id,
                            'status': 'error',
                            'message': 'No .git in /repo',
                        })
                    continue

                fetch = subprocess.run(
                    ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                )
                if fetch.returncode != 0:
                    logger.error(f'[update-check] git fetch failed: {fetch.stderr[:200]}')
                    if _sio:
                        _sio.emit('agent:update_result', {
                            'node_id': current_node_id,
                            'status': 'error',
                            'message': f'git fetch failed: {fetch.stderr[:200]}',
                        })
                    # Don't consume pending_update on failure — retry next poll
                    continue

                reset = subprocess.run(
                    ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
                )
                if reset.returncode != 0:
                    logger.error(f'[update-check] git reset failed: {reset.stderr[:200]}')
                    if _sio:
                        _sio.emit('agent:update_result', {
                            'node_id': current_node_id,
                            'status': 'error',
                            'message': f'git reset failed: {reset.stderr[:200]}',
                        })
                    # Don't consume pending_update on failure — retry next poll
                    continue

                logger.info(f'[update-check] Updated /repo to {server_ver}, restarting...')
                if _sio:
                    _sio.emit('agent:update_result', {
                        'node_id': current_node_id,
                        'status': 'synced',
                        'version': server_ver,
                    })
                time.sleep(1)  # Ensure synced event is delivered before restart
                threading.Timer(1.0, os._exit, args=[0]).start()
                break

            else:
                logger.debug(f'[update-check] Up to date: {CONFIG_VERSION}')

        except Exception as e:
            logger.warning(f'[update-check] Poll failed: {e}')


def start_client():
    """Start the WebSocket client connection to server."""
    global _sio, _telemetry_thread

    logger.info(f'[start_client] SERVER_URL={SERVER_URL}, NODE_ID={NODE_ID}')

    start_announcer(NODE_ID, NODE_NAME)

    import threading
    responder_thread = threading.Thread(
        target=_handle_msearch,
        args=(NODE_ID, NODE_NAME),
        daemon=True
    )
    responder_thread.start()
    logger.info('[agent] M-SEARCH responder started')

    if not SERVER_URL:
        logger.info('No SERVER_URL set — running standalone')
        return

    _sio = socketio.Client(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=1,
        reconnection_delay_max=30,
    )

    # Register event handlers
    handlers = make_handlers(_sio)
    for event, handler in handlers.items():
        _sio.on(event, handler)
    logger.info(f'[agent] Registered handlers: {list(handlers.keys())}')

    try:
        _sio.connect(SERVER_URL)
    except Exception as e:
        logger.error(f'Failed to connect to server: {e}')

    _telemetry_thread = threading.Thread(target=_telemetry_loop, daemon=True)
    _telemetry_thread.start()

    _update_thread = threading.Thread(target=_update_check_loop, daemon=True)
    _update_thread.start()
    logger.info('[agent] Update check loop started (every 5 minutes)')


# ==============================================================================
# MODULE: agent.routes
# ==============================================================================

"""Agent-specific routes — mode switch, status, config revert."""

import os
from flask import Blueprint, jsonify, request


agent_routes = Blueprint('agent_routes', __name__)


@agent_routes.route('/api/agent/status')
def agent_status():
    """Get agent status including server connection."""
    return jsonify({
        'control_mode': state.get('control_mode', 'server'),
        'server_connected': state.get('server_connected', False),
        'server_url': state.get('server_url', ''),
        'node_id': state.get('node_id', ''),
        'api_token': state.get('api_token', ''),
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

        for fan_id, fan_cfg in snapshot.get('fans', {}).items():
            if fan_id in state['fans']:
                for key, val in fan_cfg.items():
                    state['fans'][fan_id][key] = val

        state['agent_config_snapshot'] = None
        invalidate_state_cache()

    save_config()
    return jsonify({'status': 'reverted'})


@agent_routes.route('/api/agent/disks/<disk_id>/smart')
def agent_disk_smart(disk_id):
    """Get SMART data for a disk on the agent."""
    import logging
    logger = logging.getLogger('fancontrol')
    try:
        logger.info(f'[SMART] Request for disk_id={disk_id}')
        with state_lock:
            disk = state.get('hdd_sensors', {}).get(disk_id)
            if not disk:
                logger.warning(f'[SMART] Disk {disk_id} not found in hdd_sensors')
                return jsonify({'error': 'Disk not found'}), 404
            device = disk.get('device', '')
            if not device:
                logger.warning(f'[SMART] No device path for {disk_id}')
                return jsonify({'error': 'No device path'}), 404
        logger.info(f'[SMART] Reading {device} for disk {disk_id}')
        result = read_disk_smart(device)
        logger.info(f'[SMART] Result: attrs={len(result.get("attributes", []))} error={result.get("error")} method={result.get("access_method")}')
        return jsonify(result)
    except Exception as e:
        logger.error(f'Agent SMART error for {disk_id}: {e}', exc_info=True)
        return jsonify({'error': str(e)}), 500


# ==============================================================================
# MODULE: installer.wizard
# ==============================================================================

"""Setup wizard — Flask server for first-time configuration."""

import json
import os
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from flask import Flask, jsonify, request, render_template

app = Flask(__name__, template_folder='templates')

CONFIG_PATH = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'

_install_status = {
    'progress': 0,
    'stage': '',
    'message': '',
    'complete': False,
    'error': False,
}


@app.route('/')
def index():
    return render_template('setup.html')


@app.route('/api/config', methods=['POST'])
def save_config():
    config = request.get_json()

    if config.get('mode') == 'agent':
        if not config.get('server_url'):
            return jsonify({'error': 'Server URL required'}), 400
        if not config.get('node_name'):
            return jsonify({'error': 'Node name required'}), 400
        # api_token is auto-generated by agent — no manual input needed

    config['initialized'] = True

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

    return jsonify({'status': 'saved', 'mode': config.get('mode', 'server')})


@app.route('/api/install', methods=['POST'])
def install():
    global _install_status
    config = request.get_json()

    if config.get('mode') == 'agent':
        if not config.get('server_url'):
            return jsonify({'error': 'Server URL required'}), 400
        if not config.get('node_name'):
            return jsonify({'error': 'Node name required'}), 400
        # api_token is auto-generated by agent — no manual input needed

    config['initialized'] = True

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

    _install_status = {
        'progress': 100,
        'stage': 'Complete',
        'message': 'Configuration saved. Container will restart shortly.',
        'complete': True,
        'error': False,
    }

    threading.Thread(target=_do_exit, daemon=True).start()

    return jsonify({'status': 'installing'})


def _do_exit():
    time.sleep(2)
    os._exit(0)


@app.route('/api/status', methods=['GET'])
def status():
    return jsonify(_install_status)


@app.route('/api/restart', methods=['POST'])
def restart_container():
    threading.Thread(target=_do_exit, daemon=True).start()
    return jsonify({'status': 'restarting'})


@app.route('/api/validate-token', methods=['POST'])
def validate_token():
    data = request.get_json()
    server_url = data.get('server_url', '')

    try:
        parsed = urllib.parse.urlparse(server_url.replace('ws://', 'http://'))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((parsed.hostname, parsed.port or 80))
        s.close()
        return jsonify({'valid': True, 'message': 'Server reachable'})
    except Exception as e:
        return jsonify({'valid': False, 'message': f'Cannot reach server: {e}'})


@app.route('/api/discover-servers', methods=['GET'])
def discover_servers():
    """Scan LAN for FanControl servers via SSDP."""
    SSDP_ADDR = '239.255.255.250'
    SSDP_PORT = 1900

    servers = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(3)

        msearch = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST: 239.255.255.250:1900\r\n'
            'MAN: "ssdp:discover"\r\n'
            'ST: urn:fancontrol-web:server\r\n'
            'MX: 3\r\n'
            '\r\n'
        )
        sock.sendto(msearch.encode(), (SSDP_ADDR, SSDP_PORT))

        start = time.time()
        seen = set()
        while time.time() - start < 3:
            try:
                data, addr = sock.recvfrom(1024)
                headers = {}
                for line in data.decode(errors='ignore').split('\r\n'):
                    if ':' in line:
                        key, _, value = line.partition(':')
                        headers[key.strip().upper()] = value.strip()

                usn = headers.get('USN', '')
                if 'urn:fancontrol-web:server:' not in usn:
                    continue

                ip = addr[0]
                if ip in seen:
                    continue
                seen.add(ip)

                name = headers.get('X-FANCONTROL-NAME', 'FanControl Server')
                port = int(headers.get('X-FANCONTROL-PORT', '5059'))
                servers.append({
                    'ip': ip,
                    'port': port,
                    'name': name,
                })
            except socket.timeout:
                break
        sock.close()
    except Exception as e:
        return jsonify({'servers': [], 'error': str(e)})

    return jsonify({'servers': servers})


def run_wizard():
    print('=' * 60)
    print('FanControl Web — Setup Wizard')
    print('Open http://localhost:5059 in your browser')
    print('=' * 60)
    app.run(host='0.0.0.0', port=5059, debug=False)


if __name__ == '__main__':
    run_wizard()


# ==============================================================================
# MODULE: app — Flask/SocketIO setup and entry point
# ==============================================================================

#!/usr/bin/env python3
"""
FanControl Web v3.3.6 - Neon Cyberpunk Edition
Modern fan control with real-time monitoring and intelligent thermal management
"""

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask
from flask_socketio import SocketIO



# ============================================================================
# CONFIGURATION & INITIALIZATION
# ============================================================================

LOG_DIR = cfg.log_dir
try:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
except Exception:
    # Ignore permission errors during import (testing or restricted environments)
    pass

# Logger setup — guard against duplicate handlers (gunicorn workers re-import)
logger = logging.getLogger('fancontrol')

_console_handler = None
_file_handler = None

if not logger.hasHandlers():
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setLevel(logging.INFO)
    _console_handler.setFormatter(fmt)
    logger.addHandler(_console_handler)

    try:
        _file_handler = RotatingFileHandler(
            f'{LOG_DIR}/fancontrol.log',
            maxBytes=10*1024*1024,
            backupCount=5,
            encoding='utf-8'
        )
        _file_handler.setLevel(logging.DEBUG)
        _file_handler.setFormatter(fmt)
        logger.addHandler(_file_handler)
    except Exception:
        pass
else:
    # Handlers already set up (gunicorn workers) — grab references
    for h in logger.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            _console_handler = h
        elif isinstance(h, RotatingFileHandler):
            _file_handler = h

LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR']


def set_log_level(level_name: str):
    """Set log level for both console and file handlers."""
    level_name = level_name.upper()
    if level_name not in LOG_LEVELS:
        return False
    level = getattr(logging, level_name)
    if _console_handler:
        _console_handler.setLevel(level)
    if _file_handler:
        _file_handler.setLevel(level)
    logger.setLevel(level)
    state['log_level'] = level_name
    logger.info(f'Log level changed to {level_name}')
    return True


def get_log_level() -> str:
    """Get current effective log level (from console handler)."""
    if _console_handler:
        return logging.getLevelName(_console_handler.level)
    return 'INFO'


# Flask & SocketIO
app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS_ORIGINS = cfg.cors_origins

socketio = SocketIO(
    app,
    cors_allowed_origins=CORS_ORIGINS,
    async_mode='threading',
    logger=False,
    engineio_logger=False,
    ping_timeout=120,
    ping_interval=25
)

app.register_blueprint(routes)

app.register_blueprint(agent_routes)

# ============================================================================
# ENTRY POINT
# ============================================================================
# INITIALIZATION
# ============================================================================

def init_database():
    """Initialize SQLite database and schema"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                ts TEXT,
                mode TEXT,
                pwm INTEGER,
                rpm INTEGER,
                max_temp INTEGER,
                fan_count INTEGER,
                disk_count INTEGER
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)')
        conn.commit()
    logger.info('Database initialized')


def init_hardware():
    """Discover hardware and load configuration"""
    if CONFIG_PATH.exists():
        try:
            state['fans'], state['temp_sensors'] = discover_fans_and_sensors()
            state['hdd_sensors'] = discover_disks()
            refresh()
            load_config()

            # Apply saved log level
            saved_level = state.get('log_level', 'INFO')
            if saved_level and saved_level != 'INFO':
                set_log_level(saved_level)

            if state['initialized']:
                logger.info('System restored from saved configuration')
            else:
                logger.warning('Configuration exists but initialization incomplete')
        except Exception as e:
            logger.error(f'Startup error: {e}', exc_info=True)
    else:
        state['initialized'] = False
        logger.info('No configuration found - wizard mode')


_control_loop_started = False

def _ensure_control_loop():
    """Start control loop once (safe for gunicorn workers)"""
    global _control_loop_started
    if not _control_loop_started:
        _control_loop_started = True
        threading.Thread(target=loop, args=(socketio,), daemon=True).start()


@app.before_request
def _auto_init():
    """Auto-initialize on first request when running under gunicorn"""
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
        # Invalidate cached state and push correct state to all connected clients
        invalidate_state_cache()
        socketio.emit('update', get_state())


def is_setup_needed():
    """Check if setup wizard should be shown."""
    if not CONFIG_PATH.exists():
        logger.info(f'[setup] Config not found at {CONFIG_PATH}')
        return True
    try:
        import json
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        has_mode = bool(cfg.get('mode'))
        has_init = bool(cfg.get('initialized'))
        logger.info(f'[setup] Config found: mode={cfg.get("mode")}, initialized={cfg.get("initialized")}, keys={list(cfg.keys())}')
        return not (has_init or has_mode)
    except Exception as e:
        logger.warning(f'[setup] Config read error: {e}')
        return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description='FanControl Web')
    parser.add_argument('--mode', choices=['setup', 'server', 'agent'],
                       default=cfg.mode,
                       help='Run mode: setup, server (default), or agent')
    args = parser.parse_args()

    # Auto-detect setup mode on first boot
    if args.mode != 'setup' and is_setup_needed():
        args.mode = 'setup'

    logger.info('=' * 60)
    logger.info(f'STARTING FanControl Web {CONFIG_VERSION} - Neon Cyberpunk Edition')
    logger.info(f'Mode: {args.mode}')
    logger.info(f'PID: {os.getpid()}')
    logger.info(f'Config: {CONFIG_PATH} (exists: {CONFIG_PATH.exists()})')

    # Check /repo sync status
    repo_dir = '/repo'
    if os.path.isdir(repo_dir) and os.path.isfile(os.path.join(repo_dir, 'app.py')):
        try:
            with open(os.path.join(repo_dir, 'core', 'state.py')) as f:
                for line in f:
                    if 'CONFIG_VERSION' in line:
                        logger.info(f'/repo version: {line.strip()}')
                        break
        except Exception:
            logger.info('/repo: cannot read version')

        # Check /app templates version
        try:
            with open('/app/templates/index.html') as f:
                for line in f:
                    if 'main.js?v=' in line:
                        logger.info(f'/app template: {line.strip()}')
                        break
        except Exception:
            logger.info('/app: cannot read template')

        # Check /repo templates version
        try:
            with open(os.path.join(repo_dir, 'templates', 'index.html')) as f:
                for line in f:
                    if 'main.js?v=' in line:
                        logger.info(f'/repo template: {line.strip()}')
                        break
        except Exception:
            logger.info('/repo: cannot read template')
    else:
        logger.info('/repo: NOT FOUND or no app.py')

    logger.info('=' * 60)

    if args.mode == 'setup':
        if is_setup_needed():
            logger.info('[setup] Setup needed — launching wizard')
            run_wizard()
            return
        else:
            # Setup already done — read mode from saved config
            try:
                import json
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
                saved_mode = saved.get('mode')
                if not saved_mode:
                    logger.warning(f'[setup] Config exists but "mode" is missing! Keys: {list(saved.keys())}')
                    logger.warning('[setup] Defaulting to server mode. Re-run wizard or add "mode" to config.json')
                    saved_mode = 'server'
                args.mode = saved_mode
                logger.info(f'[setup] Setup complete — switching to mode: {args.mode}')
            except Exception as e:
                logger.warning(f'[setup] Failed to read config: {e}')
                args.mode = 'server'
    elif not is_setup_needed():
        # Setup done, no MODE env var — read mode from saved config
        try:
            import json
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            saved_mode = saved.get('mode')
            if saved_mode and saved_mode != args.mode:
                logger.info(f'[mode] Config has mode={saved_mode}, overriding default {args.mode}')
                args.mode = saved_mode
        except Exception:
            pass

    if args.mode == 'agent':
        # Load config BEFORE importing agent client (module reads env vars at import time)
        if not os.environ.get('SERVER_URL'):
            try:
                import json as _json
                with open(CONFIG_PATH) as f:
                    _cfg = _json.load(f)
                logger.info(f'Agent config loaded: server_url={_cfg.get("server_url", "MISSING")}, node_name={_cfg.get("node_name", "MISSING")}')
                if _cfg.get('server_url'):
                    os.environ['SERVER_URL'] = _cfg['server_url']
                if _cfg.get('node_name'):
                    os.environ.setdefault('NODE_NAME', _cfg['node_name'])
                if _cfg.get('api_token'):
                    os.environ.setdefault('API_TOKEN', _cfg['api_token'])
            except Exception as e:
                logger.warning(f'Could not load agent config: {e}')

        logger.info(f'Agent SERVER_URL={os.environ.get("SERVER_URL", "EMPTY")}')
        init_database()
        init_hardware()
        _init_complete.set()
        _ensure_control_loop()
        # Register basic Socket.IO handlers for agent web UI (no SSDP/heartbeat)
        @socketio.on('connect')
        def _agent_socket_connect():
            _init_complete.wait(timeout=15)
            socketio.emit('update', get_state())
        @socketio.on('get_state')
        def _agent_socket_get_state():
            socketio.emit('update', get_state())
        start_client()
    else:
        register_handlers(socketio)
        init_database()
        init_hardware()
        _init_complete.set()
        _ensure_control_loop()

    logger.info('Starting server on port 5059')
    socketio.run(app, host='0.0.0.0', port=5059, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()

SETUP_TEMPLATE_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>FanControl Web - Setup</title>\n<script src="https://cdn.tailwindcss.com"></script>\n<style>\n  body { background: #0a0a1a; font-family: \'Segoe UI\', system-ui, sans-serif; }\n  .step-dot { transition: all 0.3s ease; }\n  .step-dot.active { background: #06b6d4; box-shadow: 0 0 12px #06b6d4; }\n  .step-dot.done { background: #10b981; }\n  .card-hover { transition: all 0.3s ease; }\n  .card-hover:hover { border-color: #06b6d4; box-shadow: 0 0 20px rgba(6,182,212,0.15); }\n  .card-selected { border-color: #06b6d4 !important; background: rgba(6,182,212,0.08) !important; box-shadow: 0 0 20px rgba(6,182,212,0.2); }\n  .glow-text { text-shadow: 0 0 20px rgba(6,182,212,0.5); }\n  .progress-bar { transition: width 0.5s ease; }\n  .fade-in { animation: fadeIn 0.3s ease; }\n  @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }\n  input:focus, textarea:focus { outline: none; border-color: #06b6d4 !important; box-shadow: 0 0 8px rgba(6,182,212,0.3); }\n  .field-desc { color: #9ca3af; font-size: 0.75rem; margin-top: 0.25rem; }\n</style>\n</head>\n<body class="min-h-screen flex items-center justify-center p-4">\n<div class="w-full max-w-lg">\n\n  <!-- Header -->\n  <div class="text-center mb-8">\n    <h1 class="text-3xl font-bold text-cyan-400 glow-text">FanControl Web</h1>\n    <p id="subtitle" class="text-gray-400 mt-2">Setup Wizard</p>\n  </div>\n\n  <!-- Step Indicator -->\n  <div class="flex justify-center gap-3 mb-8">\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600 active" data-step="1"></div>\n      <span class="text-xs text-gray-500" id="step_label_1"></span>\n    </div>\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600" data-step="2"></div>\n      <span class="text-xs text-gray-500" id="step_label_2"></span>\n    </div>\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600" data-step="3"></div>\n      <span class="text-xs text-gray-500" id="step_label_3"></span>\n    </div>\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600" data-step="4"></div>\n      <span class="text-xs text-gray-500" id="step_label_4"></span>\n    </div>\n  </div>\n\n  <!-- Step 1: Language -->\n  <div id="step1" class="fade-in">\n    <h2 id="lang_title" class="text-xl font-semibold text-white text-center mb-6">Select Language</h2>\n    <div class="flex justify-center gap-4">\n      <button onclick="selectLang(\'en\')" class="card-hover w-40 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🇬🇧</div>\n        <div class="text-white font-medium">English</div>\n      </button>\n      <button onclick="selectLang(\'ru\')" class="card-hover w-40 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🇷🇺</div>\n        <div class="text-white font-medium">Русский</div>\n      </button>\n    </div>\n  </div>\n\n  <!-- Step 2: Mode -->\n  <div id="step2" class="hidden fade-in">\n    <h2 id="comp_title" class="text-xl font-semibold text-white text-center mb-6"></h2>\n    <div class="flex justify-center gap-4">\n      <button onclick="selectMode(\'server\')" id="btn_server" class="card-hover w-44 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🖥️</div>\n        <div id="lbl_server" class="text-white font-medium"></div>\n        <div id="lbl_server_desc" class="text-gray-400 text-sm mt-1"></div>\n      </button>\n      <button onclick="selectMode(\'agent\')" id="btn_agent" class="card-hover w-44 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🔗</div>\n        <div id="lbl_agent" class="text-white font-medium"></div>\n        <div id="lbl_agent_desc" class="text-gray-400 text-sm mt-1"></div>\n      </button>\n    </div>\n  </div>\n\n  <!-- Step 3: Configuration -->\n  <div id="step3" class="hidden fade-in">\n    <h2 id="config_title" class="text-xl font-semibold text-white text-center mb-6"></h2>\n    <div class="bg-gray-800/50 border border-gray-700 rounded-xl p-6 space-y-4">\n      <!-- Server fields -->\n      <div id="fields_server">\n        <div>\n          <label id="lbl_port" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_port" type="number" value="5059" min="1" max="65535"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_port" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_data_path" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_data_path" type="text" value="/data"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_data_path" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_server_name" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_server_name" type="text" value="My Server"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_server_name" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_description" class="block text-gray-300 text-sm mb-1"></label>\n          <textarea id="input_description" rows="2"\n                    class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white resize-none"></textarea>\n          <p id="desc_description" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_admin_password" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_admin_password" type="password" placeholder=""\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_admin_password" class="field-desc"></p>\n        </div>\n        <div class="mt-4 flex items-center gap-3">\n          <input id="input_ssdp_enabled" type="checkbox" checked\n                 class="w-4 h-4 rounded bg-gray-900 border-gray-600 text-cyan-500 focus:ring-cyan-500">\n          <label id="lbl_ssdp_enabled" for="input_ssdp_enabled" class="text-gray-300 text-sm"></label>\n        </div>\n        <p id="desc_ssdp_enabled" class="field-desc ml-7"></p>\n      </div>\n      <!-- Agent fields -->\n      <div id="fields_agent" class="hidden">\n        <!-- Auto-discovery section -->\n        <div class="mb-4 p-3 bg-gray-900/50 border border-gray-700 rounded-lg">\n          <div class="flex items-center justify-between mb-2">\n            <span id="lbl_autodiscover" class="text-gray-300 text-sm font-medium"></span>\n            <button onclick="discoverServers()" id="discover_btn"\n                    class="bg-cyan-700 hover:bg-cyan-600 text-white text-xs px-3 py-1.5 rounded-lg transition-colors">\n            </button>\n          </div>\n          <div id="discovered_list" class="space-y-1"></div>\n          <p id="desc_autodiscover" class="field-desc mt-1"></p>\n        </div>\n        <!-- Manual IP + Port -->\n        <div class="flex gap-3">\n          <div class="flex-1">\n            <label id="lbl_server_url" class="block text-gray-300 text-sm mb-1"></label>\n            <input id="input_server_url" type="text" placeholder="192.168.1.100"\n                   class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          </div>\n          <div class="w-24">\n            <label id="lbl_server_port" class="block text-gray-300 text-sm mb-1"></label>\n            <input id="input_server_port" type="number" value="5059" min="1" max="65535"\n                   class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          </div>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_node_name" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_node_name" type="text" value=""\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white"\n                 placeholder="">\n          <p id="desc_node_name" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_agent_data_path" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_agent_data_path" type="text" value="/data"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_agent_data_path" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <button onclick="testConnection()" id="test_conn_btn"\n                  class="bg-gray-600 hover:bg-gray-500 text-white font-medium px-4 py-2 rounded-lg transition-colors text-sm">\n          </button>\n          <span id="test_conn_result" class="ml-3 text-sm"></span>\n        </div>\n      </div>\n      <div id="config_error" class="hidden text-red-400 text-sm mt-2"></div>\n      <button onclick="startInstall()" id="install_btn"\n              class="w-full mt-4 bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-3 rounded-lg transition-colors">\n      </button>\n    </div>\n  </div>\n\n  <!-- Step 4: Progress -->\n  <div id="step4" class="hidden fade-in">\n    <div class="bg-gray-800/50 border border-gray-700 rounded-xl p-6">\n      <div class="flex justify-between text-sm text-gray-400 mb-2">\n        <span id="progress_label"></span>\n        <span id="progress_pct">0%</span>\n      </div>\n      <div class="w-full bg-gray-700 rounded-full h-3 mb-4">\n        <div id="progress_bar" class="progress-bar bg-cyan-500 h-3 rounded-full" style="width: 0%"></div>\n      </div>\n      <p id="progress_msg" class="text-gray-300 text-sm text-center"></p>\n      <div id="complete_section" class="hidden text-center mt-6">\n        <div class="text-emerald-400 text-xl font-semibold mb-4" id="complete_text"></div>\n        <p id="restart_msg" class="text-gray-400 text-sm mb-4"></p>\n        <a id="dashboard_link" href="/" target="_blank"\n           class="inline-block bg-cyan-600 hover:bg-cyan-500 text-white font-semibold px-8 py-3 rounded-lg transition-colors">\n        </a>\n      </div>\n      <div id="error_section" class="hidden text-center mt-6">\n        <p id="error_text" class="text-red-400 text-sm"></p>\n        <button onclick="location.reload()" class="mt-4 bg-gray-600 hover:bg-gray-500 text-white px-6 py-2 rounded-lg transition-colors text-sm">\n          Retry\n        </button>\n      </div>\n    </div>\n  </div>\n\n</div>\n\n<script>\nconst translations = {\n  en: {\n    title: \'Setup Wizard\',\n    step1: \'Language\',\n    step2: \'Mode\',\n    step3: \'Configuration\',\n    step4: \'Install\',\n    component_title: \'Select Component\',\n    server: \'Server\',\n    server_desc: \'Central dashboard + control for multiple nodes\',\n    agent: \'Agent\',\n    agent_desc: \'Connect to existing server\',\n    config_title: \'Configuration\',\n    port: \'Port\',\n    port_desc: \'Web interface port (default: 5059)\',\n    data_path: \'Data Path\',\n    data_path_desc: \'Container path for config and data\',\n    server_name: \'Server Name\',\n    server_name_desc: \'Display name for this server\',\n    description: \'Description\',\n    description_desc: \'Optional description\',\n    admin_password: \'Admin Password\',\n    admin_password_desc: \'Protect web interface (empty = no auth)\',\n    ssdp_enabled: \'Enable LAN Discovery\',\n    ssdp_desc: \'Allow agents to discover this server on LAN\',\n    server_url: \'Server IP\',\n    server_url_desc: \'IP address of the server\',\n    server_port: \'Port\',\n    node_name: \'Node Name\',\n    node_name_desc: \'Display name for this agent (auto-detected if empty)\',\n    agent_data_path: \'Data Path\',\n    agent_data_path_desc: \'Container path for config and data\',\n    autodiscover: \'Auto-discover servers\',\n    autodiscover_desc: \'Scan network for FanControl servers\',\n    discover_btn: \'Scan\',\n    discover_scanning: \'Scanning...\',\n    discover_none: \'No servers found. Enter IP manually.\',\n    select_server: \'Select\',\n    test_connection: \'Test Connection\',\n    test_ok: \'Connection successful!\',\n    test_fail: \'Connection failed\',\n    test_testing: \'Testing...\',\n    install_btn: \'Install\',\n    installing: \'Installing...\',\n    restarting: \'Container restarting...\',\n    complete: \'Setup Complete!\',\n    redirecting: \'Redirecting in {seconds} seconds...\',\n    open_dashboard: \'Open Dashboard\',\n  },\n  ru: {\n    title: \'Мастер установки\',\n    step1: \'Язык\',\n    step2: \'Режим\',\n    step3: \'Конфигурация\',\n    step4: \'Установка\',\n    component_title: \'Выберите компонент\',\n    server: \'Сервер\',\n    server_desc: \'Центральная панель управления для нескольких узлов\',\n    agent: \'Агент\',\n    agent_desc: \'Подключение к существующему серверу\',\n    config_title: \'Конфигурация\',\n    port: \'Порт\',\n    port_desc: \'Порт веб-интерфейса (по умолчанию: 5059)\',\n    data_path: \'Путь к данным\',\n    data_path_desc: \'Контейнерный путь для конфига и данных\',\n    server_name: \'Имя сервера\',\n    server_name_desc: \'Отображаемое имя сервера\',\n    description: \'Описание\',\n    description_desc: \'Описание (необязательно)\',\n    admin_password: \'Пароль админа\',\n    admin_password_desc: \'Защита веб-интерфейса (пусто = без пароля)\',\n    ssdp_enabled: \'Включить обнаружение в LAN\',\n    ssdp_desc: \'Разрешить агентам находить этот сервер в локальной сети\',\n    server_url: \'IP сервера\',\n    server_url_desc: \'IP адрес сервера\',\n    server_port: \'Порт\',\n    node_name: \'Имя узла\',\n    node_name_desc: \'Отображаемое имя агента (автоопределение)\',\n    agent_data_path: \'Путь к данным\',\n    agent_data_path_desc: \'Контейнерный путь для конфига и данных\',\n    autodiscover: \'Автообнаружение серверов\',\n    autodiscover_desc: \'Поиск серверов FanControl в сети\',\n    discover_btn: \'Найти\',\n    discover_scanning: \'Поиск...\',\n    discover_none: \'Серверы не найдены. Введите IP вручную.\',\n    select_server: \'Выбрать\',\n    test_connection: \'Проверить соединение\',\n    test_ok: \'Соединение успешно!\',\n    test_fail: \'Ошибка соединения\',\n    test_testing: \'Проверка...\',\n    install_btn: \'Установить\',\n    installing: \'Установка...\',\n    restarting: \'Контейнер перезапускается...\',\n    complete: \'Установка завершена!\',\n    redirecting: \'Перенаправление через {seconds} секунд...\',\n    open_dashboard: \'Открыть панель\',\n  }\n};\n\nlet lang = \'en\';\nlet mode = null;\nlet pollInterval = null;\nlet redirectCountdown = null;\n\nfunction t(key) { return translations[lang][key] || key; }\n\nfunction updateTexts() {\n  document.getElementById(\'subtitle\').textContent = t(\'title\');\n  document.getElementById(\'step_label_1\').textContent = t(\'step1\');\n  document.getElementById(\'step_label_2\').textContent = t(\'step2\');\n  document.getElementById(\'step_label_3\').textContent = t(\'step3\');\n  document.getElementById(\'step_label_4\').textContent = t(\'step4\');\n  document.getElementById(\'comp_title\').textContent = t(\'component_title\');\n  document.getElementById(\'lbl_server\').textContent = t(\'server\');\n  document.getElementById(\'lbl_server_desc\').textContent = t(\'server_desc\');\n  document.getElementById(\'lbl_agent\').textContent = t(\'agent\');\n  document.getElementById(\'lbl_agent_desc\').textContent = t(\'agent_desc\');\n  document.getElementById(\'config_title\').textContent = t(\'config_title\');\n  document.getElementById(\'lbl_port\').textContent = t(\'port\');\n  document.getElementById(\'desc_port\').textContent = t(\'port_desc\');\n  document.getElementById(\'lbl_data_path\').textContent = t(\'data_path\');\n  document.getElementById(\'desc_data_path\').textContent = t(\'data_path_desc\');\n  document.getElementById(\'lbl_server_name\').textContent = t(\'server_name\');\n  document.getElementById(\'desc_server_name\').textContent = t(\'server_name_desc\');\n  document.getElementById(\'lbl_description\').textContent = t(\'description\');\n  document.getElementById(\'desc_description\').textContent = t(\'description_desc\');\n  document.getElementById(\'lbl_admin_password\').textContent = t(\'admin_password\');\n  document.getElementById(\'desc_admin_password\').textContent = t(\'admin_password_desc\');\n  document.getElementById(\'lbl_ssdp_enabled\').textContent = t(\'ssdp_enabled\');\n  document.getElementById(\'desc_ssdp_enabled\').textContent = t(\'ssdp_desc\');\n  document.getElementById(\'lbl_server_url\').textContent = t(\'server_url\');\n  document.getElementById(\'lbl_server_port\').textContent = t(\'server_port\');\n  document.getElementById(\'lbl_node_name\').textContent = t(\'node_name\');\n  document.getElementById(\'desc_node_name\').textContent = t(\'node_name_desc\');\n  document.getElementById(\'lbl_agent_data_path\').textContent = t(\'agent_data_path\');\n  document.getElementById(\'desc_agent_data_path\').textContent = t(\'agent_data_path_desc\');\n  document.getElementById(\'lbl_autodiscover\').textContent = t(\'autodiscover\');\n  document.getElementById(\'desc_autodiscover\').textContent = t(\'autodiscover_desc\');\n  document.getElementById(\'discover_btn\').textContent = t(\'discover_btn\');\n  document.getElementById(\'test_conn_btn\').textContent = t(\'test_connection\');\n  document.getElementById(\'install_btn\').textContent = t(\'install_btn\');\n}\n\nfunction setStep(n) {\n  for (let i = 1; i <= 4; i++) {\n    const el = document.getElementById(\'step\' + i);\n    el.classList.toggle(\'hidden\', i !== n);\n    el.classList.toggle(\'fade-in\', i === n);\n    const dot = document.querySelector(`.step-dot[data-step="${i}"]`);\n    dot.classList.remove(\'active\', \'done\');\n    if (i < n) dot.classList.add(\'done\');\n    else if (i === n) dot.classList.add(\'active\');\n  }\n}\n\nfunction selectLang(l) {\n  lang = l;\n  updateTexts();\n  setStep(2);\n}\n\nfunction selectMode(m) {\n  mode = m;\n  document.getElementById(\'btn_server\').classList.toggle(\'card-selected\', m === \'server\');\n  document.getElementById(\'btn_agent\').classList.toggle(\'card-selected\', m === \'agent\');\n  document.getElementById(\'fields_server\').classList.toggle(\'hidden\', m !== \'server\');\n  document.getElementById(\'fields_agent\').classList.toggle(\'hidden\', m !== \'agent\');\n  setStep(3);\n  if (m === \'agent\') { setTimeout(discoverServers, 300); }\n}\n\nfunction testConnection() {\n  const btn = document.getElementById(\'test_conn_btn\');\n  const result = document.getElementById(\'test_conn_result\');\n  const ip = document.getElementById(\'input_server_url\').value.trim();\n  const port = document.getElementById(\'input_server_port\').value.trim() || \'5059\';\n\n  if (!ip) {\n    result.textContent = t(\'test_fail\');\n    result.className = \'ml-3 text-sm text-red-400\';\n    return;\n  }\n\n  btn.disabled = true;\n  btn.textContent = t(\'test_testing\');\n  result.textContent = \'\';\n  result.className = \'ml-3 text-sm\';\n\n  fetch(\'/api/validate-token\', {\n    method: \'POST\',\n    headers: { \'Content-Type\': \'application/json\' },\n    body: JSON.stringify({ server_url: \'ws://\' + ip + \':\' + port })\n  })\n  .then(r => r.json())\n  .then(data => {\n    if (data.valid) {\n      result.textContent = t(\'test_ok\');\n      result.className = \'ml-3 text-sm text-emerald-400\';\n    } else {\n      result.textContent = data.error || t(\'test_fail\');\n      result.className = \'ml-3 text-sm text-red-400\';\n    }\n  })\n  .catch(() => {\n    result.textContent = t(\'test_fail\');\n    result.className = \'ml-3 text-sm text-red-400\';\n  })\n  .finally(() => {\n    btn.disabled = false;\n    btn.textContent = t(\'test_connection\');\n  });\n}\n\nfunction startInstall() {\n  const errEl = document.getElementById(\'config_error\');\n  errEl.classList.add(\'hidden\');\n\n  if (mode === \'agent\') {\n    const url = document.getElementById(\'input_server_url\').value.trim();\n    const name = document.getElementById(\'input_node_name\').value.trim();\n    if (!url) {\n      errEl.textContent = lang === \'ru\' ? \'Введите IP адрес сервера\' : \'Enter server IP address\';\n      errEl.classList.remove(\'hidden\');\n      return;\n    }\n  }\n\n  const config = { mode, lang };\n  if (mode === \'server\') {\n    config.port = parseInt(document.getElementById(\'input_port\').value) || 5059;\n    config.data_path = document.getElementById(\'input_data_path\').value.trim() || \'/data\';\n    config.server_name = document.getElementById(\'input_server_name\').value.trim();\n    config.description = document.getElementById(\'input_description\').value.trim();\n    config.admin_password = document.getElementById(\'input_admin_password\').value;\n    config.ssdp_enabled = document.getElementById(\'input_ssdp_enabled\').checked;\n  } else {\n    const rawIp = document.getElementById(\'input_server_url\').value.trim();\n    const port = document.getElementById(\'input_server_port\').value.trim() || \'5059\';\n    config.server_url = \'ws://\' + rawIp + \':\' + port;\n    config.node_name = document.getElementById(\'input_node_name\').value.trim() || rawIp;\n    config.data_path = document.getElementById(\'input_agent_data_path\').value.trim() || \'/data\';\n  }\n\n  fetch(\'/api/install\', {\n    method: \'POST\',\n    headers: { \'Content-Type\': \'application/json\' },\n    body: JSON.stringify(config)\n  })\n  .then(r => r.json())\n  .then(data => {\n    if (data.error) {\n      errEl.textContent = data.error;\n      errEl.classList.remove(\'hidden\');\n      return;\n    }\n    setStep(4);\n    document.getElementById(\'install_btn\').textContent = t(\'installing\');\n    document.getElementById(\'install_btn\').disabled = true;\n    pollInterval = setInterval(pollStatus, 1000);\n    pollStatus();\n  })\n  .catch(err => {\n    errEl.textContent = err.message;\n    errEl.classList.remove(\'hidden\');\n  });\n}\n\nfunction pollStatus() {\n  fetch(\'/api/status\')\n    .then(r => r.json())\n    .then(s => {\n      document.getElementById(\'progress_bar\').style.width = s.progress + \'%\';\n      document.getElementById(\'progress_pct\').textContent = s.progress + \'%\';\n      document.getElementById(\'progress_label\').textContent = s.stage;\n      document.getElementById(\'progress_msg\').textContent = s.message;\n\n      if (s.complete) {\n        clearInterval(pollInterval);\n        if (s.error) {\n          document.getElementById(\'error_section\').classList.remove(\'hidden\');\n          document.getElementById(\'error_text\').textContent = s.message;\n        } else {\n          document.getElementById(\'complete_section\').classList.remove(\'hidden\');\n          document.getElementById(\'complete_text\').textContent = t(\'complete\');\n          document.getElementById(\'dashboard_link\').textContent = t(\'open_dashboard\');\n          startRedirectCountdown();\n        }\n      }\n    });\n}\n\nfunction startRedirectCountdown() {\n  let seconds = 10;\n  const restartMsg = document.getElementById(\'restart_msg\');\n  restartMsg.textContent = t(\'redirecting\').replace(\'{seconds}\', seconds);\n  redirectCountdown = setInterval(() => {\n    seconds--;\n    if (seconds <= 0) {\n      clearInterval(redirectCountdown);\n      location.reload();\n    } else {\n      restartMsg.textContent = t(\'redirecting\').replace(\'{seconds}\', seconds);\n    }\n  }, 1000);\n}\n\nfunction discoverServers() {\n  const btn = document.getElementById(\'discover_btn\');\n  const list = document.getElementById(\'discovered_list\');\n\n  btn.disabled = true;\n  btn.textContent = t(\'discover_scanning\');\n  list.innerHTML = \'\';\n\n  fetch(\'/api/discover-servers\')\n    .then(r => r.json())\n    .then(data => {\n      if (data.servers && data.servers.length > 0) {\n        data.servers.forEach(s => {\n          const div = document.createElement(\'div\');\n          div.className = \'flex items-center justify-between bg-gray-800 rounded-lg px-3 py-2 cursor-pointer hover:border hover:border-cyan-600 transition-colors\';\n          div.innerHTML = \'<div><span class="text-white text-sm">\' + escapeHtml(s.name) + \'</span><span class="text-gray-400 text-xs ml-2">\' + escapeHtml(s.ip) + \':\' + s.port + \'</span></div>\';\n          const selBtn = document.createElement(\'button\');\n          selBtn.className = \'bg-cyan-700 hover:bg-cyan-600 text-white text-xs px-3 py-1 rounded transition-colors\';\n          selBtn.textContent = t(\'select_server\');\n          selBtn.onclick = function() { selectServer(s.ip, s.port); };\n          div.appendChild(selBtn);\n          list.appendChild(div);\n        });\n      } else {\n        list.innerHTML = \'<p class="text-gray-500 text-xs">\' + t(\'discover_none\') + \'</p>\';\n      }\n    })\n    .catch(() => {\n      list.innerHTML = \'<p class="text-gray-500 text-xs">\' + t(\'discover_none\') + \'</p>\';\n    })\n    .finally(() => {\n      btn.disabled = false;\n      btn.textContent = t(\'discover_btn\');\n    });\n}\n\nfunction selectServer(ip, port) {\n  document.getElementById(\'input_server_url\').value = ip;\n  document.getElementById(\'input_server_port\').value = port;\n}\n\nfunction escapeHtml(str) {\n  const div = document.createElement(\'div\');\n  div.textContent = str;\n  return div.innerHTML;\n}\n\nupdateTexts();\n// Auto-generate node name placeholder from hostname\ntry { document.getElementById(\'input_node_name\').placeholder = location.hostname || \'Agent\'; } catch(e) {}\n</script>\n</body>\n</html>\n'

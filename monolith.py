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

import threading
import time
from typing import Any, Dict, Optional

CONFIG_VERSION = "3.12.89"

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
            except Exception:
                pass

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
        logger.error(f'Failed to save config: {e}', exc_info=True)


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
            temp, standby = result if result else (None, False)
            
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
            if item['day'] == 'all':
                days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
            elif item['day'] == 'weekday':
                days = ['mon', 'tue', 'wed', 'thu', 'fri']
            elif item['day'] == 'weekend':
                days = ['sat', 'sun']
            else:
                days = [item['day']]
            
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
        fans = state.get('fans', {})
        fan_count = len(fans)
        disk_count = len(state.get('hdd_sensors', {}))

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

        max_temp = state.get('max_hdd_temp', 0)

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
        # Use the highest available PWM indicator — current_pct from control loop,
        # manual_pct from user setting, or target_pwm as fallback. For inverted fans
        # current_pct may lag by one cycle, so take the max of all three.
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

        # --- STOP DETECTION ---
        # RPM < 10 while fan should be spinning (pwm > 5) → stopped
        # Also detect if fan was previously running (baseline > 0) and now RPM = 0
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

        # --- SLOWDOWN DETECTION (COMBINED) ---
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

        # --- NEEDS CALIBRATION ---
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
                pass
    return d


def init_nodes_table():
    with _lock:
        conn = _get_conn()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
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
        if 'ip' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN ip TEXT DEFAULT ''")
        if 'port' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN port INTEGER DEFAULT 5059")
        if 'agent_version' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN agent_version TEXT DEFAULT ''")
        if 'pending_update' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN pending_update INTEGER DEFAULT 0")
        if 'auto_update' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN auto_update INTEGER DEFAULT 0")
        conn.commit()


def add_node(name: str, api_token: Optional[str] = None, ip: str = '', port: int = 5059) -> Dict:
    if not api_token:
        api_token = uuid.uuid4().hex
    node_id = name.lower().replace(' ', '-')
    with _lock:
        conn = _get_conn()
        conn.execute(
            'INSERT INTO nodes (node_id, name, api_token, ip, port) VALUES (?, ?, ?, ?, ?)',
            (node_id, name, api_token, ip, port)
        )
        conn.commit()
        row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
        return _row_to_dict(row)


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
            return json.loads(row['agent_config_snapshot'])
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
            node = add_node(node_name or node_id or 'Agent', api_token=api_token,
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
        agent_node_id = data.get('node_id')
        telemetry = data.get('telemetry', {})

        # Resolve agent's node_id to registry node_id via SID mapping
        from flask import request as flask_request
        agent_sid = flask_request.sid if flask_request else None
        node_id = _sid_to_node.get(agent_sid) if agent_sid else None

        logger.info(f'[telemetry-recv] agent_sent={agent_node_id} sid={agent_sid} '
                    f'resolved={node_id} fans={list(telemetry.get("fans", {}).keys())} '
                    f'temps={list(telemetry.get("temp_sensors", {}).keys())}')

        if not node_id:
            # Fallback: try direct lookup
            node_id = agent_node_id

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
    """Proxy SMART request to a remote agent."""
    import logging
    logger = logging.getLogger('fancontrol')
    node = get_node(node_id)
    if not node:
        return jsonify({'error': 'Node not found'}), 404
    ip = node.get('ip', '')
    if not ip:
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
            temps.append(row[4] if row[4] > 0 else None)
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
    update_token = cfg.update_token
    if update_token:
        provided = request.headers.get('X-Update-Token') or request.args.get('token')
        if provided != update_token:
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
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    node = add_node(name)
    return jsonify(node), 201


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
                    state['nodes'][node_id].get('ip', '') and state['nodes'][node_id].update({'ip': ip})
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
    data = request.get_json()
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
    data = request.get_json()
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
    data = request.get_json()
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
    data = request.get_json()
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

def init_agent_config():
    """Load agent config from config.json if not set via env vars."""
    config_path = cfg.data_dir / 'config.json'
    config = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass

    server_url = cfg.server_url
    node_id = cfg.node_id
    node_name = cfg.node_name

    if not server_url and config.get('server_url'):
        server_url = config['server_url']
    if node_id == 'agent-1' and config.get('node_id'):
        node_id = config['node_id']
    if node_name == 'Agent 1' and config.get('node_name'):
        node_name = config['node_name']

    if node_id == 'agent-1' and not config.get('node_id'):
        node_id = f'agent-{uuid.uuid4().hex[:12]}'
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config['node_id'] = node_id
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception:
            pass

    return server_url, node_id, node_name


def init_token():
    """Generate or load API token for this agent."""
    config_path = cfg.data_dir / 'config.json'
    if cfg.api_token:
        return cfg.api_token
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
                if config.get('api_token'):
                    return config['api_token']
        except Exception:
            pass

    new_token = uuid.uuid4().hex
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {}
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
        config['api_token'] = new_token
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass
    return new_token


def save_local_config():
    """Save current config to local config.json, preserving wizard fields."""
    config_path = cfg.data_dir / 'config.json'
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if config_path.exists():
            with open(config_path) as f:
                existing = json.load(f)
        with state_lock:
            existing.update({
                'fans': {k: {kk: vv for kk, vv in v.items()
                             if kk not in ('rpm', 'pwm_value')}
                         for k, v in state['fans'].items()},
                'temp_sensors': state['temp_sensors'],
                'hdd_sensors': state['hdd_sensors'],
                'control_mode': state.get('control_mode', 'server'),
                'initialized': state.get('initialized', False),
                'api_token': state.get('api_token', existing.get('api_token', '')),
                'node_id': state.get('node_id', existing.get('node_id', '')),
                'node_name': state.get('node_name', existing.get('node_name', '')),
                'server_url': state.get('server_url', existing.get('server_url', '')),
            })
        with open(config_path, 'w') as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        logger.error(f'Failed to save local config: {e}')


def persist_node_id(node_id, token):
    """Persist node_id and token to config.json."""
    config_path = cfg.data_dir / 'config.json'
    try:
        config = {}
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
        if node_id:
            config['node_id'] = node_id
        if token:
            config['api_token'] = token
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass


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


def get_local_config():
    """Get current local fan config including kernel info and DSM schemes."""
    with state_lock:
        config = {
            'config_version': CONFIG_VERSION,
            'fans': {k: {kk: vv for kk, vv in v.items()
                         if kk not in ('rpm', 'pwm_value')}
                     for k, v in state['fans'].items()},
            'temp_sensors': state['temp_sensors'],
            'hdd_sensors': state['hdd_sensors'],
            'kernel_info': state.get('kernel_info', {}),
        }
        try:
            if is_dsm_fan_available():
                result = get_all_schemes()
                config['dsm_schemes'] = result.get('schemes', []) if result else []
        except Exception:
            pass
        return config


def get_telemetry():
    """Get current telemetry data for server transmission."""
    with state_lock:
        return {
            'fans': {k: {'rpm': v.get('rpm', 0),
                         'pwm_value': v.get('pwm_value', 0),
                         'control_method': v.get('control_method', 'hwmon'),
                         'label': v.get('label', k),
                         'health': v.get('health', {})}
                     for k, v in state['fans'].items()},
            'temp_sensors': {k: {'value': v.get('value', 0),
                                 'label': v.get('label', k)}
                            for k, v in state['temp_sensors'].items()},
            'hdd_sensors': {k: {'temp': v.get('temp', 0),
                                'label': v.get('label', k)}
                           for k, v in state['hdd_sensors'].items()},
            'failsafe': state.get('failsafe', False),
            'standby_mode': state.get('standby_mode', False),
        }


def apply_server_config(config):
    """Apply config received from server to local state."""
    with state_lock:
        for fan_id, fan_cfg in config.get('fans', {}).items():
            if fan_id in state['fans']:
                for key in ('mode', 'target_temp', 'manual_pct', 'sensors',
                            'sensor_mode', 'schedule', 'inverted'):
                    if key in fan_cfg:
                        state['fans'][fan_id][key] = fan_cfg[key]


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



def make_handlers(sio_ref):
    """Create handler functions bound to the socketio client instance."""
    def _on_connect():
        logger.info(f'Connected to server')
        state['server_connected'] = True
        invalidate_state_cache()
        sio_ref.emit('agent:connect', {
            'node_id': state.get('node_id'),
            'node_name': state.get('node_name'),
            'api_token': state.get('api_token'),
            'control_mode': state['control_mode'],
            'config': get_local_config(),
            'version': CONFIG_VERSION,
        })

    def _on_disconnect():
        logger.warning('Disconnected from server')
        state['server_connected'] = False
        invalidate_state_cache()

    def _on_config_push(data):
        with state_lock:
            if state['control_mode'] != 'server':
                logger.info('Config push ignored — in manual mode')
                return
            state['agent_config_snapshot'] = get_local_config()
            apply_server_config(data.get('config', {}))
            invalidate_state_cache()
            logger.info('Applied server config')
        save_local_config()

    def _on_set_control_mode(data):
        mode = data.get('mode', 'server')
        with state_lock:
            state['control_mode'] = mode
            invalidate_state_cache()
        logger.info(f'Control mode set to: {mode}')
        save_local_config()

    def _on_command(data):
        cmd = data.get('command')
        if cmd == 'set_fan':
            fan_id = data.get('fan_id')
            value = data.get('value')
            with state_lock:
                if fan_id in state['fans']:
                    state['fans'][fan_id]['manual_pct'] = value
                    state['fans'][fan_id]['mode'] = 'manual'
            invalidate_state_cache()
            set_pwm(fan_id, int(value * 255 // 100))

    def _on_node_id_push(data):
        new_node_id = data.get('node_id', '')
        new_token = data.get('token', '')
        changed = False
        if new_node_id and new_node_id != state.get('node_id'):
            logger.info(f'Received node_id from server: {state.get("node_id")} → {new_node_id}')
            state['node_id'] = new_node_id
            changed = True
        if new_token and new_token != state.get('api_token'):
            state['api_token'] = new_token
            changed = True
        if changed:
            persist_node_id(new_node_id, new_token)

    def _on_dsm_apply(data):
        scheme_type = data.get('scheme_type')
        entries = data.get('entries', [])
        logger.info(f'Received DSM scheme apply: {scheme_type} ({len(entries)} entries)')
        try:
            for entry in entries:
                idx = entry.get('index')
                if idx is not None:
                    update_scheme_entry(
                        scheme_type, idx,
                        fan_speed_pct=entry.get('fan_speed_pct'),
                        action=entry.get('action'),
                        threshold_temp=entry.get('threshold_temp'),
                    )
            logger.info(f'DSM scheme {scheme_type} applied successfully')
        except Exception as e:
            logger.error(f'Failed to apply DSM scheme: {e}')

    def _on_update(data):
        """Server requests agent to update itself."""
        logger.info('=== AGENT UPDATE RECEIVED from server ===')
        repo_dir = '/repo'
        if not os.path.isdir(os.path.join(repo_dir, '.git')):
            logger.error('[agent-update] /repo has no .git')
            try:
                sio_ref.emit('agent:update_result', {'status': 'error', 'message': 'No .git in /repo'})
            except Exception:
                pass
            return

        def _emit_safe(event, data):
            try:
                sio_ref.emit(event, data)
                time.sleep(0.5)
            except Exception as e:
                logger.error(f'[agent-update] emit {event} failed: {e}')

        def _do_update():
            try:
                success, version = do_git_pull(repo_dir)
                if not success:
                    _emit_safe('agent:update_result', {'status': 'error', 'message': 'git pull failed'})
                    return
                logger.info(f'[agent-update] updated to: {version}')
                _emit_safe('agent:update_result', {'status': 'pulling', 'version': version})
                sync_repo_to_app(repo_dir, '/app')
                _emit_safe('agent:update_result', {'status': 'synced', 'version': version})
                time.sleep(1)
                schedule_restart(delay=1.0)
            except Exception as e:
                logger.error(f'[agent-update] error: {e}')
                _emit_safe('agent:update_result', {'status': 'error', 'message': str(e)})

        threading.Thread(target=_do_update, daemon=True).start()

    def _on_request_logs(data):
        """Server requests recent log lines from agent."""
        log_file = os.path.join(cfg.log_dir, 'fancontrol.log')
        lines_count = data.get('lines', 100)
        try:
            if os.path.isfile(log_file):
                with open(log_file, 'r', errors='replace') as f:
                    all_lines = f.readlines()
                    recent = all_lines[-lines_count:]
            else:
                recent = [f'Log file not found: {log_file}\n']
        except Exception as e:
            recent = [f'Error reading logs: {e}\n']
        sio_ref.emit('agent:logs', {
            'node_id': state.get('node_id'),
            'lines': recent,
        })

    return {
        'connect': _on_connect,
        'disconnect': _on_disconnect,
        'server:config_push': _on_config_push,
        'server:set_control_mode': _on_set_control_mode,
        'server:command': _on_command,
        'server:node_id_push': _on_node_id_push,
        'server:dsm:apply': _on_dsm_apply,
        'server:update': _on_update,
        'server:request_logs': _on_request_logs,
    }


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

# Embedded asset routes (monolith only)
@app.route('/js/<path:filename>')
def serve_js_embedded(filename):
    from flask import Response
    if filename in JS_MODULES:
        return Response(JS_MODULES[filename], mimetype='application/javascript')
    return 'Not found', 404

@app.route('/api/lang/<code>')
def api_get_lang_embedded(code):
    import re as _re
    if not _re.match(r'^[a-z]{2}$', code):
        return jsonify({'error': 'Invalid language code'}), 400
    if code in ('en', 'ru'):
        return jsonify(json.loads(TEMPLATE_LANGS.get(code, '{}')))
    return jsonify({}), 404

@app.route('/')
def index_embedded():
    from flask import Response
    html = TEMPLATE_HTML.replace('{{ config_version }}', CONFIG_VERSION)
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    return resp

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


# ==============================================================================
# EMBEDDED FRONTEND ASSETS
# ==============================================================================

TEMPLATE_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n    <meta charset="UTF-8">\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">\n    <meta http-equiv="Pragma" content="no-cache">\n    <meta http-equiv="Expires" content="0">\n    <title>FanControl v{{ config_version }} - Neon Cyberpunk</title>\n    \n    <!-- Tailwind CSS via CDN -->\n    <script src="https://cdn.tailwindcss.com"></script>\n    <script>\n        tailwind.config = {\n            darkMode: \'class\',\n            theme: {\n                extend: {\n                    colors: {\n                        \'cyber-bg\': \'#0b0e14\',\n                        \'cyber-card\': \'#131820\',\n                        \'cyber-accent\': \'#1a1f2e\',\n                        \'neon-cyan\': \'#00f0ff\',\n                        \'neon-purple\': \'#b347ea\',\n                        \'neon-red\': \'#ff2d55\',\n                        \'neon-orange\': \'#ff9f0a\',\n                        \'neon-green\': \'#30d158\',\n                    },\n                    boxShadow: {\n                        \'neon-cyan\': \'0 0 15px rgba(0, 240, 255, 0.3)\',\n                        \'neon-purple\': \'0 0 15px rgba(179, 71, 234, 0.3)\',\n                        \'neon-red\': \'0 0 20px rgba(255, 45, 85, 0.4)\',\n                        \'neon-green\': \'0 0 10px rgba(48, 209, 88, 0.3)\',\n                    },\n                    animation: {\n                        \'pulse-red\': \'pulseRed 2s ease-in-out infinite\',\n                        \'spin-slow\': \'spin 3s linear infinite\',\n                    },\n                    keyframes: {\n                        pulseRed: {\n                            \'0%, 100%\': { boxShadow: \'0 0 10px rgba(255, 45, 85, 0.2)\' },\n                            \'50%\': { boxShadow: \'0 0 30px rgba(255, 45, 85, 0.6)\' },\n                        },\n                    },\n                },\n            },\n        }\n    </script>\n    \n    <!-- Socket.IO -->\n    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>\n    \n    <!-- ApexCharts -->\n    <script src="https://cdn.jsdelivr.net/npm/apexcharts@3.44.2"></script>\n    \n    <style>\n        @import url(\'https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600;700&display=swap\');\n        \n        * { font-family: \'Inter\', sans-serif; }\n        .font-mono { font-family: \'JetBrains Mono\', monospace; }\n        \n        /* Scrollbar styling */\n        ::-webkit-scrollbar { width: 4px; }\n        ::-webkit-scrollbar-track { background: #0b0e14; }\n        ::-webkit-scrollbar-thumb { background: #1a1f2e; border-radius: 2px; }\n        ::-webkit-scrollbar-thumb:hover { background: #b347ea; }\n        \n        /* Fan animation */\n        @keyframes fan-spin {\n            from { transform: rotate(0deg); }\n            to { transform: rotate(360deg); }\n        }\n        .fan-spinning { animation: fan-spin var(--fan-duration, 1s) linear infinite; }\n        \n        /* Glow text */\n        .glow-cyan { text-shadow: 0 0 10px rgba(0, 240, 255, 0.5); }\n        .glow-purple { text-shadow: 0 0 10px rgba(179, 71, 234, 0.5); }\n        .glow-red { text-shadow: 0 0 10px rgba(255, 45, 85, 0.5); }\n        \n        /* Progress bar animation */\n        .progress-fill {\n            transition: width 1.5s ease-in-out, background 1.5s ease-in-out;\n        }\n        \n        /* Pulse animation for alerts */\n        @keyframes alertPulse {\n            0%, 100% { opacity: 1; transform: scale(1); }\n            50% { opacity: 0.5; transform: scale(1.2); }\n        }\n        .alert-pulse { animation: alertPulse 1.2s ease-in-out infinite; }\n\n        /* Fan health alert classes — pulse handled by JS inline styles */\n        .fan-alert-stopped,\n        .fan-alert-slowing,\n        .fan-alert-needs-calibration {\n            transition: none !important;\n        }\n        \n        /* Compact mode */\n        body.compact-mode .fan-card { padding: 0.375rem 0.5rem !important; }\n        body.compact-mode .fan-card .text-sm { font-size: 0.7rem !important; }\n        body.compact-mode .fan-card .text-xs { font-size: 0.6rem !important; }\n        body.compact-mode #fan-list > div { margin-bottom: 0.25rem !important; }\n        body.compact-mode #disks-mini-list > div { margin-bottom: 0.125rem !important; }\n        body.compact-mode #fan-icon-container { width: 2.5rem !important; height: 2.5rem !important; }\n        body.compact-mode #fan-icon-svg { width: 2rem !important; height: 2rem !important; }\n        body.compact-mode #inspector-fan > .space-y-6 > div { padding-top: 0.75rem !important; padding-bottom: 0.75rem !important; }\n        body.compact-mode #fan-name { font-size: 1.1rem !important; }\n        body.compact-mode #fan-rpm-display { font-size: 1.5rem !important; }\n        body.compact-mode #pwm-slider { height: 0.375rem !important; }\n        body.compact-mode #inspector-content { padding: 1rem !important; }\n        body.compact-mode .text-2xl { font-size: 1.1rem !important; }\n        body.compact-mode .text-3xl { font-size: 1.5rem !important; }\n        body.compact-mode .text-6xl { font-size: 2.5rem !important; }\n        body.compact-mode #schedule-grid td { width: 14px !important; height: 14px !important; }\n        body.compact-mode #schedule-grid th { font-size: 8px !important; }\n\n        /* Dashboard cards */\n        .dashboard-card {\n            transition: box-shadow 0.2s ease, border-color 0.2s ease;\n            user-select: none;\n        }\n        .dashboard-card:hover {\n            border-color: #06b6d4;\n            box-shadow: 0 0 12px rgba(6, 182, 212, 0.3);\n        }\n        .dashboard-group {\n            transition: border-color 0.2s ease;\n        }\n        .dashboard-group:hover {\n            border-color: #a855f7;\n        }\n        .group-resize-handle {\n            background: linear-gradient(135deg, transparent 50%, #6b7280 50%);\n            border-radius: 0 0 8px 0;\n        }\n        .card-resize-handle {\n            position: absolute;\n            bottom: 4px;\n            right: 4px;\n            width: 18px;\n            height: 18px;\n            cursor: se-resize;\n            opacity: 0;\n            transition: opacity 0.2s;\n            display: flex;\n            align-items: center;\n            justify-content: center;\n            z-index: 10;\n        }\n        .card-resize-handle::after {\n            content: \'\';\n            width: 10px;\n            height: 10px;\n            border-right: 2px solid #6b7280;\n            border-bottom: 2px solid #6b7280;\n            border-radius: 0 0 2px 0;\n        }\n        [data-card-id]:hover .card-resize-handle {\n            opacity: 0.6;\n        }\n        .card-resize-handle:hover {\n            opacity: 1 !important;\n        }\n        @keyframes pulse-green {\n            0%, 100% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7); }\n            50% { box-shadow: 0 0 0 6px rgba(74, 222, 128, 0); }\n        }\n        @keyframes pulse-red {\n            0%, 100% { box-shadow: 0 0 0 0 rgba(248, 113, 113, 0.7); }\n            50% { box-shadow: 0 0 0 6px rgba(248, 113, 113, 0); }\n        }\n        @keyframes pulse-yellow {\n            0%, 100% { box-shadow: 0 0 0 0 rgba(250, 204, 21, 0.7); }\n            50% { box-shadow: 0 0 0 6px rgba(250, 204, 21, 0); }\n        }\n        .status-dot {\n            width: 8px;\n            height: 8px;\n            border-radius: 50%;\n            display: inline-block;\n        }\n        .status-dot.green { background: #4ade80; animation: pulse-green 2s infinite; }\n        .status-dot.red { background: #f87171; animation: pulse-red 2s infinite; }\n        .status-dot.yellow { background: #facc15; animation: pulse-yellow 2s infinite; }\n        .card-gradient-fan {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(34,211,238,0.1), rgba(34,211,238,0.02));\n        }\n        .card-gradient-fan:hover {\n            background-image: linear-gradient(135deg, rgba(34,211,238,0.18), rgba(34,211,238,0.05));\n        }\n        .card-gradient-temperature {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(74,222,128,0.1), rgba(74,222,128,0.02));\n        }\n        .card-gradient-temperature:hover {\n            background-image: linear-gradient(135deg, rgba(74,222,128,0.18), rgba(74,222,128,0.05));\n        }\n        .card-gradient-disk {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(192,132,252,0.1), rgba(192,132,252,0.02));\n        }\n        .card-gradient-disk:hover {\n            background-image: linear-gradient(135deg, rgba(192,132,252,0.18), rgba(192,132,252,0.05));\n        }\n        .card-gradient-system {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(250,204,21,0.1), rgba(250,204,21,0.02));\n        }\n        .card-gradient-system:hover {\n            background-image: linear-gradient(135deg, rgba(250,204,21,0.18), rgba(250,204,21,0.05));\n        }\n    </style>\n    <style>\n        .toast-container {\n            position: fixed;\n            top: 20px;\n            right: 20px;\n            z-index: 1000;\n            display: flex;\n            flex-direction: column;\n            gap: 10px;\n        }\n        .toast {\n            background: #1a1f2e;\n            border: 1px solid #22d3ee;\n            border-radius: 8px;\n            padding: 12px 16px;\n            color: #e5e7eb;\n            box-shadow: 0 4px 20px rgba(34, 211, 238, 0.2);\n            animation: toast-in 0.3s ease-out;\n            max-width: 350px;\n            display: flex;\n            align-items: center;\n            gap: 8px;\n            flex-wrap: wrap;\n        }\n        .toast-success { border-color: #4ade80; }\n        .toast-warning { border-color: #facc15; }\n        .toast-error { border-color: #f87171; }\n        @keyframes toast-in {\n            from { opacity: 0; transform: translateX(100px); }\n            to { opacity: 1; transform: translateX(0); }\n        }\n        .toast-btn {\n            background: #22d3ee;\n            color: #0f172a;\n            border: none;\n            border-radius: 4px;\n            padding: 4px 12px;\n            cursor: pointer;\n            font-weight: 600;\n            white-space: nowrap;\n        }\n        .toast-btn:hover { background: #06b6d4; }\n        .toast-btn-secondary {\n            background: transparent;\n            border: 1px solid #4b5563;\n            color: #9ca3af;\n        }\n        .toast-btn-secondary:hover { border-color: #6b7280; }\n    </style>\n</head>\n<body class="bg-cyber-bg text-gray-200 min-h-screen">\n    \n    <!-- ======================================================================== -->\n    <!-- SETUP WIZARD (shown when system not initialized) -->\n    <!-- ======================================================================== -->\n    <div id="setup-screen" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-cyber-bg bg-opacity-95">\n        <div class="bg-cyber-card border border-cyber-accent rounded-2xl p-10 max-w-2xl w-full mx-4 shadow-neon-purple">\n            \n            <!-- Language selector for setup -->\n            <div class="flex justify-end mb-4">\n                <div class="flex gap-1">\n                    <button onclick="switchLanguage(\'en\')" id="setup-lang-en"\n                            class="text-xs px-2 py-1 rounded border transition-all">EN</button>\n                    <button onclick="switchLanguage(\'ru\')" id="setup-lang-ru"\n                            class="text-xs px-2 py-1 rounded border transition-all">RU</button>\n                </div>\n            </div>\n                \n            <!-- Step 1: Intro -->\n            <div id="setup-step-intro" class="text-center">\n                <div class="text-6xl mb-6">🌀</div>\n                <h2 class="text-2xl font-bold text-white mb-4 glow-cyan" data-i18n="setup.heading">Initial System Setup</h2>\n                <p class="text-gray-400 mb-8" data-i18n="setup.description">\n                    No configuration found. System needs to scan available data buses \n                    to automatically detect fans and temperature sensors.\n                </p>\n                <button id="discover-btn" onclick="runDiscovery()" \n                        class="bg-neon-purple hover:bg-purple-700 text-white font-bold py-3 px-8 rounded-lg \n                               transition-all duration-300 hover:shadow-neon-purple disabled:opacity-50 disabled:cursor-not-allowed">\n                    🔍 <span data-i18n="setup.scan_btn">Start Hardware Scan</span>\n                </button>\n                <div id="discover-loader" class="hidden mt-4 text-neon-cyan animate-pulse">\n                    <span data-i18n="setup.scanning">Scanning sysfs bus and querying smartctl...</span>\n                </div>\n            </div>\n            \n            <!-- Step 2: Results -->\n            <div id="setup-step-results" class="hidden">\n                <h3 class="text-xl font-bold text-neon-green mb-4" data-i18n="setup.results_title">✅ Hardware Detected</h3>\n                <div id="discovered-devices" class="max-h-96 overflow-y-auto space-y-3 mb-6"></div>\n                <div id="setup-step-action" class="hidden text-center">\n                    <!-- Control Mode Selection -->\n                    <div id="control-mode-select" class="mb-6">\n                        <p class="text-gray-400 text-sm mb-4" data-i18n="control.choose_mode">Choose how to control fans:</p>\n                        <div class="flex justify-center gap-4">\n                            <button id="btn-hwmon" onclick="selectControlMode(\'hwmon\')"\n                                    class="card-hover relative w-52 p-4 rounded-xl border border-gray-700 bg-gray-800/50 text-left transition-all">\n                                <div class="flex items-center gap-2 mb-2">\n                                    <span class="text-xl">🐧</span>\n                                    <span class="text-white font-semibold text-sm" data-i18n="control.direct">Direct Control</span>\n                                </div>\n                                <div class="text-gray-400 text-xs">hwmon / PWM</div>\n                                <p class="text-gray-500 text-[10px] mt-2" data-i18n="control.direct_desc">Direct fan speed control via Linux sysfs. Requires calibration to determine RPM curves.</p>\n                            </button>\n                            <button id="btn-dsm" onclick="selectControlMode(\'dsm\')"\n                                    class="card-hover relative w-52 p-4 rounded-xl border border-gray-700 bg-gray-800/50 text-left transition-all">\n                                <div class="flex items-center gap-2 mb-2">\n                                    <span class="text-xl">🌡</span>\n                                    <span class="text-white font-semibold text-sm" data-i18n="control.dsm_scheme">DSM Scheme</span>\n                                </div>\n                                <div class="text-gray-400 text-xs">scemd.xml</div>\n                                <p class="text-gray-500 text-[10px] mt-2" data-i18n="control.dsm_scheme_desc">Control fans by editing DSM temperature-threshold schemes. No calibration needed.</p>\n                            </button>\n                        </div>\n                        <p id="mode-unavailable-hint" class="text-neon-orange text-xs mt-3 hidden"></p>\n                    </div>\n\n                    <!-- HWMon action (calibration) -->\n                    <div id="hwmon-action" class="hidden">\n                        <p id="calibrate-hint" class="text-gray-400 mb-4" data-i18n="setup.calibrate_hint">To complete setup, fans must be calibrated. This takes about 1-2 minutes.</p>\n                        <button id="calibrate-btn" onclick="runCalibration()"\n                                class="bg-neon-cyan hover:bg-cyan-600 text-black font-bold py-3 px-8 rounded-lg \n                                       transition-all duration-300 hover:shadow-neon-cyan disabled:opacity-50 disabled:cursor-not-allowed">\n                            <span data-i18n="setup.calibrate_btn">Start Fan Calibration</span>\n                        </button>\n                        <div id="calibrate-loader" class="hidden mt-4 text-neon-cyan animate-pulse">\n                            <span data-i18n="calibration.determining">Calibrating: determining PWM/RPM curves...</span>\n                        </div>\n                    </div>\n\n                    <!-- DSM action (scheme editor) -->\n                    <div id="dsm-action" class="hidden">\n                        <div class="bg-yellow-900/20 border border-yellow-500/30 rounded-lg p-4 mb-4 text-left max-w-md mx-auto">\n                            <div class="flex items-start gap-2">\n                                <span class="text-yellow-400 mt-0.5">⚠</span>\n                                <div>\n                                    <p class="text-yellow-300 text-sm font-semibold mb-1" data-i18n="control.dsm_scheme_warning_title">DSM Scheme Control</p>\n                                    <p class="text-gray-400 text-xs" data-i18n="control.dsm_scheme_warning_desc">Direct speed control is not available in this mode. You configure temperature thresholds and corresponding fan speeds by editing the DSM scheme table.</p>\n                                </div>\n                            </div>\n                        </div>\n                        <button onclick="applyDsmAndContinue()"\n                                class="bg-neon-cyan hover:bg-cyan-600 text-black font-bold py-3 px-8 rounded-lg transition-all duration-300">\n                            <span data-i18n="control.open_dsm_editor">Open DSM Scheme Editor</span>\n                        </button>\n                    </div>\n                </div>\n            </div>\n        </div>\n    </div>\n    \n    \n    <!-- ======================================================================== -->\n    <!-- MAIN DASHBOARD -->\n    <!-- ======================================================================== -->\n    <div id="main-screen" class="hidden flex flex-row h-screen">\n        \n        <!-- ======================================================================== -->\n        <!-- LEFT SIDEBAR - Server Tree (always visible) -->\n        <!-- ======================================================================== -->\n        <div class="w-64 bg-cyber-card border-r border-cyber-accent flex flex-col flex-shrink-0">\n            \n            <!-- Header -->\n            <div class="p-3 border-b border-cyber-accent">\n                <div class="flex items-center justify-between">\n                    <div class="flex items-center gap-1.5">\n                        <h1 class="text-sm font-bold glow-cyan" data-i18n="app.title">FanControl</h1>\n                        <button onclick="openServerNameEdit()" class="text-gray-500 hover:text-neon-cyan transition-colors text-xs" title="Rename server">✎</button>\n                    </div>\n                    <div class="flex items-center gap-1">\n                        <span id="header-version" class="text-xs bg-neon-purple bg-opacity-20 text-neon-purple px-1.5 py-0.5 rounded"></span>\n                        <button onclick="toggleSettings()" class="relative text-gray-400 hover:text-neon-cyan transition-colors p-1 text-sm" title="Settings">\n                            <span id="update-badge" class="hidden absolute -top-0.5 -right-0.5 w-2.5 h-2.5 bg-neon-red rounded-full animate-pulse"></span>\n                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-.426-1.756-.426-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>\n                        </button>\n                    </div>\n                </div>\n                <div class="flex items-center gap-1 text-xs text-gray-500 mt-1">\n                    <span class="w-1.5 h-1.5 bg-neon-green rounded-full"></span>\n                    <span data-i18n="header.synced">Synced</span>\n                </div>\n            </div>\n            \n            <!-- Navigation -->\n            <div class="border-b border-cyber-accent">\n                <button id="nav-dashboard-btn" onclick="showView(\'dashboard\')"\n                        class="w-full py-2 text-xs font-semibold text-neon-cyan border-b-2 border-neon-cyan transition-all flex items-center justify-center gap-1.5">\n                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zm10 0a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z"/></svg>\n                    <span data-i18n="nav.dashboard">Dashboard</span>\n                </button>\n            </div>\n            \n            <!-- Server Tree -->\n            <div class="flex-1 overflow-y-auto p-2 space-y-1" id="server-tree">\n                <div class="text-center text-gray-500 py-4 text-xs" data-i18n="setup.loading_fans">Loading...</div>\n            </div>\n            \n            <!-- Add Node (server mode only) -->\n            <div id="add-node-section" class="border-t border-cyber-accent p-2">\n                <div class="flex gap-1 mb-1">\n                    <input id="new-node-name" type="text"\n                           class="flex-1 bg-cyber-bg border border-cyber-accent rounded px-2 py-1 text-xs text-white focus:border-neon-cyan focus:outline-none min-w-0"\n                           placeholder="Node name..." data-i18n-placeholder="nodes.name_placeholder"\n                           onkeydown="if(event.key===\'Enter\')addNode()">\n                    <button onclick="addNode()"\n                            class="px-2 py-1 bg-cyber-accent border border-cyber-accent rounded text-neon-cyan text-xs hover:bg-neon-cyan hover:bg-opacity-20 transition-all flex-shrink-0">\n                        +\n                    </button>\n                </div>\n                <div class="flex gap-1">\n                    <input id="new-node-ip" type="text"\n                           class="flex-1 bg-cyber-bg border border-cyber-accent rounded px-2 py-1 text-xs text-white focus:border-neon-cyan focus:outline-none min-w-0"\n                           placeholder="IP address (optional)" data-i18n-placeholder="nodes.ip_placeholder"\n                           onkeydown="if(event.key===\'Enter\')addNode()">\n                    <button onclick="scanForAgents()" id="scan-agents-btn"\n                            class="px-2 py-1 bg-cyber-accent border border-cyber-accent rounded text-neon-cyan text-xs hover:bg-neon-cyan hover:bg-opacity-20 transition-all flex-shrink-0" title="Scan for agents">\n                        &#128269;\n                    </button>\n                </div>\n                <!-- Discovered agents list -->\n                <div id="discovered-agents-list" class="hidden mt-2 space-y-1"></div>\n            </div>\n\n            <!-- Agent Update Button -->\n            <div id="agent-update-section" class="hidden border-t border-cyber-accent p-2">\n                <button onclick="openUpdateModal()" id="agent-update-btn"\n                        class="w-full py-1.5 bg-gray-800 hover:bg-gray-700 border border-gray-600 rounded text-gray-400 hover:text-white text-xs transition-all flex items-center justify-center gap-1.5 relative">\n                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>\n                    <span data-i18n="agent.update">Update</span>\n                    <span id="agent-update-badge" class="hidden absolute -top-1 -right-1 w-3 h-3 bg-neon-green rounded-full"></span>\n                </button>\n            </div>\n\n            <!-- Update Outdated Agents (server mode only) -->\n            <div id="update-agents-outdated-section" class="hidden border-t border-cyber-accent p-2">\n                <button onclick="openUpdateAgentsModal()" id="update-agents-outdated-btn"\n                        class="w-full py-1.5 bg-orange-900/30 hover:bg-orange-900/50 border border-orange-700/50 rounded text-orange-400 hover:text-orange-300 text-xs transition-all flex items-center justify-center gap-1.5 relative">\n                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>\n                    <span data-i18n="nodes.update_agents">Update Agents</span>\n                    <span id="outdated-agents-count" class="ml-1 text-[10px] bg-orange-800 text-orange-300 px-1 rounded"></span>\n                </button>\n            </div>\n\n            <!-- Agent Token (shown in agent mode) -->\n            <div id="agent-token-section" class="hidden border-t border-cyber-accent p-2">\n                <div class="text-[10px] text-gray-500 mb-1">API Token (paste on server)</div>\n                <div class="flex items-center gap-1">\n                    <code id="agent-token-value" class="flex-1 text-[10px] text-neon-cyan bg-cyber-bg rounded px-2 py-1 truncate select-all cursor-pointer" title="Click to select"></code>\n                    <button onclick="copyAgentToken()" class="text-gray-400 hover:text-neon-cyan text-[10px] px-1 flex-shrink-0" title="Copy">&#128203;</button>\n                </div>\n            </div>\n        </div>\n        \n        <!-- ======================================================================== -->\n        <!-- MAIN CONTENT - Dashboard or Inspector -->\n        <!-- ======================================================================== -->\n        <div class="flex-1 flex flex-col overflow-hidden bg-cyber-bg relative">\n            \n            <!-- Dashboard Canvas (full screen) -->\n            <div id="dashboard-canvas-container" class="flex-1 overflow-auto relative">\n                <!-- Agent mode: show token prominently when dashboard is empty -->\n                <div id="agent-token-banner" class="hidden m-4 p-4 bg-cyber-card border border-neon-purple/30 rounded-xl">\n                    <div class="flex items-center gap-2 mb-2">\n                        <span class="text-neon-purple text-lg">🔑</span>\n                        <span class="text-white font-semibold text-sm">Agent Token</span>\n                        <span class="text-gray-500 text-xs">— paste this in server node settings</span>\n                    </div>\n                    <div class="flex items-center gap-2">\n                        <code id="agent-token-banner-value" class="flex-1 text-sm text-neon-cyan bg-cyber-bg rounded px-3 py-2 font-mono select-all cursor-pointer break-all"></code>\n                        <button onclick="copyAgentToken()" class="px-3 py-2 bg-cyber-accent hover:bg-gray-700 text-gray-300 hover:text-white rounded text-xs transition-all flex-shrink-0">Copy</button>\n                    </div>\n                    <div class="text-xs text-gray-600 mt-2">Also visible in left sidebar ⬅</div>\n                </div>\n                <div id="dashboard-empty" class="flex flex-col items-center justify-center text-gray-500 py-20">\n                    <div class="text-4xl mb-4">📊</div>\n                    <p class="text-sm" data-i18n="dashboard.empty">Dashboard is empty</p>\n                    <p class="text-xs text-gray-600 mt-1" data-i18n="dashboard.empty_hint">Click + to add monitoring cards</p>\n                </div>\n                <div id="dashboard-canvas" class="p-4" style="display: grid; grid-template-columns: repeat(12, 1fr); grid-auto-rows: 100px; gap: 8px; position: relative;"></div>\n                <button id="dashboard-add-btn" onclick="showCardPicker()"\n                        class="fixed bottom-6 right-6 w-12 h-12 bg-neon-cyan rounded-full text-black text-2xl font-bold shadow-lg hover:bg-cyan-400 transition-all z-40">\n                    +\n                </button>\n                <button id="dashboard-group-btn" onclick="showGroupCreator()"\n                        class="fixed bottom-6 right-20 w-12 h-12 bg-neon-purple rounded-full text-white text-lg shadow-lg hover:bg-purple-400 transition-all z-40">\n                    ⊞\n                </button>\n            </div>\n            \n            <!-- Inspector (shown when fan selected) -->\n            <div id="inspector-container" class="hidden flex-1 flex flex-col overflow-hidden">\n                \n                <!-- Top Bar -->\n                <div class="p-4 border-b border-cyber-accent flex items-center justify-between">\n                    <div>\n                        <h2 id="inspector-title" class="text-xl font-bold text-white" data-i18n="inspector.select">Select a device</h2>\n                        <p id="inspector-subtitle" class="text-xs text-gray-500" data-i18n="inspector.hint">Click on a fan to inspect</p>\n                    </div>\n                    <div class="flex items-center gap-3">\n                        <div id="failsafe-indicator" class="hidden flex items-center gap-2 bg-red-900 bg-opacity-30 text-neon-red px-3 py-1 rounded-lg alert-pulse">\n                            <span class="w-2 h-2 bg-neon-red rounded-full"></span> FAILSAFE\n                        </div>\n                        <div id="standby-indicator" class="hidden flex items-center gap-2 bg-blue-900 bg-opacity-30 text-blue-400 px-3 py-1 rounded-lg">\n                            <span class="w-2 h-2 bg-blue-400 rounded-full"></span> STANDBY\n                        </div>\n                        <button onclick="startCalibration()" \n                                class="text-xs bg-neon-purple bg-opacity-20 text-neon-purple px-3 py-1.5 rounded-lg hover:bg-opacity-40 transition-all" data-i18n="setup.calibrate_btn_short">\n                            Recalibrate\n                        </button>\n                        <button onclick="showView(\'dashboard\')" \n                                class="text-xs bg-gray-700 text-gray-300 px-3 py-1.5 rounded-lg hover:bg-gray-600 transition-all">\n                            <span data-i18n="inspector.back">← Back to Dashboard</span>\n                        </button>\n                    </div>\n                </div>\n            \n            <!-- Inspector Content -->\n            <div class="flex-1 overflow-y-auto p-6" id="inspector-content">\n                \n                <!-- Empty State -->\n                <div id="inspector-empty" class="flex flex-col items-center justify-center h-full text-gray-600">\n                    <div class="text-6xl mb-4">🌀</div>\n                    <p class="text-lg" data-i18n="inspector.select">Select a fan from the left panel</p>\n                    <p class="text-sm" data-i18n="inspector.hint_detail">to view controls and analytics</p>\n                </div>\n                \n                <!-- Fan Inspector (hidden by default) -->\n                <div id="inspector-fan" class="hidden space-y-6">\n                    \n                    <!-- Fan Header -->\n                    <div class="flex items-center gap-4">\n                        <div class="w-16 h-16 flex items-center justify-center" id="fan-icon-container">\n                            <svg id="fan-icon-svg" class="w-12 h-12 text-neon-cyan" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">\n                                <path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"/>\n                                <circle cx="12" cy="12" r="3"/>\n                            </svg>\n                        </div>\n                        <div>\n                            <h3 id="fan-name" class="text-2xl font-bold text-white" data-i18n-placeholder="inspector.fan_name">Fan Name</h3>\n                            <div class="flex items-center gap-2 mt-1">\n                                <span id="fan-inverted-badge" class="hidden text-xs px-2 py-0.5 rounded-full bg-cyan-900 bg-opacity-30 text-neon-cyan" data-i18n="fan.inverted">INVERTED</span>\n                                <span id="fan-status-badge" class="text-xs px-2 py-0.5 rounded-full" data-i18n="inspector.status">Status</span>\n                                <span id="fan-mode-badge" class="text-xs px-2 py-0.5 rounded-full" data-i18n="inspector.mode">Mode</span>\n                            </div>\n                        </div>\n                        <div class="ml-auto text-right">\n                            <div id="fan-rpm-display" class="text-3xl font-bold font-mono text-neon-cyan">0</div>\n                            <div class="text-xs text-gray-500" data-i18n="fan.rpm">RPM</div>\n                        </div>\n                    </div>\n                    \n                    <!-- PWM Slider -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <div class="flex items-center justify-between mb-3">\n                            <label class="text-sm font-semibold text-gray-300" data-i18n="inspector.fan_speed">Fan Speed</label>\n                            <span id="pwm-value-display" class="text-lg font-bold font-mono text-neon-purple">50%</span>\n                        </div>\n                        <input type="range" id="pwm-slider" min="0" max="100" value="50"\n                               class="w-full h-2 bg-cyber-accent rounded-lg appearance-none cursor-pointer\n                                      accent-neon-purple [&::-webkit-slider-thumb]:appearance-none \n                                      [&::-webkit-slider-thumb]:w-5 [&::-webkit-slider-thumb]:h-5 \n                                      [&::-webkit-slider-thumb]:bg-neon-purple [&::-webkit-slider-thumb]:rounded-full\n                                      [&::-webkit-slider-thumb]:shadow-neon-purple [&::-webkit-slider-thumb]:cursor-pointer">\n                        <div class="flex justify-between text-xs text-gray-500 mt-1">\n                            <span>0%</span><span>50%</span><span>100%</span>\n                        </div>\n                    </div>\n                    \n                    <!-- PWM Range -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <div class="flex items-center justify-between mb-3">\n                            <label class="text-sm font-semibold text-gray-300 flex items-center gap-1">\n                                PWM Range\n                                <span class="relative group">\n                                    <span class="text-gray-500 cursor-help">&#x24D8;</span>\n                                    <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50" data-i18n="calibration.pwm_range_hint">Dead zone boundaries. Min = lowest PWM where fan spins. Max = PWM where fan reaches full speed. 0-100% slider maps only to this range.</span>\n                                </span>\n                            </label>\n                        </div>\n                        <div class="flex items-center gap-2 mt-1">\n                            <span class="text-xs text-gray-500 w-8 flex items-center gap-1" data-i18n="calibration.min_pwm">Min\n                                <span class="relative group">\n                                    <span class="text-gray-500 cursor-help">&#x24D8;</span>\n                                    <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-56 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50">Overrides auto-calculated value. Recalibrate to restore automatic detection.</span>\n                                </span>\n                            </span>\n                            <input id="cal-min-pwm" type="range" min="0" max="255" value="0"\n                                   class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"\n                                   oninput="updateCalibrationParam(\'min_pwm\', this.value)">\n                            <span id="cal-min-pwm-val" class="text-xs text-gray-400 w-8 text-right">0</span>\n                        </div>\n                        <div class="flex items-center gap-2 mt-2">\n                            <span class="text-xs text-gray-500 w-8 flex items-center gap-1" data-i18n="calibration.max_pwm">Max\n                                <span class="relative group">\n                                    <span class="text-gray-500 cursor-help">&#x24D8;</span>\n                                    <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-56 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50">Overrides auto-calculated value. Recalibrate to restore automatic detection.</span>\n                                </span>\n                            </span>\n                            <input id="cal-max-pwm" type="range" min="0" max="255" value="255"\n                                   class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"\n                                   oninput="updateCalibrationParam(\'max_pwm\', this.value)">\n                            <span id="cal-max-pwm-val" class="text-xs text-gray-400 w-8 text-right">255</span>\n                        </div>\n                    </div>\n                    \n                    <!-- Lambda -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <div class="flex items-center justify-between mb-3">\n                            <label class="text-sm font-semibold text-gray-300 flex items-center gap-1">\n                                Curve Shape\n                                <span class="relative group">\n                                    <span class="text-gray-500 cursor-help">&#x24D8;</span>\n                                    <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50" data-i18n="calibration.lambda_hint">Controls fan response curve. 1.0 = linear. Lower = fan ramps up faster at low %. Higher = fan stays quiet longer, ramps up near 100%.</span>\n                                </span>\n                            </label>\n                            <span id="cal-lambda-val" class="text-sm font-mono text-neon-cyan">1.0</span>\n                        </div>\n                        <div class="flex items-center gap-2">\n                            <span class="text-xs text-gray-500">0.3</span>\n                            <input id="cal-lambda" type="range" min="3" max="30" value="10"\n                                   class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"\n                                   oninput="updateCalibrationParam(\'lambda\', this.value / 10)">\n                            <span class="text-xs text-gray-500">3.0</span>\n                        </div>\n                    </div>\n                    \n                    <!-- Control Buttons -->\n                    <div class="grid grid-cols-2 gap-3">\n                        <button id="btn-mode-manual" onclick="setFanMode(\'manual\')"\n                                class="py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300\n                                       bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30\n                                       hover:bg-opacity-40 hover:shadow-neon-purple" data-i18n="mode.manual">\n                            🎮 Manual\n                        </button>\n                        <button id="btn-mode-auto" onclick="setFanMode(\'auto\')"\n                                class="py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300\n                                       bg-cyber-accent text-gray-400 border border-gray-700\n                                       hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan" data-i18n="mode.auto">\n                            🤖 Auto\n                        </button>\n                    </div>\n                    \n                    <!-- Auto Mode Settings -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent" id="auto-settings" style="display:none;">\n                        <div class="flex items-center justify-between mb-3">\n                            <h4 class="text-sm font-semibold text-gray-300" data-i18n="schedule.weekly">Weekly Schedule</h4>\n                            <span id="schedule-coverage" class="text-xs text-gray-500"></span>\n                        </div>\n                        \n                        <div id="no-sensor-warning" class="hidden bg-yellow-900 bg-opacity-30 border border-yellow-600 rounded-lg p-3 mb-3">\n                            <p class="text-sm text-yellow-300 font-semibold mb-1">No sensors assigned</p>\n                            <p class="text-xs text-yellow-400 mb-2">Assign sensors in the first schedule cell, or globally below.</p>\n                        </div>\n                        \n                        <div id="schedule-incomplete-warning" class="hidden bg-yellow-900 bg-opacity-30 border border-yellow-600 rounded-lg p-3 mb-3">\n                            <p class="text-sm text-yellow-300 font-semibold mb-1" data-i18n="schedule.incomplete">Schedule incomplete</p>\n                            <p id="schedule-incomplete-detail" class="text-xs text-yellow-400"></p>\n                        </div>\n                        \n                        <!-- Schedule Grid -->\n                        <div class="overflow-x-auto mb-3">\n                            <div id="schedule-grid" class="inline-block"></div>\n                        </div>\n                        \n                        <!-- Legend -->\n                        <div class="flex items-center gap-4 text-xs text-gray-500 mb-3">\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#15803d"></span> <span data-i18n="schedule.legend_auto">Auto</span></span>\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#c2410c"></span> <span data-i18n="schedule.legend_manual">Manual</span></span>\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#991b1b"></span> <span data-i18n="schedule.legend_off">Off</span></span>\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#1f2937"></span> <span data-i18n="schedule.legend_empty">Empty</span></span>\n                        </div>\n                        \n                        <!-- Schedule Rules Summary -->\n                        <div id="schedule-rules" class="mb-3"></div>\n                        \n                        <div class="flex gap-2">\n                            <button onclick="clearSchedule()" \n                                    class="text-xs bg-red-900 bg-opacity-30 text-red-400 px-3 py-1.5 rounded-lg hover:bg-opacity-50 transition-all" data-i18n="schedule.clear_all">\n                                Clear All\n                            </button>\n                            <button onclick="fillScheduleDefaults()" \n                                    class="text-xs bg-cyber-accent text-gray-400 px-3 py-1.5 rounded-lg hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple transition-all" data-i18n="schedule.fill_auto">\n                                Fill Empty with Auto\n                            </button>\n                        </div>\n                    </div>\n\n                    <!-- Health & Service -->\n                    <div id="fan-service-section" class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                    </div>\n\n                    <!-- Chart -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <h4 class="text-sm font-semibold text-gray-300 mb-3" data-i18n="chart.temp_history">Temperature History (24h)</h4>\n                        <div id="temp-chart" class="h-64"></div>\n                    </div>\n                </div><!-- /inspector-fan -->\n                </div><!-- /inspector-content -->\n            </div><!-- /inspector-container -->\n\n            <!-- Nodes Overview Grid -->\n            <div id="nodes-grid" class="hidden flex-1 overflow-auto p-4">\n                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" id="nodes-grid-inner"></div>\n            </div>\n\n            <!-- Node Detail View -->\n            <div id="node-detail-content" class="hidden flex-1 overflow-auto p-4">\n                <div id="node-detail-inner"></div>\n            </div>\n\n            <!-- DSM Scheme Editor -->\n            <div id="dsm-scheme-container" class="hidden flex-1 overflow-auto p-4">\n                <div id="dsm-scheme-inner"></div>\n            </div>\n\n        </div><!-- /main content -->\n    </div><!-- /main-screen -->\n    \n    <!-- Sensor Popup -->\n    <div id="sensor-popup" class="hidden fixed inset-0 z-[60] flex items-center justify-center bg-black bg-opacity-60">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-md w-full mx-4 shadow-neon-purple">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="sensor.title">Select Sensors</h3>\n            <div id="sensor-popup-list" class="max-h-64 overflow-y-auto space-y-2 mb-4"></div>\n            <button id="sensor-popup-done-btn" onclick="closeSensorPopupForContext()"\n                    class="w-full bg-neon-purple text-white py-2 rounded-lg font-semibold hover:shadow-neon-purple transition-all">\n                Done\n            </button>\n        </div>\n    </div>\n    \n    <!-- Schedule Editor Popup -->\n    <div id="schedule-editor" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-60">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-lg w-full mx-4 shadow-neon-purple">\n            <div class="flex items-center justify-between mb-4">\n                <h3 class="text-lg font-bold text-white" data-i18n="editor.title">Edit Schedule</h3>\n                <span id="schedule-editor-cells" class="text-xs text-gray-500"></span>\n            </div>\n            \n            <!-- Mode Selection -->\n            <div class="mb-4">\n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.mode">Mode</label>\n                <div class="flex gap-2">\n                    <button id="sched-btn-auto" onclick="setScheduleMode(\'auto\')" \n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border" data-i18n="mode.auto">\n                        🌡️ Auto\n                    </button>\n                    <button id="sched-btn-manual" onclick="setScheduleMode(\'manual\')" \n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border" data-i18n="mode.manual">\n                        🎮 Manual\n                    </button>\n                    <button id="sched-btn-off" onclick="setScheduleMode(\'off\')" \n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border" data-i18n="schedule.legend_off">\n                        ⏻ Off\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Auto Mode Settings -->\n            <div id="sched-auto-settings" class="mb-4">\n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.target_temp">Target Temperature</label>\n                <div class="flex items-center gap-3 mb-3">\n                    <input type="number" id="sched-target-temp" value="31" min="20" max="60"\n                           class="w-20 bg-cyber-bg border border-cyber-accent rounded-lg px-3 py-2 text-white text-center font-mono\n                                  focus:border-neon-cyan focus:outline-none">\n                    <span class="text-gray-400">°C</span>\n                </div>\n                \n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.sensors">Sensors</label>\n                <div id="sched-sensor-tags" class="flex flex-wrap gap-2 mb-2"></div>\n                <button onclick="toggleScheduleSensorPopup()"\n                        class="text-xs bg-cyber-accent text-gray-400 px-3 py-1.5 rounded-lg \n                               hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple transition-all mb-3" data-i18n="editor.add_sensor">\n                    + Add Sensor\n                </button>\n                \n                <div id="sched-sensor-mode-section" class="hidden">\n                    <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.temp_mode">Temperature Mode</label>\n                    <div class="flex gap-2">\n                        <button id="sched-btn-sensor-max" onclick="setScheduleSensorMode(\'max\')" \n                                class="flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                            Max\n                        </button>\n                        <button id="sched-btn-sensor-min" onclick="setScheduleSensorMode(\'min\')" \n                                class="flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                            Min\n                        </button>\n                        <button id="sched-btn-sensor-avg" onclick="setScheduleSensorMode(\'avg\')" \n                                class="flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                            Average\n                        </button>\n                    </div>\n                </div>\n            </div>\n            \n            <!-- Manual Mode Settings -->\n            <div id="sched-manual-settings" class="hidden mb-4">\n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.fan_speed">Fan Speed</label>\n                <div class="flex items-center gap-3">\n                    <input type="range" id="sched-speed-slider" min="0" max="100" value="50"\n                           class="flex-1 h-2 bg-cyber-accent rounded-lg appearance-none cursor-pointer\n                                  accent-neon-purple [&::-webkit-slider-thumb]:appearance-none \n                                  [&::-webkit-slider-thumb]:w-5 [&::-webkit-slider-thumb]:h-5 \n                                  [&::-webkit-slider-thumb]:bg-neon-purple [&::-webkit-slider-thumb]:rounded-full\n                                  [&::-webkit-slider-thumb]:shadow-neon-purple [&::-webkit-slider-thumb]:cursor-pointer">\n                    <span id="sched-speed-value" class="text-sm font-mono text-neon-purple w-12 text-right">50%</span>\n                </div>\n            </div>\n            \n            <!-- Buttons -->\n            <div class="flex gap-2">\n                <button onclick="saveScheduleEdit()" \n                        class="flex-1 bg-neon-cyan bg-opacity-20 text-neon-cyan py-2.5 rounded-lg font-semibold\n                               hover:bg-opacity-40 transition-all" data-i18n="editor.apply">\n                    Apply\n                </button>\n                <button onclick="deleteScheduleEdit()" \n                        class="bg-red-900 bg-opacity-30 text-red-400 px-4 py-2.5 rounded-lg font-semibold\n                               hover:bg-opacity-50 transition-all" data-i18n="editor.delete">\n                    Delete\n                </button>\n                <button onclick="closeScheduleEditor()" \n                        class="bg-cyber-accent text-gray-400 px-4 py-2.5 rounded-lg font-semibold\n                               hover:text-white transition-all" data-i18n="editor.cancel">\n                    Cancel\n                </button>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Calibration Progress Modal -->\n    <div id="calibration-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-8 max-w-md w-full mx-4 text-center shadow-neon-cyan">\n            <div class="text-4xl mb-4 animate-spin-slow">⚙️</div>\n            <h3 class="text-xl font-bold text-white mb-2" data-i18n="calibration.title">Calibrating Fans</h3>\n            <p id="calibration-status" class="text-gray-400 mb-4" data-i18n="calibration.status">Starting...</p>\n            <div class="w-full bg-cyber-accent rounded-full h-2 mb-2">\n                <div id="calibration-progress-bar" class="bg-neon-cyan h-2 rounded-full transition-all duration-500" style="width: 0%"></div>\n            </div>\n            <p id="calibration-step" class="text-xs text-gray-500">Step 0/11</p>\n        </div>\n    </div>\n    \n    <!-- Settings Panel -->\n    <div id="settings-overlay" class="hidden fixed inset-0 z-[70] bg-black bg-opacity-50" onclick="toggleSettings()"></div>\n    <div id="settings-panel" class="hidden fixed top-0 right-0 h-full w-80 bg-cyber-card border-l border-cyber-accent z-[75] shadow-2xl overflow-y-auto">\n        <div class="p-5">\n            <div class="flex items-center justify-between mb-6">\n                <h2 class="text-lg font-bold text-white" data-i18n="settings.title">Settings</h2>\n                <button onclick="toggleSettings()" class="text-gray-400 hover:text-white transition-colors text-xl">&times;</button>\n            </div>\n            \n            <!-- Language -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.language">Language</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.language_hint">Select your preferred language</p>\n                <div class="flex gap-2">\n                    <button id="lang-btn-en" onclick="switchLanguage(\'en\')"\n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        English\n                    </button>\n                    <button id="lang-btn-ru" onclick="switchLanguage(\'ru\')"\n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        Русский\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Temperature Unit -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.temp_unit">Temperature Unit</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.temp_unit_hint">Choose Celsius or Fahrenheit</p>\n                <div class="flex gap-2">\n                    <button id="unit-btn-celsius" onclick="setTempUnit(\'celsius\')"\n                            class="flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        °C\n                    </button>\n                    <button id="unit-btn-fahrenheit" onclick="setTempUnit(\'fahrenheit\')"\n                            class="flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        °F\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Refresh Interval -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.refresh">Update Interval</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.refresh_hint">Reduce CPU usage by throttling updates</p>\n                <div class="flex gap-1">\n                    <button id="refresh-btn-0" onclick="setRefreshInterval(0)"\n                            class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                        <span data-i18n="settings.refresh_realtime">Realtime</span>\n                    </button>\n                    <button id="refresh-btn-1000" onclick="setRefreshInterval(1000)"\n                            class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                        1s\n                    </button>\n                    <button id="refresh-btn-5000" onclick="setRefreshInterval(5000)"\n                            class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                        5s\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Compact Mode -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.compact">Compact Dashboard</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.compact_hint">Smaller cards for small screens</p>\n                <button id="compact-toggle" onclick="toggleCompactMode()"\n                        class="w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-400 border-gray-700 hover:text-white">\n                    <span data-i18n="settings.off">Off</span>\n                </button>\n            </div>\n\n            <!-- Logging Level -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.logging">Logging Level</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.logging_hint">Control log verbosity. WARNING reduces log file size significantly.</p>\n                <div class="flex gap-1">\n                    <button id="log-btn-DEBUG" onclick="setLogLevel(\'DEBUG\')" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">DEBUG</button>\n                    <button id="log-btn-INFO" onclick="setLogLevel(\'INFO\')" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">INFO</button>\n                    <button id="log-btn-WARNING" onclick="setLogLevel(\'WARNING\')" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">WARN</button>\n                    <button id="log-btn-ERROR" onclick="setLogLevel(\'ERROR\')" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">ERROR</button>\n                </div>\n            </div>\n\n            <!-- Log Retention -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.log_retention">Log Retention</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.log_retention_hint">How long to keep log files before automatic cleanup.</p>\n                <div class="flex gap-1 flex-wrap">\n                    <button id="retention-btn-7" onclick="setLogRetention(7)" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px]">7d</button>\n                    <button id="retention-btn-14" onclick="setLogRetention(14)" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px]">14d</button>\n                    <button id="retention-btn-30" onclick="setLogRetention(30)" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px]">30d</button>\n                    <button id="retention-btn-60" onclick="setLogRetention(60)" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px]">60d</button>\n                    <button id="retention-btn-90" onclick="setLogRetention(90)" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px]">90d</button>\n                    <button id="retention-btn-180" onclick="setLogRetention(180)" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px]">6mo</button>\n                    <button id="retention-btn-365" onclick="setLogRetention(365)" class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px]">1yr</button>\n                </div>\n            </div>\n\n            <!-- System Update -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.update">System Update</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.update_hint">Check and apply updates from Git</p>\n                \n                <!-- Auto-check interval -->\n                <div class="flex gap-1 mb-3">\n                    <button id="autoupd-btn-off" onclick="setAutoUpdateInterval(0)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">Off</button>\n                    <button id="autoupd-btn-21600000" onclick="setAutoUpdateInterval(21600000)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">6h</button>\n                    <button id="autoupd-btn-43200000" onclick="setAutoUpdateInterval(43200000)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">12h</button>\n                    <button id="autoupd-btn-86400000" onclick="setAutoUpdateInterval(86400000)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">24h</button>\n                </div>\n                \n                <button id="update-check-btn" onclick="checkForUpdates()"\n                        class="w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-400 border-gray-700 hover:text-neon-cyan hover:text-white mb-2">\n                    <span data-i18n="settings.check_update">Check for Updates</span>\n                </button>\n                <div id="update-result" class="hidden text-xs mt-2 p-3 rounded-lg bg-cyber-accent border border-cyber-accent"></div>\n                <button id="update-apply-btn" onclick="openUpdateModal()" disabled class="hidden w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-500 border-gray-700 mt-2">\n                    <span data-i18n="settings.apply_update">Update & Restart</span>\n                </button>\n            </div>\n            \n            <!-- Version -->\n            <div class="mb-6 text-center">\n                <a id="version-link" href="https://github.com/Biowolfx/fancontrol-web" target="_blank" rel="noopener"\n                   class="text-xs text-gray-600 hover:text-neon-cyan transition-colors cursor-pointer">\n                    FanControl Web\n                </a>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Update Modal -->\n    <div id="update-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-cyber-accent rounded-2xl p-6 max-w-md w-full mx-4 shadow-neon-purple">\n            <h3 id="update-modal-title" class="text-lg font-bold text-white mb-4" data-i18n="settings.update_modal_title">System Update</h3>\n            \n            <div id="update-modal-steps" class="space-y-3 mb-6">\n                <!-- Steps populated by JS -->\n            </div>\n            \n            <div id="update-modal-progress" class="hidden mb-4">\n                <div class="w-full bg-cyber-accent rounded-full h-2">\n                    <div id="update-modal-bar" class="bg-neon-cyan h-2 rounded-full transition-all duration-500" style="width: 0%"></div>\n                </div>\n            </div>\n            \n            <div id="update-modal-result" class="hidden text-sm mb-4 p-3 rounded-lg"></div>\n\n            <div id="update-modal-agents" class="hidden mb-4 p-3 rounded-lg bg-cyber-accent border border-gray-700">\n                <div class="text-sm font-semibold text-gray-300 mb-2" data-i18n="settings.connected_agents">Connected Agents</div>\n                <div id="update-modal-agents-list" class="space-y-2 max-h-40 overflow-y-auto">\n                    <!-- Populated by JS -->\n                </div>\n                <p class="text-xs text-gray-500 mt-2" data-i18n="settings.update_agents_hint">Auto-update syncs with each agent\'s settings. Toggle applies immediately.</p>\n            </div>\n\n            <div class="flex gap-3">\n                <button id="update-modal-close" onclick="closeUpdateModal()" class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold bg-cyber-accent text-gray-400 border border-gray-700 hover:text-white transition-all">\n                    <span data-i18n="common.cancel">Cancel</span>\n                </button>\n                <button id="update-modal-apply" onclick="startUpdate()" class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold bg-neon-green bg-opacity-20 text-neon-green border border-neon-green border-opacity-30 hover:bg-opacity-40 transition-all">\n                    <span data-i18n="settings.apply_update">Update & Restart</span>\n                </button>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Conflict Modal -->\n    <div id="conflict-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-yellow-500/30 rounded-xl p-6 max-w-lg w-full mx-4 shadow-2xl">\n            <h3 class="text-lg font-bold text-white mb-2">\n                ⚠️ <span id="conflict-node-name"></span> — <span data-i18n="conflict.title">Config Conflict</span>\n            </h3>\n            <p class="text-gray-400 text-sm mb-4" data-i18n="conflict.desc">Agent config differs from server config.</p>\n            \n            <div class="grid grid-cols-2 gap-4 mb-6">\n                <div>\n                    <h4 class="text-white text-sm font-semibold mb-2" data-i18n="conflict.server_config">Server Config</h4>\n                    <div id="conflict-server-config" class="bg-cyber-bg rounded p-3 text-sm"></div>\n                </div>\n                <div>\n                    <h4 class="text-white text-sm font-semibold mb-2" data-i18n="conflict.agent_config">Agent Config</h4>\n                    <div id="conflict-agent-config" class="bg-cyber-bg rounded p-3 text-sm"></div>\n                </div>\n            </div>\n            \n            <div class="flex gap-3">\n                <button onclick="applyServerConfig()"\n                    class="flex-1 py-2 px-4 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded-lg font-semibold transition-all hover:bg-opacity-40">\n                    <span data-i18n="conflict.apply_server">Apply Server Config</span>\n                </button>\n                <button onclick="keepAgentConfig()"\n                    class="flex-1 py-2 px-4 bg-cyber-accent text-gray-300 rounded-lg font-semibold transition-all hover:bg-gray-700">\n                    <span data-i18n="conflict.keep_agent">Keep Agent Config</span>\n                </button>\n                <button onclick="hideConflictModal()"\n                    class="py-2 px-4 bg-cyber-accent hover:bg-gray-700 rounded-lg text-gray-400 transition-all">\n                    <span data-i18n="common.cancel">Cancel</span>\n                </button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Node Settings Modal -->\n    <div id="node-settings-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="node.settings">Node Settings</h3>\n            <input type="hidden" id="node-settings-id">\n            <div class="space-y-3">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="node.name">Name</label>\n                    <input id="node-settings-name" type="text"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none">\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="node.ip">IP Address</label>\n                    <input id="node-settings-ip" type="text"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none"\n                           placeholder="192.168.1.100">\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="node.port">Port</label>\n                    <input id="node-settings-port" type="number" value="5059"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none">\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="nodes.agent_version">Version</label>\n                    <div id="node-settings-version" class="text-sm text-gray-500">—</div>\n                </div>\n                <div class="flex items-center gap-2">\n                    <input type="checkbox" id="node-settings-auto-update" class="accent-neon-cyan">\n                    <label for="node-settings-auto-update" class="text-sm text-gray-300" data-i18n="nodes.auto_update">Auto-update</label>\n                </div>\n            </div>\n            <div class="flex gap-3 mt-5">\n                <button onclick="saveNodeSettings()"\n                    class="flex-1 py-2 px-4 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded-lg font-semibold transition-all hover:bg-opacity-40">\n                    <span data-i18n="node.save">Save</span>\n                </button>\n                <button onclick="hideNodeSettings()"\n                    class="py-2 px-4 bg-cyber-accent hover:bg-gray-700 rounded-lg text-gray-400 transition-all">\n                    <span data-i18n="node.cancel">Cancel</span>\n                </button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Server Name Edit Modal -->\n    <div id="server-name-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="node.server_name">Server Name</h3>\n            <div class="space-y-3">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="node.name">Name</label>\n                    <input id="server-name-input" type="text" maxlength="64"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none"\n                           placeholder="FanControl Server"\n                           onkeydown="if(event.key===\'Enter\')saveServerName()">\n                </div>\n            </div>\n            <div class="flex gap-3 mt-5">\n                <button onclick="saveServerName()"\n                    class="flex-1 py-2 px-4 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded-lg font-semibold transition-all hover:bg-opacity-40">\n                    <span data-i18n="node.save">Save</span>\n                </button>\n                <button onclick="hideServerNameModal()"\n                    class="py-2 px-4 bg-cyber-accent hover:bg-gray-700 rounded-lg text-gray-400 transition-all">\n                    <span data-i18n="node.cancel">Cancel</span>\n                </button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Manual Mode Warning -->\n    <div id="manual-mode-warning" class="hidden fixed bottom-4 right-4 z-[80] max-w-sm">\n        <div class="bg-yellow-900/30 border border-yellow-500/30 rounded-xl p-4">\n            <div class="flex items-center gap-2 mb-2">\n                <span class="text-yellow-400">⚠️</span>\n                <span class="text-white font-semibold">\n                    <span id="manual-mode-node-name"></span> — <span data-i18n="conflict.manual_mode">Manual Mode</span>\n                </span>\n            </div>\n            <p class="text-gray-400 text-sm mb-3" data-i18n="conflict.manual_warning">Agent is controlling fans locally.</p>\n            <div class="flex gap-2">\n                <button id="manual-mode-switch-btn"\n                    class="px-3 py-1 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded text-sm transition-all hover:bg-opacity-40">\n                    <span data-i18n="conflict.switch_to_server">Switch to Server Control</span>\n                </button>\n                <button onclick="hideManualModeWarning()"\n                    class="px-3 py-1 bg-cyber-accent rounded text-gray-400 text-sm transition-all hover:bg-gray-700">\n                    <span data-i18n="common.done">Dismiss</span>\n                </button>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Server Unavailable Banner -->\n    <div id="server-unavailable-banner" class="hidden fixed top-4 left-1/2 -translate-x-1/2 z-50">\n        <div class="bg-red-900/30 border border-red-500/30 rounded-xl px-6 py-3 flex items-center gap-3">\n            <span class="text-red-400">⚠️</span>\n            <span class="text-white" data-i18n="node.standalone_banner">Server unavailable — running in standalone mode</span>\n        </div>\n    </div>\n\n    <!-- Card Picker Modal -->\n    <div id="card-picker-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-md w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="dashboard.add_card">Add Card</h3>\n            <div class="space-y-4">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.type">Тип</label>\n                    <select id="picker-type" class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"\n                            onchange="updatePickerElements()">\n                        <option value="fan" data-i18n="picker.fan">🌀 Вентилятор</option>\n                        <option value="temperature" data-i18n="picker.temperature">🌡 Температура</option>\n                        <option value="disk" data-i18n="picker.disk">💾 Диск</option>\n                        <option value="system" data-i18n="picker.system">📊 Система</option>\n                    </select>\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.source">Источник</label>\n                    <select id="picker-source" class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"\n                            onchange="updatePickerElements()">\n                        <option value="local" data-i18n="picker.my_server">Мой сервер (локально)</option>\n                    </select>\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.element">Элемент</label>\n                    <div id="picker-elements" class="max-h-48 overflow-y-auto space-y-1 bg-cyber-bg border border-cyber-accent rounded p-2"></div>\n                </div>\n            </div>\n            <div class="flex gap-2 mt-6">\n                <button onclick="hideCardPicker()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm" data-i18n="common.cancel">Отмена</button>\n                <button onclick="addSelectedCards()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm" data-i18n="picker.add">Добавить</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Card Edit Modal -->\n    <div id="card-edit-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-md w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="picker.edit_card">Редактировать карточку</h3>\n            <div class="space-y-4">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.title">Заголовок</label>\n                    <input id="card-edit-label" type="text" data-i18n-placeholder="picker.title_placeholder" placeholder="Название карточки"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"\n                           onkeydown="if(event.key===\'Enter\')saveCardEdit()">\n                </div>\n            </div>\n            <div class="flex gap-2 mt-6">\n                <button onclick="hideCardEdit()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm" data-i18n="common.cancel">Отмена</button>\n                <button onclick="saveCardEdit()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm" data-i18n="common.save">Сохранить</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Card Configure Modal -->\n    <div id="card-config-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="picker.card_display">Отображение карточки</h3>\n            <div id="card-config-options" class="space-y-2"></div>\n            <div class="flex gap-2 mt-6">\n                <button onclick="hideCardConfig()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm" data-i18n="picker.close">Закрыть</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- SMART Detail Modal -->\n    <div id="smart-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col">\n            <div class="flex items-center justify-between mb-4">\n                <h3 class="text-lg font-bold text-white" id="smart-modal-title" data-i18n="smart.title">SMART Data</h3>\n                <div class="flex items-center gap-2">\n                    <button onclick="refreshSmartData()" class="text-gray-400 hover:text-neon-cyan text-sm transition-colors" title="Обновить">🔄</button>\n                    <button onclick="hideSmartModal()" class="text-gray-400 hover:text-white text-lg">&times;</button>\n                </div>\n            </div>\n            <div id="smart-device-info" class="text-xs text-gray-400 mb-3"></div>\n            <div id="smart-attributes-container" class="flex-1 overflow-y-auto space-y-1"></div>\n            <div class="flex gap-2 mt-4 pt-4 border-t border-gray-700">\n                <button onclick="saveSmartSelection()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm">Сохранить выбор</button>\n                <button onclick="hideSmartModal()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm">Закрыть</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Group Creator Modal -->\n    <div id="group-creator-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="dashboard.add_group">Add Group</h3>\n            <input id="group-name-input" type="text" data-i18n-placeholder="group.name_placeholder" placeholder="Group name (e.g., CPU Cooling)"\n                   class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm mb-4"\n                   onkeydown="if(event.key===\'Enter\')createGroup()">\n            <div class="flex gap-2">\n                <button onclick="hideGroupCreator()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm" data-i18n="node.cancel">Cancel</button>\n                <button onclick="createGroup()" class="flex-1 py-2 rounded-lg bg-neon-purple text-white font-semibold hover:bg-purple-400 transition-all text-sm" data-i18n="group.create">Create</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Debug Panel -->\n    <div id="debug-panel" class="hidden fixed bottom-4 right-4 z-50 bg-black/95 border border-cyber-accent rounded-xl p-4 max-w-md max-h-96 overflow-y-auto font-mono text-xs text-gray-300 shadow-2xl">\n        <div class="flex justify-between items-center mb-3">\n            <span class="text-neon-cyan font-bold">DEBUG PANEL</span>\n            <button onclick="toggleDebugPanel()" class="text-gray-500 hover:text-white">✕</button>\n        </div>\n        <div id="debug-content"></div>\n    </div>\n    <button onclick="toggleDebugPanel()" class="fixed bottom-4 right-4 z-40 w-10 h-10 rounded-full bg-cyber-accent/30 border border-cyber-accent text-lg hover:bg-cyber-accent/50 transition-all" title="Debug">🐛</button>\n\n    <div id="toast-container" class="toast-container"></div>\n    <script type="module" src="/js/main.js?v={{ config_version }}"></script>\n</body>\n</html>'

JS_MODULES = {
    'store.js': '/**\n * FanControl Web — Centralized State Store\n * Replaces 65+ scattered global variables with a single organized object.\n */\n\n// ============================================================================\n// CORE APPLICATION STATE\n// ============================================================================\n\nexport const store = {\n    // Server state (updated via Socket.IO \'update\' event)\n    state: {},\n\n    // UI navigation\n    currentFanId: null,\n    currentView: \'dashboard\',\n    selectedNodeId: null,\n    wasOnMainScreen: false,\n    currentRemoteNodeId: null,\n\n    // Multi-node\n    nodesData: [],\n\n    // Connection\n    serverAvailable: true,\n\n    // Chart\n    chart: null,\n    lastChartUpdate: 0,\n\n    // Sensors & fans\n    allSensors: [],\n    fanConfigs: {},\n\n    // Wizard\n    wizardStep: \'intro\',\n    wizardHardwareData: null,\n\n    // PWM slider\n    isDragging: false,\n\n    // UI refresh throttle\n    lastUIUpdate: 0,\n};\n\n// ============================================================================\n// CONSTANTS\n// ============================================================================\n\nexport const CHART_UPDATE_INTERVAL = 60000;\nexport const RELOAD_DELAY = 10000;\nexport const SCHEDULE_CELL_SIZE = 18;\nexport const SPARKLINE_MAX = 20;\n\nexport const BTN_ACTIVE = \'bg-neon-cyan bg-opacity-20 text-neon-cyan border-neon-cyan border-opacity-30\';\nexport const BTN_INACTIVE = \'bg-cyber-accent text-gray-400 border-gray-700 hover:text-white\';\nexport const BTN_MANUAL_ACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-purple\';\nexport const BTN_MANUAL_INACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple hover:border-neon-purple\';\nexport const BTN_AUTO_ACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-cyan bg-opacity-20 text-neon-cyan border border-neon-cyan border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-cyan\';\nexport const BTN_AUTO_INACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan\';\n\n// ============================================================================\n// PERSISTENT SETTINGS (localStorage)\n// ============================================================================\n\nexport const settingsDefaults = {\n    tempUnit: \'celsius\',\n    refreshInterval: 0,\n    compactMode: false,\n    autoUpdateCheck: 21600000,\n};\n\nexport const settings = {\n    _cache: null,\n    _cacheTime: 0,\n    CACHE_TTL: 1000,\n};\n\n// ============================================================================\n// I18N\n// ============================================================================\n\nexport const i18n = {\n    currentLang: localStorage.getItem(\'fancontrol_lang\') || \'en\',\n    translations: {},\n};\n\n// ============================================================================\n// SCHEDULE\n// ============================================================================\n\nexport const schedule = {\n    data: {},\n    selection: [],\n    isDragging: false,\n    dragStartCell: null,\n    editingCells: [],\n    editorSensors: [],\n    expandedRuleGroups: new Set(),\n};\n\n// ============================================================================\n// DASHBOARD CARDS\n// ============================================================================\n\nexport const dashboard = {\n    cards: null,\n    groups: null,\n    hiddenSensors: null,\n    loaded: false,\n    saveTimer: null,\n    liveTimer: null,\n    sparklineHistory: {},\n};\n\n// ============================================================================\n// CARD DRAG & DROP\n// ============================================================================\n\nexport const cardDrag = {\n    occurred: false,\n    dropTarget: null,\n    mouseDown: null,\n    dragClone: null,\n    gridCache: null,\n    dropPreview: null,\n};\n\n// ============================================================================\n// CARD RESIZE\n// ============================================================================\n\nexport const cardResize = {\n    resizing: null,\n    startX: 0,\n    startY: 0,\n    startW: 0,\n    startH: 0,\n    minRowSpan: 1,\n};\n\n// ============================================================================\n// CARD EDIT / CONFIG\n// ============================================================================\n\nexport const cardEdit = {\n    editingCardId: null,\n    configuringCardId: null,\n};\n\n// ============================================================================\n// SMART MODAL\n// ============================================================================\n\nexport const smart = {\n    modalCardId: null,\n    modalDiskId: null,\n    modalSource: \'local\',\n    attributes: [],\n    attrType: \'sata\',\n    cache: {},\n};\n\n// ============================================================================\n// GROUP RESIZE & DRAG\n// ============================================================================\n\nexport const groupDrag = {\n    resizingGroupId: null,\n    resizeStartY: 0,\n    resizeStartH: 0,\n    draggedGroup: null,\n    dropTarget: null,\n};\n\n// ============================================================================\n// SYSTEM TIMER\n// ============================================================================\n\nexport const timers = {\n    system: null,\n    autoUpdate: null,\n};\n\n// ============================================================================\n// DSM SCHEME EDITOR\n// ============================================================================\n\nexport const dsm = {\n    schemes: [],\n    activeScheme: null,\n};\n\n// ============================================================================\n// LOGGING\n// ============================================================================\n\nexport const logging = {\n    level: \'INFO\',\n    retention: 30,\n};\n\n// ============================================================================\n// UPDATE SYSTEM\n// ============================================================================\n\nexport const update = {\n    checked: false,\n    agentStates: {},\n    resolve: null,\n};\n\n// ============================================================================\n// CONFIG CONFLICT\n// ============================================================================\n\nexport const conflict = {\n    data: null,\n};\n\n// ============================================================================\n// DEBUG PANEL\n// ============================================================================\n\nexport const debug = {\n    open: false,\n};\n\n// ============================================================================\n// SPARKLINE (const-like, but mutable object)\n// ============================================================================\n\nexport const sparklineHistory = {};\n',
    'utils.js': '/**\n * FanControl Web — Pure utility functions\n * No state dependencies, no side effects (except DOM helpers).\n */\n\nimport { settings, settingsDefaults } from \'./store.js\';\nimport { t } from \'./i18n.js\';\n\nexport function escapeHtml(str) {\n    if (!str) return \'\';\n    return String(str).replace(/[&<>"\']/g, c => ({\n        \'&\': \'&amp;\', \'<\': \'&lt;\', \'>\': \'&gt;\', \'"\': \'&quot;\', "\'": \'&#39;\'\n    }[c]));\n}\n\nexport function fanIcon(fan, size = \'xs\') {\n    const sizeClass = size === \'xs\' ? \'w-3 h-3\' : \'w-4 h-4\';\n    const rpm = fan.rpm || 0;\n    const isDsm = fan.control_method === \'dsm_scemd\';\n    if (isDsm) {\n        return `<svg class="${sizeClass} inline-block flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg>`;\n    }\n    const healthStatus = fan.health?.status || \'healthy\';\n    let color;\n    if (healthStatus === \'stopped\') color = \'#ef4444\';\n    else if (healthStatus === \'slowing\' || healthStatus === \'needs_calibration\') color = \'#facc15\';\n    else color = rpm > 0 ? \'#22d3ee\' : \'#4b5563\';\n    const dur = rpm > 0 ? Math.max(0.3, 3 - rpm / 500) : 0;\n    const anim = rpm > 0 ? `style="animation: fan-spin ${dur}s linear infinite"` : \'\';\n    return `<svg class="${sizeClass} inline-block flex-shrink-0" viewBox="0 0 100 100" ${anim}><g fill="${color}" opacity="0.9"><path d="M50 50 Q30 20 50 5 Q70 20 50 50"/><path d="M50 50 Q80 30 95 50 Q80 70 50 50"/><path d="M50 50 Q70 80 50 95 Q30 80 50 50"/><path d="M50 50 Q20 70 5 50 Q20 30 50 50"/></g><circle cx="50" cy="50" r="6" fill="${color}" opacity="0.6"/></svg>`;\n}\n\nexport function show(el) { if (el) el.classList.remove(\'hidden\'); }\nexport function hide(el) { if (el) el.classList.add(\'hidden\'); }\nexport function toggle(el, visible) { if (el) el.classList.toggle(\'hidden\', !visible); }\n\nexport function formatTemp(celsius) {\n    if (celsius == null) return \'--\';\n    const s = getSettings();\n    if (s.tempUnit === \'fahrenheit\') {\n        return Math.round(celsius * 9 / 5 + 32) + \'°F\';\n    }\n    return celsius + \'°C\';\n}\n\nexport function getTempUnitSymbol() {\n    return getSettings().tempUnit === \'fahrenheit\' ? \'°F\' : \'°C\';\n}\n\nexport function formatBytes(bytes, unit) {\n    if (isNaN(bytes) || bytes === 0) return \'0\';\n    const units = { \'kb\': 1024, \'mb\': 1024*1024, \'gb\': 1024*1024*1024, \'tb\': 1024*1024*1024*1024 };\n    const divisor = units[unit] || 1;\n    const result = bytes / divisor;\n    if (result >= 1000) return result.toFixed(0);\n    if (result >= 100) return result.toFixed(1);\n    return result.toFixed(2);\n}\n\nexport function getUnitLabel(unit) {\n    const labels = { \'bytes\': t(\'smart.unit.bytes\', \'B\'), \'kb\': t(\'smart.unit.kb\', \'KB\'), \'mb\': t(\'smart.unit.mb\', \'MB\'), \'gb\': t(\'smart.unit.gb\', \'GB\'), \'tb\': t(\'smart.unit.tb\', \'TB\') };\n    return labels[unit] || \'\';\n}\n\nexport function getTempColorClass(temp) {\n    if (temp <= 0) return \'text-gray-500\';\n    if (temp <= 35) return \'text-neon-cyan\';\n    if (temp <= 45) return \'text-neon-orange\';\n    return \'text-neon-red\';\n}\n\nexport function getSettings() {\n    const now = Date.now();\n    if (settings._cache && (now - settings._cacheTime) < settings.CACHE_TTL) {\n        return settings._cache;\n    }\n    try {\n        const raw = localStorage.getItem(\'fancontrol_settings\');\n        settings._cache = raw ? { ...settingsDefaults, ...JSON.parse(raw) } : { ...settingsDefaults };\n    } catch { settings._cache = { ...settingsDefaults }; }\n    settings._cacheTime = now;\n    return settings._cache;\n}\n\nexport function saveSettings(partial) {\n    const s = getSettings();\n    Object.assign(s, partial);\n    localStorage.setItem(\'fancontrol_settings\', JSON.stringify(s));\n    settings._cache = s;\n    settings._cacheTime = Date.now();\n    return s;\n}\n\nexport function showToast(message, type = \'info\', actions = []) {\n    const container = document.getElementById(\'toast-container\');\n    if (!container) return;\n\n    const toast = document.createElement(\'div\');\n    toast.className = `toast toast-${type}`;\n\n    let html = `<span>${escapeHtml(message)}</span>`;\n    actions.forEach(action => {\n        html += `<button class="toast-btn ${action.secondary ? \'toast-btn-secondary\' : \'\'}" onclick="${action.onclick}">${escapeHtml(action.label)}</button>`;\n    });\n\n    toast.innerHTML = html;\n    container.appendChild(toast);\n\n    setTimeout(() => {\n        toast.style.opacity = \'0\';\n        toast.style.transform = \'translateX(100px)\';\n        setTimeout(() => toast.remove(), 300);\n    }, 8000);\n}\n',
    'i18n.js': '/**\n * FanControl Web — i18n system\n */\n\nimport { i18n, store } from \'./store.js\';\n\nexport async function loadLang(code) {\n    try {\n        const resp = await fetch(`/api/lang/${code}`);\n        if (resp.ok) {\n            i18n.translations = await resp.json();\n            i18n.currentLang = code;\n            localStorage.setItem(\'fancontrol_lang\', code);\n            applyTranslations();\n            return true;\n        }\n    } catch (e) {\n        console.error(\'[i18n] Failed to load lang:\', code, e);\n    }\n    return false;\n}\n\nexport function t(key, fallback) {\n    return i18n.translations[key] || fallback || key;\n}\n\nexport function applyTranslations() {\n    document.querySelectorAll(\'[data-i18n]\').forEach(el => {\n        const key = el.getAttribute(\'data-i18n\');\n        if (key && i18n.translations[key]) {\n            el.textContent = i18n.translations[key];\n        }\n    });\n    document.querySelectorAll(\'[data-i18n-title]\').forEach(el => {\n        const key = el.getAttribute(\'data-i18n-title\');\n        if (key && i18n.translations[key]) {\n            el.title = i18n.translations[key];\n        }\n    });\n    document.querySelectorAll(\'[data-i18n-placeholder]\').forEach(el => {\n        const key = el.getAttribute(\'data-i18n-placeholder\');\n        if (key && i18n.translations[key]) {\n            el.placeholder = i18n.translations[key];\n        }\n    });\n    const ver = store.state?.config_version;\n    if (i18n.translations[\'app.title\'] && ver) {\n        document.title = `${i18n.translations[\'app.title\']} v${ver}`;\n    }\n}\n',
    'render-helpers.js': '/**\n * FanControl Web — Shared rendering helpers\n * Deduplicates HTML template patterns used across main.js.\n */\n\nimport { t } from \'./i18n.js\';\nimport { escapeHtml, formatTemp } from \'./utils.js\';\nimport { BTN_MANUAL_ACTIVE, BTN_MANUAL_INACTIVE, BTN_AUTO_ACTIVE, BTN_AUTO_INACTIVE } from \'./store.js\';\n\n/**\n * Render a health status icon for a fan.\n * Used in renderLocalServerTree() and renderRemoteNodeTree().\n * @param {Object} fan - fan object with health.status\n * @returns {string} HTML string for the health icon\n */\nexport function healthIcon(fan) {\n    const fanHealth = fan.health?.status || \'healthy\';\n    if (fanHealth === \'stopped\') return \'<span class="text-red-400 text-[10px] ml-1 alert-pulse" title="\' + t(\'fan.health.stopped\', \'Fan stopped\') + \'">⛔</span>\';\n    if (fanHealth === \'slowing\') return \'<span class="text-yellow-400 text-[10px] ml-1 alert-pulse" title="\' + t(\'fan.health.slowing\', \'Fan slowing — bearing wear\') + \'">⚠</span>\';\n    if (fanHealth === \'needs_calibration\') return \'<span class="text-yellow-400 text-[10px] ml-1 alert-pulse" title="\' + t(\'fan.health.needs_calibration\', \'Calibration required\') + \'">⚠</span>\';\n    return \'\';\n}\n\n/**\n * Build the HTML for a sensor checkbox list (used in sensor popups).\n * @param {Array} sensors - all sensors (from store.allSensors)\n * @param {Array} checkedIds - IDs of currently checked sensors\n * @returns {string} HTML string\n */\nexport function buildSensorCheckboxList(sensors, checkedIds) {\n    const groups = {};\n    sensors.forEach(s => {\n        if (!groups[s.group]) groups[s.group] = [];\n        groups[s.group].push(s);\n    });\n    \n    let html = \'\';\n    for (const [group, slist] of Object.entries(groups)) {\n        html += `<div class="text-xs font-semibold text-gray-500 uppercase mb-2">${t(group, group)}</div>`;\n        slist.forEach(s => {\n            const checked = checkedIds.includes(s.id);\n            html += `\n                <label class="flex items-center gap-2 py-1.5 cursor-pointer hover:bg-cyber-accent rounded px-2">\n                    <input type="checkbox" value="${escapeHtml(s.id)}" ${checked ? \'checked\' : \'\'} \n                           class="accent-neon-purple">\n                    <span class="text-sm text-gray-300">${escapeHtml(s.label)}</span>\n                    <span class="text-xs text-gray-500 ml-auto">\n                        ${s.standby ? t(\'sensor.sleep\', \'Sleep\') : formatTemp(s.temp)}\n                    </span>\n                </label>\n            `;\n        });\n    }\n    return html;\n}\n\n/**\n * Set manual/auto button styles based on current mode.\n * Used in updateInspector() and setFanMode().\n * @param {string} mode - \'manual\' or \'auto\'\n */\nexport function setModeButtonStyles(mode) {\n    const btnManual = document.getElementById(\'btn-mode-manual\');\n    const btnAuto = document.getElementById(\'btn-mode-auto\');\n    if (btnManual && btnAuto) {\n        if (mode === \'manual\') {\n            btnManual.className = BTN_MANUAL_ACTIVE;\n            btnAuto.className = BTN_AUTO_INACTIVE;\n        } else {\n            btnManual.className = BTN_MANUAL_INACTIVE;\n            btnAuto.className = BTN_AUTO_ACTIVE;\n        }\n    }\n}\n',
    'socket-handlers.js': '/**\n * FanControl Web — Centralized Socket.IO event handlers\n * All socket.on() registrations in one place.\n */\n\nimport { store, dashboard, update, conflict } from \'./store.js\';\nimport { t } from \'./i18n.js\';\nimport { getSettings, showToast } from \'./utils.js\';\n\n// These functions are defined in main.js and will be passed in\n// via the registerSocketHandlers() call at the bottom.\nlet _fns = {};\n\n/**\n * Register all socket event handlers.\n * @param {SocketIOClient.Socket} socket - the socket.io instance\n * @param {Object} fns - object containing all needed functions from main.js\n */\nexport function registerSocketHandlers(socket, fns) {\n    _fns = fns;\n\n    socket.on(\'disconnect\', () => {\n        store.serverAvailable = false;\n        _fns.showServerUnavailable();\n    });\n\n    socket.on(\'connect\', () => {\n        store.serverAvailable = true;\n        _fns.hideServerUnavailable();\n    });\n\n    socket.on(\'update\', (data) => {\n        // Merge partial updates into store.state (don\'t replace)\n        if (data != null && typeof data === \'object\') Object.assign(store.state, data);\n        // Sync node data from server state\n        if (data.nodes) {\n            const nodeEntries = Object.entries(data.nodes);\n            for (const [nid, ndata] of nodeEntries) {\n                const idx = store.nodesData.findIndex(n => n.node_id === nid);\n                if (idx >= 0) {\n                    Object.assign(store.nodesData[idx], ndata);\n                } else {\n                    store.nodesData.push(ndata);\n                }\n            }\n            _fns.buildServerTree();\n        }\n        if (data.test_progress && data.testing) {\n            _fns.updateCalibrationModal(data.test_progress);\n        }\n        const interval = getSettings().refreshInterval;\n        if (interval === 0) {\n            _fns.updateUI(data);\n        } else {\n            const now = Date.now();\n            if (now - store.lastUIUpdate >= interval) {\n                store.lastUIUpdate = now;\n                _fns.updateUI(data);\n            }\n        }\n        // Show update button in sidebar for agent mode\n        const agentUpdateSection = document.getElementById(\'agent-update-section\');\n        if (agentUpdateSection) {\n            agentUpdateSection.classList.toggle(\'hidden\', !data.agent_mode);\n        }\n        // Show "Update Agents" button if any agent has outdated version (server mode only)\n        const updateAgentsOutdated = document.getElementById(\'update-agents-outdated-section\');\n        if (updateAgentsOutdated && !data.agent_mode) {\n            const serverVer = data.config_version || \'\';\n            const outdatedCount = Object.values(data.nodes || {})\n                .filter(n => n.status === \'online\' && n.agent_version && n.agent_version !== serverVer).length;\n            updateAgentsOutdated.classList.toggle(\'hidden\', outdatedCount === 0);\n            const countEl = document.getElementById(\'outdated-agents-count\');\n            if (countEl) {\n                countEl.textContent = outdatedCount > 0 ? outdatedCount : \'\';\n            }\n        }\n        // Hide "Add Node" section in agent mode (no server features)\n        const addNodeSection = document.getElementById(\'add-node-section\');\n        if (addNodeSection) {\n            addNodeSection.classList.toggle(\'hidden\', !!data.agent_mode);\n        }\n        // Show agent token in sidebar only in agent mode\n        const agentTokenSection = document.getElementById(\'agent-token-section\');\n        const agentTokenBanner = document.getElementById(\'agent-token-banner\');\n        const hasToken = data.api_token && data.api_token.length > 0;\n        if (agentTokenSection) {\n            agentTokenSection.classList.toggle(\'hidden\', !data.agent_mode || !hasToken);\n            if (hasToken) document.getElementById(\'agent-token-value\').textContent = data.api_token;\n        }\n        // Hide the big banner — token is already in sidebar\n        if (agentTokenBanner) {\n            agentTokenBanner.classList.add(\'hidden\');\n        }\n        // DSM scheme view is accessed by clicking DSM fans in tree — no nav button needed\n    });\n\n    socket.on(\'hardware_discovered\', (data) => {\n        console.log(\'[FanControl] Hardware discovered:\', data);\n        if (store.wizardStep === \'intro\' || store.wizardStep === \'scanning\') {\n            _fns.renderDiscoveredHardware(data);\n            store.wizardStep = \'results\';\n        }\n    });\n\n    socket.on(\'test_progress\', (progress) => {\n        console.log(\'[FanControl] Calibration progress:\', progress);\n        _fns.updateCalibrationModal(progress);\n    });\n\n    socket.on(\'hidden_sensors\', (data) => {\n        dashboard.hiddenSensors = data.hiddenSensors || [];\n        _fns.buildServerTree();\n    });\n\n    socket.on(\'test_complete\', (result) => {\n        console.log(\'[FanControl] Calibration complete:\', result);\n        _fns.hideCalibrationModal();\n        if (result.success) {\n            store.wizardStep = \'done\';\n            store.state = { ...store.state, initialized: true, tested: true };\n            _fns.showMainScreen();\n        }\n    });\n\n    // Agent update listeners (registered once)\n    socket.on(\'agent:update_progress\', (data) => {\n        const { node_id, status, message, version } = data;\n        update.agentStates[node_id] = { status, message, version };\n        _fns.renderUpdateAgentProgress();\n        _fns.checkAgentsDone();\n    });\n    socket.on(\'agent:logs\', (data) => {\n        _fns.renderAgentLogsModal(data.node_id, data.lines);\n    });\n\n    // Node events\n    socket.on(\'node:update\', (data) => {\n        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);\n        if (idx >= 0) {\n            store.nodesData[idx].status = data.status;\n            store.nodesData[idx].name = data.name || store.nodesData[idx].name;\n            if (data.ip) store.nodesData[idx].ip = data.ip;\n            if (data.control_mode) store.nodesData[idx].control_mode = data.control_mode;\n        }\n        _fns.buildServerTree();\n        _fns.renderNodesOverview();\n    });\n\n    socket.on(\'node:telemetry\', (data) => {\n        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);\n        if (idx >= 0) {\n            store.nodesData[idx].telemetry = data.telemetry;\n        } else {\n            // Node not yet in store.nodesData — fetch fresh list\n            _fns.loadNodes();\n            return;\n        }\n        _fns.buildServerTree();\n        _fns.renderNodesOverview();\n        if (store.selectedNodeId === data.node_id && store.currentView === \'node-detail\') {\n            _fns.loadNodeDetail(data.node_id);\n        }\n    });\n\n    socket.on(\'node:conflict\', (data) => {\n        console.warn(\'[FanControl] Node conflict:\', data);\n        conflict.data = data;\n        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);\n        if (idx >= 0) {\n            store.nodesData[idx].control_mode = \'manual\';\n        }\n        _fns.buildServerTree();\n        _fns.showConflictModal(data);\n    });\n\n    socket.on(\'node:mode_changed\', (data) => {\n        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);\n        if (idx >= 0) {\n            store.nodesData[idx].control_mode = data.mode;\n        }\n        _fns.buildServerTree();\n        _fns.renderNodesOverview();\n        if (data.mode === \'manual\') {\n            _fns.showManualModeWarning(data.node_id);\n        }\n    });\n\n    socket.on(\'node:discovered\', (data) => {\n        if (data.already_connected) {\n            // Agent auto-registered via WebSocket — already connected, just notify\n            showToast(t(\'toast.agent_connected\', \'Agent connected\') + \': \' + data.name + \' (\' + data.ip + \')\', \'success\');\n            _fns.loadNodes();\n        } else {\n            // SSDP-discovered agent — check if dismissed\n            const dismissed = JSON.parse(localStorage.getItem(\'fc_dismissed_agents\') || \'[]\');\n            if (dismissed.includes(data.node_id)) return;\n            const msg = t(\'toast.new_agent\', \'New agent: \') + data.name + \' (\' + data.ip + \')\';\n            showToast(msg, \'warning\', [\n                { label: t(\'toast.add\', \'Add\'), onclick: `acceptDiscoveredAgent(\'${data.node_id}\')` },\n                { label: t(\'toast.dismiss\', \'Don\\\'t remind\'), onclick: `dismissAgentForever(\'${data.node_id}\')`, secondary: true },\n            ]);\n        }\n    });\n\n    socket.on(\'server:name_changed\', (data) => {\n        if (data.name) {\n            store.state.server_name = data.name;\n            _fns.buildServerTree();\n        }\n    });\n}\n',
    'charts.js': 'import { store, CHART_UPDATE_INTERVAL } from \'./store.js\';\nimport { t } from \'./i18n.js\';\nimport { getTempUnitSymbol } from \'./utils.js\';\n\nexport function updateChart() {\n    const now = Date.now();\n    if (now - store.lastChartUpdate < CHART_UPDATE_INTERVAL) return;\n    if (typeof ApexCharts === \'undefined\') return;\n\n    const chartContainer = document.getElementById(\'temp-chart\');\n    if (!chartContainer || chartContainer.offsetParent === null) return;\n\n    store.lastChartUpdate = now;\n\n    fetch(\'/api/history?hours=24\')\n        .then(r => r.json())\n        .then(data => {\n            if (!data || !data.has_data || !data.timestamps || !data.timestamps.length) return;\n\n            const series = [\n                {\n                    name: t(\'chart.max_hdd_temp\', \'Max HDD Temp\'),\n                    data: data.timestamps.map((ts, i) => ({\n                        x: new Date(ts).getTime(),\n                        y: data.temps[i] ?? 0\n                    }))\n                },\n                {\n                    name: t(\'chart.avg_pwm\', \'Avg PWM\'),\n                    data: data.timestamps.map((ts, i) => ({\n                        x: new Date(ts).getTime(),\n                        y: data.pwm[i] ?? 0\n                    }))\n                }\n            ];\n\n            try {\n                if (!store.chart) {\n                    store.chart = new ApexCharts(chartContainer, {\n                        chart: {\n                            type: \'line\',\n                            height: 250,\n                            background: \'transparent\',\n                            foreColor: \'#9ca3af\',\n                            toolbar: { show: false },\n                            zoom: { enabled: false },\n                            animations: { enabled: true, easing: \'easeinout\', speed: 800 }\n                        },\n                        theme: { mode: \'dark\' },\n                        stroke: { curve: \'smooth\', width: [2, 1.5], dashArray: [0, 5] },\n                        colors: [\'#ff2d55\', \'#00f0ff\'],\n                        fill: {\n                            type: \'gradient\',\n                            gradient: { shade: \'dark\', type: \'vertical\', opacityFrom: 0.3, opacityTo: 0 }\n                        },\n                        markers: { size: 0, hover: { size: 4 } },\n                        grid: { borderColor: \'#1a1f2e\', strokeDashArray: 4 },\n                        xaxis: { type: \'datetime\', labels: { style: { colors: \'#6b7280\' } } },\n                        yaxis: [\n                            { title: { text: getTempUnitSymbol(), style: { color: \'#ff2d55\' } }, labels: { style: { colors: \'#6b7280\' } } },\n                            { opposite: true, title: { text: \'%\', style: { color: \'#00f0ff\' } }, labels: { style: { colors: \'#6b7280\' } }, min: 0, max: 100 }\n                        ],\n                        legend: { position: \'top\', labels: { colors: \'#9ca3af\' } },\n                        tooltip: { theme: \'dark\', x: { format: \'HH:mm\' } }\n                    });\n                    store.chart.render();\n                } else {\n                    store.chart.updateSeries(series, true);\n                }\n            } catch (e) {\n                console.warn(\'Chart render error (non-critical):\', e.message);\n            }\n        })\n        .catch(() => {});\n}\n\nsetInterval(updateChart, 60000);\n',
    'main.js': '/**\n * FanControl Web v3.4.1 - Neon Cyberpunk Edition\n * Main JavaScript Application\n */\n\nimport {\n    store, i18n, CHART_UPDATE_INTERVAL, RELOAD_DELAY, SCHEDULE_CELL_SIZE, SPARKLINE_MAX,\n    BTN_ACTIVE, BTN_INACTIVE,\n    settingsDefaults, settings, schedule, dashboard, cardDrag, cardResize, cardEdit,\n    smart, groupDrag, timers, dsm, logging, update, conflict, debug, sparklineHistory,\n} from \'./store.js\';\nimport { escapeHtml, fanIcon, show, hide, toggle, formatTemp, getTempUnitSymbol, formatBytes, getUnitLabel, getTempColorClass, getSettings, saveSettings, showToast } from \'./utils.js\';\nimport { loadLang, t, applyTranslations } from \'./i18n.js\';\nimport { healthIcon, buildSensorCheckboxList, setModeButtonStyles } from \'./render-helpers.js\';\nimport { registerSocketHandlers } from \'./socket-handlers.js\';\nimport { updateChart } from \'./charts.js\';\n\nfunction setDiscoverButtonState(loading) {\n    const btn = document.getElementById(\'discover-btn\');\n    const loader = document.getElementById(\'discover-loader\');\n    if (btn) btn.disabled = loading;\n    if (loader) toggle(loader, loading);\n}\n\n// ============================================================================\n// SOCKET.IO CONNECTION\n// ============================================================================\n\nconsole.log(\'[FanControl] Establishing Socket.IO connection...\');\nconst socket = io();\nwindow.socket = socket;\n\n// Register all socket event handlers centrally\nregisterSocketHandlers(socket, {\n    showServerUnavailable,\n    hideServerUnavailable,\n    updateUI,\n    buildServerTree,\n    updateCalibrationModal,\n    renderDiscoveredHardware,\n    hideCalibrationModal,\n    showMainScreen,\n    renderUpdateAgentProgress,\n    checkAgentsDone,\n    renderAgentLogsModal,\n    renderNodesOverview,\n    loadNodeDetail,\n    showConflictModal,\n    showManualModeWarning,\n    loadNodes,\n});\n\nfunction showServerUnavailable() {\n    const banner = document.getElementById(\'server-unavailable-banner\');\n    if (banner) banner.classList.remove(\'hidden\');\n}\n\nfunction hideServerUnavailable() {\n    const banner = document.getElementById(\'server-unavailable-banner\');\n    if (banner) banner.classList.add(\'hidden\');\n}\n\n// ============================================================================\n// UI UPDATE FUNCTIONS\n// ============================================================================\n\nfunction updateUI(data) {\n    if (!data) return;\n    \n    // Update version displays\n    const ver = data.config_version || \'\';\n    const headerVer = document.getElementById(\'header-version\');\n    if (headerVer && ver) headerVer.textContent = `v${ver}`;\n    const versionLink = document.getElementById(\'version-link\');\n    if (versionLink && ver) versionLink.textContent = `FanControl Web v${ver}`;\n    \n    // Show appropriate screen\n    if (!data.initialized || !data.tested) {\n        // Don\'t flash setup screen during restart if we were already on main screen\n        if (store.wasOnMainScreen) {\n            return;\n        }\n        showSetupScreen();\n        if (data.hardware_scanned && store.wizardStep === \'intro\') {\n            renderDiscoveredHardware({\n                fans: data.fans,\n                temps: data.temp_sensors,\n                disks: data.hdd_sensors\n            });\n            store.wizardStep = \'results\';\n            setDiscoverButtonState(false);\n        }\n        return;\n    }\n    \n    showMainScreen();\n    \n    // Update indicators\n    updateFailsafeIndicator(data.failsafe);\n    updateStandbyIndicator(data.standby_mode);\n    \n    // Build fan list only when empty; otherwise update in-place to preserve pulse timers\n    if (data.fans && Object.keys(data.fans).length > 0) {\n        const container = document.getElementById(\'fan-list\');\n        const existingCount = container ? container.querySelectorAll(\'.fan-card\').length : 0;\n        if (existingCount === 0) {\n            buildFanList(data.fans);\n        }\n        // Always update health classes (works on both new and existing cards)\n        updateFanHealthClasses(data.fans);\n        // DEBUG: log fan health status\n        for (const [fid, f] of Object.entries(data.fans)) {\n            if (f.health && f.health.status !== \'healthy\') {\n                console.log(`[fan-health] ${fid}: status=${f.health.status} rpm=${f.rpm} writable=${f.writable} mode=${f.mode}`);\n            }\n        }\n    }\n    \n    // Build disks list\n    if (data.hdd_sensors) {\n        buildDisksList(data.hdd_sensors);\n    }\n    \n    // Build sensor list for popup\n    buildSensorList(data);\n    \n    // Update inspector if a fan is selected\n    if (store.currentFanId && data.fans && data.fans[store.currentFanId]) {\n        updateInspector(data.fans[store.currentFanId]);\n    }\n    \n    // Update chart\n    updateChart();\n\n    // Refresh server tree\n    if (dashboard.loaded) buildServerTree();\n\n    // Dashboard live updates handled by startPickerLiveUpdate\n}\n\nfunction showSetupScreen() {\n    document.getElementById(\'setup-screen\').classList.remove(\'hidden\');\n    document.getElementById(\'main-screen\').classList.add(\'hidden\');\n    stopPickerLiveUpdate();\n    stopSystemUpdate();\n    // Close settings panel if open\n    const overlay = document.getElementById(\'settings-overlay\');\n    const panel = document.getElementById(\'settings-panel\');\n    if (overlay) overlay.classList.add(\'hidden\');\n    if (panel) panel.classList.add(\'hidden\');\n}\n\nfunction showMainScreen() {\n    store.wasOnMainScreen = true;\n    const mainScreen = document.getElementById(\'main-screen\');\n    const wasOnSetup = mainScreen?.classList.contains(\'hidden\');\n\n    document.getElementById(\'setup-screen\').classList.add(\'hidden\');\n    mainScreen?.classList.remove(\'hidden\');\n    if (!store.state || !store.state.testing) {\n        hideCalibrationModal();\n    }\n    if (wasOnSetup) showView(\'dashboard\');\n    updateCanvasColumns();\n    if (wasOnSetup) {\n        loadPickerCards().then(() => {\n            buildServerTree();\n            startPickerLiveUpdate();\n            startSystemUpdate();\n        });\n    } else {\n        if (!dashboard.liveTimer) startPickerLiveUpdate();\n        startSystemUpdate();\n    }\n}\n\nfunction updateFailsafeIndicator(failsafe) {\n    const el = document.getElementById(\'failsafe-indicator\');\n    if (failsafe) {\n        el.classList.remove(\'hidden\');\n    } else {\n        el.classList.add(\'hidden\');\n    }\n}\n\nfunction updateStandbyIndicator(standby) {\n    const el = document.getElementById(\'standby-indicator\');\n    if (standby) {\n        el.classList.remove(\'hidden\');\n    } else {\n        el.classList.add(\'hidden\');\n    }\n}\n\n// ============================================================================\n// FAN LIST (Left Panel)\n// ============================================================================\n\nfunction buildFanList(fans) {\n    const container = document.getElementById(\'fan-list\');\n    if (!container) return;\n    \n    let html = \'\';\n    \n    for (const [fanId, fan] of Object.entries(fans)) {\n        const isSelected = fanId === store.currentFanId;\n        const borderColor = isSelected ? \'border-neon-purple\' : \'border-cyber-accent\';\n        const bgColor = isSelected ? \'bg-cyber-accent\' : \'bg-cyber-card\';\n        const healthStatus = fan.health?.status || \'healthy\';\n        const healthClass = healthStatus === \'stopped\' ? \'fan-alert-stopped\' :\n                            healthStatus === \'slowing\' ? \'fan-alert-slowing\' :\n                            healthStatus === \'needs_calibration\' ? \'fan-alert-needs-calibration\' : \'\';\n        // Remove transition-all when health alert is active so CSS animation works\n        const transitionClass = healthClass ? \'\' : \'transition-all duration-200\';\n\n        html += `\n            <div id="fan-card-${escapeHtml(fanId)}"\n                 class="fan-card ${bgColor} border ${borderColor} ${healthClass} rounded-lg px-3 py-2.5 pb-2 cursor-pointer\n                        hover:border-neon-purple ${transitionClass}"\n                 onclick="selectFan(\'${escapeHtml(fanId)}\')">\n                <div class="flex items-center justify-between mb-1">\n                    <span class="text-sm font-semibold text-white truncate">${escapeHtml(fan.label)}</span>\n                    <div class="flex items-center gap-1">\n                        ${fan.inverted ? `<span class="text-xs px-1.5 py-0.5 rounded bg-cyan-900 bg-opacity-30 text-neon-cyan">${t(\'fan.inv\', \'INV\')}</span>` : \'\'}\n                        <span class="text-xs px-1.5 py-0.5 rounded ${getStatusBadgeClass(fan.health?.status || fan.status)}">${t(\'status.\' + (fan.health?.status || fan.status), fan.health?.status || fan.status)}</span>\n                    </div>\n                </div>\n                <div class="flex items-center justify-between text-xs">\n                    <span class="text-gray-500">${t(\'mode.\' + (fan.mode || \'manual\'), fan.mode || \'manual\')}</span>\n                    <span class="font-mono text-neon-cyan" id="fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0} ${t(\'fan.rpm\', \'RPM\')}</span>\n                </div>\n            </div>\n        `;\n    }\n    \n    container.innerHTML = html || `<div class="text-center text-gray-500 py-8">${t(\'setup.no_fans\', \'No fans detected\')}</div>`;\n\n    // Start pulse on any cards that already have health alerts\n    for (const [fanId, fan] of Object.entries(fans)) {\n        const healthStatus = fan.health?.status || \'healthy\';\n        if (healthStatus !== \'healthy\') {\n            const card = document.getElementById(`fan-card-${fanId}`);\n            if (card) startCardPulse(card, healthStatus);\n        }\n    }\n}\n\nfunction updateFanHealthClasses(fans) {\n    const healthClasses = [\'fan-alert-stopped\', \'fan-alert-slowing\', \'fan-alert-needs-calibration\'];\n    for (const [fanId, fan] of Object.entries(fans)) {\n        const card = document.getElementById(`fan-card-${fanId}`);\n        if (!card) continue;\n        const healthStatus = fan.health?.status || \'healthy\';\n        const hasAny = healthClasses.some(c => card.classList.contains(c));\n\n        if (healthStatus !== \'healthy\' && !hasAny) {\n            console.log(`[fan-health] STARTING pulse for ${fanId}: ${healthStatus}`);\n            card.classList.remove(\'transition-all\', \'duration-200\');\n            healthClasses.forEach(c => card.classList.remove(c));\n            card.classList.add(`fan-alert-${healthStatus}`);\n            startCardPulse(card, healthStatus);\n        } else if (healthStatus === \'healthy\' && hasAny) {\n            console.log(`[fan-health] STOPPING pulse for ${fanId}`);\n            healthClasses.forEach(c => card.classList.remove(c));\n            card.classList.add(\'transition-all\', \'duration-200\');\n            stopCardPulse(card);\n        }\n\n        // Update status badge\n        const badge = card.querySelector(\'.text-xs.px-1\\\\.5\');\n        if (badge) {\n            const ds = fan.health?.status || fan.status;\n            badge.textContent = t(\'status.\' + ds, ds);\n            badge.className = `text-xs px-1.5 py-0.5 rounded ${getStatusBadgeClass(ds)}`;\n        }\n    }\n}\n\nconst _cardPulseTimers = new Map();\n\nfunction startCardPulse(card, status) {\n    stopCardPulse(card);\n    const color = status === \'stopped\' ? \'#ef4444\' : \'#facc15\';\n    const dim   = status === \'stopped\' ? \'#450a0a\' : \'#422006\';\n    let on = true;\n    function tick() {\n        on = !on;\n        card.style.setProperty(\'outline\', on ? `3px solid ${color}` : `3px solid ${dim}`, \'important\');\n        card.style.setProperty(\'outline-offset\', \'-3px\', \'important\');\n    }\n    tick(); // immediate first frame\n    const timer = setInterval(tick, 750);\n    _cardPulseTimers.set(card.id, timer);\n    console.log(`[fan-health] pulse timer started for ${card.id}, color=${color}`);\n}\n\nfunction stopCardPulse(card) {\n    const t = _cardPulseTimers.get(card.id);\n    if (t) { clearInterval(t); _cardPulseTimers.delete(card.id); }\n    card.style.removeProperty(\'outline\');\n    card.style.removeProperty(\'outline-offset\');\n}\n\nfunction selectFan(fanId) {\n    store.currentFanId = fanId;\n    \n    // Update card highlights\n    document.querySelectorAll(\'.fan-card\').forEach(card => {\n        card.classList.remove(\'border-neon-purple\', \'bg-cyber-accent\');\n        card.classList.add(\'border-cyber-accent\', \'bg-cyber-card\');\n    });\n    \n    const selectedCard = document.getElementById(`fan-card-${fanId}`);\n    if (selectedCard) {\n        selectedCard.classList.add(\'border-neon-purple\', \'bg-cyber-accent\');\n        selectedCard.classList.remove(\'border-cyber-accent\', \'bg-cyber-card\');\n    }\n    \n    // Show inspector\n    if (store.state && store.state.fans && store.state.fans[fanId]) {\n        updateInspector(store.state.fans[fanId]);\n    }\n}\n\n// ============================================================================\n// NODE TREE\n// ============================================================================\n\nfunction buildServerTree() {\n    const container = document.getElementById(\'server-tree\');\n    if (!container) return;\n\n    let html = \'\';\n\n    // Local server\n    html += renderLocalServerTree();\n\n    // Remote nodes\n    for (const node of store.nodesData) {\n        html += renderRemoteNodeTree(node);\n    }\n\n    container.innerHTML = html || `<div class="text-center text-gray-500 py-4 text-xs">${t(\'nodes.no_nodes\', \'No nodes connected\')}</div>`;\n\n    _collapsedNodes.forEach(nodeId => {\n        const children = document.getElementById(`node-children-${nodeId}`);\n        if (children) children.classList.add(\'hidden\');\n    });\n}\n\nfunction getHiddenSensors() {\n    return dashboard.hiddenSensors || [];\n}\n\nfunction setHiddenSensors(hidden) {\n    dashboard.hiddenSensors = hidden;\n    scheduleDashboardSave();\n}\n\nfunction hideSensor(sensorId) {\n    const el = document.querySelector(`[data-sensor-id="${sensorId}"]`);\n    if (el) {\n        el.style.transition = \'opacity 0.3s, max-height 0.3s, margin 0.3s, padding 0.3s\';\n        el.style.overflow = \'hidden\';\n        el.style.opacity = \'0\';\n        el.style.maxHeight = \'0\';\n        el.style.marginTop = \'0\';\n        el.style.marginBottom = \'0\';\n        el.style.paddingTop = \'0\';\n        el.style.paddingBottom = \'0\';\n        setTimeout(() => {\n            const hidden = getHiddenSensors();\n            if (!hidden.includes(sensorId)) {\n                setHiddenSensors([...hidden, sensorId]);\n            }\n            buildServerTree();\n        }, 320);\n    } else {\n        const hidden = getHiddenSensors();\n        if (!hidden.includes(sensorId)) {\n            setHiddenSensors([...hidden, sensorId]);\n        }\n        buildServerTree();\n    }\n}\n\nfunction restoreSensor(sensorId) {\n    setHiddenSensors(getHiddenSensors().filter(id => id !== sensorId));\n    buildServerTree();\n}\n\nfunction restoreAllSensors() {\n    setHiddenSensors([]);\n    buildServerTree();\n}\n\nfunction renderLocalServerTree() {\n    if (!store.state || !store.state.fans) return \'\';\n\n    const fans = store.state.fans;\n    const temps = store.state.temp_sensors || {};\n    const disks = store.state.hdd_sensors || {};\n    const hidden = getHiddenSensors();\n\n    const visibleFans = Object.entries(fans).filter(([id]) => !hidden.includes(`fan:${id}`));\n    const visibleTemps = Object.entries(temps).filter(([id]) => !hidden.includes(`temp:${id}`));\n    const visibleDisks = Object.entries(disks).filter(([id]) => !hidden.includes(`disk:${id}`));\n    const hiddenFans = Object.entries(fans).filter(([id]) => hidden.includes(`fan:${id}`));\n    const hiddenTemps = Object.entries(temps).filter(([id]) => hidden.includes(`temp:${id}`));\n    const hiddenDisks = Object.entries(disks).filter(([id]) => hidden.includes(`disk:${id}`));\n    const hasHidden = hiddenFans.length + hiddenTemps.length + hiddenDisks.length > 0;\n\n    const serverVer = store.state?.config_version || \'\';\n\n    let html = `\n        <div class="node-group" data-node="local">\n            <div class="p-2 rounded hover:bg-cyber-accent cursor-pointer node-header group"\n                 onclick="toggleNodeGroup(\'local\')">\n                <div class="flex items-center gap-1.5">\n                    <span class="w-2 h-2 bg-neon-cyan rounded-full flex-shrink-0"></span>\n                    <span class="text-sm font-semibold text-white truncate flex-1">${escapeHtml(store.state.server_name || t(\'nodes.local_server\', \'My Server\'))}</span>\n                    ${serverVer ? `<span class="text-[10px] text-gray-600" title="${escapeHtml(serverVer)}">${escapeHtml(serverVer)}</span>` : \'\'}\n                    <button onclick="event.stopPropagation(); openServerNameEdit()"\n                            class="w-4 h-4 flex items-center justify-center text-gray-400 hover:text-neon-cyan rounded text-[10px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Rename">✎</button>\n                </div>\n                <div class="flex items-center gap-2 mt-0.5 ml-3.5">\n                    <span class="text-[10px] text-neon-green">online</span>\n                    ${visibleFans.length > 0 ? `<span class="text-[10px] text-gray-500">· ${visibleFans.length} ${t(\'nodes.fans\', \'fans\')}</span>` : \'\'}\n                    ${Object.keys(temps).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(temps).length} ${t(\'nodes.sensors\', \'sensors\')}</span>` : \'\'}\n                    ${Object.keys(disks).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(disks).length} ${t(\'nodes.disks\', \'disks\')}</span>` : \'\'}\n                </div>\n            </div>\n            <div class="node-children ml-4 space-y-px" id="node-children-local">\n    `;\n\n    for (const [fanId, fan] of visibleFans) {\n        const isSelected = fanId === store.currentFanId;\n        const fanHealth = fan.health?.status || \'healthy\';\n        const _healthIcon = healthIcon(fan);\n        html += `\n            <div data-sensor-id="fan:${escapeHtml(fanId)}" class="flex items-center gap-1.5 p-1 rounded cursor-pointer transition-all group ${isSelected ? \'bg-cyber-accent border-l-2 border-neon-purple\' : \'hover:bg-cyber-accent border-l-2 border-transparent\'}"\n                 onclick="selectFanFromTree(\'${escapeHtml(fanId)}\', \'local\')">\n                ${fanIcon(fan)}\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(fan.label)}</span>\n                ${_healthIcon}\n                <span class="ml-auto text-xs font-mono text-neon-cyan" id="tree-fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0}</span>\n                <button onclick="event.stopPropagation(); hideSensor(\'fan:${escapeHtml(fanId)}\')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>\n            </div>\n        `;\n    }\n\n    for (const [sensorId, sensor] of visibleTemps) {\n        html += `\n            <div data-sensor-id="temp:${escapeHtml(sensorId)}" class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                <span class="text-xs">🌡</span>\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(sensor.label)}</span>\n                <span class="ml-auto text-xs font-mono text-neon-green">${sensor.value || 0}°C</span>\n                <button onclick="event.stopPropagation(); hideSensor(\'temp:${escapeHtml(sensorId)}\')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>\n            </div>\n        `;\n    }\n\n    for (const [diskId, disk] of visibleDisks) {\n        html += `\n            <div data-sensor-id="disk:${escapeHtml(diskId)}" class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                <span class="text-xs">💾</span>\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(disk.label || diskId)}</span>\n                <span class="ml-auto text-xs font-mono ${getTempColorClass(disk.temp)}">${disk.temp > 0 ? disk.temp + \'°C\' : \'--\'}</span>\n                <button onclick="event.stopPropagation(); hideSensor(\'disk:${escapeHtml(diskId)}\')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>\n            </div>\n        `;\n    }\n\n    if (hasHidden) {\n        const totalHidden = hiddenFans.length + hiddenTemps.length + hiddenDisks.length;\n        const isHiddenExpanded = !_collapsedNodes.has(\'local-hidden\');\n        const arrowChar = isHiddenExpanded ? \'▼\' : \'▶\';\n        html += `\n            <div class="mt-1 border-t border-gray-700/50 pt-1">\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent cursor-pointer"\n                     onclick="toggleNodeGroup(\'local-hidden\')">\n                    <span class="text-neon-cyan text-[10px]">${arrowChar}</span>\n                    <span class="text-[10px] text-gray-500">${t(\'nodes.hidden\', \'Hidden\')} (${totalHidden})</span>\n                    <button onclick="event.stopPropagation(); restoreAllSensors()" class="ml-auto text-[10px] text-gray-600 hover:text-neon-green px-1">↺ ${t(\'nodes.all\', \'all\')}</button>\n                </div>\n                <div class="node-children ml-4 space-y-px ${isHiddenExpanded ? \'\' : \'hidden\'}" id="node-children-local-hidden">\n        `;\n\n        for (const [fanId, fan] of hiddenFans) {\n            html += `\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                    <span class="opacity-50">${fanIcon(fan)}</span>\n                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(fan.label)}</span>\n                    <button onclick="restoreSensor(\'fan:${escapeHtml(fanId)}\')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="${t(\'nodes.restore\', \'Restore\')}">↺</button>\n                </div>\n            `;\n        }\n        for (const [sensorId, sensor] of hiddenTemps) {\n            html += `\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                    <span class="text-xs opacity-50">🌡</span>\n                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(sensor.label)}</span>\n                    <button onclick="restoreSensor(\'temp:${escapeHtml(sensorId)}\')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="${t(\'nodes.restore\', \'Restore\')}">↺</button>\n                </div>\n            `;\n        }\n        for (const [diskId, disk] of hiddenDisks) {\n            html += `\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                    <span class="text-xs opacity-50">💾</span>\n                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(disk.label || diskId)}</span>\n                    <button onclick="restoreSensor(\'disk:${escapeHtml(diskId)}\')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="${t(\'nodes.restore\', \'Restore\')}">↺</button>\n                </div>\n            `;\n        }\n\n        html += `</div></div>`;\n    }\n\n    html += `</div></div>`;\n    return html;\n}\n\nfunction renderRemoteNodeTree(node) {\n    const telemetry = node.telemetry || {};\n    const fans = telemetry.fans || {};\n    const temps = telemetry.temp_sensors || {};\n    const disks = telemetry.hdd_sensors || {};\n    const fanCount = Object.keys(fans).length;\n    const statusDot = node.status === \'online\' ? \'bg-neon-green\' : \'bg-gray-500\';\n\n    const serverVer = store.state?.config_version || \'\';\n    const agentVer = node.agent_version || \'\';\n    const updateStarted = node.update_started;\n\n    let versionBadge = \'\';\n    if (updateStarted) {\n        const elapsed = Math.round((Date.now() / 1000) - updateStarted);\n        if (elapsed > 180) {\n            node.update_started = null;\n        } else {\n            versionBadge = `<span class="text-[10px] px-1 py-0.5 rounded bg-cyan-900/50 text-neon-cyan border border-cyan-700/50 animate-pulse" title="Updating... ${elapsed}s">⟳ ${elapsed}s</span>`;\n        }\n    }\n    if (!versionBadge && agentVer && serverVer && agentVer !== serverVer) {\n        versionBadge = `<span class="text-[10px] px-1 py-0.5 rounded bg-orange-900/50 text-orange-400 border border-orange-700/50 cursor-pointer hover:bg-orange-800/50" onclick="event.stopPropagation(); updateSingleAgent(\'${escapeHtml(node.node_id)}\')" title="Server: ${escapeHtml(serverVer)} — ${t(\'nodes.click_to_update\', \'click to update\')}">↑ ${escapeHtml(agentVer)}</span>`;\n    } else if (!versionBadge && agentVer) {\n        versionBadge = `<span class="text-[10px] text-gray-600" title="${escapeHtml(agentVer)}">${escapeHtml(agentVer)}</span>`;\n    }\n\n    let html = `\n        <div class="node-group" data-node="${escapeHtml(node.node_id)}">\n            <div class="p-2 rounded hover:bg-cyber-accent cursor-pointer node-header group"\n                 onclick="toggleNodeGroup(\'${escapeHtml(node.node_id)}\')">\n                <div class="flex items-center gap-1.5">\n                    <span class="w-2 h-2 ${statusDot} rounded-full flex-shrink-0"></span>\n                    <span class="text-sm font-semibold text-white truncate flex-1">${escapeHtml(node.name)}</span>\n                    ${versionBadge}\n                    <button onclick="event.stopPropagation(); showNodeSettings(\'${escapeHtml(node.node_id)}\')"\n                            class="w-4 h-4 flex items-center justify-center text-gray-400 hover:text-neon-cyan rounded text-[10px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Settings">&#9881;</button>\n                    <button onclick="event.stopPropagation(); deleteNode(\'${escapeHtml(node.node_id)}\')"\n                            class="w-4 h-4 flex items-center justify-center text-gray-400 hover:text-red-400 rounded text-[10px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Delete">✕</button>\n                </div>\n                <div class="flex items-center gap-2 mt-0.5 ml-3.5">\n                    <span class="text-[10px] ${node.status === \'online\' ? \'text-neon-green\' : \'text-gray-500\'}">${node.status}</span>\n                    ${fanCount > 0 ? `<span class="text-[10px] text-gray-500">· ${fanCount} ${t(\'nodes.fans\', \'fans\')}</span>` : \'\'}\n                    ${Object.keys(temps).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(temps).length} ${t(\'nodes.sensors\', \'sensors\')}</span>` : \'\'}\n                    ${Object.keys(disks).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(disks).length} ${t(\'nodes.disks\', \'disks\')}</span>` : \'\'}\n                </div>\n            </div>\n            <div class="node-children ml-4 space-y-0.5 ${_collapsedNodes.has(node.node_id) ? \'hidden\' : \'\'}" id="node-children-${escapeHtml(node.node_id)}">\n    `;\n\n    for (const [fanId, fan] of Object.entries(fans)) {\n        const cleanLabel = (fan.label || fanId).replace(/\\s*\\(Synology-[^)]+\\)/, \'\');\n        const isDsm = fan.control_method === \'dsm_scemd\';\n        const fanHealth = fan.health?.status || \'healthy\';\n        const _healthIcon = healthIcon(fan);\n        html += `\n            <div class="flex items-center gap-2 p-1.5 rounded cursor-pointer hover:bg-cyber-accent"\n                 onclick="selectNodeFan(\'${escapeHtml(node.node_id)}\', \'${escapeHtml(fanId)}\')">\n                ${fanIcon(fan)}\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(cleanLabel)}${isDsm ? \' <span class="text-blue-400 text-[10px]">DSM</span>\' : \'\'}</span>\n                ${_healthIcon}\n                <span class="ml-auto text-xs font-mono text-neon-cyan">${fan.rpm || 0}</span>\n            </div>\n        `;\n    }\n\n    for (const [sensorId, sensor] of Object.entries(temps)) {\n        html += `\n            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">\n                <span class="text-xs">🌡</span>\n                <span class="text-xs text-gray-300 truncate">${escapeHtml(sensor.label || sensorId)}</span>\n                <span class="ml-auto text-xs font-mono text-neon-green">${sensor.value || 0}°C</span>\n            </div>\n        `;\n    }\n\n    for (const [diskId, disk] of Object.entries(disks)) {\n        html += `\n            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">\n                <span class="text-xs">💾</span>\n                <span class="text-xs text-gray-300 truncate">${escapeHtml(disk.label || diskId)}</span>\n                <span class="ml-auto text-xs font-mono ${getTempColorClass(disk.temp)}">${disk.temp > 0 ? disk.temp + \'°C\' : \'--\'}</span>\n            </div>\n        `;\n    }\n\n    if (fanCount === 0 && Object.keys(temps).length === 0 && Object.keys(disks).length === 0) {\n        html += `<div class="text-xs text-gray-600 p-1.5">${t(\'node.no_telemetry\', \'No telemetry\')}</div>`;\n    }\n\n    html += `</div></div>`;\n    return html;\n}\n\nlet _collapsedNodes = new Set(JSON.parse(localStorage.getItem(\'fc_collapsed_nodes\') || \'[]\'));\n\nfunction toggleNodeGroup(nodeId) {\n    const children = document.getElementById(`node-children-${nodeId}`);\n    if (children) {\n        children.classList.toggle(\'hidden\');\n        if (children.classList.contains(\'hidden\')) {\n            _collapsedNodes.add(nodeId);\n        } else {\n            _collapsedNodes.delete(nodeId);\n        }\n        localStorage.setItem(\'fc_collapsed_nodes\', JSON.stringify([..._collapsedNodes]));\n    }\n}\n\nfunction selectFanFromTree(fanId, source) {\n    store.currentFanId = fanId;\n\n    // Check if this is a DSM fan — open scheme editor instead of inspector\n    if (store.state && store.state.fans && store.state.fans[fanId]) {\n        const fan = store.state.fans[fanId];\n        if (fan.control_method === \'dsm_scemd\') {\n            showView(\'dsm-scheme\');\n            buildServerTree();\n            return;\n        }\n    }\n\n    // Show inspector view\n    showView(\'inspector\');\n\n    // Update inspector\n    if (source === \'local\' && store.state && store.state.fans && store.state.fans[fanId]) {\n        updateInspector(store.state.fans[fanId]);\n    }\n\n    // Rebuild server tree to highlight selected\n    buildServerTree();\n}\n\nfunction selectNodeFan(nodeId, fanId) {\n    // Check if this is a DSM fan on a remote node\n    const node = store.nodesData.find(n => n.node_id === nodeId);\n    if (node && node.telemetry && node.telemetry.fans && node.telemetry.fans[fanId]) {\n        const fan = node.telemetry.fans[fanId];\n        if (fan.control_method === \'dsm_scemd\') {\n            store.currentRemoteNodeId = nodeId;\n            showView(\'dsm-scheme\');\n            renderDsmSchemeEditor(nodeId);\n            return;\n        }\n    }\n    console.log(\'[FanControl] Select node fan:\', nodeId, fanId);\n}\n\n// ============================================================================\n// DASHBOARD CARDS\n// ============================================================================\n\nfunction showCardPicker() {\n    const modal = document.getElementById(\'card-picker-modal\');\n    if (!modal) return;\n    modal.classList.remove(\'hidden\');\n    populatePickerSources();\n    updatePickerElements();\n}\n\nfunction hideCardPicker() {\n    const modal = document.getElementById(\'card-picker-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n}\n\nfunction populatePickerSources() {\n    const select = document.getElementById(\'picker-source\');\n    if (!select) return;\n    select.innerHTML = `<option value="local">${t(\'picker.my_server\', \'My Server (local)\')}</option>`;\n    for (const node of store.nodesData) {\n        select.innerHTML += `<option value="${escapeHtml(node.node_id)}">${escapeHtml(node.name || node.node_id)}</option>`;\n    }\n}\n\nfunction updatePickerElements() {\n    const type = document.getElementById(\'picker-type\')?.value;\n    const source = document.getElementById(\'picker-source\')?.value;\n    const container = document.getElementById(\'picker-elements\');\n    if (!container) return;\n\n    let elements = [];\n\n    if (source === \'local\') {\n        if (type === \'fan\' && store.state?.fans) {\n            elements = Object.entries(store.state.fans).map(([id, f]) => ({ id, label: f.label || id, extra: `${f.rpm || 0} RPM` }));\n        } else if (type === \'temperature\' && store.state?.temp_sensors) {\n            elements = Object.entries(store.state.temp_sensors).map(([id, s]) => ({ id, label: s.label || id, extra: `${s.value || 0}°C` }));\n        } else if (type === \'disk\' && store.state?.hdd_sensors) {\n            elements = Object.entries(store.state.hdd_sensors).map(([id, d]) => ({ id, label: d.label || id, extra: `${d.temp || 0}°C` }));\n        } else if (type === \'system\') {\n            elements = [\n                { id: \'max_temp\', label: t(\'picker.max_temp\', \'Макс. температура\'), extra: `${store.state?.max_hdd_temp || \'--\'}°C` },\n                { id: \'fans_summary\', label: t(\'picker.fans_summary\', \'Сводка по вентиляторам\'), extra: \'\' },\n            ];\n        }\n    } else {\n        const node = store.nodesData.find(n => n.node_id === source);\n        if (node?.telemetry) {\n            const tel = node.telemetry;\n            if (type === \'fan\' && tel.fans) {\n                elements = Object.entries(tel.fans).map(([id, f]) => ({ id, label: f.label || id, extra: `${f.rpm || 0} RPM` }));\n            } else if (type === \'temperature\' && tel.temp_sensors) {\n                elements = Object.entries(tel.temp_sensors).map(([id, s]) => ({ id, label: s.label || id, extra: `${s.value || 0}°C` }));\n            } else if (type === \'disk\' && tel.hdd_sensors) {\n                elements = Object.entries(tel.hdd_sensors).map(([id, d]) => ({ id, label: d.label || id, extra: `${d.temp || 0}°C` }));\n            }\n        }\n    }\n\n    container.innerHTML = elements.length > 0\n        ? elements.map(el => {\n            const cardId = `picker-${source}-${el.id}`;\n            const exists = document.querySelector(`[data-card-id="${cardId}"]`);\n            return `<label class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">\n                <input type="checkbox" value="${escapeHtml(el.id)}" data-label="${escapeHtml(el.label)}" class="picker-checkbox rounded" ${exists ? \'checked disabled\' : \'\'}>\n                <span class="text-xs ${exists ? \'text-gray-500 line-through\' : \'text-gray-300\'}">${escapeHtml(el.label)}</span>\n                <span class="ml-auto text-xs text-gray-500">${exists ? t(\'picker.added\', \'добавлено\') : el.extra}</span>\n            </label>`;\n        }).join(\'\')\n        : `<div class="text-xs text-gray-500 text-center py-4">${t(\'picker.no_elements\', \'Элементы не найдены\')}</div>`;\n}\n\nfunction addSelectedCards() {\n    const type = document.getElementById(\'picker-type\')?.value;\n    const source = document.getElementById(\'picker-source\')?.value;\n    const checkboxes = document.querySelectorAll(\'.picker-checkbox:checked\');\n    if (!checkboxes.length) return;\n\n    const saved = getPickerCards();\n\n    checkboxes.forEach(cb => {\n        const cardId = `picker-${source}-${cb.value}`;\n        if (document.querySelector(`[data-card-id="${cardId}"]`)) return;\n        if (saved.some(c => c.id === cardId)) return;\n\n        const label = cb.dataset.label || cb.value;\n        const colSpan = 3;\n        const pos = findFreePosition(saved, colSpan, 1, null);\n        const cardData = { id: cardId, type, source, sourceId: cb.value, label, col: pos.col, row: pos.row, colSpan };\n        renderPickerCard(cardData);\n        saved.push(cardData);\n    });\n\n    setPickerCards(saved);\n    document.getElementById(\'dashboard-empty\')?.classList.add(\'hidden\');\n    hideCardPicker();\n    startPickerLiveUpdate();\n}\n\nfunction renderPickerCard(card) {\n    const { id, type, source, sourceId, label } = card;\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    let icon = \'📊\';\n    let colorClass = \'text-neon-cyan\';\n    let valueHtml = \'\';\n\n    if (type === \'fan\') {\n        const fanData = getFanData(source, sourceId);\n        const fanStatus = fanData?.status || \'unknown\';\n        const rpm = fanData?.rpm || 0;\n        const dotColor = fanStatus === \'running\' ? \'green\' : (fanStatus === \'failsafe\' || fanStatus === \'critical\') ? \'red\' : \'yellow\';\n        const fanColor = fanStatus === \'running\' ? \'#22d3ee\' : (fanStatus === \'failsafe\' || fanStatus === \'critical\') ? \'#ef4444\' : \'#facc15\';\n        const animDuration = rpm > 0 ? Math.max(0.2, 2 - (rpm / 1500)) : 0;\n        const animStyle = rpm > 0 ? `animation: fan-spin ${animDuration}s linear infinite` : \'\';\n        icon = `<svg class="w-8 h-8 inline-block" data-fan-anim-id="${sourceId}" data-fan-source="${source}" viewBox="0 0 100 100" style="${animStyle}">\n            <g fill="${fanColor}" opacity="0.9">\n                <path d="M50 50 Q30 20 50 5 Q70 20 50 50"/>\n                <path d="M50 50 Q80 30 95 50 Q80 70 50 50"/>\n                <path d="M50 50 Q70 80 50 95 Q30 80 50 50"/>\n                <path d="M50 50 Q20 70 5 50 Q20 30 50 50"/>\n            </g>\n            <circle cx="50" cy="50" r="6" fill="${fanColor}" opacity="0.6"/>\n        </svg> <span class="status-dot ${dotColor}"></span>`;\n        colorClass = \'text-neon-cyan\';\n        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-fan-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">RPM</span></div>`;\n        valueHtml += renderSparkline(`fan:${source}:${sourceId}`, \'#22d3ee\');\n    } else if (type === \'temperature\') {\n        icon = \'🌡\';\n        colorClass = \'text-neon-green\';\n        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-temp-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">°C</span></div>`;\n        valueHtml += renderSparkline(`temp:${source}:${sourceId}`, \'#4ade80\');\n    } else if (type === \'disk\') {\n        icon = \'💾\';\n        colorClass = \'text-neon-purple\';\n        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-disk-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">°C</span></div>`;\n        valueHtml += renderSparkline(`disk:${source}:${sourceId}`, \'#c084fc\');\n    } else if (type === \'system\') {\n        icon = \'🖥\';\n        colorClass = \'text-yellow-400\';\n        valueHtml = `\n        <div class="space-y-2 mt-1">\n            <div class="flex justify-between text-xs">\n                <span class="text-gray-500">Uptime</span>\n                <span class="text-gray-300 font-mono" data-system-field="uptime">--</span>\n            </div>\n            <div>\n                <div class="flex justify-between text-xs mb-1">\n                    <span class="text-gray-500">CPU</span>\n                    <span class="text-gray-300 font-mono" data-system-field="cpu">--%</span>\n                </div>\n                <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">\n                    <div class="h-full bg-cyan-400 rounded-full transition-all duration-500" data-system-bar="cpu" style="width:0%"></div>\n                </div>\n            </div>\n            <div>\n                <div class="flex justify-between text-xs mb-1">\n                    <span class="text-gray-500">RAM</span>\n                    <span class="text-gray-300 font-mono" data-system-field="mem">--%</span>\n                </div>\n                <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">\n                    <div class="h-full bg-purple-400 rounded-full transition-all duration-500" data-system-bar="mem" style="width:0%"></div>\n                </div>\n            </div>\n        </div>`;\n    } else {\n        valueHtml = `<div class="text-2xl font-bold font-mono text-neon-cyan">--</div>`;\n    }\n\n    const configBtn = type === \'fan\'\n        ? `<button onclick="event.stopPropagation(); showCardConfig(\'${id}\')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Configure">⚙</button>`\n        : type === \'disk\'\n        ? `<button onclick="event.stopPropagation(); showSmartModal(\'${id}\')" class="text-gray-600 hover:text-neon-purple text-xs transition-colors" title="SMART">⚙</button>`\n        : \'\';\n    const lockIcon = card.lockSize ? \'🔒\' : \'🔓\';\n    const lockClass = card.lockSize ? \'text-neon-cyan\' : \'text-gray-600\';\n    const lockBtn = `<button onclick="event.stopPropagation(); toggleCardLockSize(\'${id}\')" class="lock-size-btn ${lockClass} hover:text-neon-cyan text-xs transition-colors" title="Lock/Unlock size">${lockIcon}</button>`;\n    const editBtn = `<button onclick="event.stopPropagation(); showCardEdit(\'${id}\')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Edit name">✎</button>`;\n    const removeBtn = `<button onclick="event.stopPropagation(); removePickerCard(\'${id}\')" class="text-gray-600 hover:text-red-400 text-xs transition-colors">×</button>`;\n\n    const el = document.createElement(\'div\');\n    const gradientClass = `card-gradient-${type}`;\n    el.className = `border border-cyber-accent rounded-xl p-4 transition-[border-color,box-shadow,background-image] duration-200 hover:border-neon-cyan/50 hover:shadow-neon-cyan/10 hover:shadow-lg cursor-grab active:cursor-grabbing ${gradientClass}`;\n    el.setAttribute(\'data-card-id\', id);\n    el.innerHTML = `\n        <div class="card-content overflow-hidden h-full flex flex-col">\n            <div class="flex items-center justify-between mb-1">\n                <div class="flex items-center gap-2">\n                    <span class="text-gray-600 text-xs select-none">⠿</span>\n                    <span class="text-lg">${icon}</span>\n                    <span class="text-sm text-gray-300 font-medium truncate">${escapeHtml(label)}</span>\n                </div>\n            <div class="flex items-center gap-1">\n                ${configBtn}${lockBtn}${editBtn}${removeBtn}\n            </div>\n            </div>\n            ${valueHtml}\n            <div class="card-details flex-1 overflow-y-auto min-h-0"></div>\n        </div>\n        <div class="card-resize-handle"></div>`;\n\n    el.addEventListener(\'mousedown\', onCardMouseDown);\n\n    if (!card.col || !card.row) {\n        const saved = getPickerCards().filter(c => c.id !== card.id);\n        const pos = findFreePosition(saved, card.colSpan || 3, 1, card.id);\n        card.col = pos.col;\n        card.row = pos.row;\n    }\n    el.style.gridColumn = `${card.col} / span ${card.colSpan || 3}`;\n    el.style.gridRow = `${card.row} / span ${card.rowSpan || 1}`;\n    el.style.position = \'relative\';\n    el.style.alignSelf = \'stretch\';\n    el.style.minWidth = \'0\';\n\n    canvas.appendChild(el);\n\n    const resizeHandle = el.querySelector(\'.card-resize-handle\');\n    if (resizeHandle) {\n        resizeHandle.addEventListener(\'mousedown\', (e) => onCardResizeStart(e, id));\n        if (card.lockSize) resizeHandle.style.display = \'none\';\n    }\n    if (card.lockSize) el.style.cursor = \'default\';\n\n    if (type === \'disk\') {\n        el.addEventListener(\'click\', (e) => {\n            if (cardDrag.occurred || e.target.closest(\'button\')) return;\n            showSmartModal(id);\n        });\n    }\n\n    updateCardDetails(id);\n}\n\nfunction snapCardToGrid(cardEl) {\n    const cardId = cardEl.dataset?.cardId;\n    if (!cardId) return;\n    if (cardDrag.mouseDown?.cardId === cardId || cardResize.resizing?.cardId === cardId) return;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    const current = card.rowSpan || 1;\n    const needed = computeMinRows(cardEl);\n\n    if (needed !== current) {\n        const delta = needed - current;\n        const oldBottom = card.row + current;\n        const cardColStart = card.col || 1;\n        const cardColEnd = cardColStart + (card.colSpan || 3) - 1;\n        card.rowSpan = needed;\n        cardEl.style.gridRow = `${card.row} / span ${needed}`;\n\n        for (const c of saved) {\n            if (c.id === card.id || !c.col || !c.row) continue;\n            const cColStart = c.col;\n            const cColEnd = cColStart + (c.colSpan || 3) - 1;\n            if (c.row >= oldBottom && cColStart <= cardColEnd && cColEnd >= cardColStart) {\n                c.row += delta;\n                const el = document.querySelector(`[data-card-id="${c.id}"]`);\n                if (el) el.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;\n            }\n        }\n\n        setPickerCards(saved);\n    }\n}\n\nfunction toggleCardLockSize(cardId) {\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    card.lockSize = !card.lockSize;\n    setPickerCards(saved);\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (!el) return;\n    const btn = el.querySelector(\'.lock-size-btn\');\n    if (btn) {\n        btn.textContent = card.lockSize ? \'🔒\' : \'🔓\';\n        btn.className = card.lockSize\n            ? \'lock-size-btn text-neon-cyan hover:text-neon-cyan text-xs transition-colors\'\n            : \'lock-size-btn text-gray-600 hover:text-neon-cyan text-xs transition-colors\';\n    }\n    const handle = el.querySelector(\'.card-resize-handle\');\n    if (handle) handle.style.display = card.lockSize ? \'none\' : \'\';\n    el.style.cursor = card.lockSize ? \'default\' : \'grab\';\n}\nfunction computeMinRows(el) {\n    const contentEl = el.querySelector(\'.card-content\');\n    el.style.alignSelf = \'start\';\n    if (contentEl) { contentEl.style.height = \'auto\'; contentEl.style.overflow = \'visible\'; }\n    void el.offsetHeight;\n    const contentH = contentEl ? contentEl.scrollHeight : 0;\n    const padV = parseFloat(getComputedStyle(el).paddingTop) + parseFloat(getComputedStyle(el).paddingBottom);\n    el.style.alignSelf = \'stretch\';\n    if (contentEl) { contentEl.style.height = \'\'; contentEl.style.overflow = \'\'; }\n    for (let r = 1; r <= 10; r++) {\n        if (contentH <= r * 100 - padV - 2 + 10) return r;\n    }\n    return 10;\n}\n\nfunction onCardResizeStart(e, cardId) {\n    e.preventDefault();\n    e.stopPropagation();\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (!el) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (card?.lockSize) return;\n\n    cardResize.minRowSpan = computeMinRows(el);\n\n    cardResize.resizing = { cardId, el, col: card?.col, row: card?.row };\n    cardResize.startX = e.clientX;\n    cardResize.startY = e.clientY;\n    cardResize.startW = el.offsetWidth;\n    cardResize.startH = el.offsetHeight;\n\n    el.setAttribute(\'draggable\', \'false\');\n    document.body.style.cursor = \'se-resize\';\n    document.body.style.userSelect = \'none\';\n\n    document.addEventListener(\'mousemove\', onCardResizeMove);\n    document.addEventListener(\'mouseup\', onCardResizeEnd);\n}\n\nfunction getCanvasCols() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return 12;\n    const style = getComputedStyle(canvas);\n    return style.gridTemplateColumns.split(\' \').length || 12;\n}\n\nfunction updateCanvasColumns() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    const w = window.innerWidth;\n    let cols = 4;\n    if (w >= 1280) cols = 12;\n    else if (w >= 1024) cols = 8;\n    else if (w >= 640) cols = 6;\n    canvas.style.display = \'grid\';\n    canvas.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;\n    canvas.style.gridAutoRows = \'100px\';\n    canvas.style.gap = \'8px\';\n    canvas.style.position = \'relative\';\n}\n\nfunction onCardResizeMove(e) {\n    if (!cardResize.resizing) return;\n    const el = cardResize.resizing.el;\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    const dx = e.clientX - cardResize.startX;\n    const dy = e.clientY - cardResize.startY;\n    const cols = getCanvasCols();\n    const gap = 8;\n    const padL = parseInt(getComputedStyle(canvas).paddingLeft) || 16;\n    const padR = parseInt(getComputedStyle(canvas).paddingRight) || 16;\n    const contentW = canvas.offsetWidth - padL - padR;\n    const colWidth = (contentW - (cols - 1) * gap) / cols;\n    const rowHeight = 100;\n    const rowStep = rowHeight + gap;\n\n    const newW = cardResize.startW + dx;\n    const newH = cardResize.startH + dy;\n    const newColSpan = Math.max(2, Math.min(cols, Math.round(newW / (colWidth + gap))));\n    const newRowSpan = Math.max(cardResize.minRowSpan, Math.min(8, Math.round(newH / rowStep)));\n\n    el.style.gridColumn = `${cardResize.resizing.col || \'auto\'} / span ${newColSpan}`;\n    el.style.gridRow = `${cardResize.resizing.row || \'auto\'} / span ${newRowSpan}`;\n    el._resizeColSpan = newColSpan;\n    el._resizeRowSpan = newRowSpan;\n}\n\nfunction onCardResizeEnd(e) {\n    if (!cardResize.resizing) return;\n    const el = cardResize.resizing.el;\n    const cardId = cardResize.resizing.cardId;\n\n    let colSpan = el._resizeColSpan || 3;\n    let rowSpan = el._resizeRowSpan || 1;\n\n    document.body.style.cursor = \'\';\n    document.body.style.userSelect = \'\';\n\n    document.removeEventListener(\'mousemove\', onCardResizeMove);\n    document.removeEventListener(\'mouseup\', onCardResizeEnd);\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (card) {\n        if (rowSpan < cardResize.minRowSpan) rowSpan = cardResize.minRowSpan;\n        const cols = getCanvasCols();\n        if (card.col + colSpan - 1 > cols) colSpan = cols - card.col + 1;\n\n        card.colSpan = colSpan;\n        card.rowSpan = rowSpan;\n        resolveOverlaps(saved, cardId);\n\n        for (const c of saved) {\n            if (c.id === cardId) continue;\n            const el2 = document.querySelector(`[data-card-id="${c.id}"]`);\n            if (el2) {\n                el2.style.gridColumn = `${c.col} / span ${c.colSpan || 3}`;\n                el2.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;\n            }\n        }\n        el.style.gridColumn = `${card.col} / span ${colSpan}`;\n        el.style.gridRow = `${card.row} / span ${rowSpan}`;\n        setPickerCards(saved);\n    }\n\n    cardResize.resizing = null;\n    cardDrag.occurred = true;\n    setTimeout(() => { cardDrag.occurred = false; }, 200);\n    updateCanvasMinHeight();\n}\n\nfunction getGridCell(canvas, x, y) {\n    const rect = canvas.getBoundingClientRect();\n    const cs = getComputedStyle(canvas);\n    const padL = parseFloat(cs.paddingLeft) || 16;\n    const padT = parseFloat(cs.paddingTop) || 16;\n    const padR = parseFloat(cs.paddingRight) || 16;\n    const cols = getCanvasCols();\n    const gap = 8;\n    const contentW = rect.width - padL - padR;\n    const colW = (contentW - (cols - 1) * gap) / cols;\n    const rowStep = 100 + gap;\n    const offset = x - rect.left - padL;\n    const col = Math.max(1, Math.min(cols, Math.floor(offset / (colW + gap)) + 1));\n    const row = Math.max(1, Math.floor((y - rect.top - padT) / rowStep) + 1);\n    return { col, row };\n}\n\nfunction findNextPosition(savedCards, colSpan) {\n    const cols = getCanvasCols();\n    const occupied = new Set();\n    for (const c of savedCards) {\n        const cs = c.col || 1;\n        const rs = c.row || 1;\n        const sp = c.colSpan || 3;\n        const sr = c.rowSpan || 1;\n        for (let r = rs; r < rs + sr; r++) {\n            for (let c2 = cs; c2 < cs + sp; c2++) {\n                occupied.add(`${c2},${r}`);\n            }\n        }\n    }\n    for (let row = 1; row <= 20; row++) {\n        for (let col = 1; col <= cols - colSpan + 1; col++) {\n            let fits = true;\n            for (let c2 = col; c2 < col + colSpan && fits; c2++) {\n                if (occupied.has(`${c2},${row}`)) fits = false;\n            }\n            if (fits) return { col, row };\n        }\n    }\n    return { col: 1, row: 1 };\n}\n\nfunction _computeGridCache() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return null;\n    const style = getComputedStyle(canvas);\n    const padL = parseFloat(style.paddingLeft) || 16;\n    const padT = parseFloat(style.paddingTop) || 16;\n    const padR = parseFloat(style.paddingRight) || 16;\n    const contentW = canvas.offsetWidth - padL - padR;\n    const cols = parseInt(style.gridTemplateColumns?.split(\' \')?.length || 12);\n    const gap = parseFloat(style.gap) || 8;\n    const colW = (contentW - (cols - 1) * gap) / cols;\n    const rowH = 100;\n    return { cols, padL, padT, padR, gap, colW, rowH };\n}\n\nfunction onCardMouseDown(e) {\n    if (e.target.closest(\'button\') || e.target.closest(\'input\') || e.target.closest(\'.card-resize-handle\')) return;\n    if (e.button !== 0) return;\n    const cardEl = e.target.closest(\'[data-card-id]\');\n    if (!cardEl || cardEl.closest(\'[data-group-id]\')) return;\n    e.preventDefault();\n\n    const cardId = cardEl.dataset.cardId;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    if (card.lockSize) return;\n\n    const rect = cardEl.getBoundingClientRect();\n    const offsetX = e.clientX - rect.left;\n    const offsetY = e.clientY - rect.top;\n\n    const gridColMatch = cardEl.style.gridColumn?.match(/(\\d+)\\s*\\/\\s*span\\s+(\\d+)/);\n    const gridRowMatch = cardEl.style.gridRow?.match(/(\\d+)\\s*\\/\\s*span\\s+(\\d+)/);\n    const domColSpan = gridColMatch ? parseInt(gridColMatch[2]) : (card.colSpan || 3);\n    const domRowSpan = gridRowMatch ? parseInt(gridRowMatch[2]) : (card.rowSpan || 1);\n    const domCol = gridColMatch ? parseInt(gridColMatch[1]) : (card.col || 1);\n    const domRow = gridRowMatch ? parseInt(gridRowMatch[1]) : (card.row || 1);\n\n    cardDrag.mouseDown = {\n        cardId, cardEl, card,\n        startX: e.clientX, startY: e.clientY,\n        offsetX, offsetY, dragging: false,\n        colSpan: domColSpan,\n        rowSpan: domRowSpan,\n        cardCol: domCol,\n        cardRow: domRow\n    };\n    cardDrag.gridCache = _computeGridCache();\n\n    console.log(`[DOWN] card=${cardId} pos(col=${card.col},row=${card.row}) span(col=${card.colSpan||3},row=${card.rowSpan||1}) offset(X=${Math.round(offsetX)},Y=${Math.round(offsetY)}) cardRect(left=${Math.round(rect.left)},top=${Math.round(rect.top)},w=${Math.round(rect.width)},h=${Math.round(rect.height)})`);\n\n    document.addEventListener(\'mousemove\', onCardMouseMove);\n    document.addEventListener(\'mouseup\', onCardMouseUp);\n}\n\nfunction onCardMouseMove(e) {\n    if (!cardDrag.mouseDown) return;\n    const dx = Math.abs(e.clientX - cardDrag.mouseDown.startX);\n    const dy = Math.abs(e.clientY - cardDrag.mouseDown.startY);\n    if (!cardDrag.mouseDown.dragging && (dx < 4 && dy < 4)) return;\n\n    if (!cardDrag.mouseDown.dragging) {\n        cardDrag.mouseDown.dragging = true;\n        cardDrag.mouseDown.cardEl.classList.add(\'opacity-40\');\n        cardDrag.occurred = true;\n\n        const canvas = document.getElementById(\'dashboard-canvas\');\n        const cs = getComputedStyle(canvas);\n        const padL = parseFloat(cs.paddingLeft) || 16;\n        const padT = parseFloat(cs.paddingTop) || 16;\n        const padR = parseFloat(cs.paddingRight) || 16;\n        const contentW = canvas.offsetWidth - padL - padR;\n        const cols = getCanvasCols();\n        const gap = 8;\n        const colW = (contentW - (cols - 1) * gap) / cols;\n        const cardW = cardDrag.mouseDown.cardEl.offsetWidth;\n        const rowH = 100;\n        const rowStep = rowH + gap;\n        cardDrag.mouseDown.gridSnapshot = {\n            padL, padT, cardW, cardElH: cardDrag.mouseDown.cardEl.offsetHeight, cols, gap, colW, rowH, rowStep,\n            canvasLeft: canvas.getBoundingClientRect().left,\n            canvasTop: canvas.getBoundingClientRect().top\n        };\n\n        cardDrag.dragClone = cardDrag.mouseDown.cardEl.cloneNode(true);\n        cardDrag.dragClone.classList.remove(\'opacity-40\');\n        cardDrag.dragClone.style.cssText = `\n            position:fixed;z-index:10000;pointer-events:none;\n            width:${cardDrag.mouseDown.cardEl.offsetWidth}px;\n            height:${cardDrag.mouseDown.cardEl.offsetHeight}px;\n            opacity:0.85;\n            box-shadow:0 8px 32px rgba(0,0,0,0.4);\n            transition:none;\n            overflow:hidden;\n        `;\n        document.body.appendChild(cardDrag.dragClone);\n    }\n\n    const cloneW = cardDrag.mouseDown.cardEl.offsetWidth;\n    const cloneH = cardDrag.mouseDown.cardEl.offsetHeight;\n    cardDrag.dragClone.style.left = (e.clientX - cardDrag.mouseDown.offsetX) + \'px\';\n    cardDrag.dragClone.style.top = (e.clientY - cardDrag.mouseDown.offsetY) + \'px\';\n\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    const card = cardDrag.mouseDown.card;\n    const colSpan = cardDrag.mouseDown.colSpan;\n    const rowSpan = cardDrag.mouseDown.rowSpan;\n    const cols = getCanvasCols();\n    const snap = cardDrag.mouseDown.gridSnapshot;\n\n    const cardCol = cardDrag.mouseDown.cardCol;\n    const cardRow = cardDrag.mouseDown.cardRow;\n\n    const cardLeft = snap.canvasLeft + snap.padL + (cardCol - 1) * (snap.colW + snap.gap);\n    const cardTop = snap.canvasTop + snap.padT + (cardRow - 1) * snap.rowStep;\n    const cardWidth = snap.cardW || (colSpan * snap.colW + (colSpan - 1) * snap.gap);\n    const cardHeight = snap.cardElH || (rowSpan * snap.rowStep - snap.gap);\n    const cardCenterX = cardLeft + cardWidth / 2;\n    const cardCenterY = cardTop + cardHeight / 2;\n    const halfW = cardWidth / 2;\n    const halfH = cardHeight / 2;\n\n    const relX = e.clientX - cardCenterX;\n    const relY = e.clientY - cardCenterY;\n\n    let newCol, newRow;\n    if (Math.abs(relX) <= halfW) {\n        newCol = cardCol;\n    } else {\n        const offset = e.clientX - snap.canvasLeft - snap.padL;\n        newCol = Math.max(1, Math.min(cols - colSpan + 1, Math.floor(offset / (snap.colW + snap.gap)) + 1));\n    }\n    if (Math.abs(relY) <= halfH) {\n        newRow = cardRow;\n    } else {\n        const offset = e.clientY - snap.canvasTop - snap.padT;\n        newRow = Math.max(1, Math.floor(offset / snap.rowStep) + 1);\n    }\n    const occupied = isCellOccupied(newCol, newRow, colSpan, rowSpan, card.id);\n\n    if (!cardDrag.dropPreview) {\n        cardDrag.dropPreview = document.createElement(\'div\');\n        cardDrag.dropPreview.style.cssText = \'position:fixed;pointer-events:none;z-index:9999;border:2px dashed #06b6d4;border-radius:12px;transition:none;background:rgba(6,182,212,0.08);\';\n        document.body.appendChild(cardDrag.dropPreview);\n    }\n\n    cardDrag.dropPreview.style.left = (snap.canvasLeft + snap.padL + (newCol - 1) * (snap.colW + snap.gap)) + \'px\';\n    cardDrag.dropPreview.style.top = (snap.canvasTop + snap.padT + (newRow - 1) * snap.rowStep) + \'px\';\n    cardDrag.dropPreview.style.width = (colSpan * snap.colW + (colSpan - 1) * snap.gap) + \'px\';\n    cardDrag.dropPreview.style.height = (rowSpan * snap.rowStep - snap.gap) + \'px\';\n    cardDrag.dropPreview.style.borderColor = occupied ? \'#ef4444\' : \'#06b6d4\';\n    cardDrag.dropPreview.style.background = occupied ? \'rgba(239,68,68,0.08)\' : \'rgba(6,182,212,0.08)\';\n    cardDrag.dropPreview.style.display = \'block\';\n\n    cardDrag.dropTarget = { col: newCol, row: newRow, occupied };\n\n    console.log(`[MOVE] card=${card.id} stored(col=${cardCol},row=${cardRow}) span(${colSpan}x${rowSpan}) relX=${Math.round(relX)},relY=${Math.round(relY)} halfW=${Math.round(halfW)},halfH=${Math.round(halfH)} → new(col=${newCol},row=${newRow}) occ=${occupied}`);\n\n    const groupEl = document.elementFromPoint(e.clientX, e.clientY)?.closest(\'[data-group-id]\');\n    document.querySelectorAll(\'[data-group-id].drag-hover\').forEach(el => el.classList.remove(\'drag-hover\'));\n    if (groupEl && !groupEl.contains(cardDrag.mouseDown.cardEl)) {\n        groupEl.classList.add(\'drag-hover\');\n        groupEl.style.borderColor = \'#a855f7\';\n        groupEl.style.background = \'rgba(168,85,247,0.1)\';\n    }\n}\n\nfunction onCardMouseUp(e) {\n    document.removeEventListener(\'mousemove\', onCardMouseMove);\n    document.removeEventListener(\'mouseup\', onCardMouseUp);\n\n    if (cardDrag.dragClone) {\n        cardDrag.dragClone.remove();\n        cardDrag.dragClone = null;\n    }\n    if (cardDrag.dropPreview) {\n        cardDrag.dropPreview.style.display = \'none\';\n    }\n\n    document.querySelectorAll(\'[data-group-id].drag-hover\').forEach(el => {\n        el.classList.remove(\'drag-hover\');\n        el.style.borderColor = \'\';\n        el.style.background = \'\';\n    });\n\n    if (!cardDrag.mouseDown) return;\n\n    const { cardEl, card, dragging } = cardDrag.mouseDown;\n    const totalDx = Math.abs(e.clientX - cardDrag.mouseDown.startX);\n    const totalDy = Math.abs(e.clientY - cardDrag.mouseDown.startY);\n    if (totalDx > 2 || totalDy > 2) cardDrag.occurred = true;\n    cardEl.classList.remove(\'opacity-40\');\n\n    if (dragging && cardDrag.dropTarget) {\n        const groupEl = document.elementFromPoint(e.clientX, e.clientY)?.closest(\'[data-group-id]\');\n        if (groupEl && !groupEl.contains(cardEl)) {\n            const groupCards = groupEl.querySelector(\'.group-cards\');\n            if (groupCards) {\n                const saved = getPickerCards();\n                const cardData = saved.find(c => c.id === card.id);\n                if (cardData) {\n                    cardData.groupId = groupEl.dataset.groupId;\n                    setPickerCards(saved);\n                }\n                groupCards.appendChild(cardEl);\n                cardEl.classList.remove(\'cursor-grab\');\n                cardEl.classList.add(\'cursor-default\');\n            }\n        } else {\n            const saved = getPickerCards();\n            const cardData = saved.find(c => c.id === card.id);\n                if (cardData) {\n                    const oldCol = cardData.col, oldRow = cardData.row;\n                    let newCol = cardDrag.dropTarget.col;\n                    let newRow = cardDrag.dropTarget.row;\n                    const colSp = cardData.colSpan || 3;\n                    const rowSp = cardData.rowSpan || 1;\n                    const cols = getCanvasCols();\n                    if (newCol + colSp - 1 > cols) newCol = cols - colSp + 1;\n                    cardData._isDrag = true;\n                    cardData.col = newCol;\n                    cardData.row = newRow;\n                    resolveOverlaps(saved, card.id);\n                console.log(`[DROP] card=${card.id} from(col=${oldCol},row=${oldRow}) target(col=${newCol},row=${newRow})`);\n                for (const c of saved) {\n                    const el2 = document.querySelector(`[data-card-id="${c.id}"]`);\n                    if (el2) {\n                        el2.style.gridColumn = `${c.col} / span ${c.colSpan || 3}`;\n                        el2.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;\n                    }\n                }\n                setPickerCards(saved);\n                updateCanvasMinHeight();\n            }\n        }\n    }\n\n    cardDrag.mouseDown = null;\n    cardDrag.dropTarget = null;\n    cardDrag.gridCache = null;\n    setTimeout(() => { cardDrag.occurred = false; }, 200);\n}\n\nfunction isCellOccupied(col, row, colSpan, rowSpan, excludeCardId) {\n    const saved = getPickerCards();\n    for (const c of saved) {\n        if (c.id === excludeCardId || !c.col || !c.row) continue;\n        const cs = c.col, rs = c.row;\n        const ce = cs + (c.colSpan || 3) - 1;\n        const re = rs + (c.rowSpan || 1) - 1;\n        const ne = col + colSpan - 1;\n        const nr = row + rowSpan - 1;\n        if (col <= ce && ne >= cs && row <= re && nr >= rs) return true;\n    }\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (canvas) {\n        const g = cardDrag.gridCache || _computeGridCache();\n        if (g) {\n            const ne = col + colSpan - 1;\n            const nr = row + rowSpan - 1;\n            for (const gEl of canvas.querySelectorAll(\'[data-group-id]\')) {\n                const rect = gEl.getBoundingClientRect();\n                const cRect = canvas.getBoundingClientRect();\n                const gColStart = Math.max(1, Math.round((rect.left - cRect.left - g.padL) / (g.colW + g.gap)) + 1);\n                const gColEnd = Math.max(gColStart, Math.round((rect.right - cRect.left - g.padL) / (g.colW + g.gap)));\n                const gRowStart = Math.max(1, Math.round((rect.top - cRect.top - g.padT) / (g.rowH + g.gap)) + 1);\n                const gRowEnd = Math.max(gRowStart, Math.round((rect.bottom - cRect.top - g.padT) / (g.rowH + g.gap)));\n                if (col <= gColEnd && ne >= gColStart && row <= gRowEnd && nr >= gRowStart) return true;\n            }\n        }\n    }\n    return false;\n}\n\nfunction resolveOverlaps(saved, cardId) {\n    const cols = getCanvasCols();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    delete card._isDrag;\n\n    function overlaps(a, b) {\n        if (!a.col || !a.row || !b.col || !b.row) return false;\n        const aCe = a.col + (a.colSpan || 3) - 1, aRe = a.row + (a.rowSpan || 1) - 1;\n        const bCe = b.col + (b.colSpan || 3) - 1, bRe = b.row + (b.rowSpan || 1) - 1;\n        return a.col <= bCe && aCe >= b.col && a.row <= bRe && aRe >= b.row;\n    }\n\n    function pushRight(anchor, target) {\n        const anchorCe = anchor.col + (anchor.colSpan || 3) - 1;\n        target.col = anchorCe + 1;\n    }\n\n    const affected = new Set([cardId]);\n    let iter = 0;\n    let changed = true;\n    while (changed && iter < 50) {\n        changed = false;\n        iter++;\n        for (const c of saved) {\n            if (!c.col || !c.row || affected.has(c.id)) continue;\n            for (const aId of affected) {\n                const a = saved.find(x => x.id === aId);\n                if (a && overlaps(a, c)) {\n                    pushRight(a, c);\n                    affected.add(c.id);\n                    changed = true;\n                    break;\n                }\n            }\n        }\n    }\n}\n\nfunction findFreePosition(savedCards, colSpan, rowSpan, excludeCardId) {\n    const cols = getCanvasCols();\n    if (colSpan > cols) colSpan = cols;\n    const occupied = new Set();\n    for (const c of savedCards) {\n        if (c.id === excludeCardId || !c.col || !c.row) continue;\n        const cs = c.col, rs = c.row;\n        const sp = c.colSpan || 3, sr = c.rowSpan || 1;\n        for (let r = rs; r < rs + sr; r++) {\n            for (let c2 = cs; c2 < cs + sp; c2++) {\n                occupied.add(`${c2},${r}`);\n            }\n        }\n    }\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (canvas) {\n        const cs2 = getComputedStyle(canvas);\n        const padL = parseFloat(cs2.paddingLeft) || 16;\n        const padT = parseFloat(cs2.paddingTop) || 16;\n        const padR = parseFloat(cs2.paddingRight) || 16;\n        const contentW = canvas.offsetWidth - padL - padR;\n        const gap = 8;\n        const colW = (contentW - (cols - 1) * gap) / cols;\n        const rowH = 100;\n        for (const gEl of canvas.querySelectorAll(\'[data-group-id]\')) {\n            const rect = gEl.getBoundingClientRect();\n            const cRect = canvas.getBoundingClientRect();\n            const gColStart = Math.max(1, Math.round((rect.left - cRect.left - padL) / (colW + gap)) + 1);\n            const gColEnd = Math.max(gColStart, Math.round((rect.right - cRect.left - padL) / (colW + gap)));\n            const gRowStart = Math.max(1, Math.round((rect.top - cRect.top - padT) / (rowH + gap)) + 1);\n            const gRowEnd = Math.max(gRowStart, Math.round((rect.bottom - cRect.top - padT) / (rowH + gap)));\n            for (let r = gRowStart; r <= gRowEnd; r++) {\n                for (let c2 = gColStart; c2 <= gColEnd; c2++) {\n                    occupied.add(`${c2},${r}`);\n                }\n            }\n        }\n    }\n    for (let row = 1; row <= 50; row++) {\n        for (let col = 1; col <= cols - colSpan + 1; col++) {\n            let fits = true;\n            for (let r = row; r < row + rowSpan && fits; r++) {\n                for (let c = col; c < col + colSpan && fits; c++) {\n                    if (occupied.has(`${c},${r}`)) fits = false;\n                }\n            }\n            if (fits) return { col, row };\n        }\n    }\n    return { col: 1, row: 1 };\n}\n\nfunction getDragAfterElement(container, x, y) {\n    const cards = [...container.querySelectorAll(\'[data-card-id]:not(.opacity-40), [data-group-id]:not(.opacity-40)\')];\n    let closest = null;\n    let closestDist = Infinity;\n    for (const child of cards) {\n        const box = child.getBoundingClientRect();\n        const cx = box.left + box.width / 2;\n        const cy = box.top + box.height / 2;\n        const dist = Math.hypot(x - cx, y - cy);\n        if (dist < closestDist) {\n            closestDist = dist;\n            closest = child;\n        }\n    }\n    if (!closest) return null;\n    const box = closest.getBoundingClientRect();\n    const isAfter = x > box.left + box.width / 2 || y > box.top + box.height / 2;\n    return isAfter ? closest.nextElementSibling : closest;\n}\nfunction saveCardOrder() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    const ordered = [...canvas.querySelectorAll(\'[data-card-id]\')].map(el => el.dataset.cardId);\n    const saved = getPickerCards();\n    const orderedCards = ordered.map(id => saved.find(c => c.id === id)).filter(Boolean);\n    setPickerCards(orderedCards);\n}\n\nfunction removePickerCard(cardId) {\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (el) el.remove();\n    const saved = getPickerCards().filter(c => c.id !== cardId);\n    setPickerCards(saved);\n    if (!saved.length) document.getElementById(\'dashboard-empty\')?.classList.remove(\'hidden\');\n    updateCanvasMinHeight();\n}\n\nfunction showCardEdit(cardId) {\n    cardEdit.editingCardId = cardId;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    const modal = document.getElementById(\'card-edit-modal\');\n    const labelInput = document.getElementById(\'card-edit-label\');\n\n    labelInput.value = card.label || \'\';\n\n    modal.classList.remove(\'hidden\');\n    labelInput.focus();\n}\n\nfunction hideCardEdit() {\n    const modal = document.getElementById(\'card-edit-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n    cardEdit.editingCardId = null;\n}\n\nfunction saveCardEdit() {\n    if (!cardEdit.editingCardId) return;\n\n    const label = document.getElementById(\'card-edit-label\').value.trim();\n    if (!label) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardEdit.editingCardId);\n    if (!card) return;\n\n    card.label = label;\n    setPickerCards(saved);\n\n    const cardEl = document.querySelector(`[data-card-id="${cardEdit.editingCardId}"]`);\n    if (cardEl) {\n        const labelEl = cardEl.querySelector(\'.text-sm.text-gray-300\');\n        if (labelEl) labelEl.textContent = label;\n    }\n\n    hideCardEdit();\n}\n\nfunction showCardConfig(cardId) {\n    cardEdit.configuringCardId = cardId;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card || card.type !== \'fan\') return;\n\n    const modal = document.getElementById(\'card-config-modal\');\n    const container = document.getElementById(\'card-config-options\');\n\n    const fanData = getFanData(card.source, card.sourceId);\n    if (!fanData) return;\n\n    const options = [\n        { key: \'rpm\', label: \'RPM\', checked: card.showRpm !== false },\n        { key: \'mode\', label: \'Mode\', checked: card.showMode === true },\n        { key: \'sensors\', label: \'Sensors\', checked: card.showSensors === true },\n        { key: \'target\', label: \'Target Temp\', checked: card.showTarget === true },\n    ];\n\n    container.innerHTML = options.map(opt => `\n        <label class="flex items-center gap-3 p-2 rounded hover:bg-cyber-accent cursor-pointer">\n            <input type="checkbox" data-option="${opt.key}" ${opt.checked ? \'checked\' : \'\'}\n                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan">\n            <span class="text-sm text-gray-300">${opt.label}</span>\n        </label>\n    `).join(\'\');\n\n    container.querySelectorAll(\'input[type="checkbox"]\').forEach(cb => {\n        cb.addEventListener(\'change\', () => toggleCardOption(cardId, cb.dataset.option, cb.checked));\n    });\n\n    modal.classList.remove(\'hidden\');\n}\n\nfunction hideCardConfig() {\n    const modal = document.getElementById(\'card-config-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n    cardEdit.configuringCardId = null;\n}\n\nasync function fetchDiskSmart(diskId, forceRefresh = false, source = \'local\', nodeId = null) {\n    try {\n        let url;\n        if (source === \'local\') {\n            url = forceRefresh\n                ? `/api/disks/${diskId}/smart?refresh=1`\n                : `/api/disks/${diskId}/smart`;\n        } else {\n            url = forceRefresh\n                ? `/api/nodes/${source}/disks/${diskId}/smart?refresh=1`\n                : `/api/nodes/${source}/disks/${diskId}/smart`;\n        }\n        const resp = await fetch(url);\n        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);\n        return await resp.json();\n    } catch (e) {\n        console.error(\'SMART fetch error:\', e);\n        return null;\n    }\n}\n\nfunction showSmartModal(cardId) {\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    smart.modalCardId = cardId;\n    smart.modalDiskId = card.sourceId;\n    smart.modalSource = card.source || \'local\';\n\n    let disk;\n    if (smart.modalSource === \'local\') {\n        disk = store.state?.hdd_sensors?.[card.sourceId];\n    } else {\n        const node = store.nodesData.find(n => n.node_id === smart.modalSource);\n        disk = node?.telemetry?.hdd_sensors?.[card.sourceId];\n    }\n\n    const title = document.getElementById(\'smart-modal-title\');\n    if (title && disk) {\n        title.textContent = `SMART — ${disk.label || disk.dev_name || card.sourceId}`;\n    } else if (title) {\n        title.textContent = `SMART — ${card.sourceId}`;\n    }\n    document.getElementById(\'smart-modal\')?.classList.remove(\'hidden\');\n    refreshSmartData();\n}\n\nfunction hideSmartModal() {\n    document.getElementById(\'smart-modal\')?.classList.add(\'hidden\');\n    smart.modalCardId = null;\n    smart.modalDiskId = null;\n    smart.modalSource = \'local\';\n}\n\nasync function refreshSmartData() {\n    if (!smart.modalDiskId) return;\n    const container = document.getElementById(\'smart-attributes-container\');\n    if (!container) return;\n\n    container.innerHTML = `<div class="text-center text-gray-400 py-4">${t(\'smart.loading\', \'Loading...\')}</div>`;\n\n    const data = await fetchDiskSmart(smart.modalDiskId, true, smart.modalSource);\n    if (!data || data.error) {\n        container.innerHTML = `<div class="text-center text-red-400 py-4">${data?.error || t(\'smart.load_error\', \'SMART data load error\')}</div>`;\n        return;\n    }\n\n    smart.cache[`${smart.modalSource}:${smart.modalDiskId}`] = data;\n\n    const infoEl = document.getElementById(\'smart-device-info\');\n    if (infoEl && data.device_info) {\n        const info = data.device_info;\n        infoEl.textContent = [info.model, info.serial, info.firmware, info.capacity].filter(Boolean).join(\' | \');\n    }\n\n    smart.attrType = data.attr_type || \'sata\';\n    smart.attributes = data.attributes || [];\n\n    renderSmartAttributes();\n}\n\nfunction renderSmartAttributes() {\n    const container = document.getElementById(\'smart-attributes-container\');\n    if (!container) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === smart.modalCardId);\n    const selectedIds = card?.smartAttributes || [];\n\n    if (smart.attrType === \'nvme\') {\n        renderNvmeAttributes(container, selectedIds);\n    } else {\n        renderSataAttributes(container, selectedIds);\n    }\n}\n\nfunction renderSataAttributes(container, selectedIds) {\n    if (!smart.attributes.length) {\n        container.innerHTML = `<div class="text-center text-gray-400 py-4">${t(\'smart.no_attributes\', \'No SMART attributes\')}</div>`;\n        return;\n    }\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === smart.modalCardId);\n    const smartUnits = card?.smartUnits || {};\n\n    container.innerHTML = smart.attributes.map(attr => {\n        // Unified color: use status (SATA) or criticality (NVMe fallback)\n        const severity = attr.status || attr.criticality || \'ok\';\n        const statusColor = severity === \'critical\' ? \'text-red-400\' :\n                           severity === \'warning\' || severity === \'important\' ? \'text-yellow-400\' : \'text-neon-green\';\n        const statusBg = severity === \'critical\' ? \'bg-red-500/10\' :\n                        severity === \'warning\' || severity === \'important\' ? \'bg-yellow-500/10\' : \'bg-green-500/10\';\n        const critBadge = attr.criticality === \'critical\' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">${t(\'smart.critical\', \'CRITICAL\')}</span>` :\n                         attr.criticality === \'important\' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">${t(\'smart.important\', \'IMPORTANT\')}</span>` : \'\';\n        const checked = selectedIds.includes(String(attr.id)) ? \'checked\' : \'\';\n\n        let unitHtml = \'\';\n        if (attr.unit === \'bytes\') {\n            const currentUnit = smartUnits[attr.id] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${attr.id}" onchange="onSmartUnitChange(${attr.id}, this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Raw</option>\n                    <option value="bytes" ${currentUnit === \'bytes\' ? \'selected\' : \'\'}>Байты</option>\n                    <option value="kb" ${currentUnit === \'kb\' ? \'selected\' : \'\'}>КБ</option>\n                    <option value="mb" ${currentUnit === \'mb\' ? \'selected\' : \'\'}>МБ</option>\n                    <option value="gb" ${currentUnit === \'gb\' ? \'selected\' : \'\'}>ГБ</option>\n                    <option value="tb" ${currentUnit === \'tb\' ? \'selected\' : \'\'}>ТБ</option>\n                </select>`;\n        } else if (attr.unit === \'hours\') {\n            const currentUnit = smartUnits[attr.id] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${attr.id}" onchange="onSmartUnitChange(${attr.id}, this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Часы</option>\n                    <option value="days" ${currentUnit === \'days\' ? \'selected\' : \'\'}>Дни</option>\n                    <option value="months" ${currentUnit === \'months\' ? \'selected\' : \'\'}>Месяцы</option>\n                </select>`;\n        }\n\n        let displayValue = attr.raw;\n        if (attr.unit === \'bytes\' && attr.unit_divisor) {\n            const unit = smartUnits[attr.id] || \'raw\';\n            if (unit !== \'raw\') {\n                displayValue = formatBytes(parseInt(attr.raw_num || attr.raw) * attr.unit_divisor, unit);\n            }\n        } else if (attr.unit === \'hours\') {\n            const unit = smartUnits[attr.id] || \'raw\';\n            if (unit === \'days\') {\n                displayValue = (parseInt(attr.raw || \'0\') / 24).toFixed(1) + t(\'smart.unit.days_short\', \' дн\');\n            } else if (unit === \'months\') {\n                displayValue = (parseInt(attr.raw || \'0\') / 720).toFixed(1) + t(\'smart.unit.months_short\', \' мес\');\n            }\n        }\n\n        return `\n        <div class="flex items-center gap-3 p-2 rounded ${statusBg} hover:bg-white/5 transition-colors group"\n             title="${escapeHtml(attr.tooltip)}">\n            <input type="checkbox" data-smart-id="${attr.id}" ${checked}\n                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan shrink-0">\n            <div class="flex-1 min-w-0">\n                <div class="flex items-center">\n                    <span class="text-xs text-gray-500 w-8">${attr.id}</span>\n                    <span class="text-sm text-gray-200 truncate">${escapeHtml(attr.description)}</span>\n                    ${critBadge}\n                    ${unitHtml}\n                </div>\n                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>\n            </div>\n            <div class="text-right shrink-0">\n                <div class="text-sm font-mono ${statusColor}">${attr.value}</div>\n                <div class="text-[10px] text-gray-500">worst:${attr.worst} thr:${attr.threshold}</div>\n            </div>\n            <div class="text-right shrink-0 w-20">\n                <div class="text-xs text-gray-400 font-mono">${displayValue}</div>\n            </div>\n        </div>`;\n    }).join(\'\');\n}\n\nfunction onSmartUnitChange(attrId, unit) {\n    if (!smart.modalCardId) return;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === smart.modalCardId);\n    if (!card) return;\n\n    if (!card.smartUnits) card.smartUnits = {};\n    card.smartUnits[attrId] = unit;\n    setPickerCards(saved);\n    renderSmartAttributes();\n}\n\nfunction renderNvmeAttributes(container, selectedIds) {\n    const attrs = smart.attributes;\n    if (!Object.keys(attrs).length) {\n        container.innerHTML = `<div class="text-center text-gray-400 py-4">${t(\'smart.no_nvme_attributes\', \'No NVMe attributes\')}</div>`;\n        return;\n    }\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === smart.modalCardId);\n    const smartUnits = card?.smartUnits || {};\n\n    container.innerHTML = Object.entries(attrs).map(([key, attr]) => {\n        const severity = attr.status || attr.criticality || \'info\';\n        const statusColor = severity === \'critical\' ? \'text-red-400\' :\n                           severity === \'warning\' || severity === \'important\' ? \'text-yellow-400\' : \'text-neon-green\';\n        const critBadge = attr.criticality === \'critical\' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">${t(\'smart.critical\', \'CRITICAL\')}</span>` :\n                         attr.criticality === \'important\' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">${t(\'smart.important\', \'IMPORTANT\')}</span>` : \'\';\n        const checked = selectedIds.includes(key) ? \'checked\' : \'\';\n\n        let unitHtml = \'\';\n        let displayValue = attr.value;\n\n        if (attr.unit === \'nvme_blocks\') {\n            const currentUnit = smartUnits[key] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${key}" onchange="onSmartUnitChange(\'${key}\', this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Raw</option>\n                    <option value="bytes" ${currentUnit === \'bytes\' ? \'selected\' : \'\'}>Байты</option>\n                    <option value="kb" ${currentUnit === \'kb\' ? \'selected\' : \'\'}>КБ</option>\n                    <option value="mb" ${currentUnit === \'mb\' ? \'selected\' : \'\'}>МБ</option>\n                    <option value="gb" ${currentUnit === \'gb\' ? \'selected\' : \'\'}>ГБ</option>\n                    <option value="tb" ${currentUnit === \'tb\' ? \'selected\' : \'\'}>ТБ</option>\n                </select>`;\n            if (currentUnit !== \'raw\' && attr.unit_divisor) {\n                displayValue = formatBytes(attr.value * attr.unit_divisor, currentUnit);\n            }\n        } else if (attr.unit === \'hours\') {\n            const currentUnit = smartUnits[key] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${key}" onchange="onSmartUnitChange(\'${key}\', this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Часы</option>\n                    <option value="days" ${currentUnit === \'days\' ? \'selected\' : \'\'}>Дни</option>\n                    <option value="months" ${currentUnit === \'months\' ? \'selected\' : \'\'}>Месяцы</option>\n                </select>`;\n            if (currentUnit === \'days\') {\n                displayValue = (parseInt(attr.value || \'0\') / 24).toFixed(1);\n            } else if (currentUnit === \'months\') {\n                displayValue = (parseInt(attr.value || \'0\') / 720).toFixed(1);\n            }\n        }\n\n        let suffix = \'\';\n        if (key === \'temperature\') suffix = \'°C\';\n        else if (key === \'percentage_used\' || key === \'available_spare\' || key === \'available_spare_threshold\') suffix = \'%\';\n        else if (key === \'controller_busy_time\' || key === \'warning_temp_time\' || key === \'critical_comp_time\') suffix = \' мин\';\n        else if (attr.unit === \'hours\' && (smartUnits[key] || \'raw\') === \'days\') suffix = t(\'smart.unit.days_short\', \' дн\');\n        else if (attr.unit === \'hours\' && (smartUnits[key] || \'raw\') === \'months\') suffix = t(\'smart.unit.months_short\', \' мес\');\n\n        return `\n        <div class="flex items-center gap-3 p-2 rounded bg-green-500/5 hover:bg-white/5 transition-colors"\n             title="${escapeHtml(attr.tooltip)}">\n            <input type="checkbox" data-smart-key="${key}" ${checked}\n                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan shrink-0">\n            <div class="flex-1 min-w-0">\n                <div class="flex items-center">\n                    <span class="text-sm text-gray-200 truncate">${escapeHtml(attr.description)}</span>\n                    ${critBadge}\n                    ${unitHtml}\n                </div>\n                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>\n            </div>\n            <div class="text-right shrink-0">\n                <div class="text-sm font-mono ${statusColor}">${displayValue}${suffix}</div>\n            </div>\n        </div>`;\n    }).join(\'\');\n}\n\nfunction saveSmartSelection() {\n    if (!smart.modalCardId) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === smart.modalCardId);\n    if (!card) return;\n\n    const checkboxes = document.querySelectorAll(\'#smart-attributes-container input[type="checkbox"]\');\n    const selected = [];\n    checkboxes.forEach(cb => {\n        if (cb.checked) {\n            selected.push(cb.dataset.smartId || cb.dataset.smartKey);\n        }\n    });\n\n    const unitSelects = document.querySelectorAll(\'#smart-attributes-container select[data-smart-unit]\');\n    const units = {};\n    unitSelects.forEach(sel => {\n        const attrId = sel.dataset.smartUnit;\n        units[attrId] = sel.value;\n    });\n\n    card.smartAttributes = selected;\n    card.smartUnits = units;\n    setPickerCards(saved);\n    updateCardDetails(smart.modalCardId);\n    const cardEl = document.querySelector(`[data-card-id="${smart.modalCardId}"]`);\n    if (cardEl) snapCardToGrid(cardEl);\n    hideSmartModal();\n    saveDashboardToServer();\n}\n\nfunction toggleCardOption(cardId, option, enabled) {\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    if (option === \'rpm\') card.showRpm = enabled;\n    else if (option === \'mode\') card.showMode = enabled;\n    else if (option === \'sensors\') card.showSensors = enabled;\n    else if (option === \'target\') card.showTarget = enabled;\n\n    setPickerCards(saved);\n    updateCardDetails(cardId);\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (el) snapCardToGrid(el);\n}\n\nfunction getFanData(source, sourceId) {\n    if (source === \'local\') return store.state?.fans?.[sourceId] || null;\n    const node = store.nodesData.find(n => n.node_id === source);\n    return node?.telemetry?.fans?.[sourceId] || null;\n}\n\nfunction getSensorLabel(sensorId) {\n    if (sensorId.startsWith(\'hdd:\')) {\n        const id = sensorId.slice(4);\n        return store.state?.hdd_sensors?.[id]?.label || id;\n    } else if (sensorId.startsWith(\'temp:\')) {\n        const id = sensorId.slice(5);\n        return store.state?.temp_sensors?.[id]?.label || id;\n    }\n    return sensorId;\n}\n\nfunction updateCardDetails(cardId) {\n    const cardEl = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (!cardEl) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    const detailsEl = cardEl.querySelector(\'.card-details\');\n    if (!detailsEl) return;\n\n    if (card.type === \'disk\') {\n        updateDiskCardDetails(card, detailsEl);\n        return;\n    }\n    if (card.type !== \'fan\') {\n        return;\n    }\n\n    const fanData = getFanData(card.source, card.sourceId);\n    if (!fanData) return;\n\n    let html = \'\';\n    if (card.showMode) {\n        const mode = fanData.mode || \'manual\';\n        const modeClass = mode === \'auto\' ? \'text-neon-green\' : \'text-neon-cyan\';\n        const modeLabel = mode === \'auto\' ? t(\'mode.auto\', \'AUTO\') : t(\'mode.manual\', \'MANUAL\');\n        html += `<div class="text-xs ${modeClass} mt-1">${modeLabel}</div>`;\n    }\n    if (card.showTarget && fanData.mode === \'auto\') {\n        html += `<div class="text-xs text-gray-500 mt-1">${t(\'inspector.target\', \'Target:\')} ${fanData.target_temp || \'--\'}°C</div>`;\n    }\n    if (card.showSensors && fanData.sensors && fanData.sensors.length > 0) {\n        const sensorLabels = fanData.sensors.map(s => getSensorLabel(s)).join(\', \');\n        html += `<div class="text-xs text-gray-500 mt-1 truncate" title="${escapeHtml(sensorLabels)}">${t(\'inspector.sensors\', \'Sensors:\')} ${escapeHtml(sensorLabels)}</div>`;\n    }\n\n    detailsEl.innerHTML = html;\n}\n\nfunction updateDiskCardDetails(card, detailsEl) {\n    if (!card.smartAttributes?.length) {\n        return;\n    }\n\n    // diskData check removed — SMART attributes render from smart.cache independently\n    // of hdd_sensors state loading. This prevents race condition where live update\n    // skips rendering because hdd_sensors hasn\'t loaded yet.\n\n    let html = \'\';\n    const smartUnits = card.smartUnits || {};\n\n    for (const attrKey of card.smartAttributes) {\n        const attrId = parseInt(attrKey);\n        const cacheKey = `${card.source || \'local\'}:${card.sourceId}`;\n        if (!isNaN(attrId)) {\n            const cachedSmart = smart.cache?.[cacheKey];\n            if (cachedSmart?.attributes) {\n                const attr = cachedSmart.attributes.find(a => a.id === attrId);\n                if (attr) {\n                    const severity = attr.status || attr.criticality || \'ok\';\n                    const color = severity === \'critical\' ? \'text-red-400\' :\n                                 severity === \'warning\' || severity === \'important\' ? \'text-yellow-400\' : \'text-neon-green\';\n                    let displayValue = attr.raw;\n                    if (attr.unit === \'bytes\' && attr.unit_divisor) {\n                        const unit = smartUnits[attr.id] || \'raw\';\n                        if (unit !== \'raw\') {\n                            displayValue = formatBytes(parseInt(attr.raw_num || attr.raw) * attr.unit_divisor, unit) + \' \' + getUnitLabel(unit);\n                        }\n                    } else if (attr.unit === \'hours\') {\n                        const unit = smartUnits[attr.id] || \'raw\';\n                        if (unit === \'days\') {\n                            displayValue = (parseInt(attr.raw || \'0\') / 24).toFixed(1) + t(\'smart.unit.days_short\', \' дн\');\n                        } else if (unit === \'months\') {\n                            displayValue = (parseInt(attr.raw || \'0\') / 720).toFixed(1) + t(\'smart.unit.months_short\', \' мес\');\n                        }\n                    } else if (attr.unit === \'nvme_blocks\') {\n                        const unit = smartUnits[attr.id] || \'raw\';\n                        if (unit !== \'raw\') {\n                            displayValue = formatBytes(attr.value * (attr.unit_divisor || 1), unit) + \' \' + getUnitLabel(unit);\n                        }\n                    }\n                    html += `<div class="text-xs mt-1" title="${escapeHtml(attr.tooltip)}">\n                        <span class="text-gray-500">${escapeHtml(attr.description)}:</span>\n                        <span class="${color} font-mono">${displayValue}</span>\n                    </div>`;\n                }\n            }\n        } else {\n            const cachedSmart = smart.cache?.[`${card.source || \'local\'}:${card.sourceId}`];\n            if (cachedSmart?.attributes?.[attrKey]) {\n                const attr = cachedSmart.attributes[attrKey];\n                const severity = attr.status || attr.criticality || \'info\';\n                const color = severity === \'critical\' ? \'text-red-400\' :\n                             severity === \'warning\' || severity === \'important\' ? \'text-yellow-400\' : \'text-neon-green\';\n                let displayValue = attr.value;\n                let suffix = attrKey === \'temperature\' ? \'°C\' :\n                            attrKey.includes(\'percentage\') || attrKey.includes(\'spare\') ? \'%\' : \'\';\n                if (attr.unit === \'nvme_blocks\' && attr.unit_divisor) {\n                    const unit = smartUnits[attrKey] || \'raw\';\n                    if (unit !== \'raw\') {\n                        displayValue = formatBytes(attr.value * attr.unit_divisor, unit);\n                        suffix = \' \' + getUnitLabel(unit);\n                    }\n                } else if (attr.unit === \'hours\') {\n                    const unit = smartUnits[attrKey] || \'raw\';\n                    if (unit === \'days\') {\n                        displayValue = (parseInt(attr.value || \'0\') / 24).toFixed(1);\n                        suffix = t(\'smart.unit.days_short\', \' дн\');\n                    } else if (unit === \'months\') {\n                        displayValue = (parseInt(attr.value || \'0\') / 720).toFixed(1);\n                        suffix = t(\'smart.unit.months_short\', \' мес\');\n                    }\n                }\n                html += `<div class="text-xs mt-1" title="${escapeHtml(attr.tooltip)}">\n                    <span class="text-gray-500">${escapeHtml(attr.description)}:</span>\n                    <span class="${color} font-mono">${displayValue}${suffix}</span>\n                </div>`;\n            }\n        }\n    }\n\n    if (html) detailsEl.innerHTML = html;\n}\n\nfunction pushSparkline(key, value) {\n    if (!sparklineHistory[key]) sparklineHistory[key] = [];\n    sparklineHistory[key].push(value);\n    if (sparklineHistory[key].length > SPARKLINE_MAX) sparklineHistory[key].shift();\n}\n\nfunction getSparkline(key) {\n    return sparklineHistory[key] || [];\n}\n\nfunction renderSparkline(key, color = \'#22d3ee\', width = 120, height = 30) {\n    const data = getSparkline(key);\n    if (data.length < 2) return \'\';\n    \n    const min = Math.min(...data);\n    const max = Math.max(...data);\n    const range = max - min || 1;\n    \n    const points = data.map((v, i) => {\n        const x = (i / (data.length - 1)) * width;\n        const y = height - ((v - min) / range) * (height - 4) - 2;\n        return `${x},${y}`;\n    }).join(\' \');\n    \n    return `<svg width="${width}" height="${height}" class="mt-2 opacity-60">\n        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>\n    </svg>`;\n}\n\nasync function loadDashboardFromServer() {\n    try {\n        const resp = await fetch(\'/api/dashboard\');\n        if (resp.ok) {\n            const data = await resp.json();\n            dashboard.cards = data.cards || [];\n            dashboard.groups = data.groups || [];\n            dashboard.hiddenSensors = data.hiddenSensors || [];\n            dashboard.loaded = true;\n            return;\n        }\n    } catch (e) { console.warn(\'Dashboard load failed:\', e); }\n    dashboard.cards = [];\n    dashboard.groups = [];\n    dashboard.hiddenSensors = [];\n    dashboard.loaded = true;\n}\n\nfunction getPickerCards() {\n    return dashboard.cards || [];\n}\n\nfunction setPickerCards(cards) {\n    dashboard.cards = cards;\n    scheduleDashboardSave();\n}\n\nfunction getPickerGroups() {\n    return dashboard.groups || [];\n}\n\nfunction setPickerGroups(groups) {\n    dashboard.groups = groups;\n    scheduleDashboardSave();\n}\n\nfunction scheduleDashboardSave() {\n    if (dashboard.saveTimer) clearTimeout(dashboard.saveTimer);\n    dashboard.saveTimer = setTimeout(saveDashboardToServer, 500);\n}\n\nasync function saveDashboardToServer() {\n    try {\n        await fetch(\'/api/dashboard\', {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ cards: dashboard.cards || [], groups: dashboard.groups || [], hiddenSensors: dashboard.hiddenSensors || [] })\n        });\n    } catch (e) { console.warn(\'Dashboard save failed:\', e); }\n}\n\nasync function loadPickerCards() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    await loadDashboardFromServer();\n\n    if (!canvas._groupHandlersAttached) {\n        canvas.addEventListener(\'dragover\', onGroupDragOver);\n        canvas.addEventListener(\'drop\', onGroupDropOutside);\n        canvas._groupHandlersAttached = true;\n    }\n\n    const groups = getPickerGroups();\n    if (groups.length) {\n        groups.forEach(g => {\n            if (!document.querySelector(`[data-group-id="${g.id}"]`)) {\n                renderDashboardGroup(g);\n            }\n        });\n    }\n\n    const cards = getPickerCards();\n    if (!cards.length && !groups.length) return;\n\n    let positionsChanged = false;\n    cards.forEach(c => {\n        if (document.querySelector(`[data-card-id="${c.id}"]`)) return;\n        if (!c.col || !c.row) { positionsChanged = true; }\n        renderPickerCard(c);\n        if (c.groupId) {\n            const groupEl = document.querySelector(`[data-group-id="${c.groupId}"] .group-cards`);\n            const cardEl = document.querySelector(`[data-card-id="${c.id}"]`);\n            if (groupEl && cardEl) {\n                groupEl.appendChild(cardEl);\n                cardEl.classList.remove(\'cursor-grab\');\n                cardEl.classList.add(\'cursor-default\');\n            }\n        }\n    });\n\n    for (const c of cards) {\n        if (c.groupId) continue;\n        const colSp = c.colSpan || 3;\n        const rowSp = c.rowSpan || 1;\n        if (isCellOccupied(c.col, c.row, colSp, rowSp, c.id)) {\n            const free = findFreePosition(cards, colSp, rowSp, c.id);\n            c.col = free.col;\n            c.row = free.row;\n            const el = document.querySelector(`[data-card-id="${c.id}"]`);\n            if (el) {\n                el.style.gridColumn = `${c.col} / span ${colSp}`;\n                el.style.gridRow = `${c.row} / span ${rowSp}`;\n            }\n            positionsChanged = true;\n        }\n    }\n    if (positionsChanged) setPickerCards(cards);\n    document.getElementById(\'dashboard-empty\')?.classList.add(\'hidden\');\n    startPickerLiveUpdate();\n    prefetchSmartForCards();\n    updateCanvasMinHeight();\n}\n\nfunction updateCanvasMinHeight() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    let maxRow = 0;\n    for (const c of getPickerCards()) {\n        if (!c.row) continue;\n        const bottom = c.row + (c.rowSpan || 1) - 1;\n        if (bottom > maxRow) maxRow = bottom;\n    }\n    for (const gEl of canvas.querySelectorAll(\'[data-group-id]\')) {\n        const rect = gEl.getBoundingClientRect();\n        const cRect = canvas.getBoundingClientRect();\n        const cs = getComputedStyle(canvas);\n        const padT = parseFloat(cs.paddingTop) || 16;\n        const rowH = 100;\n        const gap = 8;\n        const gRowEnd = Math.max(1, Math.round((rect.bottom - cRect.top - padT) / (rowH + gap)) + 1);\n        if (gRowEnd > maxRow) maxRow = gRowEnd;\n    }\n    const minRows = Math.max(maxRow + 5, 8);\n    const rowH = 100;\n    const gap = 8;\n    const padY = 32;\n    canvas.style.minHeight = (minRows * (rowH + gap) - gap + padY) + \'px\';\n}\n\nasync function prefetchSmartForCards() {\n    const cards = getPickerCards().filter(c => c.type === \'disk\' && c.smartAttributes?.length);\n    // Fetch all SMART data in parallel for faster initial load\n    // Cache key = source:sourceId to avoid mixing local/remote data for same disk\n    const promises = cards\n        .filter(c => !smart.cache[`${c.source || \'local\'}:${c.sourceId}`])\n        .map(async (card) => {\n            try {\n                const data = await fetchDiskSmart(card.sourceId, false, card.source || \'local\');\n                if (data && !data.error) {\n                    smart.cache[`${card.source || \'local\'}:${card.sourceId}`] = data;\n                    updateCardDetails(card.id);\n                }\n            } catch (e) { console.warn(\'SMART prefetch failed:\', e); }\n        });\n    await Promise.all(promises);\n}\n\nfunction startPickerLiveUpdate() {\n    if (dashboard.liveTimer) return;\n    dashboard.liveTimer = setInterval(() => {\n        document.querySelectorAll(\'[data-fan-id]\').forEach(el => {\n            const src = el.dataset.source;\n            const id = el.dataset.fanId;\n            let fan = null;\n            if (src === \'local\' && store.state?.fans?.[id]) {\n                fan = store.state.fans[id];\n            } else {\n                const node = store.nodesData.find(n => n.node_id === src);\n                fan = node?.telemetry?.fans?.[id];\n            }\n            if (fan) {\n                el.textContent = fan.rpm || 0;\n                pushSparkline(`fan:${src}:${id}`, fan.rpm || 0);\n                const cardEl = el.closest(\'[data-card-id]\');\n                if (cardEl) updateCardDetails(cardEl.dataset.cardId);\n                const dot = cardEl?.querySelector(\'.status-dot\');\n                if (dot) {\n                    const s = fan.status || \'unknown\';\n                    dot.className = \'status-dot \' + (s === \'running\' ? \'green\' : (s === \'failsafe\' || s === \'critical\') ? \'red\' : \'yellow\');\n                }\n                const animEl = document.querySelector(`[data-fan-anim-id="${id}"][data-fan-source="${src}"]`);\n                if (animEl) {\n                    const rpm = fan.rpm || 0;\n                    const dur = rpm > 0 ? Math.max(0.2, 2 - (rpm / 1500)) : 0;\n                    animEl.style.animation = rpm > 0 ? `fan-spin ${dur}s linear infinite` : \'none\';\n                    const fanColor = fan.status === \'running\' ? \'#22d3ee\' : (fan.status === \'failsafe\' || fan.status === \'critical\') ? \'#ef4444\' : \'#facc15\';\n                    animEl.querySelectorAll(\'path, circle\').forEach(p => p.setAttribute(\'fill\', fanColor));\n                }\n            }\n        });\n        document.querySelectorAll(\'[data-temp-id]\').forEach(el => {\n            const src = el.dataset.source;\n            const id = el.dataset.tempId;\n            let val = null;\n            if (src === \'local\' && store.state?.temp_sensors?.[id]) {\n                val = store.state.temp_sensors[id].value;\n            } else {\n                const node = store.nodesData.find(n => n.node_id === src);\n                val = node?.telemetry?.temp_sensors?.[id]?.value;\n            }\n            if (val != null) el.textContent = val;\n            pushSparkline(`temp:${src}:${id}`, val);\n        });\n        document.querySelectorAll(\'[data-disk-id]\').forEach(el => {\n            const id = el.dataset.diskId;\n            const src = el.dataset.source;\n            let temp = null;\n            if (src === \'local\') {\n                temp = store.state?.hdd_sensors?.[id]?.temp;\n            } else {\n                const node = store.nodesData.find(n => n.node_id === src);\n                temp = node?.telemetry?.hdd_sensors?.[id]?.temp;\n            }\n            if (temp != null) {\n                el.textContent = temp || \'--\';\n                pushSparkline(`disk:${src}:${id}`, temp || 0);\n            }\n        });\n        getPickerCards().filter(c => c.type === \'disk\' && c.smartAttributes?.length).forEach(c => {\n            if (smart.cache[`${c.source || \'local\'}:${c.sourceId}`]) {\n                const cardEl = document.querySelector(`[data-card-id="${c.id}"]`);\n                if (cardEl) {\n                    const detailsEl = cardEl.querySelector(\'.card-details\');\n                    if (detailsEl) updateDiskCardDetails(c, detailsEl);\n                }\n            }\n        });\n    }, 2000);\n}\n\nfunction stopPickerLiveUpdate() {\n    if (dashboard.liveTimer) {\n        clearInterval(dashboard.liveTimer);\n        dashboard.liveTimer = null;\n    }\n}\n\nfunction startSystemUpdate() {\n    if (timers.system) return;\n    timers.system = setInterval(async () => {\n        try {\n            const resp = await fetch(\'/api/system\');\n            const data = await resp.json();\n            document.querySelectorAll(\'[data-system-field="uptime"]\').forEach(el => el.textContent = data.uptime || \'--\');\n            document.querySelectorAll(\'[data-system-field="cpu"]\').forEach(el => el.textContent = (data.cpu_load || 0) + \'%\');\n            document.querySelectorAll(\'[data-system-field="mem"]\').forEach(el => el.textContent = (data.mem_percent || 0) + \'%\');\n            document.querySelectorAll(\'[data-system-bar="cpu"]\').forEach(el => el.style.width = (data.cpu_load || 0) + \'%\');\n            document.querySelectorAll(\'[data-system-bar="mem"]\').forEach(el => el.style.width = (data.mem_percent || 0) + \'%\');\n        } catch(e) {}\n    }, 5000);\n}\nfunction stopSystemUpdate() {\n    if (timers.system) { clearInterval(timers.system); timers.system = null; }\n}\n\nfunction showGroupCreator() {\n    const modal = document.getElementById(\'group-creator-modal\');\n    if (modal) modal.classList.remove(\'hidden\');\n}\n\nfunction hideGroupCreator() {\n    const modal = document.getElementById(\'group-creator-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n}\n\nfunction createGroup() {\n    const name = document.getElementById(\'group-name-input\')?.value?.trim();\n    if (!name) return;\n\n    const group = {\n        id: \'group-\' + Date.now(),\n        name: name,\n    };\n\n    const groups = getPickerGroups();\n    groups.push(group);\n    setPickerGroups(groups);\n\n    renderDashboardGroup(group);\n    hideGroupCreator();\n    document.getElementById(\'dashboard-empty\')?.classList.add(\'hidden\');\n}\n\nfunction renderDashboardGroup(group) {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    const el = document.createElement(\'div\');\n    el.className = \'dashboard-group bg-cyber-bg border-2 border-dashed border-gray-700 rounded-xl p-3 transition-colors hover:border-neon-purple/50 relative\';\n    el.setAttribute(\'data-group-id\', group.id);\n    el.setAttribute(\'draggable\', \'true\');\n    el.style.gridColumn = `span ${group.colSpan || getCanvasCols()}`;\n    el.style.display = \'flex\';\n    el.style.flexDirection = \'column\';\n    if (group.minHeight) el.style.minHeight = group.minHeight;\n\n    el.innerHTML = `\n        <div class="flex items-center justify-between mb-2">\n            <div class="flex items-center gap-2">\n                <span class="text-gray-600 text-xs select-none cursor-grab">⠿</span>\n                <span class="text-xs text-gray-400 font-medium cursor-pointer hover:text-white transition-colors" onclick="startGroupRename(\'${group.id}\')">${escapeHtml(group.name)}</span>\n            </div>\n            <button onclick="removePickerGroup(\'${group.id}\')" class="text-gray-600 hover:text-red-400 text-xs transition-colors">×</button>\n        </div>\n        <div class="group-cards flex flex-wrap gap-2 flex-1"></div>\n        <div class="group-resize-handle absolute bottom-0 right-0 w-4 h-4 cursor-ns-resize opacity-30 hover:opacity-80 transition-opacity"></div>`;\n\n    el.addEventListener(\'dragstart\', onGroupDragStart);\n    el.addEventListener(\'dragover\', onGroupCardDragOver);\n    el.addEventListener(\'drop\', onGroupDrop);\n    el.addEventListener(\'dragleave\', onGroupDragLeave);\n    el.addEventListener(\'dragend\', onGroupDragEnd);\n\n    const handle = el.querySelector(\'.group-resize-handle\');\n    handle.addEventListener(\'mousedown\', (e) => startGroupResize(e, group.id));\n\n    canvas.appendChild(el);\n}\n\nfunction removePickerGroup(groupId) {\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (!el) return;\n\n    const cards = el.querySelectorAll(\'[data-card-id]\');\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    cards.forEach(card => {\n        card.classList.remove(\'cursor-grab\');\n        canvas.appendChild(card);\n    });\n\n    el.remove();\n\n    const saved = getPickerGroups().filter(g => g.id !== groupId);\n    setPickerGroups(saved);\n\n    const allCards = getPickerCards();\n    allCards.forEach(c => { if (c.groupId === groupId) delete c.groupId; });\n    setPickerCards(allCards);\n\n    if (!saved.length && !document.querySelector(\'[data-card-id]\')) {\n        document.getElementById(\'dashboard-empty\')?.classList.remove(\'hidden\');\n    }\n    updateCanvasMinHeight();\n}\n\nfunction onGroupCardDragOver(e) {\n    e.preventDefault();\n    e.dataTransfer.dropEffect = \'move\';\n}\n\nfunction onGroupDragLeave(e) {\n    this.classList.remove(\'border-neon-purple\', \'bg-purple-900/10\');\n}\n\nfunction onGroupDropOutside(e) {\n    if (groupDrag.draggedGroup) {\n        groupDrag.draggedGroup.classList.remove(\'opacity-40\');\n        groupDrag.draggedGroup = null;\n    }\n}\n\nfunction onGroupDrop(e) {\n    e.preventDefault();\n    e.stopPropagation();\n    this.classList.remove(\'border-neon-purple\', \'bg-purple-900/10\');\n\n    const cardId = e.dataTransfer.getData(\'text/plain\');\n    const groupId = e.dataTransfer.getData(\'text/group\');\n    if (!cardId && !groupId) return;\n\n    if (cardId) {\n        const cardEl = document.querySelector(`[data-card-id="${cardId}"]`);\n        const groupCards = this.querySelector(\'.group-cards\');\n        if (!cardEl || !groupCards) return;\n\n        const saved = getPickerCards();\n        const cardData = saved.find(c => c.id === cardId);\n        if (cardData) {\n            cardData.groupId = this.dataset.groupId;\n            setPickerCards(saved);\n        }\n\n        groupCards.appendChild(cardEl);\n        cardEl.classList.remove(\'cursor-grab\');\n        cardEl.classList.add(\'cursor-default\');\n    }\n}\n\nfunction startGroupResize(e, groupId) {\n    e.preventDefault();\n    e.stopPropagation();\n    groupDrag.resizingGroupId = groupId;\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (!el) return;\n    groupDrag.resizeStartY = e.clientY;\n    groupDrag.resizeStartH = el.offsetHeight;\n    document.addEventListener(\'mousemove\', onGroupResize);\n    document.addEventListener(\'mouseup\', stopGroupResize);\n}\n\nfunction onGroupResize(e) {\n    if (!groupDrag.resizingGroupId) return;\n    const el = document.querySelector(`[data-group-id="${groupDrag.resizingGroupId}"]`);\n    if (!el) return;\n    const h = Math.max(100, groupDrag.resizeStartH + (e.clientY - groupDrag.resizeStartY));\n    el.style.minHeight = h + \'px\';\n}\n\nfunction stopGroupResize() {\n    if (!groupDrag.resizingGroupId) return;\n    const groups = getPickerGroups();\n    const group = groups.find(g => g.id === groupDrag.resizingGroupId);\n    const el = document.querySelector(`[data-group-id="${groupDrag.resizingGroupId}"]`);\n    if (group && el) {\n        group.minHeight = el.style.minHeight;\n        setPickerGroups(groups);\n    }\n    groupDrag.resizingGroupId = null;\n    document.removeEventListener(\'mousemove\', onGroupResize);\n    document.removeEventListener(\'mouseup\', stopGroupResize);\n}\n\nfunction onGroupDragStart(e) {\n    if (e.target.closest(\'.group-resize-handle\') || e.target.closest(\'button\') || e.target.closest(\'input\')) return;\n    groupDrag.draggedGroup = this;\n    groupDrag.dropTarget = null;\n    this.classList.add(\'opacity-40\');\n    e.dataTransfer.effectAllowed = \'move\';\n    e.dataTransfer.setData(\'text/group\', this.dataset.groupId);\n}\n\nfunction onGroupDragOver(e) {\n    if (!groupDrag.draggedGroup) return;\n    e.preventDefault();\n    e.dataTransfer.dropEffect = \'move\';\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    groupDrag.dropTarget = getDragAfterElement(canvas, e.clientX, e.clientY);\n}\n\nfunction onGroupDragEnd() {\n    if (groupDrag.draggedGroup) {\n        if (groupDrag.dropTarget !== undefined) {\n            const canvas = document.getElementById(\'dashboard-canvas\');\n            if (groupDrag.dropTarget) {\n                canvas.insertBefore(groupDrag.draggedGroup, groupDrag.dropTarget);\n            } else {\n                canvas.appendChild(groupDrag.draggedGroup);\n            }\n        }\n        groupDrag.draggedGroup.classList.remove(\'opacity-40\');\n        groupDrag.draggedGroup = null;\n        saveGroupOrder();\n    }\n}\n\nfunction saveGroupOrder() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    const ordered = [...canvas.querySelectorAll(\'[data-group-id]\')].map(el => el.dataset.groupId);\n    const saved = getPickerGroups();\n    const orderedGroups = ordered.map(id => saved.find(g => g.id === id)).filter(Boolean);\n    setPickerGroups(orderedGroups);\n}\n\nfunction startGroupRename(groupId) {\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (!el) return;\n    const nameSpan = el.querySelector(\'.flex.items-center.justify-between span\');\n    if (!nameSpan) return;\n\n    const groups = getPickerGroups();\n    const group = groups.find(g => g.id === groupId);\n    if (!group) return;\n\n    const input = document.createElement(\'input\');\n    input.type = \'text\';\n    input.value = group.name;\n    input.className = \'bg-cyber-bg border border-neon-purple rounded px-1 py-0 text-xs text-white w-32\';\n    input.onblur = () => finishGroupRename(groupId, input.value);\n    input.onkeydown = (e) => { if (e.key === \'Enter\') input.blur(); if (e.key === \'Escape\') { input.value = group.name; input.blur(); } };\n\n    nameSpan.replaceWith(input);\n    input.focus();\n    input.select();\n}\n\nfunction finishGroupRename(groupId, newName) {\n    newName = newName.trim();\n    if (!newName) return;\n\n    const groups = getPickerGroups();\n    const group = groups.find(g => g.id === groupId);\n    if (!group) return;\n\n    group.name = newName;\n    setPickerGroups(groups);\n\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (el) {\n        const input = el.querySelector(\'input[type="text"]\');\n        if (input) {\n            const span = document.createElement(\'span\');\n            span.className = \'text-xs text-gray-400 font-medium cursor-pointer hover:text-white transition-colors\';\n            span.setAttribute(\'onclick\', `startGroupRename(\'${groupId}\')`);\n            span.textContent = newName;\n            input.replaceWith(span);\n        }\n    }\n}\n\nfunction getStatusBadgeClass(status) {\n    const classes = {\n        \'nominal\': \'bg-green-900 bg-opacity-30 text-neon-green\',\n        \'warning\': \'bg-orange-900 bg-opacity-30 text-neon-orange\',\n        \'critical\': \'bg-red-900 bg-opacity-30 text-neon-red\',\n        \'failsafe\': \'bg-red-900 bg-opacity-50 text-neon-red\',\n        \'standby\': \'bg-blue-900 bg-opacity-30 text-blue-400\',\n        \'inverted\': \'bg-cyan-900 bg-opacity-30 text-neon-cyan\',\n        \'no_sensor\': \'bg-yellow-900 bg-opacity-30 text-neon-orange\',\n        \'not_tested\': \'bg-gray-700 text-gray-400\',\n        \'calibrating\': \'bg-purple-900 bg-opacity-30 text-neon-purple\',\n        \'stopped\': \'bg-red-900 bg-opacity-50 text-neon-red\',\n        \'slowing\': \'bg-yellow-900 bg-opacity-30 text-yellow-400\',\n        \'needs_calibration\': \'bg-yellow-900 bg-opacity-30 text-yellow-400\',\n    };\n    return classes[status] || \'bg-gray-700 text-gray-400\';\n}\n\n// ============================================================================\n// INSPECTOR (Right Panel)\n// ============================================================================\n\nfunction updateInspector(fan) {\n    document.getElementById(\'inspector-empty\')?.classList.add(\'hidden\');\n    document.getElementById(\'inspector-fan\')?.classList.remove(\'hidden\');\n\n    const inspectorTitle = document.getElementById(\'inspector-title\');\n    if (inspectorTitle) inspectorTitle.textContent = fan.label;\n    const inspectorSubtitle = document.getElementById(\'inspector-subtitle\');\n    if (inspectorSubtitle) inspectorSubtitle.textContent = `ID: ${fan.id || \'unknown\'}`;\n\n    const fanName = document.getElementById(\'fan-name\');\n    if (fanName) fanName.textContent = fan.label;\n    \n    const statusBadge = document.getElementById(\'fan-status-badge\');\n    if (statusBadge) {\n        statusBadge.textContent = t(\'status.\' + fan.status, fan.status || \'unknown\');\n        statusBadge.className = `text-xs px-2 py-0.5 rounded-full ${getStatusBadgeClass(fan.status)}`;\n    }\n    \n    const invertedBadge = document.getElementById(\'fan-inverted-badge\');\n    if (invertedBadge) {\n        invertedBadge.classList.toggle(\'hidden\', !fan.inverted);\n    }\n    \n    const modeBadge = document.getElementById(\'fan-mode-badge\');\n    const mode = fan.mode || \'manual\';\n    if (modeBadge) {\n        modeBadge.textContent = t(\'mode.\' + mode, mode).toUpperCase();\n        modeBadge.className = mode === \'auto\' \n            ? \'text-xs px-2 py-0.5 rounded-full bg-cyan-900 bg-opacity-30 text-neon-cyan\'\n            : \'text-xs px-2 py-0.5 rounded-full bg-purple-900 bg-opacity-30 text-neon-purple\';\n    }\n    \n    const rpmDisplay = document.getElementById(\'fan-rpm-display\');\n    if (rpmDisplay) {\n        rpmDisplay.textContent = fan.rpm || 0;\n        rpmDisplay.classList.remove(\'text-neon-cyan\', \'text-neon-orange\', \'text-neon-red\');\n        if (fan.rpm > (fan.max_rpm * 0.8 || 1500)) {\n            rpmDisplay.classList.add(\'text-neon-orange\');\n        } else if (fan.status === \'failsafe\' || fan.status === \'critical\') {\n            rpmDisplay.classList.add(\'text-neon-red\');\n        } else {\n            rpmDisplay.classList.add(\'text-neon-cyan\');\n        }\n    }\n    \n    if (!store.isDragging) {\n        const slider = document.getElementById(\'pwm-slider\');\n        const pct = fan.current_pct != null ? fan.current_pct : (fan.manual_pct != null ? fan.manual_pct : 50);\n        if (slider) {\n            slider.value = pct;\n            slider.disabled = (mode === \'auto\');\n        }\n        const pwmValueDisplay = document.getElementById(\'pwm-value-display\');\n        if (pwmValueDisplay) pwmValueDisplay.textContent = `${pct}%`;\n    }\n    \n    setModeButtonStyles(mode);\n    \n    const autoSettings = document.getElementById(\'auto-settings\');\n    if (autoSettings) {\n        autoSettings.style.display = (mode === \'auto\') ? \'block\' : \'none\';\n    }\n    \n    // Render schedule grid when in auto mode\n    if (mode === \'auto\') {\n        setTimeout(() => renderScheduleGrid(), 50);\n    }\n    \n    // Store config\n    if (!store.fanConfigs[store.currentFanId]) store.fanConfigs[store.currentFanId] = {};\n    store.fanConfigs[store.currentFanId].sensors = fan.sensors || [];\n    store.fanConfigs[store.currentFanId].target_temp = fan.target_temp || 31;\n    store.fanConfigs[store.currentFanId].mode = mode;\n    store.fanConfigs[store.currentFanId].sensor_mode = fan.sensor_mode || \'max\';\n\n    // Calibration params\n    const cal = fan.calibration || {};\n    const minPwmEl = document.getElementById(\'cal-min-pwm\');\n    const maxPwmEl = document.getElementById(\'cal-max-pwm\');\n    const lambdaEl = document.getElementById(\'cal-lambda\');\n    if (minPwmEl) {\n        minPwmEl.value = cal.min_pwm || 0;\n        const calMinPwmVal = document.getElementById(\'cal-min-pwm-val\');\n        if (calMinPwmVal) calMinPwmVal.textContent = cal.min_pwm || 0;\n    }\n    if (maxPwmEl) {\n        maxPwmEl.value = cal.max_pwm || 255;\n        const calMaxPwmVal = document.getElementById(\'cal-max-pwm-val\');\n        if (calMaxPwmVal) calMaxPwmVal.textContent = cal.max_pwm || 255;\n    }\n    if (lambdaEl) {\n        lambdaEl.value = (cal.lambda || 1.0) * 10;\n        const calLambdaVal = document.getElementById(\'cal-lambda-val\');\n        if (calLambdaVal) calLambdaVal.textContent = (cal.lambda || 1.0).toFixed(1);\n    }\n\n    // Health & Service section\n    const health = fan.health || {};\n    const serviceSection = document.getElementById(\'fan-service-section\');\n    if (serviceSection) {\n        const lastService = health.last_service_date;\n        const needsCal = health.calibration_required;\n        const hStatus = health.status;\n\n        let svcHtml = \'\';\n        if (lastService) {\n            svcHtml += `<div class="text-xs text-gray-500">${t(\'fan.service_date\', \'Last service\')}: ${new Date(lastService).toLocaleDateString()}</div>`;\n        }\n        if (hStatus === \'stopped\' || hStatus === \'slowing\' || needsCal) {\n            svcHtml += `<button onclick="showServiceFanModal(\'${escapeHtml(fan.id)}\')" class="mt-2 px-3 py-1.5 bg-yellow-900/30 border border-yellow-700/50 rounded text-xs text-yellow-400 hover:bg-yellow-800/50 transition">\n                ${t(\'fan.service\', \'Service\')} / ${t(\'fan.replace\', \'Replace\')}\n            </button>`;\n        }\n        if (needsCal) {\n            svcHtml += `<div class="mt-2 px-2 py-1 rounded bg-yellow-900/20 border border-yellow-700/30 text-xs text-yellow-400">\n                ⚠ ${t(\'fan.calibration_required\', \'Calibration required after service\')}\n                <button onclick="startFanCalibration(\'${escapeHtml(fan.id)}\')" class="ml-2 underline hover:text-yellow-300">${t(\'inspector.calibrate\', \'Calibrate\')}</button>\n            </div>`;\n        }\n        serviceSection.innerHTML = svcHtml;\n    }\n}\n\n// ============================================================================\n// FAN CONTROL ACTIONS\n// ============================================================================\n\nfunction showServiceFanModal(fanId) {\n    const fan = store.state?.fans?.[fanId];\n    if (!fan) return;\n\n    const overlay = document.createElement(\'div\');\n    overlay.className = \'fixed inset-0 bg-black/60 z-50 flex items-center justify-center\';\n    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };\n\n    const today = new Date().toISOString().split(\'T\')[0];\n\n    overlay.innerHTML = `\n        <div class="bg-gray-900 border border-gray-700 rounded-xl p-5 w-80 shadow-2xl">\n            <h3 class="text-white font-semibold mb-3">${t(\'fan.service\', \'Service\')} / ${t(\'fan.replace\', \'Replace\')}</h3>\n            <p class="text-xs text-gray-400 mb-4">${escapeHtml(fan.label)}</p>\n            <div class="mb-4">\n                <label class="text-xs text-gray-500 block mb-1">${t(\'fan.service_date\', \'Date\')}</label>\n                <input type="date" id="service-date" value="${today}" class="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-xs text-white">\n            </div>\n            <div class="flex gap-2">\n                <button onclick="recordFanService(\'${escapeHtml(fanId)}\', \'service\', this)" class="flex-1 px-3 py-2 bg-yellow-900/30 border border-yellow-700/50 rounded text-xs text-yellow-400 hover:bg-yellow-800/50 transition">\n                    ${t(\'fan.service\', \'Service\')}\n                </button>\n                <button onclick="recordFanService(\'${escapeHtml(fanId)}\', \'replace\', this)" class="flex-1 px-3 py-2 bg-orange-900/30 border border-orange-700/50 rounded text-xs text-orange-400 hover:bg-orange-800/50 transition">\n                    ${t(\'fan.replace\', \'Replace\')}\n                </button>\n            </div>\n            <button onclick="this.closest(\'.fixed\').remove()" class="w-full mt-2 px-3 py-1.5 text-xs text-gray-500 hover:text-gray-300 transition">\n                ${t(\'common.cancel\', \'Cancel\')}\n            </button>\n        </div>\n    `;\n    document.body.appendChild(overlay);\n}\n\nasync function recordFanService(fanId, action, btnEl) {\n    const dateEl = document.getElementById(\'service-date\');\n    const date = dateEl ? dateEl.value : new Date().toISOString().split(\'T\')[0];\n\n    try {\n        const resp = await fetch(`/api/fan/${fanId}/service`, {\n            method: \'POST\',\n            headers: {\'Content-Type\': \'application/json\'},\n            body: JSON.stringify({action, date})\n        });\n        const data = await resp.json();\n        if (data.status === \'ok\') {\n            // Close modal\n            const overlay = btnEl?.closest(\'.fixed\');\n            if (overlay) overlay.remove();\n\n            showToast(t(\'fan.service_done\', \'Service recorded. Calibration recommended.\'), \'warning\', [\n                {label: t(\'inspector.calibrate\', \'Calibrate\'), onclick: () => startFanCalibration(fanId)},\n                {label: t(\'common.later\', \'Later\'), onclick: () => {}, secondary: true}\n            ]);\n\n            // Update local state\n            if (store.state?.fans?.[fanId]) {\n                store.state.fans[fanId].health = data.health;\n            }\n            if (store.currentFanId === fanId) {\n                updateInspector(store.state.fans[fanId]);\n            }\n            buildFanList(store.state.fans || {});\n            buildServerTree();\n        }\n    } catch (e) {\n        showToast(t(\'common.error\', \'Error\'), \'error\');\n    }\n}\n\nasync function startFanCalibration(fanId) {\n    try {\n        const resp = await fetch(\'/api/test/start\', {\n            method: \'POST\',\n            headers: {\'Content-Type\': \'application/json\'},\n            body: JSON.stringify({fan: fanId})\n        });\n        if (resp.ok) {\n            showToast(t(\'calibration.started\', \'Calibration started...\'), \'info\');\n        }\n    } catch (e) {\n        showToast(t(\'common.error\', \'Error\'), \'error\');\n    }\n}\n\nfunction setFanMode(mode) {\n    if (!store.currentFanId) return;\n    \n    // Update local state immediately for instant UI feedback\n    if (store.state?.fans?.[store.currentFanId]) {\n        store.state.fans[store.currentFanId].mode = mode;\n    }\n    if (store.fanConfigs[store.currentFanId]) {\n        store.fanConfigs[store.currentFanId].mode = mode;\n    }\n    \n    // Update button styles immediately\n    setModeButtonStyles(mode);\n    \n    document.getElementById(\'auto-settings\').style.display = (mode === \'auto\') ? \'block\' : \'none\';\n    if (mode === \'auto\') {\n        setTimeout(() => renderScheduleGrid(), 50);\n    }\n    \n    sendControl({\n        action: \'set_fan_config\',\n        fan: store.currentFanId,\n        fan_mode: mode\n    });\n}\n\nfunction sendControl(payload) {\n    fetch(\'/api/control\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify(payload)\n    })\n    .then(r => {\n        if (!r.ok) throw new Error(`HTTP ${r.status}`);\n        return r.json();\n    })\n    .catch(err => {\n        console.error(\'Control error:\', err);\n        showToast(t(\'toast.control_error\', \'Control command failed\'), \'error\');\n    });\n}\n\n// ============================================================================\n// PWM SLIDER\n// ============================================================================\n\ndocument.addEventListener(\'DOMContentLoaded\', () => {\n    updateCanvasColumns();\n    window.addEventListener(\'resize\', updateCanvasColumns);\n\n    const slider = document.getElementById(\'pwm-slider\');\n    if (!slider) return;\n    \n    slider.addEventListener(\'input\', (e) => {\n        document.getElementById(\'pwm-value-display\').textContent = `${e.target.value}%`;\n    });\n    \n    slider.addEventListener(\'mousedown\', () => {\n        store.isDragging = true;\n    });\n    \n    slider.addEventListener(\'mouseup\', (e) => {\n        store.isDragging = false;\n        applyPWM(e.target.value);\n    });\n    \n    slider.addEventListener(\'touchend\', (e) => {\n        store.isDragging = false;\n        applyPWM(e.target.value);\n    });\n});\n\nfunction applyPWM(value) {\n    if (!store.currentFanId) return;\n    \n    sendControl({\n        action: \'set_fan_pwm\',\n        fan: store.currentFanId,\n        pwm: parseInt(value)\n    });\n}\n\n// ============================================================================\n// SENSOR POPUP\n// ============================================================================\n\nfunction buildSensorList(data) {\n    store.allSensors = [];\n    const hidden = getHiddenSensors();\n\n    if (data.hdd_sensors) {\n        for (const [id, disk] of Object.entries(data.hdd_sensors)) {\n            if (hidden.includes(`disk:${id}`)) continue;\n            store.allSensors.push({\n                id: `hdd:${id}`,\n                label: disk.label,\n                temp: disk.temp,\n                standby: disk.standby,\n                group: \'sensors.disks\'\n            });\n        }\n    }\n\n    if (data.temp_sensors) {\n        for (const [id, sensor] of Object.entries(data.temp_sensors)) {\n            if (hidden.includes(`temp:${id}`)) continue;\n            store.allSensors.push({\n                id: `temp:${id}`,\n                label: sensor.label,\n                temp: sensor.value,\n                standby: false,\n                group: \'sensors.sensors_group\'\n            });\n        }\n    }\n}\n\nfunction toggleSensorPopup() {\n    const popup = document.getElementById(\'sensor-popup\');\n    const list = document.getElementById(\'sensor-popup-list\');\n    \n    if (!popup || !list) return;\n    \n    if (popup.classList.contains(\'hidden\')) {\n        // Build list\n        const currentSensors = store.fanConfigs[store.currentFanId]?.sensors || [];\n        list.innerHTML = buildSensorCheckboxList(store.allSensors, currentSensors);\n        popup.classList.remove(\'hidden\');\n    } else {\n        closeSensorPopup();\n    }\n}\n\nfunction closeSensorPopupForContext() {\n    const popup = document.getElementById(\'sensor-popup\');\n    if (!popup) return;\n    \n    if (popup._scheduleMode) {\n        toggleScheduleSensorPopup();\n    } else {\n        closeSensorPopup();\n    }\n}\n\nfunction closeSensorPopup() {\n    const popup = document.getElementById(\'sensor-popup\');\n    if (!popup) return;\n    \n    // Collect checked sensors\n    const checked = popup.querySelectorAll(\'input[type=checkbox]:checked\');\n    const sensors = Array.from(checked).map(cb => cb.value);\n    \n    if (store.currentFanId) {\n        if (!store.fanConfigs[store.currentFanId]) store.fanConfigs[store.currentFanId] = {};\n        store.fanConfigs[store.currentFanId].sensors = sensors;\n        \n        sendControl({\n            action: \'set_fan_config\',\n            fan: store.currentFanId,\n            sensors: sensors\n        });\n        \n        // Update no-sensor warning and sensor mode section\n        const mode = store.fanConfigs[store.currentFanId]?.mode || \'manual\';\n        const noSensorWarning = document.getElementById(\'no-sensor-warning\');\n        const sensorModeSection = document.getElementById(\'sensor-mode-section\');\n        if (noSensorWarning) {\n            noSensorWarning.classList.toggle(\'hidden\', sensors.length > 0 || mode !== \'auto\');\n        }\n        if (sensorModeSection) {\n            sensorModeSection.classList.toggle(\'hidden\', sensors.length <= 1);\n        }\n    }\n    \n    popup.classList.add(\'hidden\');\n}\n\n// ============================================================================\n// DISKS LIST (Left Panel Bottom)\n// ============================================================================\n\nfunction buildDisksList(disks) {\n    const container = document.getElementById(\'disks-mini-list\');\n    if (!container) return;\n    \n    let html = \'\';\n    \n    for (const [id, disk] of Object.entries(disks)) {\n        const pct = disk.pct_fill || 0;\n        const colorMap = {\n            \'cyan\': \'bg-neon-cyan\',\n            \'orange\': \'bg-neon-orange\',\n            \'red\': \'bg-neon-red\',\n            \'critical\': \'bg-neon-red animate-pulse\',\n            \'unknown\': \'bg-gray-600\'\n        };\n        const barColor = colorMap[disk.color_zone] || \'bg-gray-600\';\n        \n        html += `\n            <div class="flex items-center gap-2">\n                <span class="text-xs text-gray-400 w-14 truncate">${escapeHtml(disk.label)}</span>\n                <div class="flex-1 h-1.5 bg-cyber-accent rounded-full overflow-hidden">\n                    <div class="h-full ${barColor} rounded-full progress-fill" style="width: ${pct}%"></div>\n                </div>\n                <span class="text-xs font-mono w-10 text-right ${getTempColorClass(disk.temp)}">\n                    ${disk.standby ? t(\'sensor.sleep\', \'Sleep\') : disk.temp > 0 ? formatTemp(disk.temp) : \'--\'}\n                </span>\n            </div>\n        `;\n    }\n    \n    container.innerHTML = html || `<div class="text-xs text-gray-500">${t(\'setup.no_disks\', \'No disks detected\')}</div>`;\n}\n\n// ============================================================================\n// SETUP WIZARD\n// ============================================================================\n\nfunction runDiscovery() {\n    console.log(\'[FanControl] Starting hardware discovery...\');\n    \n    setDiscoverButtonState(true);\n    store.wizardStep = \'scanning\';\n    \n    fetch(\'/api/discover\', { method: \'POST\' })\n        .then(r => r.json())\n        .then(data => {\n            setDiscoverButtonState(false);\n            \n            if (data.status === \'ok\') {\n                renderDiscoveredHardware(data);\n                store.wizardStep = \'results\';\n                \n                document.getElementById(\'setup-step-intro\').classList.add(\'hidden\');\n                document.getElementById(\'setup-step-results\').classList.remove(\'hidden\');\n            } else {\n                alert(t(\'discover.scan_error\', \'Scan error: \') + data.message);\n                store.wizardStep = \'intro\';\n            }\n        })\n        .catch(err => {\n            console.error(\'Discovery error:\', err);\n            alert(t(\'discover.connection_error\', \'Connection error\'));\n            setDiscoverButtonState(false);\n            store.wizardStep = \'intro\';\n        });\n}\n\nfunction renderDiscoveredHardware(data) {\n    const container = document.getElementById(\'discovered-devices\');\n    if (!container) return;\n    \n    let html = \'\';\n    \n    // Kernel info banner\n    if (data.kernel_info) {\n        const ki = data.kernel_info;\n        const isCustom = ki.type === \'custom\';\n        const kernelColor = isCustom ? \'text-neon-green\' : \'text-neon-orange\';\n        const kernelLabel = isCustom ? \'Custom ARC\' : ki.type === \'official\' ? \'Official Synology\' : \'Unknown\';\n        const fanMethod = ki.has_hwmon_pwm ? \'hwmon (PWM)\' : ki.has_scemd ? \'scemd.xml (DSM API)\' : \'none\';\n        html += `<div class="bg-cyber-accent rounded-lg p-3 mb-4 text-xs">\n            <div class="flex justify-between mb-1">\n                <span class="text-gray-400">Kernel:</span>\n                <span class="${kernelColor} font-semibold">${kernelLabel}</span>\n            </div>\n            <div class="flex justify-between mb-1">\n                <span class="text-gray-400">Fan control:</span>\n                <span class="text-white">${fanMethod}</span>\n            </div>\n            ${ki.version ? `<div class="text-gray-500 mt-1 truncate" title="${escapeHtml(ki.version)}">${escapeHtml(ki.version)}</div>` : \'\'}\n        </div>`;\n    }\n    \n    // Fans section\n    if (data.fans && Object.keys(data.fans).length > 0) {\n        html += \'<h4 class="text-sm font-semibold text-neon-cyan mb-2">🌀 Fans</h4>\';\n        for (const [id, fan] of Object.entries(data.fans)) {\n            const cleanLabel = fan.label.replace(/\\s*\\(Synology-[^)]+\\)/, \'\');\n            const isDsm = fan.control_method === \'dsm_scemd\';\n            html += `\n                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">\n                    <div>\n                        <span class="text-sm text-white">${escapeHtml(cleanLabel)}</span>\n                        <span class="text-xs text-gray-500 ml-2">${fan.writable ? \'Controllable\' : \'Read-only\'}</span>\n                        ${isDsm ? \'<span class="text-xs bg-blue-900 bg-opacity-30 text-blue-400 px-2 py-0.5 rounded ml-2">DSM</span>\' : \'\'}\n                    </div>\n                    ${!isDsm ? \'<span class="text-xs bg-orange-900 bg-opacity-30 text-neon-orange px-2 py-0.5 rounded">Not calibrated</span>\' : \'\'}\n                </div>\n            `;\n        }\n    }\n    \n    // Sensors section\n    if (data.temps && Object.keys(data.temps).length > 0) {\n        html += \'<h4 class="text-sm font-semibold text-neon-green mb-2 mt-4">🌡️ Temperature Sensors</h4>\';\n        for (const [id, sensor] of Object.entries(data.temps)) {\n            html += `\n                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">\n                    <span class="text-sm text-white">${escapeHtml(sensor.label)}</span>\n                    <span class="text-sm font-mono text-neon-cyan">${formatTemp(sensor.value)}</span>\n                </div>\n            `;\n        }\n    }\n    \n    // Disks section\n    if (data.disks && Object.keys(data.disks).length > 0) {\n        html += \'<h4 class="text-sm font-semibold text-neon-purple mb-2 mt-4">💾 Storage Disks</h4>\';\n        for (const [id, disk] of Object.entries(data.disks)) {\n            html += `\n                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">\n                    <span class="text-sm text-white">${escapeHtml(disk.label)} <span class="text-xs text-gray-500">(${escapeHtml(disk.type)})</span></span>\n                    <span class="text-sm font-mono ${getTempColorClass(disk.temp)}">\n                            ${disk.standby ? t(\'sensor.sleep\', \'Sleep\') : disk.temp > 0 ? formatTemp(disk.temp) : \'--\'}\n                    </span>\n                </div>\n            `;\n        }\n    }\n    \n    container.innerHTML = html || `<p class="text-gray-500">${t(\'setup.no_hardware\', \'No hardware detected\')}</p>`;\n    \n    // Determine available control modes\n    const actionDiv = document.getElementById(\'setup-step-action\');\n    const controlSelect = document.getElementById(\'control-mode-select\');\n    const hwmonBtn = document.getElementById(\'btn-hwmon\');\n    const dsmBtn = document.getElementById(\'btn-dsm\');\n    const hint = document.getElementById(\'mode-unavailable-hint\');\n    \n    const kernelInfo = data.kernel_info || {};\n    const hasHwmon = kernelInfo.has_hwmon_pwm;\n    const hasDsm = kernelInfo.has_scemd;\n    const hasFans = data.fans && Object.keys(data.fans).length > 0;\n    \n    // Always show mode selection when fans are detected\n    if (hasFans && (hasHwmon || hasDsm)) {\n        controlSelect.classList.remove(\'hidden\');\n        document.getElementById(\'hwmon-action\').classList.add(\'hidden\');\n        document.getElementById(\'dsm-action\').classList.add(\'hidden\');\n        actionDiv.classList.remove(\'hidden\');\n        \n        // HWMon button state\n        if (hasHwmon) {\n            hwmonBtn.classList.remove(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            hwmonBtn.disabled = false;\n        } else {\n            hwmonBtn.classList.add(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            hwmonBtn.disabled = true;\n        }\n        \n        // DSM button state\n        if (hasDsm) {\n            dsmBtn.classList.remove(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            dsmBtn.disabled = false;\n        } else {\n            dsmBtn.classList.add(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            dsmBtn.disabled = true;\n        }\n        \n        // Show hint if one mode unavailable\n        if (hasHwmon && !hasDsm) {\n            hint.textContent = \'DSM schemes not found — only hwmon control available.\';\n            hint.classList.remove(\'hidden\');\n        } else if (!hasHwmon && hasDsm) {\n            hint.textContent = \'hwmon PWM not available on this kernel — only DSM scheme control available.\';\n            hint.classList.remove(\'hidden\');\n        } else {\n            hint.classList.add(\'hidden\');\n        }\n    } else if (hasFans && !hasHwmon && !hasDsm) {\n        // Fans but no control method\n        controlSelect.classList.add(\'hidden\');\n        document.getElementById(\'hwmon-action\').classList.add(\'hidden\');\n        document.getElementById(\'dsm-action\').classList.add(\'hidden\');\n        actionDiv.classList.remove(\'hidden\');\n        hint.textContent = \'No fan control method available.\';\n        hint.classList.remove(\'hidden\');\n    } else {\n        // No fans\n        controlSelect.classList.add(\'hidden\');\n        document.getElementById(\'hwmon-action\').classList.add(\'hidden\');\n        document.getElementById(\'dsm-action\').classList.add(\'hidden\');\n        actionDiv.classList.add(\'hidden\');\n    }\n}\n\nfunction selectControlMode(mode) {\n    const hwmonAction = document.getElementById(\'hwmon-action\');\n    const dsmAction = document.getElementById(\'dsm-action\');\n    const hwmonBtn = document.getElementById(\'btn-hwmon\');\n    const dsmBtn = document.getElementById(\'btn-dsm\');\n    \n    hwmonAction.classList.add(\'hidden\');\n    dsmAction.classList.add(\'hidden\');\n    \n    if (mode === \'hwmon\') {\n        hwmonBtn.classList.add(\'card-selected\');\n        dsmBtn.classList.remove(\'card-selected\');\n        hwmonAction.classList.remove(\'hidden\');\n    } else {\n        dsmBtn.classList.add(\'card-selected\');\n        hwmonBtn.classList.remove(\'card-selected\');\n        dsmAction.classList.remove(\'hidden\');\n    }\n}\n\nfunction applyDsmAndContinue() {\n    // Skip calibration, go straight to DSM scheme editor\n    fetch(\'/api/skip-calibration\', { method: \'POST\' }).catch(err => console.error(\'Skip calibration error:\', err));\n    fetch(\'/api/dsm/fan-speed\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify({ speed: 50 })\n    }).catch(err => console.error(\'DSM fan speed error:\', err));\n    store.wizardStep = \'done\';\n    store.state = { ...store.state, initialized: true, tested: true };\n    showMainScreen();\n    setTimeout(() => showView(\'dsm-scheme\'), 500);\n}\n\nfunction skipCalibration() {\n    console.log(\'[FanControl] Skipping calibration — monitoring-only mode\');\n    fetch(\'/api/skip-calibration\', { method: \'POST\' })\n        .catch(err => console.error(\'Skip calibration error:\', err));\n    store.wizardStep = \'done\';\n    store.state = { ...store.state, initialized: true, tested: true };\n    showMainScreen();\n}\n\nfunction applyDsmFanSpeed() {\n    const speed = parseInt(document.getElementById(\'dsm-speed-slider\').value);\n    console.log(`[FanControl] Setting DSM fan speed to ${speed}%`);\n    \n    fetch(\'/api/dsm/fan-speed\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify({ speed })\n    })\n    .then(r => r.json())\n    .then(data => {\n        if (data.status === \'ok\') {\n            fetch(\'/api/skip-calibration\', { method: \'POST\' }).catch(err => console.error(\'Skip calibration error:\', err));\n            store.wizardStep = \'done\';\n            store.state = { ...store.state, initialized: true, tested: true };\n            showMainScreen();\n        } else {\n            alert(\'Error: \' + (data.message || t(\'toast.speed_failed\', \'Failed to set fan speed\')));\n        }\n    })\n    .catch(err => {\n        console.error(\'DSM fan speed error:\', err);\n        alert(t(\'toast.speed_failed\', \'Failed to set fan speed\'));\n    });\n}\n\n// ============================================================================\n// DSM SCHEME EDITOR\n// ============================================================================\n\nasync function renderDsmSchemeEditor(remoteNodeId) {\n    const container = document.getElementById(\'dsm-scheme-inner\');\n    if (!container) return;\n\n    container.innerHTML = `<div class="text-gray-500 text-center py-8">${t(\'dsm.loading\', \'Loading DSM schemes...\')}</div>`;\n\n    try {\n        let schemesData, activeData;\n\n        if (remoteNodeId) {\n            // Remote node — use schemes from node state\n            const node = store.nodesData.find(n => n.node_id === remoteNodeId);\n            if (!node) {\n                container.innerHTML = `<div class="text-red-400 text-center py-8">${t(\'dsm.node_not_found\', \'Node not found\')}</div>`;\n                return;\n            }\n            schemesData = { status: \'ok\', schemes: node.telemetry?.dsm_schemes || node.config?.dsm_schemes || node.dsm_schemes || [] };\n            activeData = { active_scheme: null };\n        } else {\n            // Local server\n            const [schemesResp, activeResp] = await Promise.all([\n                fetch(\'/api/dsm/schemes\'),\n                fetch(\'/api/dsm/active\')\n            ]);\n            schemesData = await schemesResp.json();\n            activeData = await activeResp.json();\n        }\n\n        if (schemesData.status !== \'ok\') {\n            container.innerHTML = `<div class="text-red-400 text-center py-8">${schemesData.message || \'Failed to load schemes\'}</div>`;\n            return;\n        }\n\n        dsm.schemes = schemesData.schemes || [];\n        dsm.activeScheme = activeData.active_scheme || null;\n\n        if (dsm.schemes.length === 0) {\n            container.innerHTML = `<div class="text-gray-500 text-center py-8">${t(\'dsm.no_schemes\', \'No fan schemes found in scemd.xml\')}</div>`;\n            return;\n        }\n\n        let html = `\n            <div class="max-w-4xl mx-auto">\n                <div class="flex items-center justify-between mb-6">\n                    <h2 class="text-xl font-bold text-white">DSM Fan Schemes</h2>\n                    <button onclick="showView(\'dashboard\')" class="text-gray-400 hover:text-white text-sm">\n                        &larr; ${t(\'dsm.back\', \'Back to Dashboard\')}\n                    </button>\n                </div>\n        `;\n\n        for (const scheme of dsm.schemes) {\n            const isActive = scheme.type === dsm.activeScheme;\n            const schemeLabel = _schemeLabel(scheme.type);\n\n            html += `\n                <div class="mb-6 bg-gray-900/50 border ${isActive ? \'border-green-500/50\' : \'border-gray-700\'} rounded-xl p-4">\n                    <div class="flex items-center justify-between mb-3">\n                        <div class="flex items-center gap-3">\n                            <h3 class="text-white font-semibold">${schemeLabel}</h3>\n                            ${isActive ? `<span class="text-xs bg-green-900/50 text-green-400 px-2 py-0.5 rounded">${t(\'dsm.active\', \'Active\')}</span>` : \'\'}\n                            ${scheme.hibernation_speed === \'STOP\' ? \'<span class="text-xs bg-yellow-900/50 text-yellow-400 px-2 py-0.5 rounded">Hibernation: STOP</span>\' : \'\'}\n                        </div>\n                        <button onclick="applyDsmScheme(\'${escapeHtml(scheme.type)}\')"\n                                class="px-3 py-1 bg-neon-cyan/20 border border-neon-cyan/50 text-neon-cyan text-xs rounded hover:bg-neon-cyan/30 transition-all">\n                            ${t(\'dsm.apply\', \'Apply\')}\n                        </button>\n                    </div>\n            `;\n\n            if (scheme.entries.length > 0) {\n                html += `\n                    <table class="w-full text-sm">\n                        <thead>\n                            <tr class="text-gray-400 text-xs border-b border-gray-700">\n                                <th class="text-left py-2">${t(\'dsm.col_sensor\', \'Sensor\')}</th>\n                                <th class="text-left py-2">${t(\'dsm.col_speed\', \'Speed\')}</th>\n                                <th class="text-left py-2">${t(\'dsm.col_action\', \'Action\')}</th>\n                                <th class="text-left py-2">${t(\'dsm.col_threshold\', \'Threshold\')}</th>\n                                <th class="text-right py-2">${t(\'dsm.col_edit\', \'Edit\')}</th>\n                            </tr>\n                        </thead>\n                        <tbody>\n                `;\n\n                for (let i = 0; i < scheme.entries.length; i++) {\n                    const entry = scheme.entries[i];\n                    const isLast = i === scheme.entries.length - 1;\n                    const sensorLabel = entry.sensor_type === \'cpu_temperature\' ? \'CPU\' : \'Disk\';\n                    const speedDisplay = entry.fan_speed || \'--\';\n                    const actionClass = entry.action === \'SHUTDOWN\' ? \'text-red-400\' : \'text-gray-300\';\n                    const threshold = entry.threshold_temp + \'°C\';\n\n                    html += `\n                        <tr class="border-b border-gray-800 hover:bg-gray-800/30">\n                            <td class="py-2">\n                                <span class="px-1.5 py-0.5 rounded text-xs ${entry.sensor_type === \'cpu_temperature\' ? \'bg-blue-900/50 text-blue-300\' : \'bg-purple-900/50 text-purple-300\'}">${sensorLabel}</span>\n                            </td>\n                            <td class="py-2 text-white font-mono">${escapeHtml(speedDisplay)}</td>\n                            <td class="py-2 ${actionClass}">${escapeHtml(entry.action)}</td>\n                            <td class="py-2 text-gray-300">${threshold}</td>\n                            <td class="py-2 text-right">\n                                <button onclick="editDsmEntry(\'${escapeHtml(scheme.type)}\', ${i})"\n                                        class="text-gray-500 hover:text-neon-cyan text-xs px-1">✎</button>\n                            </td>\n                        </tr>\n                    `;\n                }\n\n                html += \'</tbody></table>\';\n            } else {\n                html += `<div class="text-gray-500 text-xs py-2">${t(\'dsm.no_entries\', \'No entries\')}</div>`;\n            }\n\n            html += \'</div>\';\n        }\n\n        html += \'</div>\';\n        container.innerHTML = html;\n\n    } catch (e) {\n        container.innerHTML = `<div class="text-red-400 text-center py-8">Error loading DSM schemes: ${e.message}</div>`;\n    }\n}\n\nfunction _schemeLabel(type) {\n    const labels = {\n        \'DUAL_MODE_HIGH\': \'High Performance\',\n        \'DUAL_MODE_LOW\': \'Quiet Mode\',\n        \'FULL_SPEED\': \'Full Speed\',\n        \'STOP\': \'Stop (Fan Off)\',\n        \'FLAT\': \'Flat Config\',\n    };\n    return labels[type] || type;\n}\n\nasync function editDsmEntry(schemeType, index) {\n    const scheme = dsm.schemes.find(s => s.type === schemeType);\n    if (!scheme || !scheme.entries[index]) return;\n\n    const entry = scheme.entries[index];\n    const newSpeed = prompt(`Fan speed % for ${entry.sensor_type} (threshold ${entry.threshold_temp}°C):`, entry.fan_speed || \'20\');\n    if (newSpeed === null) return;\n\n    const newAction = prompt(`Action (NONE or SHUTDOWN):`, entry.action || \'NONE\');\n    if (newAction === null) return;\n\n    const newThreshold = prompt(`Threshold temperature °C:`, entry.threshold_temp || \'0\');\n    if (newThreshold === null) return;\n\n    // Update locally first (works for both local and remote)\n    entry.fan_speed = parseInt(newSpeed) || 20;\n    entry.action = newAction.toUpperCase() === \'SHUTDOWN\' ? \'SHUTDOWN\' : \'NONE\';\n    entry.threshold_temp = parseInt(newThreshold) || 0;\n\n    if (store.currentRemoteNodeId) {\n        // Remote — local edit only, applied when user clicks "Apply"\n        renderDsmSchemeEditor();\n        return;\n    }\n\n    // Local — persist to scemd.xml immediately\n    try {\n        const resp = await fetch(`/api/dsm/scheme/${schemeType}/entry/${index}`, {\n            method: \'PUT\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({\n                fan_speed_pct: parseInt(newSpeed) || 20,\n                action: newAction.toUpperCase() === \'SHUTDOWN\' ? \'SHUTDOWN\' : \'NONE\',\n                threshold_temp: parseInt(newThreshold) || 0\n            })\n        });\n        if (resp.ok) {\n            renderDsmSchemeEditor();\n        } else {\n            const err = await resp.json();\n            alert(err.message || t(\'dsm.entry_failed\', \'Failed to update entry\'));\n        }\n    } catch (e) {\n        alert(\'Error: \' + e.message);\n    }\n}\n\nasync function applyDsmScheme(schemeType) {\n    try {\n        if (store.currentRemoteNodeId) {\n            // Remote node — push scheme via WebSocket\n            const node = store.nodesData.find(n => n.node_id === store.currentRemoteNodeId);\n            const scheme = (node?.telemetry?.dsm_schemes || node?.config?.dsm_schemes || node?.dsm_schemes || []).find(s => s.type === schemeType);\n            if (!scheme) {\n                showToast(t(\'dsm.node_not_found\', \'Node not found\'), \'error\');\n                return;\n            }\n            socket.emit(\'server:dsm:apply\', {\n                node_id: store.currentRemoteNodeId,\n                scheme_type: schemeType,\n                entries: scheme.entries.map((e, i) => ({\n                    index: i,\n                    fan_speed_pct: e.fan_speed,\n                    action: e.action,\n                    threshold_temp: e.threshold_temp,\n                })),\n            });\n            showToast(t(\'dsm.apply_remote\', \'Scheme applied to remote agent\'), \'success\');\n        } else {\n            // Local server\n            const resp = await fetch(\'/api/dsm/apply\', { method: \'POST\' });\n            const data = await resp.json();\n            if (data.status === \'ok\') {\n                showToast(t(\'dsm.apply_ok\', \'Scheme applied successfully\'), \'success\');\n            } else {\n                showToast(data.message || t(\'dsm.apply_failed\', \'Failed to apply scheme\'), \'error\');\n            }\n        }\n    } catch (e) {\n        showToast(t(\'dsm.apply_failed\', \'Failed to apply scheme\') + \': \' + e.message, \'error\');\n    }\n}\n\nfunction runCalibration() {\n    console.log(\'[FanControl] Starting calibration...\');\n    \n    document.getElementById(\'calibrate-btn\').disabled = true;\n    document.getElementById(\'calibrate-loader\').classList.remove(\'hidden\');\n    store.wizardStep = \'calibrating\';\n    \n    document.getElementById(\'calibration-modal\').classList.remove(\'hidden\');\n    document.getElementById(\'calibration-status\').textContent = t(\'calibration.starting\', \'Starting...\');\n    document.getElementById(\'calibration-progress-bar\').style.width = \'0%\';\n    document.getElementById(\'calibration-step\').textContent = t(\'calibration.step_label\', \'Step 0/11\').replace(\'${current}\', \'0\').replace(\'${total}\', \'11\');\n    \n    fetch(\'/api/initialize\', { method: \'POST\' })\n        .then(r => r.json())\n        .then(data => {\n            console.log(\'[FanControl] Calibration initiated:\', data);\n        })\n        .catch(err => {\n            console.error(\'Calibration error:\', err);\n            hideCalibrationModal();\n            document.getElementById(\'calibrate-btn\').disabled = false;\n            document.getElementById(\'calibrate-loader\').classList.add(\'hidden\');\n        });\n}\n\nfunction updateCalibrationModal(progress) {\n    const modal = document.getElementById(\'calibration-modal\');\n    if (modal.classList.contains(\'hidden\')) {\n        modal.classList.remove(\'hidden\');\n    }\n    \n    document.getElementById(\'calibration-status\').textContent = progress.status;\n    document.getElementById(\'calibration-step\').textContent =\n        t(\'calibration.step_label\', \'Step ${current}/${total}\').replace(\'${current}\', progress.step).replace(\'${total}\', progress.total);\n    \n    const pct = progress.total > 0 ? (progress.step / progress.total * 100) : 0;\n    document.getElementById(\'calibration-progress-bar\').style.width = `${pct}%`;\n}\n\nfunction hideCalibrationModal() {\n    document.getElementById(\'calibration-modal\').classList.add(\'hidden\');\n}\n\nfunction updateCalibrationParam(param, value) {\n    if (!store.currentFanId || !store.state || !store.state.fans) return;\n    const fan = store.state.fans[store.currentFanId];\n    if (!fan) return;\n\n    if (!fan.calibration) fan.calibration = {};\n\n    if (param === \'lambda\') {\n        fan.calibration.lambda = parseFloat(value);\n        document.getElementById(\'cal-lambda-val\').textContent = parseFloat(value).toFixed(1);\n    } else if (param === \'min_pwm\') {\n        fan.calibration.min_pwm = parseInt(value);\n        document.getElementById(\'cal-min-pwm-val\').textContent = value;\n    } else if (param === \'max_pwm\') {\n        fan.calibration.max_pwm = parseInt(value);\n        document.getElementById(\'cal-max-pwm-val\').textContent = value;\n    }\n\n    saveFanCalibration(store.currentFanId, fan.calibration);\n}\n\nfunction saveFanCalibration(fanId, calibration) {\n    fetch(\'/api/fan/\' + fanId + \'/calibration\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify(calibration)\n    }).catch(err => console.error(\'Save calibration error:\', err));\n}\n\nfunction startCalibration() {\n    if (!confirm(t(\'calibration.confirm\', \'Recalibrate all fans? This takes 1-2 minutes.\'))) return;\n    \n    document.getElementById(\'calibration-modal\').classList.remove(\'hidden\');\n    document.getElementById(\'calibration-status\').textContent = t(\'calibration.starting\', \'Starting...\');\n    document.getElementById(\'calibration-progress-bar\').style.width = \'0%\';\n    document.getElementById(\'calibration-step\').textContent = t(\'calibration.step_label\', \'Step 0/21\').replace(\'${current}\', \'0\').replace(\'${total}\', \'21\');\n    \n    fetch(\'/api/initialize\', { method: \'POST\' })\n        .catch(err => console.error(\'Calibration error:\', err));\n}\n\n// ============================================================================\n// SCHEDULE GRID\n// ============================================================================\n\nconst DAYS = [\'mon\', \'tue\', \'wed\', \'thu\', \'fri\', \'sat\', \'sun\'];\nconst DAY_LABELS = [\'Mon\', \'Tue\', \'Wed\', \'Thu\', \'Fri\', \'Sat\', \'Sun\'];\nconst DAY_KEYS = [\'days.mon\', \'days.tue\', \'days.wed\', \'days.thu\', \'days.fri\', \'days.sat\', \'days.sun\'];\n\nfunction tDay(idx) {\n    return t(DAY_KEYS[idx], DAY_LABELS[idx]);\n}\n\nfunction renderScheduleGrid() {\n    const container = document.getElementById(\'schedule-grid\');\n    if (!container) return;\n    \n    const fan = store.state?.fans?.[store.currentFanId];\n    const fanSchedule = fan?.schedule || [];\n    schedule.data = {};\n    fanSchedule.forEach(item => {\n        const key = `${item.day}_${item.time_start}`;\n        schedule.data[key] = item;\n    });\n    \n    // Build color map for cells\n    const colorMap = {};\n    const groups = {};\n    fanSchedule.forEach(item => {\n        const key = ruleKey(item);\n        if (!groups[key]) groups[key] = [];\n        groups[key].push(item);\n    });\n    const groupKeys = Object.keys(groups);\n    groupKeys.forEach((gk, idx) => {\n        const color = getRuleColor(idx);\n        groups[gk].forEach(item => {\n            const cellKey = `${item.day}_${item.time_start}`;\n            colorMap[cellKey] = color;\n        });\n    });\n    \n    let html = \'<table class="border-collapse" style="border-spacing: 1px;">\';\n    \n    // Header row: empty corner + 24 hours\n    html += \'<tr><th class="w-12 h-5"></th>\';\n    for (let h = 0; h < 24; h++) {\n        html += `<th class="h-5 px-0 text-[10px] text-gray-500 font-normal" style="width:${SCHEDULE_CELL_SIZE}px">${h}</th>`;\n    }\n    html += \'</tr>\';\n    \n    // Day rows\n    for (let d = 0; d < DAYS.length; d++) {\n        const day = DAYS[d];\n        html += `<tr><td class="w-12 h-5 text-[10px] text-gray-400 font-semibold pr-1 text-right align-middle">${tDay(d)}</td>`;\n        \n        for (let h = 0; h < 24; h++) {\n            const timeStr = String(h).padStart(2, \'0\') + \':00\';\n            const key = `${day}_${timeStr}`;\n            const item = schedule.data[key];\n            \n            let bgStyle = \'background:#1f2937\';\n            if (item) {\n                const cm = colorMap[key];\n                if (cm) {\n                    bgStyle = `background:${cm.hex}`;\n                } else {\n                    bgStyle = item.mode === \'auto\' ? \'background:#15803d\' : item.mode === \'manual\' ? \'background:#c2410c\' : \'background:#991b1b\';\n                }\n            }\n            \n            html += `<td class="cursor-pointer schedule-cell transition-colors duration-75"\n                         data-day="${day}" data-hour="${h}"\n                         onmousedown="onScheduleMouseDown(event,\'${day}\',${h})"\n                         onmouseenter="onScheduleMouseEnter(event,\'${day}\',${h})"\n                         title="${tDay(d)} ${timeStr}${item ? \' [\' + t(\'mode.\' + item.mode, item.mode) + \']\' : \'\'}"\n                         style="width:${SCHEDULE_CELL_SIZE}px;height:${SCHEDULE_CELL_SIZE}px;${bgStyle}"></td>`;\n        }\n        html += \'</tr>\';\n    }\n    \n    html += \'</table>\';\n    container.innerHTML = html;\n    \n    renderScheduleRules();\n    validateSchedule();\n}\n\nconst RULE_COLORS = [\n    { hex: \'#15803d\', dot: \'#4ade80\', text: \'#86efac\' },\n    { hex: \'#c2410c\', dot: \'#fb923c\', text: \'#fdba74\' },\n    { hex: \'#991b1b\', dot: \'#f87171\', text: \'#fca5a5\' },\n    { hex: \'#1d4ed8\', dot: \'#60a5fa\', text: \'#93c5fd\' },\n    { hex: \'#7e22ce\', dot: \'#c084fc\', text: \'#d8b4fe\' },\n    { hex: \'#a16207\', dot: \'#facc15\', text: \'#fde047\' },\n    { hex: \'#be185d\', dot: \'#f472b6\', text: \'#f9a8d4\' },\n    { hex: \'#0f766e\', dot: \'#2dd4bf\', text: \'#5eead4\' },\n];\n\nfunction getRuleColor(idx) {\n    if (idx < RULE_COLORS.length) return RULE_COLORS[idx];\n    // Generate colors via HSL for groups beyond 8\n    const hue = (idx * 137) % 360;\n    const hex = `hsl(${hue}, 60%, 35%)`;\n    const dot = `hsl(${hue}, 70%, 65%)`;\n    const text = `hsl(${hue}, 70%, 80%)`;\n    return { hex, dot, text };\n}\n\nfunction ruleKey(item) {\n    return JSON.stringify({\n        mode: item.mode,\n        target_temp: item.target_temp,\n        speed_pct: item.speed_pct,\n        sensors: [...(item.sensors || [])].sort(),\n        sensor_mode: item.sensor_mode\n    });\n}\n\nfunction renderScheduleRules() {\n    const container = document.getElementById(\'schedule-rules\');\n    if (!container) return;\n    \n    const fan = store.state?.fans?.[store.currentFanId];\n    const fanSchedule = fan?.schedule || [];\n    \n    if (fanSchedule.length === 0) {\n        container.innerHTML = `<p class="text-xs text-gray-500 italic">${t(\'schedule.no_rules\', \'No rules configured\')}</p>`;\n        return;\n    }\n    \n    // Group by identical settings\n    const groups = {};\n    fanSchedule.forEach(item => {\n        const key = ruleKey(item);\n        if (!groups[key]) groups[key] = { item, cells: [] };\n        groups[key].cells.push(item);\n    });\n    \n    const groupList = Object.values(groups);\n    \n    let html = \'<div class="space-y-1">\';\n    groupList.forEach((group, gIdx) => {\n        const color = getRuleColor(gIdx);\n        const item = group.item;\n        const cells = group.cells;\n        \n        let settings = \'\';\n        if (item.mode === \'auto\') {\n            const sensorNames = (item.sensors || []).map(s => {\n                const sen = store.allSensors.find(x => x.id === s);\n                return sen ? sen.label : s.split(\':\').pop();\n            });\n            settings = `${formatTemp(item.target_temp || 31)}`;\n            if (sensorNames.length > 0) {\n                settings += ` · ${sensorNames.join(\', \')}`;\n                if (item.sensor_mode && sensorNames.length > 1) {\n                    settings += ` (${item.sensor_mode})`;\n                }\n            }\n        } else if (item.mode === \'manual\') {\n            settings = `${item.speed_pct ?? 50}%`;\n        } else {\n            settings = \'off\';\n        }\n        \n        // Group cells by day to build sub-periods\n        const byDay = {};\n        cells.forEach(c => {\n            if (!byDay[c.day]) byDay[c.day] = [];\n            byDay[c.day].push(c);\n        });\n        \n        // Build contiguous time ranges per day\n        const subPeriods = [];\n        for (const [day, dayCells] of Object.entries(byDay)) {\n            const hours = dayCells.map(c => parseInt(c.time_start)).sort((a, b) => a - b);\n            let start = hours[0], prev = hours[0];\n            for (let i = 1; i < hours.length; i++) {\n                if (hours[i] === prev + 1) {\n                    prev = hours[i];\n                } else {\n                    subPeriods.push({ day, from: start, to: prev });\n                    start = hours[i];\n                    prev = hours[i];\n                }\n            }\n            subPeriods.push({ day, from: start, to: prev });\n        }\n        subPeriods.sort((a, b) => {\n            const d = DAYS.indexOf(a.day) - DAYS.indexOf(b.day);\n            return d !== 0 ? d : a.from - b.from;\n        });\n        \n        const modeIcon = item.mode === \'auto\' ? \'🌡️\' : item.mode === \'manual\' ? \'🎮\' : \'⏻\';\n        \n        html += `\n            <div class="bg-cyber-accent rounded-lg overflow-hidden">\n                <div class="flex items-center gap-2 px-3 py-2">\n                    <span class="w-3 h-3 rounded-full flex-shrink-0" style="background:${color.dot}"></span>\n                    <span class="text-xs flex-shrink-0">${modeIcon}</span>\n                    <div class="flex-1 min-w-0 cursor-pointer" onclick="toggleRuleGroup(${gIdx})">\n                        <span class="text-xs font-semibold" style="color:${color.text}">${escapeHtml(settings)}</span>\n                        <span class="text-[10px] text-gray-500 ml-2">${cells.length}h</span>\n                    </div>\n                    <button onclick="editRuleGroup(${gIdx}); event.stopPropagation()"\n                            class="text-[10px] text-gray-400 hover:text-neon-cyan px-1.5 py-0.5 rounded hover:bg-cyber-bg transition-all flex-shrink-0">\n                        ${t(\'schedule.edit\', \'Edit\')}\n                    </button>\n                    <button onclick="deleteRuleGroup(${gIdx}); event.stopPropagation()"\n                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-bg transition-all flex-shrink-0">\n                        ${t(\'schedule.delete\', \'Del\')}\n                    </button>\n                    <span id="rule-chevron-${gIdx}" class="text-[10px] text-gray-500 transition-transform duration-200 cursor-pointer" onclick="toggleRuleGroup(${gIdx})">▸</span>\n                </div>\n                <div id="rule-subperiods-${gIdx}" class="hidden border-t border-gray-700">\n        `;\n        \n        subPeriods.forEach((sp, sIdx) => {\n            const dayLabel = tDay(DAYS.indexOf(sp.day));\n            const fromStr = String(sp.from).padStart(2, \'0\') + \':00\';\n            const toStr = String(sp.to + 1).padStart(2, \'0\') + \':00\';\n            \n            html += `\n                <div class="flex items-center gap-2 px-3 py-1.5 hover:bg-cyber-bg transition-all">\n                    <span class="w-2 h-2 rounded-full flex-shrink-0" style="background:${color.dot}; opacity:0.6"></span>\n                    <span class="text-[11px] text-gray-300 flex-1">${dayLabel} ${fromStr}–${toStr}</span>\n                    <button onclick="editSinglePeriod(\'${sp.day}\', ${sp.from}, ${sp.to}); event.stopPropagation()"\n                            class="text-[10px] text-gray-400 hover:text-neon-cyan px-1.5 py-0.5 rounded hover:bg-cyber-accent transition-all">\n                        ${t(\'schedule.edit\', \'Edit\')}\n                    </button>\n                    <button onclick="deleteSinglePeriod(\'${sp.day}\', ${sp.from}, ${sp.to}); event.stopPropagation()"\n                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-accent transition-all">\n                        ${t(\'schedule.delete\', \'Del\')}\n                    </button>\n                </div>\n            `;\n        });\n        \n        html += `\n                </div>\n            </div>\n        `;\n    });\n    html += \'</div>\';\n    container.innerHTML = html;\n    container._groups = groupList;\n    \n    // Restore expanded state\n    schedule.expandedRuleGroups.forEach(idx => {\n        const el = document.getElementById(`rule-subperiods-${idx}`);\n        const chevron = document.getElementById(`rule-chevron-${idx}`);\n        if (el) {\n            el.classList.remove(\'hidden\');\n            if (chevron) chevron.textContent = \'▾\';\n        }\n    });\n}\n\nfunction toggleRuleGroup(idx) {\n    const el = document.getElementById(`rule-subperiods-${idx}`);\n    const chevron = document.getElementById(`rule-chevron-${idx}`);\n    if (!el) return;\n    el.classList.toggle(\'hidden\');\n    if (el.classList.contains(\'hidden\')) {\n        schedule.expandedRuleGroups.delete(idx);\n    } else {\n        schedule.expandedRuleGroups.add(idx);\n    }\n    if (chevron) chevron.textContent = el.classList.contains(\'hidden\') ? \'▸\' : \'▾\';\n}\n\nfunction editSinglePeriod(day, fromHour, toHour) {\n    const cells = [];\n    for (let h = fromHour; h <= toHour; h++) {\n        cells.push({ day, hour: h });\n    }\n    openScheduleEditor(cells);\n}\n\nfunction deleteSinglePeriod(day, fromHour, toHour) {\n    for (let h = fromHour; h <= toHour; h++) {\n        const key = `${day}_${String(h).padStart(2, \'0\')}:00`;\n        delete schedule.data[key];\n    }\n    applyScheduleToFan();\n}\n\nfunction editRuleGroup(idx) {\n    const container = document.getElementById(\'schedule-rules\');\n    const group = container._groups[idx];\n    if (!group) return;\n    const cells = group.cells.map(c => ({ day: c.day, hour: parseInt(c.time_start) }));\n    openScheduleEditor(cells);\n}\n\nfunction deleteRuleGroup(idx) {\n    const container = document.getElementById(\'schedule-rules\');\n    const group = container._groups[idx];\n    if (!group) return;\n    group.cells.forEach(cell => {\n        const key = `${cell.day}_${cell.time_start}`;\n        delete schedule.data[key];\n    });\n    schedule.expandedRuleGroups.delete(idx);\n    applyScheduleToFan();\n}\n\nfunction onScheduleMouseDown(e, day, hour) {\n    e.preventDefault();\n    schedule.isDragging = true;\n    schedule.dragStartCell = { day, hour };\n    schedule.selection = [{ day, hour }];\n    highlightSelection();\n}\n\nfunction onScheduleMouseEnter(e, day, hour) {\n    if (!schedule.isDragging || !schedule.dragStartCell) return;\n    \n    const startH = schedule.dragStartCell.hour;\n    const startD = DAYS.indexOf(schedule.dragStartCell.day);\n    const endD = DAYS.indexOf(day);\n    const minD = Math.min(startD, endD);\n    const maxD = Math.max(startD, endD);\n    \n    schedule.selection = [];\n    \n    if (minD === maxD) {\n        // Same day: select hour range\n        const hFrom = Math.min(startH, hour);\n        const hTo = Math.max(startH, hour);\n        for (let h = hFrom; h <= hTo; h++) {\n            schedule.selection.push({ day: DAYS[minD], hour: h });\n        }\n    } else {\n        // Cross-day: select ALL hours on each day in range\n        for (let d = minD; d <= maxD; d++) {\n            for (let h = 0; h < 24; h++) {\n                schedule.selection.push({ day: DAYS[d], hour: h });\n            }\n        }\n    }\n    highlightSelection();\n}\n\nfunction highlightSelection() {\n    clearHighlight();\n    for (const cell of schedule.selection) {\n        const el = document.querySelector(`.schedule-cell[data-day="${cell.day}"][data-hour="${cell.hour}"]`);\n        if (el) {\n            el.style.outline = \'2px solid #00f0ff\';\n            el.style.outlineOffset = \'-1px\';\n            el.style.zIndex = \'1\';\n        }\n    }\n}\n\nfunction clearHighlight() {\n    document.querySelectorAll(\'.schedule-cell\').forEach(el => {\n        el.style.outline = \'\';\n        el.style.outlineOffset = \'\';\n        el.style.zIndex = \'\';\n    });\n}\n\ndocument.addEventListener(\'mouseup\', () => {\n    if (!schedule.isDragging) return;\n    schedule.isDragging = false;\n    \n    if (schedule.selection.length === 1) {\n        openScheduleEditor([schedule.selection[0]]);\n    } else if (schedule.selection.length > 1) {\n        openScheduleEditor([...schedule.selection]);\n    }\n    schedule.selection = [];\n    clearHighlight();\n});\n\n// ============================================================================\n// SCHEDULE EDITOR\n// ============================================================================\n\nfunction openScheduleEditor(cells) {\n    schedule.editingCells = cells;\n    schedule.editorSensors = [];\n    \n    const editor = document.getElementById(\'schedule-editor\');\n    editor.classList.remove(\'hidden\');\n    \n    // Build human-readable period description\n    document.getElementById(\'schedule-editor-cells\').textContent = describeCells(cells);\n    \n    // Get existing data from first cell\n    const key = `${cells[0].day}_${String(cells[0].hour).padStart(2, \'0\')}:00`;\n    const existing = schedule.data[key];\n    \n    if (existing) {\n        setScheduleMode(existing.mode);\n        document.getElementById(\'sched-target-temp\').value = existing.target_temp || 31;\n        document.getElementById(\'sched-speed-slider\').value = existing.speed_pct ?? 50;\n        document.getElementById(\'sched-speed-value\').textContent = `${existing.speed_pct ?? 50}%`;\n        schedule.editorSensors = [...(existing.sensors || [])];\n        if (existing.sensor_mode) setScheduleSensorMode(existing.sensor_mode);\n    } else {\n        setScheduleMode(\'auto\');\n        document.getElementById(\'sched-target-temp\').value = 31;\n        document.getElementById(\'sched-speed-slider\').value = 50;\n        document.getElementById(\'sched-speed-value\').textContent = \'50%\';\n        \n        // Auto-fill sensors from first existing schedule item\n        const fan = store.state?.fans?.[store.currentFanId];\n        const fanSchedule = fan?.schedule || [];\n        if (fanSchedule.length > 0) {\n            const first = fanSchedule[0];\n            schedule.editorSensors = [...(first.sensors || [])];\n            if (first.sensor_mode) setScheduleSensorMode(first.sensor_mode);\n        }\n    }\n    \n    updateScheduleEditorSensors();\n}\n\nfunction setScheduleMode(mode) {\n    const modes = [\'auto\', \'manual\', \'off\'];\n    \n    modes.forEach(m => {\n        const btn = document.getElementById(`sched-btn-${m}`);\n        if (btn) btn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${m === mode ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n    \n    document.getElementById(\'sched-auto-settings\').classList.toggle(\'hidden\', mode !== \'auto\');\n    document.getElementById(\'sched-manual-settings\').classList.toggle(\'hidden\', mode !== \'manual\');\n}\n\nfunction setScheduleSensorMode(sensorMode) {\n    const modes = [\'max\', \'min\', \'avg\'];\n    \n    modes.forEach(m => {\n        const btn = document.getElementById(`sched-btn-sensor-${m}`);\n        if (btn) btn.className = `flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border ${m === sensorMode ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n}\n\nfunction updateScheduleEditorSensors() {\n    const container = document.getElementById(\'sched-sensor-tags\');\n    if (!container) return;\n    \n    if (schedule.editorSensors.length === 0) {\n        container.innerHTML = `<span class="text-xs text-gray-500 italic">${t(\'editor.no_sensors\', \'No sensors assigned\')}</span>`;\n        document.getElementById(\'sched-sensor-mode-section\').classList.add(\'hidden\');\n        return;\n    }\n    \n    container.innerHTML = schedule.editorSensors.map(s => {\n        const sensor = store.allSensors.find(x => x.id === s);\n        const label = sensor ? sensor.label : s;\n        return `\n            <span class="inline-flex items-center gap-1 bg-cyber-accent text-gray-300 text-xs px-2 py-1 rounded-full">\n                ${escapeHtml(label)}\n                <button onclick="removeScheduleSensor(\'${escapeHtml(s)}\')" class="text-neon-red hover:text-red-400 ml-1">&times;</button>\n            </span>\n        `;\n    }).join(\'\');\n    \n    document.getElementById(\'sched-sensor-mode-section\').classList.toggle(\'hidden\', schedule.editorSensors.length <= 1);\n}\n\nfunction removeScheduleSensor(sensorId) {\n    schedule.editorSensors = schedule.editorSensors.filter(s => s !== sensorId);\n    updateScheduleEditorSensors();\n}\n\nfunction toggleScheduleSensorPopup() {\n    const popup = document.getElementById(\'sensor-popup\');\n    const list = document.getElementById(\'sensor-popup-list\');\n    if (!popup || !list) return;\n    \n    if (popup.classList.contains(\'hidden\')) {\n        list.innerHTML = buildSensorCheckboxList(store.allSensors, schedule.editorSensors);\n        popup.classList.remove(\'hidden\');\n        \n        // Override close behavior for schedule context\n        popup._scheduleMode = true;\n    } else {\n        // Collect checked sensors\n        const checked = popup.querySelectorAll(\'input[type=checkbox]:checked\');\n        schedule.editorSensors = Array.from(checked).map(cb => cb.value);\n        updateScheduleEditorSensors();\n        popup.classList.add(\'hidden\');\n        popup._scheduleMode = false;\n    }\n}\n\nfunction saveScheduleEdit() {\n    const mode = document.querySelector(\'#sched-btn-auto.bg-neon-cyan\') ? \'auto\'\n        : document.querySelector(\'#sched-btn-manual.bg-neon-cyan\') ? \'manual\' : \'off\';\n    \n    const newItems = schedule.editingCells.map(cell => {\n        const key = `${cell.day}_${String(cell.hour).padStart(2, \'0\')}:00`;\n        const item = {\n            day: cell.day,\n            time_start: String(cell.hour).padStart(2, \'0\') + \':00\',\n            time_end: String(cell.hour).padStart(2, \'0\') + \':59\',\n            mode: mode\n        };\n        \n        if (mode === \'auto\') {\n            item.target_temp = parseInt(document.getElementById(\'sched-target-temp\').value) || 31;\n            item.sensors = [...schedule.editorSensors];\n            const activeSensorMode = document.querySelector(\'#sched-btn-sensor-max.bg-neon-cyan\') ? \'max\'\n                : document.querySelector(\'#sched-btn-sensor-min.bg-neon-cyan\') ? \'min\' : \'avg\';\n            item.sensor_mode = activeSensorMode;\n        } else if (mode === \'manual\') {\n            item.speed_pct = parseInt(document.getElementById(\'sched-speed-slider\').value) || 50;\n        }\n        \n        schedule.data[key] = item;\n        return item;\n    });\n    \n    closeScheduleEditor();\n    applyScheduleToFan();\n}\n\nfunction deleteScheduleEdit() {\n    for (const cell of schedule.editingCells) {\n        const key = `${cell.day}_${String(cell.hour).padStart(2, \'0\')}:00`;\n        delete schedule.data[key];\n    }\n    closeScheduleEditor();\n    applyScheduleToFan();\n}\n\nfunction closeScheduleEditor() {\n    document.getElementById(\'schedule-editor\').classList.add(\'hidden\');\n    schedule.editingCells = [];\n}\n\nfunction clearSchedule() {\n    schedule.data = {};\n    applyScheduleToFan();\n}\n\nfunction fillScheduleDefaults() {\n    const fan = store.state?.fans?.[store.currentFanId];\n    const defaultSensors = fan?.sensors || [];\n    const defaultSensorMode = fan?.sensor_mode || \'max\';\n    const defaultTemp = fan?.target_temp || 31;\n    \n    for (const day of DAYS) {\n        for (let hour = 0; hour < 24; hour++) {\n            const key = `${day}_${String(hour).padStart(2, \'0\')}:00`;\n            if (!schedule.data[key]) {\n                schedule.data[key] = {\n                    day: day,\n                    time_start: String(hour).padStart(2, \'0\') + \':00\',\n                    time_end: String(hour).padStart(2, \'0\') + \':59\',\n                    mode: \'auto\',\n                    target_temp: defaultTemp,\n                    sensors: [...defaultSensors],\n                    sensor_mode: defaultSensorMode\n                };\n            }\n        }\n    }\n    applyScheduleToFan();\n}\n\nfunction applyScheduleToFan() {\n    const fanSchedule = Object.values(schedule.data);\n    \n    // Update local state immediately so render sees new data\n    if (store.state?.fans?.[store.currentFanId]) {\n        store.state.fans[store.currentFanId].schedule = fanSchedule;\n    }\n    \n    sendControl({\n        action: \'set_fan_config\',\n        fan: store.currentFanId,\n        schedule: fanSchedule\n    });\n    renderScheduleGrid();\n}\n\nfunction describeCells(cells) {\n    if (cells.length === 0) return \'\';\n    if (cells.length === 1) {\n        return `${tDay(DAYS.indexOf(cells[0].day))} ${String(cells[0].hour).padStart(2, \'0\')}:00`;\n    }\n    \n    const days = [...new Set(cells.map(c => c.day))].sort((a, b) => DAYS.indexOf(a) - DAYS.indexOf(b));\n    const hours = [...new Set(cells.map(c => c.hour))].sort((a, b) => a - b);\n    \n    let dayStr = \'\';\n    if (days.length === 7) {\n        dayStr = t(\'schedule.every_day\', \'Every day\');\n    } else if (days.length === 5 && !days.includes(\'sat\') && !days.includes(\'sun\')) {\n        dayStr = t(\'schedule.weekdays\', \'Weekdays\');\n    } else if (days.length === 2 && days.includes(\'sat\') && days.includes(\'sun\')) {\n        dayStr = t(\'schedule.weekends\', \'Weekends\');\n    } else if (days.length <= 3) {\n        dayStr = days.map(d => tDay(DAYS.indexOf(d))).join(\', \');\n    } else {\n        dayStr = t(\'schedule.days\', \'${count} days\').replace(\'${count}\', days.length);\n    }\n    \n    if (hours.length === 24) {\n        return `${dayStr}, 00:00-23:59`;\n    }\n    \n    const minH = String(Math.min(...hours)).padStart(2, \'0\');\n    const maxH = String(Math.max(...hours) + 1).padStart(2, \'0\');\n    return `${dayStr}, ${minH}:00-${maxH.length > 5 ? \'00:00 next day\' : maxH + \':00\'}`;\n}\n\nfunction validateSchedule() {\n    const fan = store.state?.fans?.[store.currentFanId];\n    const fanSchedule = fan?.schedule || [];\n    const coverage = document.getElementById(\'schedule-coverage\');\n    const warning = document.getElementById(\'schedule-incomplete-warning\');\n    const detail = document.getElementById(\'schedule-incomplete-detail\');\n    \n    if (!coverage) return;\n    \n    const total = 7 * 24;\n    const filled = fanSchedule.length;\n    const pct = Math.round((filled / total) * 100);\n    \n    coverage.textContent = `${filled}/${total} (${pct}%)`;\n    coverage.className = pct === 100 ? \'text-xs text-neon-green\' : \'text-xs text-neon-orange\';\n    \n    if (pct < 100) {\n        const emptyDays = [];\n        for (let i = 0; i < DAYS.length; i++) {\n            const dayHours = fanSchedule.filter(s => s.day === DAYS[i]).length;\n            if (dayHours < 24) emptyDays.push(tDay(i));\n        }\n        warning.classList.remove(\'hidden\');\n        detail.textContent = `${t(\'schedule.missing\', \'Missing\')}: ${emptyDays.join(\', \')}. ${t(\'schedule.empty_hours\', \'Empty hours = fan off.\')}`;\n    } else {\n        warning.classList.add(\'hidden\');\n    }\n}\n\n// ============================================================================\n// SETTINGS & LANGUAGE\n// ============================================================================\n\nfunction toggleSettings() {\n    const overlay = document.getElementById(\'settings-overlay\');\n    const panel = document.getElementById(\'settings-panel\');\n    if (!overlay || !panel) return;\n    \n    const isOpen = !panel.classList.contains(\'hidden\');\n    if (isOpen) {\n        overlay.classList.add(\'hidden\');\n        panel.classList.add(\'hidden\');\n    } else {\n        overlay.classList.remove(\'hidden\');\n        panel.classList.remove(\'hidden\');\n        updateLangButtons();\n        updateSettingsUI();\n        fetchLogSettings();\n        fetchTelegramStatus();\n        autoCheckUpdate();\n    }\n}\n\nfunction updateLangButtons() {\n    const enBtn = document.getElementById(\'lang-btn-en\');\n    const ruBtn = document.getElementById(\'lang-btn-ru\');\n    const setupEn = document.getElementById(\'setup-lang-en\');\n    const setupRu = document.getElementById(\'setup-lang-ru\');\n    \n    if (enBtn) enBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${i18n.currentLang === \'en\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (ruBtn) ruBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${i18n.currentLang === \'ru\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (setupEn) setupEn.className = `text-xs px-2 py-1 rounded border transition-all ${i18n.currentLang === \'en\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (setupRu) setupRu.className = `text-xs px-2 py-1 rounded border transition-all ${i18n.currentLang === \'ru\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    \n    updateSettingsUI();\n}\n\nfunction updateSettingsUI() {\n    const s = getSettings();\n    \n    // Temperature unit buttons\n    const celsiusBtn = document.getElementById(\'unit-btn-celsius\');\n    const fahrBtn = document.getElementById(\'unit-btn-fahrenheit\');\n    if (celsiusBtn) celsiusBtn.className = `flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${s.tempUnit === \'celsius\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (fahrBtn) fahrBtn.className = `flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${s.tempUnit === \'fahrenheit\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    \n    // Refresh interval buttons\n    [0, 1000, 5000].forEach(v => {\n        const btn = document.getElementById(`refresh-btn-${v}`);\n        if (btn) btn.className = `flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border ${s.refreshInterval === v ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n    \n    // Compact mode toggle\n    const compactBtn = document.getElementById(\'compact-toggle\');\n    if (compactBtn) {\n        compactBtn.className = s.compactMode\n            ? `w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${BTN_ACTIVE}`\n            : `w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${BTN_INACTIVE}`;\n        compactBtn.querySelector(\'span\').textContent = s.compactMode ? t(\'settings.on\', \'On\') : t(\'settings.off\', \'Off\');\n    }\n    \n    // Apply compact mode to body\n    document.body.classList.toggle(\'compact-mode\', s.compactMode);\n    \n    // Auto-update interval buttons\n    [0, 21600000, 43200000, 86400000].forEach(v => {\n        const btn = document.getElementById(`autoupd-btn-${v}`);\n        if (btn) btn.className = `flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border ${s.autoUpdateCheck === v ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n}\n\n// ============================================================================\n// LOGGING LEVEL\n// ============================================================================\n\nasync function fetchLogSettings() {\n    try {\n        const resp = await fetch(\'/api/logging\');\n        const data = await resp.json();\n        logging.level = data.level || \'INFO\';\n        logging.retention = data.retention_days || 30;\n        updateLogLevelButtons();\n        updateRetentionButtons();\n    } catch (err) { console.error(\'Failed to fetch log settings:\', err); }\n}\n\nfunction updateLogLevelButtons() {\n    [\'DEBUG\', \'INFO\', \'WARNING\', \'ERROR\'].forEach(level => {\n        const btn = document.getElementById(`log-btn-${level}`);\n        if (btn) {\n            btn.className = `flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border ${logging.level === level ? BTN_ACTIVE : BTN_INACTIVE}`;\n        }\n    });\n}\n\nasync function setLogLevel(level) {\n    try {\n        const resp = await fetch(\'/api/logging\', {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ level })\n        });\n        if (resp.ok) {\n            logging.level = level;\n            updateLogLevelButtons();\n        }\n    } catch (err) { console.error(\'Failed to set log level:\', err); }\n}\n\nfunction updateRetentionButtons() {\n    [7, 14, 30, 60, 90, 180, 365].forEach(days => {\n        const btn = document.getElementById(`retention-btn-${days}`);\n        if (btn) {\n            btn.className = `flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px] ${logging.retention === days ? BTN_ACTIVE : BTN_INACTIVE}`;\n        }\n    });\n}\n\nasync function setLogRetention(days) {\n    try {\n        const resp = await fetch(\'/api/logging\', {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ retention_days: days })\n        });\n        if (resp.ok) {\n            logging.retention = days;\n            updateRetentionButtons();\n        }\n    } catch (err) { console.error(\'Failed to set log retention:\', err); }\n}\n\nfunction setTempUnit(unit) {\n    saveSettings({ tempUnit: unit });\n    updateSettingsUI();\n    // Re-render current data\n    if (store.state) updateUI(store.state);\n}\n\nfunction setRefreshInterval(ms) {\n    saveSettings({ refreshInterval: ms });\n    updateSettingsUI();\n}\n\nfunction toggleCompactMode() {\n    const s = getSettings();\n    saveSettings({ compactMode: !s.compactMode });\n    updateSettingsUI();\n}\n\nfunction setAutoUpdateInterval(ms) {\n    saveSettings({ autoUpdateCheck: ms });\n    updateSettingsUI();\n    scheduleAutoUpdate();\n}\n\nfunction scheduleAutoUpdate() {\n    if (timers.autoUpdate) { clearInterval(timers.autoUpdate); timers.autoUpdate = null; }\n    const ms = getSettings().autoUpdateCheck;\n    if (ms > 0) {\n        timers.autoUpdate = setInterval(() => { update.checked = false; autoCheckUpdate(); }, ms);\n    }\n}\n\n// ─── Telegram Notifications ──────────────────────────────────────────\n\nlet tgConfig = { configured: false, enabled: false, events: {} };\n\nasync function fetchTelegramStatus() {\n    try {\n        const resp = await fetch(\'/api/telegram/status\');\n        tgConfig = await resp.json();\n        // Update UI\n        const toggle = document.getElementById(\'tg-enabled\');\n        const tokenInput = document.getElementById(\'tg-bot-token\');\n        const chatInput = document.getElementById(\'tg-chat-id\');\n        if (toggle) toggle.checked = tgConfig.enabled;\n        if (tokenInput) tokenInput.value = tgConfig.has_token ? \'••••••••\' : \'\';\n        if (chatInput) chatInput.value = tgConfig.has_chat_id ? (store.state?.telegram_chat_id || \'\') : \'\';\n        // Update event checkboxes\n        const events = tgConfig.events || {};\n        const fanCb = document.getElementById(\'tg-evt-fan\');\n        const agentCb = document.getElementById(\'tg-evt-agent\');\n        const updateCb = document.getElementById(\'tg-evt-update\');\n        if (fanCb) fanCb.checked = events.fan_health !== false;\n        if (agentCb) agentCb.checked = events.agent_status !== false;\n        if (updateCb) updateCb.checked = events.updates !== false;\n    } catch (err) { console.error(\'Failed to fetch Telegram status:\', err); }\n}\n\nfunction saveTelegramConfig() {\n    const enabled = document.getElementById(\'tg-enabled\')?.checked || false;\n    const tokenInput = document.getElementById(\'tg-bot-token\')?.value || \'\';\n    const chatId = document.getElementById(\'tg-chat-id\')?.value || \'\';\n    const events = {\n        fan_health: document.getElementById(\'tg-evt-fan\')?.checked !== false,\n        agent_status: document.getElementById(\'tg-evt-agent\')?.checked !== false,\n        updates: document.getElementById(\'tg-evt-update\')?.checked !== false,\n    };\n\n    const body = { enabled, events };\n    // Only send token/chat_id if they\'re not placeholder\n    if (tokenInput && tokenInput !== \'••••••••\') body.bot_token = tokenInput;\n    if (chatId) body.chat_id = chatId;\n\n    fetch(\'/api/telegram/config\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify(body),\n    }).then(r => r.json()).then(data => {\n        if (data.status === \'ok\') {\n            showToast(\'Telegram config saved\', \'success\');\n            fetchTelegramStatus();\n        } else {\n            showToast(data.message || \'Save failed\', \'error\');\n        }\n    }).catch(err => showToast(\'Save failed: \' + err.message, \'error\'));\n}\n\nasync function testTelegram() {\n    const btn = document.getElementById(\'tg-test-btn\');\n    const result = document.getElementById(\'tg-test-result\');\n    btn.disabled = true;\n    btn.textContent = \'⏳ Sending...\';\n    result.classList.add(\'hidden\');\n\n    try {\n        const resp = await fetch(\'/api/telegram/test\', { method: \'POST\' });\n        const data = await resp.json();\n        result.classList.remove(\'hidden\');\n        if (data.status === \'ok\') {\n            result.className = \'text-xs text-center text-neon-green\';\n            result.textContent = \'✓ Message sent!\';\n        } else {\n            result.className = \'text-xs text-center text-neon-red\';\n            result.textContent = \'✗ \' + (data.message || \'Failed\');\n        }\n    } catch (err) {\n        result.classList.remove(\'hidden\');\n        result.className = \'text-xs text-center text-neon-red\';\n        result.textContent = \'✗ \' + err.message;\n    } finally {\n        btn.disabled = false;\n        btn.innerHTML = \'📱 <span data-i18n="settings.telegram_test">Отправить тест</span>\';\n        setTimeout(() => { result.classList.add(\'hidden\'); }, 5000);\n    }\n}\n\nwindow.saveTelegramConfig = saveTelegramConfig;\nwindow.testTelegram = testTelegram;\n\n// ─── End Telegram ────────────────────────────────────────────────────\n\nasync function checkForUpdates() {\n    const btn = document.getElementById(\'update-check-btn\');\n    const result = document.getElementById(\'update-result\');\n    const applyBtn = document.getElementById(\'update-apply-btn\');\n    \n    if (btn) {\n        btn.disabled = true;\n        btn.querySelector(\'span\').textContent = t(\'settings.checking\', \'Checking...\');\n    }\n    if (result) result.classList.add(\'hidden\');\n    if (applyBtn) {\n        applyBtn.classList.add(\'hidden\');\n        applyBtn.disabled = true;\n        applyBtn.className = \'hidden w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-500 border-gray-700 mt-2\';\n    }\n    \n    try {\n        const resp = await fetch(\'/api/update/check\');\n        const data = await resp.json();\n        \n        const badge = document.getElementById(\'update-badge\');\n        \n        if (data.has_update) {\n            if (badge) badge.classList.remove(\'hidden\');\n            if (result) {\n                result.classList.remove(\'hidden\');\n                result.className = \'text-xs mt-2 p-3 rounded-lg bg-green-900 bg-opacity-20 border border-green-800 text-neon-green\';\n                result.innerHTML = `\n                    <div class="font-semibold mb-2">${t(\'settings.update_available\', \'Update available\')}</div>\n                    <div class="flex justify-between mb-1"><span class="text-gray-400">${t(\'settings.current_version\', \'Current\')}:</span><span class="font-mono">${escapeHtml(data.current_version || \'?\')}</span></div>\n                    <div class="flex justify-between mb-1"><span class="text-gray-400">${t(\'settings.new_version\', \'New\')}:</span><span class="font-mono text-white font-bold">${escapeHtml(data.remote_version || \'?\')}</span></div>\n                    ${data.commit_message ? `<div class="mt-2 pt-2 border-t border-green-800 text-gray-300">${escapeHtml(data.commit_message)}</div>` : \'\'}`;\n            }\n            if (applyBtn) {\n                applyBtn.classList.remove(\'hidden\');\n                applyBtn.disabled = false;\n                applyBtn.className = \'w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border mt-2 bg-green-900 bg-opacity-30 text-neon-green border-green-700 hover:bg-opacity-50\';\n            }\n        } else {\n            if (badge) badge.classList.add(\'hidden\');\n            if (result) {\n                result.classList.remove(\'hidden\');\n                result.className = \'text-xs mt-2 p-3 rounded-lg bg-cyber-accent border border-cyber-accent text-gray-400\';\n                result.textContent = t(\'settings.up_to_date\', \'System is up to date\');\n            }\n        }\n        return data.has_update;\n    } catch (e) {\n        if (result) {\n            result.classList.remove(\'hidden\');\n            result.className = \'text-xs mt-2 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red\';\n            result.textContent = t(\'settings.update_error\', \'Failed to check for updates\');\n        }\n        return false;\n    } finally {\n        if (btn) {\n            btn.disabled = false;\n            btn.querySelector(\'span\').textContent = t(\'settings.check_update\', \'Check for Updates\');\n        }\n    }\n}\n\nfunction copyAgentToken() {\n    const token = document.getElementById(\'agent-token-value\').textContent;\n    if (token && navigator.clipboard) {\n        navigator.clipboard.writeText(token).then(() => showToast(t(\'toast.token_copied\', \'Token copied!\'), \'success\'));\n    }\n}\n\nfunction openUpdateModal() {\n    const modal = document.getElementById(\'update-modal\');\n    const steps = document.getElementById(\'update-modal-steps\');\n    const progress = document.getElementById(\'update-modal-progress\');\n    const result = document.getElementById(\'update-modal-result\');\n    const applyBtn = document.getElementById(\'update-modal-apply\');\n    const closeBtn = document.getElementById(\'update-modal-close\');\n    \n    const onlineAgentCount = store.nodesData.filter(n => n.status === \'online\').length;\n    const agentStep = onlineAgentCount > 0\n        ? `<div id="upd-step-agents" class="flex items-center gap-3 text-sm opacity-40">\n            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-agents-icon">1</span>\n            <span class="text-gray-300">${t(\'settings.step_agents\', \'Updating agents...\')}</span>\n        </div>`\n        : \'\';\n    const waitStep = onlineAgentCount > 0\n        ? `<div id="upd-step-wait" class="flex items-center gap-3 text-sm opacity-40">\n            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-wait-icon">2</span>\n            <span class="text-gray-300">${t(\'update.wait_agents\', \'Waiting for agents...\')}</span>\n        </div>`\n        : \'\';\n    const serverStepNum = onlineAgentCount > 0 ? \'3\' : \'1\';\n    const restartStepNum = onlineAgentCount > 0 ? \'4\' : \'2\';\n    steps.innerHTML = `\n        ${agentStep}\n        ${waitStep}\n        <div id="upd-step-pull" class="flex items-center gap-3 text-sm ${onlineAgentCount > 0 ? \'opacity-40\' : \'\'}">\n            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-pull-icon">${serverStepNum}</span>\n            <span class="text-gray-300">${t(\'settings.step_pull\', \'Pulling latest code...\')}</span>\n        </div>\n        <div id="upd-step-restart" class="flex items-center gap-3 text-sm opacity-40">\n            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-restart-icon">${restartStepNum}</span>\n            <span class="text-gray-300">${t(\'settings.step_restart\', \'Restarting container...\')}</span>\n        </div>\n    `;\n\n    // Show agents list if there are online agents\n    const agentsSection = document.getElementById(\'update-modal-agents\');\n    const agentsList = document.getElementById(\'update-modal-agents-list\');\n    const onlineAgents = store.nodesData.filter(n => n.status === \'online\');\n    if (agentsSection) {\n        if (onlineAgents.length > 0) {\n            agentsSection.classList.remove(\'hidden\');\n            if (agentsList) {\n                agentsList.innerHTML = onlineAgents.map(agent => {\n                    const ver = agent.agent_version || \'—\';\n                    const serverVer = store.state?.config_version || \'?\';\n                    const needsUpdate = ver !== \'—\' && serverVer !== \'?\' && ver !== serverVer;\n                    const checked = agent.auto_update ? \'checked\' : \'\';\n                    return `\n                        <div class="flex items-center justify-between py-1.5 px-2 rounded bg-cyber-accent border border-gray-700">\n                            <div class="flex items-center gap-2 min-w-0">\n                                <span class="w-2 h-2 rounded-full ${needsUpdate ? \'bg-orange-400\' : \'bg-neon-green\'} flex-shrink-0"></span>\n                                <span class="text-xs text-gray-300 truncate">${escapeHtml(agent.name || agent.node_id)}</span>\n                                <span class="text-[10px] ${needsUpdate ? \'text-orange-400\' : \'text-gray-500\'}">${ver}</span>\n                            </div>\n                            <label class="flex items-center gap-1 cursor-pointer flex-shrink-0">\n                                <input type="checkbox" class="accent-neon-cyan w-3.5 h-3.5" ${checked}\n                                    onchange="toggleAgentAutoUpdate(\'${agent.node_id}\', this.checked)">\n                                <span class="text-[10px] text-gray-400">auto</span>\n                            </label>\n                        </div>`;\n                }).join(\'\');\n            }\n        } else {\n            agentsSection.classList.add(\'hidden\');\n        }\n    }\n    \n    progress.classList.add(\'hidden\');\n    result.classList.add(\'hidden\');\n    applyBtn.disabled = false;\n    applyBtn.classList.remove(\'hidden\');\n    closeBtn.classList.remove(\'hidden\');\n    \n    modal.classList.remove(\'hidden\');\n}\n\nfunction closeUpdateModal() {\n    document.getElementById(\'update-modal\').classList.add(\'hidden\');\n}\n\nasync function toggleAgentAutoUpdate(nodeId, enabled) {\n    try {\n        await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/auto-update`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ enabled }),\n        });\n        // Update local state so startUpdate() respects the change immediately\n        const node = store.nodesData.find(n => n.node_id === nodeId);\n        if (node) node.auto_update = enabled;\n    } catch (err) { console.error(\'Failed to toggle auto-update:\', err); }\n}\n\nfunction setStepState(step, state) {\n    const el = document.getElementById(`upd-step-${step}`);\n    const icon = document.getElementById(`upd-step-${step}-icon`);\n    if (!el || !icon) return;\n    \n    el.classList.remove(\'opacity-40\');\n    \n    if (state === \'active\') {\n        icon.className = \'w-5 h-5 rounded-full border-2 border-neon-cyan flex-shrink-0 flex items-center justify-center text-[10px] text-neon-cyan animate-pulse\';\n        icon.innerHTML = \'⟳\';\n    } else if (state === \'done\') {\n        icon.className = \'w-5 h-5 rounded-full bg-neon-green flex-shrink-0 flex items-center justify-center text-[10px] text-black\';\n        icon.innerHTML = \'✓\';\n    } else if (state === \'error\') {\n        icon.className = \'w-5 h-5 rounded-full bg-neon-red flex-shrink-0 flex items-center justify-center text-[10px] text-white\';\n        icon.innerHTML = \'✕\';\n    }\n}\n\n\nfunction checkAgentsDone() {\n    const pending = Object.entries(update.agentStates).filter(([_, s]) =>\n        ![\'synced\', \'error\', \'skipped\'].includes(s.status));\n    if (pending.length === 0 && update.resolve) {\n        update.resolve();\n        update.resolve = null;\n    }\n}\n\nfunction renderUpdateAgentProgress() {\n    const el = document.getElementById(\'update-modal-agents-progress\');\n    if (!el) return;\n    const serverVer = store.state?.config_version || \'?\';\n    let html = \'\';\n    for (const [nid, st] of Object.entries(update.agentStates)) {\n        const node = store.nodesData.find(n => n.node_id === nid);\n        const name = node?.name || nid;\n        let statusIcon = \'\', statusText = \'\', actions = \'\';\n        switch (st.status) {\n            case \'pending\':\n                statusIcon = \'<span class="w-2 h-2 rounded-full bg-gray-500 animate-pulse"></span>\';\n                statusText = \'<span class="text-gray-400">\' + t(\'update.sending\', \'Sending...\') + \'</span>\';\n                break;\n            case \'pulling\':\n                statusIcon = \'<span class="w-2 h-2 rounded-full bg-neon-cyan animate-pulse"></span>\';\n                statusText = `<span class="text-neon-cyan">${t(\'update.pulling\', \'Pulling code...\')} ${st.version || \'\'}</span>`;\n                break;\n            case \'synced\':\n                statusIcon = \'<span class="w-2 h-2 rounded-full bg-neon-green"></span>\';\n                statusText = `<span class="text-neon-green">${t(\'update.synced\', \'Synced, restarting...\')}</span>`;\n                break;\n            case \'error\':\n                statusIcon = \'<span class="w-2 h-2 rounded-full bg-neon-red"></span>\';\n                statusText = `<span class="text-neon-red">${t(\'update.error\', \'Error\')}: ${escapeHtml(st.message || \'unknown\')}</span>`;\n                actions = `<button onclick="retryAgentUpdate(\'${nid}\')" class="text-[10px] text-neon-cyan hover:underline ml-1">${t(\'update.retry\', \'Retry\')}</button>\n                    <button onclick="skipAgentUpdate(\'${nid}\')" class="text-[10px] text-gray-500 hover:underline ml-1">${t(\'update.skip\', \'Skip\')}</button>`;\n                break;\n            case \'skipped\':\n                statusIcon = \'<span class="w-2 h-2 rounded-full bg-gray-600"></span>\';\n                statusText = `<span class="text-gray-500">${t(\'update.skipped\', \'Skipped\')}</span>`;\n                break;\n            case \'version_mismatch\':\n                statusIcon = \'<span class="w-2 h-2 rounded-full bg-yellow-400"></span>\';\n                statusText = `<span class="text-yellow-400">${t(\'update.version_mismatch\', \'Version mismatch after update\')}</span>`;\n                actions = `<button onclick="retryAgentUpdate(\'${nid}\')" class="text-[10px] text-neon-cyan hover:underline ml-1">${t(\'update.retry\', \'Retry\')}</button>\n                    <button onclick="skipAgentUpdate(\'${nid}\')" class="text-[10px] text-gray-500 hover:underline ml-1">${t(\'update.skip\', \'Skip\')}</button>`;\n                break;\n        }\n        html += `<div class="flex items-center gap-2 py-1 px-2 rounded bg-cyber-accent border border-gray-700 text-xs">\n            ${statusIcon}\n            <span class="text-gray-300 truncate flex-1">${escapeHtml(name)}</span>\n            ${statusText}\n            ${actions}\n            <button onclick="requestAgentLogs(\'${nid}\')" class="text-[10px] text-gray-500 hover:text-neon-cyan ml-1" title="${t(\'update.view_logs\', \'View logs\')}">📋</button>\n        </div>`;\n    }\n    el.innerHTML = html;\n}\n\nfunction retryAgentUpdate(nodeId) {\n    update.agentStates[nodeId] = { status: \'pending\' };\n    renderUpdateAgentProgress();\n    fetch(\'/api/update/agents\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify({ node_ids: [nodeId] }),\n    }).then(r => r.json()).then(data => {\n        if (data.already_ok && data.already_ok.includes(nodeId)) {\n            update.agentStates[nodeId] = { status: \'skipped\' };\n            renderUpdateAgentProgress();\n            checkAgentsDone();\n        }\n    }).catch(err => console.error(\'Agent update retry error:\', err));\n}\n\nfunction skipAgentUpdate(nodeId) {\n    if (update.agentStates[nodeId]) {\n        update.agentStates[nodeId].status = \'skipped\';\n    }\n    renderUpdateAgentProgress();\n    checkAgentsDone();\n}\n\nfunction requestAgentLogs(nodeId) {\n    fetch(`/api/nodes/${encodeURIComponent(nodeId)}/request-logs`, {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify({ lines: 150 }),\n    }).catch(() => {\n        showToast(t(\'update.logs_failed\', \'Failed to request logs\'), \'error\');\n    });\n}\n\nfunction renderAgentLogsModal(nodeId, lines) {\n    const node = store.nodesData.find(n => n.node_id === nodeId);\n    const name = node?.name || nodeId;\n    const overlay = document.createElement(\'div\');\n    overlay.className = \'fixed inset-0 bg-black/70 z-[90] flex items-center justify-center\';\n    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };\n    overlay.innerHTML = `\n        <div class="bg-gray-900 border border-gray-700 rounded-xl w-[700px] max-h-[80vh] flex flex-col shadow-2xl">\n            <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700">\n                <h3 class="text-white font-semibold text-sm">📋 ${escapeHtml(name)} — ${t(\'update.agent_logs\', \'Logs\')}</h3>\n                <div class="flex gap-2">\n                    <button onclick="requestAgentLogs(\'${nodeId}\')" class="text-xs text-gray-400 hover:text-neon-cyan">🔄 ${t(\'discovery.refresh\', \'Refresh\')}</button>\n                    <button onclick="this.closest(\'.fixed\').remove()" class="text-gray-400 hover:text-white text-lg">&times;</button>\n                </div>\n            </div>\n            <pre class="flex-1 overflow-auto p-4 text-[11px] text-gray-300 font-mono whitespace-pre-wrap">${escapeHtml(lines.join(\'\'))}</pre>\n        </div>\n    `;\n    document.body.appendChild(overlay);\n}\n\nasync function startUpdate() {\n    const applyBtn = document.getElementById(\'update-modal-apply\');\n    const progress = document.getElementById(\'update-modal-progress\');\n    const bar = document.getElementById(\'update-modal-bar\');\n    const result = document.getElementById(\'update-modal-result\');\n    const closeBtn = document.getElementById(\'update-modal-close\');\n\n    const serverVer = store.state?.config_version || \'?\';\n    const onlineAgents = store.nodesData.filter(n => n.status === \'online\');\n    // Only update agents with auto_update enabled\n    // auto_update may be boolean or integer (0/1) from SQLite\n    const outdatedAgents = onlineAgents.filter(n => {\n        const au = n.auto_update;\n        return au && au !== 0 && au !== \'0\';\n    });\n\n    applyBtn.classList.add(\'hidden\');\n    closeBtn.classList.add(\'hidden\');\n    progress.classList.remove(\'hidden\');\n    bar.style.width = \'10%\';\n\n    // Step 1: Send update to agents (while server is still running)\n    if (outdatedAgents.length > 0) {\n        setStepState(\'agents\', \'active\');\n        bar.style.width = \'5%\';\n\n        // Initialize agent states\n        update.agentStates = {};\n        outdatedAgents.forEach(n => {\n            update.agentStates[n.node_id] = { status: \'pending\' };\n        });\n        renderUpdateAgentProgress();\n\n        try {\n            const agentResp = await fetch(\'/api/update/agents\', {\n                method: \'POST\',\n                headers: { \'Content-Type\': \'application/json\' },\n                body: JSON.stringify({ node_ids: outdatedAgents.map(n => n.node_id) }),\n            });\n            const agentData = await agentResp.json();\n\n            // Mark agents already at correct version as skipped (not pending)\n            if (agentData.already_ok) {\n                agentData.already_ok.forEach(nid => {\n                    if (update.agentStates[nid]) {\n                        update.agentStates[nid] = { status: \'skipped\' };\n                    }\n                });\n            }\n            // Mark agents with no SID as pending (will update via polling)\n            if (agentData.no_sid) {\n                agentData.no_sid.forEach(nid => {\n                    if (update.agentStates[nid]) {\n                        update.agentStates[nid] = { status: \'pending\' };\n                    }\n                });\n            }\n\n            setStepState(\'agents\', \'done\');\n            bar.style.width = \'15%\';\n\n            // Step 2: Wait for all agents in real-time via WebSocket\n            setStepState(\'wait\', \'active\');\n            result.classList.remove(\'hidden\');\n            result.className = \'text-sm mb-4 p-3 rounded-lg bg-cyan-900/20 border border-cyan-800 text-neon-cyan\';\n            result.innerHTML = `<div class="font-semibold mb-1">${t(\'update.waiting_agents\', \'Waiting for agents to update...\')}</div>\n                <div id="update-modal-agents-progress" class="space-y-1 mt-2"></div>`;\n            renderUpdateAgentProgress();\n\n            // Wait for all agents to finish (no timeout)\n            await new Promise((resolve) => {\n                update.resolve = resolve;\n                // Also check immediately in case all already done\n                checkAgentsDone();\n            });\n\n            const skipped = Object.values(update.agentStates).filter(s => s.status === \'skipped\').length;\n            const errors = Object.values(update.agentStates).filter(s => s.status === \'error\').length;\n            setStepState(\'wait\', errors > 0 && skipped === 0 ? \'error\' : \'done\');\n\n            if (errors > 0 && skipped === 0) {\n                result.innerHTML += `<div class="text-yellow-400 text-xs mt-2">${t(\'update.agents_errors\', \'Some agents had errors. Skip them or retry, then continue.\')}</div>`;\n                bar.style.width = \'35%\';\n                // Show continue button so user can proceed with server update\n                const continueBtn = document.createElement(\'button\');\n                continueBtn.textContent = t(\'update.continue_server\', \'Continue server update\');\n                continueBtn.className = \'mt-2 px-3 py-1.5 bg-neon-cyan/20 border border-neon-cyan/50 rounded text-xs text-neon-cyan hover:bg-neon-cyan/30 transition\';\n                continueBtn.onclick = () => {\n                    continueBtn.remove();\n                    startServerUpdate(bar, result, applyBtn, closeBtn);\n                };\n                result.appendChild(continueBtn);\n                closeBtn.classList.remove(\'hidden\');\n                return;\n            }\n\n            bar.style.width = \'35%\';\n        } catch (e) {\n            console.error(\'Failed to notify agents:\', e);\n            setStepState(\'agents\', \'error\');\n        }\n    }\n\n    // Step 3: Git pull on server\n    await startServerUpdate(bar, result, applyBtn, closeBtn);\n}\n\nasync function startServerUpdate(bar, result, applyBtn, closeBtn) {\n    setStepState(\'pull\', \'active\');\n    bar.style.width = \'40%\';\n\n    try {\n        const resp = await fetch(\'/api/update/apply\', { method: \'POST\' });\n        const data = await resp.json();\n\n        if (data.status === \'error\') {\n            setStepState(\'pull\', \'error\');\n            bar.style.width = \'100%\';\n            bar.className = \'bg-neon-red h-2 rounded-full transition-all duration-500\';\n            result.classList.remove(\'hidden\');\n            result.className = \'text-sm mb-4 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red\';\n            result.textContent = data.message || t(\'settings.update_failed\', \'Update failed\');\n            applyBtn.classList.remove(\'hidden\');\n            applyBtn.disabled = false;\n            closeBtn.classList.remove(\'hidden\');\n            return;\n        }\n\n        setStepState(\'pull\', \'done\');\n        bar.style.width = \'60%\';\n\n        // Step 4: Restart\n        setStepState(\'restart\', \'active\');\n        bar.style.width = \'80%\';\n\n        result.classList.remove(\'hidden\');\n        result.className = \'text-sm mb-4 p-3 rounded-lg bg-green-900 bg-opacity-20 border border-green-800 text-neon-green\';\n        result.innerHTML = `\n            <div class="font-semibold mb-1">${t(\'settings.update_success\', \'Update complete!\')}</div>\n            <div class="text-gray-400">${t(\'settings.restart_notice\', \'Container is restarting. Page will reload in 10 seconds...\')}</div>\n        `;\n\n        bar.style.width = \'100%\';\n        setStepState(\'restart\', \'done\');\n        setTimeout(() => { window.location.reload(); }, RELOAD_DELAY);\n\n    } catch (e) {\n        setStepState(\'pull\', \'error\');\n        bar.style.width = \'100%\';\n        bar.className = \'bg-neon-red h-2 rounded-full transition-all duration-500\';\n        result.classList.remove(\'hidden\');\n        result.className = \'text-sm mb-4 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red\';\n        result.textContent = t(\'settings.update_error\', \'Failed to apply update\');\n        applyBtn.classList.remove(\'hidden\');\n        applyBtn.disabled = false;\n        closeBtn.classList.remove(\'hidden\');\n    }\n}\n\nfunction openUpdateAgentsModal() {\n    const serverVer = store.state?.config_version || \'?\';\n    const outdated = store.nodesData.filter(n =>\n        n.status === \'online\' && n.agent_version && n.agent_version !== serverVer);\n\n    if (outdated.length === 0) {\n        showToast(t(\'nodes.all_up_to_date\', \'All agents are up to date\'), \'info\');\n        return;\n    }\n\n    // Open the full update modal with agent progress tracking\n    openUpdateModal();\n}\n\nasync function updateAgentsNow(nodeIds) {\n    try {\n        showToast(t(\'nodes.updating_agents\', \'Sending update to agents...\'), \'info\');\n        const resp = await fetch(\'/api/update/agents\', {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ node_ids: nodeIds }),\n            signal: AbortSignal.timeout(30000),\n        });\n        if (!resp.ok) {\n            const text = await resp.text().catch(() => \'\');\n            const msg = text.includes(\'<!doctype\') ? `Server error (${resp.status})` : text;\n            showToast(msg || `HTTP ${resp.status}`, \'error\');\n            return;\n        }\n        const data = await resp.json();\n        if (data.status === \'ok\') {\n            showToast(\n                t(\'nodes.agents_update_sent\', \'Update sent to {count} agent(s)\').replace(\'{count}\', data.updated.length),\n                \'success\'\n            );\n        } else {\n            showToast(data.message || t(\'toast.update_failed\', \'Update failed\'), \'error\');\n        }\n    } catch (e) {\n        showToast(t(\'common.error\', \'Error\') + \': \' + e.message, \'error\');\n    }\n}\n\nfunction updateSingleAgent(nodeId) {\n    updateAgentsNow([nodeId]);\n}\n\nasync function autoCheckUpdate() {\n    if (update.checked) return;\n    update.checked = true;\n    await checkForUpdates();\n}\n\nasync function switchLanguage(code) {\n    if (code === i18n.currentLang) return;\n    \n    const success = await loadLang(code);\n    if (success) {\n        updateLangButtons();\n        // Save to server config\n        fetch(\'/api/language\', {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ language: code })\n        }).catch(err => console.error(\'Language save error:\', err));\n        \n        // Re-render dynamic content\n        if (store.currentFanId) {\n            const fan = store.state?.fans?.[store.currentFanId];\n            if (fan) updateInspector(fan);\n        }\n    }\n}\n\n// ============================================================================\n// INITIALIZATION\n// ============================================================================\n\ndocument.addEventListener(\'DOMContentLoaded\', async () => {\n    console.log(\'[FanControl] Neon Cyberpunk Edition initialized\');\n\n    window.addEventListener(\'beforeunload\', () => {\n        if (dashboard.saveTimer) {\n            clearTimeout(dashboard.saveTimer);\n            saveDashboardToServer();\n        }\n    });\n\n    // Load language\n    await loadLang(i18n.currentLang);\n    updateLangButtons();\n    updateSettingsUI();\n    \n    // Click outside to close sensor popup (stop propagation to avoid closing editor underneath)\n    document.getElementById(\'sensor-popup\')?.addEventListener(\'click\', function(e) {\n        e.stopPropagation();\n        if (e.target === this) {\n            closeSensorPopupForContext();\n        }\n    });\n    \n    // Click outside to close schedule editor (only if sensor popup is not open)\n    document.getElementById(\'schedule-editor\')?.addEventListener(\'click\', function(e) {\n        if (e.target === this && document.getElementById(\'sensor-popup\')?.classList.contains(\'hidden\')) {\n            closeScheduleEditor();\n        }\n    });\n    \n    // Schedule speed slider\n    document.getElementById(\'sched-speed-slider\')?.addEventListener(\'input\', (e) => {\n        document.getElementById(\'sched-speed-value\').textContent = `${e.target.value}%`;\n    });\n    \n    // Initial chart load (after short delay to ensure DOM is ready)\n    setTimeout(updateChart, 2000);\n    \n    // Auto-check for updates in background (5s after load)\n    setTimeout(() => autoCheckUpdate(), 5000);\n    \n    // Schedule periodic auto-check\n    scheduleAutoUpdate();\n    \n    // Load nodes for multi-node dashboard\n    loadNodes();\n});\n\n// ============================================================================\n// NODE MANAGEMENT (Multi-node Dashboard)\n// ============================================================================\n\nasync function loadNodes() {\n    try {\n        const resp = await fetch(\'/api/nodes\');\n        store.nodesData = await resp.json();\n        buildServerTree();\n        renderNodesOverview();\n    } catch (e) {\n        console.error(\'[FanControl] Failed to load nodes:\', e);\n    }\n}\n\n// renderNodeSidebar removed — nodes are rendered via buildServerTree/renderRemoteNodeTree\n\nfunction renderNodesOverview() {\n    const container = document.getElementById(\'nodes-grid-inner\');\n    if (!container) return;\n    \n    let html = \'\';\n    for (const node of store.nodesData) {\n        const telemetry = node.telemetry || {};\n        const fans = telemetry.fans || {};\n        const temps = telemetry.temp_sensors || {};\n        const tempValues = Object.values(temps).map(s => (s && s.value) || 0);\n        const maxTemp = tempValues.length > 0 ? Math.max(...tempValues) : 0;\n        const totalRPM = Object.values(fans).reduce((sum, f) => sum + ((f && f.rpm) || 0), 0);\n        \n        html += `\n            <div class="bg-gray-900/50 border border-gray-700 rounded-xl p-4 cursor-pointer hover:border-cyan-500/50 transition-all"\n                 onclick="selectNode(\'${escapeHtml(node.node_id)}\')">\n                <div class="flex items-center justify-between mb-3">\n                    <h3 class="text-white font-semibold">${escapeHtml(node.name)}</h3>\n                    <div class="flex items-center gap-2">\n                        <span class="text-xs ${node.status === \'online\' ? \'text-green-400\' : \'text-gray-500\'}">${node.status}</span>\n                        ${node.control_mode === \'manual\' ? `<span class="text-yellow-400 text-xs">&#9888; ${t(\'node.detail.manual\', \'Manual\')}</span>` : \'\'}\n                    </div>\n                </div>\n                <div class="grid grid-cols-2 gap-2 text-sm">\n                    <div class="text-gray-400">${t(\'nodes.max_temp\', \'Max Temp\')}</div>\n                    <div class="text-white text-right">${maxTemp}&deg;C</div>\n                    <div class="text-gray-400">${t(\'nodes.total_rpm\', \'Total RPM\')}</div>\n                    <div class="text-white text-right">${totalRPM}</div>\n                    <div class="text-gray-400">${t(\'nodes.fans\', \'Fans\')}</div>\n                    <div class="text-white text-right">${Object.keys(fans).length}</div>\n                </div>\n            </div>\n        `;\n    }\n    \n    if (store.nodesData.length === 0) {\n        html = `<div class="text-gray-500 text-center py-8 col-span-2">${t(\'nodes.no_nodes\', \'No nodes connected. Add a node to get started.\')}</div>`;\n    }\n    \n    container.innerHTML = html;\n}\n\nfunction selectNode(nodeId) {\n    store.selectedNodeId = nodeId;\n    store.currentView = \'node-detail\';\n    showView(\'node-detail\');\n    loadNodeDetail(nodeId);\n}\n\nasync function loadNodeDetail(nodeId) {\n    try {\n        const resp = await fetch(`/api/nodes/${nodeId}`);\n        const node = await resp.json();\n        renderNodeDetail(node);\n    } catch (e) {\n        console.error(\'[FanControl] Failed to load node detail:\', e);\n    }\n}\n\nfunction renderNodeDetail(node) {\n    const container = document.getElementById(\'node-detail-inner\');\n    if (!container) return;\n    \n    const telemetry = node.telemetry || {};\n    const fans = telemetry.fans || {};\n    const temps = telemetry.temp_sensors || {};\n    \n    let fansHtml = \'\';\n    for (const [id, fan] of Object.entries(fans)) {\n        const pwm = (fan && fan.pwm_value) || 0;\n        fansHtml += `\n            <div class="bg-gray-800/50 rounded-lg p-3">\n                <div class="flex justify-between text-sm">\n                    <span class="text-gray-400">${escapeHtml(id)}</span>\n                    <span class="text-white">${(fan && fan.rpm) || 0} RPM</span>\n                </div>\n                <div class="mt-1 bg-gray-700 rounded-full h-2">\n                    <div class="bg-cyan-500 h-2 rounded-full" style="width: ${pwm / 255 * 100}%"></div>\n                </div>\n            </div>\n        `;\n    }\n    \n    let tempsHtml = \'\';\n    for (const [id, temp] of Object.entries(temps)) {\n        tempsHtml += `\n            <div class="flex justify-between text-sm">\n                <span class="text-gray-400">${escapeHtml(id)}</span>\n                <span class="text-white">${(temp && temp.value) || 0}&deg;C</span>\n            </div>\n        `;\n    }\n    \n    container.innerHTML = `\n        <div class="flex items-center justify-between mb-6">\n            <div>\n                <h2 class="text-xl font-bold text-white">${escapeHtml(node.name)}</h2>\n                <p class="text-gray-400 text-sm">${node.node_id} &middot; ${node.status} &middot; ${node.control_mode || \'auto\'} mode</p>\n            </div>\n            <div class="flex gap-2">\n                <button onclick="deleteNode(\'${escapeHtml(node.node_id)}\')"\n                    class="px-3 py-1 bg-red-900/30 border border-red-500/30 rounded text-red-400 text-sm hover:bg-red-900/50 transition-all">\n                    ${t(\'nodes.delete\', \'Delete\')}\n                </button>\n                <button onclick="showView(\'nodes\')"\n                    class="px-3 py-1 bg-gray-800 border border-gray-600 rounded text-gray-300 text-sm hover:bg-gray-700 transition-all">\n                    ${t(\'nodes.back\', \'Back\')}\n                </button>\n            </div>\n        </div>\n        \n        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">\n            <div>\n                <h3 class="text-white font-semibold mb-3">${t(\'nodes.fans\', \'Fans\')}</h3>\n                <div class="space-y-2">${fansHtml || `<div class="text-gray-500 text-sm">${t(\'node.detail.no_fans\', \'No fan data\')}</div>`}</div>\n            </div>\n            <div>\n                <h3 class="text-white font-semibold mb-3">${t(\'node.temperatures\', \'Temperatures\')}</h3>\n                <div class="space-y-2">${tempsHtml || `<div class="text-gray-500 text-sm">${t(\'node.detail.no_temps\', \'No temperature data\')}</div>`}</div>\n            </div>\n        </div>\n    `;\n}\n\nfunction showView(view) {\n    store.currentView = view;\n\n    const canvas = document.getElementById(\'dashboard-canvas-container\');\n    const inspector = document.getElementById(\'inspector-container\');\n    const addBtn = document.getElementById(\'dashboard-add-btn\');\n    const groupBtn = document.getElementById(\'dashboard-group-btn\');\n    const nodesGrid = document.getElementById(\'nodes-grid\');\n    const nodeDetail = document.getElementById(\'node-detail-content\');\n    const dsmScheme = document.getElementById(\'dsm-scheme-container\');\n\n    // Hide all views first\n    [canvas, inspector, nodesGrid, nodeDetail, dsmScheme].forEach(el => {\n        if (el) el.classList.add(\'hidden\');\n    });\n    [addBtn, groupBtn].forEach(el => {\n        if (el) el.classList.add(\'hidden\');\n    });\n\n    // Show the requested view\n    if (view === \'dashboard\') {\n        if (canvas) canvas.classList.remove(\'hidden\');\n        if (addBtn) addBtn.classList.remove(\'hidden\');\n        if (groupBtn) groupBtn.classList.remove(\'hidden\');\n    } else if (view === \'inspector\') {\n        if (inspector) inspector.classList.remove(\'hidden\');\n    } else if (view === \'nodes\') {\n        if (nodesGrid) nodesGrid.classList.remove(\'hidden\');\n        renderNodesOverview();\n    } else if (view === \'node-detail\') {\n        if (nodeDetail) nodeDetail.classList.remove(\'hidden\');\n    } else if (view === \'dsm-scheme\') {\n        if (dsmScheme) dsmScheme.classList.remove(\'hidden\');\n        renderDsmSchemeEditor(store.currentRemoteNodeId);\n    }\n\n    // Update nav button styles\n    const dashBtn = document.getElementById(\'nav-dashboard-btn\');\n    if (dashBtn) {\n        if (view === \'dashboard\') {\n            dashBtn.classList.add(\'text-neon-cyan\', \'border-neon-cyan\');\n            dashBtn.classList.remove(\'text-gray-500\', \'border-transparent\');\n        } else {\n            dashBtn.classList.remove(\'text-neon-cyan\', \'border-neon-cyan\');\n            dashBtn.classList.add(\'text-gray-500\', \'border-transparent\');\n        }\n    }\n}\n\nasync function addNode() {\n    const nameInput = document.getElementById(\'new-node-name\');\n    const ipInput = document.getElementById(\'new-node-ip\');\n    const name = nameInput?.value?.trim();\n    const ip = ipInput?.value?.trim();\n    if (!name && !ip) return;\n\n    try {\n        let resp;\n        if (ip) {\n            // Add by IP — probes agent automatically\n            resp = await fetch(\'/api/nodes/add-by-ip\', {\n                method: \'POST\',\n                headers: { \'Content-Type\': \'application/json\' },\n                body: JSON.stringify({ name: name || ip, ip })\n            });\n        } else {\n            resp = await fetch(\'/api/nodes\', {\n                method: \'POST\',\n                headers: { \'Content-Type\': \'application/json\' },\n                body: JSON.stringify({ name })\n            });\n        }\n        if (resp.ok) {\n            nameInput.value = \'\';\n            ipInput.value = \'\';\n            loadNodes();\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            showToast(err.error || t(\'toast.add_node_failed\', \'Failed to add node\'), \'error\');\n        }\n    } catch (e) {\n        console.error(\'[FanControl] Failed to add node:\', e);\n        showToast(t(\'toast.add_node_failed\', \'Failed to add node\') + \': \' + e.message, \'error\');\n    }\n}\n\nasync function deleteNode(nodeId) {\n    if (!confirm(t(\'nodes.confirm_delete\', \'Delete this node?\'))) return;\n    try {\n        const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, { method: \'DELETE\' });\n        if (resp.ok) {\n            if (store.selectedNodeId === nodeId) {\n                store.selectedNodeId = null;\n                showView(\'nodes\');\n            }\n            loadNodes();\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            console.error(\'[FanControl] Delete failed:\', resp.status, err);\n            showToast(t(\'toast.delete_failed\', \'Delete failed\') + \': \' + (err.error || resp.status), \'error\');\n        }\n    } catch (e) {\n        console.error(\'[FanControl] Failed to delete node:\', e);\n        showToast(t(\'toast.delete_failed\', \'Delete failed\') + \': \' + e.message, \'error\');\n    }\n}\n\nfunction showNodeSettings(nodeId) {\n    const node = store.nodesData.find(n => n.node_id === nodeId);\n    if (!node) return;\n    document.getElementById(\'node-settings-id\').value = nodeId;\n    document.getElementById(\'node-settings-name\').value = node.name || \'\';\n    document.getElementById(\'node-settings-ip\').value = node.ip || \'\';\n    document.getElementById(\'node-settings-port\').value = node.port || 5059;\n    const versionEl = document.getElementById(\'node-settings-version\');\n    if (versionEl) {\n        const serverVer = store.state?.config_version || \'?\';\n        const agentVer = node.agent_version || \'—\';\n        const needsUpdate = agentVer !== \'—\' && serverVer !== \'?\' && agentVer !== serverVer;\n        versionEl.textContent = agentVer;\n        versionEl.className = needsUpdate\n            ? \'text-sm text-orange-400\'\n            : agentVer !== \'—\' ? \'text-sm text-neon-green\' : \'text-sm text-gray-500\';\n    }\n    const autoUpdateCb = document.getElementById(\'node-settings-auto-update\');\n    if (autoUpdateCb) {\n        autoUpdateCb.checked = !!node.auto_update;\n        autoUpdateCb.onchange = async () => {\n            try {\n                await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/auto-update`, {\n                    method: \'POST\',\n                    headers: { \'Content-Type\': \'application/json\' },\n                    body: JSON.stringify({ enabled: autoUpdateCb.checked }),\n                });\n            } catch (e) {\n                console.error(\'Failed to toggle auto-update:\', e);\n            }\n        };\n    }\n    document.getElementById(\'node-settings-modal\').classList.remove(\'hidden\');\n}\n\nfunction hideNodeSettings() {\n    document.getElementById(\'node-settings-modal\').classList.add(\'hidden\');\n}\n\nfunction openServerNameEdit() {\n    const input = document.getElementById(\'server-name-input\');\n    input.value = store.state.server_name || \'\';\n    document.getElementById(\'server-name-modal\').classList.remove(\'hidden\');\n    input.focus();\n    input.select();\n}\n\nfunction hideServerNameModal() {\n    document.getElementById(\'server-name-modal\').classList.add(\'hidden\');\n}\n\nasync function saveServerName() {\n    const name = document.getElementById(\'server-name-input\').value.trim();\n    if (!name) { showToast(t(\'toast.name_required\', \'Name required\'), \'error\'); return; }\n\n    try {\n        const resp = await fetch(\'/api/server-name\', {\n            method: \'PUT\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ name })\n        });\n        if (resp.ok) {\n            hideServerNameModal();\n            store.state.server_name = name;\n            showToast(t(\'toast.server_renamed\', \'Server renamed\'), \'success\');\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            showToast(err.error || t(\'toast.save_failed\', \'Save failed\'), \'error\');\n        }\n    } catch (e) {\n        showToast(t(\'toast.save_failed\', \'Save failed\') + \': \' + e.message, \'error\');\n    }\n}\n\nasync function saveNodeSettings() {\n    const nodeId = document.getElementById(\'node-settings-id\').value;\n    const name = document.getElementById(\'node-settings-name\').value.trim();\n    const ip = document.getElementById(\'node-settings-ip\').value.trim();\n    const port = parseInt(document.getElementById(\'node-settings-port\').value) || 5059;\n    if (!name) { showToast(t(\'toast.name_required\', \'Name required\'), \'error\'); return; }\n\n    try {\n        const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {\n            method: \'PUT\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ name, ip, port })\n        });\n        if (resp.ok) {\n            hideNodeSettings();\n            loadNodes();\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            showToast(err.error || t(\'toast.save_failed\', \'Save failed\'), \'error\');\n        }\n    } catch (e) {\n        showToast(t(\'toast.save_failed\', \'Save failed\') + \': \' + e.message, \'error\');\n    }\n}\n\nasync function scanForAgents() {\n    const btn = document.getElementById(\'scan-agents-btn\');\n    const list = document.getElementById(\'discovered-agents-list\');\n    if (!list) return;\n\n    btn.disabled = true;\n    btn.textContent = \'...\';\n    list.classList.remove(\'hidden\');\n    list.innerHTML = `<div class="text-gray-500 text-xs py-1">${t(\'discovery.scanning\', \'Scanning network...\')}</div>`;\n\n    try {\n        const [discoverResp, discoveredResp, subnetResp] = await Promise.all([\n            fetch(\'/api/nodes/discover\'),\n            fetch(\'/api/discovered\'),\n            fetch(\'/api/nodes/scan-subnet\', { method: \'POST\', headers: { \'Content-Type\': \'application/json\' }, body: \'{}\' }),\n        ]);\n\n        const scanResults = await discoverResp.json();\n        const pendingAgents = await discoveredResp.json();\n        const subnetResults = await subnetResp.json();\n\n        // Merge results: SSDP + subnet scan, deduplicate by IP\n        const merged = new Map();\n        for (const a of (Array.isArray(scanResults) ? scanResults : [])) {\n            if (a.ip) merged.set(a.ip, a);\n        }\n        for (const a of (Array.isArray(subnetResults) ? subnetResults : [])) {\n            if (a.ip && !merged.has(a.ip)) merged.set(a.ip, a);\n        }\n        const allAgents = [...merged.values()];\n\n        let html = \'\';\n\n        // Show merged scan results\n        if (allAgents.length > 0) {\n            for (const agent of allAgents) {\n                const label = agent.already_registered\n                    ? `<span class="text-neon-green">online</span> ${escapeHtml(agent.name || agent.node_id)}`\n                    : escapeHtml(agent.name || agent.node_id);\n                const btnLabel = agent.already_registered ? t(\'discovery.refresh\', \'Refresh\') : t(\'discovery.add\', \'+ Add\');\n                const onclick = agent.already_registered\n                    ? `loadNodes(); showToast(t(\'toast.node_refreshed\', \'Node refreshed\'), \'success\')`\n                    : `acceptDiscoveredAgent(\'${escapeHtml(agent.node_id)}\')`;\n                html += `\n                    <div class="flex items-center justify-between bg-gray-800/50 rounded p-1.5 text-xs">\n                        <span class="text-white truncate">${label} <span class="text-gray-500">${escapeHtml(agent.ip || \'\')}</span></span>\n                        <button onclick="${onclick}" class="text-neon-cyan hover:text-cyan-300 px-1">${btnLabel}</button>\n                    </div>\n                `;\n            }\n        }\n\n        // Also show pending discovered agents\n        if (pendingAgents && pendingAgents.length > 0) {\n            for (const agent of pendingAgents) {\n                if (!allAgents.find(a => a.node_id === agent.node_id)) {\n                    html += `\n                        <div class="flex items-center justify-between bg-gray-800/50 rounded p-1.5 text-xs">\n                            <span class="text-white truncate">${escapeHtml(agent.name || agent.node_id)} <span class="text-gray-500">${escapeHtml(agent.ip || \'\')}</span></span>\n                            <button onclick="acceptDiscoveredAgent(\'${escapeHtml(agent.node_id)}\')" class="text-neon-cyan hover:text-cyan-300 px-1">${t(\'discovery.add\', \'+ Add\')}</button>\n                        </div>\n                    `;\n                }\n            }\n        }\n\n        if (!html) {\n            html = \'<div class="text-gray-500 text-xs py-1">\';\n            html += t(\'discovery.no_agents\', \'No agents found. Use IP field below to add manually.\');\n            html += \'</div>\';\n        }\n\n        list.innerHTML = html;\n    } catch (e) {\n        list.innerHTML = `<div class="text-red-400 text-xs py-1">Scan failed: ${e.message}</div>`;\n    }\n\n    btn.disabled = false;\n    btn.textContent = \'\\uD83D\\uDD0D\';\n}\n\nasync function acceptDiscoveredAgent(nodeId) {\n    try {\n        const resp = await fetch(`/api/discovered/${nodeId}/accept`, { method: \'POST\' });\n        if (resp.ok) {\n            showToast(t(\'toast.agent_added\', \'Agent added! Reconnecting...\'), \'success\');\n            loadNodes();\n        }\n    } catch (e) {\n        showToast(t(\'toast.agent_add_error\', \'Failed to add agent\'), \'error\');\n    }\n}\n\nfunction dismissAgentForever(nodeId) {\n    const dismissed = JSON.parse(localStorage.getItem(\'fc_dismissed_agents\') || \'[]\');\n    if (!dismissed.includes(nodeId)) {\n        dismissed.push(nodeId);\n        localStorage.setItem(\'fc_dismissed_agents\', JSON.stringify(dismissed));\n    }\n    showToast(t(\'toast.dismissed\', \'Won\\\'t remind again\'), \'success\');\n}\n\nfunction showConflictModal(data) {\n    const modal = document.getElementById(\'conflict-modal\');\n    if (!modal) return;\n\n    document.getElementById(\'conflict-node-name\').textContent = data.name || data.node_id;\n\n    const serverFans = (data.server_config || {}).fans || {};\n    let serverHtml = \'\';\n    for (const [id, fan] of Object.entries(serverFans)) {\n        serverHtml += `<div class="text-sm"><span class="text-gray-400">${escapeHtml(id)}:</span> <span class="text-white">mode=${fan.mode}, temp=${fan.target_temp}°C</span></div>`;\n    }\n    document.getElementById(\'conflict-server-config\').innerHTML = serverHtml || `<div class="text-gray-500 text-sm">${t(\'conflict.no_config\', \'No config\')}</div>`;\n\n    const agentFans = (data.agent_config || {}).fans || {};\n    let agentHtml = \'\';\n    for (const [id, fan] of Object.entries(agentFans)) {\n        agentHtml += `<div class="text-sm"><span class="text-gray-400">${escapeHtml(id)}:</span> <span class="text-white">mode=${fan.mode}, temp=${fan.target_temp}°C</span></div>`;\n    }\n    document.getElementById(\'conflict-agent-config\').innerHTML = agentHtml || `<div class="text-gray-500 text-sm">${t(\'conflict.no_config\', \'No config\')}</div>`;\n\n    modal.classList.remove(\'hidden\');\n}\n\nfunction hideConflictModal() {\n    document.getElementById(\'conflict-modal\')?.classList.add(\'hidden\');\n    conflict.data = null;\n}\n\nasync function applyServerConfig() {\n    if (!conflict.data) return;\n    try {\n        await fetch(`/api/nodes/${conflict.data.node_id}/config`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ config: conflict.data.server_config })\n        });\n        hideConflictModal();\n    } catch (e) {\n        console.error(\'Failed to apply server config:\', e);\n    }\n}\n\nasync function keepAgentConfig() {\n    if (!conflict.data) return;\n    try {\n        await fetch(`/api/nodes/${conflict.data.node_id}/config`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ config: conflict.data.agent_config })\n        });\n        hideConflictModal();\n    } catch (e) {\n        console.error(\'Failed to keep agent config:\', e);\n    }\n}\n\nfunction showManualModeWarning(nodeId) {\n    const node = store.nodesData.find(n => n.node_id === nodeId);\n    if (!node) return;\n    const warning = document.getElementById(\'manual-mode-warning\');\n    if (!warning) return;\n\n    document.getElementById(\'manual-mode-node-name\').textContent = node.name || nodeId;\n    document.getElementById(\'manual-mode-switch-btn\').onclick = () => switchToServerMode(nodeId);\n    warning.classList.remove(\'hidden\');\n}\n\nfunction hideManualModeWarning() {\n    document.getElementById(\'manual-mode-warning\')?.classList.add(\'hidden\');\n}\n\nasync function switchToServerMode(nodeId) {\n    try {\n        await fetch(`/api/nodes/${nodeId}/mode`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ mode: \'server\' })\n        });\n        hideManualModeWarning();\n    } catch (e) {\n        console.error(\'Failed to switch mode:\', e);\n    }\n}\n\nasync function pushConfigToNode(nodeId) {\n    try {\n        const resp = await fetch(\'/api/state\');\n        const state = await resp.json();\n        await fetch(`/api/nodes/${nodeId}/config`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ config: { fans: state.fans } })\n        });\n    } catch (e) {\n        console.error(\'Failed to push config:\', e);\n    }\n}\n\nconsole.log(\'[FanControl] main.js loaded successfully\');\n\n// ============================================================================\n// DEBUG PANEL\n// ============================================================================\n\nfunction toggleDebugPanel() {\n    debug.open = !debug.open;\n    const panel = document.getElementById(\'debug-panel\');\n    const btn = document.querySelector(\'[title="Debug"]\');\n    if (debug.open) {\n        panel.classList.remove(\'hidden\');\n        btn.classList.add(\'hidden\');\n        renderDebugPanel();\n    } else {\n        panel.classList.add(\'hidden\');\n        btn.classList.remove(\'hidden\');\n    }\n}\n\nfunction renderDebugPanel() {\n    if (!debug.open) return;\n    const el = document.getElementById(\'debug-content\');\n    if (!el) return;\n\n    const saved = getPickerCards();\n    const fans = store.state?.fans || {};\n    const temps = store.state?.temp_sensors || {};\n    const disks = store.state?.hdd_sensors || {};\n\n    let html = \'\';\n\n    // Connection status\n    html += `<div class="mb-3"><span class="text-neon-cyan">Socket.IO:</span> ${socket?.connected ? \'✅ connected\' : \'❌ disconnected\'}</div>`;\n\n    // Cards\n    html += `<div class="mb-3"><span class="text-neon-cyan">Cards (${saved.length}):</span></div>`;\n    for (const card of saved) {\n        const el2 = document.querySelector(`[data-card-id="${card.id}"]`);\n        const w = el2 ? el2.offsetWidth : 0;\n        const h = el2 ? el2.offsetHeight : 0;\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-gray-500">${card.type}</span> `;\n        html += `<span class="text-white">${card.label || card.id.slice(-8)}</span> `;\n        html += `<span class="text-yellow-400">${card.colSpan || 3}x${card.rowSpan || 1}</span> `;\n        html += `<span class="text-gray-600">pos(${card.col},${card.row})</span> `;\n        html += `<span class="text-gray-600">${w}x${h}px</span>`;\n        if (card.lockSize) html += ` <span class="text-red-400">🔒</span>`;\n        html += `</div>`;\n    }\n\n    // Fans\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Fans (${Object.keys(fans).length}):</span></div>`;\n    for (const [id, fan] of Object.entries(fans)) {\n        const spark = getSparkline(`fan:local:${id}`);\n        const last = spark.length ? spark[spark.length - 1] : \'--\';\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-white">${fan.label || id.slice(-8)}</span> `;\n        html += `<span class="text-cyan-400">${fan.rpm || 0} RPM</span> `;\n        html += `<span class="text-gray-600">mode=${fan.mode}</span> `;\n        html += `<span class="text-gray-600">spark=${last}</span>`;\n        html += `</div>`;\n    }\n\n    // Temps\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Temps (${Object.keys(temps).length}):</span></div>`;\n    for (const [id, sensor] of Object.entries(temps)) {\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-white">${sensor.label || id}</span> `;\n        html += `<span class="text-green-400">${sensor.value || \'--\'}°C</span>`;\n        html += `</div>`;\n    }\n\n    // Disks\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Disks (${Object.keys(disks).length}):</span></div>`;\n    for (const [id, disk] of Object.entries(disks)) {\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-white">${disk.name || id}</span> `;\n        html += `<span class="text-purple-400">${disk.temp || \'--\'}°C</span>`;\n        html += `</div>`;\n    }\n\n    // Sparkline stats\n    const sparkKeys = Object.keys(sparklineHistory);\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Sparklines (${sparkKeys.length}):</span></div>`;\n    for (const key of sparkKeys.slice(0, 10)) {\n        const data = sparklineHistory[key];\n        html += `<div class="ml-2 mb-1"><span class="text-gray-500">${key}:</span> <span class="text-gray-400">${data.length} pts, last=${data[data.length-1]}</span></div>`;\n    }\n\n    el.innerHTML = html;\n    requestAnimationFrame(() => { if (debug.open) renderDebugPanel(); });\n}\n\n\n// ============================================================================\n// WINDOW EXPORTS (for onclick handlers in HTML)\n// ============================================================================\n\nwindow.selectFan = selectFan;\nwindow.setFanMode = setFanMode;\nwindow.sendControl = sendControl;\nwindow.toggleSettings = toggleSettings;\nwindow.showView = showView;\nwindow.addNode = addNode;\nwindow.scanForAgents = scanForAgents;\nwindow.openUpdateModal = openUpdateModal;\nwindow.openUpdateAgentsModal = openUpdateAgentsModal;\nwindow.copyAgentToken = copyAgentToken;\nwindow.showCardPicker = showCardPicker;\nwindow.showGroupCreator = showGroupCreator;\nwindow.hideCardPicker = hideCardPicker;\nwindow.addSelectedCards = addSelectedCards;\nwindow.hideCardEdit = hideCardEdit;\nwindow.saveCardEdit = saveCardEdit;\nwindow.hideCardConfig = hideCardConfig;\nwindow.refreshSmartData = refreshSmartData;\nwindow.hideSmartModal = hideSmartModal;\nwindow.saveSmartSelection = saveSmartSelection;\nwindow.hideGroupCreator = hideGroupCreator;\nwindow.createGroup = createGroup;\nwindow.toggleDebugPanel = toggleDebugPanel;\nwindow.runDiscovery = runDiscovery;\nwindow.selectControlMode = selectControlMode;\nwindow.runCalibration = runCalibration;\nwindow.applyDsmAndContinue = applyDsmAndContinue;\nwindow.openServerNameEdit = openServerNameEdit;\nwindow.switchLanguage = switchLanguage;\nwindow.setTempUnit = setTempUnit;\nwindow.setRefreshInterval = setRefreshInterval;\nwindow.toggleCompactMode = toggleCompactMode;\nwindow.checkForUpdates = checkForUpdates;\nwindow.applyServerConfig = applyServerConfig;\nwindow.keepAgentConfig = keepAgentConfig;\nwindow.hideConflictModal = hideConflictModal;\nwindow.saveNodeSettings = saveNodeSettings;\nwindow.hideNodeSettings = hideNodeSettings;\nwindow.saveServerName = saveServerName;\nwindow.hideServerNameModal = hideServerNameModal;\nwindow.hideManualModeWarning = hideManualModeWarning;\nwindow.showServiceFanModal = showServiceFanModal;\nwindow.recordFanService = recordFanService;\nwindow.startCalibration = startCalibration;\nwindow.clearSchedule = clearSchedule;\nwindow.fillScheduleDefaults = fillScheduleDefaults;\nwindow.closeSensorPopupForContext = closeSensorPopupForContext;\nwindow.setScheduleMode = setScheduleMode;\nwindow.setScheduleSensorMode = setScheduleSensorMode;\nwindow.toggleScheduleSensorPopup = toggleScheduleSensorPopup;\nwindow.saveScheduleEdit = saveScheduleEdit;\nwindow.deleteScheduleEdit = deleteScheduleEdit;\nwindow.closeScheduleEditor = closeScheduleEditor;\nwindow.setLogLevel = setLogLevel;\nwindow.setLogRetention = setLogRetention;\nwindow.setAutoUpdateInterval = setAutoUpdateInterval;\nwindow.startUpdate = startUpdate;\nwindow.closeUpdateModal = closeUpdateModal;\nwindow.selectFanFromTree = selectFanFromTree;\nwindow.selectNodeFan = selectNodeFan;\nwindow.selectNode = selectNode;\nwindow.toggleNodeGroup = toggleNodeGroup;\nwindow.showNodeSettings = showNodeSettings;\nwindow.deleteNode = deleteNode;\nwindow.restoreSensor = restoreSensor;\nwindow.hideSensor = hideSensor;\nwindow.startGroupRename = startGroupRename;\nwindow.removePickerGroup = removePickerGroup;\nwindow.updateSingleAgent = updateSingleAgent;\nwindow.acceptDiscoveredAgent = acceptDiscoveredAgent;\nwindow.dismissAgentForever = dismissAgentForever;\nwindow.retryAgentUpdate = retryAgentUpdate;\nwindow.skipAgentUpdate = skipAgentUpdate;\nwindow.requestAgentLogs = requestAgentLogs;\nwindow.applyDsmScheme = applyDsmScheme;\nwindow.editDsmEntry = editDsmEntry;\nwindow.startFanCalibration = startFanCalibration;\nwindow.editSinglePeriod = editSinglePeriod;\nwindow.deleteSinglePeriod = deleteSinglePeriod;\nwindow.removeScheduleSensor = removeScheduleSensor;\nwindow.toggleRuleGroup = toggleRuleGroup;\nwindow.editRuleGroup = editRuleGroup;\nwindow.deleteRuleGroup = deleteRuleGroup;\nwindow.updateCalibrationParam = updateCalibrationParam;\nwindow.onSmartUnitChange = onSmartUnitChange;\nwindow.toggleAgentAutoUpdate = toggleAgentAutoUpdate;\nwindow.updatePickerElements = updatePickerElements;\nwindow.onScheduleMouseDown = onScheduleMouseDown;\nwindow.onScheduleMouseEnter = onScheduleMouseEnter;\n',
    'ru': '{\n  "app.title": "FanControl",\n  "app.subtitle": "Neon Cyberpunk Edition",\n  "setup.heading": "Начальная настройка системы",\n  "setup.description": "Конфигурация не найдена. Системе необходимо сканировать доступные шины данных для автоматического обнаружения вентиляторов и датчиков температуры.",\n  "setup.scan_btn": "Начать сканирование оборудования",\n  "setup.scanning": "Сканирование шины sysfs и запрос smartctl...",\n  "setup.results_title": "Оборудование обнаружено",\n  "setup.calibrate_hint": "Для завершения настройки необходимо откалибровать вентиляторы. Это занимает около 1-2 минут.",\n  "setup.calibrate_btn": "Начать калибровку вентиляторов",\n  "setup.calibrating": "Калибровка: определение кривых PWM/RPM...",\n  "setup.controllable": "Управляемый",\n  "setup.readonly": "Только чтение",\n  "setup.not_calibrated": "Не откалиброван",\n  "setup.fans_header": "Вентиляторы",\n  "setup.sensors_header": "Датчики температуры",\n  "setup.disks_header": "Диски хранения",\n  "setup.loading_fans": "Загрузка вентиляторов...",\n  "setup.no_fans": "Вентиляторы не обнаружены",\n  "setup.no_disks": "Диски не обнаружены",\n  "setup.no_hardware": "Оборудование не обнаружено",\n  "setup.calibrate_btn_short": "Перекалибровать",\n  "header.synced": "Синхронизировано",\n  "header.storage": "Хранилище",\n  "header.settings": "Настройки",\n  "inspector.select": "Выберите устройство",\n  "inspector.hint": "Нажмите на вентилятор для просмотра",\n  "inspector.fallback_id": "ID: неизвестен",\n  "inspector.fan_speed": "Скорость вентилятора",\n  "inspector.status": "Статус",\n  "inspector.mode": "Режим",\n  "inspector.hint_detail": "для просмотра управления и аналитики",\n  "inspector.fan_name": "Имя вентилятора",\n  "mode.manual": "Ручной",\n  "mode.auto": "Авто",\n  "status.nominal": "норма",\n  "status.warning": "внимание",\n  "status.critical": "критично",\n  "status.failsafe": "аварийный",\n  "status.standby": "ожидание",\n  "status.inverted": "инвертированный",\n  "status.no_sensor": "нет датчика",\n  "status.not_tested": "не тестирован",\n  "status.calibrating": "калибровка",\n  "status.not_connected": "не подключён",\n  "status.normal": "нормальный",\n  "status.manual": "ручной",\n  "status.off": "выкл",\n  "status.fixed": "фиксир.",\n  "status.low": "тихий",\n  "status.stopped": "ОСТАНОВЛЕН",\n  "status.slowing": "ЗАМЕДЛЕН",\n  "status.needs_calibration": "НУЖНА КАЛИБРОВКА",\n  "schedule.weekly": "Недельное расписание",\n  "schedule.incomplete": "Расписание неполное",\n  "schedule.no_sensor_title": "Датчики не назначены",\n  "schedule.no_sensor_hint": "Назначьте датчики в первой ячейке расписания или глобально ниже.",\n  "schedule.legend_auto": "Авто",\n  "schedule.legend_manual": "Ручной",\n  "schedule.legend_off": "Выкл",\n  "schedule.legend_empty": "Пусто",\n  "schedule.clear_all": "Очистить всё",\n  "schedule.fill_auto": "Заполнить пустые Авто",\n  "schedule.no_rules": "Правила не настроены",\n  "schedule.every_day": "Каждый день",\n  "schedule.weekdays": "Будни",\n  "schedule.weekends": "Выходные",\n  "schedule.days": "${count} дней",\n  "schedule.periods": "периодов",\n  "schedule.period": "период",\n  "schedule.hours_short": "ч",\n  "schedule.missing": "Пропущено",\n  "schedule.empty_hours": "Пустые часы = вентилятор выключен.",\n  "editor.title": "Редактирование расписания",\n  "editor.period": "Период",\n  "editor.mode": "Режим",\n  "editor.target_temp": "Целевая температура",\n  "editor.sensors": "Датчики",\n  "editor.add_sensor": "Добавить датчик",\n  "editor.temp_mode": "Режим температуры",\n  "editor.fan_speed": "Скорость вентилятора",\n  "editor.apply": "Применить",\n  "editor.delete": "Удалить",\n  "editor.cancel": "Отмена",\n  "editor.max": "Макс",\n  "editor.min": "Мин",\n  "editor.average": "Среднее",\n  "editor.no_sensors": "Датчики не назначены",\n  "sensor.title": "Выбор датчиков",\n  "sensor.done": "Готово",\n  "sensor.sleep": "Сон",\n  "calibration.title": "Калибровка вентиляторов",\n  "calibration.status": "Запуск...",\n  "calibration.step": "Шаг",\n  "calibration.step_label": "Шаг ${current}/${total}",\n  "calibration.ready": "Готово!",\n  "calibration.errors": "Завершено с ошибками",\n  "calibration.confirm": "Перекалибровать все вентиляторы? Это займёт 1-2 минуты.",\n  "chart.temp_history": "История температур (24ч)",\n  "chart.max_hdd_temp": "Макс. темп. HDD",\n  "chart.avg_pwm": "Средний PWM",\n  "fan.inv": "ИНВ",\n  "fan.inverted": "ИНВЕРТИРОВАН",\n  "fan.failsafe": "АВАРИЙНЫЙ",\n  "fan.standby": "ОЖИДАНИЕ",\n  "fan.rpm": "об/мин",\n  "fan.service": "Обслуживание",\n  "fan.replace": "Замена",\n  "fan.service_date": "Последнее обслуживание",\n  "fan.service_done": "Обслуживание записано. Рекомендуется калибровка.",\n  "fan.calibration_required": "Требуется калибровка после обслуживания",\n  "fan.health.stopped": "Вентилятор остановлен — проверьте питание и подключения",\n  "fan.health.slowing": "Вентилятор замедляется — возможный износ подшипника. Замените для предотвращения перегрева.",\n  "fan.health.needs_calibration": "Вентилятор заменён/обслужен — откалибруйте для точного управления скоростью.",\n  "discover.scan_error": "Ошибка сканирования: ",\n  "discover.connection_error": "Ошибка подключения при сканировании",\n  "settings.title": "Настройки",\n  "settings.language": "Язык",\n  "settings.language_hint": "Выберите предпочитаемый язык",\n  "settings.temp_unit": "Единицы температуры",\n  "settings.temp_unit_hint": "Цельсий или Фаренгейт",\n  "settings.refresh": "Интервал обновления",\n  "settings.refresh_hint": "Снизить нагрузку CPU уменьшением частоты обновлений",\n  "settings.refresh_realtime": "Реалтайм",\n  "settings.compact": "Компактный режим",\n  "settings.compact_hint": "Уменьшенные карточки для маленьких экранов",\n  "settings.on": "Вкл",\n  "settings.off": "Выкл",\n  "settings.update": "Обновление системы",\n  "settings.update_hint": "Проверить и применить обновления из Git",\n  "settings.check_update": "Проверить обновления",\n  "settings.checking": "Проверка...",\n  "settings.up_to_date": "Система обновлена",\n  "settings.update_error": "Не удалось проверить обновления",\n  "settings.apply_update": "Применить и перезапустить",\n  "settings.update_agents": "Также обновить всех подключённых агентов",\n  "settings.connected_agents": "Подключённые агенты",\n  "settings.update_agents_hint": "Рекомендуется: обновить все узлы до одной версии для избежания конфликтов конфигураций.",\n  "settings.updating": "Обновление...",\n  "settings.update_applied": "Обновление применено. Контейнер перезапускается...",\n  "settings.update_failed": "Обновление не удалось",\n  "settings.update_confirm": "Обновление перезапустит контейнер. Продолжить?",\n  "settings.update_available": "Доступно обновление",\n  "settings.current_version": "Текущая",\n  "settings.new_version": "Новая",\n  "settings.restarting": "Контейнер перезапускается...",\n  "settings.rebuilding": "Зависимости изменились, пересборка образа...",\n  "settings.update_modal_title": "Обновление системы",\n  "settings.step_agents": "Обновление агентов...",\n  "settings.step_pull": "Загрузка последних изменений...",\n  "settings.step_deps": "Проверка зависимостей...",\n  "settings.step_deps_ok": "Зависимости не изменились",\n  "settings.step_restart": "Перезапуск контейнера...",\n  "settings.update_success": "Обновление завершено!",\n  "settings.restart_notice": "Контейнер перезапускается. Страница обновится через 10 секунд...",\n  "settings.update_host_hint": "Выполните на хосте для применения:",\n  "settings.logging": "Уровень логирования",\n  "settings.logging_hint": "Управление детализацией логов. WARNING значительно уменьшает размер файла логов.",\n  "settings.log_retention": "Хранение логов",\n  "settings.log_retention_hint": "Срок хранения логов до автоматической очистки.",\n  "common.edit": "Изм.",\n  "common.del": "Удл.",\n  "common.save": "Сохранить",\n  "common.apply": "Применить",\n  "common.cancel": "Отмена",\n  "common.delete": "Удалить",\n  "common.done": "Готово",\n  "tooltip.auto_mode": "Авто: скорость вентилятора регулируется автоматически на основе датчиков температуры и расписания",\n  "tooltip.manual_mode": "Ручной: установите скорость вентилятора вручную с помощью ползунка",\n  "tooltip.fan_speed": "Установите скорость от 0% (выкл) до 100% (максимум)",\n  "tooltip.target_temp": "Целевая температура — вентилятор будет поддерживать эту температуру",\n  "tooltip.sensor_mode_max": "Использовать максимальную температуру из всех назначенных датчиков",\n  "tooltip.sensor_mode_min": "Использовать минимальную температуру из всех назначенных датчиков",\n  "tooltip.sensor_mode_avg": "Использовать среднюю температуру из всех назначенных датчиков",\n  "tooltip.schedule_grid": "Нажмите или перетащите для выбора ячеек, затем настройте поведение вентилятора для каждого периода",\n  "tooltip.inverted": "Этот вентилятор имеет инвертированное управление PWM — более высокие значения PWM дают меньшие обороты",\n  "days.mon": "Пн",\n  "days.tue": "Вт",\n  "days.wed": "Ср",\n  "days.thu": "Чт",\n  "days.fri": "Пт",\n  "days.sat": "Сб",\n  "days.sun": "Вс",\n  "sensors.disks": "Диски",\n  "sensors.sensors_group": "Датчики",\n  "nav.dashboard": "Дашборд",\n  "nav.nodes": "Узлы",\n  "nodes.title": "Узлы",\n  "nodes.name_placeholder": "Имя узла",\n  "nodes.no_nodes": "Нет подключённых узлов",\n  "nodes.add": "Добавить узел",\n  "nodes.delete": "Удалить",\n  "nodes.back": "Назад",\n  "nodes.max_temp": "Макс. темп.",\n  "nodes.total_rpm": "Общий RPM",\n  "nodes.fans": "вентиляторов",\n  "nodes.confirm_delete": "Удалить этот узел?",\n  "nodes.update_agents": "Обновить агенты",\n  "nodes.update_n_agents": "Обновить {count} агент(ов) до версии {version}?",\n  "nodes.updating_agents": "Отправка обновления агентам...",\n  "nodes.agents_update_sent": "Обновление отправлено {count} агент(ам)",\n  "nodes.all_up_to_date": "Все агенты обновлены",\n  "nodes.agent_version": "Версия",\n  "nodes.auto_update": "Автообновление",\n  "nodes.click_to_update": "нажмите для обновления",\n  "node.temperatures": "Температуры",\n  "node.manual_mode": "Ручной режим",\n  "conflict.title": "Конфликт конфигураций",\n  "conflict.desc": "Конфигурация агента отличается от серверной.",\n  "conflict.no_config": "Нет конфигурации",\n  "conflict.server_config": "Серверная конфигурация",\n  "conflict.agent_config": "Конфигурация агента",\n  "conflict.apply_server": "Применить серверную",\n  "conflict.keep_agent": "Оставить конфигурацию агента",\n  "conflict.manual_mode": "Ручной режим",\n  "conflict.manual_warning": "Агент управляет вентиляторами локально.",\n  "conflict.switch_to_server": "Переключить на серверное управление",\n  "calibration.pwm_range": "Диапазон PWM",\n  "calibration.pwm_range_hint": "Переопределяет автоматически рассчитанное значение. Перекалибруйте для восстановления.",\n  "calibration.min_pwm": "Мин",\n  "calibration.max_pwm": "Макс",\n  "calibration.curve_shape": "Форма кривой",\n  "calibration.lambda_hint": "Управляет формой кривой вентилятора. 1.0 = линейно. Меньше = вентилятор быстрее набирает обороты на низких %. Больше = вентилятор дольше тихий.",\n  "nav.settings": "Настройки",\n  "dashboard.empty": "Дашборд пуст",\n  "dashboard.empty_hint": "Нажмите + чтобы добавить карточки мониторинга",\n  "dashboard.add_card": "Добавить карточку",\n  "dashboard.add_group": "Добавить группу",\n  "nodes.local_server": "Мой сервер",\n  "nodes.sensors": "датчиков",\n  "nodes.disks": "дисков",\n  "picker.type": "Тип",\n  "picker.fan": "🌀 Вентилятор",\n  "picker.temperature": "🌡 Температура",\n  "picker.disk": "💾 Диск",\n  "picker.system": "📊 Система",\n  "picker.source": "Источник",\n  "picker.my_server": "Мой сервер (локально)",\n  "picker.element": "Элемент",\n  "picker.add": "Добавить",\n  "picker.no_elements": "Элементы не найдены",\n  "picker.added": "добавлено",\n  "picker.max_temp": "Макс. температура",\n  "picker.fans_summary": "Сводка по вентиляторам",\n  "picker.edit_card": "Редактировать карточку",\n  "picker.title": "Заголовок",\n  "picker.title_placeholder": "Название карточки",\n  "picker.card_display": "Отображение карточки",\n  "picker.close": "Закрыть",\n  "control.choose_mode": "Выберите способ управления вентиляторами:",\n  "control.direct": "Прямое управление",\n  "control.direct_desc": "Прямое управление скоростью через Linux sysfs. Требует калибровки для определения кривых RPM.",\n  "control.dsm_scheme": "Схема DSM",\n  "control.dsm_scheme_desc": "Управление вентиляторами через редактирование пороговых схем температуры DSM. Калибровка не нужна.",\n  "control.dsm_scheme_warning_title": "Управление схемой DSM",\n  "control.dsm_scheme_warning_desc": "Прямое управление скоростью недоступно в этом режиме. Настройте пороги температуры и соответствующие скорости вентиляторов через таблицу схемы DSM.",\n  "control.open_dsm_editor": "Открыть редактор схем DSM",\n  "inspector.back": "← Назад к панели мониторинга",\n  "calibration.starting": "Запуск...",\n  "calibration.determining": "Калибровка: определение кривых PWM/RPM...",\n  "schedule.no_sensor_warning": "Датчики не назначены",\n  "node.settings": "Настройки узла",\n  "node.name": "Имя",\n  "node.ip": "IP-адрес",\n  "node.port": "Порт",\n  "node.save": "Сохранить",\n  "node.cancel": "Отмена",\n  "node.server_name": "Имя сервера",\n  "node.standalone_banner": "Сервер недоступен — работа в автономном режиме",\n  "token.title": "API-токен (вставьте на сервере)",\n  "token.agent": "Токен агента",\n  "token.agent_hint": "— вставьте в настройках узла сервера",\n  "token.copy": "Копировать",\n  "token.sidebar_hint": "Также виден в левой панели",\n  "update.off": "Выкл",\n  "update.auto_agents": "${count} агент(ов) будут обновляться автоматически.",\n  "smart.title": "Данные SMART",\n  "group.name_placeholder": "Название группы (напр., Охлаждение CPU)",\n  "group.create": "Создать",\n  "dsm.loading": "Загрузка схем DSM...",\n  "dsm.node_not_found": "Узел не найден",\n  "dsm.load_failed": "Ошибка загрузки схем",\n  "dsm.no_schemes": "Схемы вентиляторов не найдены в scemd.xml",\n  "dsm.title": "Схемы вентиляторов DSM",\n  "dsm.back": "Назад к панели мониторинга",\n  "dsm.active": "Активна",\n  "dsm.hibernation_stop": "Режим гибернации: СТОП",\n  "dsm.apply": "Применить",\n  "dsm.col_sensor": "Датчик",\n  "dsm.col_speed": "Скорость",\n  "dsm.col_action": "Действие",\n  "dsm.col_threshold": "Порог",\n  "dsm.col_edit": "Ред.",\n  "dsm.no_entries": "Нет записей",\n  "dsm.error_loading": "Ошибка загрузки схем DSM",\n  "dsm.apply_remote": "Схема применена к удалённому агенту",\n  "dsm.apply_ok": "Схема успешно применена",\n  "dsm.apply_failed": "Не удалось применить схему",\n  "dsm.apply_error": "Ошибка применения схемы",\n  "dsm.prompt_speed": "Скорость вентилятора % для ${sensor} (порог ${temp}°C):",\n  "dsm.prompt_action": "Действие (NONE или SHUTDOWN):",\n  "dsm.prompt_threshold": "Пороговая температура °C:",\n  "dsm.entry_failed": "Не удалось обновить запись",\n  "hw.custom_arc": "Пользовательский ARC",\n  "hw.official_synology": "Официальный Synology",\n  "hw.unknown": "Неизвестно",\n  "hw.hwmon_pwm": "hwmon (PWM)",\n  "hw.scedm_api": "scemd.xml (DSM API)",\n  "hw.none": "нет",\n  "hw.kernel": "Ядро:",\n  "hw.fan_control": "Управление вентиляторами:",\n  "hw.fans_section": "🌀 Вентиляторы",\n  "hw.controllable": "Управляемые",\n  "hw.readonly": "Только чтение",\n  "hw.not_calibrated": "Не откалиброван",\n  "hw.sensors_section": "🌡️ Датчики температуры",\n  "hw.disks_section": "💾 Диски",\n  "hw.no_hwmon_hint": "hwmon PWM недоступен на этом ядре — только управление схемой DSM.",\n  "hw.no_dsm_hint": "Схемы DSM не найдены — только управление hwmon.",\n  "hw.no_control": "Способ управления вентиляторами недоступен.",\n  "discovery.scanning": "Сканирование сети...",\n  "discovery.refresh": "Обновить",\n  "discovery.add": "+ Добавить",\n  "discovery.no_agents": "Агенты не найдены. Используйте поле IP ниже для ручного добавления.",\n  "discovery.scan_failed": "Ошибка сканирования",\n  "discovery.token_copied": "Токен скопирован!",\n  "toast.token_copied": "Токен скопирован!",\n  "toast.name_required": "Требуется имя",\n  "toast.server_renamed": "Сервер переименован",\n  "toast.save_failed": "Ошибка сохранения",\n  "toast.update_failed": "Ошибка обновления",\n  "toast.add_node_failed": "Не удалось добавить узел",\n  "toast.delete_failed": "Ошибка удаления",\n  "toast.agent_connected": "Агент подключён: ${name} (${ip})",\n  "node.detail.no_fans": "Нет данных о вентиляторах",\n  "node.detail.no_temps": "Нет данных о температуре",\n  "node.detail.manual": "Ручной",\n  "schedule.edit": "Ред.",\n  "schedule.delete": "Удл.",\n  "schedule.next_day": "00:00 следующий день",\n  "fan.status_label": "Состояние вентилятора:",\n  "fan.controllable": "Управляемый",\n  "fan.readonly_label": "Только чтение",\n  "calibration.started": "Калибровка начата...",\n  "common.error": "Ошибка",\n  "common.later": "Позже",\n  "inspector.calibrate": "Калибровать",\n  "inspector.sensors": "Датчики:",\n  "inspector.target": "Цель:",\n  "node.no_telemetry": "Нет телеметрии",\n  "nodes.all": "все",\n  "nodes.hidden": "Скрытые",\n  "nodes.restore": "Восстановить",\n  "toast.add": "Добавить",\n  "toast.agent_add_error": "Ошибка добавления агента",\n  "toast.agent_added": "Агент добавлен! Переподключение...",\n  "toast.dismiss": "Не напоминать",\n  "toast.dismissed": "Больше не напоминать",\n  "toast.new_agent": "Новый агент: ",\n  "toast.node_refreshed": "Узел обновлён",\n  "toast.speed_failed": "Не удалось установить скорость",\n  "toast.control_error": "Ошибка команды управления",\n  "update.wait_agents": "Ожидание агентов...",\n  "update.waiting_agents": "Ожидание обновления агентов...",\n  "update.agents_remaining": "Осталось ${count} агент(ов)...",\n  "update.agents_timeout": "Таймаут — продолжаем обновление сервера. Оставшиеся агенты обновятся позже.",\n  "update.sending": "Отправка...",\n  "update.pulling": "Загрузка кода...",\n  "update.synced": "Синхронизировано, перезапуск...",\n  "update.error": "Ошибка",\n  "update.retry": "Повторить",\n  "update.skip": "Пропустить",\n  "update.skipped": "Пропущен",\n  "update.version_mismatch": "Несоответствие версии после обновления",\n  "update.agent_logs": "Логи",\n  "update.view_logs": "Просмотр логов",\n  "update.logs_failed": "Не удалось запросить логи",\n  "update.agents_errors": "Некоторые агенты завершились с ошибкой. Пропустите или повторите, затем продолжите.",\n  "update.continue_server": "Продолжить обновление сервера",\n  "smart.loading": "Загрузка...",\n  "smart.load_error": "Ошибка загрузки SMART данных",\n  "smart.no_attributes": "Нет SMART атрибутов",\n  "smart.no_nvme_attributes": "Нет NVMe атрибутов",\n  "smart.critical": "КРИТИЧНЫЙ",\n  "smart.important": "ВАЖНЫЙ",\n  "smart.unit.bytes": "Б",\n  "smart.unit.kb": "КБ",\n  "smart.unit.mb": "МБ",\n  "smart.unit.gb": "ГБ",\n  "smart.unit.tb": "ТБ",\n  "smart.unit.days_short": " дн",\n  "smart.unit.months_short": " мес"\n}'
}


SETUP_TEMPLATE_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>FanControl Web - Setup</title>\n<script src="https://cdn.tailwindcss.com"></script>\n<style>\n  body { background: #0a0a1a; font-family: \'Segoe UI\', system-ui, sans-serif; }\n  .step-dot { transition: all 0.3s ease; }\n  .step-dot.active { background: #06b6d4; box-shadow: 0 0 12px #06b6d4; }\n  .step-dot.done { background: #10b981; }\n  .card-hover { transition: all 0.3s ease; }\n  .card-hover:hover { border-color: #06b6d4; box-shadow: 0 0 20px rgba(6,182,212,0.15); }\n  .card-selected { border-color: #06b6d4 !important; background: rgba(6,182,212,0.08) !important; box-shadow: 0 0 20px rgba(6,182,212,0.2); }\n  .glow-text { text-shadow: 0 0 20px rgba(6,182,212,0.5); }\n  .progress-bar { transition: width 0.5s ease; }\n  .fade-in { animation: fadeIn 0.3s ease; }\n  @keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }\n  input:focus, textarea:focus { outline: none; border-color: #06b6d4 !important; box-shadow: 0 0 8px rgba(6,182,212,0.3); }\n  .field-desc { color: #9ca3af; font-size: 0.75rem; margin-top: 0.25rem; }\n</style>\n</head>\n<body class="min-h-screen flex items-center justify-center p-4">\n<div class="w-full max-w-lg">\n\n  <!-- Header -->\n  <div class="text-center mb-8">\n    <h1 class="text-3xl font-bold text-cyan-400 glow-text">FanControl Web</h1>\n    <p id="subtitle" class="text-gray-400 mt-2">Setup Wizard</p>\n  </div>\n\n  <!-- Step Indicator -->\n  <div class="flex justify-center gap-3 mb-8">\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600 active" data-step="1"></div>\n      <span class="text-xs text-gray-500" id="step_label_1"></span>\n    </div>\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600" data-step="2"></div>\n      <span class="text-xs text-gray-500" id="step_label_2"></span>\n    </div>\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600" data-step="3"></div>\n      <span class="text-xs text-gray-500" id="step_label_3"></span>\n    </div>\n    <div class="flex items-center gap-2">\n      <div class="step-dot w-3 h-3 rounded-full bg-gray-600" data-step="4"></div>\n      <span class="text-xs text-gray-500" id="step_label_4"></span>\n    </div>\n  </div>\n\n  <!-- Step 1: Language -->\n  <div id="step1" class="fade-in">\n    <h2 id="lang_title" class="text-xl font-semibold text-white text-center mb-6">Select Language</h2>\n    <div class="flex justify-center gap-4">\n      <button onclick="selectLang(\'en\')" class="card-hover w-40 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🇬🇧</div>\n        <div class="text-white font-medium">English</div>\n      </button>\n      <button onclick="selectLang(\'ru\')" class="card-hover w-40 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🇷🇺</div>\n        <div class="text-white font-medium">Русский</div>\n      </button>\n    </div>\n  </div>\n\n  <!-- Step 2: Mode -->\n  <div id="step2" class="hidden fade-in">\n    <h2 id="comp_title" class="text-xl font-semibold text-white text-center mb-6"></h2>\n    <div class="flex justify-center gap-4">\n      <button onclick="selectMode(\'server\')" id="btn_server" class="card-hover w-44 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🖥️</div>\n        <div id="lbl_server" class="text-white font-medium"></div>\n        <div id="lbl_server_desc" class="text-gray-400 text-sm mt-1"></div>\n      </button>\n      <button onclick="selectMode(\'agent\')" id="btn_agent" class="card-hover w-44 p-6 rounded-xl border border-gray-700 bg-gray-800/50 text-center cursor-pointer">\n        <div class="text-3xl mb-2">🔗</div>\n        <div id="lbl_agent" class="text-white font-medium"></div>\n        <div id="lbl_agent_desc" class="text-gray-400 text-sm mt-1"></div>\n      </button>\n    </div>\n  </div>\n\n  <!-- Step 3: Configuration -->\n  <div id="step3" class="hidden fade-in">\n    <h2 id="config_title" class="text-xl font-semibold text-white text-center mb-6"></h2>\n    <div class="bg-gray-800/50 border border-gray-700 rounded-xl p-6 space-y-4">\n      <!-- Server fields -->\n      <div id="fields_server">\n        <div>\n          <label id="lbl_port" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_port" type="number" value="5059" min="1" max="65535"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_port" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_data_path" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_data_path" type="text" value="/data"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_data_path" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_server_name" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_server_name" type="text" value="My Server"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_server_name" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_description" class="block text-gray-300 text-sm mb-1"></label>\n          <textarea id="input_description" rows="2"\n                    class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white resize-none"></textarea>\n          <p id="desc_description" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_admin_password" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_admin_password" type="password" placeholder=""\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_admin_password" class="field-desc"></p>\n        </div>\n        <div class="mt-4 flex items-center gap-3">\n          <input id="input_ssdp_enabled" type="checkbox" checked\n                 class="w-4 h-4 rounded bg-gray-900 border-gray-600 text-cyan-500 focus:ring-cyan-500">\n          <label id="lbl_ssdp_enabled" for="input_ssdp_enabled" class="text-gray-300 text-sm"></label>\n        </div>\n        <p id="desc_ssdp_enabled" class="field-desc ml-7"></p>\n      </div>\n      <!-- Agent fields -->\n      <div id="fields_agent" class="hidden">\n        <!-- Auto-discovery section -->\n        <div class="mb-4 p-3 bg-gray-900/50 border border-gray-700 rounded-lg">\n          <div class="flex items-center justify-between mb-2">\n            <span id="lbl_autodiscover" class="text-gray-300 text-sm font-medium"></span>\n            <button onclick="discoverServers()" id="discover_btn"\n                    class="bg-cyan-700 hover:bg-cyan-600 text-white text-xs px-3 py-1.5 rounded-lg transition-colors">\n            </button>\n          </div>\n          <div id="discovered_list" class="space-y-1"></div>\n          <p id="desc_autodiscover" class="field-desc mt-1"></p>\n        </div>\n        <!-- Manual IP + Port -->\n        <div class="flex gap-3">\n          <div class="flex-1">\n            <label id="lbl_server_url" class="block text-gray-300 text-sm mb-1"></label>\n            <input id="input_server_url" type="text" placeholder="192.168.1.100"\n                   class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          </div>\n          <div class="w-24">\n            <label id="lbl_server_port" class="block text-gray-300 text-sm mb-1"></label>\n            <input id="input_server_port" type="number" value="5059" min="1" max="65535"\n                   class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          </div>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_node_name" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_node_name" type="text" value=""\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white"\n                 placeholder="">\n          <p id="desc_node_name" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <label id="lbl_agent_data_path" class="block text-gray-300 text-sm mb-1"></label>\n          <input id="input_agent_data_path" type="text" value="/data"\n                 class="w-full bg-gray-900 border border-gray-600 rounded-lg px-4 py-2.5 text-white">\n          <p id="desc_agent_data_path" class="field-desc"></p>\n        </div>\n        <div class="mt-4">\n          <button onclick="testConnection()" id="test_conn_btn"\n                  class="bg-gray-600 hover:bg-gray-500 text-white font-medium px-4 py-2 rounded-lg transition-colors text-sm">\n          </button>\n          <span id="test_conn_result" class="ml-3 text-sm"></span>\n        </div>\n      </div>\n      <div id="config_error" class="hidden text-red-400 text-sm mt-2"></div>\n      <button onclick="startInstall()" id="install_btn"\n              class="w-full mt-4 bg-cyan-600 hover:bg-cyan-500 text-white font-semibold py-3 rounded-lg transition-colors">\n      </button>\n    </div>\n  </div>\n\n  <!-- Step 4: Progress -->\n  <div id="step4" class="hidden fade-in">\n    <div class="bg-gray-800/50 border border-gray-700 rounded-xl p-6">\n      <div class="flex justify-between text-sm text-gray-400 mb-2">\n        <span id="progress_label"></span>\n        <span id="progress_pct">0%</span>\n      </div>\n      <div class="w-full bg-gray-700 rounded-full h-3 mb-4">\n        <div id="progress_bar" class="progress-bar bg-cyan-500 h-3 rounded-full" style="width: 0%"></div>\n      </div>\n      <p id="progress_msg" class="text-gray-300 text-sm text-center"></p>\n      <div id="complete_section" class="hidden text-center mt-6">\n        <div class="text-emerald-400 text-xl font-semibold mb-4" id="complete_text"></div>\n        <p id="restart_msg" class="text-gray-400 text-sm mb-4"></p>\n        <a id="dashboard_link" href="/" target="_blank"\n           class="inline-block bg-cyan-600 hover:bg-cyan-500 text-white font-semibold px-8 py-3 rounded-lg transition-colors">\n        </a>\n      </div>\n      <div id="error_section" class="hidden text-center mt-6">\n        <p id="error_text" class="text-red-400 text-sm"></p>\n        <button onclick="location.reload()" class="mt-4 bg-gray-600 hover:bg-gray-500 text-white px-6 py-2 rounded-lg transition-colors text-sm">\n          Retry\n        </button>\n      </div>\n    </div>\n  </div>\n\n</div>\n\n<script>\nconst translations = {\n  en: {\n    title: \'Setup Wizard\',\n    step1: \'Language\',\n    step2: \'Mode\',\n    step3: \'Configuration\',\n    step4: \'Install\',\n    component_title: \'Select Component\',\n    server: \'Server\',\n    server_desc: \'Central dashboard + control for multiple nodes\',\n    agent: \'Agent\',\n    agent_desc: \'Connect to existing server\',\n    config_title: \'Configuration\',\n    port: \'Port\',\n    port_desc: \'Web interface port (default: 5059)\',\n    data_path: \'Data Path\',\n    data_path_desc: \'Container path for config and data\',\n    server_name: \'Server Name\',\n    server_name_desc: \'Display name for this server\',\n    description: \'Description\',\n    description_desc: \'Optional description\',\n    admin_password: \'Admin Password\',\n    admin_password_desc: \'Protect web interface (empty = no auth)\',\n    ssdp_enabled: \'Enable LAN Discovery\',\n    ssdp_desc: \'Allow agents to discover this server on LAN\',\n    server_url: \'Server IP\',\n    server_url_desc: \'IP address of the server\',\n    server_port: \'Port\',\n    node_name: \'Node Name\',\n    node_name_desc: \'Display name for this agent (auto-detected if empty)\',\n    agent_data_path: \'Data Path\',\n    agent_data_path_desc: \'Container path for config and data\',\n    autodiscover: \'Auto-discover servers\',\n    autodiscover_desc: \'Scan network for FanControl servers\',\n    discover_btn: \'Scan\',\n    discover_scanning: \'Scanning...\',\n    discover_none: \'No servers found. Enter IP manually.\',\n    select_server: \'Select\',\n    test_connection: \'Test Connection\',\n    test_ok: \'Connection successful!\',\n    test_fail: \'Connection failed\',\n    test_testing: \'Testing...\',\n    install_btn: \'Install\',\n    installing: \'Installing...\',\n    restarting: \'Container restarting...\',\n    complete: \'Setup Complete!\',\n    redirecting: \'Redirecting in {seconds} seconds...\',\n    open_dashboard: \'Open Dashboard\',\n  },\n  ru: {\n    title: \'Мастер установки\',\n    step1: \'Язык\',\n    step2: \'Режим\',\n    step3: \'Конфигурация\',\n    step4: \'Установка\',\n    component_title: \'Выберите компонент\',\n    server: \'Сервер\',\n    server_desc: \'Центральная панель управления для нескольких узлов\',\n    agent: \'Агент\',\n    agent_desc: \'Подключение к существующему серверу\',\n    config_title: \'Конфигурация\',\n    port: \'Порт\',\n    port_desc: \'Порт веб-интерфейса (по умолчанию: 5059)\',\n    data_path: \'Путь к данным\',\n    data_path_desc: \'Контейнерный путь для конфига и данных\',\n    server_name: \'Имя сервера\',\n    server_name_desc: \'Отображаемое имя сервера\',\n    description: \'Описание\',\n    description_desc: \'Описание (необязательно)\',\n    admin_password: \'Пароль админа\',\n    admin_password_desc: \'Защита веб-интерфейса (пусто = без пароля)\',\n    ssdp_enabled: \'Включить обнаружение в LAN\',\n    ssdp_desc: \'Разрешить агентам находить этот сервер в локальной сети\',\n    server_url: \'IP сервера\',\n    server_url_desc: \'IP адрес сервера\',\n    server_port: \'Порт\',\n    node_name: \'Имя узла\',\n    node_name_desc: \'Отображаемое имя агента (автоопределение)\',\n    agent_data_path: \'Путь к данным\',\n    agent_data_path_desc: \'Контейнерный путь для конфига и данных\',\n    autodiscover: \'Автообнаружение серверов\',\n    autodiscover_desc: \'Поиск серверов FanControl в сети\',\n    discover_btn: \'Найти\',\n    discover_scanning: \'Поиск...\',\n    discover_none: \'Серверы не найдены. Введите IP вручную.\',\n    select_server: \'Выбрать\',\n    test_connection: \'Проверить соединение\',\n    test_ok: \'Соединение успешно!\',\n    test_fail: \'Ошибка соединения\',\n    test_testing: \'Проверка...\',\n    install_btn: \'Установить\',\n    installing: \'Установка...\',\n    restarting: \'Контейнер перезапускается...\',\n    complete: \'Установка завершена!\',\n    redirecting: \'Перенаправление через {seconds} секунд...\',\n    open_dashboard: \'Открыть панель\',\n  }\n};\n\nlet lang = \'en\';\nlet mode = null;\nlet pollInterval = null;\nlet redirectCountdown = null;\n\nfunction t(key) { return translations[lang][key] || key; }\n\nfunction updateTexts() {\n  document.getElementById(\'subtitle\').textContent = t(\'title\');\n  document.getElementById(\'step_label_1\').textContent = t(\'step1\');\n  document.getElementById(\'step_label_2\').textContent = t(\'step2\');\n  document.getElementById(\'step_label_3\').textContent = t(\'step3\');\n  document.getElementById(\'step_label_4\').textContent = t(\'step4\');\n  document.getElementById(\'comp_title\').textContent = t(\'component_title\');\n  document.getElementById(\'lbl_server\').textContent = t(\'server\');\n  document.getElementById(\'lbl_server_desc\').textContent = t(\'server_desc\');\n  document.getElementById(\'lbl_agent\').textContent = t(\'agent\');\n  document.getElementById(\'lbl_agent_desc\').textContent = t(\'agent_desc\');\n  document.getElementById(\'config_title\').textContent = t(\'config_title\');\n  document.getElementById(\'lbl_port\').textContent = t(\'port\');\n  document.getElementById(\'desc_port\').textContent = t(\'port_desc\');\n  document.getElementById(\'lbl_data_path\').textContent = t(\'data_path\');\n  document.getElementById(\'desc_data_path\').textContent = t(\'data_path_desc\');\n  document.getElementById(\'lbl_server_name\').textContent = t(\'server_name\');\n  document.getElementById(\'desc_server_name\').textContent = t(\'server_name_desc\');\n  document.getElementById(\'lbl_description\').textContent = t(\'description\');\n  document.getElementById(\'desc_description\').textContent = t(\'description_desc\');\n  document.getElementById(\'lbl_admin_password\').textContent = t(\'admin_password\');\n  document.getElementById(\'desc_admin_password\').textContent = t(\'admin_password_desc\');\n  document.getElementById(\'lbl_ssdp_enabled\').textContent = t(\'ssdp_enabled\');\n  document.getElementById(\'desc_ssdp_enabled\').textContent = t(\'ssdp_desc\');\n  document.getElementById(\'lbl_server_url\').textContent = t(\'server_url\');\n  document.getElementById(\'lbl_server_port\').textContent = t(\'server_port\');\n  document.getElementById(\'lbl_node_name\').textContent = t(\'node_name\');\n  document.getElementById(\'desc_node_name\').textContent = t(\'node_name_desc\');\n  document.getElementById(\'lbl_agent_data_path\').textContent = t(\'agent_data_path\');\n  document.getElementById(\'desc_agent_data_path\').textContent = t(\'agent_data_path_desc\');\n  document.getElementById(\'lbl_autodiscover\').textContent = t(\'autodiscover\');\n  document.getElementById(\'desc_autodiscover\').textContent = t(\'autodiscover_desc\');\n  document.getElementById(\'discover_btn\').textContent = t(\'discover_btn\');\n  document.getElementById(\'test_conn_btn\').textContent = t(\'test_connection\');\n  document.getElementById(\'install_btn\').textContent = t(\'install_btn\');\n}\n\nfunction setStep(n) {\n  for (let i = 1; i <= 4; i++) {\n    const el = document.getElementById(\'step\' + i);\n    el.classList.toggle(\'hidden\', i !== n);\n    el.classList.toggle(\'fade-in\', i === n);\n    const dot = document.querySelector(`.step-dot[data-step="${i}"]`);\n    dot.classList.remove(\'active\', \'done\');\n    if (i < n) dot.classList.add(\'done\');\n    else if (i === n) dot.classList.add(\'active\');\n  }\n}\n\nfunction selectLang(l) {\n  lang = l;\n  updateTexts();\n  setStep(2);\n}\n\nfunction selectMode(m) {\n  mode = m;\n  document.getElementById(\'btn_server\').classList.toggle(\'card-selected\', m === \'server\');\n  document.getElementById(\'btn_agent\').classList.toggle(\'card-selected\', m === \'agent\');\n  document.getElementById(\'fields_server\').classList.toggle(\'hidden\', m !== \'server\');\n  document.getElementById(\'fields_agent\').classList.toggle(\'hidden\', m !== \'agent\');\n  setStep(3);\n  if (m === \'agent\') { setTimeout(discoverServers, 300); }\n}\n\nfunction testConnection() {\n  const btn = document.getElementById(\'test_conn_btn\');\n  const result = document.getElementById(\'test_conn_result\');\n  const ip = document.getElementById(\'input_server_url\').value.trim();\n  const port = document.getElementById(\'input_server_port\').value.trim() || \'5059\';\n\n  if (!ip) {\n    result.textContent = t(\'test_fail\');\n    result.className = \'ml-3 text-sm text-red-400\';\n    return;\n  }\n\n  btn.disabled = true;\n  btn.textContent = t(\'test_testing\');\n  result.textContent = \'\';\n  result.className = \'ml-3 text-sm\';\n\n  fetch(\'/api/validate-token\', {\n    method: \'POST\',\n    headers: { \'Content-Type\': \'application/json\' },\n    body: JSON.stringify({ server_url: \'ws://\' + ip + \':\' + port })\n  })\n  .then(r => r.json())\n  .then(data => {\n    if (data.valid) {\n      result.textContent = t(\'test_ok\');\n      result.className = \'ml-3 text-sm text-emerald-400\';\n    } else {\n      result.textContent = data.error || t(\'test_fail\');\n      result.className = \'ml-3 text-sm text-red-400\';\n    }\n  })\n  .catch(() => {\n    result.textContent = t(\'test_fail\');\n    result.className = \'ml-3 text-sm text-red-400\';\n  })\n  .finally(() => {\n    btn.disabled = false;\n    btn.textContent = t(\'test_connection\');\n  });\n}\n\nfunction startInstall() {\n  const errEl = document.getElementById(\'config_error\');\n  errEl.classList.add(\'hidden\');\n\n  if (mode === \'agent\') {\n    const url = document.getElementById(\'input_server_url\').value.trim();\n    const name = document.getElementById(\'input_node_name\').value.trim();\n    if (!url) {\n      errEl.textContent = lang === \'ru\' ? \'Введите IP адрес сервера\' : \'Enter server IP address\';\n      errEl.classList.remove(\'hidden\');\n      return;\n    }\n  }\n\n  const config = { mode, lang };\n  if (mode === \'server\') {\n    config.port = parseInt(document.getElementById(\'input_port\').value) || 5059;\n    config.data_path = document.getElementById(\'input_data_path\').value.trim() || \'/data\';\n    config.server_name = document.getElementById(\'input_server_name\').value.trim();\n    config.description = document.getElementById(\'input_description\').value.trim();\n    config.admin_password = document.getElementById(\'input_admin_password\').value;\n    config.ssdp_enabled = document.getElementById(\'input_ssdp_enabled\').checked;\n  } else {\n    const rawIp = document.getElementById(\'input_server_url\').value.trim();\n    const port = document.getElementById(\'input_server_port\').value.trim() || \'5059\';\n    config.server_url = \'ws://\' + rawIp + \':\' + port;\n    config.node_name = document.getElementById(\'input_node_name\').value.trim() || rawIp;\n    config.data_path = document.getElementById(\'input_agent_data_path\').value.trim() || \'/data\';\n  }\n\n  fetch(\'/api/install\', {\n    method: \'POST\',\n    headers: { \'Content-Type\': \'application/json\' },\n    body: JSON.stringify(config)\n  })\n  .then(r => r.json())\n  .then(data => {\n    if (data.error) {\n      errEl.textContent = data.error;\n      errEl.classList.remove(\'hidden\');\n      return;\n    }\n    setStep(4);\n    document.getElementById(\'install_btn\').textContent = t(\'installing\');\n    document.getElementById(\'install_btn\').disabled = true;\n    pollInterval = setInterval(pollStatus, 1000);\n    pollStatus();\n  })\n  .catch(err => {\n    errEl.textContent = err.message;\n    errEl.classList.remove(\'hidden\');\n  });\n}\n\nfunction pollStatus() {\n  fetch(\'/api/status\')\n    .then(r => r.json())\n    .then(s => {\n      document.getElementById(\'progress_bar\').style.width = s.progress + \'%\';\n      document.getElementById(\'progress_pct\').textContent = s.progress + \'%\';\n      document.getElementById(\'progress_label\').textContent = s.stage;\n      document.getElementById(\'progress_msg\').textContent = s.message;\n\n      if (s.complete) {\n        clearInterval(pollInterval);\n        if (s.error) {\n          document.getElementById(\'error_section\').classList.remove(\'hidden\');\n          document.getElementById(\'error_text\').textContent = s.message;\n        } else {\n          document.getElementById(\'complete_section\').classList.remove(\'hidden\');\n          document.getElementById(\'complete_text\').textContent = t(\'complete\');\n          document.getElementById(\'dashboard_link\').textContent = t(\'open_dashboard\');\n          startRedirectCountdown();\n        }\n      }\n    });\n}\n\nfunction startRedirectCountdown() {\n  let seconds = 10;\n  const restartMsg = document.getElementById(\'restart_msg\');\n  restartMsg.textContent = t(\'redirecting\').replace(\'{seconds}\', seconds);\n  redirectCountdown = setInterval(() => {\n    seconds--;\n    if (seconds <= 0) {\n      clearInterval(redirectCountdown);\n      location.reload();\n    } else {\n      restartMsg.textContent = t(\'redirecting\').replace(\'{seconds}\', seconds);\n    }\n  }, 1000);\n}\n\nfunction discoverServers() {\n  const btn = document.getElementById(\'discover_btn\');\n  const list = document.getElementById(\'discovered_list\');\n\n  btn.disabled = true;\n  btn.textContent = t(\'discover_scanning\');\n  list.innerHTML = \'\';\n\n  fetch(\'/api/discover-servers\')\n    .then(r => r.json())\n    .then(data => {\n      if (data.servers && data.servers.length > 0) {\n        data.servers.forEach(s => {\n          const div = document.createElement(\'div\');\n          div.className = \'flex items-center justify-between bg-gray-800 rounded-lg px-3 py-2 cursor-pointer hover:border hover:border-cyan-600 transition-colors\';\n          div.innerHTML = \'<div><span class="text-white text-sm">\' + escapeHtml(s.name) + \'</span><span class="text-gray-400 text-xs ml-2">\' + escapeHtml(s.ip) + \':\' + s.port + \'</span></div>\';\n          const selBtn = document.createElement(\'button\');\n          selBtn.className = \'bg-cyan-700 hover:bg-cyan-600 text-white text-xs px-3 py-1 rounded transition-colors\';\n          selBtn.textContent = t(\'select_server\');\n          selBtn.onclick = function() { selectServer(s.ip, s.port); };\n          div.appendChild(selBtn);\n          list.appendChild(div);\n        });\n      } else {\n        list.innerHTML = \'<p class="text-gray-500 text-xs">\' + t(\'discover_none\') + \'</p>\';\n      }\n    })\n    .catch(() => {\n      list.innerHTML = \'<p class="text-gray-500 text-xs">\' + t(\'discover_none\') + \'</p>\';\n    })\n    .finally(() => {\n      btn.disabled = false;\n      btn.textContent = t(\'discover_btn\');\n    });\n}\n\nfunction selectServer(ip, port) {\n  document.getElementById(\'input_server_url\').value = ip;\n  document.getElementById(\'input_server_port\').value = port;\n}\n\nfunction escapeHtml(str) {\n  const div = document.createElement(\'div\');\n  div.textContent = str;\n  return div.innerHTML;\n}\n\nupdateTexts();\n// Auto-generate node name placeholder from hostname\ntry { document.getElementById(\'input_node_name\').placeholder = location.hostname || \'Agent\'; } catch(e) {}\n</script>\n</body>\n</html>\n'

#!/usr/bin/env python3
"""
FanControl Web - Monolith (single-file version)
All Python modules, HTML template, JS, and lang files merged into one file.
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

import copy
import threading
import time
from typing import Any, Dict, Optional

CONFIG_VERSION = "3.12.7"

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
        'server_name': state.get('server_name', 'FanControl Server'),
        'nodes': dict(state.get('nodes', {})),
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
from pathlib import Path
from typing import Dict, Optional


logger = logging.getLogger('fancontrol')

DATA_DIR = Path(os.getenv('FANCONTROL_DATA_DIR', '/data'))
CONFIG_PATH = DATA_DIR / 'config.json'
DB_FILE = DATA_DIR / 'fancontrol.db'

FAN_FIELDS = [
    'id', 'label', 'hw_path', 'pwm_path', 'fan_path',
    'inverted', 'min_rpm', 'max_rpm', 'manual_pct',
    'sensors', 'sensor_mode', 'target_temp', 'mode',
    'status', 'target_pwm', 'current_pct',
    'schedule', 'curve', 'calibration'
]

SAVE_DEBOUNCE_SECONDS = 0.5
_save_timer: Optional[threading.Timer] = None
_save_lock = threading.Lock()


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
    """Actually write config to disk, preserving all existing fields."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        # Read existing config to preserve wizard-set fields
        existing = {}
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            except Exception:
                pass

        existing['config_version'] = CONFIG_VERSION
        existing['initialized'] = state.get('initialized', False)
        existing['tested'] = state.get('tested', False)
        existing['language'] = state.get('language', 'en')
        existing['server_name'] = state.get('server_name', 'FanControl Server')

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

executor = ThreadPoolExecutor(max_workers=8)

HWMON_DIR = Path(os.getenv('FANCONTROL_HWMON_DIR', '/sys/class/hwmon'))

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
                        'calibration': {}
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
            # 3. MegaRAID (common on Synology official kernel)
            if disk_index >= 0:
                for megaraid_idx in range(disk_index, disk_index + 1):
                    attempts.append(['smartctl', '-A', '-i', '-d', f'megaraid,{megaraid_idx}', f'/dev/sda'])
            # 4. Areca RAID (some Synology models)
            if disk_index >= 0:
                attempts.append(['smartctl', '-A', '-i', '-d', f'areca,{disk_index + 1}', '/dev/arcmsr0'])

        output = ''
        used_cmd = None
        for cmd in attempts:
            try:
                logger.info(f'SMART attempt: {" ".join(cmd)}')
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
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
            # MegaRAID
            disk_index = -1
            if clean_name.startswith('sata'):
                try:
                    disk_index = int(clean_name.replace('sata', ''))
                except ValueError:
                    pass
            elif clean_name.startswith('sd'):
                disk_index = ord(clean_name[2]) - ord('a')
            if disk_index >= 0:
                attempts.append(['smartctl', '-a', '-n', 'standby', '-d', f'megaraid,{disk_index}', '/dev/sda'])
                attempts.append(['smartctl', '-a', '-n', 'standby', '-d', f'areca,{disk_index + 1}', '/dev/arcmsr0'])

            for cmd in attempts:
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

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
            capture_output=True, text=True, timeout=10
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
    """Update temperature and RPM readings"""
    with state_lock:
        temp_items = [(k, v.copy()) for k, v in state['temp_sensors'].items()]
        fan_items = [(k, v.copy()) for k, v in state['fans'].items()]
    
    for key, sensor in temp_items:
        try:
            value = int(Path(sensor['path']).read_text().strip()) // 1000
        except Exception:
            continue
        
        with state_lock:
            if key in state['temp_sensors']:
                state['temp_sensors'][key]['value'] = value
    
    def poll_fan(item):
        k, fan = item
        try:
            rpm = int(Path(fan['fan_path']).read_text().strip())
        except Exception:
            rpm = None
        return k, rpm
    
    futures = [executor.submit(poll_fan, item) for item in fan_items]
    for future in futures:
        try:
            key, rpm = future.result(timeout=2)
            if rpm is not None:
                with state_lock:
                    if key in state['fans']:
                        state['fans'][key]['rpm'] = rpm
        except Exception:
            pass


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


def is_dsm_fan_available():
    """Check if scemd.xml exists and can be used for fan control."""
    return Path(SCEMD_PATH).exists() and os.access(SCEMD_PATH, os.R_OK | os.W_OK)


def _parse_scemd():
    """Parse scemd.xml and return the tree."""
    try:
        tree = ET.parse(SCEMD_PATH)
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
    """Write tree back to scemd.xml and restart service."""
    try:
        tree.write(SCEMD_PATH, encoding='unicode', xml_declaration=False)
        logger.info(f'Wrote {SCEMD_PATH}')
    except Exception as e:
        logger.error(f'Failed to write {SCEMD_PATH}: {e}')
        return False

    return _restart_scemd()


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
# MODULE: core.control
# ==============================================================================

"""Control loop — fan temperature evaluation, PWM calculation, and main loop."""

import copy
import logging
import sqlite3
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
DISK_POLL_COOLDOWN = 30


def get_db_connection() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_FILE, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
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
            result = future.result(timeout=5)
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
    """Calculate effective temperature for a fan based on assigned sensors"""
    sensors = override_sensors if override_sensors is not None else fan.get('sensors', [])
    mode = override_sensor_mode if override_sensor_mode is not None else fan.get('sensor_mode', 'max')
    
    if not sensors:
        return SENSOR_FAILURE_TEMP
    
    temps = []
    
    with state_lock:
        hdd_copy = {k: v.copy() for k, v in state['hdd_sensors'].items()}
        temp_copy = {k: v.copy() for k, v in state['temp_sensors'].items()}
    
    for sensor_id in sensors:
        if sensor_id.startswith('hdd:'):
            disk_id = sensor_id.split(':', 1)[1]
            disk = hdd_copy.get(disk_id, {})
            temp = disk.get('temp', 0)
        elif sensor_id.startswith('temp:'):
            temp_id = sensor_id.split(':', 1)[1]
            sensor = temp_copy.get(temp_id, {})
            temp = sensor.get('value', 0)
        else:
            if sensor_id in hdd_copy:
                temp = hdd_copy[sensor_id].get('temp', 0)
            elif sensor_id in temp_copy:
                temp = temp_copy[sensor_id].get('value', 0)
            else:
                temp = 0
        
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
    """Log telemetry data to SQLite"""
    try:
        with state_lock:
            fan_count = len(state['fans'])
            disk_count = len(state['hdd_sensors'])
            
            if fan_count > 0:
                avg_pwm = sum(
                    f.get('raw_pwm', f.get('pwm_value', 0))
                    for f in state['fans'].values()
                ) // fan_count
                avg_rpm = sum(
                    f.get('rpm', 0)
                    for f in state['fans'].values()
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
            
            with state_lock:
                fans_snapshot = copy.deepcopy(state['fans'])
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
                cleanup_logs()
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


def _get_conn() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
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
        try:
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
            # Migration: add ip column if missing
            cols = [r[1] for r in conn.execute('PRAGMA table_info(nodes)').fetchall()]
            if 'ip' not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN ip TEXT DEFAULT ''")
            if 'port' not in cols:
                conn.execute("ALTER TABLE nodes ADD COLUMN port INTEGER DEFAULT 5059")
            conn.commit()
        finally:
            conn.close()


def add_node(name: str, api_token: Optional[str] = None, ip: str = '', port: int = 5059) -> Dict:
    if not api_token:
        api_token = uuid.uuid4().hex
    node_id = name.lower().replace(' ', '-')
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'INSERT INTO nodes (node_id, name, api_token, ip, port) VALUES (?, ?, ?, ?, ?)',
                (node_id, name, api_token, ip, port)
            )
            conn.commit()
            row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()


def get_node(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def get_node_by_token(api_token: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute('SELECT * FROM nodes WHERE api_token = ?', (api_token,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def list_nodes() -> List[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute('SELECT * FROM nodes ORDER BY created_at DESC').fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()


def delete_node(node_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            cursor = conn.execute('DELETE FROM nodes WHERE node_id = ?', (node_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def update_node(node_id: str, name: Optional[str] = None, ip: Optional[str] = None,
                port: Optional[int] = None, api_token: Optional[str] = None) -> bool:
    with _lock:
        conn = _get_conn()
        try:
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
        finally:
            conn.close()


def update_node_status(node_id: str, status: str, telemetry: Optional[Dict] = None) -> bool:
    with _lock:
        conn = _get_conn()
        try:
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
        finally:
            conn.close()


def update_node_config(node_id: str, config: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'UPDATE nodes SET config = ? WHERE node_id = ?',
                (json.dumps(config), node_id)
            )
            conn.commit()
            return conn.execute('SELECT changes()').fetchone()[0] > 0
        finally:
            conn.close()


def update_node_control_mode(node_id: str, mode: str) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'UPDATE nodes SET control_mode = ? WHERE node_id = ?',
                (mode, node_id)
            )
            conn.commit()
            return conn.execute('SELECT changes()').fetchone()[0] > 0
        finally:
            conn.close()


def save_agent_snapshot(node_id: str, snapshot: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'UPDATE nodes SET agent_config_snapshot = ? WHERE node_id = ?',
                (json.dumps(snapshot), node_id)
            )
            conn.commit()
            return conn.execute('SELECT changes()').fetchone()[0] > 0
        finally:
            conn.close()


def get_agent_snapshot(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                'SELECT agent_config_snapshot FROM nodes WHERE node_id = ?',
                (node_id,)
            ).fetchone()
            if row and row['agent_config_snapshot']:
                return json.loads(row['agent_config_snapshot'])
            return None
        finally:
            conn.close()


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
    api_token = headers.get('X-FANCONTROL-TOKEN', '')
    location = headers.get('LOCATION', f'http://{source_ip}:5059')

    logger.info(f'SSDP scan found agent: {node_name} ({source_ip})')

    with _lock:
        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'api_token': api_token,
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
    api_token = headers.get('X-FANCONTROL-TOKEN', '')
    location = headers.get('LOCATION', '')

    if not node_id:
        return

    with _lock:
        if node_id in _discovered_nodes:
            return

        if get_node(node_id) or get_node_by_token(api_token):
            return

        _discovered_nodes[node_id] = {
            'node_id': node_id,
            'name': node_name,
            'ip': source_ip,
            'api_token': api_token,
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

                nodes = list_nodes()
                now = datetime.utcnow()

                for node in nodes:
                    nid = node['node_id']

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


def _emit_to_node(socketio, event, data, node_id):
    """Emit event to a specific agent by node_id via its SID."""
    sid = _node_to_sid.get(node_id)
    if sid:
        socketio.emit(event, data, room=sid)
    else:
        logger.debug(f'No SID for node {node_id}, emit {event} skipped')


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

        if on_connect:
            on_connect(node_id)

        # Push node_id to agent so it uses the registry ID for telemetry
        _emit_to_node(socketio, 'server:node_id_push', {
            'node_id': node_id,
            'token': node['api_token'],
        }, node_id)

        with state_lock:
            state['nodes'][node_id] = {
                'node_id': node_id,
                'name': node['name'],
                'status': 'online',
                'control_mode': control_mode,
                'config': agent_config,
                'dsm_schemes': agent_config.get('dsm_schemes', []),
                'kernel_info': agent_config.get('kernel_info', {}),
            }
        invalidate_state_cache()

        # Push server config to agent if in server mode
        server_config = node.get('config', {})
        if server_config and control_mode == 'server':
            _emit_to_node(socketio, 'server:config_push', {
                'config': server_config,
            }, node_id)
            logger.info(f'Pushed config to {node["name"]}')

            # Check for conflict on reconnect
            if agent_config and server_config != agent_config:
                save_agent_snapshot(node_id, agent_config)
                socketio.emit('node:conflict', {
                    'node_id': node_id,
                    'name': node['name'],
                    'server_config': server_config,
                    'agent_config': agent_config,
                })
                logger.info(f'Config conflict on reconnect for {node["name"]}')

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

        # Check for conflict: agent config differs from server config
        if server_config and agent_config and server_config != agent_config:
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

        # Push to all connected clients so UI updates instantly
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
    import subprocess

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
    update_token = os.environ.get('FANCONTROL_UPDATE_TOKEN')
    if update_token:
        provided = request.headers.get('X-Update-Token') or request.args.get('token')
        if provided != update_token:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401

    try:
        repo_dir = '/repo'
        app_dir = '/app'

        logger.info(f'[UPDATE] ====== START ====== PID={os.getpid()} VERSION={CONFIG_VERSION}')

        # Step 1: Check repo exists
        repo_exists = os.path.isdir(repo_dir)
        app_py = os.path.isfile(os.path.join(repo_dir, 'app.py'))
        logger.info(f'[UPDATE] Step 1: /repo exists={repo_exists}, app.py={app_py}')

        if not repo_exists or not app_py:
            logger.error(f'[UPDATE] /repo not ready!')
            return jsonify({'status': 'error', 'message': '/repo not ready'}), 500

        # Step 2: Git pull
        logger.info('[UPDATE] Step 2: git fetch + reset...')
        fetch = subprocess.run(
            ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        if fetch.returncode != 0:
            logger.error(f'[UPDATE] Git fetch FAILED: {fetch.stderr.strip()[:300]}')
            return jsonify({'status': 'error', 'message': fetch.stderr.strip()}), 500

        reset = subprocess.run(
            ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
        )
        pull_output = reset.stdout.strip() + '\n' + reset.stderr.strip()
        already_up = 'Already up to date' in pull_output or 'HEAD is now at' in pull_output
        logger.info(f'[UPDATE] Step 2 result: rc={reset.returncode}, output={pull_output.strip()[:300]}')

        # Step 3: Check what version /repo has after pull
        try:
            with open(os.path.join(repo_dir, 'core', 'state.py')) as f:
                for line in f:
                    if 'CONFIG_VERSION' in line:
                        logger.info(f'[UPDATE] Step 3: /repo version after pull: {line.strip()}')
                        break
        except Exception as e:
            logger.error(f'[UPDATE] Step 3: Cannot read /repo version: {e}')

        # Step 4: Sync /repo → /app
        logger.info('[UPDATE] Step 4: syncing files...')
        import shutil
        synced = []
        for f in os.listdir(repo_dir):
            if f.endswith('.py') or f.endswith('.txt') or f in ('Dockerfile', 'docker-compose.yml'):
                src = os.path.join(repo_dir, f)
                dst = os.path.join(app_dir, f)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                    synced.append(f)
        for d in ('templates', 'static', 'core', 'server', 'agent', 'installer', 'tests'):
            src = os.path.join(repo_dir, d)
            dst = os.path.join(app_dir, d)
            if os.path.isdir(src):
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                synced.append(f'{d}/')
        logger.info(f'[UPDATE] Step 4: synced {len(synced)} items: {", ".join(synced[:15])}')

        # Step 5: Verify /app version after sync
        try:
            with open(os.path.join(app_dir, 'core', 'state.py')) as f:
                for line in f:
                    if 'CONFIG_VERSION' in line:
                        logger.info(f'[UPDATE] Step 5: /app version after sync: {line.strip()}')
                        break
        except Exception as e:
            logger.error(f'[UPDATE] Step 5: Cannot read /app version: {e}')

        # Step 6: Schedule process exit
        logger.info('[UPDATE] Step 6: scheduling os._exit(0) in 1s...')
        import threading
        def delayed_exit():
            logger.info('[UPDATE] Step 6: os._exit(0) called!')
            os._exit(0)
        threading.Timer(1.0, delayed_exit).start()

        logger.info('[UPDATE] ====== DONE (waiting for timer) ======')
        return jsonify({'status': 'ok', 'message': f'Synced. Restarting in 1s...'})

    except Exception as e:
        logger.error(f'[UPDATE] ERROR: {e}', exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


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

    save_config()
    return jsonify({'status': 'saved'})


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
    
    with state_lock:
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
    socketio.emit('server:config_push', {
        'config': data.get('config', {}),
    }, room=node_id)
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
    socketio.emit('server:set_control_mode', {
        'mode': mode,
    }, room=node_id)
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
    api_token = info.get('X-FANCONTROL-TOKEN', '') if info else ''

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
    """Accept a discovered agent and register it."""

    with _lock:
        agent = _discovered_nodes.get(node_id)
        if not agent:
            return jsonify({'error': 'Agent not found'}), 404

        node = add_node(agent['name'], api_token=agent.get('api_token', ''), ip=agent.get('ip', ''))
        del _discovered_nodes[node_id]

    return jsonify(node), 201


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


def _build_ssdp_response(node_id: str, node_name: str, port: int = 5059, api_token: str = '') -> str:
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
        f'X-FanControl-Token: {api_token}\r\n'
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


def start_announcer(node_id: str, node_name: str, port: int = 5059, api_token: str = '') -> Optional[threading.Thread]:
    def _announce_loop():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            response = _build_ssdp_response(node_id, node_name, port, api_token)

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


def _handle_msearch(node_id: str, node_name: str, port: int = 5059, api_token: str = ''):
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

        response = _build_ssdp_response(node_id, node_name, port, api_token=api_token)
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

"""WebSocket client — connects agent to server."""

import logging
import os
import threading
import time
from typing import Optional

import socketio


logger = logging.getLogger('fancontrol')

SERVER_URL = os.environ.get('SERVER_URL', '')
API_TOKEN = os.environ.get('API_TOKEN', '')
NODE_ID = os.environ.get('NODE_ID', 'agent-1')
NODE_NAME = os.environ.get('NODE_NAME', 'Agent 1')
TELEMETRY_INTERVAL = int(os.environ.get('TELEMETRY_INTERVAL', '5'))


def _init_agent_config():
    """Load agent config from config.json if not set via env vars.
    Auto-generates node_id if missing."""
    global SERVER_URL, NODE_ID, NODE_NAME
    import json
    from pathlib import Path

    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
    config = {}
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            pass

    logger.info(f'[agent-config] config_path={config_path}, exists={config_path.exists()}, server_url_in_file={config.get("server_url", "NONE")}')

    if not SERVER_URL and config.get('server_url'):
        SERVER_URL = config['server_url']
    if NODE_ID == 'agent-1' and config.get('node_id'):
        NODE_ID = config['node_id']
    if NODE_NAME == 'Agent 1' and config.get('node_name'):
        NODE_NAME = config['node_name']

    # Auto-generate stable node_id if still default
    if NODE_ID == 'agent-1' and not config.get('node_id'):
        import uuid
        NODE_ID = f'agent-{uuid.uuid4().hex[:12]}'
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config['node_id'] = NODE_ID
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception:
            pass

    logger.info(f'[agent-config] SERVER_URL={SERVER_URL}, NODE_ID={NODE_ID}, NODE_NAME={NODE_NAME}')


_init_agent_config()


def _init_token():
    """Generate or load API token for this agent."""
    import json
    from pathlib import Path

    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'

    # If token provided via env, use it
    if API_TOKEN:
        return API_TOKEN

    # Try to load from config
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
                if config.get('api_token'):
                    return config['api_token']
        except Exception:
            pass

    # Generate new token
    import uuid
    new_token = uuid.uuid4().hex

    # Save to config (best-effort)
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
            except Exception:
                pass

        config['api_token'] = new_token
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.warning(f'Could not persist api_token to config: {e}')

    logger.info(f'Generated new API token: {new_token[:8]}...')
    return new_token


API_TOKEN = _init_token()

# Agent-specific state fields
state['control_mode'] = 'server'  # 'server' or 'manual'
state['server_connected'] = False
state['server_url'] = SERVER_URL
state['node_id'] = NODE_ID
state['node_name'] = NODE_NAME
state['api_token'] = API_TOKEN
state['agent_config_snapshot'] = None

# Detect kernel info
try:
    state['kernel_info'] = get_kernel_info()
except Exception:
    state['kernel_info'] = {}

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
    """Server pushes new config — apply and save locally."""
    with state_lock:
        if state['control_mode'] != 'server':
            logger.info('Config push ignored — in manual mode')
            return

        state['agent_config_snapshot'] = _get_local_config()
        _apply_config(data.get('config', {}))
        invalidate_state_cache()
        logger.info('Applied server config')

    _save_local_config()


def _on_set_control_mode(data):
    """Server requests mode change."""
    mode = data.get('mode', 'server')
    with state_lock:
        state['control_mode'] = mode
        invalidate_state_cache()
    logger.info(f'Control mode set to: {mode}')
    _save_local_config()


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
        set_pwm(fan_id, int(value * 255 // 100))


def _on_dsm_apply(data):
    """Server pushes a DSM scheme change — apply locally."""
    scheme_type = data.get('scheme_type')
    entries = data.get('entries', [])
    logger.info(f'Received DSM scheme apply: {scheme_type} ({len(entries)} entries)')
    try:
        for entry in entries:
            idx = entry.get('index')
            field = entry.get('field', 'fan_speed')
            value = entry.get('value')
            if idx is not None and value is not None:
                update_scheme_entry(scheme_type, idx, field, value)
                logger.info(f'Updated {scheme_type}[{idx}].{field} = {value}')
        # Reload schemes in state
        with state_lock:
            try:
                if is_dsm_fan_available():
                    state['dsm_schemes'] = get_all_schemes()
            except Exception:
                pass
        invalidate_state_cache()
        logger.info(f'DSM scheme {scheme_type} applied successfully')
    except Exception as e:
        logger.error(f'Failed to apply DSM scheme: {e}')


def _telemetry_loop():
    """Send telemetry to server periodically."""
    while True:
        time.sleep(TELEMETRY_INTERVAL)
        if _sio and state['server_connected']:
            try:
                telemetry = _get_telemetry()
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


def _get_local_config():
    """Get current local fan config including kernel info and DSM schemes."""
    with state_lock:
        config = {
            'fans': {k: {kk: vv for kk, vv in v.items()
                         if kk not in ('rpm', 'pwm_value')}
                     for k, v in state['fans'].items()},
            'temp_sensors': state['temp_sensors'],
            'hdd_sensors': state['hdd_sensors'],
            'kernel_info': state.get('kernel_info', {}),
        }
        # Include DSM schemes if available
        try:
            if is_dsm_fan_available():
                config['dsm_schemes'] = get_all_schemes()
                logger.info(f'Including {len(config["dsm_schemes"])} DSM schemes in config')
        except Exception as e:
            logger.debug(f'Could not load DSM schemes: {e}')
        return config


def _get_telemetry():
    """Get current telemetry data."""
    with state_lock:
        return {
            'fans': {k: {'rpm': v.get('rpm', 0),
                         'pwm_value': v.get('pwm_value', 0),
                         'control_method': v.get('control_method', 'hwmon'),
                         'label': v.get('label', k)}
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


def _save_local_config():
    """Save current config to local config.json, preserving wizard fields."""
    import json
    from pathlib import Path

    config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # Read existing config to preserve wizard-set fields
        existing = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    existing = json.load(f)
            except Exception:
                pass

        with state_lock:
            # Update runtime fields, preserve mode/server_url/node_name etc.
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


def _apply_config(config):
    """Apply config received from server."""
    with state_lock:
        for fan_id, fan_cfg in config.get('fans', {}).items():
            if fan_id in state['fans']:
                for key in ('mode', 'target_temp', 'manual_pct', 'sensors',
                            'sensor_mode', 'schedule', 'inverted'):
                    if key in fan_cfg:
                        state['fans'][fan_id][key] = fan_cfg[key]


def _on_node_id_push(data):
    """Server pushes correct node_id and token — update and save locally."""
    new_node_id = data.get('node_id', '')
    new_token = data.get('token', '')

    global NODE_ID, API_TOKEN
    changed = False

    if new_node_id and new_node_id != NODE_ID:
        logger.info(f'Received node_id from server: {NODE_ID} → {new_node_id}')
        NODE_ID = new_node_id
        state['node_id'] = new_node_id
        changed = True

    if new_token and new_token != API_TOKEN:
        API_TOKEN = new_token
        state['api_token'] = new_token
        changed = True

    if changed:
        import json
        from pathlib import Path
        config_path = Path(os.environ.get('FANCONTROL_DATA_DIR', '/data')) / 'config.json'
        try:
            config = {}
            if config_path.exists():
                with open(config_path) as f:
                    config = json.load(f)
            if new_node_id:
                config['node_id'] = new_node_id
            if new_token:
                config['api_token'] = new_token
            with open(config_path, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            logger.warning(f'Could not persist node_id/token to config: {e}')


def _on_dsm_apply(data):
    """Server pushes DSM scheme changes — apply to local scemd.xml."""
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


def start_client():
    """Start the WebSocket client connection to server."""
    global _sio, _telemetry_thread

    logger.info(f'[start_client] SERVER_URL={SERVER_URL}, NODE_ID={NODE_ID}')

    start_announcer(NODE_ID, NODE_NAME, api_token=API_TOKEN)

    # Start M-SEARCH responder so server's active scan can find this agent
    import threading
    responder_thread = threading.Thread(
        target=_handle_msearch,
        args=(NODE_ID, NODE_NAME),
        kwargs={'api_token': API_TOKEN},
        daemon=True
    )
    responder_thread.start()
    logger.info('[agent] M-SEARCH responder started')

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
    _sio.on('server:node_id_push', _on_node_id_push)
    _sio.on('server:dsm:apply', _on_dsm_apply)

    try:
        _sio.connect(SERVER_URL)
    except Exception as e:
        logger.error(f'Failed to connect to server: {e}')

    _telemetry_thread = threading.Thread(target=_telemetry_loop, daemon=True)
    _telemetry_thread.start()


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


# ============================================================================
# MODULE: app — Flask/SocketIO setup and entry point
# ============================================================================

# socketio set to None at module level, assigned real value in main()
socketio = None

LOG_DIR = os.getenv('FANCONTROL_LOG_DIR', str(DATA_DIR / 'logs'))
try:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
except Exception:
    pass

logger = logging.getLogger('fancontrol')
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter(
    '%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(_fmt)
logger.addHandler(console_handler)

try:
    file_handler = RotatingFileHandler(
        f'{LOG_DIR}/fancontrol.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_fmt)
    logger.addHandler(file_handler)
except Exception:
    pass


def init_database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                ts TEXT, mode TEXT, pwm INTEGER, rpm INTEGER,
                max_temp INTEGER, fan_count INTEGER, disk_count INTEGER
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts)')
        conn.commit()
    logger.info('Database initialized')


def init_hardware():
    if CONFIG_PATH.exists():
        try:
            state['fans'], state['temp_sensors'] = discover_fans_and_sensors()
            state['hdd_sensors'] = discover_disks()
            refresh()
            load_config()
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
    global _control_loop_started
    if not _control_loop_started:
        _control_loop_started = True
        threading.Thread(target=loop, args=(socketio,), daemon=True).start()


def is_setup_needed():
    if not CONFIG_PATH.exists():
        logger.info(f'[setup] Config not found at {CONFIG_PATH}')
        return True
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        has_mode = bool(cfg.get('mode'))
        has_init = bool(cfg.get('initialized'))
        return not (has_init or has_mode)
    except Exception as e:
        logger.warning(f'[setup] Config read error: {e}')
        return True


def main():
    global socketio

    import argparse
    parser = argparse.ArgumentParser(description='FanControl Web')
    parser.add_argument('--mode', choices=['setup', 'server', 'agent'],
                       default=os.environ.get('MODE', 'server'),
                       help='Run mode: setup, server (default), or agent')
    args = parser.parse_args()

    if args.mode != 'setup' and is_setup_needed():
        args.mode = 'setup'

    logger.info('=' * 60)
    logger.info(f'STARTING FanControl Web {CONFIG_VERSION} - Neon Cyberpunk Edition')
    logger.info(f'Mode: {args.mode}, PID: {os.getpid()}')
    logger.info('=' * 60)

    if args.mode == 'setup':
        if is_setup_needed():
            logger.info('[setup] Setup needed — launching wizard')
            run_wizard()
            return
        else:
            try:
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
                args.mode = saved.get('mode') or 'server'
            except Exception:
                args.mode = 'server'
    elif not is_setup_needed():
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            saved_mode = saved.get('mode')
            if saved_mode and saved_mode != args.mode:
                args.mode = saved_mode
        except Exception:
            pass

    flask_app = Flask(__name__, static_folder='static', static_url_path='/static')
    CORS_ORIGINS = os.getenv('FANCONTROL_CORS_ORIGINS', '*').split(',')

    socketio = SocketIO(
        flask_app,
        cors_allowed_origins=CORS_ORIGINS,
        async_mode='threading',
        logger=False,
        engineio_logger=False,
        ping_timeout=120,
        ping_interval=25
    )

    flask_app.register_blueprint(routes)
    flask_app.register_blueprint(agent_routes)

    # Embedded asset routes
    @flask_app.route('/js/<path:filename>')
    def serve_js_embedded(filename):
        if filename == 'main.js':
            from flask import Response
            return Response(TEMPLATE_JS, mimetype='application/javascript')
        return 'Not found', 404

    @flask_app.route('/api/lang/<code>')
    def api_get_lang_embedded(code):
        if not re.match(r'^[a-z]{2}$', code):
            return jsonify({'error': 'Invalid language code'}), 400
        if code == 'en':
            return jsonify(json.loads(TEMPLATE_LANG_EN))
        elif code == 'ru':
            return jsonify(json.loads(TEMPLATE_LANG_RU))
        return jsonify({}), 404

    @flask_app.route('/')
    def index_embedded():
        from flask import Response
        html = TEMPLATE_HTML.replace('{{ config_version }}', CONFIG_VERSION)
        resp = Response(html, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        return resp

    if args.mode == 'agent':
        if not os.environ.get('SERVER_URL'):
            try:
                with open(CONFIG_PATH) as f:
                    _cfg = json.load(f)
                if _cfg.get('server_url'):
                    os.environ['SERVER_URL'] = _cfg['server_url']
                if _cfg.get('node_name'):
                    os.environ.setdefault('NODE_NAME', _cfg['node_name'])
                if _cfg.get('api_token'):
                    os.environ.setdefault('API_TOKEN', _cfg['api_token'])
            except Exception:
                pass

        init_database()
        init_hardware()
        _init_complete.set()
        _ensure_control_loop()

        @socketio.on('connect')
        def _agent_socket_connect():
            _init_complete.wait(timeout=15)
            socketio.emit('update', get_state())

        @socketio.on('get_state')
        def _agent_socket_get_state():
            socketio.emit('update', get_state())

        start_client()
    else:
        @flask_app.before_request
        def _auto_init():
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
                invalidate_state_cache()
                socketio.emit('update', get_state())

        register_handlers(socketio)
        init_database()
        init_hardware()
        _init_complete.set()
        _ensure_control_loop()

    logger.info('Starting server on port 5059')
    socketio.run(flask_app, host='0.0.0.0', port=5059, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()

# ==============================================================================# EMBEDDED FRONTEND ASSETS# ==============================================================================
TEMPLATE_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n    <meta charset="UTF-8">\n    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">\n    <meta http-equiv="Pragma" content="no-cache">\n    <meta http-equiv="Expires" content="0">\n    <title>FanControl v{{ config_version }} - Neon Cyberpunk</title>\n    \n    <!-- Tailwind CSS via CDN -->\n    <script src="https://cdn.tailwindcss.com"></script>\n    <script>\n        tailwind.config = {\n            darkMode: \'class\',\n            theme: {\n                extend: {\n                    colors: {\n                        \'cyber-bg\': \'#0b0e14\',\n                        \'cyber-card\': \'#131820\',\n                        \'cyber-accent\': \'#1a1f2e\',\n                        \'neon-cyan\': \'#00f0ff\',\n                        \'neon-purple\': \'#b347ea\',\n                        \'neon-red\': \'#ff2d55\',\n                        \'neon-orange\': \'#ff9f0a\',\n                        \'neon-green\': \'#30d158\',\n                    },\n                    boxShadow: {\n                        \'neon-cyan\': \'0 0 15px rgba(0, 240, 255, 0.3)\',\n                        \'neon-purple\': \'0 0 15px rgba(179, 71, 234, 0.3)\',\n                        \'neon-red\': \'0 0 20px rgba(255, 45, 85, 0.4)\',\n                        \'neon-green\': \'0 0 10px rgba(48, 209, 88, 0.3)\',\n                    },\n                    animation: {\n                        \'pulse-red\': \'pulseRed 2s ease-in-out infinite\',\n                        \'spin-slow\': \'spin 3s linear infinite\',\n                    },\n                    keyframes: {\n                        pulseRed: {\n                            \'0%, 100%\': { boxShadow: \'0 0 10px rgba(255, 45, 85, 0.2)\' },\n                            \'50%\': { boxShadow: \'0 0 30px rgba(255, 45, 85, 0.6)\' },\n                        },\n                    },\n                },\n            },\n        }\n    </script>\n    \n    <!-- Socket.IO -->\n    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>\n    \n    <style>\n        @import url(\'https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600;700&display=swap\');\n        \n        * { font-family: \'Inter\', sans-serif; }\n        .font-mono { font-family: \'JetBrains Mono\', monospace; }\n        \n        /* Scrollbar styling */\n        ::-webkit-scrollbar { width: 4px; }\n        ::-webkit-scrollbar-track { background: #0b0e14; }\n        ::-webkit-scrollbar-thumb { background: #1a1f2e; border-radius: 2px; }\n        ::-webkit-scrollbar-thumb:hover { background: #b347ea; }\n        \n        /* Fan animation */\n        @keyframes fan-spin {\n            from { transform: rotate(0deg); }\n            to { transform: rotate(360deg); }\n        }\n        .fan-spinning { animation: fan-spin var(--fan-duration, 1s) linear infinite; }\n        \n        /* Glow text */\n        .glow-cyan { text-shadow: 0 0 10px rgba(0, 240, 255, 0.5); }\n        .glow-purple { text-shadow: 0 0 10px rgba(179, 71, 234, 0.5); }\n        .glow-red { text-shadow: 0 0 10px rgba(255, 45, 85, 0.5); }\n        \n        /* Progress bar animation */\n        .progress-fill {\n            transition: width 1.5s ease-in-out, background 1.5s ease-in-out;\n        }\n        \n        /* Pulse animation for alerts */\n        @keyframes alertPulse {\n            0%, 100% { opacity: 1; }\n            50% { opacity: 0.6; }\n        }\n        .alert-pulse { animation: alertPulse 1.5s ease-in-out infinite; }\n        \n        /* Compact mode */\n        body.compact-mode .fan-card { padding: 0.375rem 0.5rem !important; }\n        body.compact-mode .fan-card .text-sm { font-size: 0.7rem !important; }\n        body.compact-mode .fan-card .text-xs { font-size: 0.6rem !important; }\n        body.compact-mode #fan-list > div { margin-bottom: 0.25rem !important; }\n        body.compact-mode #disks-mini-list > div { margin-bottom: 0.125rem !important; }\n        body.compact-mode #fan-icon-container { width: 2.5rem !important; height: 2.5rem !important; }\n        body.compact-mode #fan-icon-svg { width: 2rem !important; height: 2rem !important; }\n        body.compact-mode #inspector-fan > .space-y-6 > div { padding-top: 0.75rem !important; padding-bottom: 0.75rem !important; }\n        body.compact-mode #fan-name { font-size: 1.1rem !important; }\n        body.compact-mode #fan-rpm-display { font-size: 1.5rem !important; }\n        body.compact-mode #pwm-slider { height: 0.375rem !important; }\n        body.compact-mode #inspector-content { padding: 1rem !important; }\n        body.compact-mode .text-2xl { font-size: 1.1rem !important; }\n        body.compact-mode .text-3xl { font-size: 1.5rem !important; }\n        body.compact-mode .text-6xl { font-size: 2.5rem !important; }\n        body.compact-mode #schedule-grid td { width: 14px !important; height: 14px !important; }\n        body.compact-mode #schedule-grid th { font-size: 8px !important; }\n\n        /* Dashboard cards */\n        .dashboard-card {\n            transition: box-shadow 0.2s ease, border-color 0.2s ease;\n            user-select: none;\n        }\n        .dashboard-card:hover {\n            border-color: #06b6d4;\n            box-shadow: 0 0 12px rgba(6, 182, 212, 0.3);\n        }\n        .dashboard-group {\n            transition: border-color 0.2s ease;\n        }\n        .dashboard-group:hover {\n            border-color: #a855f7;\n        }\n        .group-resize-handle {\n            background: linear-gradient(135deg, transparent 50%, #6b7280 50%);\n            border-radius: 0 0 8px 0;\n        }\n        .card-resize-handle {\n            position: absolute;\n            bottom: 4px;\n            right: 4px;\n            width: 18px;\n            height: 18px;\n            cursor: se-resize;\n            opacity: 0;\n            transition: opacity 0.2s;\n            display: flex;\n            align-items: center;\n            justify-content: center;\n            z-index: 10;\n        }\n        .card-resize-handle::after {\n            content: \'\';\n            width: 10px;\n            height: 10px;\n            border-right: 2px solid #6b7280;\n            border-bottom: 2px solid #6b7280;\n            border-radius: 0 0 2px 0;\n        }\n        [data-card-id]:hover .card-resize-handle {\n            opacity: 0.6;\n        }\n        .card-resize-handle:hover {\n            opacity: 1 !important;\n        }\n        @keyframes pulse-green {\n            0%, 100% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7); }\n            50% { box-shadow: 0 0 0 6px rgba(74, 222, 128, 0); }\n        }\n        @keyframes pulse-red {\n            0%, 100% { box-shadow: 0 0 0 0 rgba(248, 113, 113, 0.7); }\n            50% { box-shadow: 0 0 0 6px rgba(248, 113, 113, 0); }\n        }\n        @keyframes pulse-yellow {\n            0%, 100% { box-shadow: 0 0 0 0 rgba(250, 204, 21, 0.7); }\n            50% { box-shadow: 0 0 0 6px rgba(250, 204, 21, 0); }\n        }\n        .status-dot {\n            width: 8px;\n            height: 8px;\n            border-radius: 50%;\n            display: inline-block;\n        }\n        .status-dot.green { background: #4ade80; animation: pulse-green 2s infinite; }\n        .status-dot.red { background: #f87171; animation: pulse-red 2s infinite; }\n        .status-dot.yellow { background: #facc15; animation: pulse-yellow 2s infinite; }\n        .card-gradient-fan {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(34,211,238,0.1), rgba(34,211,238,0.02));\n        }\n        .card-gradient-fan:hover {\n            background-image: linear-gradient(135deg, rgba(34,211,238,0.18), rgba(34,211,238,0.05));\n        }\n        .card-gradient-temperature {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(74,222,128,0.1), rgba(74,222,128,0.02));\n        }\n        .card-gradient-temperature:hover {\n            background-image: linear-gradient(135deg, rgba(74,222,128,0.18), rgba(74,222,128,0.05));\n        }\n        .card-gradient-disk {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(192,132,252,0.1), rgba(192,132,252,0.02));\n        }\n        .card-gradient-disk:hover {\n            background-image: linear-gradient(135deg, rgba(192,132,252,0.18), rgba(192,132,252,0.05));\n        }\n        .card-gradient-system {\n            background-color: #131820;\n            background-image: linear-gradient(135deg, rgba(250,204,21,0.1), rgba(250,204,21,0.02));\n        }\n        .card-gradient-system:hover {\n            background-image: linear-gradient(135deg, rgba(250,204,21,0.18), rgba(250,204,21,0.05));\n        }\n    </style>\n    <style>\n        .toast-container {\n            position: fixed;\n            top: 20px;\n            right: 20px;\n            z-index: 1000;\n            display: flex;\n            flex-direction: column;\n            gap: 10px;\n        }\n        .toast {\n            background: #1a1f2e;\n            border: 1px solid #22d3ee;\n            border-radius: 8px;\n            padding: 12px 16px;\n            color: #e5e7eb;\n            box-shadow: 0 4px 20px rgba(34, 211, 238, 0.2);\n            animation: toast-in 0.3s ease-out;\n            max-width: 350px;\n            display: flex;\n            align-items: center;\n            gap: 8px;\n            flex-wrap: wrap;\n        }\n        .toast-success { border-color: #4ade80; }\n        .toast-warning { border-color: #facc15; }\n        .toast-error { border-color: #f87171; }\n        @keyframes toast-in {\n            from { opacity: 0; transform: translateX(100px); }\n            to { opacity: 1; transform: translateX(0); }\n        }\n        .toast-btn {\n            background: #22d3ee;\n            color: #0f172a;\n            border: none;\n            border-radius: 4px;\n            padding: 4px 12px;\n            cursor: pointer;\n            font-weight: 600;\n            white-space: nowrap;\n        }\n        .toast-btn:hover { background: #06b6d4; }\n        .toast-btn-secondary {\n            background: transparent;\n            border: 1px solid #4b5563;\n            color: #9ca3af;\n        }\n        .toast-btn-secondary:hover { border-color: #6b7280; }\n    </style>\n</head>\n<body class="bg-cyber-bg text-gray-200 min-h-screen">\n    \n    <!-- ======================================================================== -->\n    <!-- SETUP WIZARD (shown when system not initialized) -->\n    <!-- ======================================================================== -->\n    <div id="setup-screen" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-cyber-bg bg-opacity-95">\n        <div class="bg-cyber-card border border-cyber-accent rounded-2xl p-10 max-w-2xl w-full mx-4 shadow-neon-purple">\n            \n            <!-- Language selector for setup -->\n            <div class="flex justify-end mb-4">\n                <div class="flex gap-1">\n                    <button onclick="switchLanguage(\'en\')" id="setup-lang-en"\n                            class="text-xs px-2 py-1 rounded border transition-all">EN</button>\n                    <button onclick="switchLanguage(\'ru\')" id="setup-lang-ru"\n                            class="text-xs px-2 py-1 rounded border transition-all">RU</button>\n                </div>\n            </div>\n                \n            <!-- Step 1: Intro -->\n            <div id="setup-step-intro" class="text-center">\n                <div class="text-6xl mb-6">🌀</div>\n                <h2 class="text-2xl font-bold text-white mb-4 glow-cyan" data-i18n="setup.heading">Initial System Setup</h2>\n                <p class="text-gray-400 mb-8" data-i18n="setup.description">\n                    No configuration found. System needs to scan available data buses \n                    to automatically detect fans and temperature sensors.\n                </p>\n                <button id="discover-btn" onclick="runDiscovery()" \n                        class="bg-neon-purple hover:bg-purple-700 text-white font-bold py-3 px-8 rounded-lg \n                               transition-all duration-300 hover:shadow-neon-purple disabled:opacity-50 disabled:cursor-not-allowed">\n                    🔍 Start Hardware Scan\n                </button>\n                <div id="discover-loader" class="hidden mt-4 text-neon-cyan animate-pulse">\n                    Scanning sysfs bus and querying smartctl...\n                </div>\n            </div>\n            \n            <!-- Step 2: Results -->\n            <div id="setup-step-results" class="hidden">\n                <h3 class="text-xl font-bold text-neon-green mb-4" data-i18n="setup.results_title">✅ Hardware Detected</h3>\n                <div id="discovered-devices" class="max-h-96 overflow-y-auto space-y-3 mb-6"></div>\n                <div id="setup-step-action" class="hidden text-center">\n                    <!-- Control Mode Selection -->\n                    <div id="control-mode-select" class="mb-6">\n                        <p class="text-gray-400 text-sm mb-4">Choose how to control fans:</p>\n                        <div class="flex justify-center gap-4">\n                            <button id="btn-hwmon" onclick="selectControlMode(\'hwmon\')"\n                                    class="card-hover relative w-52 p-4 rounded-xl border border-gray-700 bg-gray-800/50 text-left transition-all">\n                                <div class="flex items-center gap-2 mb-2">\n                                    <span class="text-xl">🐧</span>\n                                    <span class="text-white font-semibold text-sm">Direct Control</span>\n                                </div>\n                                <div class="text-gray-400 text-xs">hwmon / PWM</div>\n                                <p class="text-gray-500 text-[10px] mt-2">Direct fan speed control via Linux sysfs. Requires calibration to determine RPM curves.</p>\n                            </button>\n                            <button id="btn-dsm" onclick="selectControlMode(\'dsm\')"\n                                    class="card-hover relative w-52 p-4 rounded-xl border border-gray-700 bg-gray-800/50 text-left transition-all">\n                                <div class="flex items-center gap-2 mb-2">\n                                    <span class="text-xl">🌡</span>\n                                    <span class="text-white font-semibold text-sm">DSM Scheme</span>\n                                </div>\n                                <div class="text-gray-400 text-xs">scemd.xml</div>\n                                <p class="text-gray-500 text-[10px] mt-2">Control fans by editing DSM temperature-threshold schemes. No calibration needed.</p>\n                            </button>\n                        </div>\n                        <p id="mode-unavailable-hint" class="text-neon-orange text-xs mt-3 hidden"></p>\n                    </div>\n\n                    <!-- HWMon action (calibration) -->\n                    <div id="hwmon-action" class="hidden">\n                        <p id="calibrate-hint" class="text-gray-400 mb-4">To complete setup, fans must be calibrated. This takes about 1-2 minutes.</p>\n                        <button id="calibrate-btn" onclick="runCalibration()"\n                                class="bg-neon-cyan hover:bg-cyan-600 text-black font-bold py-3 px-8 rounded-lg \n                                       transition-all duration-300 hover:shadow-neon-cyan disabled:opacity-50 disabled:cursor-not-allowed">\n                            Start Fan Calibration\n                        </button>\n                        <div id="calibrate-loader" class="hidden mt-4 text-neon-cyan animate-pulse">\n                            Calibrating: determining PWM/RPM curves...\n                        </div>\n                    </div>\n\n                    <!-- DSM action (scheme editor) -->\n                    <div id="dsm-action" class="hidden">\n                        <div class="bg-yellow-900/20 border border-yellow-500/30 rounded-lg p-4 mb-4 text-left max-w-md mx-auto">\n                            <div class="flex items-start gap-2">\n                                <span class="text-yellow-400 mt-0.5">⚠</span>\n                                <div>\n                                    <p class="text-yellow-300 text-sm font-semibold mb-1">DSM Scheme Control</p>\n                                    <p class="text-gray-400 text-xs">Direct speed control is not available in this mode. You configure temperature thresholds and corresponding fan speeds by editing the DSM scheme table.</p>\n                                </div>\n                            </div>\n                        </div>\n                        <button onclick="applyDsmAndContinue()"\n                                class="bg-neon-cyan hover:bg-cyan-600 text-black font-bold py-3 px-8 rounded-lg transition-all duration-300">\n                            Open DSM Scheme Editor\n                        </button>\n                    </div>\n                </div>\n            </div>\n        </div>\n    </div>\n    \n    \n    <!-- ======================================================================== -->\n    <!-- MAIN DASHBOARD -->\n    <!-- ======================================================================== -->\n    <div id="main-screen" class="hidden flex flex-row h-screen">\n        \n        <!-- ======================================================================== -->\n        <!-- LEFT SIDEBAR - Server Tree (always visible) -->\n        <!-- ======================================================================== -->\n        <div class="w-64 bg-cyber-card border-r border-cyber-accent flex flex-col flex-shrink-0">\n            \n            <!-- Header -->\n            <div class="p-3 border-b border-cyber-accent">\n                <div class="flex items-center justify-between">\n                    <div class="flex items-center gap-1.5">\n                        <h1 class="text-sm font-bold glow-cyan" data-i18n="app.title">FanControl</h1>\n                        <button onclick="openServerNameEdit()" class="text-gray-500 hover:text-neon-cyan transition-colors text-xs" title="Rename server">✎</button>\n                    </div>\n                    <div class="flex items-center gap-1">\n                        <span id="header-version" class="text-xs bg-neon-purple bg-opacity-20 text-neon-purple px-1.5 py-0.5 rounded"></span>\n                        <button onclick="toggleSettings()" class="relative text-gray-400 hover:text-neon-cyan transition-colors p-1 text-sm">\n                            ⚙\n                        </button>\n                    </div>\n                </div>\n                <div class="flex items-center gap-1 text-xs text-gray-500 mt-1">\n                    <span class="w-1.5 h-1.5 bg-neon-green rounded-full"></span>\n                    <span data-i18n="header.synced">Synced</span>\n                </div>\n            </div>\n            \n            <!-- Navigation -->\n            <div class="flex border-b border-cyber-accent">\n                <button id="nav-dashboard-btn" onclick="showView(\'dashboard\')"\n                        class="flex-1 py-2 text-xs font-semibold text-neon-cyan border-b-2 border-neon-cyan transition-all">\n                    📊 <span data-i18n="nav.dashboard">Dashboard</span>\n                </button>\n                <button id="nav-dsm-btn" class="hidden flex-1 py-2 text-xs font-semibold text-gray-500 hover:text-gray-300 border-b-2 border-transparent transition-all">\n                    &#127777; <span>DSM Schemes</span>\n                </button>\n                <button id="nav-settings-btn" onclick="toggleSettings()"\n                        class="flex-1 py-2 text-xs font-semibold text-gray-500 hover:text-gray-300 border-b-2 border-transparent transition-all">\n                    ⚙ <span data-i18n="nav.settings">Settings</span>\n                </button>\n            </div>\n            \n            <!-- Server Tree -->\n            <div class="flex-1 overflow-y-auto p-2 space-y-1" id="server-tree">\n                <div class="text-center text-gray-500 py-4 text-xs" data-i18n="setup.loading_fans">Loading...</div>\n            </div>\n            \n            <!-- Add Node (server mode only) -->\n            <div id="add-node-section" class="border-t border-cyber-accent p-2">\n                <div class="flex gap-1 mb-1">\n                    <input id="new-node-name" type="text"\n                           class="flex-1 bg-cyber-bg border border-cyber-accent rounded px-2 py-1 text-xs text-white focus:border-neon-cyan focus:outline-none min-w-0"\n                           placeholder="Node name..." data-i18n-placeholder="nodes.name_placeholder"\n                           onkeydown="if(event.key===\'Enter\')addNode()">\n                    <button onclick="addNode()"\n                            class="px-2 py-1 bg-cyber-accent border border-cyber-accent rounded text-neon-cyan text-xs hover:bg-neon-cyan hover:bg-opacity-20 transition-all flex-shrink-0">\n                        +\n                    </button>\n                </div>\n                <div class="flex gap-1">\n                    <input id="new-node-ip" type="text"\n                           class="flex-1 bg-cyber-bg border border-cyber-accent rounded px-2 py-1 text-xs text-white focus:border-neon-cyan focus:outline-none min-w-0"\n                           placeholder="IP address (optional)" data-i18n-placeholder="nodes.ip_placeholder"\n                           onkeydown="if(event.key===\'Enter\')addNode()">\n                    <button onclick="scanForAgents()" id="scan-agents-btn"\n                            class="px-2 py-1 bg-cyber-accent border border-cyber-accent rounded text-neon-cyan text-xs hover:bg-neon-cyan hover:bg-opacity-20 transition-all flex-shrink-0" title="Scan for agents">\n                        &#128269;\n                    </button>\n                </div>\n                <!-- Discovered agents list -->\n                <div id="discovered-agents-list" class="hidden mt-2 space-y-1"></div>\n            </div>\n\n            <!-- Agent Update Button -->\n            <div id="agent-update-section" class="hidden border-t border-cyber-accent p-2">\n                <button onclick="openUpdateModal()" id="agent-update-btn"\n                        class="w-full py-1.5 bg-gray-800 hover:bg-gray-700 border border-gray-600 rounded text-gray-400 hover:text-white text-xs transition-all flex items-center justify-center gap-1.5 relative">\n                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>\n                    <span data-i18n="agent.update">Update</span>\n                    <span id="agent-update-badge" class="hidden absolute -top-1 -right-1 w-3 h-3 bg-neon-green rounded-full"></span>\n                </button>\n            </div>\n\n            <!-- Agent Token (shown in agent mode) -->\n            <div id="agent-token-section" class="hidden border-t border-cyber-accent p-2">\n                <div class="text-[10px] text-gray-500 mb-1">API Token (paste on server)</div>\n                <div class="flex items-center gap-1">\n                    <code id="agent-token-value" class="flex-1 text-[10px] text-neon-cyan bg-cyber-bg rounded px-2 py-1 truncate select-all cursor-pointer" title="Click to select"></code>\n                    <button onclick="copyAgentToken()" class="text-gray-400 hover:text-neon-cyan text-[10px] px-1 flex-shrink-0" title="Copy">&#128203;</button>\n                </div>\n            </div>\n        </div>\n        \n        <!-- ======================================================================== -->\n        <!-- MAIN CONTENT - Dashboard or Inspector -->\n        <!-- ======================================================================== -->\n        <div class="flex-1 flex flex-col overflow-hidden bg-cyber-bg relative">\n            \n            <!-- Dashboard Canvas (full screen) -->\n            <div id="dashboard-canvas-container" class="flex-1 overflow-auto relative">\n                <!-- Agent mode: show token prominently when dashboard is empty -->\n                <div id="agent-token-banner" class="hidden m-4 p-4 bg-cyber-card border border-neon-purple/30 rounded-xl">\n                    <div class="flex items-center gap-2 mb-2">\n                        <span class="text-neon-purple text-lg">🔑</span>\n                        <span class="text-white font-semibold text-sm">Agent Token</span>\n                        <span class="text-gray-500 text-xs">— paste this in server node settings</span>\n                    </div>\n                    <div class="flex items-center gap-2">\n                        <code id="agent-token-banner-value" class="flex-1 text-sm text-neon-cyan bg-cyber-bg rounded px-3 py-2 font-mono select-all cursor-pointer break-all"></code>\n                        <button onclick="copyAgentToken()" class="px-3 py-2 bg-cyber-accent hover:bg-gray-700 text-gray-300 hover:text-white rounded text-xs transition-all flex-shrink-0">Copy</button>\n                    </div>\n                    <div class="text-xs text-gray-600 mt-2">Also visible in left sidebar ⬅</div>\n                </div>\n                <div id="dashboard-empty" class="flex flex-col items-center justify-center text-gray-500 py-20">\n                    <div class="text-4xl mb-4">📊</div>\n                    <p class="text-sm" data-i18n="dashboard.empty">Dashboard is empty</p>\n                    <p class="text-xs text-gray-600 mt-1" data-i18n="dashboard.empty_hint">Click + to add monitoring cards</p>\n                </div>\n                <div id="dashboard-canvas" class="p-4" style="display: grid; grid-template-columns: repeat(12, 1fr); grid-auto-rows: 100px; gap: 8px; position: relative;"></div>\n                <button id="dashboard-add-btn" onclick="showCardPicker()"\n                        class="fixed bottom-6 right-6 w-12 h-12 bg-neon-cyan rounded-full text-black text-2xl font-bold shadow-lg hover:bg-cyan-400 transition-all z-40">\n                    +\n                </button>\n                <button id="dashboard-group-btn" onclick="showGroupCreator()"\n                        class="fixed bottom-6 right-20 w-12 h-12 bg-neon-purple rounded-full text-white text-lg shadow-lg hover:bg-purple-400 transition-all z-40">\n                    ⊞\n                </button>\n            </div>\n            \n            <!-- Inspector (shown when fan selected) -->\n            <div id="inspector-container" class="hidden flex-1 flex flex-col overflow-hidden">\n                \n                <!-- Top Bar -->\n                <div class="p-4 border-b border-cyber-accent flex items-center justify-between">\n                    <div>\n                        <h2 id="inspector-title" class="text-xl font-bold text-white" data-i18n="inspector.select">Select a device</h2>\n                        <p id="inspector-subtitle" class="text-xs text-gray-500" data-i18n="inspector.hint">Click on a fan to inspect</p>\n                    </div>\n                    <div class="flex items-center gap-3">\n                        <div id="failsafe-indicator" class="hidden flex items-center gap-2 bg-red-900 bg-opacity-30 text-neon-red px-3 py-1 rounded-lg alert-pulse">\n                            <span class="w-2 h-2 bg-neon-red rounded-full"></span> FAILSAFE\n                        </div>\n                        <div id="standby-indicator" class="hidden flex items-center gap-2 bg-blue-900 bg-opacity-30 text-blue-400 px-3 py-1 rounded-lg">\n                            <span class="w-2 h-2 bg-blue-400 rounded-full"></span> STANDBY\n                        </div>\n                        <button onclick="startCalibration()" \n                                class="text-xs bg-neon-purple bg-opacity-20 text-neon-purple px-3 py-1.5 rounded-lg hover:bg-opacity-40 transition-all" data-i18n="setup.calibrate_btn_short">\n                            Recalibrate\n                        </button>\n                        <button onclick="showView(\'dashboard\')" \n                                class="text-xs bg-gray-700 text-gray-300 px-3 py-1.5 rounded-lg hover:bg-gray-600 transition-all">\n                            ← Back to Dashboard\n                        </button>\n                    </div>\n                </div>\n            \n            <!-- Inspector Content -->\n            <div class="flex-1 overflow-y-auto p-6" id="inspector-content">\n                \n                <!-- Empty State -->\n                <div id="inspector-empty" class="flex flex-col items-center justify-center h-full text-gray-600">\n                    <div class="text-6xl mb-4">🌀</div>\n                    <p class="text-lg" data-i18n="inspector.select">Select a fan from the left panel</p>\n                    <p class="text-sm" data-i18n="inspector.hint_detail">to view controls and analytics</p>\n                </div>\n                \n                <!-- Fan Inspector (hidden by default) -->\n                <div id="inspector-fan" class="hidden space-y-6">\n                    \n                    <!-- Fan Header -->\n                    <div class="flex items-center gap-4">\n                        <div class="w-16 h-16 flex items-center justify-center" id="fan-icon-container">\n                            <svg id="fan-icon-svg" class="w-12 h-12 text-neon-cyan" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">\n                                <path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"/>\n                                <circle cx="12" cy="12" r="3"/>\n                            </svg>\n                        </div>\n                        <div>\n                            <h3 id="fan-name" class="text-2xl font-bold text-white" data-i18n-placeholder="inspector.fan_name">Fan Name</h3>\n                            <div class="flex items-center gap-2 mt-1">\n                                <span id="fan-inverted-badge" class="hidden text-xs px-2 py-0.5 rounded-full bg-cyan-900 bg-opacity-30 text-neon-cyan" data-i18n="fan.inverted">INVERTED</span>\n                                <span id="fan-status-badge" class="text-xs px-2 py-0.5 rounded-full" data-i18n="inspector.status">Status</span>\n                                <span id="fan-mode-badge" class="text-xs px-2 py-0.5 rounded-full" data-i18n="inspector.mode">Mode</span>\n                            </div>\n                        </div>\n                        <div class="ml-auto text-right">\n                            <div id="fan-rpm-display" class="text-3xl font-bold font-mono text-neon-cyan">0</div>\n                            <div class="text-xs text-gray-500" data-i18n="fan.rpm">RPM</div>\n                        </div>\n                    </div>\n                    \n                    <!-- PWM Slider -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <div class="flex items-center justify-between mb-3">\n                            <label class="text-sm font-semibold text-gray-300" data-i18n="inspector.fan_speed">Fan Speed</label>\n                            <span id="pwm-value-display" class="text-lg font-bold font-mono text-neon-purple">50%</span>\n                        </div>\n                        <input type="range" id="pwm-slider" min="0" max="100" value="50"\n                               class="w-full h-2 bg-cyber-accent rounded-lg appearance-none cursor-pointer\n                                      accent-neon-purple [&::-webkit-slider-thumb]:appearance-none \n                                      [&::-webkit-slider-thumb]:w-5 [&::-webkit-slider-thumb]:h-5 \n                                      [&::-webkit-slider-thumb]:bg-neon-purple [&::-webkit-slider-thumb]:rounded-full\n                                      [&::-webkit-slider-thumb]:shadow-neon-purple [&::-webkit-slider-thumb]:cursor-pointer">\n                        <div class="flex justify-between text-xs text-gray-500 mt-1">\n                            <span>0%</span><span>50%</span><span>100%</span>\n                        </div>\n                    </div>\n                    \n                    <!-- PWM Range -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <div class="flex items-center justify-between mb-3">\n                            <label class="text-sm font-semibold text-gray-300 flex items-center gap-1">\n                                PWM Range\n                                <span class="relative group">\n                                    <span class="text-gray-500 cursor-help">&#x24D8;</span>\n                                    <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50" data-i18n="calibration.pwm_range_hint">Dead zone boundaries. Min = lowest PWM where fan spins. Max = PWM where fan reaches full speed. 0-100% slider maps only to this range.</span>\n                                </span>\n                            </label>\n                        </div>\n                        <div class="flex items-center gap-2 mt-1">\n                            <span class="text-xs text-gray-500 w-8" data-i18n="calibration.min_pwm">Min</span>\n                            <input id="cal-min-pwm" type="range" min="0" max="255" value="0"\n                                   class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"\n                                   oninput="updateCalibrationParam(\'min_pwm\', this.value)">\n                            <span id="cal-min-pwm-val" class="text-xs text-gray-400 w-8 text-right">0</span>\n                        </div>\n                        <div class="flex items-center gap-2 mt-2">\n                            <span class="text-xs text-gray-500 w-8" data-i18n="calibration.max_pwm">Max</span>\n                            <input id="cal-max-pwm" type="range" min="0" max="255" value="255"\n                                   class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"\n                                   oninput="updateCalibrationParam(\'max_pwm\', this.value)">\n                            <span id="cal-max-pwm-val" class="text-xs text-gray-400 w-8 text-right">255</span>\n                        </div>\n                    </div>\n                    \n                    <!-- Lambda -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <div class="flex items-center justify-between mb-3">\n                            <label class="text-sm font-semibold text-gray-300 flex items-center gap-1">\n                                Curve Shape\n                                <span class="relative group">\n                                    <span class="text-gray-500 cursor-help">&#x24D8;</span>\n                                    <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-64 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50" data-i18n="calibration.lambda_hint">Controls fan response curve. 1.0 = linear. Lower = fan ramps up faster at low %. Higher = fan stays quiet longer, ramps up near 100%.</span>\n                                </span>\n                            </label>\n                            <span id="cal-lambda-val" class="text-sm font-mono text-neon-cyan">1.0</span>\n                        </div>\n                        <div class="flex items-center gap-2">\n                            <span class="text-xs text-gray-500">0.3</span>\n                            <input id="cal-lambda" type="range" min="3" max="30" value="10"\n                                   class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"\n                                   oninput="updateCalibrationParam(\'lambda\', this.value / 10)">\n                            <span class="text-xs text-gray-500">3.0</span>\n                        </div>\n                    </div>\n                    \n                    <!-- Control Buttons -->\n                    <div class="grid grid-cols-2 gap-3">\n                        <button id="btn-mode-manual" onclick="setFanMode(\'manual\')"\n                                class="py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300\n                                       bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30\n                                       hover:bg-opacity-40 hover:shadow-neon-purple" data-i18n="mode.manual">\n                            🎮 Manual\n                        </button>\n                        <button id="btn-mode-auto" onclick="setFanMode(\'auto\')"\n                                class="py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300\n                                       bg-cyber-accent text-gray-400 border border-gray-700\n                                       hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan" data-i18n="mode.auto">\n                            🤖 Auto\n                        </button>\n                    </div>\n                    \n                    <!-- Auto Mode Settings -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent" id="auto-settings" style="display:none;">\n                        <div class="flex items-center justify-between mb-3">\n                            <h4 class="text-sm font-semibold text-gray-300" data-i18n="schedule.weekly">Weekly Schedule</h4>\n                            <span id="schedule-coverage" class="text-xs text-gray-500"></span>\n                        </div>\n                        \n                        <div id="no-sensor-warning" class="hidden bg-yellow-900 bg-opacity-30 border border-yellow-600 rounded-lg p-3 mb-3">\n                            <p class="text-sm text-yellow-300 font-semibold mb-1">No sensors assigned</p>\n                            <p class="text-xs text-yellow-400 mb-2">Assign sensors in the first schedule cell, or globally below.</p>\n                        </div>\n                        \n                        <div id="schedule-incomplete-warning" class="hidden bg-yellow-900 bg-opacity-30 border border-yellow-600 rounded-lg p-3 mb-3">\n                            <p class="text-sm text-yellow-300 font-semibold mb-1" data-i18n="schedule.incomplete">Schedule incomplete</p>\n                            <p id="schedule-incomplete-detail" class="text-xs text-yellow-400"></p>\n                        </div>\n                        \n                        <!-- Schedule Grid -->\n                        <div class="overflow-x-auto mb-3">\n                            <div id="schedule-grid" class="inline-block"></div>\n                        </div>\n                        \n                        <!-- Legend -->\n                        <div class="flex items-center gap-4 text-xs text-gray-500 mb-3">\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#15803d"></span> <span data-i18n="schedule.legend_auto">Auto</span></span>\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#c2410c"></span> <span data-i18n="schedule.legend_manual">Manual</span></span>\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#991b1b"></span> <span data-i18n="schedule.legend_off">Off</span></span>\n                            <span class="flex items-center gap-1"><span class="w-3 h-3 rounded" style="background:#1f2937"></span> <span data-i18n="schedule.legend_empty">Empty</span></span>\n                        </div>\n                        \n                        <!-- Schedule Rules Summary -->\n                        <div id="schedule-rules" class="mb-3"></div>\n                        \n                        <div class="flex gap-2">\n                            <button onclick="clearSchedule()" \n                                    class="text-xs bg-red-900 bg-opacity-30 text-red-400 px-3 py-1.5 rounded-lg hover:bg-opacity-50 transition-all" data-i18n="schedule.clear_all">\n                                Clear All\n                            </button>\n                            <button onclick="fillScheduleDefaults()" \n                                    class="text-xs bg-cyber-accent text-gray-400 px-3 py-1.5 rounded-lg hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple transition-all" data-i18n="schedule.fill_auto">\n                                Fill Empty with Auto\n                            </button>\n                        </div>\n                    </div>\n                    \n                    \n                    <!-- Chart -->\n                    <div class="bg-cyber-card rounded-xl p-5 border border-cyber-accent">\n                        <h4 class="text-sm font-semibold text-gray-300 mb-3" data-i18n="chart.temp_history">Temperature History (24h)</h4>\n                        <div id="temp-chart" class="h-64"></div>\n                    </div>\n                </div><!-- /inspector-fan -->\n                </div><!-- /inspector-content -->\n            </div><!-- /inspector-container -->\n\n            <!-- Nodes Overview Grid -->\n            <div id="nodes-grid" class="hidden flex-1 overflow-auto p-4">\n                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" id="nodes-grid-inner"></div>\n            </div>\n\n            <!-- Node Detail View -->\n            <div id="node-detail-content" class="hidden flex-1 overflow-auto p-4">\n                <div id="node-detail-inner"></div>\n            </div>\n\n            <!-- DSM Scheme Editor -->\n            <div id="dsm-scheme-container" class="hidden flex-1 overflow-auto p-4">\n                <div id="dsm-scheme-inner"></div>\n            </div>\n\n        </div><!-- /main content -->\n    </div><!-- /main-screen -->\n    \n    <!-- Sensor Popup -->\n    <div id="sensor-popup" class="hidden fixed inset-0 z-[60] flex items-center justify-center bg-black bg-opacity-60">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-md w-full mx-4 shadow-neon-purple">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="sensor.title">Select Sensors</h3>\n            <div id="sensor-popup-list" class="max-h-64 overflow-y-auto space-y-2 mb-4"></div>\n            <button id="sensor-popup-done-btn" onclick="closeSensorPopupForContext()"\n                    class="w-full bg-neon-purple text-white py-2 rounded-lg font-semibold hover:shadow-neon-purple transition-all">\n                Done\n            </button>\n        </div>\n    </div>\n    \n    <!-- Schedule Editor Popup -->\n    <div id="schedule-editor" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-60">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-lg w-full mx-4 shadow-neon-purple">\n            <div class="flex items-center justify-between mb-4">\n                <h3 class="text-lg font-bold text-white" data-i18n="editor.title">Edit Schedule</h3>\n                <span id="schedule-editor-cells" class="text-xs text-gray-500"></span>\n            </div>\n            \n            <!-- Mode Selection -->\n            <div class="mb-4">\n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.mode">Mode</label>\n                <div class="flex gap-2">\n                    <button id="sched-btn-auto" onclick="setScheduleMode(\'auto\')" \n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border" data-i18n="mode.auto">\n                        🌡️ Auto\n                    </button>\n                    <button id="sched-btn-manual" onclick="setScheduleMode(\'manual\')" \n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border" data-i18n="mode.manual">\n                        🎮 Manual\n                    </button>\n                    <button id="sched-btn-off" onclick="setScheduleMode(\'off\')" \n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border" data-i18n="schedule.legend_off">\n                        ⏻ Off\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Auto Mode Settings -->\n            <div id="sched-auto-settings" class="mb-4">\n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.target_temp">Target Temperature</label>\n                <div class="flex items-center gap-3 mb-3">\n                    <input type="number" id="sched-target-temp" value="31" min="20" max="60"\n                           class="w-20 bg-cyber-bg border border-cyber-accent rounded-lg px-3 py-2 text-white text-center font-mono\n                                  focus:border-neon-cyan focus:outline-none">\n                    <span class="text-gray-400">°C</span>\n                </div>\n                \n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.sensors">Sensors</label>\n                <div id="sched-sensor-tags" class="flex flex-wrap gap-2 mb-2"></div>\n                <button onclick="toggleScheduleSensorPopup()"\n                        class="text-xs bg-cyber-accent text-gray-400 px-3 py-1.5 rounded-lg \n                               hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple transition-all mb-3" data-i18n="editor.add_sensor">\n                    + Add Sensor\n                </button>\n                \n                <div id="sched-sensor-mode-section" class="hidden">\n                    <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.temp_mode">Temperature Mode</label>\n                    <div class="flex gap-2">\n                        <button id="sched-btn-sensor-max" onclick="setScheduleSensorMode(\'max\')" \n                                class="flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                            Max\n                        </button>\n                        <button id="sched-btn-sensor-min" onclick="setScheduleSensorMode(\'min\')" \n                                class="flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                            Min\n                        </button>\n                        <button id="sched-btn-sensor-avg" onclick="setScheduleSensorMode(\'avg\')" \n                                class="flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                            Average\n                        </button>\n                    </div>\n                </div>\n            </div>\n            \n            <!-- Manual Mode Settings -->\n            <div id="sched-manual-settings" class="hidden mb-4">\n                <label class="text-sm font-semibold text-gray-300 block mb-2" data-i18n="editor.fan_speed">Fan Speed</label>\n                <div class="flex items-center gap-3">\n                    <input type="range" id="sched-speed-slider" min="0" max="100" value="50"\n                           class="flex-1 h-2 bg-cyber-accent rounded-lg appearance-none cursor-pointer\n                                  accent-neon-purple [&::-webkit-slider-thumb]:appearance-none \n                                  [&::-webkit-slider-thumb]:w-5 [&::-webkit-slider-thumb]:h-5 \n                                  [&::-webkit-slider-thumb]:bg-neon-purple [&::-webkit-slider-thumb]:rounded-full\n                                  [&::-webkit-slider-thumb]:shadow-neon-purple [&::-webkit-slider-thumb]:cursor-pointer">\n                    <span id="sched-speed-value" class="text-sm font-mono text-neon-purple w-12 text-right">50%</span>\n                </div>\n            </div>\n            \n            <!-- Buttons -->\n            <div class="flex gap-2">\n                <button onclick="saveScheduleEdit()" \n                        class="flex-1 bg-neon-cyan bg-opacity-20 text-neon-cyan py-2.5 rounded-lg font-semibold\n                               hover:bg-opacity-40 transition-all" data-i18n="editor.apply">\n                    Apply\n                </button>\n                <button onclick="deleteScheduleEdit()" \n                        class="bg-red-900 bg-opacity-30 text-red-400 px-4 py-2.5 rounded-lg font-semibold\n                               hover:bg-opacity-50 transition-all" data-i18n="editor.delete">\n                    Delete\n                </button>\n                <button onclick="closeScheduleEditor()" \n                        class="bg-cyber-accent text-gray-400 px-4 py-2.5 rounded-lg font-semibold\n                               hover:text-white transition-all" data-i18n="editor.cancel">\n                    Cancel\n                </button>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Calibration Progress Modal -->\n    <div id="calibration-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-8 max-w-md w-full mx-4 text-center shadow-neon-cyan">\n            <div class="text-4xl mb-4 animate-spin-slow">⚙️</div>\n            <h3 class="text-xl font-bold text-white mb-2" data-i18n="calibration.title">Calibrating Fans</h3>\n            <p id="calibration-status" class="text-gray-400 mb-4" data-i18n="calibration.status">Starting...</p>\n            <div class="w-full bg-cyber-accent rounded-full h-2 mb-2">\n                <div id="calibration-progress-bar" class="bg-neon-cyan h-2 rounded-full transition-all duration-500" style="width: 0%"></div>\n            </div>\n            <p id="calibration-step" class="text-xs text-gray-500">Step 0/11</p>\n        </div>\n    </div>\n    \n    <!-- Settings Panel -->\n    <div id="settings-overlay" class="hidden fixed inset-0 z-[70] bg-black bg-opacity-50" onclick="toggleSettings()"></div>\n    <div id="settings-panel" class="hidden fixed top-0 right-0 h-full w-80 bg-cyber-card border-l border-cyber-accent z-[75] shadow-2xl overflow-y-auto">\n        <div class="p-5">\n            <div class="flex items-center justify-between mb-6">\n                <h2 class="text-lg font-bold text-white" data-i18n="settings.title">Settings</h2>\n                <button onclick="toggleSettings()" class="text-gray-400 hover:text-white transition-colors text-xl">&times;</button>\n            </div>\n            \n            <!-- Language -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.language">Language</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.language_hint">Select your preferred language</p>\n                <div class="flex gap-2">\n                    <button id="lang-btn-en" onclick="switchLanguage(\'en\')"\n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        English\n                    </button>\n                    <button id="lang-btn-ru" onclick="switchLanguage(\'ru\')"\n                            class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        Русский\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Temperature Unit -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.temp_unit">Temperature Unit</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.temp_unit_hint">Choose Celsius or Fahrenheit</p>\n                <div class="flex gap-2">\n                    <button id="unit-btn-celsius" onclick="setTempUnit(\'celsius\')"\n                            class="flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        °C\n                    </button>\n                    <button id="unit-btn-fahrenheit" onclick="setTempUnit(\'fahrenheit\')"\n                            class="flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border">\n                        °F\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Refresh Interval -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.refresh">Update Interval</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.refresh_hint">Reduce CPU usage by throttling updates</p>\n                <div class="flex gap-1">\n                    <button id="refresh-btn-0" onclick="setRefreshInterval(0)"\n                            class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                        <span data-i18n="settings.refresh_realtime">Realtime</span>\n                    </button>\n                    <button id="refresh-btn-1000" onclick="setRefreshInterval(1000)"\n                            class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                        1s\n                    </button>\n                    <button id="refresh-btn-5000" onclick="setRefreshInterval(5000)"\n                            class="flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border">\n                        5s\n                    </button>\n                </div>\n            </div>\n            \n            <!-- Compact Mode -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.compact">Compact Dashboard</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.compact_hint">Smaller cards for small screens</p>\n                <button id="compact-toggle" onclick="toggleCompactMode()"\n                        class="w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-400 border-gray-700 hover:text-white">\n                    <span data-i18n="settings.off">Off</span>\n                </button>\n            </div>\n            \n            <!-- System Update -->\n            <div class="mb-6">\n                <label class="text-sm font-semibold text-gray-300 block mb-1" data-i18n="settings.update">System Update</label>\n                <p class="text-xs text-gray-500 mb-3" data-i18n="settings.update_hint">Check and apply updates from Git</p>\n                \n                <!-- Auto-check interval -->\n                <div class="flex gap-1 mb-3">\n                    <button id="autoupd-btn-off" onclick="setAutoUpdateInterval(0)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">Off</button>\n                    <button id="autoupd-btn-21600000" onclick="setAutoUpdateInterval(21600000)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">6h</button>\n                    <button id="autoupd-btn-43200000" onclick="setAutoUpdateInterval(43200000)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">12h</button>\n                    <button id="autoupd-btn-86400000" onclick="setAutoUpdateInterval(86400000)" class="flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border">24h</button>\n                </div>\n                \n                <button id="update-check-btn" onclick="checkForUpdates()"\n                        class="w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-400 border-gray-700 hover:text-neon-cyan hover:text-white mb-2">\n                    <span data-i18n="settings.check_update">Check for Updates</span>\n                </button>\n                <div id="update-result" class="hidden text-xs mt-2 p-3 rounded-lg bg-cyber-accent border border-cyber-accent"></div>\n                <button id="update-apply-btn" onclick="openUpdateModal()" disabled class="hidden w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-500 border-gray-700 mt-2">\n                    <span data-i18n="settings.apply_update">Update & Restart</span>\n                </button>\n            </div>\n            \n            <!-- Version -->\n            <div class="mb-6 text-center">\n                <a id="version-link" href="https://github.com/Biowolfx/fancontrol-web" target="_blank" rel="noopener"\n                   class="text-xs text-gray-600 hover:text-neon-cyan transition-colors cursor-pointer">\n                    FanControl Web\n                </a>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Update Modal -->\n    <div id="update-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-cyber-accent rounded-2xl p-6 max-w-md w-full mx-4 shadow-neon-purple">\n            <h3 id="update-modal-title" class="text-lg font-bold text-white mb-4" data-i18n="settings.update_modal_title">System Update</h3>\n            \n            <div id="update-modal-steps" class="space-y-3 mb-6">\n                <!-- Steps populated by JS -->\n            </div>\n            \n            <div id="update-modal-progress" class="hidden mb-4">\n                <div class="w-full bg-cyber-accent rounded-full h-2">\n                    <div id="update-modal-bar" class="bg-neon-cyan h-2 rounded-full transition-all duration-500" style="width: 0%"></div>\n                </div>\n            </div>\n            \n            <div id="update-modal-result" class="hidden text-sm mb-4 p-3 rounded-lg"></div>\n            \n            <div class="flex gap-3">\n                <button id="update-modal-close" onclick="closeUpdateModal()" class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold bg-cyber-accent text-gray-400 border border-gray-700 hover:text-white transition-all">\n                    <span data-i18n="common.cancel">Cancel</span>\n                </button>\n                <button id="update-modal-apply" onclick="startUpdate()" class="flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold bg-neon-green bg-opacity-20 text-neon-green border border-neon-green border-opacity-30 hover:bg-opacity-40 transition-all">\n                    <span data-i18n="settings.apply_update">Update & Restart</span>\n                </button>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Conflict Modal -->\n    <div id="conflict-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-yellow-500/30 rounded-xl p-6 max-w-lg w-full mx-4 shadow-2xl">\n            <h3 class="text-lg font-bold text-white mb-2">\n                ⚠️ <span id="conflict-node-name"></span> — <span data-i18n="conflict.title">Config Conflict</span>\n            </h3>\n            <p class="text-gray-400 text-sm mb-4" data-i18n="conflict.desc">Agent config differs from server config.</p>\n            \n            <div class="grid grid-cols-2 gap-4 mb-6">\n                <div>\n                    <h4 class="text-white text-sm font-semibold mb-2" data-i18n="conflict.server_config">Server Config</h4>\n                    <div id="conflict-server-config" class="bg-cyber-bg rounded p-3 text-sm"></div>\n                </div>\n                <div>\n                    <h4 class="text-white text-sm font-semibold mb-2" data-i18n="conflict.agent_config">Agent Config</h4>\n                    <div id="conflict-agent-config" class="bg-cyber-bg rounded p-3 text-sm"></div>\n                </div>\n            </div>\n            \n            <div class="flex gap-3">\n                <button onclick="applyServerConfig()"\n                    class="flex-1 py-2 px-4 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded-lg font-semibold transition-all hover:bg-opacity-40">\n                    <span data-i18n="conflict.apply_server">Apply Server Config</span>\n                </button>\n                <button onclick="keepAgentConfig()"\n                    class="flex-1 py-2 px-4 bg-cyber-accent text-gray-300 rounded-lg font-semibold transition-all hover:bg-gray-700">\n                    <span data-i18n="conflict.keep_agent">Keep Agent Config</span>\n                </button>\n                <button onclick="hideConflictModal()"\n                    class="py-2 px-4 bg-cyber-accent hover:bg-gray-700 rounded-lg text-gray-400 transition-all">\n                    <span data-i18n="common.cancel">Cancel</span>\n                </button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Node Settings Modal -->\n    <div id="node-settings-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">\n            <h3 class="text-lg font-bold text-white mb-4">Node Settings</h3>\n            <input type="hidden" id="node-settings-id">\n            <div class="space-y-3">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1">Name</label>\n                    <input id="node-settings-name" type="text"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none">\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1">IP Address</label>\n                    <input id="node-settings-ip" type="text"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none"\n                           placeholder="192.168.1.100">\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1">Port</label>\n                    <input id="node-settings-port" type="number" value="5059"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none">\n                </div>\n            </div>\n            <div class="flex gap-3 mt-5">\n                <button onclick="saveNodeSettings()"\n                    class="flex-1 py-2 px-4 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded-lg font-semibold transition-all hover:bg-opacity-40">\n                    Save\n                </button>\n                <button onclick="hideNodeSettings()"\n                    class="py-2 px-4 bg-cyber-accent hover:bg-gray-700 rounded-lg text-gray-400 transition-all">\n                    Cancel\n                </button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Server Name Edit Modal -->\n    <div id="server-name-modal" class="hidden fixed inset-0 z-[80] flex items-center justify-center bg-black bg-opacity-70">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">\n            <h3 class="text-lg font-bold text-white mb-4">Server Name</h3>\n            <div class="space-y-3">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1">Name</label>\n                    <input id="server-name-input" type="text" maxlength="64"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-sm text-white focus:border-neon-cyan focus:outline-none"\n                           placeholder="FanControl Server"\n                           onkeydown="if(event.key===\'Enter\')saveServerName()">\n                </div>\n            </div>\n            <div class="flex gap-3 mt-5">\n                <button onclick="saveServerName()"\n                    class="flex-1 py-2 px-4 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded-lg font-semibold transition-all hover:bg-opacity-40">\n                    Save\n                </button>\n                <button onclick="hideServerNameModal()"\n                    class="py-2 px-4 bg-cyber-accent hover:bg-gray-700 rounded-lg text-gray-400 transition-all">\n                    Cancel\n                </button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Manual Mode Warning -->\n    <div id="manual-mode-warning" class="hidden fixed bottom-4 right-4 z-[80] max-w-sm">\n        <div class="bg-yellow-900/30 border border-yellow-500/30 rounded-xl p-4">\n            <div class="flex items-center gap-2 mb-2">\n                <span class="text-yellow-400">⚠️</span>\n                <span class="text-white font-semibold">\n                    <span id="manual-mode-node-name"></span> — <span data-i18n="conflict.manual_mode">Manual Mode</span>\n                </span>\n            </div>\n            <p class="text-gray-400 text-sm mb-3" data-i18n="conflict.manual_warning">Agent is controlling fans locally.</p>\n            <div class="flex gap-2">\n                <button id="manual-mode-switch-btn"\n                    class="px-3 py-1 bg-neon-cyan bg-opacity-20 text-neon-cyan rounded text-sm transition-all hover:bg-opacity-40">\n                    <span data-i18n="conflict.switch_to_server">Switch to Server Control</span>\n                </button>\n                <button onclick="hideManualModeWarning()"\n                    class="px-3 py-1 bg-cyber-accent rounded text-gray-400 text-sm transition-all hover:bg-gray-700">\n                    <span data-i18n="common.done">Dismiss</span>\n                </button>\n            </div>\n        </div>\n    </div>\n    \n    <!-- Server Unavailable Banner -->\n    <div id="server-unavailable-banner" class="hidden fixed top-4 left-1/2 -translate-x-1/2 z-50">\n        <div class="bg-red-900/30 border border-red-500/30 rounded-xl px-6 py-3 flex items-center gap-3">\n            <span class="text-red-400">⚠️</span>\n            <span class="text-white">Server unavailable — running in standalone mode</span>\n        </div>\n    </div>\n\n    <!-- Card Picker Modal -->\n    <div id="card-picker-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-md w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="dashboard.add_card">Add Card</h3>\n            <div class="space-y-4">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.type">Тип</label>\n                    <select id="picker-type" class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"\n                            onchange="updatePickerElements()">\n                        <option value="fan" data-i18n="picker.fan">🌀 Вентилятор</option>\n                        <option value="temperature" data-i18n="picker.temperature">🌡 Температура</option>\n                        <option value="disk" data-i18n="picker.disk">💾 Диск</option>\n                        <option value="system" data-i18n="picker.system">📊 Система</option>\n                    </select>\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.source">Источник</label>\n                    <select id="picker-source" class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"\n                            onchange="updatePickerElements()">\n                        <option value="local" data-i18n="picker.my_server">Мой сервер (локально)</option>\n                    </select>\n                </div>\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.element">Элемент</label>\n                    <div id="picker-elements" class="max-h-48 overflow-y-auto space-y-1 bg-cyber-bg border border-cyber-accent rounded p-2"></div>\n                </div>\n            </div>\n            <div class="flex gap-2 mt-6">\n                <button onclick="hideCardPicker()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm" data-i18n="common.cancel">Отмена</button>\n                <button onclick="addSelectedCards()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm" data-i18n="picker.add">Добавить</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Card Edit Modal -->\n    <div id="card-edit-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-md w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="picker.edit_card">Редактировать карточку</h3>\n            <div class="space-y-4">\n                <div>\n                    <label class="text-xs text-gray-400 block mb-1" data-i18n="picker.title">Заголовок</label>\n                    <input id="card-edit-label" type="text" data-i18n-placeholder="picker.title_placeholder" placeholder="Название карточки"\n                           class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"\n                           onkeydown="if(event.key===\'Enter\')saveCardEdit()">\n                </div>\n            </div>\n            <div class="flex gap-2 mt-6">\n                <button onclick="hideCardEdit()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm" data-i18n="common.cancel">Отмена</button>\n                <button onclick="saveCardEdit()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm" data-i18n="common.save">Сохранить</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Card Configure Modal -->\n    <div id="card-config-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="picker.card_display">Отображение карточки</h3>\n            <div id="card-config-options" class="space-y-2"></div>\n            <div class="flex gap-2 mt-6">\n                <button onclick="hideCardConfig()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm" data-i18n="picker.close">Закрыть</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- SMART Detail Modal -->\n    <div id="smart-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col">\n            <div class="flex items-center justify-between mb-4">\n                <h3 class="text-lg font-bold text-white" id="smart-modal-title">SMART Data</h3>\n                <div class="flex items-center gap-2">\n                    <button onclick="refreshSmartData()" class="text-gray-400 hover:text-neon-cyan text-sm transition-colors" title="Обновить">🔄</button>\n                    <button onclick="hideSmartModal()" class="text-gray-400 hover:text-white text-lg">&times;</button>\n                </div>\n            </div>\n            <div id="smart-device-info" class="text-xs text-gray-400 mb-3"></div>\n            <div id="smart-attributes-container" class="flex-1 overflow-y-auto space-y-1"></div>\n            <div class="flex gap-2 mt-4 pt-4 border-t border-gray-700">\n                <button onclick="saveSmartSelection()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm">Сохранить выбор</button>\n                <button onclick="hideSmartModal()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm">Закрыть</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Group Creator Modal -->\n    <div id="group-creator-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">\n        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4">\n            <h3 class="text-lg font-bold text-white mb-4" data-i18n="dashboard.add_group">Add Group</h3>\n            <input id="group-name-input" type="text" placeholder="Group name (e.g., CPU Cooling)"\n                   class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm mb-4"\n                   onkeydown="if(event.key===\'Enter\')createGroup()">\n            <div class="flex gap-2">\n                <button onclick="hideGroupCreator()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm">Cancel</button>\n                <button onclick="createGroup()" class="flex-1 py-2 rounded-lg bg-neon-purple text-white font-semibold hover:bg-purple-400 transition-all text-sm">Create</button>\n            </div>\n        </div>\n    </div>\n\n    <!-- Debug Panel -->\n    <div id="debug-panel" class="hidden fixed bottom-4 right-4 z-50 bg-black/95 border border-cyber-accent rounded-xl p-4 max-w-md max-h-96 overflow-y-auto font-mono text-xs text-gray-300 shadow-2xl">\n        <div class="flex justify-between items-center mb-3">\n            <span class="text-neon-cyan font-bold">DEBUG PANEL</span>\n            <button onclick="toggleDebugPanel()" class="text-gray-500 hover:text-white">✕</button>\n        </div>\n        <div id="debug-content"></div>\n    </div>\n    <button onclick="toggleDebugPanel()" class="fixed bottom-4 right-4 z-40 w-10 h-10 rounded-full bg-cyber-accent/30 border border-cyber-accent text-lg hover:bg-cyber-accent/50 transition-all" title="Debug">🐛</button>\n\n    <div id="toast-container" class="toast-container"></div>\n    <script src="/js/main.js?v={{ config_version }}"></script>\n</body>\n</html>'

TEMPLATE_JS = '/**\n * FanControl Web v3.4.1 - Neon Cyberpunk Edition\n * Main JavaScript Application\n */\n\n// ============================================================================\n// GLOBAL STATE\n// ============================================================================\n\nlet chart = null;\nlet currentFanId = null;\nlet allSensors = [];\nlet fanConfigs = {};\nlet isDragging = false;\nlet wizardStep = \'intro\';\nlet currentState = null;\nlet lastChartUpdate = 0;\nconst CHART_UPDATE_INTERVAL = 60000;\nconst RELOAD_DELAY = 10000;\nconst SCHEDULE_CELL_SIZE = 18;\n\nconst BTN_ACTIVE = \'bg-neon-cyan bg-opacity-20 text-neon-cyan border-neon-cyan border-opacity-30\';\nconst BTN_INACTIVE = \'bg-cyber-accent text-gray-400 border-gray-700 hover:text-white\';\nconst BTN_MANUAL_ACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-purple\';\nconst BTN_MANUAL_INACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple hover:border-neon-purple\';\nconst BTN_AUTO_ACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-cyan bg-opacity-20 text-neon-cyan border border-neon-cyan border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-cyan\';\nconst BTN_AUTO_INACTIVE = \'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan\';\n\n// ============================================================================\n// PERSISTENT SETTINGS\n// ============================================================================\n\nconst settingsDefaults = {\n    tempUnit: \'celsius\',\n    refreshInterval: 0,\n    compactMode: false,\n    autoUpdateCheck: 21600000\n};\n\nlet _cachedSettings = null;\nlet _settingsCacheTime = 0;\nconst SETTINGS_CACHE_TTL = 1000;\n\nfunction getSettings() {\n    const now = Date.now();\n    if (_cachedSettings && (now - _settingsCacheTime) < SETTINGS_CACHE_TTL) {\n        return _cachedSettings;\n    }\n    try {\n        const raw = localStorage.getItem(\'fancontrol_settings\');\n        _cachedSettings = raw ? { ...settingsDefaults, ...JSON.parse(raw) } : { ...settingsDefaults };\n    } catch { _cachedSettings = { ...settingsDefaults }; }\n    _settingsCacheTime = now;\n    return _cachedSettings;\n}\n\nfunction saveSettings(partial) {\n    const s = getSettings();\n    Object.assign(s, partial);\n    localStorage.setItem(\'fancontrol_settings\', JSON.stringify(s));\n    _cachedSettings = s;\n    _settingsCacheTime = Date.now();\n    return s;\n}\n\nfunction formatTemp(celsius) {\n    if (celsius == null) return \'--\';\n    const s = getSettings();\n    if (s.tempUnit === \'fahrenheit\') {\n        return Math.round(celsius * 9 / 5 + 32) + \'°F\';\n    }\n    return celsius + \'°C\';\n}\n\nfunction getTempUnitSymbol() {\n    return getSettings().tempUnit === \'fahrenheit\' ? \'°F\' : \'°C\';\n}\n\n// Schedule state\nlet scheduleData = {};\nlet scheduleSelection = [];\nlet isDraggingSchedule = false;\nlet dragStartCell = null;\nlet editingCells = [];\nlet scheduleEditorSensors = [];\nlet expandedRuleGroups = new Set();\n\n// ============================================================================\n// I18N SYSTEM\n// ============================================================================\n\nlet currentLang = localStorage.getItem(\'fancontrol_lang\') || \'en\';\nlet translations = {};\n\nasync function loadLang(code) {\n    try {\n        const resp = await fetch(`/api/lang/${code}`);\n        if (resp.ok) {\n            translations = await resp.json();\n            currentLang = code;\n            localStorage.setItem(\'fancontrol_lang\', code);\n            applyTranslations();\n            return true;\n        }\n    } catch (e) {\n        console.error(\'[i18n] Failed to load lang:\', code, e);\n    }\n    return false;\n}\n\nfunction t(key, fallback) {\n    return translations[key] || fallback || key;\n}\n\nfunction applyTranslations() {\n    document.querySelectorAll(\'[data-i18n]\').forEach(el => {\n        const key = el.getAttribute(\'data-i18n\');\n        if (key && translations[key]) {\n            el.textContent = translations[key];\n        }\n    });\n    document.querySelectorAll(\'[data-i18n-title]\').forEach(el => {\n        const key = el.getAttribute(\'data-i18n-title\');\n        if (key && translations[key]) {\n            el.title = translations[key];\n        }\n    });\n    document.querySelectorAll(\'[data-i18n-placeholder]\').forEach(el => {\n        const key = el.getAttribute(\'data-i18n-placeholder\');\n        if (key && translations[key]) {\n            el.placeholder = translations[key];\n        }\n    });\n    // Update page title\n    const ver = currentState?.config_version;\n    if (translations[\'app.title\'] && ver) {\n        document.title = `${translations[\'app.title\']} v${ver}`;\n    }\n}\n\n// ============================================================================\n// UTILITIES\n// ============================================================================\n\nfunction escapeHtml(str) {\n    if (!str) return \'\';\n    return String(str).replace(/[&<>"\']/g, c => ({\n        \'&\': \'&amp;\', \'<\': \'&lt;\', \'>\': \'&gt;\', \'"\': \'&quot;\', "\'": \'&#39;\'\n    }[c]));\n}\n\nfunction show(el) { if (el) el.classList.remove(\'hidden\'); }\nfunction hide(el) { if (el) el.classList.add(\'hidden\'); }\nfunction toggle(el, visible) { if (el) el.classList.toggle(\'hidden\', !visible); }\n\nfunction setDiscoverButtonState(loading) {\n    const btn = document.getElementById(\'discover-btn\');\n    const loader = document.getElementById(\'discover-loader\');\n    if (btn) btn.disabled = loading;\n    if (loader) toggle(loader, loading);\n}\n\n// ============================================================================\n// SOCKET.IO CONNECTION\n// ============================================================================\n\nconsole.log(\'[FanControl] Establishing Socket.IO connection...\');\nconst socket = io();\nwindow.socket = socket;\n\nlet serverAvailable = true;\n\nsocket.on(\'disconnect\', () => {\n    serverAvailable = false;\n    showServerUnavailable();\n});\n\nsocket.on(\'connect\', () => {\n    serverAvailable = true;\n    hideServerUnavailable();\n});\n\nfunction showServerUnavailable() {\n    const banner = document.getElementById(\'server-unavailable-banner\');\n    if (banner) banner.classList.remove(\'hidden\');\n}\n\nfunction hideServerUnavailable() {\n    const banner = document.getElementById(\'server-unavailable-banner\');\n    if (banner) banner.classList.add(\'hidden\');\n}\n\nlet lastUIUpdate = 0;\nsocket.on(\'update\', (data) => {\n    currentState = data;\n    // Sync node data from server state\n    if (data.nodes) {\n        const nodeEntries = Object.entries(data.nodes);\n        for (const [nid, ndata] of nodeEntries) {\n            const idx = nodesData.findIndex(n => n.node_id === nid);\n            if (idx >= 0) {\n                Object.assign(nodesData[idx], ndata);\n            } else {\n                nodesData.push(ndata);\n            }\n        }\n        buildServerTree();\n    }\n    if (data.test_progress && data.testing) {\n        updateCalibrationModal(data.test_progress);\n    }\n    const interval = getSettings().refreshInterval;\n    if (interval === 0) {\n        updateUI(data);\n    } else {\n        const now = Date.now();\n        if (now - lastUIUpdate >= interval) {\n            lastUIUpdate = now;\n            updateUI(data);\n        }\n    }\n    // Show update button in sidebar for agent mode\n    const agentUpdateSection = document.getElementById(\'agent-update-section\');\n    if (agentUpdateSection) {\n        agentUpdateSection.classList.toggle(\'hidden\', !data.agent_mode);\n    }\n    // Hide "Add Node" section in agent mode (no server features)\n    const addNodeSection = document.getElementById(\'add-node-section\');\n    if (addNodeSection) {\n        addNodeSection.classList.toggle(\'hidden\', !!data.agent_mode);\n    }\n    // Show agent token in sidebar only in agent mode\n    const agentTokenSection = document.getElementById(\'agent-token-section\');\n    const agentTokenBanner = document.getElementById(\'agent-token-banner\');\n    const hasToken = data.api_token && data.api_token.length > 0;\n    if (agentTokenSection) {\n        agentTokenSection.classList.toggle(\'hidden\', !data.agent_mode || !hasToken);\n        if (hasToken) document.getElementById(\'agent-token-value\').textContent = data.api_token;\n    }\n    // Hide the big banner — token is already in sidebar\n    if (agentTokenBanner) {\n        agentTokenBanner.classList.add(\'hidden\');\n    }\n    // DSM scheme view is accessed by clicking DSM fans in tree — no nav button needed\n});\n\nsocket.on(\'hardware_discovered\', (data) => {\n    console.log(\'[FanControl] Hardware discovered:\', data);\n    if (wizardStep === \'intro\' || wizardStep === \'scanning\') {\n        renderDiscoveredHardware(data);\n        wizardStep = \'results\';\n    }\n});\n\nsocket.on(\'test_progress\', (progress) => {\n    console.log(\'[FanControl] Calibration progress:\', progress);\n    updateCalibrationModal(progress);\n});\n\nsocket.on(\'hidden_sensors\', (data) => {\n    _hiddenSensors = data.hiddenSensors || [];\n    buildServerTree();\n});\n\nsocket.on(\'test_complete\', (result) => {\n    console.log(\'[FanControl] Calibration complete:\', result);\n    hideCalibrationModal();\n    \n    if (result.success) {\n        wizardStep = \'done\';\n        currentState = { ...currentState, initialized: true, tested: true };\n        showMainScreen();\n    }\n});\n\n// ============================================================================\n// UI UPDATE FUNCTIONS\n// ============================================================================\n\nfunction updateUI(data) {\n    if (!data) return;\n    \n    // Update version displays\n    const ver = data.config_version || \'\';\n    const headerVer = document.getElementById(\'header-version\');\n    if (headerVer && ver) headerVer.textContent = `v${ver}`;\n    const versionLink = document.getElementById(\'version-link\');\n    if (versionLink && ver) versionLink.textContent = `FanControl Web v${ver}`;\n    \n    // Show appropriate screen\n    if (!data.initialized || !data.tested) {\n        showSetupScreen();\n        if (data.hardware_scanned && wizardStep === \'intro\') {\n            renderDiscoveredHardware({\n                fans: data.fans,\n                temps: data.temp_sensors,\n                disks: data.hdd_sensors\n            });\n            wizardStep = \'results\';\n            setDiscoverButtonState(false);\n        }\n        return;\n    }\n    \n    showMainScreen();\n    \n    // Update indicators\n    updateFailsafeIndicator(data.failsafe);\n    updateStandbyIndicator(data.standby_mode);\n    \n    // Build fan list if needed\n    if (data.fans && Object.keys(data.fans).length > 0) {\n        buildFanList(data.fans);\n    }\n    \n    // Build disks list\n    if (data.hdd_sensors) {\n        buildDisksList(data.hdd_sensors);\n    }\n    \n    // Build sensor list for popup\n    buildSensorList(data);\n    \n    // Update inspector if a fan is selected\n    if (currentFanId && data.fans && data.fans[currentFanId]) {\n        updateInspector(data.fans[currentFanId]);\n    }\n    \n    // Update chart\n    updateChart();\n\n    // Refresh server tree\n    if (_dashboardLoaded) buildServerTree();\n\n    // Dashboard live updates handled by startPickerLiveUpdate\n}\n\nfunction showSetupScreen() {\n    document.getElementById(\'setup-screen\').classList.remove(\'hidden\');\n    document.getElementById(\'main-screen\').classList.add(\'hidden\');\n    stopPickerLiveUpdate();\n    stopSystemUpdate();\n    // Close settings panel if open\n    const overlay = document.getElementById(\'settings-overlay\');\n    const panel = document.getElementById(\'settings-panel\');\n    if (overlay) overlay.classList.add(\'hidden\');\n    if (panel) panel.classList.add(\'hidden\');\n}\n\nfunction showMainScreen() {\n    const mainScreen = document.getElementById(\'main-screen\');\n    const wasOnSetup = mainScreen?.classList.contains(\'hidden\');\n\n    document.getElementById(\'setup-screen\').classList.add(\'hidden\');\n    mainScreen?.classList.remove(\'hidden\');\n    if (!currentState || !currentState.testing) {\n        hideCalibrationModal();\n    }\n    if (wasOnSetup) showView(\'dashboard\');\n    updateCanvasColumns();\n    if (wasOnSetup) {\n        loadPickerCards().then(() => {\n            buildServerTree();\n            startPickerLiveUpdate();\n            startSystemUpdate();\n        });\n    } else {\n        if (!_pickerLiveTimer) startPickerLiveUpdate();\n        startSystemUpdate();\n    }\n}\n\nfunction updateFailsafeIndicator(failsafe) {\n    const el = document.getElementById(\'failsafe-indicator\');\n    if (failsafe) {\n        el.classList.remove(\'hidden\');\n    } else {\n        el.classList.add(\'hidden\');\n    }\n}\n\nfunction updateStandbyIndicator(standby) {\n    const el = document.getElementById(\'standby-indicator\');\n    if (standby) {\n        el.classList.remove(\'hidden\');\n    } else {\n        el.classList.add(\'hidden\');\n    }\n}\n\n// ============================================================================\n// FAN LIST (Left Panel)\n// ============================================================================\n\nfunction buildFanList(fans) {\n    const container = document.getElementById(\'fan-list\');\n    if (!container) return;\n    \n    let html = \'\';\n    \n    for (const [fanId, fan] of Object.entries(fans)) {\n        const isSelected = fanId === currentFanId;\n        const borderColor = isSelected ? \'border-neon-purple\' : \'border-cyber-accent\';\n        const bgColor = isSelected ? \'bg-cyber-accent\' : \'bg-cyber-card\';\n        \n        html += `\n            <div id="fan-card-${escapeHtml(fanId)}" \n                 class="fan-card ${bgColor} border ${borderColor} rounded-lg p-3 cursor-pointer \n                        hover:border-neon-purple transition-all duration-200"\n                 onclick="selectFan(\'${escapeHtml(fanId)}\')">\n                <div class="flex items-center justify-between mb-1">\n                    <span class="text-sm font-semibold text-white truncate">${escapeHtml(fan.label)}</span>\n                    <div class="flex items-center gap-1">\n                        ${fan.inverted ? `<span class="text-xs px-1.5 py-0.5 rounded bg-cyan-900 bg-opacity-30 text-neon-cyan">${t(\'fan.inv\', \'INV\')}</span>` : \'\'}\n                        <span class="text-xs px-1.5 py-0.5 rounded ${getStatusBadgeClass(fan.status)}">${t(\'status.\' + fan.status, fan.status)}</span>\n                    </div>\n                </div>\n                <div class="flex items-center justify-between text-xs">\n                    <span class="text-gray-500">${t(\'mode.\' + (fan.mode || \'manual\'), fan.mode || \'manual\')}</span>\n                    <span class="font-mono text-neon-cyan" id="fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0} ${t(\'fan.rpm\', \'RPM\')}</span>\n                </div>\n            </div>\n        `;\n    }\n    \n    container.innerHTML = html || `<div class="text-center text-gray-500 py-8">${t(\'setup.no_fans\', \'No fans detected\')}</div>`;\n}\n\nfunction selectFan(fanId) {\n    currentFanId = fanId;\n    \n    // Update card highlights\n    document.querySelectorAll(\'.fan-card\').forEach(card => {\n        card.classList.remove(\'border-neon-purple\', \'bg-cyber-accent\');\n        card.classList.add(\'border-cyber-accent\', \'bg-cyber-card\');\n    });\n    \n    const selectedCard = document.getElementById(`fan-card-${fanId}`);\n    if (selectedCard) {\n        selectedCard.classList.add(\'border-neon-purple\', \'bg-cyber-accent\');\n        selectedCard.classList.remove(\'border-cyber-accent\', \'bg-cyber-card\');\n    }\n    \n    // Show inspector\n    if (currentState && currentState.fans && currentState.fans[fanId]) {\n        updateInspector(currentState.fans[fanId]);\n    }\n}\n\n// ============================================================================\n// NODE TREE\n// ============================================================================\n\nfunction buildServerTree() {\n    const container = document.getElementById(\'server-tree\');\n    if (!container) return;\n\n    let html = \'\';\n\n    // Local server\n    html += renderLocalServerTree();\n\n    // Remote nodes\n    for (const node of nodesData) {\n        html += renderRemoteNodeTree(node);\n    }\n\n    container.innerHTML = html || `<div class="text-center text-gray-500 py-4 text-xs">${t(\'nodes.no_nodes\', \'No nodes connected\')}</div>`;\n\n    _collapsedNodes.forEach(nodeId => {\n        const children = document.getElementById(`node-children-${nodeId}`);\n        if (children) children.classList.add(\'hidden\');\n    });\n}\n\nfunction getHiddenSensors() {\n    return _hiddenSensors || [];\n}\n\nfunction setHiddenSensors(hidden) {\n    _hiddenSensors = hidden;\n    scheduleDashboardSave();\n}\n\nfunction hideSensor(sensorId) {\n    const el = document.querySelector(`[data-sensor-id="${sensorId}"]`);\n    if (el) {\n        el.style.transition = \'opacity 0.3s, max-height 0.3s, margin 0.3s, padding 0.3s\';\n        el.style.overflow = \'hidden\';\n        el.style.opacity = \'0\';\n        el.style.maxHeight = \'0\';\n        el.style.marginTop = \'0\';\n        el.style.marginBottom = \'0\';\n        el.style.paddingTop = \'0\';\n        el.style.paddingBottom = \'0\';\n        setTimeout(() => {\n            const hidden = getHiddenSensors();\n            if (!hidden.includes(sensorId)) {\n                setHiddenSensors([...hidden, sensorId]);\n            }\n            buildServerTree();\n        }, 320);\n    } else {\n        const hidden = getHiddenSensors();\n        if (!hidden.includes(sensorId)) {\n            setHiddenSensors([...hidden, sensorId]);\n        }\n        buildServerTree();\n    }\n}\n\nfunction restoreSensor(sensorId) {\n    setHiddenSensors(getHiddenSensors().filter(id => id !== sensorId));\n    buildServerTree();\n}\n\nfunction restoreAllSensors() {\n    setHiddenSensors([]);\n    buildServerTree();\n}\n\nfunction renderLocalServerTree() {\n    if (!currentState || !currentState.fans) return \'\';\n\n    const fans = currentState.fans;\n    const temps = currentState.temp_sensors || {};\n    const disks = currentState.hdd_sensors || {};\n    const hidden = getHiddenSensors();\n\n    const visibleFans = Object.entries(fans).filter(([id]) => !hidden.includes(`fan:${id}`));\n    const visibleTemps = Object.entries(temps).filter(([id]) => !hidden.includes(`temp:${id}`));\n    const visibleDisks = Object.entries(disks).filter(([id]) => !hidden.includes(`disk:${id}`));\n    const hiddenFans = Object.entries(fans).filter(([id]) => hidden.includes(`fan:${id}`));\n    const hiddenTemps = Object.entries(temps).filter(([id]) => hidden.includes(`temp:${id}`));\n    const hiddenDisks = Object.entries(disks).filter(([id]) => hidden.includes(`disk:${id}`));\n    const hasHidden = hiddenFans.length + hiddenTemps.length + hiddenDisks.length > 0;\n\n    let html = `\n        <div class="node-group" data-node="local">\n            <div class="flex items-center gap-2 p-2 rounded hover:bg-cyber-accent cursor-pointer node-header"\n                 onclick="toggleNodeGroup(\'local\')">\n                <span class="text-neon-cyan text-xs">▼</span>\n                <span class="text-sm font-semibold text-white">🖥 ${escapeHtml(currentState.server_name || t(\'nodes.local_server\', \'My Server\'))}</span>\n                <span class="ml-auto text-xs bg-green-900 bg-opacity-30 text-neon-green px-1.5 py-0.5 rounded">${visibleFans.length} ${t(\'nodes.fans\', \'fans\')}</span>\n            </div>\n            <div class="node-children ml-4 space-y-px" id="node-children-local">\n    `;\n\n    for (const [fanId, fan] of visibleFans) {\n        const isSelected = fanId === currentFanId;\n        html += `\n            <div data-sensor-id="fan:${escapeHtml(fanId)}" class="flex items-center gap-1.5 p-1 rounded cursor-pointer transition-all group ${isSelected ? \'bg-cyber-accent border-l-2 border-neon-purple\' : \'hover:bg-cyber-accent border-l-2 border-transparent\'}"\n                 onclick="selectFanFromTree(\'${escapeHtml(fanId)}\', \'local\')">\n                <span class="text-xs">🌀</span>\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(fan.label)}</span>\n                <span class="ml-auto text-xs font-mono text-neon-cyan" id="tree-fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0}</span>\n                <button onclick="event.stopPropagation(); hideSensor(\'fan:${escapeHtml(fanId)}\')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>\n            </div>\n        `;\n    }\n\n    for (const [sensorId, sensor] of visibleTemps) {\n        html += `\n            <div data-sensor-id="temp:${escapeHtml(sensorId)}" class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                <span class="text-xs">🌡</span>\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(sensor.label)}</span>\n                <span class="ml-auto text-xs font-mono text-neon-green">${sensor.value || 0}°C</span>\n                <button onclick="event.stopPropagation(); hideSensor(\'temp:${escapeHtml(sensorId)}\')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>\n            </div>\n        `;\n    }\n\n    for (const [diskId, disk] of visibleDisks) {\n        html += `\n            <div data-sensor-id="disk:${escapeHtml(diskId)}" class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                <span class="text-xs">💾</span>\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(disk.label || diskId)}</span>\n                <span class="ml-auto text-xs font-mono ${getTempColorClass(disk.temp)}">${disk.temp > 0 ? disk.temp + \'°C\' : \'--\'}</span>\n                <button onclick="event.stopPropagation(); hideSensor(\'disk:${escapeHtml(diskId)}\')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>\n            </div>\n        `;\n    }\n\n    if (hasHidden) {\n        const totalHidden = hiddenFans.length + hiddenTemps.length + hiddenDisks.length;\n        const isHiddenExpanded = !_collapsedNodes.has(\'local-hidden\');\n        const arrowChar = isHiddenExpanded ? \'▼\' : \'▶\';\n        html += `\n            <div class="mt-1 border-t border-gray-700/50 pt-1">\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent cursor-pointer"\n                     onclick="toggleNodeGroup(\'local-hidden\')">\n                    <span class="text-neon-cyan text-[10px]">${arrowChar}</span>\n                    <span class="text-[10px] text-gray-500">Удалённые (${totalHidden})</span>\n                    <button onclick="event.stopPropagation(); restoreAllSensors()" class="ml-auto text-[10px] text-gray-600 hover:text-neon-green px-1">↺ все</button>\n                </div>\n                <div class="node-children ml-4 space-y-px ${isHiddenExpanded ? \'\' : \'hidden\'}" id="node-children-local-hidden">\n        `;\n\n        for (const [fanId, fan] of hiddenFans) {\n            html += `\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                    <span class="text-xs opacity-50">🌀</span>\n                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(fan.label)}</span>\n                    <button onclick="restoreSensor(\'fan:${escapeHtml(fanId)}\')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="Восстановить">↺</button>\n                </div>\n            `;\n        }\n        for (const [sensorId, sensor] of hiddenTemps) {\n            html += `\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                    <span class="text-xs opacity-50">🌡</span>\n                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(sensor.label)}</span>\n                    <button onclick="restoreSensor(\'temp:${escapeHtml(sensorId)}\')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="Восстановить">↺</button>\n                </div>\n            `;\n        }\n        for (const [diskId, disk] of hiddenDisks) {\n            html += `\n                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">\n                    <span class="text-xs opacity-50">💾</span>\n                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(disk.label || diskId)}</span>\n                    <button onclick="restoreSensor(\'disk:${escapeHtml(diskId)}\')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="Восстановить">↺</button>\n                </div>\n            `;\n        }\n\n        html += `</div></div>`;\n    }\n\n    html += `</div></div>`;\n    return html;\n}\n\nfunction renderRemoteNodeTree(node) {\n    const telemetry = node.telemetry || {};\n    const fans = telemetry.fans || {};\n    const temps = telemetry.temp_sensors || {};\n    const disks = telemetry.hdd_sensors || {};\n    const fanCount = Object.keys(fans).length;\n    const statusColor = node.status === \'online\' ? \'text-neon-green\' : \'text-gray-500\';\n    const statusDot = node.status === \'online\' ? \'bg-neon-green\' : \'bg-gray-500\';\n\n    let html = `\n        <div class="node-group" data-node="${escapeHtml(node.node_id)}">\n            <div class="flex items-center gap-1 p-2 rounded hover:bg-cyber-accent cursor-pointer node-header group"\n                 onclick="toggleNodeGroup(\'${escapeHtml(node.node_id)}\')">\n                <span class="w-2 h-2 ${statusDot} rounded-full flex-shrink-0"></span>\n                <span class="text-sm font-semibold text-white truncate flex-1">🖥 ${escapeHtml(node.name)}</span>\n                <span class="text-xs ${statusColor} flex-shrink-0">${node.status}</span>\n                <button onclick="event.stopPropagation(); showNodeSettings(\'${escapeHtml(node.node_id)}\')"\n                        class="w-5 h-5 flex items-center justify-center text-gray-400 hover:text-neon-cyan hover:bg-gray-700 rounded text-[11px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Settings">&#9881;</button>\n                <button onclick="event.stopPropagation(); deleteNode(\'${escapeHtml(node.node_id)}\')"\n                        class="w-5 h-5 flex items-center justify-center text-gray-400 hover:text-red-400 hover:bg-red-900/40 rounded text-[11px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Delete">X</button>\n            </div>\n            <div class="node-children ml-4 space-y-0.5 ${_collapsedNodes.has(node.node_id) ? \'hidden\' : \'\'}" id="node-children-${escapeHtml(node.node_id)}">\n    `;\n\n    for (const [fanId, fan] of Object.entries(fans)) {\n        const cleanLabel = (fan.label || fanId).replace(/\\s*\\(Synology-[^)]+\\)/, \'\');\n        const isDsm = fan.control_method === \'dsm_scemd\';\n        html += `\n            <div class="flex items-center gap-2 p-1.5 rounded cursor-pointer hover:bg-cyber-accent"\n                 onclick="selectNodeFan(\'${escapeHtml(node.node_id)}\', \'${escapeHtml(fanId)}\')">\n                <span class="text-xs">🌀</span>\n                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(cleanLabel)}${isDsm ? \' <span class="text-blue-400 text-[10px]">DSM</span>\' : \'\'}</span>\n                <span class="ml-auto text-xs font-mono text-neon-cyan">${fan.rpm || 0}</span>\n            </div>\n        `;\n    }\n\n    for (const [sensorId, sensor] of Object.entries(temps)) {\n        html += `\n            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">\n                <span class="text-xs">🌡</span>\n                <span class="text-xs text-gray-300 truncate">${escapeHtml(sensor.label || sensorId)}</span>\n                <span class="ml-auto text-xs font-mono text-neon-green">${sensor.value || 0}°C</span>\n            </div>\n        `;\n    }\n\n    for (const [diskId, disk] of Object.entries(disks)) {\n        html += `\n            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">\n                <span class="text-xs">💾</span>\n                <span class="text-xs text-gray-300 truncate">${escapeHtml(disk.label || diskId)}</span>\n                <span class="ml-auto text-xs font-mono ${getTempColorClass(disk.temp)}">${disk.temp > 0 ? disk.temp + \'°C\' : \'--\'}</span>\n            </div>\n        `;\n    }\n\n    if (fanCount === 0 && Object.keys(temps).length === 0 && Object.keys(disks).length === 0) {\n        html += `<div class="text-xs text-gray-600 p-1.5">No telemetry</div>`;\n    }\n\n    html += `</div></div>`;\n    return html;\n}\n\nlet _collapsedNodes = new Set(JSON.parse(localStorage.getItem(\'fc_collapsed_nodes\') || \'[]\'));\n\nfunction toggleNodeGroup(nodeId) {\n    const children = document.getElementById(`node-children-${nodeId}`);\n    if (children) {\n        children.classList.toggle(\'hidden\');\n        if (children.classList.contains(\'hidden\')) {\n            _collapsedNodes.add(nodeId);\n        } else {\n            _collapsedNodes.delete(nodeId);\n        }\n        localStorage.setItem(\'fc_collapsed_nodes\', JSON.stringify([..._collapsedNodes]));\n    }\n}\n\nfunction selectFanFromTree(fanId, source) {\n    currentFanId = fanId;\n\n    // Check if this is a DSM fan — open scheme editor instead of inspector\n    if (currentState && currentState.fans && currentState.fans[fanId]) {\n        const fan = currentState.fans[fanId];\n        if (fan.control_method === \'dsm_scemd\') {\n            showView(\'dsm-scheme\');\n            buildServerTree();\n            return;\n        }\n    }\n\n    // Show inspector view\n    showView(\'inspector\');\n\n    // Update inspector\n    if (source === \'local\' && currentState && currentState.fans && currentState.fans[fanId]) {\n        updateInspector(currentState.fans[fanId]);\n    }\n\n    // Rebuild server tree to highlight selected\n    buildServerTree();\n}\n\nfunction selectNodeFan(nodeId, fanId) {\n    // Check if this is a DSM fan on a remote node\n    const node = nodesData.find(n => n.node_id === nodeId);\n    if (node && node.telemetry && node.telemetry.fans && node.telemetry.fans[fanId]) {\n        const fan = node.telemetry.fans[fanId];\n        if (fan.control_method === \'dsm_scemd\') {\n            _currentRemoteNodeId = nodeId;\n            showView(\'dsm-scheme\');\n            renderDsmSchemeEditor(nodeId);\n            return;\n        }\n    }\n    console.log(\'[FanControl] Select node fan:\', nodeId, fanId);\n}\n\nlet _currentRemoteNodeId = null;\n\n// ============================================================================\n// DASHBOARD CARDS\n// ============================================================================\n\nfunction showCardPicker() {\n    const modal = document.getElementById(\'card-picker-modal\');\n    if (!modal) return;\n    modal.classList.remove(\'hidden\');\n    populatePickerSources();\n    updatePickerElements();\n}\n\nfunction hideCardPicker() {\n    const modal = document.getElementById(\'card-picker-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n}\n\nfunction populatePickerSources() {\n    const select = document.getElementById(\'picker-source\');\n    if (!select) return;\n    select.innerHTML = \'<option value="local">My Server (local)</option>\';\n    for (const node of nodesData) {\n        select.innerHTML += `<option value="${escapeHtml(node.node_id)}">${escapeHtml(node.name || node.node_id)}</option>`;\n    }\n}\n\nfunction updatePickerElements() {\n    const type = document.getElementById(\'picker-type\')?.value;\n    const source = document.getElementById(\'picker-source\')?.value;\n    const container = document.getElementById(\'picker-elements\');\n    if (!container) return;\n\n    let elements = [];\n\n    if (source === \'local\') {\n        if (type === \'fan\' && currentState?.fans) {\n            elements = Object.entries(currentState.fans).map(([id, f]) => ({ id, label: f.label || id, extra: `${f.rpm || 0} RPM` }));\n        } else if (type === \'temperature\' && currentState?.temp_sensors) {\n            elements = Object.entries(currentState.temp_sensors).map(([id, s]) => ({ id, label: s.label || id, extra: `${s.value || 0}°C` }));\n        } else if (type === \'disk\' && currentState?.hdd_sensors) {\n            elements = Object.entries(currentState.hdd_sensors).map(([id, d]) => ({ id, label: d.label || id, extra: `${d.temp || 0}°C` }));\n        } else if (type === \'system\') {\n            elements = [\n                { id: \'max_temp\', label: t(\'picker.max_temp\', \'Макс. температура\'), extra: `${currentState?.max_hdd_temp || \'--\'}°C` },\n                { id: \'fans_summary\', label: t(\'picker.fans_summary\', \'Сводка по вентиляторам\'), extra: \'\' },\n            ];\n        }\n    } else {\n        const node = nodesData.find(n => n.node_id === source);\n        if (node?.telemetry) {\n            const tel = node.telemetry;\n            if (type === \'fan\' && tel.fans) {\n                elements = Object.entries(tel.fans).map(([id, f]) => ({ id, label: f.label || id, extra: `${f.rpm || 0} RPM` }));\n            } else if (type === \'temperature\' && tel.temp_sensors) {\n                elements = Object.entries(tel.temp_sensors).map(([id, s]) => ({ id, label: s.label || id, extra: `${s.value || 0}°C` }));\n            } else if (type === \'disk\' && tel.hdd_sensors) {\n                elements = Object.entries(tel.hdd_sensors).map(([id, d]) => ({ id, label: d.label || id, extra: `${d.temp || 0}°C` }));\n            }\n        }\n    }\n\n    container.innerHTML = elements.length > 0\n        ? elements.map(el => {\n            const cardId = `picker-${source}-${el.id}`;\n            const exists = document.querySelector(`[data-card-id="${cardId}"]`);\n            return `<label class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">\n                <input type="checkbox" value="${escapeHtml(el.id)}" data-label="${escapeHtml(el.label)}" class="picker-checkbox rounded" ${exists ? \'checked disabled\' : \'\'}>\n                <span class="text-xs ${exists ? \'text-gray-500 line-through\' : \'text-gray-300\'}">${escapeHtml(el.label)}</span>\n                <span class="ml-auto text-xs text-gray-500">${exists ? t(\'picker.added\', \'добавлено\') : el.extra}</span>\n            </label>`;\n        }).join(\'\')\n        : `<div class="text-xs text-gray-500 text-center py-4">${t(\'picker.no_elements\', \'Элементы не найдены\')}</div>`;\n}\n\nfunction addSelectedCards() {\n    const type = document.getElementById(\'picker-type\')?.value;\n    const source = document.getElementById(\'picker-source\')?.value;\n    const checkboxes = document.querySelectorAll(\'.picker-checkbox:checked\');\n    if (!checkboxes.length) return;\n\n    const saved = getPickerCards();\n\n    checkboxes.forEach(cb => {\n        const cardId = `picker-${source}-${cb.value}`;\n        if (document.querySelector(`[data-card-id="${cardId}"]`)) return;\n        if (saved.some(c => c.id === cardId)) return;\n\n        const label = cb.dataset.label || cb.value;\n        const colSpan = 3;\n        const pos = findFreePosition(saved, colSpan, 1, null);\n        const cardData = { id: cardId, type, source, sourceId: cb.value, label, col: pos.col, row: pos.row, colSpan };\n        renderPickerCard(cardData);\n        saved.push(cardData);\n    });\n\n    setPickerCards(saved);\n    document.getElementById(\'dashboard-empty\')?.classList.add(\'hidden\');\n    hideCardPicker();\n    startPickerLiveUpdate();\n}\n\nfunction renderPickerCard(card) {\n    const { id, type, source, sourceId, label } = card;\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    let icon = \'📊\';\n    let colorClass = \'text-neon-cyan\';\n    let valueHtml = \'\';\n\n    if (type === \'fan\') {\n        const fanData = getFanData(source, sourceId);\n        const fanStatus = fanData?.status || \'unknown\';\n        const rpm = fanData?.rpm || 0;\n        const dotColor = fanStatus === \'running\' ? \'green\' : (fanStatus === \'failsafe\' || fanStatus === \'critical\') ? \'red\' : \'yellow\';\n        const fanColor = fanStatus === \'running\' ? \'#22d3ee\' : (fanStatus === \'failsafe\' || fanStatus === \'critical\') ? \'#ef4444\' : \'#facc15\';\n        const animDuration = rpm > 0 ? Math.max(0.2, 2 - (rpm / 1500)) : 0;\n        const animStyle = rpm > 0 ? `animation: fan-spin ${animDuration}s linear infinite` : \'\';\n        icon = `<svg class="w-8 h-8 inline-block" data-fan-anim-id="${sourceId}" data-fan-source="${source}" viewBox="0 0 100 100" style="${animStyle}">\n            <g fill="${fanColor}" opacity="0.9">\n                <path d="M50 50 Q30 20 50 5 Q70 20 50 50"/>\n                <path d="M50 50 Q80 30 95 50 Q80 70 50 50"/>\n                <path d="M50 50 Q70 80 50 95 Q30 80 50 50"/>\n                <path d="M50 50 Q20 70 5 50 Q20 30 50 50"/>\n            </g>\n            <circle cx="50" cy="50" r="6" fill="${fanColor}" opacity="0.6"/>\n        </svg> <span class="status-dot ${dotColor}"></span>`;\n        colorClass = \'text-neon-cyan\';\n        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-fan-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">RPM</span></div>`;\n        valueHtml += renderSparkline(`fan:${source}:${sourceId}`, \'#22d3ee\');\n    } else if (type === \'temperature\') {\n        icon = \'🌡\';\n        colorClass = \'text-neon-green\';\n        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-temp-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">°C</span></div>`;\n        valueHtml += renderSparkline(`temp:${source}:${sourceId}`, \'#4ade80\');\n    } else if (type === \'disk\') {\n        icon = \'💾\';\n        colorClass = \'text-neon-purple\';\n        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-disk-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">°C</span></div>`;\n        valueHtml += renderSparkline(`disk:${source}:${sourceId}`, \'#c084fc\');\n    } else if (type === \'system\') {\n        icon = \'🖥\';\n        colorClass = \'text-yellow-400\';\n        valueHtml = `\n        <div class="space-y-2 mt-1">\n            <div class="flex justify-between text-xs">\n                <span class="text-gray-500">Uptime</span>\n                <span class="text-gray-300 font-mono" data-system-field="uptime">--</span>\n            </div>\n            <div>\n                <div class="flex justify-between text-xs mb-1">\n                    <span class="text-gray-500">CPU</span>\n                    <span class="text-gray-300 font-mono" data-system-field="cpu">--%</span>\n                </div>\n                <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">\n                    <div class="h-full bg-cyan-400 rounded-full transition-all duration-500" data-system-bar="cpu" style="width:0%"></div>\n                </div>\n            </div>\n            <div>\n                <div class="flex justify-between text-xs mb-1">\n                    <span class="text-gray-500">RAM</span>\n                    <span class="text-gray-300 font-mono" data-system-field="mem">--%</span>\n                </div>\n                <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">\n                    <div class="h-full bg-purple-400 rounded-full transition-all duration-500" data-system-bar="mem" style="width:0%"></div>\n                </div>\n            </div>\n        </div>`;\n    } else {\n        valueHtml = `<div class="text-2xl font-bold font-mono text-neon-cyan">--</div>`;\n    }\n\n    const configBtn = type === \'fan\'\n        ? `<button onclick="event.stopPropagation(); showCardConfig(\'${id}\')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Configure">⚙</button>`\n        : type === \'disk\'\n        ? `<button onclick="event.stopPropagation(); showSmartModal(\'${id}\')" class="text-gray-600 hover:text-neon-purple text-xs transition-colors" title="SMART">⚙</button>`\n        : \'\';\n    const lockIcon = card.lockSize ? \'🔒\' : \'🔓\';\n    const lockClass = card.lockSize ? \'text-neon-cyan\' : \'text-gray-600\';\n    const lockBtn = `<button onclick="event.stopPropagation(); toggleCardLockSize(\'${id}\')" class="lock-size-btn ${lockClass} hover:text-neon-cyan text-xs transition-colors" title="Lock/Unlock size">${lockIcon}</button>`;\n    const editBtn = `<button onclick="event.stopPropagation(); showCardEdit(\'${id}\')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Edit name">✎</button>`;\n    const removeBtn = `<button onclick="event.stopPropagation(); removePickerCard(\'${id}\')" class="text-gray-600 hover:text-red-400 text-xs transition-colors">×</button>`;\n\n    const el = document.createElement(\'div\');\n    const gradientClass = `card-gradient-${type}`;\n    el.className = `border border-cyber-accent rounded-xl p-4 transition-[border-color,box-shadow,background-image] duration-200 hover:border-neon-cyan/50 hover:shadow-neon-cyan/10 hover:shadow-lg cursor-grab active:cursor-grabbing ${gradientClass}`;\n    el.setAttribute(\'data-card-id\', id);\n    el.innerHTML = `\n        <div class="card-content overflow-hidden h-full">\n            <div class="flex items-center justify-between mb-3">\n                <div class="flex items-center gap-2">\n                    <span class="text-gray-600 text-xs select-none">⠿</span>\n                    <span class="text-lg">${icon}</span>\n                    <span class="text-sm text-gray-300 font-medium truncate">${escapeHtml(label)}</span>\n                </div>\n            <div class="flex items-center gap-1">\n                ${configBtn}${lockBtn}${editBtn}${removeBtn}\n            </div>\n            </div>\n            ${valueHtml}\n            <div class="card-details"></div>\n        </div>\n        <div class="card-resize-handle"></div>`;\n\n    el.addEventListener(\'mousedown\', onCardMouseDown);\n\n    if (!card.col || !card.row) {\n        const saved = getPickerCards().filter(c => c.id !== card.id);\n        const pos = findFreePosition(saved, card.colSpan || 3, 1, card.id);\n        card.col = pos.col;\n        card.row = pos.row;\n    }\n    el.style.gridColumn = `${card.col} / span ${card.colSpan || 3}`;\n    el.style.gridRow = `${card.row} / span ${card.rowSpan || 1}`;\n    el.style.position = \'relative\';\n    el.style.alignSelf = \'stretch\';\n    el.style.minWidth = \'0\';\n\n    canvas.appendChild(el);\n\n    const resizeHandle = el.querySelector(\'.card-resize-handle\');\n    if (resizeHandle) {\n        resizeHandle.addEventListener(\'mousedown\', (e) => onCardResizeStart(e, id));\n        if (card.lockSize) resizeHandle.style.display = \'none\';\n    }\n    if (card.lockSize) el.style.cursor = \'default\';\n\n    if (type === \'disk\') {\n        el.addEventListener(\'click\', (e) => {\n            if (_cardDragOccurred || e.target.closest(\'button\')) return;\n            showSmartModal(id);\n        });\n    }\n\n    updateCardDetails(id);\n}\n\nfunction snapCardToGrid(cardEl) {\n    const cardId = cardEl.dataset?.cardId;\n    if (!cardId) return;\n    if (_cardMouseDown?.cardId === cardId || _cardResizing?.cardId === cardId) return;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    const current = card.rowSpan || 1;\n    const needed = computeMinRows(cardEl);\n\n    if (needed !== current) {\n        const delta = needed - current;\n        const oldBottom = card.row + current;\n        const cardColStart = card.col || 1;\n        const cardColEnd = cardColStart + (card.colSpan || 3) - 1;\n        card.rowSpan = needed;\n        cardEl.style.gridRow = `${card.row} / span ${needed}`;\n\n        for (const c of saved) {\n            if (c.id === card.id || !c.col || !c.row) continue;\n            const cColStart = c.col;\n            const cColEnd = cColStart + (c.colSpan || 3) - 1;\n            if (c.row >= oldBottom && cColStart <= cardColEnd && cColEnd >= cardColStart) {\n                c.row += delta;\n                const el = document.querySelector(`[data-card-id="${c.id}"]`);\n                if (el) el.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;\n            }\n        }\n\n        setPickerCards(saved);\n    }\n}\n\nlet _cardDragOccurred = false;\n\nfunction toggleCardLockSize(cardId) {\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    card.lockSize = !card.lockSize;\n    setPickerCards(saved);\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (!el) return;\n    const btn = el.querySelector(\'.lock-size-btn\');\n    if (btn) {\n        btn.textContent = card.lockSize ? \'🔒\' : \'🔓\';\n        btn.className = card.lockSize\n            ? \'lock-size-btn text-neon-cyan hover:text-neon-cyan text-xs transition-colors\'\n            : \'lock-size-btn text-gray-600 hover:text-neon-cyan text-xs transition-colors\';\n    }\n    const handle = el.querySelector(\'.card-resize-handle\');\n    if (handle) handle.style.display = card.lockSize ? \'none\' : \'\';\n    el.style.cursor = card.lockSize ? \'default\' : \'grab\';\n}\nlet _dropTarget = null;\n\nlet _cardResizing = null;\nlet _cardResizeStartX = 0;\nlet _cardResizeStartY = 0;\nlet _cardResizeStartW = 0;\nlet _cardResizeStartH = 0;\nlet _cardResizeMinRowSpan = 1;\n\nfunction computeMinRows(el) {\n    const contentEl = el.querySelector(\'.card-content\');\n    el.style.alignSelf = \'start\';\n    if (contentEl) { contentEl.style.height = \'auto\'; contentEl.style.overflow = \'visible\'; }\n    void el.offsetHeight;\n    const contentH = contentEl ? contentEl.scrollHeight : 0;\n    const padV = parseFloat(getComputedStyle(el).paddingTop) + parseFloat(getComputedStyle(el).paddingBottom);\n    el.style.alignSelf = \'stretch\';\n    if (contentEl) { contentEl.style.height = \'\'; contentEl.style.overflow = \'\'; }\n    for (let r = 1; r <= 10; r++) {\n        if (contentH <= r * 100 - padV - 2 + 10) return r;\n    }\n    return 10;\n}\n\nfunction onCardResizeStart(e, cardId) {\n    e.preventDefault();\n    e.stopPropagation();\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (!el) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (card?.lockSize) return;\n\n    _cardResizeMinRowSpan = computeMinRows(el);\n\n    _cardResizing = { cardId, el, col: card?.col, row: card?.row };\n    _cardResizeStartX = e.clientX;\n    _cardResizeStartY = e.clientY;\n    _cardResizeStartW = el.offsetWidth;\n    _cardResizeStartH = el.offsetHeight;\n\n    el.setAttribute(\'draggable\', \'false\');\n    document.body.style.cursor = \'se-resize\';\n    document.body.style.userSelect = \'none\';\n\n    document.addEventListener(\'mousemove\', onCardResizeMove);\n    document.addEventListener(\'mouseup\', onCardResizeEnd);\n}\n\nfunction getCanvasCols() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return 12;\n    const style = getComputedStyle(canvas);\n    return style.gridTemplateColumns.split(\' \').length || 12;\n}\n\nfunction updateCanvasColumns() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    const w = window.innerWidth;\n    let cols = 4;\n    if (w >= 1280) cols = 12;\n    else if (w >= 1024) cols = 8;\n    else if (w >= 640) cols = 6;\n    canvas.style.display = \'grid\';\n    canvas.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;\n    canvas.style.gridAutoRows = \'100px\';\n    canvas.style.gap = \'8px\';\n    canvas.style.position = \'relative\';\n}\n\nfunction onCardResizeMove(e) {\n    if (!_cardResizing) return;\n    const el = _cardResizing.el;\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    const dx = e.clientX - _cardResizeStartX;\n    const dy = e.clientY - _cardResizeStartY;\n    const cols = getCanvasCols();\n    const gap = 8;\n    const padL = parseInt(getComputedStyle(canvas).paddingLeft) || 16;\n    const padR = parseInt(getComputedStyle(canvas).paddingRight) || 16;\n    const contentW = canvas.offsetWidth - padL - padR;\n    const colWidth = (contentW - (cols - 1) * gap) / cols;\n    const rowHeight = 100;\n    const rowStep = rowHeight + gap;\n\n    const newW = _cardResizeStartW + dx;\n    const newH = _cardResizeStartH + dy;\n    const newColSpan = Math.max(2, Math.min(cols, Math.round(newW / (colWidth + gap))));\n    const newRowSpan = Math.max(_cardResizeMinRowSpan, Math.min(8, Math.round(newH / rowStep)));\n\n    el.style.gridColumn = `${_cardResizing.col || \'auto\'} / span ${newColSpan}`;\n    el.style.gridRow = `${_cardResizing.row || \'auto\'} / span ${newRowSpan}`;\n    el._resizeColSpan = newColSpan;\n    el._resizeRowSpan = newRowSpan;\n}\n\nfunction onCardResizeEnd(e) {\n    if (!_cardResizing) return;\n    const el = _cardResizing.el;\n    const cardId = _cardResizing.cardId;\n\n    let colSpan = el._resizeColSpan || 3;\n    let rowSpan = el._resizeRowSpan || 1;\n\n    document.body.style.cursor = \'\';\n    document.body.style.userSelect = \'\';\n\n    document.removeEventListener(\'mousemove\', onCardResizeMove);\n    document.removeEventListener(\'mouseup\', onCardResizeEnd);\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (card) {\n        if (rowSpan < _cardResizeMinRowSpan) rowSpan = _cardResizeMinRowSpan;\n        const cols = getCanvasCols();\n        if (card.col + colSpan - 1 > cols) colSpan = cols - card.col + 1;\n\n        card.colSpan = colSpan;\n        card.rowSpan = rowSpan;\n        resolveOverlaps(saved, cardId);\n\n        for (const c of saved) {\n            if (c.id === cardId) continue;\n            const el2 = document.querySelector(`[data-card-id="${c.id}"]`);\n            if (el2) {\n                el2.style.gridColumn = `${c.col} / span ${c.colSpan || 3}`;\n                el2.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;\n            }\n        }\n        el.style.gridColumn = `${card.col} / span ${colSpan}`;\n        el.style.gridRow = `${card.row} / span ${rowSpan}`;\n        setPickerCards(saved);\n    }\n\n    _cardResizing = null;\n    _cardDragOccurred = true;\n    setTimeout(() => { _cardDragOccurred = false; }, 200);\n    updateCanvasMinHeight();\n}\n\nfunction getGridCell(canvas, x, y) {\n    const rect = canvas.getBoundingClientRect();\n    const cs = getComputedStyle(canvas);\n    const padL = parseFloat(cs.paddingLeft) || 16;\n    const padT = parseFloat(cs.paddingTop) || 16;\n    const padR = parseFloat(cs.paddingRight) || 16;\n    const cols = getCanvasCols();\n    const gap = 8;\n    const contentW = rect.width - padL - padR;\n    const colW = (contentW - (cols - 1) * gap) / cols;\n    const rowStep = 100 + gap;\n    const offset = x - rect.left - padL;\n    const col = Math.max(1, Math.min(cols, Math.floor(offset / (colW + gap)) + 1));\n    const row = Math.max(1, Math.floor((y - rect.top - padT) / rowStep) + 1);\n    return { col, row };\n}\n\nfunction findNextPosition(savedCards, colSpan) {\n    const cols = getCanvasCols();\n    const occupied = new Set();\n    for (const c of savedCards) {\n        const cs = c.col || 1;\n        const rs = c.row || 1;\n        const sp = c.colSpan || 3;\n        const sr = c.rowSpan || 1;\n        for (let r = rs; r < rs + sr; r++) {\n            for (let c2 = cs; c2 < cs + sp; c2++) {\n                occupied.add(`${c2},${r}`);\n            }\n        }\n    }\n    for (let row = 1; row <= 20; row++) {\n        for (let col = 1; col <= cols - colSpan + 1; col++) {\n            let fits = true;\n            for (let c2 = col; c2 < col + colSpan && fits; c2++) {\n                if (occupied.has(`${c2},${row}`)) fits = false;\n            }\n            if (fits) return { col, row };\n        }\n    }\n    return { col: 1, row: 1 };\n}\n\nlet _cardMouseDown = null;\nlet _cardDragClone = null;\nlet _dragGridCache = null;\n\nfunction _computeGridCache() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return null;\n    const style = getComputedStyle(canvas);\n    const padL = parseFloat(style.paddingLeft) || 16;\n    const padT = parseFloat(style.paddingTop) || 16;\n    const padR = parseFloat(style.paddingRight) || 16;\n    const contentW = canvas.offsetWidth - padL - padR;\n    const cols = parseInt(style.gridTemplateColumns?.split(\' \')?.length || 12);\n    const gap = parseFloat(style.gap) || 8;\n    const colW = (contentW - (cols - 1) * gap) / cols;\n    const rowH = 100;\n    return { cols, padL, padT, padR, gap, colW, rowH };\n}\n\nfunction onCardMouseDown(e) {\n    if (e.target.closest(\'button\') || e.target.closest(\'input\') || e.target.closest(\'.card-resize-handle\')) return;\n    if (e.button !== 0) return;\n    const cardEl = e.target.closest(\'[data-card-id]\');\n    if (!cardEl || cardEl.closest(\'[data-group-id]\')) return;\n    e.preventDefault();\n\n    const cardId = cardEl.dataset.cardId;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    if (card.lockSize) return;\n\n    const rect = cardEl.getBoundingClientRect();\n    const offsetX = e.clientX - rect.left;\n    const offsetY = e.clientY - rect.top;\n\n    const gridColMatch = cardEl.style.gridColumn?.match(/(\\d+)\\s*\\/\\s*span\\s+(\\d+)/);\n    const gridRowMatch = cardEl.style.gridRow?.match(/(\\d+)\\s*\\/\\s*span\\s+(\\d+)/);\n    const domColSpan = gridColMatch ? parseInt(gridColMatch[2]) : (card.colSpan || 3);\n    const domRowSpan = gridRowMatch ? parseInt(gridRowMatch[2]) : (card.rowSpan || 1);\n    const domCol = gridColMatch ? parseInt(gridColMatch[1]) : (card.col || 1);\n    const domRow = gridRowMatch ? parseInt(gridRowMatch[1]) : (card.row || 1);\n\n    _cardMouseDown = {\n        cardId, cardEl, card,\n        startX: e.clientX, startY: e.clientY,\n        offsetX, offsetY, dragging: false,\n        colSpan: domColSpan,\n        rowSpan: domRowSpan,\n        cardCol: domCol,\n        cardRow: domRow\n    };\n    _dragGridCache = _computeGridCache();\n\n    console.log(`[DOWN] card=${cardId} pos(col=${card.col},row=${card.row}) span(col=${card.colSpan||3},row=${card.rowSpan||1}) offset(X=${Math.round(offsetX)},Y=${Math.round(offsetY)}) cardRect(left=${Math.round(rect.left)},top=${Math.round(rect.top)},w=${Math.round(rect.width)},h=${Math.round(rect.height)})`);\n\n    document.addEventListener(\'mousemove\', onCardMouseMove);\n    document.addEventListener(\'mouseup\', onCardMouseUp);\n}\n\nfunction onCardMouseMove(e) {\n    if (!_cardMouseDown) return;\n    const dx = Math.abs(e.clientX - _cardMouseDown.startX);\n    const dy = Math.abs(e.clientY - _cardMouseDown.startY);\n    if (!_cardMouseDown.dragging && (dx < 4 && dy < 4)) return;\n\n    if (!_cardMouseDown.dragging) {\n        _cardMouseDown.dragging = true;\n        _cardMouseDown.cardEl.classList.add(\'opacity-40\');\n        _cardDragOccurred = true;\n\n        const canvas = document.getElementById(\'dashboard-canvas\');\n        const cs = getComputedStyle(canvas);\n        const padL = parseFloat(cs.paddingLeft) || 16;\n        const padT = parseFloat(cs.paddingTop) || 16;\n        const padR = parseFloat(cs.paddingRight) || 16;\n        const contentW = canvas.offsetWidth - padL - padR;\n        const cols = getCanvasCols();\n        const gap = 8;\n        const colW = (contentW - (cols - 1) * gap) / cols;\n        const cardW = _cardMouseDown.cardEl.offsetWidth;\n        const rowH = 100;\n        const rowStep = rowH + gap;\n        _cardMouseDown.gridSnapshot = {\n            padL, padT, cardW, cardElH: _cardMouseDown.cardEl.offsetHeight, cols, gap, colW, rowH, rowStep,\n            canvasLeft: canvas.getBoundingClientRect().left,\n            canvasTop: canvas.getBoundingClientRect().top\n        };\n\n        _cardDragClone = _cardMouseDown.cardEl.cloneNode(true);\n        _cardDragClone.classList.remove(\'opacity-40\');\n        _cardDragClone.style.cssText = `\n            position:fixed;z-index:10000;pointer-events:none;\n            width:${_cardMouseDown.cardEl.offsetWidth}px;\n            height:${_cardMouseDown.cardEl.offsetHeight}px;\n            opacity:0.85;\n            box-shadow:0 8px 32px rgba(0,0,0,0.4);\n            transition:none;\n            overflow:hidden;\n        `;\n        document.body.appendChild(_cardDragClone);\n    }\n\n    const cloneW = _cardMouseDown.cardEl.offsetWidth;\n    const cloneH = _cardMouseDown.cardEl.offsetHeight;\n    _cardDragClone.style.left = (e.clientX - _cardMouseDown.offsetX) + \'px\';\n    _cardDragClone.style.top = (e.clientY - _cardMouseDown.offsetY) + \'px\';\n\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    const card = _cardMouseDown.card;\n    const colSpan = _cardMouseDown.colSpan;\n    const rowSpan = _cardMouseDown.rowSpan;\n    const cols = getCanvasCols();\n    const snap = _cardMouseDown.gridSnapshot;\n\n    const cardCol = _cardMouseDown.cardCol;\n    const cardRow = _cardMouseDown.cardRow;\n\n    const cardLeft = snap.canvasLeft + snap.padL + (cardCol - 1) * (snap.colW + snap.gap);\n    const cardTop = snap.canvasTop + snap.padT + (cardRow - 1) * snap.rowStep;\n    const cardWidth = snap.cardW || (colSpan * snap.colW + (colSpan - 1) * snap.gap);\n    const cardHeight = snap.cardElH || (rowSpan * snap.rowStep - snap.gap);\n    const cardCenterX = cardLeft + cardWidth / 2;\n    const cardCenterY = cardTop + cardHeight / 2;\n    const halfW = cardWidth / 2;\n    const halfH = cardHeight / 2;\n\n    const relX = e.clientX - cardCenterX;\n    const relY = e.clientY - cardCenterY;\n\n    let newCol, newRow;\n    if (Math.abs(relX) <= halfW) {\n        newCol = cardCol;\n    } else {\n        const offset = e.clientX - snap.canvasLeft - snap.padL;\n        newCol = Math.max(1, Math.min(cols - colSpan + 1, Math.floor(offset / (snap.colW + snap.gap)) + 1));\n    }\n    if (Math.abs(relY) <= halfH) {\n        newRow = cardRow;\n    } else {\n        const offset = e.clientY - snap.canvasTop - snap.padT;\n        newRow = Math.max(1, Math.floor(offset / snap.rowStep) + 1);\n    }\n    const occupied = isCellOccupied(newCol, newRow, colSpan, rowSpan, card.id);\n\n    if (!_cardDropPreview) {\n        _cardDropPreview = document.createElement(\'div\');\n        _cardDropPreview.style.cssText = \'position:fixed;pointer-events:none;z-index:9999;border:2px dashed #06b6d4;border-radius:12px;transition:none;background:rgba(6,182,212,0.08);\';\n        document.body.appendChild(_cardDropPreview);\n    }\n\n    _cardDropPreview.style.left = (snap.canvasLeft + snap.padL + (newCol - 1) * (snap.colW + snap.gap)) + \'px\';\n    _cardDropPreview.style.top = (snap.canvasTop + snap.padT + (newRow - 1) * snap.rowStep) + \'px\';\n    _cardDropPreview.style.width = (colSpan * snap.colW + (colSpan - 1) * snap.gap) + \'px\';\n    _cardDropPreview.style.height = (rowSpan * snap.rowStep - snap.gap) + \'px\';\n    _cardDropPreview.style.borderColor = occupied ? \'#ef4444\' : \'#06b6d4\';\n    _cardDropPreview.style.background = occupied ? \'rgba(239,68,68,0.08)\' : \'rgba(6,182,212,0.08)\';\n    _cardDropPreview.style.display = \'block\';\n\n    _dropTarget = { col: newCol, row: newRow, occupied };\n\n    console.log(`[MOVE] card=${card.id} stored(col=${cardCol},row=${cardRow}) span(${colSpan}x${rowSpan}) relX=${Math.round(relX)},relY=${Math.round(relY)} halfW=${Math.round(halfW)},halfH=${Math.round(halfH)} → new(col=${newCol},row=${newRow}) occ=${occupied}`);\n\n    const groupEl = document.elementFromPoint(e.clientX, e.clientY)?.closest(\'[data-group-id]\');\n    document.querySelectorAll(\'[data-group-id].drag-hover\').forEach(el => el.classList.remove(\'drag-hover\'));\n    if (groupEl && !groupEl.contains(_cardMouseDown.cardEl)) {\n        groupEl.classList.add(\'drag-hover\');\n        groupEl.style.borderColor = \'#a855f7\';\n        groupEl.style.background = \'rgba(168,85,247,0.1)\';\n    }\n}\n\nfunction onCardMouseUp(e) {\n    document.removeEventListener(\'mousemove\', onCardMouseMove);\n    document.removeEventListener(\'mouseup\', onCardMouseUp);\n\n    if (_cardDragClone) {\n        _cardDragClone.remove();\n        _cardDragClone = null;\n    }\n    if (_cardDropPreview) {\n        _cardDropPreview.style.display = \'none\';\n    }\n\n    document.querySelectorAll(\'[data-group-id].drag-hover\').forEach(el => {\n        el.classList.remove(\'drag-hover\');\n        el.style.borderColor = \'\';\n        el.style.background = \'\';\n    });\n\n    if (!_cardMouseDown) return;\n\n    const { cardEl, card, dragging } = _cardMouseDown;\n    const totalDx = Math.abs(e.clientX - _cardMouseDown.startX);\n    const totalDy = Math.abs(e.clientY - _cardMouseDown.startY);\n    if (totalDx > 2 || totalDy > 2) _cardDragOccurred = true;\n    cardEl.classList.remove(\'opacity-40\');\n\n    if (dragging && _dropTarget) {\n        const groupEl = document.elementFromPoint(e.clientX, e.clientY)?.closest(\'[data-group-id]\');\n        if (groupEl && !groupEl.contains(cardEl)) {\n            const groupCards = groupEl.querySelector(\'.group-cards\');\n            if (groupCards) {\n                const saved = getPickerCards();\n                const cardData = saved.find(c => c.id === card.id);\n                if (cardData) {\n                    cardData.groupId = groupEl.dataset.groupId;\n                    setPickerCards(saved);\n                }\n                groupCards.appendChild(cardEl);\n                cardEl.classList.remove(\'cursor-grab\');\n                cardEl.classList.add(\'cursor-default\');\n            }\n        } else {\n            const saved = getPickerCards();\n            const cardData = saved.find(c => c.id === card.id);\n                if (cardData) {\n                    const oldCol = cardData.col, oldRow = cardData.row;\n                    let newCol = _dropTarget.col;\n                    let newRow = _dropTarget.row;\n                    const colSp = cardData.colSpan || 3;\n                    const rowSp = cardData.rowSpan || 1;\n                    const cols = getCanvasCols();\n                    if (newCol + colSp - 1 > cols) newCol = cols - colSp + 1;\n                    cardData._isDrag = true;\n                    cardData.col = newCol;\n                    cardData.row = newRow;\n                    resolveOverlaps(saved, card.id);\n                console.log(`[DROP] card=${card.id} from(col=${oldCol},row=${oldRow}) target(col=${newCol},row=${newRow})`);\n                for (const c of saved) {\n                    const el2 = document.querySelector(`[data-card-id="${c.id}"]`);\n                    if (el2) {\n                        el2.style.gridColumn = `${c.col} / span ${c.colSpan || 3}`;\n                        el2.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;\n                    }\n                }\n                setPickerCards(saved);\n                updateCanvasMinHeight();\n            }\n        }\n    }\n\n    _cardMouseDown = null;\n    _dropTarget = null;\n    _dragGridCache = null;\n    setTimeout(() => { _cardDragOccurred = false; }, 200);\n}\n\nfunction isCellOccupied(col, row, colSpan, rowSpan, excludeCardId) {\n    const saved = getPickerCards();\n    for (const c of saved) {\n        if (c.id === excludeCardId || !c.col || !c.row) continue;\n        const cs = c.col, rs = c.row;\n        const ce = cs + (c.colSpan || 3) - 1;\n        const re = rs + (c.rowSpan || 1) - 1;\n        const ne = col + colSpan - 1;\n        const nr = row + rowSpan - 1;\n        if (col <= ce && ne >= cs && row <= re && nr >= rs) return true;\n    }\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (canvas) {\n        const g = _dragGridCache || _computeGridCache();\n        if (g) {\n            const ne = col + colSpan - 1;\n            const nr = row + rowSpan - 1;\n            for (const gEl of canvas.querySelectorAll(\'[data-group-id]\')) {\n                const rect = gEl.getBoundingClientRect();\n                const cRect = canvas.getBoundingClientRect();\n                const gColStart = Math.max(1, Math.round((rect.left - cRect.left - g.padL) / (g.colW + g.gap)) + 1);\n                const gColEnd = Math.max(gColStart, Math.round((rect.right - cRect.left - g.padL) / (g.colW + g.gap)));\n                const gRowStart = Math.max(1, Math.round((rect.top - cRect.top - g.padT) / (g.rowH + g.gap)) + 1);\n                const gRowEnd = Math.max(gRowStart, Math.round((rect.bottom - cRect.top - g.padT) / (g.rowH + g.gap)));\n                if (col <= gColEnd && ne >= gColStart && row <= gRowEnd && nr >= gRowStart) return true;\n            }\n        }\n    }\n    return false;\n}\n\nfunction resolveOverlaps(saved, cardId) {\n    const cols = getCanvasCols();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n    delete card._isDrag;\n\n    function overlaps(a, b) {\n        if (!a.col || !a.row || !b.col || !b.row) return false;\n        const aCe = a.col + (a.colSpan || 3) - 1, aRe = a.row + (a.rowSpan || 1) - 1;\n        const bCe = b.col + (b.colSpan || 3) - 1, bRe = b.row + (b.rowSpan || 1) - 1;\n        return a.col <= bCe && aCe >= b.col && a.row <= bRe && aRe >= b.row;\n    }\n\n    function pushRight(anchor, target) {\n        const anchorCe = anchor.col + (anchor.colSpan || 3) - 1;\n        target.col = anchorCe + 1;\n    }\n\n    const affected = new Set([cardId]);\n    let iter = 0;\n    let changed = true;\n    while (changed && iter < 50) {\n        changed = false;\n        iter++;\n        for (const c of saved) {\n            if (!c.col || !c.row || affected.has(c.id)) continue;\n            for (const aId of affected) {\n                const a = saved.find(x => x.id === aId);\n                if (a && overlaps(a, c)) {\n                    pushRight(a, c);\n                    affected.add(c.id);\n                    changed = true;\n                    break;\n                }\n            }\n        }\n    }\n}\n\nfunction findFreePosition(savedCards, colSpan, rowSpan, excludeCardId) {\n    const cols = getCanvasCols();\n    if (colSpan > cols) colSpan = cols;\n    const occupied = new Set();\n    for (const c of savedCards) {\n        if (c.id === excludeCardId || !c.col || !c.row) continue;\n        const cs = c.col, rs = c.row;\n        const sp = c.colSpan || 3, sr = c.rowSpan || 1;\n        for (let r = rs; r < rs + sr; r++) {\n            for (let c2 = cs; c2 < cs + sp; c2++) {\n                occupied.add(`${c2},${r}`);\n            }\n        }\n    }\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (canvas) {\n        const cs2 = getComputedStyle(canvas);\n        const padL = parseFloat(cs2.paddingLeft) || 16;\n        const padT = parseFloat(cs2.paddingTop) || 16;\n        const padR = parseFloat(cs2.paddingRight) || 16;\n        const contentW = canvas.offsetWidth - padL - padR;\n        const gap = 8;\n        const colW = (contentW - (cols - 1) * gap) / cols;\n        const rowH = 100;\n        for (const gEl of canvas.querySelectorAll(\'[data-group-id]\')) {\n            const rect = gEl.getBoundingClientRect();\n            const cRect = canvas.getBoundingClientRect();\n            const gColStart = Math.max(1, Math.round((rect.left - cRect.left - padL) / (colW + gap)) + 1);\n            const gColEnd = Math.max(gColStart, Math.round((rect.right - cRect.left - padL) / (colW + gap)));\n            const gRowStart = Math.max(1, Math.round((rect.top - cRect.top - padT) / (rowH + gap)) + 1);\n            const gRowEnd = Math.max(gRowStart, Math.round((rect.bottom - cRect.top - padT) / (rowH + gap)));\n            for (let r = gRowStart; r <= gRowEnd; r++) {\n                for (let c2 = gColStart; c2 <= gColEnd; c2++) {\n                    occupied.add(`${c2},${r}`);\n                }\n            }\n        }\n    }\n    for (let row = 1; row <= 50; row++) {\n        for (let col = 1; col <= cols - colSpan + 1; col++) {\n            let fits = true;\n            for (let r = row; r < row + rowSpan && fits; r++) {\n                for (let c = col; c < col + colSpan && fits; c++) {\n                    if (occupied.has(`${c},${r}`)) fits = false;\n                }\n            }\n            if (fits) return { col, row };\n        }\n    }\n    return { col: 1, row: 1 };\n}\n\nlet _cardDropPreview = null;\n\nfunction getDragAfterElement(container, x, y) {\n    const cards = [...container.querySelectorAll(\'[data-card-id]:not(.opacity-40), [data-group-id]:not(.opacity-40)\')];\n    let closest = null;\n    let closestDist = Infinity;\n    for (const child of cards) {\n        const box = child.getBoundingClientRect();\n        const cx = box.left + box.width / 2;\n        const cy = box.top + box.height / 2;\n        const dist = Math.hypot(x - cx, y - cy);\n        if (dist < closestDist) {\n            closestDist = dist;\n            closest = child;\n        }\n    }\n    if (!closest) return null;\n    const box = closest.getBoundingClientRect();\n    const isAfter = x > box.left + box.width / 2 || y > box.top + box.height / 2;\n    return isAfter ? closest.nextElementSibling : closest;\n}\nfunction saveCardOrder() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    const ordered = [...canvas.querySelectorAll(\'[data-card-id]\')].map(el => el.dataset.cardId);\n    const saved = getPickerCards();\n    const orderedCards = ordered.map(id => saved.find(c => c.id === id)).filter(Boolean);\n    setPickerCards(orderedCards);\n}\n\nfunction removePickerCard(cardId) {\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (el) el.remove();\n    const saved = getPickerCards().filter(c => c.id !== cardId);\n    setPickerCards(saved);\n    if (!saved.length) document.getElementById(\'dashboard-empty\')?.classList.remove(\'hidden\');\n    updateCanvasMinHeight();\n}\n\nlet _editingCardId = null;\n\nfunction showCardEdit(cardId) {\n    _editingCardId = cardId;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    const modal = document.getElementById(\'card-edit-modal\');\n    const labelInput = document.getElementById(\'card-edit-label\');\n\n    labelInput.value = card.label || \'\';\n\n    modal.classList.remove(\'hidden\');\n    labelInput.focus();\n}\n\nfunction hideCardEdit() {\n    const modal = document.getElementById(\'card-edit-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n    _editingCardId = null;\n}\n\nfunction saveCardEdit() {\n    if (!_editingCardId) return;\n\n    const label = document.getElementById(\'card-edit-label\').value.trim();\n    if (!label) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === _editingCardId);\n    if (!card) return;\n\n    card.label = label;\n    setPickerCards(saved);\n\n    const cardEl = document.querySelector(`[data-card-id="${_editingCardId}"]`);\n    if (cardEl) {\n        const labelEl = cardEl.querySelector(\'.text-sm.text-gray-300\');\n        if (labelEl) labelEl.textContent = label;\n    }\n\n    hideCardEdit();\n}\n\nlet _configuringCardId = null;\n\nfunction showCardConfig(cardId) {\n    _configuringCardId = cardId;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card || card.type !== \'fan\') return;\n\n    const modal = document.getElementById(\'card-config-modal\');\n    const container = document.getElementById(\'card-config-options\');\n\n    const fanData = getFanData(card.source, card.sourceId);\n    if (!fanData) return;\n\n    const options = [\n        { key: \'rpm\', label: \'RPM\', checked: card.showRpm !== false },\n        { key: \'mode\', label: \'Mode\', checked: card.showMode === true },\n        { key: \'sensors\', label: \'Sensors\', checked: card.showSensors === true },\n        { key: \'target\', label: \'Target Temp\', checked: card.showTarget === true },\n    ];\n\n    container.innerHTML = options.map(opt => `\n        <label class="flex items-center gap-3 p-2 rounded hover:bg-cyber-accent cursor-pointer">\n            <input type="checkbox" data-option="${opt.key}" ${opt.checked ? \'checked\' : \'\'}\n                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan">\n            <span class="text-sm text-gray-300">${opt.label}</span>\n        </label>\n    `).join(\'\');\n\n    container.querySelectorAll(\'input[type="checkbox"]\').forEach(cb => {\n        cb.addEventListener(\'change\', () => toggleCardOption(cardId, cb.dataset.option, cb.checked));\n    });\n\n    modal.classList.remove(\'hidden\');\n}\n\nfunction hideCardConfig() {\n    const modal = document.getElementById(\'card-config-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n    _configuringCardId = null;\n}\n\nlet _smartModalCardId = null;\nlet _smartModalDiskId = null;\nlet _smartModalSource = \'local\';\nlet _smartAttributes = [];\nlet _smartAttrType = \'sata\';\nlet _smartCache = {};\n\nasync function fetchDiskSmart(diskId, forceRefresh = false, source = \'local\', nodeId = null) {\n    try {\n        let url;\n        if (source === \'local\') {\n            url = forceRefresh\n                ? `/api/disks/${diskId}/smart?refresh=1`\n                : `/api/disks/${diskId}/smart`;\n        } else {\n            url = forceRefresh\n                ? `/api/nodes/${source}/disks/${diskId}/smart?refresh=1`\n                : `/api/nodes/${source}/disks/${diskId}/smart`;\n        }\n        const resp = await fetch(url);\n        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);\n        return await resp.json();\n    } catch (e) {\n        console.error(\'SMART fetch error:\', e);\n        return null;\n    }\n}\n\nfunction showSmartModal(cardId) {\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    _smartModalCardId = cardId;\n    _smartModalDiskId = card.sourceId;\n    _smartModalSource = card.source || \'local\';\n\n    let disk;\n    if (_smartModalSource === \'local\') {\n        disk = currentState?.hdd_sensors?.[card.sourceId];\n    } else {\n        const node = nodesData.find(n => n.node_id === _smartModalSource);\n        disk = node?.telemetry?.hdd_sensors?.[card.sourceId];\n    }\n\n    const title = document.getElementById(\'smart-modal-title\');\n    if (title && disk) {\n        title.textContent = `SMART — ${disk.label || disk.dev_name || card.sourceId}`;\n    } else if (title) {\n        title.textContent = `SMART — ${card.sourceId}`;\n    }\n    document.getElementById(\'smart-modal\')?.classList.remove(\'hidden\');\n    refreshSmartData();\n}\n\nfunction hideSmartModal() {\n    document.getElementById(\'smart-modal\')?.classList.add(\'hidden\');\n    _smartModalCardId = null;\n    _smartModalDiskId = null;\n    _smartModalSource = \'local\';\n}\n\nasync function refreshSmartData() {\n    if (!_smartModalDiskId) return;\n    const container = document.getElementById(\'smart-attributes-container\');\n    if (!container) return;\n\n    container.innerHTML = \'<div class="text-center text-gray-400 py-4">Загрузка...</div>\';\n\n    const data = await fetchDiskSmart(_smartModalDiskId, true, _smartModalSource);\n    if (!data || data.error) {\n        container.innerHTML = `<div class="text-center text-red-400 py-4">${data?.error || \'Ошибка загрузки SMART данных\'}</div>`;\n        return;\n    }\n\n    _smartCache[_smartModalDiskId] = data;\n\n    const infoEl = document.getElementById(\'smart-device-info\');\n    if (infoEl && data.device_info) {\n        const info = data.device_info;\n        infoEl.textContent = [info.model, info.serial, info.firmware, info.capacity].filter(Boolean).join(\' | \');\n    }\n\n    _smartAttrType = data.attr_type || \'sata\';\n    _smartAttributes = data.attributes || [];\n\n    renderSmartAttributes();\n}\n\nfunction renderSmartAttributes() {\n    const container = document.getElementById(\'smart-attributes-container\');\n    if (!container) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === _smartModalCardId);\n    const selectedIds = card?.smartAttributes || [];\n\n    if (_smartAttrType === \'nvme\') {\n        renderNvmeAttributes(container, selectedIds);\n    } else {\n        renderSataAttributes(container, selectedIds);\n    }\n}\n\nfunction renderSataAttributes(container, selectedIds) {\n    if (!_smartAttributes.length) {\n        container.innerHTML = \'<div class="text-center text-gray-400 py-4">Нет SMART атрибутов</div>\';\n        return;\n    }\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === _smartModalCardId);\n    const smartUnits = card?.smartUnits || {};\n\n    container.innerHTML = _smartAttributes.map(attr => {\n        const statusColor = attr.status === \'critical\' ? \'text-red-400\' :\n                           attr.status === \'warning\' ? \'text-yellow-400\' : \'text-neon-green\';\n        const statusBg = attr.status === \'critical\' ? \'bg-red-500/10\' :\n                        attr.status === \'warning\' ? \'bg-yellow-500/10\' : \'bg-green-500/10\';\n        const critBadge = attr.criticality === \'critical\' ? \'<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">КРИТИЧНЫЙ</span>\' :\n                         attr.criticality === \'important\' ? \'<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">ВАЖНЫЙ</span>\' : \'\';\n        const checked = selectedIds.includes(String(attr.id)) ? \'checked\' : \'\';\n\n        let unitHtml = \'\';\n        if (attr.unit === \'bytes\') {\n            const currentUnit = smartUnits[attr.id] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${attr.id}" onchange="onSmartUnitChange(${attr.id}, this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Raw</option>\n                    <option value="bytes" ${currentUnit === \'bytes\' ? \'selected\' : \'\'}>Байты</option>\n                    <option value="kb" ${currentUnit === \'kb\' ? \'selected\' : \'\'}>КБ</option>\n                    <option value="mb" ${currentUnit === \'mb\' ? \'selected\' : \'\'}>МБ</option>\n                    <option value="gb" ${currentUnit === \'gb\' ? \'selected\' : \'\'}>ГБ</option>\n                    <option value="tb" ${currentUnit === \'tb\' ? \'selected\' : \'\'}>ТБ</option>\n                </select>`;\n        } else if (attr.unit === \'hours\') {\n            const currentUnit = smartUnits[attr.id] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${attr.id}" onchange="onSmartUnitChange(${attr.id}, this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Часы</option>\n                    <option value="days" ${currentUnit === \'days\' ? \'selected\' : \'\'}>Дни</option>\n                    <option value="months" ${currentUnit === \'months\' ? \'selected\' : \'\'}>Месяцы</option>\n                </select>`;\n        }\n\n        let displayValue = attr.raw;\n        if (attr.unit === \'bytes\' && attr.unit_divisor) {\n            const unit = smartUnits[attr.id] || \'raw\';\n            if (unit !== \'raw\') {\n                displayValue = formatBytes(parseInt(attr.raw_num || attr.raw) * attr.unit_divisor, unit);\n            }\n        } else if (attr.unit === \'hours\') {\n            const unit = smartUnits[attr.id] || \'raw\';\n            if (unit === \'days\') {\n                displayValue = (parseInt(attr.raw || \'0\') / 24).toFixed(1) + \' дн\';\n            } else if (unit === \'months\') {\n                displayValue = (parseInt(attr.raw || \'0\') / 720).toFixed(1) + \' мес\';\n            }\n        }\n\n        return `\n        <div class="flex items-center gap-3 p-2 rounded ${statusBg} hover:bg-white/5 transition-colors group"\n             title="${escapeHtml(attr.tooltip)}">\n            <input type="checkbox" data-smart-id="${attr.id}" ${checked}\n                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan shrink-0">\n            <div class="flex-1 min-w-0">\n                <div class="flex items-center">\n                    <span class="text-xs text-gray-500 w-8">${attr.id}</span>\n                    <span class="text-sm text-gray-200 truncate">${escapeHtml(attr.description)}</span>\n                    ${critBadge}\n                    ${unitHtml}\n                </div>\n                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>\n            </div>\n            <div class="text-right shrink-0">\n                <div class="text-sm font-mono ${statusColor}">${attr.value}</div>\n                <div class="text-[10px] text-gray-500">worst:${attr.worst} thr:${attr.threshold}</div>\n            </div>\n            <div class="text-right shrink-0 w-20">\n                <div class="text-xs text-gray-400 font-mono">${displayValue}</div>\n            </div>\n        </div>`;\n    }).join(\'\');\n}\n\nfunction formatBytes(bytes, unit) {\n    if (isNaN(bytes) || bytes === 0) return \'0\';\n    const units = { \'kb\': 1024, \'mb\': 1024*1024, \'gb\': 1024*1024*1024, \'tb\': 1024*1024*1024*1024 };\n    const divisor = units[unit] || 1;\n    const result = bytes / divisor;\n    if (result >= 1000) return result.toFixed(0);\n    if (result >= 100) return result.toFixed(1);\n    return result.toFixed(2);\n}\n\nfunction onSmartUnitChange(attrId, unit) {\n    if (!_smartModalCardId) return;\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === _smartModalCardId);\n    if (!card) return;\n\n    if (!card.smartUnits) card.smartUnits = {};\n    card.smartUnits[attrId] = unit;\n    setPickerCards(saved);\n    renderSmartAttributes();\n}\n\nfunction renderNvmeAttributes(container, selectedIds) {\n    const attrs = _smartAttributes;\n    if (!Object.keys(attrs).length) {\n        container.innerHTML = \'<div class="text-center text-gray-400 py-4">Нет NVMe атрибутов</div>\';\n        return;\n    }\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === _smartModalCardId);\n    const smartUnits = card?.smartUnits || {};\n\n    container.innerHTML = Object.entries(attrs).map(([key, attr]) => {\n        const statusColor = attr.criticality === \'critical\' ? \'text-red-400\' :\n                           attr.criticality === \'important\' ? \'text-yellow-400\' : \'text-neon-green\';\n        const critBadge = attr.criticality === \'critical\' ? \'<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">КРИТИЧНЫЙ</span>\' :\n                         attr.criticality === \'important\' ? \'<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">ВАЖНЫЙ</span>\' : \'\';\n        const checked = selectedIds.includes(key) ? \'checked\' : \'\';\n\n        let unitHtml = \'\';\n        let displayValue = attr.value;\n\n        if (attr.unit === \'nvme_blocks\') {\n            const currentUnit = smartUnits[key] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${key}" onchange="onSmartUnitChange(\'${key}\', this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Raw</option>\n                    <option value="bytes" ${currentUnit === \'bytes\' ? \'selected\' : \'\'}>Байты</option>\n                    <option value="kb" ${currentUnit === \'kb\' ? \'selected\' : \'\'}>КБ</option>\n                    <option value="mb" ${currentUnit === \'mb\' ? \'selected\' : \'\'}>МБ</option>\n                    <option value="gb" ${currentUnit === \'gb\' ? \'selected\' : \'\'}>ГБ</option>\n                    <option value="tb" ${currentUnit === \'tb\' ? \'selected\' : \'\'}>ТБ</option>\n                </select>`;\n            if (currentUnit !== \'raw\' && attr.unit_divisor) {\n                displayValue = formatBytes(attr.value * attr.unit_divisor, currentUnit);\n            }\n        } else if (attr.unit === \'hours\') {\n            const currentUnit = smartUnits[key] || \'raw\';\n            unitHtml = `\n                <select data-smart-unit="${key}" onchange="onSmartUnitChange(\'${key}\', this.value)"\n                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">\n                    <option value="raw" ${currentUnit === \'raw\' ? \'selected\' : \'\'}>Часы</option>\n                    <option value="days" ${currentUnit === \'days\' ? \'selected\' : \'\'}>Дни</option>\n                    <option value="months" ${currentUnit === \'months\' ? \'selected\' : \'\'}>Месяцы</option>\n                </select>`;\n            if (currentUnit === \'days\') {\n                displayValue = (parseInt(attr.value || \'0\') / 24).toFixed(1);\n            } else if (currentUnit === \'months\') {\n                displayValue = (parseInt(attr.value || \'0\') / 720).toFixed(1);\n            }\n        }\n\n        let suffix = \'\';\n        if (key === \'temperature\') suffix = \'°C\';\n        else if (key === \'percentage_used\' || key === \'available_spare\' || key === \'available_spare_threshold\') suffix = \'%\';\n        else if (key === \'controller_busy_time\' || key === \'warning_temp_time\' || key === \'critical_comp_time\') suffix = \' мин\';\n        else if (attr.unit === \'hours\' && (smartUnits[key] || \'raw\') === \'days\') suffix = \' дн\';\n        else if (attr.unit === \'hours\' && (smartUnits[key] || \'raw\') === \'months\') suffix = \' мес\';\n\n        return `\n        <div class="flex items-center gap-3 p-2 rounded bg-green-500/5 hover:bg-white/5 transition-colors"\n             title="${escapeHtml(attr.tooltip)}">\n            <input type="checkbox" data-smart-key="${key}" ${checked}\n                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan shrink-0">\n            <div class="flex-1 min-w-0">\n                <div class="flex items-center">\n                    <span class="text-sm text-gray-200 truncate">${escapeHtml(attr.description)}</span>\n                    ${critBadge}\n                    ${unitHtml}\n                </div>\n                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>\n            </div>\n            <div class="text-right shrink-0">\n                <div class="text-sm font-mono ${statusColor}">${displayValue}${suffix}</div>\n            </div>\n        </div>`;\n    }).join(\'\');\n}\n\nfunction saveSmartSelection() {\n    if (!_smartModalCardId) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === _smartModalCardId);\n    if (!card) return;\n\n    const checkboxes = document.querySelectorAll(\'#smart-attributes-container input[type="checkbox"]\');\n    const selected = [];\n    checkboxes.forEach(cb => {\n        if (cb.checked) {\n            selected.push(cb.dataset.smartId || cb.dataset.smartKey);\n        }\n    });\n\n    const unitSelects = document.querySelectorAll(\'#smart-attributes-container select[data-smart-unit]\');\n    const units = {};\n    unitSelects.forEach(sel => {\n        const attrId = sel.dataset.smartUnit;\n        units[attrId] = sel.value;\n    });\n\n    card.smartAttributes = selected;\n    card.smartUnits = units;\n    setPickerCards(saved);\n    updateCardDetails(_smartModalCardId);\n    const cardEl = document.querySelector(`[data-card-id="${_smartModalCardId}"]`);\n    if (cardEl) snapCardToGrid(cardEl);\n    hideSmartModal();\n    saveDashboardToServer();\n}\n\nfunction toggleCardOption(cardId, option, enabled) {\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    if (option === \'rpm\') card.showRpm = enabled;\n    else if (option === \'mode\') card.showMode = enabled;\n    else if (option === \'sensors\') card.showSensors = enabled;\n    else if (option === \'target\') card.showTarget = enabled;\n\n    setPickerCards(saved);\n    updateCardDetails(cardId);\n    const el = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (el) snapCardToGrid(el);\n}\n\nfunction getFanData(source, sourceId) {\n    if (source === \'local\') return currentState?.fans?.[sourceId] || null;\n    const node = nodesData.find(n => n.node_id === source);\n    return node?.telemetry?.fans?.[sourceId] || null;\n}\n\nfunction getSensorLabel(sensorId) {\n    if (sensorId.startsWith(\'hdd:\')) {\n        const id = sensorId.slice(4);\n        return currentState?.hdd_sensors?.[id]?.label || id;\n    } else if (sensorId.startsWith(\'temp:\')) {\n        const id = sensorId.slice(5);\n        return currentState?.temp_sensors?.[id]?.label || id;\n    }\n    return sensorId;\n}\n\nfunction updateCardDetails(cardId) {\n    const cardEl = document.querySelector(`[data-card-id="${cardId}"]`);\n    if (!cardEl) return;\n\n    const saved = getPickerCards();\n    const card = saved.find(c => c.id === cardId);\n    if (!card) return;\n\n    const detailsEl = cardEl.querySelector(\'.card-details\');\n    if (!detailsEl) return;\n\n    if (card.type === \'disk\') {\n        updateDiskCardDetails(card, detailsEl);\n        return;\n    }\n    if (card.type !== \'fan\') {\n        detailsEl.innerHTML = \'\';\n        return;\n    }\n\n    const fanData = getFanData(card.source, card.sourceId);\n    let html = \'\';\n\n    if (fanData) {\n        if (card.showMode) {\n            const mode = fanData.mode || \'manual\';\n            const modeClass = mode === \'auto\' ? \'text-neon-green\' : \'text-neon-cyan\';\n            const modeLabel = mode === \'auto\' ? \'AUTO\' : \'MANUAL\';\n            html += `<div class="text-xs ${modeClass} mt-1">${modeLabel}</div>`;\n        }\n        if (card.showTarget && fanData.mode === \'auto\') {\n            html += `<div class="text-xs text-gray-500 mt-1">Target: ${fanData.target_temp || \'--\'}°C</div>`;\n        }\n        if (card.showSensors && fanData.sensors && fanData.sensors.length > 0) {\n            const sensorLabels = fanData.sensors.map(s => getSensorLabel(s)).join(\', \');\n            html += `<div class="text-xs text-gray-500 mt-1 truncate" title="${escapeHtml(sensorLabels)}">Sensors: ${escapeHtml(sensorLabels)}</div>`;\n        }\n    }\n\n    detailsEl.innerHTML = html;\n}\n\nfunction updateDiskCardDetails(card, detailsEl) {\n    if (!card.smartAttributes?.length) {\n        detailsEl.innerHTML = \'\';\n        return;\n    }\n\n    const diskData = currentState?.hdd_sensors?.[card.sourceId];\n    if (!diskData) {\n        detailsEl.innerHTML = \'\';\n        return;\n    }\n\n    let html = \'\';\n    const smartUnits = card.smartUnits || {};\n\n    for (const attrKey of card.smartAttributes) {\n        const attrId = parseInt(attrKey);\n        if (!isNaN(attrId)) {\n            const cachedSmart = _smartCache?.[card.sourceId];\n            if (cachedSmart?.attributes) {\n                const attr = cachedSmart.attributes.find(a => a.id === attrId);\n                if (attr) {\n                    const color = attr.status === \'critical\' ? \'text-red-400\' :\n                                 attr.status === \'warning\' ? \'text-yellow-400\' : \'text-neon-green\';\n                    let displayValue = attr.raw;\n                    if (attr.unit === \'bytes\' && attr.unit_divisor) {\n                        const unit = smartUnits[attr.id] || \'raw\';\n                        if (unit !== \'raw\') {\n                            displayValue = formatBytes(parseInt(attr.raw_num || attr.raw) * attr.unit_divisor, unit) + \' \' + getUnitLabel(unit);\n                        }\n                    } else if (attr.unit === \'hours\') {\n                        const unit = smartUnits[attr.id] || \'raw\';\n                        if (unit === \'days\') {\n                            displayValue = (parseInt(attr.raw || \'0\') / 24).toFixed(1) + \' дн\';\n                        } else if (unit === \'months\') {\n                            displayValue = (parseInt(attr.raw || \'0\') / 720).toFixed(1) + \' мес\';\n                        }\n                    } else if (attr.unit === \'nvme_blocks\') {\n                        const unit = smartUnits[attr.id] || \'raw\';\n                        if (unit !== \'raw\') {\n                            displayValue = formatBytes(attr.value * (attr.unit_divisor || 1), unit) + \' \' + getUnitLabel(unit);\n                        }\n                    }\n                    html += `<div class="text-xs mt-1" title="${escapeHtml(attr.tooltip)}">\n                        <span class="text-gray-500">${escapeHtml(attr.description)}:</span>\n                        <span class="${color} font-mono">${displayValue}</span>\n                    </div>`;\n                }\n            }\n        } else {\n            const cachedSmart = _smartCache?.[card.sourceId];\n            if (cachedSmart?.attributes?.[attrKey]) {\n                const attr = cachedSmart.attributes[attrKey];\n                const color = attr.criticality === \'critical\' ? \'text-red-400\' :\n                             attr.criticality === \'important\' ? \'text-yellow-400\' : \'text-neon-green\';\n                let displayValue = attr.value;\n                let suffix = attrKey === \'temperature\' ? \'°C\' :\n                            attrKey.includes(\'percentage\') || attrKey.includes(\'spare\') ? \'%\' : \'\';\n                if (attr.unit === \'nvme_blocks\' && attr.unit_divisor) {\n                    const unit = smartUnits[attrKey] || \'raw\';\n                    if (unit !== \'raw\') {\n                        displayValue = formatBytes(attr.value * attr.unit_divisor, unit);\n                        suffix = \' \' + getUnitLabel(unit);\n                    }\n                } else if (attr.unit === \'hours\') {\n                    const unit = smartUnits[attrKey] || \'raw\';\n                    if (unit === \'days\') {\n                        displayValue = (parseInt(attr.value || \'0\') / 24).toFixed(1);\n                        suffix = \' дн\';\n                    } else if (unit === \'months\') {\n                        displayValue = (parseInt(attr.value || \'0\') / 720).toFixed(1);\n                        suffix = \' мес\';\n                    }\n                }\n                html += `<div class="text-xs mt-1" title="${escapeHtml(attr.tooltip)}">\n                    <span class="text-gray-500">${escapeHtml(attr.description)}:</span>\n                    <span class="${color} font-mono">${displayValue}${suffix}</span>\n                </div>`;\n            }\n        }\n    }\n\n    detailsEl.innerHTML = html;\n}\n\nfunction getUnitLabel(unit) {\n    const labels = { \'bytes\': \'Б\', \'kb\': \'КБ\', \'mb\': \'МБ\', \'gb\': \'ГБ\', \'tb\': \'ТБ\' };\n    return labels[unit] || \'\';\n}\n\nlet _pickerCards = null;\nlet _pickerGroups = null;\nlet _hiddenSensors = null;\nlet _dashboardLoaded = false;\nlet _dashboardSaveTimer = null;\n\nconst _sparklineHistory = {};\nconst SPARKLINE_MAX = 20;\n\nfunction pushSparkline(key, value) {\n    if (!_sparklineHistory[key]) _sparklineHistory[key] = [];\n    _sparklineHistory[key].push(value);\n    if (_sparklineHistory[key].length > SPARKLINE_MAX) _sparklineHistory[key].shift();\n}\n\nfunction getSparkline(key) {\n    return _sparklineHistory[key] || [];\n}\n\nfunction renderSparkline(key, color = \'#22d3ee\', width = 120, height = 30) {\n    const data = getSparkline(key);\n    if (data.length < 2) return \'\';\n    \n    const min = Math.min(...data);\n    const max = Math.max(...data);\n    const range = max - min || 1;\n    \n    const points = data.map((v, i) => {\n        const x = (i / (data.length - 1)) * width;\n        const y = height - ((v - min) / range) * (height - 4) - 2;\n        return `${x},${y}`;\n    }).join(\' \');\n    \n    return `<svg width="${width}" height="${height}" class="mt-2 opacity-60">\n        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>\n    </svg>`;\n}\n\nasync function loadDashboardFromServer() {\n    try {\n        const resp = await fetch(\'/api/dashboard\');\n        if (resp.ok) {\n            const data = await resp.json();\n            _pickerCards = data.cards || [];\n            _pickerGroups = data.groups || [];\n            _hiddenSensors = data.hiddenSensors || [];\n            _dashboardLoaded = true;\n            return;\n        }\n    } catch (e) {}\n    _pickerCards = [];\n    _pickerGroups = [];\n    _hiddenSensors = [];\n    _dashboardLoaded = true;\n}\n\nfunction getPickerCards() {\n    return _pickerCards || [];\n}\n\nfunction setPickerCards(cards) {\n    _pickerCards = cards;\n    scheduleDashboardSave();\n}\n\nfunction getPickerGroups() {\n    return _pickerGroups || [];\n}\n\nfunction setPickerGroups(groups) {\n    _pickerGroups = groups;\n    scheduleDashboardSave();\n}\n\nfunction scheduleDashboardSave() {\n    if (_dashboardSaveTimer) clearTimeout(_dashboardSaveTimer);\n    _dashboardSaveTimer = setTimeout(saveDashboardToServer, 500);\n}\n\nasync function saveDashboardToServer() {\n    try {\n        await fetch(\'/api/dashboard\', {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ cards: _pickerCards || [], groups: _pickerGroups || [], hiddenSensors: _hiddenSensors || [] })\n        });\n    } catch (e) {}\n}\n\nasync function loadPickerCards() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    await loadDashboardFromServer();\n\n    if (!canvas._groupHandlersAttached) {\n        canvas.addEventListener(\'dragover\', onGroupDragOver);\n        canvas.addEventListener(\'drop\', onGroupDropOutside);\n        canvas._groupHandlersAttached = true;\n    }\n\n    const groups = getPickerGroups();\n    if (groups.length) {\n        groups.forEach(g => {\n            if (!document.querySelector(`[data-group-id="${g.id}"]`)) {\n                renderDashboardGroup(g);\n            }\n        });\n    }\n\n    const cards = getPickerCards();\n    if (!cards.length && !groups.length) return;\n\n    let positionsChanged = false;\n    cards.forEach(c => {\n        if (document.querySelector(`[data-card-id="${c.id}"]`)) return;\n        if (!c.col || !c.row) { positionsChanged = true; }\n        renderPickerCard(c);\n        if (c.groupId) {\n            const groupEl = document.querySelector(`[data-group-id="${c.groupId}"] .group-cards`);\n            const cardEl = document.querySelector(`[data-card-id="${c.id}"]`);\n            if (groupEl && cardEl) {\n                groupEl.appendChild(cardEl);\n                cardEl.classList.remove(\'cursor-grab\');\n                cardEl.classList.add(\'cursor-default\');\n            }\n        }\n    });\n\n    for (const c of cards) {\n        if (c.groupId) continue;\n        const colSp = c.colSpan || 3;\n        const rowSp = c.rowSpan || 1;\n        if (isCellOccupied(c.col, c.row, colSp, rowSp, c.id)) {\n            const free = findFreePosition(cards, colSp, rowSp, c.id);\n            c.col = free.col;\n            c.row = free.row;\n            const el = document.querySelector(`[data-card-id="${c.id}"]`);\n            if (el) {\n                el.style.gridColumn = `${c.col} / span ${colSp}`;\n                el.style.gridRow = `${c.row} / span ${rowSp}`;\n            }\n            positionsChanged = true;\n        }\n    }\n    if (positionsChanged) setPickerCards(cards);\n    document.getElementById(\'dashboard-empty\')?.classList.add(\'hidden\');\n    startPickerLiveUpdate();\n    prefetchSmartForCards();\n    updateCanvasMinHeight();\n}\n\nfunction updateCanvasMinHeight() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    let maxRow = 0;\n    for (const c of getPickerCards()) {\n        if (!c.row) continue;\n        const bottom = c.row + (c.rowSpan || 1) - 1;\n        if (bottom > maxRow) maxRow = bottom;\n    }\n    for (const gEl of canvas.querySelectorAll(\'[data-group-id]\')) {\n        const rect = gEl.getBoundingClientRect();\n        const cRect = canvas.getBoundingClientRect();\n        const cs = getComputedStyle(canvas);\n        const padT = parseFloat(cs.paddingTop) || 16;\n        const rowH = 100;\n        const gap = 8;\n        const gRowEnd = Math.max(1, Math.round((rect.bottom - cRect.top - padT) / (rowH + gap)) + 1);\n        if (gRowEnd > maxRow) maxRow = gRowEnd;\n    }\n    const minRows = Math.max(maxRow + 5, 8);\n    const rowH = 100;\n    const gap = 8;\n    const padY = 32;\n    canvas.style.minHeight = (minRows * (rowH + gap) - gap + padY) + \'px\';\n}\n\nasync function prefetchSmartForCards() {\n    const cards = getPickerCards().filter(c => c.type === \'disk\' && c.smartAttributes?.length);\n    for (const card of cards) {\n        if (_smartCache[card.sourceId]) continue;\n        try {\n            const data = await fetchDiskSmart(card.sourceId, false, card.source || \'local\');\n            if (data && !data.error) {\n                _smartCache[card.sourceId] = data;\n                updateCardDetails(card.id);\n            }\n        } catch (e) {}\n    }\n}\n\nlet _pickerLiveTimer = null;\n\nfunction startPickerLiveUpdate() {\n    if (_pickerLiveTimer) return;\n    _pickerLiveTimer = setInterval(() => {\n        document.querySelectorAll(\'[data-fan-id]\').forEach(el => {\n            const src = el.dataset.source;\n            const id = el.dataset.fanId;\n            let fan = null;\n            if (src === \'local\' && currentState?.fans?.[id]) {\n                fan = currentState.fans[id];\n            } else {\n                const node = nodesData.find(n => n.node_id === src);\n                fan = node?.telemetry?.fans?.[id];\n            }\n            if (fan) {\n                el.textContent = fan.rpm || 0;\n                pushSparkline(`fan:${src}:${id}`, fan.rpm || 0);\n                const cardEl = el.closest(\'[data-card-id]\');\n                if (cardEl) updateCardDetails(cardEl.dataset.cardId);\n                const dot = cardEl?.querySelector(\'.status-dot\');\n                if (dot) {\n                    const s = fan.status || \'unknown\';\n                    dot.className = \'status-dot \' + (s === \'running\' ? \'green\' : (s === \'failsafe\' || s === \'critical\') ? \'red\' : \'yellow\');\n                }\n                const animEl = document.querySelector(`[data-fan-anim-id="${id}"][data-fan-source="${src}"]`);\n                if (animEl) {\n                    const rpm = fan.rpm || 0;\n                    const dur = rpm > 0 ? Math.max(0.2, 2 - (rpm / 1500)) : 0;\n                    animEl.style.animation = rpm > 0 ? `fan-spin ${dur}s linear infinite` : \'none\';\n                    const fanColor = fan.status === \'running\' ? \'#22d3ee\' : (fan.status === \'failsafe\' || fan.status === \'critical\') ? \'#ef4444\' : \'#facc15\';\n                    animEl.querySelectorAll(\'path, circle\').forEach(p => p.setAttribute(\'fill\', fanColor));\n                }\n            }\n        });\n        document.querySelectorAll(\'[data-temp-id]\').forEach(el => {\n            const src = el.dataset.source;\n            const id = el.dataset.tempId;\n            let val = null;\n            if (src === \'local\' && currentState?.temp_sensors?.[id]) {\n                val = currentState.temp_sensors[id].value;\n            } else {\n                const node = nodesData.find(n => n.node_id === src);\n                val = node?.telemetry?.temp_sensors?.[id]?.value;\n            }\n            if (val != null) el.textContent = val;\n            pushSparkline(`temp:${src}:${id}`, val);\n        });\n        document.querySelectorAll(\'[data-disk-id]\').forEach(el => {\n            const id = el.dataset.diskId;\n            const src = el.dataset.source;\n            let temp = null;\n            if (src === \'local\') {\n                temp = currentState?.hdd_sensors?.[id]?.temp;\n            } else {\n                const node = nodesData.find(n => n.node_id === src);\n                temp = node?.telemetry?.hdd_sensors?.[id]?.temp;\n            }\n            if (temp != null) {\n                el.textContent = temp || \'--\';\n                pushSparkline(`disk:${src}:${id}`, temp || 0);\n            }\n        });\n        getPickerCards().filter(c => c.type === \'disk\' && c.smartAttributes?.length).forEach(c => {\n            if (_smartCache[c.sourceId]) {\n                const cardEl = document.querySelector(`[data-card-id="${c.id}"]`);\n                if (cardEl) {\n                    const detailsEl = cardEl.querySelector(\'.card-details\');\n                    if (detailsEl) updateDiskCardDetails(c, detailsEl);\n                }\n            }\n        });\n    }, 2000);\n}\n\nfunction stopPickerLiveUpdate() {\n    if (_pickerLiveTimer) {\n        clearInterval(_pickerLiveTimer);\n        _pickerLiveTimer = null;\n    }\n}\n\nlet _systemTimer = null;\nfunction startSystemUpdate() {\n    if (_systemTimer) return;\n    _systemTimer = setInterval(async () => {\n        try {\n            const resp = await fetch(\'/api/system\');\n            const data = await resp.json();\n            document.querySelectorAll(\'[data-system-field="uptime"]\').forEach(el => el.textContent = data.uptime || \'--\');\n            document.querySelectorAll(\'[data-system-field="cpu"]\').forEach(el => el.textContent = (data.cpu_load || 0) + \'%\');\n            document.querySelectorAll(\'[data-system-field="mem"]\').forEach(el => el.textContent = (data.mem_percent || 0) + \'%\');\n            document.querySelectorAll(\'[data-system-bar="cpu"]\').forEach(el => el.style.width = (data.cpu_load || 0) + \'%\');\n            document.querySelectorAll(\'[data-system-bar="mem"]\').forEach(el => el.style.width = (data.mem_percent || 0) + \'%\');\n        } catch(e) {}\n    }, 5000);\n}\nfunction stopSystemUpdate() {\n    if (_systemTimer) { clearInterval(_systemTimer); _systemTimer = null; }\n}\n\nfunction showGroupCreator() {\n    const modal = document.getElementById(\'group-creator-modal\');\n    if (modal) modal.classList.remove(\'hidden\');\n}\n\nfunction hideGroupCreator() {\n    const modal = document.getElementById(\'group-creator-modal\');\n    if (modal) modal.classList.add(\'hidden\');\n}\n\nfunction createGroup() {\n    const name = document.getElementById(\'group-name-input\')?.value?.trim();\n    if (!name) return;\n\n    const group = {\n        id: \'group-\' + Date.now(),\n        name: name,\n    };\n\n    const groups = getPickerGroups();\n    groups.push(group);\n    setPickerGroups(groups);\n\n    renderDashboardGroup(group);\n    hideGroupCreator();\n    document.getElementById(\'dashboard-empty\')?.classList.add(\'hidden\');\n}\n\nfunction renderDashboardGroup(group) {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n\n    const el = document.createElement(\'div\');\n    el.className = \'dashboard-group bg-cyber-bg border-2 border-dashed border-gray-700 rounded-xl p-3 transition-colors hover:border-neon-purple/50 relative\';\n    el.setAttribute(\'data-group-id\', group.id);\n    el.setAttribute(\'draggable\', \'true\');\n    el.style.gridColumn = `span ${group.colSpan || getCanvasCols()}`;\n    el.style.display = \'flex\';\n    el.style.flexDirection = \'column\';\n    if (group.minHeight) el.style.minHeight = group.minHeight;\n\n    el.innerHTML = `\n        <div class="flex items-center justify-between mb-2">\n            <div class="flex items-center gap-2">\n                <span class="text-gray-600 text-xs select-none cursor-grab">⠿</span>\n                <span class="text-xs text-gray-400 font-medium cursor-pointer hover:text-white transition-colors" onclick="startGroupRename(\'${group.id}\')">${escapeHtml(group.name)}</span>\n            </div>\n            <button onclick="removePickerGroup(\'${group.id}\')" class="text-gray-600 hover:text-red-400 text-xs transition-colors">×</button>\n        </div>\n        <div class="group-cards flex flex-wrap gap-2 flex-1"></div>\n        <div class="group-resize-handle absolute bottom-0 right-0 w-4 h-4 cursor-ns-resize opacity-30 hover:opacity-80 transition-opacity"></div>`;\n\n    el.addEventListener(\'dragstart\', onGroupDragStart);\n    el.addEventListener(\'dragover\', onGroupCardDragOver);\n    el.addEventListener(\'drop\', onGroupDrop);\n    el.addEventListener(\'dragleave\', onGroupDragLeave);\n    el.addEventListener(\'dragend\', onGroupDragEnd);\n\n    const handle = el.querySelector(\'.group-resize-handle\');\n    handle.addEventListener(\'mousedown\', (e) => startGroupResize(e, group.id));\n\n    canvas.appendChild(el);\n}\n\nfunction removePickerGroup(groupId) {\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (!el) return;\n\n    const cards = el.querySelectorAll(\'[data-card-id]\');\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    cards.forEach(card => {\n        card.classList.remove(\'cursor-grab\');\n        canvas.appendChild(card);\n    });\n\n    el.remove();\n\n    const saved = getPickerGroups().filter(g => g.id !== groupId);\n    setPickerGroups(saved);\n\n    const allCards = getPickerCards();\n    allCards.forEach(c => { if (c.groupId === groupId) delete c.groupId; });\n    setPickerCards(allCards);\n\n    if (!saved.length && !document.querySelector(\'[data-card-id]\')) {\n        document.getElementById(\'dashboard-empty\')?.classList.remove(\'hidden\');\n    }\n    updateCanvasMinHeight();\n}\n\nfunction onGroupCardDragOver(e) {\n    e.preventDefault();\n    e.dataTransfer.dropEffect = \'move\';\n}\n\nfunction onGroupDragLeave(e) {\n    this.classList.remove(\'border-neon-purple\', \'bg-purple-900/10\');\n}\n\nfunction onGroupDropOutside(e) {\n    if (_draggedGroup) {\n        _draggedGroup.classList.remove(\'opacity-40\');\n        _draggedGroup = null;\n    }\n}\n\nfunction onGroupDrop(e) {\n    e.preventDefault();\n    e.stopPropagation();\n    this.classList.remove(\'border-neon-purple\', \'bg-purple-900/10\');\n\n    const cardId = e.dataTransfer.getData(\'text/plain\');\n    const groupId = e.dataTransfer.getData(\'text/group\');\n    if (!cardId && !groupId) return;\n\n    if (cardId) {\n        const cardEl = document.querySelector(`[data-card-id="${cardId}"]`);\n        const groupCards = this.querySelector(\'.group-cards\');\n        if (!cardEl || !groupCards) return;\n\n        const saved = getPickerCards();\n        const cardData = saved.find(c => c.id === cardId);\n        if (cardData) {\n            cardData.groupId = this.dataset.groupId;\n            setPickerCards(saved);\n        }\n\n        groupCards.appendChild(cardEl);\n        cardEl.classList.remove(\'cursor-grab\');\n        cardEl.classList.add(\'cursor-default\');\n    }\n}\n\nlet _resizingGroupId = null;\nlet _resizeStartY = 0;\nlet _resizeStartH = 0;\n\nfunction startGroupResize(e, groupId) {\n    e.preventDefault();\n    e.stopPropagation();\n    _resizingGroupId = groupId;\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (!el) return;\n    _resizeStartY = e.clientY;\n    _resizeStartH = el.offsetHeight;\n    document.addEventListener(\'mousemove\', onGroupResize);\n    document.addEventListener(\'mouseup\', stopGroupResize);\n}\n\nfunction onGroupResize(e) {\n    if (!_resizingGroupId) return;\n    const el = document.querySelector(`[data-group-id="${_resizingGroupId}"]`);\n    if (!el) return;\n    const h = Math.max(100, _resizeStartH + (e.clientY - _resizeStartY));\n    el.style.minHeight = h + \'px\';\n}\n\nfunction stopGroupResize() {\n    if (!_resizingGroupId) return;\n    const groups = getPickerGroups();\n    const group = groups.find(g => g.id === _resizingGroupId);\n    const el = document.querySelector(`[data-group-id="${_resizingGroupId}"]`);\n    if (group && el) {\n        group.minHeight = el.style.minHeight;\n        setPickerGroups(groups);\n    }\n    _resizingGroupId = null;\n    document.removeEventListener(\'mousemove\', onGroupResize);\n    document.removeEventListener(\'mouseup\', stopGroupResize);\n}\n\nlet _draggedGroup = null;\nlet _groupDropTarget = null;\n\nfunction onGroupDragStart(e) {\n    if (e.target.closest(\'.group-resize-handle\') || e.target.closest(\'button\') || e.target.closest(\'input\')) return;\n    _draggedGroup = this;\n    _groupDropTarget = null;\n    this.classList.add(\'opacity-40\');\n    e.dataTransfer.effectAllowed = \'move\';\n    e.dataTransfer.setData(\'text/group\', this.dataset.groupId);\n}\n\nfunction onGroupDragOver(e) {\n    if (!_draggedGroup) return;\n    e.preventDefault();\n    e.dataTransfer.dropEffect = \'move\';\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    _groupDropTarget = getDragAfterElement(canvas, e.clientX, e.clientY);\n}\n\nfunction onGroupDragEnd() {\n    if (_draggedGroup) {\n        if (_groupDropTarget !== undefined) {\n            const canvas = document.getElementById(\'dashboard-canvas\');\n            if (_groupDropTarget) {\n                canvas.insertBefore(_draggedGroup, _groupDropTarget);\n            } else {\n                canvas.appendChild(_draggedGroup);\n            }\n        }\n        _draggedGroup.classList.remove(\'opacity-40\');\n        _draggedGroup = null;\n        saveGroupOrder();\n    }\n}\n\nfunction saveGroupOrder() {\n    const canvas = document.getElementById(\'dashboard-canvas\');\n    if (!canvas) return;\n    const ordered = [...canvas.querySelectorAll(\'[data-group-id]\')].map(el => el.dataset.groupId);\n    const saved = getPickerGroups();\n    const orderedGroups = ordered.map(id => saved.find(g => g.id === id)).filter(Boolean);\n    setPickerGroups(orderedGroups);\n}\n\nfunction startGroupRename(groupId) {\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (!el) return;\n    const nameSpan = el.querySelector(\'.flex.items-center.justify-between span\');\n    if (!nameSpan) return;\n\n    const groups = getPickerGroups();\n    const group = groups.find(g => g.id === groupId);\n    if (!group) return;\n\n    const input = document.createElement(\'input\');\n    input.type = \'text\';\n    input.value = group.name;\n    input.className = \'bg-cyber-bg border border-neon-purple rounded px-1 py-0 text-xs text-white w-32\';\n    input.onblur = () => finishGroupRename(groupId, input.value);\n    input.onkeydown = (e) => { if (e.key === \'Enter\') input.blur(); if (e.key === \'Escape\') { input.value = group.name; input.blur(); } };\n\n    nameSpan.replaceWith(input);\n    input.focus();\n    input.select();\n}\n\nfunction finishGroupRename(groupId, newName) {\n    newName = newName.trim();\n    if (!newName) return;\n\n    const groups = getPickerGroups();\n    const group = groups.find(g => g.id === groupId);\n    if (!group) return;\n\n    group.name = newName;\n    setPickerGroups(groups);\n\n    const el = document.querySelector(`[data-group-id="${groupId}"]`);\n    if (el) {\n        const input = el.querySelector(\'input[type="text"]\');\n        if (input) {\n            const span = document.createElement(\'span\');\n            span.className = \'text-xs text-gray-400 font-medium cursor-pointer hover:text-white transition-colors\';\n            span.setAttribute(\'onclick\', `startGroupRename(\'${groupId}\')`);\n            span.textContent = newName;\n            input.replaceWith(span);\n        }\n    }\n}\n\nfunction getStatusBadgeClass(status) {\n    const classes = {\n        \'nominal\': \'bg-green-900 bg-opacity-30 text-neon-green\',\n        \'warning\': \'bg-orange-900 bg-opacity-30 text-neon-orange\',\n        \'critical\': \'bg-red-900 bg-opacity-30 text-neon-red\',\n        \'failsafe\': \'bg-red-900 bg-opacity-50 text-neon-red\',\n        \'standby\': \'bg-blue-900 bg-opacity-30 text-blue-400\',\n        \'inverted\': \'bg-cyan-900 bg-opacity-30 text-neon-cyan\',\n        \'no_sensor\': \'bg-yellow-900 bg-opacity-30 text-neon-orange\',\n        \'not_tested\': \'bg-gray-700 text-gray-400\',\n        \'calibrating\': \'bg-purple-900 bg-opacity-30 text-neon-purple\',\n    };\n    return classes[status] || \'bg-gray-700 text-gray-400\';\n}\n\n// ============================================================================\n// INSPECTOR (Right Panel)\n// ============================================================================\n\nfunction updateInspector(fan) {\n    document.getElementById(\'inspector-empty\')?.classList.add(\'hidden\');\n    document.getElementById(\'inspector-fan\')?.classList.remove(\'hidden\');\n\n    const inspectorTitle = document.getElementById(\'inspector-title\');\n    if (inspectorTitle) inspectorTitle.textContent = fan.label;\n    const inspectorSubtitle = document.getElementById(\'inspector-subtitle\');\n    if (inspectorSubtitle) inspectorSubtitle.textContent = `ID: ${fan.id || \'unknown\'}`;\n\n    const fanName = document.getElementById(\'fan-name\');\n    if (fanName) fanName.textContent = fan.label;\n    \n    const statusBadge = document.getElementById(\'fan-status-badge\');\n    if (statusBadge) {\n        statusBadge.textContent = t(\'status.\' + fan.status, fan.status || \'unknown\');\n        statusBadge.className = `text-xs px-2 py-0.5 rounded-full ${getStatusBadgeClass(fan.status)}`;\n    }\n    \n    const invertedBadge = document.getElementById(\'fan-inverted-badge\');\n    if (invertedBadge) {\n        invertedBadge.classList.toggle(\'hidden\', !fan.inverted);\n    }\n    \n    const modeBadge = document.getElementById(\'fan-mode-badge\');\n    const mode = fan.mode || \'manual\';\n    if (modeBadge) {\n        modeBadge.textContent = t(\'mode.\' + mode, mode).toUpperCase();\n        modeBadge.className = mode === \'auto\' \n            ? \'text-xs px-2 py-0.5 rounded-full bg-cyan-900 bg-opacity-30 text-neon-cyan\'\n            : \'text-xs px-2 py-0.5 rounded-full bg-purple-900 bg-opacity-30 text-neon-purple\';\n    }\n    \n    const rpmDisplay = document.getElementById(\'fan-rpm-display\');\n    if (rpmDisplay) {\n        rpmDisplay.textContent = fan.rpm || 0;\n        rpmDisplay.classList.remove(\'text-neon-cyan\', \'text-neon-orange\', \'text-neon-red\');\n        if (fan.rpm > (fan.max_rpm * 0.8 || 1500)) {\n            rpmDisplay.classList.add(\'text-neon-orange\');\n        } else if (fan.status === \'failsafe\' || fan.status === \'critical\') {\n            rpmDisplay.classList.add(\'text-neon-red\');\n        } else {\n            rpmDisplay.classList.add(\'text-neon-cyan\');\n        }\n    }\n    \n    if (!isDragging) {\n        const slider = document.getElementById(\'pwm-slider\');\n        const pct = fan.current_pct != null ? fan.current_pct : (fan.manual_pct != null ? fan.manual_pct : 50);\n        if (slider) {\n            slider.value = pct;\n            slider.disabled = (mode === \'auto\');\n        }\n        const pwmValueDisplay = document.getElementById(\'pwm-value-display\');\n        if (pwmValueDisplay) pwmValueDisplay.textContent = `${pct}%`;\n    }\n    \n    const btnManual = document.getElementById(\'btn-mode-manual\');\n    const btnAuto = document.getElementById(\'btn-mode-auto\');\n    \n    if (btnManual && btnAuto) {\n        if (mode === \'manual\') {\n            btnManual.className = BTN_MANUAL_ACTIVE;\n            btnAuto.className = BTN_AUTO_INACTIVE;\n        } else {\n            btnManual.className = BTN_MANUAL_INACTIVE;\n            btnAuto.className = BTN_AUTO_ACTIVE;\n        }\n    }\n    \n    const autoSettings = document.getElementById(\'auto-settings\');\n    if (autoSettings) {\n        autoSettings.style.display = (mode === \'auto\') ? \'block\' : \'none\';\n    }\n    \n    // Render schedule grid when in auto mode\n    if (mode === \'auto\') {\n        setTimeout(() => renderScheduleGrid(), 50);\n    }\n    \n    // Store config\n    if (!fanConfigs[currentFanId]) fanConfigs[currentFanId] = {};\n    fanConfigs[currentFanId].sensors = fan.sensors || [];\n    fanConfigs[currentFanId].target_temp = fan.target_temp || 31;\n    fanConfigs[currentFanId].mode = mode;\n    fanConfigs[currentFanId].sensor_mode = fan.sensor_mode || \'max\';\n\n    // Calibration params\n    const cal = fan.calibration || {};\n    const minPwmEl = document.getElementById(\'cal-min-pwm\');\n    const maxPwmEl = document.getElementById(\'cal-max-pwm\');\n    const lambdaEl = document.getElementById(\'cal-lambda\');\n    if (minPwmEl) {\n        minPwmEl.value = cal.min_pwm || 0;\n        const calMinPwmVal = document.getElementById(\'cal-min-pwm-val\');\n        if (calMinPwmVal) calMinPwmVal.textContent = cal.min_pwm || 0;\n    }\n    if (maxPwmEl) {\n        maxPwmEl.value = cal.max_pwm || 255;\n        const calMaxPwmVal = document.getElementById(\'cal-max-pwm-val\');\n        if (calMaxPwmVal) calMaxPwmVal.textContent = cal.max_pwm || 255;\n    }\n    if (lambdaEl) {\n        lambdaEl.value = (cal.lambda || 1.0) * 10;\n        const calLambdaVal = document.getElementById(\'cal-lambda-val\');\n        if (calLambdaVal) calLambdaVal.textContent = (cal.lambda || 1.0).toFixed(1);\n    }\n}\n\n// ============================================================================\n// FAN CONTROL ACTIONS\n// ============================================================================\n\nfunction setFanMode(mode) {\n    if (!currentFanId) return;\n    \n    // Update local state immediately for instant UI feedback\n    if (currentState?.fans?.[currentFanId]) {\n        currentState.fans[currentFanId].mode = mode;\n    }\n    if (fanConfigs[currentFanId]) {\n        fanConfigs[currentFanId].mode = mode;\n    }\n    \n    // Update button styles immediately\n    const btnManual = document.getElementById(\'btn-mode-manual\');\n    const btnAuto = document.getElementById(\'btn-mode-auto\');\n    if (btnManual && btnAuto) {\n        if (mode === \'manual\') {\n            btnManual.className = BTN_MANUAL_ACTIVE;\n            btnAuto.className = BTN_AUTO_INACTIVE;\n        } else {\n            btnManual.className = BTN_MANUAL_INACTIVE;\n            btnAuto.className = BTN_AUTO_ACTIVE;\n        }\n    }\n    \n    document.getElementById(\'auto-settings\').style.display = (mode === \'auto\') ? \'block\' : \'none\';\n    if (mode === \'auto\') {\n        setTimeout(() => renderScheduleGrid(), 50);\n    }\n    \n    sendControl({\n        action: \'set_fan_config\',\n        fan: currentFanId,\n        fan_mode: mode\n    });\n}\n\nfunction sendControl(payload) {\n    fetch(\'/api/control\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify(payload)\n    })\n    .then(r => r.json())\n    .catch(err => console.error(\'Control error:\', err));\n}\n\n// ============================================================================\n// PWM SLIDER\n// ============================================================================\n\ndocument.addEventListener(\'DOMContentLoaded\', () => {\n    updateCanvasColumns();\n    window.addEventListener(\'resize\', updateCanvasColumns);\n\n    const slider = document.getElementById(\'pwm-slider\');\n    if (!slider) return;\n    \n    slider.addEventListener(\'input\', (e) => {\n        document.getElementById(\'pwm-value-display\').textContent = `${e.target.value}%`;\n    });\n    \n    slider.addEventListener(\'mousedown\', () => {\n        isDragging = true;\n    });\n    \n    slider.addEventListener(\'mouseup\', (e) => {\n        isDragging = false;\n        applyPWM(e.target.value);\n    });\n    \n    slider.addEventListener(\'touchend\', (e) => {\n        isDragging = false;\n        applyPWM(e.target.value);\n    });\n});\n\nfunction applyPWM(value) {\n    if (!currentFanId) return;\n    \n    sendControl({\n        action: \'set_fan_pwm\',\n        fan: currentFanId,\n        pwm: parseInt(value)\n    });\n}\n\n// ============================================================================\n// SENSOR POPUP\n// ============================================================================\n\nfunction buildSensorList(data) {\n    allSensors = [];\n    const hidden = getHiddenSensors();\n\n    if (data.hdd_sensors) {\n        for (const [id, disk] of Object.entries(data.hdd_sensors)) {\n            if (hidden.includes(`disk:${id}`)) continue;\n            allSensors.push({\n                id: `hdd:${id}`,\n                label: disk.label,\n                temp: disk.temp,\n                standby: disk.standby,\n                group: \'sensors.disks\'\n            });\n        }\n    }\n\n    if (data.temp_sensors) {\n        for (const [id, sensor] of Object.entries(data.temp_sensors)) {\n            if (hidden.includes(`temp:${id}`)) continue;\n            allSensors.push({\n                id: `temp:${id}`,\n                label: sensor.label,\n                temp: sensor.value,\n                standby: false,\n                group: \'sensors.sensors_group\'\n            });\n        }\n    }\n}\n\nfunction toggleSensorPopup() {\n    const popup = document.getElementById(\'sensor-popup\');\n    const list = document.getElementById(\'sensor-popup-list\');\n    \n    if (!popup || !list) return;\n    \n    if (popup.classList.contains(\'hidden\')) {\n        // Build list\n        const currentSensors = fanConfigs[currentFanId]?.sensors || [];\n        \n        // Group sensors\n        const groups = {};\n        allSensors.forEach(s => {\n            if (!groups[s.group]) groups[s.group] = [];\n            groups[s.group].push(s);\n        });\n        \n        let html = \'\';\n        for (const [group, sensors] of Object.entries(groups)) {\n            html += `<div class="text-xs font-semibold text-gray-500 uppercase mb-2">${t(group, group)}</div>`;\n            sensors.forEach(s => {\n                const checked = currentSensors.includes(s.id);\n                html += `\n                    <label class="flex items-center gap-2 py-1.5 cursor-pointer hover:bg-cyber-accent rounded px-2">\n                        <input type="checkbox" value="${escapeHtml(s.id)}" ${checked ? \'checked\' : \'\'} \n                               class="accent-neon-purple">\n                        <span class="text-sm text-gray-300">${escapeHtml(s.label)}</span>\n                        <span class="text-xs text-gray-500 ml-auto">\n                            ${s.standby ? t(\'sensor.sleep\', \'Sleep\') : formatTemp(s.temp)}\n                        </span>\n                    </label>\n                `;\n            });\n        }\n        \n        list.innerHTML = html;\n        popup.classList.remove(\'hidden\');\n    } else {\n        closeSensorPopup();\n    }\n}\n\nfunction closeSensorPopupForContext() {\n    const popup = document.getElementById(\'sensor-popup\');\n    if (!popup) return;\n    \n    if (popup._scheduleMode) {\n        toggleScheduleSensorPopup();\n    } else {\n        closeSensorPopup();\n    }\n}\n\nfunction closeSensorPopup() {\n    const popup = document.getElementById(\'sensor-popup\');\n    if (!popup) return;\n    \n    // Collect checked sensors\n    const checked = popup.querySelectorAll(\'input[type=checkbox]:checked\');\n    const sensors = Array.from(checked).map(cb => cb.value);\n    \n    if (currentFanId) {\n        if (!fanConfigs[currentFanId]) fanConfigs[currentFanId] = {};\n        fanConfigs[currentFanId].sensors = sensors;\n        \n        sendControl({\n            action: \'set_fan_config\',\n            fan: currentFanId,\n            sensors: sensors\n        });\n        \n        // Update no-sensor warning and sensor mode section\n        const mode = fanConfigs[currentFanId]?.mode || \'manual\';\n        const noSensorWarning = document.getElementById(\'no-sensor-warning\');\n        const sensorModeSection = document.getElementById(\'sensor-mode-section\');\n        if (noSensorWarning) {\n            noSensorWarning.classList.toggle(\'hidden\', sensors.length > 0 || mode !== \'auto\');\n        }\n        if (sensorModeSection) {\n            sensorModeSection.classList.toggle(\'hidden\', sensors.length <= 1);\n        }\n    }\n    \n    popup.classList.add(\'hidden\');\n}\n\n// ============================================================================\n// CHART (ApexCharts)\n// ============================================================================\n\nfunction updateChart() {\n    const now = Date.now();\n    if (now - lastChartUpdate < CHART_UPDATE_INTERVAL) return;\n    \n    const chartContainer = document.getElementById(\'temp-chart\');\n    if (!chartContainer || chartContainer.offsetParent === null) return;\n    \n    lastChartUpdate = now;\n    \n    fetch(\'/api/history?hours=24\')\n        .then(r => r.json())\n        .then(data => {\n            if (!data.has_data) return;\n            \n            const series = [\n                {\n                    name: t(\'chart.max_hdd_temp\', \'Max HDD Temp\'),\n                    data: data.timestamps.map((ts, i) => ({\n                        x: new Date(ts).getTime(),\n                        y: data.temps[i]\n                    }))\n                },\n                {\n                    name: t(\'chart.avg_pwm\', \'Avg PWM\'),\n                    data: data.timestamps.map((ts, i) => ({\n                        x: new Date(ts).getTime(),\n                        y: data.pwm[i]\n                    }))\n                }\n            ];\n            \n            if (!chart) {\n                chart = new ApexCharts(chartContainer, {\n                    chart: {\n                        type: \'line\',\n                        height: 250,\n                        background: \'transparent\',\n                        foreColor: \'#9ca3af\',\n                        toolbar: { show: false },\n                        zoom: { enabled: false },\n                        animations: {\n                            enabled: true,\n                            easing: \'easeinout\',\n                            speed: 800\n                        }\n                    },\n                    theme: { mode: \'dark\' },\n                    stroke: {\n                        curve: \'smooth\',\n                        width: [2, 1.5],\n                        dashArray: [0, 5]\n                    },\n                    colors: [\'#ff2d55\', \'#00f0ff\'],\n                    fill: {\n                        type: \'gradient\',\n                        gradient: {\n                            shade: \'dark\',\n                            type: \'vertical\',\n                            opacityFrom: 0.3,\n                            opacityTo: 0\n                        }\n                    },\n                    markers: {\n                        size: 0,\n                        hover: { size: 4 }\n                    },\n                    grid: {\n                        borderColor: \'#1a1f2e\',\n                        strokeDashArray: 4\n                    },\n                    xaxis: {\n                        type: \'datetime\',\n                        labels: {\n                            style: { colors: \'#6b7280\' }\n                        }\n                    },\n                    yaxis: [\n                        {\n                            title: { text: getTempUnitSymbol(), style: { color: \'#ff2d55\' } },\n                            labels: { style: { colors: \'#6b7280\' } }\n                        },\n                        {\n                            opposite: true,\n                            title: { text: \'%\', style: { color: \'#00f0ff\' } },\n                            labels: { style: { colors: \'#6b7280\' } },\n                            min: 0,\n                            max: 100\n                        }\n                    ],\n                    legend: {\n                        position: \'top\',\n                        labels: { colors: \'#9ca3af\' }\n                    },\n                    tooltip: {\n                        theme: \'dark\',\n                        x: { format: \'HH:mm\' }\n                    }\n                });\n                \n                chart.render();\n            } else {\n                chart.updateSeries(series);\n            }\n        })\n        .catch(err => console.error(\'Chart error:\', err));\n}\n\n// Update chart every 60 seconds\nsetInterval(updateChart, 60000);\n\n// ============================================================================\n// DISKS LIST (Left Panel Bottom)\n// ============================================================================\n\nfunction buildDisksList(disks) {\n    const container = document.getElementById(\'disks-mini-list\');\n    if (!container) return;\n    \n    let html = \'\';\n    \n    for (const [id, disk] of Object.entries(disks)) {\n        const pct = disk.pct_fill || 0;\n        const colorMap = {\n            \'cyan\': \'bg-neon-cyan\',\n            \'orange\': \'bg-neon-orange\',\n            \'red\': \'bg-neon-red\',\n            \'critical\': \'bg-neon-red animate-pulse\',\n            \'unknown\': \'bg-gray-600\'\n        };\n        const barColor = colorMap[disk.color_zone] || \'bg-gray-600\';\n        \n        html += `\n            <div class="flex items-center gap-2">\n                <span class="text-xs text-gray-400 w-14 truncate">${escapeHtml(disk.label)}</span>\n                <div class="flex-1 h-1.5 bg-cyber-accent rounded-full overflow-hidden">\n                    <div class="h-full ${barColor} rounded-full progress-fill" style="width: ${pct}%"></div>\n                </div>\n                <span class="text-xs font-mono w-10 text-right ${getTempColorClass(disk.temp)}">\n                    ${disk.standby ? t(\'sensor.sleep\', \'Sleep\') : disk.temp > 0 ? formatTemp(disk.temp) : \'--\'}\n                </span>\n            </div>\n        `;\n    }\n    \n    container.innerHTML = html || `<div class="text-xs text-gray-500">${t(\'setup.no_disks\', \'No disks detected\')}</div>`;\n}\n\nfunction getTempColorClass(temp) {\n    if (temp <= 0) return \'text-gray-500\';\n    if (temp <= 35) return \'text-neon-cyan\';\n    if (temp <= 45) return \'text-neon-orange\';\n    return \'text-neon-red\';\n}\n\n// ============================================================================\n// SETUP WIZARD\n// ============================================================================\n\nfunction runDiscovery() {\n    console.log(\'[FanControl] Starting hardware discovery...\');\n    \n    setDiscoverButtonState(true);\n    wizardStep = \'scanning\';\n    \n    fetch(\'/api/discover\', { method: \'POST\' })\n        .then(r => r.json())\n        .then(data => {\n            setDiscoverButtonState(false);\n            \n            if (data.status === \'ok\') {\n                renderDiscoveredHardware(data);\n                wizardStep = \'results\';\n                \n                document.getElementById(\'setup-step-intro\').classList.add(\'hidden\');\n                document.getElementById(\'setup-step-results\').classList.remove(\'hidden\');\n            } else {\n                alert(\'Scan error: \' + data.message);\n                wizardStep = \'intro\';\n            }\n        })\n        .catch(err => {\n            console.error(\'Discovery error:\', err);\n            alert(\'Connection error during scan\');\n            setDiscoverButtonState(false);\n            wizardStep = \'intro\';\n        });\n}\n\nfunction renderDiscoveredHardware(data) {\n    const container = document.getElementById(\'discovered-devices\');\n    if (!container) return;\n    \n    let html = \'\';\n    \n    // Kernel info banner\n    if (data.kernel_info) {\n        const ki = data.kernel_info;\n        const isCustom = ki.type === \'custom\';\n        const kernelColor = isCustom ? \'text-neon-green\' : \'text-neon-orange\';\n        const kernelLabel = isCustom ? \'Custom ARC\' : ki.type === \'official\' ? \'Official Synology\' : \'Unknown\';\n        const fanMethod = ki.has_hwmon_pwm ? \'hwmon (PWM)\' : ki.has_scemd ? \'scemd.xml (DSM API)\' : \'none\';\n        html += `<div class="bg-cyber-accent rounded-lg p-3 mb-4 text-xs">\n            <div class="flex justify-between mb-1">\n                <span class="text-gray-400">Kernel:</span>\n                <span class="${kernelColor} font-semibold">${kernelLabel}</span>\n            </div>\n            <div class="flex justify-between mb-1">\n                <span class="text-gray-400">Fan control:</span>\n                <span class="text-white">${fanMethod}</span>\n            </div>\n            ${ki.version ? `<div class="text-gray-500 mt-1 truncate" title="${escapeHtml(ki.version)}">${escapeHtml(ki.version)}</div>` : \'\'}\n        </div>`;\n    }\n    \n    // Fans section\n    if (data.fans && Object.keys(data.fans).length > 0) {\n        html += \'<h4 class="text-sm font-semibold text-neon-cyan mb-2">🌀 Fans</h4>\';\n        for (const [id, fan] of Object.entries(data.fans)) {\n            const cleanLabel = fan.label.replace(/\\s*\\(Synology-[^)]+\\)/, \'\');\n            const isDsm = fan.control_method === \'dsm_scemd\';\n            html += `\n                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">\n                    <div>\n                        <span class="text-sm text-white">${escapeHtml(cleanLabel)}</span>\n                        <span class="text-xs text-gray-500 ml-2">${fan.writable ? \'Controllable\' : \'Read-only\'}</span>\n                        ${isDsm ? \'<span class="text-xs bg-blue-900 bg-opacity-30 text-blue-400 px-2 py-0.5 rounded ml-2">DSM</span>\' : \'\'}\n                    </div>\n                    ${!isDsm ? \'<span class="text-xs bg-orange-900 bg-opacity-30 text-neon-orange px-2 py-0.5 rounded">Not calibrated</span>\' : \'\'}\n                </div>\n            `;\n        }\n    }\n    \n    // Sensors section\n    if (data.temps && Object.keys(data.temps).length > 0) {\n        html += \'<h4 class="text-sm font-semibold text-neon-green mb-2 mt-4">🌡️ Temperature Sensors</h4>\';\n        for (const [id, sensor] of Object.entries(data.temps)) {\n            html += `\n                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">\n                    <span class="text-sm text-white">${escapeHtml(sensor.label)}</span>\n                    <span class="text-sm font-mono text-neon-cyan">${formatTemp(sensor.value)}</span>\n                </div>\n            `;\n        }\n    }\n    \n    // Disks section\n    if (data.disks && Object.keys(data.disks).length > 0) {\n        html += \'<h4 class="text-sm font-semibold text-neon-purple mb-2 mt-4">💾 Storage Disks</h4>\';\n        for (const [id, disk] of Object.entries(data.disks)) {\n            html += `\n                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">\n                    <span class="text-sm text-white">${escapeHtml(disk.label)} <span class="text-xs text-gray-500">(${escapeHtml(disk.type)})</span></span>\n                    <span class="text-sm font-mono ${getTempColorClass(disk.temp)}">\n                            ${disk.standby ? t(\'sensor.sleep\', \'Sleep\') : disk.temp > 0 ? formatTemp(disk.temp) : \'--\'}\n                    </span>\n                </div>\n            `;\n        }\n    }\n    \n    container.innerHTML = html || `<p class="text-gray-500">${t(\'setup.no_hardware\', \'No hardware detected\')}</p>`;\n    \n    // Determine available control modes\n    const actionDiv = document.getElementById(\'setup-step-action\');\n    const controlSelect = document.getElementById(\'control-mode-select\');\n    const hwmonBtn = document.getElementById(\'btn-hwmon\');\n    const dsmBtn = document.getElementById(\'btn-dsm\');\n    const hint = document.getElementById(\'mode-unavailable-hint\');\n    \n    const kernelInfo = data.kernel_info || {};\n    const hasHwmon = kernelInfo.has_hwmon_pwm;\n    const hasDsm = kernelInfo.has_scemd;\n    const hasFans = data.fans && Object.keys(data.fans).length > 0;\n    \n    // Always show mode selection when fans are detected\n    if (hasFans && (hasHwmon || hasDsm)) {\n        controlSelect.classList.remove(\'hidden\');\n        document.getElementById(\'hwmon-action\').classList.add(\'hidden\');\n        document.getElementById(\'dsm-action\').classList.add(\'hidden\');\n        actionDiv.classList.remove(\'hidden\');\n        \n        // HWMon button state\n        if (hasHwmon) {\n            hwmonBtn.classList.remove(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            hwmonBtn.disabled = false;\n        } else {\n            hwmonBtn.classList.add(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            hwmonBtn.disabled = true;\n        }\n        \n        // DSM button state\n        if (hasDsm) {\n            dsmBtn.classList.remove(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            dsmBtn.disabled = false;\n        } else {\n            dsmBtn.classList.add(\'opacity-40\', \'cursor-not-allowed\', \'pointer-events-none\');\n            dsmBtn.disabled = true;\n        }\n        \n        // Show hint if one mode unavailable\n        if (hasHwmon && !hasDsm) {\n            hint.textContent = \'DSM schemes not found — only hwmon control available.\';\n            hint.classList.remove(\'hidden\');\n        } else if (!hasHwmon && hasDsm) {\n            hint.textContent = \'hwmon PWM not available on this kernel — only DSM scheme control available.\';\n            hint.classList.remove(\'hidden\');\n        } else {\n            hint.classList.add(\'hidden\');\n        }\n    } else if (hasFans && !hasHwmon && !hasDsm) {\n        // Fans but no control method\n        controlSelect.classList.add(\'hidden\');\n        document.getElementById(\'hwmon-action\').classList.add(\'hidden\');\n        document.getElementById(\'dsm-action\').classList.add(\'hidden\');\n        actionDiv.classList.remove(\'hidden\');\n        hint.textContent = \'No fan control method available.\';\n        hint.classList.remove(\'hidden\');\n    } else {\n        // No fans\n        controlSelect.classList.add(\'hidden\');\n        document.getElementById(\'hwmon-action\').classList.add(\'hidden\');\n        document.getElementById(\'dsm-action\').classList.add(\'hidden\');\n        actionDiv.classList.add(\'hidden\');\n    }\n}\n\nlet _wizardHardwareData = null;\n\nfunction selectControlMode(mode) {\n    const hwmonAction = document.getElementById(\'hwmon-action\');\n    const dsmAction = document.getElementById(\'dsm-action\');\n    const hwmonBtn = document.getElementById(\'btn-hwmon\');\n    const dsmBtn = document.getElementById(\'btn-dsm\');\n    \n    hwmonAction.classList.add(\'hidden\');\n    dsmAction.classList.add(\'hidden\');\n    \n    if (mode === \'hwmon\') {\n        hwmonBtn.classList.add(\'card-selected\');\n        dsmBtn.classList.remove(\'card-selected\');\n        hwmonAction.classList.remove(\'hidden\');\n    } else {\n        dsmBtn.classList.add(\'card-selected\');\n        hwmonBtn.classList.remove(\'card-selected\');\n        dsmAction.classList.remove(\'hidden\');\n    }\n}\n\nfunction applyDsmAndContinue() {\n    // Skip calibration, go straight to DSM scheme editor\n    fetch(\'/api/skip-calibration\', { method: \'POST\' }).catch(() => {});\n    fetch(\'/api/dsm/fan-speed\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify({ speed: 50 })\n    }).catch(() => {});\n    wizardStep = \'done\';\n    currentState = { ...currentState, initialized: true, tested: true };\n    showMainScreen();\n    setTimeout(() => showView(\'dsm-scheme\'), 500);\n}\n\nfunction skipCalibration() {\n    console.log(\'[FanControl] Skipping calibration — monitoring-only mode\');\n    fetch(\'/api/skip-calibration\', { method: \'POST\' })\n        .then(() => {})\n        .catch(() => {});\n    wizardStep = \'done\';\n    currentState = { ...currentState, initialized: true, tested: true };\n    showMainScreen();\n}\n\nfunction applyDsmFanSpeed() {\n    const speed = parseInt(document.getElementById(\'dsm-speed-slider\').value);\n    console.log(`[FanControl] Setting DSM fan speed to ${speed}%`);\n    \n    fetch(\'/api/dsm/fan-speed\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify({ speed })\n    })\n    .then(r => r.json())\n    .then(data => {\n        if (data.status === \'ok\') {\n            fetch(\'/api/skip-calibration\', { method: \'POST\' }).catch(() => {});\n            wizardStep = \'done\';\n            currentState = { ...currentState, initialized: true, tested: true };\n            showMainScreen();\n        } else {\n            alert(\'Error: \' + (data.message || \'Failed to set fan speed\'));\n        }\n    })\n    .catch(err => {\n        console.error(\'DSM fan speed error:\', err);\n        alert(\'Failed to set fan speed\');\n    });\n}\n\n// ============================================================================\n// DSM SCHEME EDITOR\n// ============================================================================\n\nlet _dsmSchemes = [];\nlet _dsmActiveScheme = null;\n\nasync function renderDsmSchemeEditor(remoteNodeId) {\n    const container = document.getElementById(\'dsm-scheme-inner\');\n    if (!container) return;\n\n    container.innerHTML = \'<div class="text-gray-500 text-center py-8">Loading DSM schemes...</div>\';\n\n    try {\n        let schemesData, activeData;\n\n        if (remoteNodeId) {\n            // Remote node — use schemes from node state\n            const node = nodesData.find(n => n.node_id === remoteNodeId);\n            if (!node) {\n                container.innerHTML = \'<div class="text-red-400 text-center py-8">Node not found</div>\';\n                return;\n            }\n            schemesData = { status: \'ok\', schemes: node.dsm_schemes || [] };\n            activeData = { active_scheme: null };\n        } else {\n            // Local server\n            const [schemesResp, activeResp] = await Promise.all([\n                fetch(\'/api/dsm/schemes\'),\n                fetch(\'/api/dsm/active\')\n            ]);\n            schemesData = await schemesResp.json();\n            activeData = await activeResp.json();\n        }\n\n        if (schemesData.status !== \'ok\') {\n            container.innerHTML = `<div class="text-red-400 text-center py-8">${schemesData.message || \'Failed to load schemes\'}</div>`;\n            return;\n        }\n\n        _dsmSchemes = schemesData.schemes || [];\n        _dsmActiveScheme = activeData.active_scheme || null;\n\n        if (_dsmSchemes.length === 0) {\n            container.innerHTML = \'<div class="text-gray-500 text-center py-8">No fan schemes found in scemd.xml</div>\';\n            return;\n        }\n\n        let html = `\n            <div class="max-w-4xl mx-auto">\n                <div class="flex items-center justify-between mb-6">\n                    <h2 class="text-xl font-bold text-white">DSM Fan Schemes</h2>\n                    <button onclick="showView(\'dashboard\')" class="text-gray-400 hover:text-white text-sm">\n                        &larr; Back to Dashboard\n                    </button>\n                </div>\n        `;\n\n        for (const scheme of _dsmSchemes) {\n            const isActive = scheme.type === _dsmActiveScheme;\n            const schemeLabel = _schemeLabel(scheme.type);\n\n            html += `\n                <div class="mb-6 bg-gray-900/50 border ${isActive ? \'border-green-500/50\' : \'border-gray-700\'} rounded-xl p-4">\n                    <div class="flex items-center justify-between mb-3">\n                        <div class="flex items-center gap-3">\n                            <h3 class="text-white font-semibold">${schemeLabel}</h3>\n                            ${isActive ? \'<span class="text-xs bg-green-900/50 text-green-400 px-2 py-0.5 rounded">Active</span>\' : \'\'}\n                            ${scheme.hibernation_speed === \'STOP\' ? \'<span class="text-xs bg-yellow-900/50 text-yellow-400 px-2 py-0.5 rounded">Hibernation: STOP</span>\' : \'\'}\n                        </div>\n                        <button onclick="applyDsmScheme(\'${escapeHtml(scheme.type)}\')"\n                                class="px-3 py-1 bg-neon-cyan/20 border border-neon-cyan/50 text-neon-cyan text-xs rounded hover:bg-neon-cyan/30 transition-all">\n                            Apply\n                        </button>\n                    </div>\n            `;\n\n            if (scheme.entries.length > 0) {\n                html += `\n                    <table class="w-full text-sm">\n                        <thead>\n                            <tr class="text-gray-400 text-xs border-b border-gray-700">\n                                <th class="text-left py-2">Sensor</th>\n                                <th class="text-left py-2">Speed</th>\n                                <th class="text-left py-2">Action</th>\n                                <th class="text-left py-2">Threshold</th>\n                                <th class="text-right py-2">Edit</th>\n                            </tr>\n                        </thead>\n                        <tbody>\n                `;\n\n                for (let i = 0; i < scheme.entries.length; i++) {\n                    const entry = scheme.entries[i];\n                    const isLast = i === scheme.entries.length - 1;\n                    const sensorLabel = entry.sensor_type === \'cpu_temperature\' ? \'CPU\' : \'Disk\';\n                    const speedDisplay = entry.fan_speed || \'--\';\n                    const actionClass = entry.action === \'SHUTDOWN\' ? \'text-red-400\' : \'text-gray-300\';\n                    const threshold = entry.threshold_temp + \'°C\';\n\n                    html += `\n                        <tr class="border-b border-gray-800 hover:bg-gray-800/30">\n                            <td class="py-2">\n                                <span class="px-1.5 py-0.5 rounded text-xs ${entry.sensor_type === \'cpu_temperature\' ? \'bg-blue-900/50 text-blue-300\' : \'bg-purple-900/50 text-purple-300\'}">${sensorLabel}</span>\n                            </td>\n                            <td class="py-2 text-white font-mono">${escapeHtml(speedDisplay)}</td>\n                            <td class="py-2 ${actionClass}">${escapeHtml(entry.action)}</td>\n                            <td class="py-2 text-gray-300">${threshold}</td>\n                            <td class="py-2 text-right">\n                                <button onclick="editDsmEntry(\'${escapeHtml(scheme.type)}\', ${i})"\n                                        class="text-gray-500 hover:text-neon-cyan text-xs px-1">✎</button>\n                            </td>\n                        </tr>\n                    `;\n                }\n\n                html += \'</tbody></table>\';\n            } else {\n                html += \'<div class="text-gray-500 text-xs py-2">No entries</div>\';\n            }\n\n            html += \'</div>\';\n        }\n\n        html += \'</div>\';\n        container.innerHTML = html;\n\n    } catch (e) {\n        container.innerHTML = `<div class="text-red-400 text-center py-8">Error loading DSM schemes: ${e.message}</div>`;\n    }\n}\n\nfunction _schemeLabel(type) {\n    const labels = {\n        \'DUAL_MODE_HIGH\': \'High Performance\',\n        \'DUAL_MODE_LOW\': \'Quiet Mode\',\n        \'FULL_SPEED\': \'Full Speed\',\n        \'STOP\': \'Stop (Fan Off)\',\n        \'FLAT\': \'Flat Config\',\n    };\n    return labels[type] || type;\n}\n\nasync function editDsmEntry(schemeType, index) {\n    const scheme = _dsmSchemes.find(s => s.type === schemeType);\n    if (!scheme || !scheme.entries[index]) return;\n\n    const entry = scheme.entries[index];\n    const newSpeed = prompt(`Fan speed % for ${entry.sensor_type} (threshold ${entry.threshold_temp}°C):`, entry.fan_speed || \'20\');\n    if (newSpeed === null) return;\n\n    const newAction = prompt(`Action (NONE or SHUTDOWN):`, entry.action || \'NONE\');\n    if (newAction === null) return;\n\n    const newThreshold = prompt(`Threshold temperature °C:`, entry.threshold_temp || \'0\');\n    if (newThreshold === null) return;\n\n    try {\n        const resp = await fetch(`/api/dsm/scheme/${schemeType}/entry/${index}`, {\n            method: \'PUT\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({\n                fan_speed_pct: parseInt(newSpeed) || 20,\n                action: newAction.toUpperCase() === \'SHUTDOWN\' ? \'SHUTDOWN\' : \'NONE\',\n                threshold_temp: parseInt(newThreshold) || 0\n            })\n        });\n        if (resp.ok) {\n            renderDsmSchemeEditor();\n        } else {\n            const err = await resp.json();\n            alert(err.message || \'Failed to update entry\');\n        }\n    } catch (e) {\n        alert(\'Error: \' + e.message);\n    }\n}\n\nasync function applyDsmScheme(schemeType) {\n    try {\n        if (_currentRemoteNodeId) {\n            // Remote node — push scheme via WebSocket\n            const node = nodesData.find(n => n.node_id === _currentRemoteNodeId);\n            const scheme = (node?.dsm_schemes || []).find(s => s.type === schemeType);\n            if (!scheme) {\n                showToast(\'Scheme not found\', \'error\');\n                return;\n            }\n            socket.emit(\'server:dsm:apply\', {\n                node_id: _currentRemoteNodeId,\n                scheme_type: schemeType,\n                entries: scheme.entries,\n            });\n            showToast(\'Scheme applied to remote agent\', \'success\');\n        } else {\n            // Local server\n            const resp = await fetch(\'/api/dsm/apply\', { method: \'POST\' });\n            const data = await resp.json();\n            if (data.status === \'ok\') {\n                showToast(\'Scheme applied successfully\', \'success\');\n            } else {\n                showToast(data.message || \'Failed to apply scheme\', \'error\');\n            }\n        }\n    } catch (e) {\n        showToast(\'Error applying scheme: \' + e.message, \'error\');\n    }\n}\n\nfunction runCalibration() {\n    console.log(\'[FanControl] Starting calibration...\');\n    \n    document.getElementById(\'calibrate-btn\').disabled = true;\n    document.getElementById(\'calibrate-loader\').classList.remove(\'hidden\');\n    wizardStep = \'calibrating\';\n    \n    document.getElementById(\'calibration-modal\').classList.remove(\'hidden\');\n    document.getElementById(\'calibration-status\').textContent = \'Starting...\';\n    document.getElementById(\'calibration-progress-bar\').style.width = \'0%\';\n    document.getElementById(\'calibration-step\').textContent = \'Step 0/11\';\n    \n    fetch(\'/api/initialize\', { method: \'POST\' })\n        .then(r => r.json())\n        .then(data => {\n            console.log(\'[FanControl] Calibration initiated:\', data);\n        })\n        .catch(err => {\n            console.error(\'Calibration error:\', err);\n            hideCalibrationModal();\n            document.getElementById(\'calibrate-btn\').disabled = false;\n            document.getElementById(\'calibrate-loader\').classList.add(\'hidden\');\n        });\n}\n\nfunction updateCalibrationModal(progress) {\n    const modal = document.getElementById(\'calibration-modal\');\n    if (modal.classList.contains(\'hidden\')) {\n        modal.classList.remove(\'hidden\');\n    }\n    \n    document.getElementById(\'calibration-status\').textContent = progress.status;\n    document.getElementById(\'calibration-step\').textContent = \n        `Step ${progress.step}/${progress.total}`;\n    \n    const pct = progress.total > 0 ? (progress.step / progress.total * 100) : 0;\n    document.getElementById(\'calibration-progress-bar\').style.width = `${pct}%`;\n}\n\nfunction hideCalibrationModal() {\n    document.getElementById(\'calibration-modal\').classList.add(\'hidden\');\n}\n\nfunction updateCalibrationParam(param, value) {\n    if (!currentFanId || !currentState || !currentState.fans) return;\n    const fan = currentState.fans[currentFanId];\n    if (!fan) return;\n\n    if (!fan.calibration) fan.calibration = {};\n\n    if (param === \'lambda\') {\n        fan.calibration.lambda = parseFloat(value);\n        document.getElementById(\'cal-lambda-val\').textContent = parseFloat(value).toFixed(1);\n    } else if (param === \'min_pwm\') {\n        fan.calibration.min_pwm = parseInt(value);\n        document.getElementById(\'cal-min-pwm-val\').textContent = value;\n    } else if (param === \'max_pwm\') {\n        fan.calibration.max_pwm = parseInt(value);\n        document.getElementById(\'cal-max-pwm-val\').textContent = value;\n    }\n\n    saveFanCalibration(currentFanId, fan.calibration);\n}\n\nfunction saveFanCalibration(fanId, calibration) {\n    fetch(\'/api/fan/\' + fanId + \'/calibration\', {\n        method: \'POST\',\n        headers: { \'Content-Type\': \'application/json\' },\n        body: JSON.stringify(calibration)\n    }).catch(err => console.error(\'Save calibration error:\', err));\n}\n\nfunction startCalibration() {\n    if (!confirm(t(\'calibration.confirm\', \'Recalibrate all fans? This takes 1-2 minutes.\'))) return;\n    \n    document.getElementById(\'calibration-modal\').classList.remove(\'hidden\');\n    document.getElementById(\'calibration-status\').textContent = \'Starting...\';\n    document.getElementById(\'calibration-progress-bar\').style.width = \'0%\';\n    document.getElementById(\'calibration-step\').textContent = \'Step 0/21\';\n    \n    fetch(\'/api/initialize\', { method: \'POST\' })\n        .catch(err => console.error(\'Calibration error:\', err));\n}\n\n// ============================================================================\n// SCHEDULE GRID\n// ============================================================================\n\nconst DAYS = [\'mon\', \'tue\', \'wed\', \'thu\', \'fri\', \'sat\', \'sun\'];\nconst DAY_LABELS = [\'Mon\', \'Tue\', \'Wed\', \'Thu\', \'Fri\', \'Sat\', \'Sun\'];\nconst DAY_KEYS = [\'days.mon\', \'days.tue\', \'days.wed\', \'days.thu\', \'days.fri\', \'days.sat\', \'days.sun\'];\n\nfunction tDay(idx) {\n    return t(DAY_KEYS[idx], DAY_LABELS[idx]);\n}\n\nfunction renderScheduleGrid() {\n    const container = document.getElementById(\'schedule-grid\');\n    if (!container) return;\n    \n    const fan = currentState?.fans?.[currentFanId];\n    const schedule = fan?.schedule || [];\n    scheduleData = {};\n    schedule.forEach(item => {\n        const key = `${item.day}_${item.time_start}`;\n        scheduleData[key] = item;\n    });\n    \n    // Build color map for cells\n    const colorMap = {};\n    const groups = {};\n    schedule.forEach(item => {\n        const key = ruleKey(item);\n        if (!groups[key]) groups[key] = [];\n        groups[key].push(item);\n    });\n    const groupKeys = Object.keys(groups);\n    groupKeys.forEach((gk, idx) => {\n        const color = getRuleColor(idx);\n        groups[gk].forEach(item => {\n            const cellKey = `${item.day}_${item.time_start}`;\n            colorMap[cellKey] = color;\n        });\n    });\n    \n    let html = \'<table class="border-collapse" style="border-spacing: 1px;">\';\n    \n    // Header row: empty corner + 24 hours\n    html += \'<tr><th class="w-12 h-5"></th>\';\n    for (let h = 0; h < 24; h++) {\n        html += `<th class="h-5 px-0 text-[10px] text-gray-500 font-normal" style="width:${SCHEDULE_CELL_SIZE}px">${h}</th>`;\n    }\n    html += \'</tr>\';\n    \n    // Day rows\n    for (let d = 0; d < DAYS.length; d++) {\n        const day = DAYS[d];\n        html += `<tr><td class="w-12 h-5 text-[10px] text-gray-400 font-semibold pr-1 text-right align-middle">${tDay(d)}</td>`;\n        \n        for (let h = 0; h < 24; h++) {\n            const timeStr = String(h).padStart(2, \'0\') + \':00\';\n            const key = `${day}_${timeStr}`;\n            const item = scheduleData[key];\n            \n            let bgStyle = \'background:#1f2937\';\n            if (item) {\n                const cm = colorMap[key];\n                if (cm) {\n                    bgStyle = `background:${cm.hex}`;\n                } else {\n                    bgStyle = item.mode === \'auto\' ? \'background:#15803d\' : item.mode === \'manual\' ? \'background:#c2410c\' : \'background:#991b1b\';\n                }\n            }\n            \n            html += `<td class="cursor-pointer schedule-cell transition-colors duration-75"\n                         data-day="${day}" data-hour="${h}"\n                         onmousedown="onScheduleMouseDown(event,\'${day}\',${h})"\n                         onmouseenter="onScheduleMouseEnter(event,\'${day}\',${h})"\n                         title="${tDay(d)} ${timeStr}${item ? \' [\' + t(\'mode.\' + item.mode, item.mode) + \']\' : \'\'}"\n                         style="width:${SCHEDULE_CELL_SIZE}px;height:${SCHEDULE_CELL_SIZE}px;${bgStyle}"></td>`;\n        }\n        html += \'</tr>\';\n    }\n    \n    html += \'</table>\';\n    container.innerHTML = html;\n    \n    renderScheduleRules();\n    validateSchedule();\n}\n\nconst RULE_COLORS = [\n    { hex: \'#15803d\', dot: \'#4ade80\', text: \'#86efac\' },\n    { hex: \'#c2410c\', dot: \'#fb923c\', text: \'#fdba74\' },\n    { hex: \'#991b1b\', dot: \'#f87171\', text: \'#fca5a5\' },\n    { hex: \'#1d4ed8\', dot: \'#60a5fa\', text: \'#93c5fd\' },\n    { hex: \'#7e22ce\', dot: \'#c084fc\', text: \'#d8b4fe\' },\n    { hex: \'#a16207\', dot: \'#facc15\', text: \'#fde047\' },\n    { hex: \'#be185d\', dot: \'#f472b6\', text: \'#f9a8d4\' },\n    { hex: \'#0f766e\', dot: \'#2dd4bf\', text: \'#5eead4\' },\n];\n\nfunction getRuleColor(idx) {\n    if (idx < RULE_COLORS.length) return RULE_COLORS[idx];\n    // Generate colors via HSL for groups beyond 8\n    const hue = (idx * 137) % 360;\n    const hex = `hsl(${hue}, 60%, 35%)`;\n    const dot = `hsl(${hue}, 70%, 65%)`;\n    const text = `hsl(${hue}, 70%, 80%)`;\n    return { hex, dot, text };\n}\n\nfunction ruleKey(item) {\n    return JSON.stringify({\n        mode: item.mode,\n        target_temp: item.target_temp,\n        speed_pct: item.speed_pct,\n        sensors: [...(item.sensors || [])].sort(),\n        sensor_mode: item.sensor_mode\n    });\n}\n\nfunction renderScheduleRules() {\n    const container = document.getElementById(\'schedule-rules\');\n    if (!container) return;\n    \n    const fan = currentState?.fans?.[currentFanId];\n    const schedule = fan?.schedule || [];\n    \n    if (schedule.length === 0) {\n        container.innerHTML = `<p class="text-xs text-gray-500 italic">${t(\'schedule.no_rules\', \'No rules configured\')}</p>`;\n        return;\n    }\n    \n    // Group by identical settings\n    const groups = {};\n    schedule.forEach(item => {\n        const key = ruleKey(item);\n        if (!groups[key]) groups[key] = { item, cells: [] };\n        groups[key].cells.push(item);\n    });\n    \n    const groupList = Object.values(groups);\n    \n    let html = \'<div class="space-y-1">\';\n    groupList.forEach((group, gIdx) => {\n        const color = getRuleColor(gIdx);\n        const item = group.item;\n        const cells = group.cells;\n        \n        let settings = \'\';\n        if (item.mode === \'auto\') {\n            const sensorNames = (item.sensors || []).map(s => {\n                const sen = allSensors.find(x => x.id === s);\n                return sen ? sen.label : s.split(\':\').pop();\n            });\n            settings = `${formatTemp(item.target_temp || 31)}`;\n            if (sensorNames.length > 0) {\n                settings += ` · ${sensorNames.join(\', \')}`;\n                if (item.sensor_mode && sensorNames.length > 1) {\n                    settings += ` (${item.sensor_mode})`;\n                }\n            }\n        } else if (item.mode === \'manual\') {\n            settings = `${item.speed_pct ?? 50}%`;\n        } else {\n            settings = \'off\';\n        }\n        \n        // Group cells by day to build sub-periods\n        const byDay = {};\n        cells.forEach(c => {\n            if (!byDay[c.day]) byDay[c.day] = [];\n            byDay[c.day].push(c);\n        });\n        \n        // Build contiguous time ranges per day\n        const subPeriods = [];\n        for (const [day, dayCells] of Object.entries(byDay)) {\n            const hours = dayCells.map(c => parseInt(c.time_start)).sort((a, b) => a - b);\n            let start = hours[0], prev = hours[0];\n            for (let i = 1; i < hours.length; i++) {\n                if (hours[i] === prev + 1) {\n                    prev = hours[i];\n                } else {\n                    subPeriods.push({ day, from: start, to: prev });\n                    start = hours[i];\n                    prev = hours[i];\n                }\n            }\n            subPeriods.push({ day, from: start, to: prev });\n        }\n        subPeriods.sort((a, b) => {\n            const d = DAYS.indexOf(a.day) - DAYS.indexOf(b.day);\n            return d !== 0 ? d : a.from - b.from;\n        });\n        \n        const modeIcon = item.mode === \'auto\' ? \'🌡️\' : item.mode === \'manual\' ? \'🎮\' : \'⏻\';\n        \n        html += `\n            <div class="bg-cyber-accent rounded-lg overflow-hidden">\n                <div class="flex items-center gap-2 px-3 py-2">\n                    <span class="w-3 h-3 rounded-full flex-shrink-0" style="background:${color.dot}"></span>\n                    <span class="text-xs flex-shrink-0">${modeIcon}</span>\n                    <div class="flex-1 min-w-0 cursor-pointer" onclick="toggleRuleGroup(${gIdx})">\n                        <span class="text-xs font-semibold" style="color:${color.text}">${escapeHtml(settings)}</span>\n                        <span class="text-[10px] text-gray-500 ml-2">${cells.length}h</span>\n                    </div>\n                    <button onclick="editRuleGroup(${gIdx}); event.stopPropagation()" \n                            class="text-[10px] text-gray-400 hover:text-neon-cyan px-1.5 py-0.5 rounded hover:bg-cyber-bg transition-all flex-shrink-0">\n                        Edit\n                    </button>\n                    <button onclick="deleteRuleGroup(${gIdx}); event.stopPropagation()" \n                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-bg transition-all flex-shrink-0">\n                        Del\n                    </button>\n                    <span id="rule-chevron-${gIdx}" class="text-[10px] text-gray-500 transition-transform duration-200 cursor-pointer" onclick="toggleRuleGroup(${gIdx})">▸</span>\n                </div>\n                <div id="rule-subperiods-${gIdx}" class="hidden border-t border-gray-700">\n        `;\n        \n        subPeriods.forEach((sp, sIdx) => {\n            const dayLabel = tDay(DAYS.indexOf(sp.day));\n            const fromStr = String(sp.from).padStart(2, \'0\') + \':00\';\n            const toStr = String(sp.to + 1).padStart(2, \'0\') + \':00\';\n            \n            html += `\n                <div class="flex items-center gap-2 px-3 py-1.5 hover:bg-cyber-bg transition-all">\n                    <span class="w-2 h-2 rounded-full flex-shrink-0" style="background:${color.dot}; opacity:0.6"></span>\n                    <span class="text-[11px] text-gray-300 flex-1">${dayLabel} ${fromStr}–${toStr}</span>\n                    <button onclick="editSinglePeriod(\'${sp.day}\', ${sp.from}, ${sp.to}); event.stopPropagation()" \n                            class="text-[10px] text-gray-400 hover:text-neon-cyan px-1.5 py-0.5 rounded hover:bg-cyber-accent transition-all">\n                        Edit\n                    </button>\n                    <button onclick="deleteSinglePeriod(\'${sp.day}\', ${sp.from}, ${sp.to}); event.stopPropagation()" \n                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-accent transition-all">\n                        Del\n                    </button>\n                </div>\n            `;\n        });\n        \n        html += `\n                </div>\n            </div>\n        `;\n    });\n    html += \'</div>\';\n    container.innerHTML = html;\n    container._groups = groupList;\n    \n    // Restore expanded state\n    expandedRuleGroups.forEach(idx => {\n        const el = document.getElementById(`rule-subperiods-${idx}`);\n        const chevron = document.getElementById(`rule-chevron-${idx}`);\n        if (el) {\n            el.classList.remove(\'hidden\');\n            if (chevron) chevron.textContent = \'▾\';\n        }\n    });\n}\n\nfunction toggleRuleGroup(idx) {\n    const el = document.getElementById(`rule-subperiods-${idx}`);\n    const chevron = document.getElementById(`rule-chevron-${idx}`);\n    if (!el) return;\n    el.classList.toggle(\'hidden\');\n    if (el.classList.contains(\'hidden\')) {\n        expandedRuleGroups.delete(idx);\n    } else {\n        expandedRuleGroups.add(idx);\n    }\n    if (chevron) chevron.textContent = el.classList.contains(\'hidden\') ? \'▸\' : \'▾\';\n}\n\nfunction editSinglePeriod(day, fromHour, toHour) {\n    const cells = [];\n    for (let h = fromHour; h <= toHour; h++) {\n        cells.push({ day, hour: h });\n    }\n    openScheduleEditor(cells);\n}\n\nfunction deleteSinglePeriod(day, fromHour, toHour) {\n    for (let h = fromHour; h <= toHour; h++) {\n        const key = `${day}_${String(h).padStart(2, \'0\')}:00`;\n        delete scheduleData[key];\n    }\n    applyScheduleToFan();\n}\n\nfunction editRuleGroup(idx) {\n    const container = document.getElementById(\'schedule-rules\');\n    const group = container._groups[idx];\n    if (!group) return;\n    const cells = group.cells.map(c => ({ day: c.day, hour: parseInt(c.time_start) }));\n    openScheduleEditor(cells);\n}\n\nfunction deleteRuleGroup(idx) {\n    const container = document.getElementById(\'schedule-rules\');\n    const group = container._groups[idx];\n    if (!group) return;\n    group.cells.forEach(cell => {\n        const key = `${cell.day}_${cell.time_start}`;\n        delete scheduleData[key];\n    });\n    expandedRuleGroups.delete(idx);\n    applyScheduleToFan();\n}\n\nfunction onScheduleMouseDown(e, day, hour) {\n    e.preventDefault();\n    isDraggingSchedule = true;\n    dragStartCell = { day, hour };\n    scheduleSelection = [{ day, hour }];\n    highlightSelection();\n}\n\nfunction onScheduleMouseEnter(e, day, hour) {\n    if (!isDraggingSchedule || !dragStartCell) return;\n    \n    const startH = dragStartCell.hour;\n    const startD = DAYS.indexOf(dragStartCell.day);\n    const endD = DAYS.indexOf(day);\n    const minD = Math.min(startD, endD);\n    const maxD = Math.max(startD, endD);\n    \n    scheduleSelection = [];\n    \n    if (minD === maxD) {\n        // Same day: select hour range\n        const hFrom = Math.min(startH, hour);\n        const hTo = Math.max(startH, hour);\n        for (let h = hFrom; h <= hTo; h++) {\n            scheduleSelection.push({ day: DAYS[minD], hour: h });\n        }\n    } else {\n        // Cross-day: select ALL hours on each day in range\n        for (let d = minD; d <= maxD; d++) {\n            for (let h = 0; h < 24; h++) {\n                scheduleSelection.push({ day: DAYS[d], hour: h });\n            }\n        }\n    }\n    highlightSelection();\n}\n\nfunction highlightSelection() {\n    clearHighlight();\n    for (const cell of scheduleSelection) {\n        const el = document.querySelector(`.schedule-cell[data-day="${cell.day}"][data-hour="${cell.hour}"]`);\n        if (el) {\n            el.style.outline = \'2px solid #00f0ff\';\n            el.style.outlineOffset = \'-1px\';\n            el.style.zIndex = \'1\';\n        }\n    }\n}\n\nfunction clearHighlight() {\n    document.querySelectorAll(\'.schedule-cell\').forEach(el => {\n        el.style.outline = \'\';\n        el.style.outlineOffset = \'\';\n        el.style.zIndex = \'\';\n    });\n}\n\ndocument.addEventListener(\'mouseup\', () => {\n    if (!isDraggingSchedule) return;\n    isDraggingSchedule = false;\n    \n    if (scheduleSelection.length === 1) {\n        openScheduleEditor([scheduleSelection[0]]);\n    } else if (scheduleSelection.length > 1) {\n        openScheduleEditor([...scheduleSelection]);\n    }\n    scheduleSelection = [];\n    clearHighlight();\n});\n\n// ============================================================================\n// SCHEDULE EDITOR\n// ============================================================================\n\nfunction openScheduleEditor(cells) {\n    editingCells = cells;\n    scheduleEditorSensors = [];\n    \n    const editor = document.getElementById(\'schedule-editor\');\n    editor.classList.remove(\'hidden\');\n    \n    // Build human-readable period description\n    document.getElementById(\'schedule-editor-cells\').textContent = describeCells(cells);\n    \n    // Get existing data from first cell\n    const key = `${cells[0].day}_${String(cells[0].hour).padStart(2, \'0\')}:00`;\n    const existing = scheduleData[key];\n    \n    if (existing) {\n        setScheduleMode(existing.mode);\n        document.getElementById(\'sched-target-temp\').value = existing.target_temp || 31;\n        document.getElementById(\'sched-speed-slider\').value = existing.speed_pct ?? 50;\n        document.getElementById(\'sched-speed-value\').textContent = `${existing.speed_pct ?? 50}%`;\n        scheduleEditorSensors = [...(existing.sensors || [])];\n        if (existing.sensor_mode) setScheduleSensorMode(existing.sensor_mode);\n    } else {\n        setScheduleMode(\'auto\');\n        document.getElementById(\'sched-target-temp\').value = 31;\n        document.getElementById(\'sched-speed-slider\').value = 50;\n        document.getElementById(\'sched-speed-value\').textContent = \'50%\';\n        \n        // Auto-fill sensors from first existing schedule item\n        const fan = currentState?.fans?.[currentFanId];\n        const schedule = fan?.schedule || [];\n        if (schedule.length > 0) {\n            const first = schedule[0];\n            scheduleEditorSensors = [...(first.sensors || [])];\n            if (first.sensor_mode) setScheduleSensorMode(first.sensor_mode);\n        }\n    }\n    \n    updateScheduleEditorSensors();\n}\n\nfunction setScheduleMode(mode) {\n    const modes = [\'auto\', \'manual\', \'off\'];\n    \n    modes.forEach(m => {\n        const btn = document.getElementById(`sched-btn-${m}`);\n        if (btn) btn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${m === mode ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n    \n    document.getElementById(\'sched-auto-settings\').classList.toggle(\'hidden\', mode !== \'auto\');\n    document.getElementById(\'sched-manual-settings\').classList.toggle(\'hidden\', mode !== \'manual\');\n}\n\nfunction setScheduleSensorMode(sensorMode) {\n    const modes = [\'max\', \'min\', \'avg\'];\n    \n    modes.forEach(m => {\n        const btn = document.getElementById(`sched-btn-sensor-${m}`);\n        if (btn) btn.className = `flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border ${m === sensorMode ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n}\n\nfunction updateScheduleEditorSensors() {\n    const container = document.getElementById(\'sched-sensor-tags\');\n    if (!container) return;\n    \n    if (scheduleEditorSensors.length === 0) {\n        container.innerHTML = `<span class="text-xs text-gray-500 italic">${t(\'editor.no_sensors\', \'No sensors assigned\')}</span>`;\n        document.getElementById(\'sched-sensor-mode-section\').classList.add(\'hidden\');\n        return;\n    }\n    \n    container.innerHTML = scheduleEditorSensors.map(s => {\n        const sensor = allSensors.find(x => x.id === s);\n        const label = sensor ? sensor.label : s;\n        return `\n            <span class="inline-flex items-center gap-1 bg-cyber-accent text-gray-300 text-xs px-2 py-1 rounded-full">\n                ${escapeHtml(label)}\n                <button onclick="removeScheduleSensor(\'${escapeHtml(s)}\')" class="text-neon-red hover:text-red-400 ml-1">&times;</button>\n            </span>\n        `;\n    }).join(\'\');\n    \n    document.getElementById(\'sched-sensor-mode-section\').classList.toggle(\'hidden\', scheduleEditorSensors.length <= 1);\n}\n\nfunction removeScheduleSensor(sensorId) {\n    scheduleEditorSensors = scheduleEditorSensors.filter(s => s !== sensorId);\n    updateScheduleEditorSensors();\n}\n\nfunction toggleScheduleSensorPopup() {\n    const popup = document.getElementById(\'sensor-popup\');\n    const list = document.getElementById(\'sensor-popup-list\');\n    if (!popup || !list) return;\n    \n    if (popup.classList.contains(\'hidden\')) {\n        const groups = {};\n        allSensors.forEach(s => {\n            if (!groups[s.group]) groups[s.group] = [];\n            groups[s.group].push(s);\n        });\n        \n        let html = \'\';\n        for (const [group, sensors] of Object.entries(groups)) {\n            html += `<div class="text-xs font-semibold text-gray-500 uppercase mb-2">${t(group, group)}</div>`;\n            sensors.forEach(s => {\n                const checked = scheduleEditorSensors.includes(s.id);\n                html += `\n                    <label class="flex items-center gap-2 py-1.5 cursor-pointer hover:bg-cyber-accent rounded px-2">\n                        <input type="checkbox" value="${escapeHtml(s.id)}" ${checked ? \'checked\' : \'\'} \n                               class="accent-neon-purple">\n                        <span class="text-sm text-gray-300">${escapeHtml(s.label)}</span>\n                        <span class="text-xs text-gray-500 ml-auto">\n                            ${s.standby ? t(\'sensor.sleep\', \'Sleep\') : formatTemp(s.temp)}\n                        </span>\n                    </label>\n                `;\n            });\n        }\n        \n        list.innerHTML = html;\n        popup.classList.remove(\'hidden\');\n        \n        // Override close behavior for schedule context\n        popup._scheduleMode = true;\n    } else {\n        // Collect checked sensors\n        const checked = popup.querySelectorAll(\'input[type=checkbox]:checked\');\n        scheduleEditorSensors = Array.from(checked).map(cb => cb.value);\n        updateScheduleEditorSensors();\n        popup.classList.add(\'hidden\');\n        popup._scheduleMode = false;\n    }\n}\n\nfunction saveScheduleEdit() {\n    const mode = document.querySelector(\'#sched-btn-auto.bg-neon-cyan\') ? \'auto\'\n        : document.querySelector(\'#sched-btn-manual.bg-neon-cyan\') ? \'manual\' : \'off\';\n    \n    const newItems = editingCells.map(cell => {\n        const key = `${cell.day}_${String(cell.hour).padStart(2, \'0\')}:00`;\n        const item = {\n            day: cell.day,\n            time_start: String(cell.hour).padStart(2, \'0\') + \':00\',\n            time_end: String(cell.hour).padStart(2, \'0\') + \':59\',\n            mode: mode\n        };\n        \n        if (mode === \'auto\') {\n            item.target_temp = parseInt(document.getElementById(\'sched-target-temp\').value) || 31;\n            item.sensors = [...scheduleEditorSensors];\n            const activeSensorMode = document.querySelector(\'#sched-btn-sensor-max.bg-neon-cyan\') ? \'max\'\n                : document.querySelector(\'#sched-btn-sensor-min.bg-neon-cyan\') ? \'min\' : \'avg\';\n            item.sensor_mode = activeSensorMode;\n        } else if (mode === \'manual\') {\n            item.speed_pct = parseInt(document.getElementById(\'sched-speed-slider\').value) || 50;\n        }\n        \n        scheduleData[key] = item;\n        return item;\n    });\n    \n    closeScheduleEditor();\n    applyScheduleToFan();\n}\n\nfunction deleteScheduleEdit() {\n    for (const cell of editingCells) {\n        const key = `${cell.day}_${String(cell.hour).padStart(2, \'0\')}:00`;\n        delete scheduleData[key];\n    }\n    closeScheduleEditor();\n    applyScheduleToFan();\n}\n\nfunction closeScheduleEditor() {\n    document.getElementById(\'schedule-editor\').classList.add(\'hidden\');\n    editingCells = [];\n}\n\nfunction clearSchedule() {\n    scheduleData = {};\n    applyScheduleToFan();\n}\n\nfunction fillScheduleDefaults() {\n    const fan = currentState?.fans?.[currentFanId];\n    const defaultSensors = fan?.sensors || [];\n    const defaultSensorMode = fan?.sensor_mode || \'max\';\n    const defaultTemp = fan?.target_temp || 31;\n    \n    for (const day of DAYS) {\n        for (let hour = 0; hour < 24; hour++) {\n            const key = `${day}_${String(hour).padStart(2, \'0\')}:00`;\n            if (!scheduleData[key]) {\n                scheduleData[key] = {\n                    day: day,\n                    time_start: String(hour).padStart(2, \'0\') + \':00\',\n                    time_end: String(hour).padStart(2, \'0\') + \':59\',\n                    mode: \'auto\',\n                    target_temp: defaultTemp,\n                    sensors: [...defaultSensors],\n                    sensor_mode: defaultSensorMode\n                };\n            }\n        }\n    }\n    applyScheduleToFan();\n}\n\nfunction applyScheduleToFan() {\n    const schedule = Object.values(scheduleData);\n    \n    // Update local state immediately so render sees new data\n    if (currentState?.fans?.[currentFanId]) {\n        currentState.fans[currentFanId].schedule = schedule;\n    }\n    \n    sendControl({\n        action: \'set_fan_config\',\n        fan: currentFanId,\n        schedule: schedule\n    });\n    renderScheduleGrid();\n}\n\nfunction describeCells(cells) {\n    if (cells.length === 0) return \'\';\n    if (cells.length === 1) {\n        return `${tDay(DAYS.indexOf(cells[0].day))} ${String(cells[0].hour).padStart(2, \'0\')}:00`;\n    }\n    \n    const days = [...new Set(cells.map(c => c.day))].sort((a, b) => DAYS.indexOf(a) - DAYS.indexOf(b));\n    const hours = [...new Set(cells.map(c => c.hour))].sort((a, b) => a - b);\n    \n    let dayStr = \'\';\n    if (days.length === 7) {\n        dayStr = t(\'schedule.every_day\', \'Every day\');\n    } else if (days.length === 5 && !days.includes(\'sat\') && !days.includes(\'sun\')) {\n        dayStr = t(\'schedule.weekdays\', \'Weekdays\');\n    } else if (days.length === 2 && days.includes(\'sat\') && days.includes(\'sun\')) {\n        dayStr = t(\'schedule.weekends\', \'Weekends\');\n    } else if (days.length <= 3) {\n        dayStr = days.map(d => tDay(DAYS.indexOf(d))).join(\', \');\n    } else {\n        dayStr = `${days.length} days`;\n    }\n    \n    if (hours.length === 24) {\n        return `${dayStr}, 00:00-23:59`;\n    }\n    \n    const minH = String(Math.min(...hours)).padStart(2, \'0\');\n    const maxH = String(Math.max(...hours) + 1).padStart(2, \'0\');\n    return `${dayStr}, ${minH}:00-${maxH.length > 5 ? \'00:00 next day\' : maxH + \':00\'}`;\n}\n\nfunction validateSchedule() {\n    const fan = currentState?.fans?.[currentFanId];\n    const schedule = fan?.schedule || [];\n    const coverage = document.getElementById(\'schedule-coverage\');\n    const warning = document.getElementById(\'schedule-incomplete-warning\');\n    const detail = document.getElementById(\'schedule-incomplete-detail\');\n    \n    if (!coverage) return;\n    \n    const total = 7 * 24;\n    const filled = schedule.length;\n    const pct = Math.round((filled / total) * 100);\n    \n    coverage.textContent = `${filled}/${total} (${pct}%)`;\n    coverage.className = pct === 100 ? \'text-xs text-neon-green\' : \'text-xs text-neon-orange\';\n    \n    if (pct < 100) {\n        const emptyDays = [];\n        for (let i = 0; i < DAYS.length; i++) {\n            const dayHours = schedule.filter(s => s.day === DAYS[i]).length;\n            if (dayHours < 24) emptyDays.push(tDay(i));\n        }\n        warning.classList.remove(\'hidden\');\n        detail.textContent = `${t(\'schedule.missing\', \'Missing\')}: ${emptyDays.join(\', \')}. ${t(\'schedule.empty_hours\', \'Empty hours = fan off.\')}`;\n    } else {\n        warning.classList.add(\'hidden\');\n    }\n}\n\n// ============================================================================\n// SETTINGS & LANGUAGE\n// ============================================================================\n\nfunction toggleSettings() {\n    const overlay = document.getElementById(\'settings-overlay\');\n    const panel = document.getElementById(\'settings-panel\');\n    if (!overlay || !panel) return;\n    \n    const isOpen = !panel.classList.contains(\'hidden\');\n    if (isOpen) {\n        overlay.classList.add(\'hidden\');\n        panel.classList.add(\'hidden\');\n    } else {\n        overlay.classList.remove(\'hidden\');\n        panel.classList.remove(\'hidden\');\n        updateLangButtons();\n        updateSettingsUI();\n        autoCheckUpdate();\n    }\n}\n\nfunction updateLangButtons() {\n    const enBtn = document.getElementById(\'lang-btn-en\');\n    const ruBtn = document.getElementById(\'lang-btn-ru\');\n    const setupEn = document.getElementById(\'setup-lang-en\');\n    const setupRu = document.getElementById(\'setup-lang-ru\');\n    \n    if (enBtn) enBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${currentLang === \'en\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (ruBtn) ruBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${currentLang === \'ru\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (setupEn) setupEn.className = `text-xs px-2 py-1 rounded border transition-all ${currentLang === \'en\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (setupRu) setupRu.className = `text-xs px-2 py-1 rounded border transition-all ${currentLang === \'ru\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    \n    updateSettingsUI();\n}\n\nfunction updateSettingsUI() {\n    const s = getSettings();\n    \n    // Temperature unit buttons\n    const celsiusBtn = document.getElementById(\'unit-btn-celsius\');\n    const fahrBtn = document.getElementById(\'unit-btn-fahrenheit\');\n    if (celsiusBtn) celsiusBtn.className = `flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${s.tempUnit === \'celsius\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    if (fahrBtn) fahrBtn.className = `flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${s.tempUnit === \'fahrenheit\' ? BTN_ACTIVE : BTN_INACTIVE}`;\n    \n    // Refresh interval buttons\n    [0, 1000, 5000].forEach(v => {\n        const btn = document.getElementById(`refresh-btn-${v}`);\n        if (btn) btn.className = `flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border ${s.refreshInterval === v ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n    \n    // Compact mode toggle\n    const compactBtn = document.getElementById(\'compact-toggle\');\n    if (compactBtn) {\n        compactBtn.className = s.compactMode\n            ? `w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${BTN_ACTIVE}`\n            : `w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${BTN_INACTIVE}`;\n        compactBtn.querySelector(\'span\').textContent = s.compactMode ? t(\'settings.on\', \'On\') : t(\'settings.off\', \'Off\');\n    }\n    \n    // Apply compact mode to body\n    document.body.classList.toggle(\'compact-mode\', s.compactMode);\n    \n    // Auto-update interval buttons\n    [0, 21600000, 43200000, 86400000].forEach(v => {\n        const btn = document.getElementById(`autoupd-btn-${v}`);\n        if (btn) btn.className = `flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border ${s.autoUpdateCheck === v ? BTN_ACTIVE : BTN_INACTIVE}`;\n    });\n}\n\nfunction setTempUnit(unit) {\n    saveSettings({ tempUnit: unit });\n    updateSettingsUI();\n    // Re-render current data\n    if (currentState) updateUI(currentState);\n}\n\nfunction setRefreshInterval(ms) {\n    saveSettings({ refreshInterval: ms });\n    updateSettingsUI();\n}\n\nfunction toggleCompactMode() {\n    const s = getSettings();\n    saveSettings({ compactMode: !s.compactMode });\n    updateSettingsUI();\n}\n\nfunction setAutoUpdateInterval(ms) {\n    saveSettings({ autoUpdateCheck: ms });\n    updateSettingsUI();\n    scheduleAutoUpdate();\n}\n\nlet _autoUpdateTimer = null;\nfunction scheduleAutoUpdate() {\n    if (_autoUpdateTimer) { clearInterval(_autoUpdateTimer); _autoUpdateTimer = null; }\n    const ms = getSettings().autoUpdateCheck;\n    if (ms > 0) {\n        _autoUpdateTimer = setInterval(() => { _updateChecked = false; autoCheckUpdate(); }, ms);\n    }\n}\n\nasync function checkForUpdates() {\n    const btn = document.getElementById(\'update-check-btn\');\n    const result = document.getElementById(\'update-result\');\n    const applyBtn = document.getElementById(\'update-apply-btn\');\n    \n    if (btn) {\n        btn.disabled = true;\n        btn.querySelector(\'span\').textContent = t(\'settings.checking\', \'Checking...\');\n    }\n    if (result) result.classList.add(\'hidden\');\n    if (applyBtn) {\n        applyBtn.classList.add(\'hidden\');\n        applyBtn.disabled = true;\n        applyBtn.className = \'hidden w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-500 border-gray-700 mt-2\';\n    }\n    \n    try {\n        const resp = await fetch(\'/api/update/check\');\n        const data = await resp.json();\n        \n        const badge = document.getElementById(\'update-badge\');\n        \n        if (data.has_update) {\n            if (badge) badge.classList.remove(\'hidden\');\n            if (result) {\n                result.classList.remove(\'hidden\');\n                result.className = \'text-xs mt-2 p-3 rounded-lg bg-green-900 bg-opacity-20 border border-green-800 text-neon-green\';\n                result.innerHTML = `\n                    <div class="font-semibold mb-2">${t(\'settings.update_available\', \'Update available\')}</div>\n                    <div class="flex justify-between mb-1"><span class="text-gray-400">${t(\'settings.current_version\', \'Current\')}:</span><span class="font-mono">${escapeHtml(data.current_version || \'?\')}</span></div>\n                    <div class="flex justify-between mb-1"><span class="text-gray-400">${t(\'settings.new_version\', \'New\')}:</span><span class="font-mono text-white font-bold">${escapeHtml(data.remote_version || \'?\')}</span></div>\n                    ${data.commit_message ? `<div class="mt-2 pt-2 border-t border-green-800 text-gray-300">${escapeHtml(data.commit_message)}</div>` : \'\'}`;\n            }\n            if (applyBtn) {\n                applyBtn.classList.remove(\'hidden\');\n                applyBtn.disabled = false;\n                applyBtn.className = \'w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border mt-2 bg-green-900 bg-opacity-30 text-neon-green border-green-700 hover:bg-opacity-50\';\n            }\n        } else {\n            if (badge) badge.classList.add(\'hidden\');\n            if (result) {\n                result.classList.remove(\'hidden\');\n                result.className = \'text-xs mt-2 p-3 rounded-lg bg-cyber-accent border border-cyber-accent text-gray-400\';\n                result.textContent = t(\'settings.up_to_date\', \'System is up to date\');\n            }\n        }\n        return data.has_update;\n    } catch (e) {\n        if (result) {\n            result.classList.remove(\'hidden\');\n            result.className = \'text-xs mt-2 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red\';\n            result.textContent = t(\'settings.update_error\', \'Failed to check for updates\');\n        }\n        return false;\n    } finally {\n        if (btn) {\n            btn.disabled = false;\n            btn.querySelector(\'span\').textContent = t(\'settings.check_update\', \'Check for Updates\');\n        }\n    }\n}\n\nlet _updateChecked = false;\n\nfunction copyAgentToken() {\n    const token = document.getElementById(\'agent-token-value\').textContent;\n    if (token && navigator.clipboard) {\n        navigator.clipboard.writeText(token).then(() => showToast(\'Token copied!\', \'success\'));\n    }\n}\n\nfunction openUpdateModal() {\n    const modal = document.getElementById(\'update-modal\');\n    const steps = document.getElementById(\'update-modal-steps\');\n    const progress = document.getElementById(\'update-modal-progress\');\n    const result = document.getElementById(\'update-modal-result\');\n    const applyBtn = document.getElementById(\'update-modal-apply\');\n    const closeBtn = document.getElementById(\'update-modal-close\');\n    \n    steps.innerHTML = `\n        <div id="upd-step-pull" class="flex items-center gap-3 text-sm">\n            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-pull-icon">1</span>\n            <span class="text-gray-300">${t(\'settings.step_pull\', \'Pulling latest code...\')}</span>\n        </div>\n        <div id="upd-step-restart" class="flex items-center gap-3 text-sm opacity-40">\n            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-restart-icon">2</span>\n            <span class="text-gray-300">${t(\'settings.step_restart\', \'Restarting container...\')}</span>\n        </div>\n    `;\n    \n    progress.classList.add(\'hidden\');\n    result.classList.add(\'hidden\');\n    applyBtn.disabled = false;\n    applyBtn.classList.remove(\'hidden\');\n    closeBtn.classList.remove(\'hidden\');\n    \n    modal.classList.remove(\'hidden\');\n}\n\nfunction closeUpdateModal() {\n    document.getElementById(\'update-modal\').classList.add(\'hidden\');\n}\n\nfunction setStepState(step, state) {\n    const el = document.getElementById(`upd-step-${step}`);\n    const icon = document.getElementById(`upd-step-${step}-icon`);\n    if (!el || !icon) return;\n    \n    el.classList.remove(\'opacity-40\');\n    \n    if (state === \'active\') {\n        icon.className = \'w-5 h-5 rounded-full border-2 border-neon-cyan flex-shrink-0 flex items-center justify-center text-[10px] text-neon-cyan animate-pulse\';\n        icon.innerHTML = \'⟳\';\n    } else if (state === \'done\') {\n        icon.className = \'w-5 h-5 rounded-full bg-neon-green flex-shrink-0 flex items-center justify-center text-[10px] text-black\';\n        icon.innerHTML = \'✓\';\n    } else if (state === \'error\') {\n        icon.className = \'w-5 h-5 rounded-full bg-neon-red flex-shrink-0 flex items-center justify-center text-[10px] text-white\';\n        icon.innerHTML = \'✕\';\n    }\n}\n\nasync function startUpdate() {\n    const applyBtn = document.getElementById(\'update-modal-apply\');\n    const progress = document.getElementById(\'update-modal-progress\');\n    const bar = document.getElementById(\'update-modal-bar\');\n    const result = document.getElementById(\'update-modal-result\');\n    const closeBtn = document.getElementById(\'update-modal-close\');\n    \n    applyBtn.classList.add(\'hidden\');\n    closeBtn.classList.add(\'hidden\');\n    progress.classList.remove(\'hidden\');\n    bar.style.width = \'10%\';\n    \n    // Step 1: Git pull\n    setStepState(\'pull\', \'active\');\n    bar.style.width = \'20%\';\n    \n    try {\n        const resp = await fetch(\'/api/update/apply\', { method: \'POST\' });\n        const data = await resp.json();\n        \n        if (data.status === \'error\') {\n            setStepState(\'pull\', \'error\');\n            bar.style.width = \'100%\';\n            bar.className = \'bg-neon-red h-2 rounded-full transition-all duration-500\';\n            result.classList.remove(\'hidden\');\n            result.className = \'text-sm mb-4 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red\';\n            result.textContent = data.message || t(\'settings.update_failed\', \'Update failed\');\n            applyBtn.classList.remove(\'hidden\');\n            applyBtn.disabled = false;\n            closeBtn.classList.remove(\'hidden\');\n            return;\n        }\n        \n        // Step 1 done\n        setStepState(\'pull\', \'done\');\n        bar.style.width = \'50%\';\n        \n        // Step 2: Restart (entrypoint syncs code from /repo)\n        setStepState(\'restart\', \'active\');\n        bar.style.width = \'80%\';\n        \n        // Show restart notification\n        result.classList.remove(\'hidden\');\n        result.className = \'text-sm mb-4 p-3 rounded-lg bg-green-900 bg-opacity-20 border border-green-800 text-neon-green\';\n        result.innerHTML = `\n            <div class="font-semibold mb-1">${t(\'settings.update_success\', \'Update complete!\')}</div>\n            <div class="text-gray-400">${t(\'settings.restart_notice\', \'Container is restarting. Page will reload in 10 seconds...\')}</div>\n        `;\n        \n        bar.style.width = \'100%\';\n        setStepState(\'restart\', \'done\');\n        \n        // Reload after delay\n        setTimeout(() => { window.location.reload(); }, RELOAD_DELAY);\n        \n    } catch (e) {\n        setStepState(\'pull\', \'error\');\n        bar.style.width = \'100%\';\n        bar.className = \'bg-neon-red h-2 rounded-full transition-all duration-500\';\n        result.classList.remove(\'hidden\');\n        result.className = \'text-sm mb-4 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red\';\n        result.textContent = t(\'settings.update_error\', \'Failed to apply update\');\n        applyBtn.classList.remove(\'hidden\');\n        applyBtn.disabled = false;\n        closeBtn.classList.remove(\'hidden\');\n    }\n}\nasync function autoCheckUpdate() {\n    if (_updateChecked) return;\n    _updateChecked = true;\n    await checkForUpdates();\n}\n\nasync function switchLanguage(code) {\n    if (code === currentLang) return;\n    \n    const success = await loadLang(code);\n    if (success) {\n        updateLangButtons();\n        // Save to server config\n        fetch(\'/api/language\', {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ language: code })\n        }).catch(() => {});\n        \n        // Re-render dynamic content\n        if (currentFanId) {\n            const fan = currentState?.fans?.[currentFanId];\n            if (fan) updateInspector(fan);\n        }\n    }\n}\n\n// ============================================================================\n// INITIALIZATION\n// ============================================================================\n\ndocument.addEventListener(\'DOMContentLoaded\', async () => {\n    console.log(\'[FanControl] Neon Cyberpunk Edition initialized\');\n\n    window.addEventListener(\'beforeunload\', () => {\n        if (_dashboardSaveTimer) {\n            clearTimeout(_dashboardSaveTimer);\n            saveDashboardToServer();\n        }\n    });\n\n    // Load language\n    await loadLang(currentLang);\n    updateLangButtons();\n    updateSettingsUI();\n    \n    // Click outside to close sensor popup (stop propagation to avoid closing editor underneath)\n    document.getElementById(\'sensor-popup\')?.addEventListener(\'click\', function(e) {\n        e.stopPropagation();\n        if (e.target === this) {\n            closeSensorPopupForContext();\n        }\n    });\n    \n    // Click outside to close schedule editor (only if sensor popup is not open)\n    document.getElementById(\'schedule-editor\')?.addEventListener(\'click\', function(e) {\n        if (e.target === this && document.getElementById(\'sensor-popup\')?.classList.contains(\'hidden\')) {\n            closeScheduleEditor();\n        }\n    });\n    \n    // Schedule speed slider\n    document.getElementById(\'sched-speed-slider\')?.addEventListener(\'input\', (e) => {\n        document.getElementById(\'sched-speed-value\').textContent = `${e.target.value}%`;\n    });\n    \n    // Initial chart load (after short delay to ensure DOM is ready)\n    setTimeout(updateChart, 2000);\n    \n    // Auto-check for updates in background (5s after load)\n    setTimeout(() => autoCheckUpdate(), 5000);\n    \n    // Schedule periodic auto-check\n    scheduleAutoUpdate();\n    \n    // Load nodes for multi-node dashboard\n    loadNodes();\n});\n\n// ============================================================================\n// NODE MANAGEMENT (Multi-node Dashboard)\n// ============================================================================\n\nlet currentView = \'dashboard\';\nlet selectedNodeId = null;\nlet nodesData = [];\n\nasync function loadNodes() {\n    try {\n        const resp = await fetch(\'/api/nodes\');\n        nodesData = await resp.json();\n        buildServerTree();\n        renderNodesOverview();\n    } catch (e) {\n        console.error(\'[FanControl] Failed to load nodes:\', e);\n    }\n}\n\n// renderNodeSidebar removed — nodes are rendered via buildServerTree/renderRemoteNodeTree\n\nfunction renderNodesOverview() {\n    const container = document.getElementById(\'nodes-grid-inner\');\n    if (!container) return;\n    \n    let html = \'\';\n    for (const node of nodesData) {\n        const telemetry = node.telemetry || {};\n        const fans = telemetry.fans || {};\n        const temps = telemetry.temp_sensors || {};\n        const tempValues = Object.values(temps).map(s => (s && s.value) || 0);\n        const maxTemp = tempValues.length > 0 ? Math.max(...tempValues) : 0;\n        const totalRPM = Object.values(fans).reduce((sum, f) => sum + ((f && f.rpm) || 0), 0);\n        \n        html += `\n            <div class="bg-gray-900/50 border border-gray-700 rounded-xl p-4 cursor-pointer hover:border-cyan-500/50 transition-all"\n                 onclick="selectNode(\'${escapeHtml(node.node_id)}\')">\n                <div class="flex items-center justify-between mb-3">\n                    <h3 class="text-white font-semibold">${escapeHtml(node.name)}</h3>\n                    <div class="flex items-center gap-2">\n                        <span class="text-xs ${node.status === \'online\' ? \'text-green-400\' : \'text-gray-500\'}">${node.status}</span>\n                        ${node.control_mode === \'manual\' ? \'<span class="text-yellow-400 text-xs">&#9888; Manual</span>\' : \'\'}\n                    </div>\n                </div>\n                <div class="grid grid-cols-2 gap-2 text-sm">\n                    <div class="text-gray-400">${t(\'nodes.max_temp\', \'Max Temp\')}</div>\n                    <div class="text-white text-right">${maxTemp}&deg;C</div>\n                    <div class="text-gray-400">${t(\'nodes.total_rpm\', \'Total RPM\')}</div>\n                    <div class="text-white text-right">${totalRPM}</div>\n                    <div class="text-gray-400">${t(\'nodes.fans\', \'Fans\')}</div>\n                    <div class="text-white text-right">${Object.keys(fans).length}</div>\n                </div>\n            </div>\n        `;\n    }\n    \n    if (nodesData.length === 0) {\n        html = `<div class="text-gray-500 text-center py-8 col-span-2">${t(\'nodes.no_nodes\', \'No nodes connected. Add a node to get started.\')}</div>`;\n    }\n    \n    container.innerHTML = html;\n}\n\nfunction selectNode(nodeId) {\n    selectedNodeId = nodeId;\n    currentView = \'node-detail\';\n    showView(\'node-detail\');\n    loadNodeDetail(nodeId);\n}\n\nasync function loadNodeDetail(nodeId) {\n    try {\n        const resp = await fetch(`/api/nodes/${nodeId}`);\n        const node = await resp.json();\n        renderNodeDetail(node);\n    } catch (e) {\n        console.error(\'[FanControl] Failed to load node detail:\', e);\n    }\n}\n\nfunction renderNodeDetail(node) {\n    const container = document.getElementById(\'node-detail-inner\');\n    if (!container) return;\n    \n    const telemetry = node.telemetry || {};\n    const fans = telemetry.fans || {};\n    const temps = telemetry.temp_sensors || {};\n    \n    let fansHtml = \'\';\n    for (const [id, fan] of Object.entries(fans)) {\n        const pwm = (fan && fan.pwm_value) || 0;\n        fansHtml += `\n            <div class="bg-gray-800/50 rounded-lg p-3">\n                <div class="flex justify-between text-sm">\n                    <span class="text-gray-400">${escapeHtml(id)}</span>\n                    <span class="text-white">${(fan && fan.rpm) || 0} RPM</span>\n                </div>\n                <div class="mt-1 bg-gray-700 rounded-full h-2">\n                    <div class="bg-cyan-500 h-2 rounded-full" style="width: ${pwm / 255 * 100}%"></div>\n                </div>\n            </div>\n        `;\n    }\n    \n    let tempsHtml = \'\';\n    for (const [id, temp] of Object.entries(temps)) {\n        tempsHtml += `\n            <div class="flex justify-between text-sm">\n                <span class="text-gray-400">${escapeHtml(id)}</span>\n                <span class="text-white">${(temp && temp.value) || 0}&deg;C</span>\n            </div>\n        `;\n    }\n    \n    container.innerHTML = `\n        <div class="flex items-center justify-between mb-6">\n            <div>\n                <h2 class="text-xl font-bold text-white">${escapeHtml(node.name)}</h2>\n                <p class="text-gray-400 text-sm">${node.node_id} &middot; ${node.status} &middot; ${node.control_mode || \'auto\'} mode</p>\n            </div>\n            <div class="flex gap-2">\n                <button onclick="deleteNode(\'${escapeHtml(node.node_id)}\')"\n                    class="px-3 py-1 bg-red-900/30 border border-red-500/30 rounded text-red-400 text-sm hover:bg-red-900/50 transition-all">\n                    ${t(\'nodes.delete\', \'Delete\')}\n                </button>\n                <button onclick="showView(\'nodes\')"\n                    class="px-3 py-1 bg-gray-800 border border-gray-600 rounded text-gray-300 text-sm hover:bg-gray-700 transition-all">\n                    ${t(\'nodes.back\', \'Back\')}\n                </button>\n            </div>\n        </div>\n        \n        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">\n            <div>\n                <h3 class="text-white font-semibold mb-3">${t(\'nodes.fans\', \'Fans\')}</h3>\n                <div class="space-y-2">${fansHtml || \'<div class="text-gray-500 text-sm">No fan data</div>\'}</div>\n            </div>\n            <div>\n                <h3 class="text-white font-semibold mb-3">${t(\'node.temperatures\', \'Temperatures\')}</h3>\n                <div class="space-y-2">${tempsHtml || \'<div class="text-gray-500 text-sm">No temperature data</div>\'}</div>\n            </div>\n        </div>\n    `;\n}\n\nfunction showView(view) {\n    currentView = view;\n\n    const canvas = document.getElementById(\'dashboard-canvas-container\');\n    const inspector = document.getElementById(\'inspector-container\');\n    const addBtn = document.getElementById(\'dashboard-add-btn\');\n    const groupBtn = document.getElementById(\'dashboard-group-btn\');\n    const nodesGrid = document.getElementById(\'nodes-grid\');\n    const nodeDetail = document.getElementById(\'node-detail-content\');\n    const dsmScheme = document.getElementById(\'dsm-scheme-container\');\n\n    // Hide all views first\n    [canvas, inspector, nodesGrid, nodeDetail, dsmScheme].forEach(el => {\n        if (el) el.classList.add(\'hidden\');\n    });\n    [addBtn, groupBtn].forEach(el => {\n        if (el) el.classList.add(\'hidden\');\n    });\n\n    // Show the requested view\n    if (view === \'dashboard\') {\n        if (canvas) canvas.classList.remove(\'hidden\');\n        if (addBtn) addBtn.classList.remove(\'hidden\');\n        if (groupBtn) groupBtn.classList.remove(\'hidden\');\n    } else if (view === \'inspector\') {\n        if (inspector) inspector.classList.remove(\'hidden\');\n    } else if (view === \'nodes\') {\n        if (nodesGrid) nodesGrid.classList.remove(\'hidden\');\n        renderNodesOverview();\n    } else if (view === \'node-detail\') {\n        if (nodeDetail) nodeDetail.classList.remove(\'hidden\');\n    } else if (view === \'dsm-scheme\') {\n        if (dsmScheme) dsmScheme.classList.remove(\'hidden\');\n        renderDsmSchemeEditor();\n    }\n\n    // Update nav button styles\n    const dashBtn = document.getElementById(\'nav-dashboard-btn\');\n    if (dashBtn) {\n        if (view === \'dashboard\') {\n            dashBtn.classList.add(\'text-neon-cyan\', \'border-neon-cyan\');\n            dashBtn.classList.remove(\'text-gray-500\', \'border-transparent\');\n        } else {\n            dashBtn.classList.remove(\'text-neon-cyan\', \'border-neon-cyan\');\n            dashBtn.classList.add(\'text-gray-500\', \'border-transparent\');\n        }\n    }\n}\n\nasync function addNode() {\n    const nameInput = document.getElementById(\'new-node-name\');\n    const ipInput = document.getElementById(\'new-node-ip\');\n    const name = nameInput?.value?.trim();\n    const ip = ipInput?.value?.trim();\n    if (!name && !ip) return;\n\n    try {\n        let resp;\n        if (ip) {\n            // Add by IP — probes agent automatically\n            resp = await fetch(\'/api/nodes/add-by-ip\', {\n                method: \'POST\',\n                headers: { \'Content-Type\': \'application/json\' },\n                body: JSON.stringify({ name: name || ip, ip })\n            });\n        } else {\n            resp = await fetch(\'/api/nodes\', {\n                method: \'POST\',\n                headers: { \'Content-Type\': \'application/json\' },\n                body: JSON.stringify({ name })\n            });\n        }\n        if (resp.ok) {\n            nameInput.value = \'\';\n            ipInput.value = \'\';\n            loadNodes();\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            showToast(err.error || \'Failed to add node\', \'error\');\n        }\n    } catch (e) {\n        console.error(\'[FanControl] Failed to add node:\', e);\n        showToast(\'Failed to add node: \' + e.message, \'error\');\n    }\n}\n\nasync function deleteNode(nodeId) {\n    if (!confirm(t(\'nodes.confirm_delete\', \'Delete this node?\'))) return;\n    try {\n        const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, { method: \'DELETE\' });\n        if (resp.ok) {\n            if (selectedNodeId === nodeId) {\n                selectedNodeId = null;\n                showView(\'nodes\');\n            }\n            loadNodes();\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            console.error(\'[FanControl] Delete failed:\', resp.status, err);\n            showToast(`Delete failed: ${err.error || resp.status}`, \'error\');\n        }\n    } catch (e) {\n        console.error(\'[FanControl] Failed to delete node:\', e);\n        showToast(\'Delete failed: \' + e.message, \'error\');\n    }\n}\n\nfunction showNodeSettings(nodeId) {\n    const node = nodesData.find(n => n.node_id === nodeId);\n    if (!node) return;\n    document.getElementById(\'node-settings-id\').value = nodeId;\n    document.getElementById(\'node-settings-name\').value = node.name || \'\';\n    document.getElementById(\'node-settings-ip\').value = node.ip || \'\';\n    document.getElementById(\'node-settings-port\').value = node.port || 5059;\n    document.getElementById(\'node-settings-modal\').classList.remove(\'hidden\');\n}\n\nfunction hideNodeSettings() {\n    document.getElementById(\'node-settings-modal\').classList.add(\'hidden\');\n}\n\nfunction openServerNameEdit() {\n    const input = document.getElementById(\'server-name-input\');\n    input.value = currentState.server_name || \'\';\n    document.getElementById(\'server-name-modal\').classList.remove(\'hidden\');\n    input.focus();\n    input.select();\n}\n\nfunction hideServerNameModal() {\n    document.getElementById(\'server-name-modal\').classList.add(\'hidden\');\n}\n\nasync function saveServerName() {\n    const name = document.getElementById(\'server-name-input\').value.trim();\n    if (!name) { showToast(\'Name required\', \'error\'); return; }\n\n    try {\n        const resp = await fetch(\'/api/server-name\', {\n            method: \'PUT\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ name })\n        });\n        if (resp.ok) {\n            hideServerNameModal();\n            currentState.server_name = name;\n            showToast(\'Server renamed\', \'success\');\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            showToast(err.error || \'Save failed\', \'error\');\n        }\n    } catch (e) {\n        showToast(\'Save failed: \' + e.message, \'error\');\n    }\n}\n\nasync function saveNodeSettings() {\n    const nodeId = document.getElementById(\'node-settings-id\').value;\n    const name = document.getElementById(\'node-settings-name\').value.trim();\n    const ip = document.getElementById(\'node-settings-ip\').value.trim();\n    const port = parseInt(document.getElementById(\'node-settings-port\').value) || 5059;\n    if (!name) { showToast(\'Name required\', \'error\'); return; }\n\n    try {\n        const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {\n            method: \'PUT\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ name, ip, port })\n        });\n        if (resp.ok) {\n            hideNodeSettings();\n            loadNodes();\n        } else {\n            const err = await resp.json().catch(() => ({}));\n            showToast(err.error || \'Save failed\', \'error\');\n        }\n    } catch (e) {\n        showToast(\'Save failed: \' + e.message, \'error\');\n    }\n}\n\nasync function scanForAgents() {\n    const btn = document.getElementById(\'scan-agents-btn\');\n    const list = document.getElementById(\'discovered-agents-list\');\n    if (!list) return;\n\n    btn.disabled = true;\n    btn.textContent = \'...\';\n    list.classList.remove(\'hidden\');\n    list.innerHTML = \'<div class="text-gray-500 text-xs py-1">Scanning network...</div>\';\n\n    try {\n        const [discoverResp, discoveredResp, subnetResp] = await Promise.all([\n            fetch(\'/api/nodes/discover\'),\n            fetch(\'/api/discovered\'),\n            fetch(\'/api/nodes/scan-subnet\', { method: \'POST\', headers: { \'Content-Type\': \'application/json\' }, body: \'{}\' }),\n        ]);\n\n        const scanResults = await discoverResp.json();\n        const pendingAgents = await discoveredResp.json();\n        const subnetResults = await subnetResp.json();\n\n        // Merge results: SSDP + subnet scan, deduplicate by IP\n        const merged = new Map();\n        for (const a of (Array.isArray(scanResults) ? scanResults : [])) {\n            if (a.ip) merged.set(a.ip, a);\n        }\n        for (const a of (Array.isArray(subnetResults) ? subnetResults : [])) {\n            if (a.ip && !merged.has(a.ip)) merged.set(a.ip, a);\n        }\n        const allAgents = [...merged.values()];\n\n        let html = \'\';\n\n        // Show merged scan results\n        if (allAgents.length > 0) {\n            for (const agent of allAgents) {\n                const label = agent.already_registered\n                    ? `<span class="text-neon-green">online</span> ${escapeHtml(agent.name || agent.node_id)}`\n                    : escapeHtml(agent.name || agent.node_id);\n                const btnLabel = agent.already_registered ? \'Refresh\' : \'+ Add\';\n                const onclick = agent.already_registered\n                    ? `loadNodes(); showToast(\'Node refreshed\', \'success\')`\n                    : `acceptDiscoveredAgent(\'${escapeHtml(agent.node_id)}\')`;\n                html += `\n                    <div class="flex items-center justify-between bg-gray-800/50 rounded p-1.5 text-xs">\n                        <span class="text-white truncate">${label} <span class="text-gray-500">${escapeHtml(agent.ip || \'\')}</span></span>\n                        <button onclick="${onclick}" class="text-neon-cyan hover:text-cyan-300 px-1">${btnLabel}</button>\n                    </div>\n                `;\n            }\n        }\n\n        // Also show pending discovered agents\n        if (pendingAgents && pendingAgents.length > 0) {\n            for (const agent of pendingAgents) {\n                if (!allAgents.find(a => a.node_id === agent.node_id)) {\n                    html += `\n                        <div class="flex items-center justify-between bg-gray-800/50 rounded p-1.5 text-xs">\n                            <span class="text-white truncate">${escapeHtml(agent.name || agent.node_id)} <span class="text-gray-500">${escapeHtml(agent.ip || \'\')}</span></span>\n                            <button onclick="acceptDiscoveredAgent(\'${escapeHtml(agent.node_id)}\')" class="text-neon-cyan hover:text-cyan-300 px-1">+ Add</button>\n                        </div>\n                    `;\n                }\n            }\n        }\n\n        if (!html) {\n            html = \'<div class="text-gray-500 text-xs py-1">\';\n            html += \'No agents found. Use IP field below to add manually.\';\n            html += \'</div>\';\n        }\n\n        list.innerHTML = html;\n    } catch (e) {\n        list.innerHTML = `<div class="text-red-400 text-xs py-1">Scan failed: ${e.message}</div>`;\n    }\n\n    btn.disabled = false;\n    btn.textContent = \'\\uD83D\\uDD0D\';\n}\n\nsocket.on(\'node:update\', (data) => {\n    const idx = nodesData.findIndex(n => n.node_id === data.node_id);\n    if (idx >= 0) {\n        nodesData[idx].status = data.status;\n        nodesData[idx].name = data.name || nodesData[idx].name;\n        if (data.ip) nodesData[idx].ip = data.ip;\n        if (data.control_mode) nodesData[idx].control_mode = data.control_mode;\n    }\n    buildServerTree();\n    renderNodesOverview();\n});\n\nsocket.on(\'node:telemetry\', (data) => {\n    const idx = nodesData.findIndex(n => n.node_id === data.node_id);\n    if (idx >= 0) {\n        nodesData[idx].telemetry = data.telemetry;\n    } else {\n        // Node not yet in nodesData — fetch fresh list\n        loadNodes();\n        return;\n    }\n    buildServerTree();\n    renderNodesOverview();\n    if (selectedNodeId === data.node_id && currentView === \'node-detail\') {\n        loadNodeDetail(data.node_id);\n    }\n});\n\n// ============================================================================\n// CONFIG SYNC & CONFLICT MANAGEMENT\n// ============================================================================\n\nlet conflictData = null;\n\nsocket.on(\'node:conflict\', (data) => {\n    console.warn(\'[FanControl] Node conflict:\', data);\n    conflictData = data;\n    const idx = nodesData.findIndex(n => n.node_id === data.node_id);\n    if (idx >= 0) {\n        nodesData[idx].control_mode = \'manual\';\n    }\n    buildServerTree();\n    showConflictModal(data);\n});\n\nsocket.on(\'node:mode_changed\', (data) => {\n    const idx = nodesData.findIndex(n => n.node_id === data.node_id);\n    if (idx >= 0) {\n        nodesData[idx].control_mode = data.mode;\n    }\n    buildServerTree();\n    renderNodesOverview();\n    if (data.mode === \'manual\') {\n        showManualModeWarning(data.node_id);\n    }\n});\n\nfunction showToast(message, type = \'info\', actions = []) {\n    const container = document.getElementById(\'toast-container\');\n    if (!container) return;\n\n    const toast = document.createElement(\'div\');\n    toast.className = `toast toast-${type}`;\n\n    let html = `<span>${escapeHtml(message)}</span>`;\n    actions.forEach(action => {\n        html += `<button class="toast-btn ${action.secondary ? \'toast-btn-secondary\' : \'\'}" onclick="${action.onclick}">${escapeHtml(action.label)}</button>`;\n    });\n\n    toast.innerHTML = html;\n    container.appendChild(toast);\n\n    setTimeout(() => {\n        toast.style.opacity = \'0\';\n        toast.style.transform = \'translateX(100px)\';\n        setTimeout(() => toast.remove(), 300);\n    }, 8000);\n}\n\nsocket.on(\'node:discovered\', (data) => {\n    if (data.already_connected) {\n        // Agent auto-registered via WebSocket — already connected, just notify\n        showToast(`Agent connected: ${data.name} (${data.ip})`, \'success\');\n        loadNodes();\n    } else {\n        // SSDP-discovered agent — check if dismissed\n        const dismissed = JSON.parse(localStorage.getItem(\'fc_dismissed_agents\') || \'[]\');\n        if (dismissed.includes(data.node_id)) return;\n        const msg = `Новый агент: ${data.name} (${data.ip})`;\n        showToast(msg, \'warning\', [\n            { label: \'Добавить\', onclick: `acceptDiscoveredAgent(\'${data.node_id}\')` },\n            { label: \'Не напоминать\', onclick: `dismissAgentForever(\'${data.node_id}\')`, secondary: true },\n        ]);\n    }\n});\n\nsocket.on(\'server:name_changed\', (data) => {\n    if (data.name) {\n        currentState.server_name = data.name;\n        buildServerTree();\n    }\n});\n\nasync function acceptDiscoveredAgent(nodeId) {\n    try {\n        const resp = await fetch(`/api/discovered/${nodeId}/accept`, { method: \'POST\' });\n        if (resp.ok) {\n            showToast(\'Агент добавлен! Переподключение...\', \'success\');\n            loadNodes();\n        }\n    } catch (e) {\n        showToast(\'Ошибка добавления агента\', \'error\');\n    }\n}\n\nfunction dismissAgentForever(nodeId) {\n    const dismissed = JSON.parse(localStorage.getItem(\'fc_dismissed_agents\') || \'[]\');\n    if (!dismissed.includes(nodeId)) {\n        dismissed.push(nodeId);\n        localStorage.setItem(\'fc_dismissed_agents\', JSON.stringify(dismissed));\n    }\n    showToast(\'Больше не напоминать\', \'success\');\n}\n\nfunction showConflictModal(data) {\n    const modal = document.getElementById(\'conflict-modal\');\n    if (!modal) return;\n\n    document.getElementById(\'conflict-node-name\').textContent = data.name || data.node_id;\n\n    const serverFans = (data.server_config || {}).fans || {};\n    let serverHtml = \'\';\n    for (const [id, fan] of Object.entries(serverFans)) {\n        serverHtml += `<div class="text-sm"><span class="text-gray-400">${escapeHtml(id)}:</span> <span class="text-white">mode=${fan.mode}, temp=${fan.target_temp}°C</span></div>`;\n    }\n    document.getElementById(\'conflict-server-config\').innerHTML = serverHtml || `<div class="text-gray-500 text-sm">${t(\'conflict.no_config\', \'No config\')}</div>`;\n\n    const agentFans = (data.agent_config || {}).fans || {};\n    let agentHtml = \'\';\n    for (const [id, fan] of Object.entries(agentFans)) {\n        agentHtml += `<div class="text-sm"><span class="text-gray-400">${escapeHtml(id)}:</span> <span class="text-white">mode=${fan.mode}, temp=${fan.target_temp}°C</span></div>`;\n    }\n    document.getElementById(\'conflict-agent-config\').innerHTML = agentHtml || `<div class="text-gray-500 text-sm">${t(\'conflict.no_config\', \'No config\')}</div>`;\n\n    modal.classList.remove(\'hidden\');\n}\n\nfunction hideConflictModal() {\n    document.getElementById(\'conflict-modal\')?.classList.add(\'hidden\');\n    conflictData = null;\n}\n\nasync function applyServerConfig() {\n    if (!conflictData) return;\n    try {\n        await fetch(`/api/nodes/${conflictData.node_id}/config`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ config: conflictData.server_config })\n        });\n        hideConflictModal();\n    } catch (e) {\n        console.error(\'Failed to apply server config:\', e);\n    }\n}\n\nasync function keepAgentConfig() {\n    if (!conflictData) return;\n    try {\n        await fetch(`/api/nodes/${conflictData.node_id}/config`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ config: conflictData.agent_config })\n        });\n        hideConflictModal();\n    } catch (e) {\n        console.error(\'Failed to keep agent config:\', e);\n    }\n}\n\nfunction showManualModeWarning(nodeId) {\n    const node = nodesData.find(n => n.node_id === nodeId);\n    if (!node) return;\n    const warning = document.getElementById(\'manual-mode-warning\');\n    if (!warning) return;\n\n    document.getElementById(\'manual-mode-node-name\').textContent = node.name || nodeId;\n    document.getElementById(\'manual-mode-switch-btn\').onclick = () => switchToServerMode(nodeId);\n    warning.classList.remove(\'hidden\');\n}\n\nfunction hideManualModeWarning() {\n    document.getElementById(\'manual-mode-warning\')?.classList.add(\'hidden\');\n}\n\nasync function switchToServerMode(nodeId) {\n    try {\n        await fetch(`/api/nodes/${nodeId}/mode`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ mode: \'server\' })\n        });\n        hideManualModeWarning();\n    } catch (e) {\n        console.error(\'Failed to switch mode:\', e);\n    }\n}\n\nasync function pushConfigToNode(nodeId) {\n    try {\n        const resp = await fetch(\'/api/state\');\n        const state = await resp.json();\n        await fetch(`/api/nodes/${nodeId}/config`, {\n            method: \'POST\',\n            headers: { \'Content-Type\': \'application/json\' },\n            body: JSON.stringify({ config: { fans: state.fans } })\n        });\n    } catch (e) {\n        console.error(\'Failed to push config:\', e);\n    }\n}\n\nconsole.log(\'[FanControl] main.js loaded successfully\');\n\n// ============================================================================\n// DEBUG PANEL\n// ============================================================================\n\nlet _debugOpen = false;\n\nfunction toggleDebugPanel() {\n    _debugOpen = !_debugOpen;\n    const panel = document.getElementById(\'debug-panel\');\n    const btn = document.querySelector(\'[title="Debug"]\');\n    if (_debugOpen) {\n        panel.classList.remove(\'hidden\');\n        btn.classList.add(\'hidden\');\n        renderDebugPanel();\n    } else {\n        panel.classList.add(\'hidden\');\n        btn.classList.remove(\'hidden\');\n    }\n}\n\nfunction renderDebugPanel() {\n    if (!_debugOpen) return;\n    const el = document.getElementById(\'debug-content\');\n    if (!el) return;\n\n    const saved = getPickerCards();\n    const fans = currentState?.fans || {};\n    const temps = currentState?.temp_sensors || {};\n    const disks = currentState?.hdd_sensors || {};\n\n    let html = \'\';\n\n    // Connection status\n    html += `<div class="mb-3"><span class="text-neon-cyan">Socket.IO:</span> ${socket?.connected ? \'✅ connected\' : \'❌ disconnected\'}</div>`;\n\n    // Cards\n    html += `<div class="mb-3"><span class="text-neon-cyan">Cards (${saved.length}):</span></div>`;\n    for (const card of saved) {\n        const el2 = document.querySelector(`[data-card-id="${card.id}"]`);\n        const w = el2 ? el2.offsetWidth : 0;\n        const h = el2 ? el2.offsetHeight : 0;\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-gray-500">${card.type}</span> `;\n        html += `<span class="text-white">${card.label || card.id.slice(-8)}</span> `;\n        html += `<span class="text-yellow-400">${card.colSpan || 3}x${card.rowSpan || 1}</span> `;\n        html += `<span class="text-gray-600">pos(${card.col},${card.row})</span> `;\n        html += `<span class="text-gray-600">${w}x${h}px</span>`;\n        if (card.lockSize) html += ` <span class="text-red-400">🔒</span>`;\n        html += `</div>`;\n    }\n\n    // Fans\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Fans (${Object.keys(fans).length}):</span></div>`;\n    for (const [id, fan] of Object.entries(fans)) {\n        const spark = getSparkline(`fan:local:${id}`);\n        const last = spark.length ? spark[spark.length - 1] : \'--\';\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-white">${fan.label || id.slice(-8)}</span> `;\n        html += `<span class="text-cyan-400">${fan.rpm || 0} RPM</span> `;\n        html += `<span class="text-gray-600">mode=${fan.mode}</span> `;\n        html += `<span class="text-gray-600">spark=${last}</span>`;\n        html += `</div>`;\n    }\n\n    // Temps\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Temps (${Object.keys(temps).length}):</span></div>`;\n    for (const [id, sensor] of Object.entries(temps)) {\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-white">${sensor.label || id}</span> `;\n        html += `<span class="text-green-400">${sensor.value || \'--\'}°C</span>`;\n        html += `</div>`;\n    }\n\n    // Disks\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Disks (${Object.keys(disks).length}):</span></div>`;\n    for (const [id, disk] of Object.entries(disks)) {\n        html += `<div class="ml-2 mb-1">`;\n        html += `<span class="text-white">${disk.name || id}</span> `;\n        html += `<span class="text-purple-400">${disk.temp || \'--\'}°C</span>`;\n        html += `</div>`;\n    }\n\n    // Sparkline stats\n    const sparkKeys = Object.keys(_sparklineHistory);\n    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Sparklines (${sparkKeys.length}):</span></div>`;\n    for (const key of sparkKeys.slice(0, 10)) {\n        const data = _sparklineHistory[key];\n        html += `<div class="ml-2 mb-1"><span class="text-gray-500">${key}:</span> <span class="text-gray-400">${data.length} pts, last=${data[data.length-1]}</span></div>`;\n    }\n\n    el.innerHTML = html;\n    requestAnimationFrame(() => { if (_debugOpen) renderDebugPanel(); });\n}'

TEMPLATE_LANG_EN = '{\n  "app.title": "FanControl",\n  "app.subtitle": "Neon Cyberpunk Edition",\n\n  "setup.heading": "Initial System Setup",\n  "setup.description": "No configuration found. System needs to scan available data buses to automatically detect fans and temperature sensors.",\n  "setup.scan_btn": "Start Hardware Scan",\n  "setup.scanning": "Scanning sysfs bus and querying smartctl...",\n  "setup.results_title": "Hardware Detected",\n  "setup.calibrate_hint": "To complete setup, fans must be calibrated. This takes about 1-2 minutes.",\n  "setup.calibrate_btn": "Start Fan Calibration",\n  "setup.calibrating": "Calibrating: determining PWM/RPM curves...",\n  "setup.controllable": "Controllable",\n  "setup.readonly": "Read-only",\n  "setup.not_calibrated": "Not calibrated",\n  "setup.fans_header": "Fans",\n  "setup.sensors_header": "Temperature Sensors",\n  "setup.disks_header": "Storage Disks",\n  "setup.loading_fans": "Loading fans...",\n  "setup.no_fans": "No fans detected",\n  "setup.no_disks": "No disks detected",\n  "setup.no_hardware": "No hardware detected",\n  "setup.calibrate_btn_short": "Recalibrate",\n\n  "header.synced": "Synced",\n  "header.storage": "Storage",\n  "header.settings": "Settings",\n\n  "inspector.select": "Select a device",\n  "inspector.hint": "Click on a fan to inspect",\n  "inspector.fallback_id": "ID: unknown",\n  "inspector.fan_speed": "Fan Speed",\n  "inspector.status": "Status",\n  "inspector.mode": "Mode",\n  "inspector.hint_detail": "to view controls and analytics",\n  "inspector.fan_name": "Fan Name",\n\n  "mode.manual": "Manual",\n  "mode.auto": "Auto",\n\n  "status.nominal": "nominal",\n  "status.warning": "warning",\n  "status.critical": "critical",\n  "status.failsafe": "failsafe",\n  "status.standby": "standby",\n  "status.inverted": "inverted",\n  "status.no_sensor": "no_sensor",\n  "status.not_tested": "not tested",\n  "status.calibrating": "calibrating",\n  "status.not_connected": "not connected",\n  "status.normal": "normal",\n  "status.manual": "manual",\n  "status.off": "off",\n  "status.fixed": "fixed",\n  "status.low": "low",\n\n  "schedule.weekly": "Weekly Schedule",\n  "schedule.incomplete": "Schedule incomplete",\n  "schedule.no_sensor_title": "No sensors assigned",\n  "schedule.no_sensor_hint": "Assign sensors in the first schedule cell, or globally below.",\n  "schedule.legend_auto": "Auto",\n  "schedule.legend_manual": "Manual",\n  "schedule.legend_off": "Off",\n  "schedule.legend_empty": "Empty",\n  "schedule.clear_all": "Clear All",\n  "schedule.fill_auto": "Fill Empty with Auto",\n  "schedule.no_rules": "No rules configured",\n  "schedule.every_day": "Every day",\n  "schedule.weekdays": "Weekdays",\n  "schedule.weekends": "Weekends",\n  "schedule.days": "days",\n  "schedule.periods": "periods",\n  "schedule.period": "period",\n  "schedule.hours_short": "h",\n  "schedule.missing": "Missing",\n  "schedule.empty_hours": "Empty hours = fan off.",\n\n  "editor.title": "Edit Schedule",\n  "editor.period": "Period",\n  "editor.mode": "Mode",\n  "editor.target_temp": "Target Temperature",\n  "editor.sensors": "Sensors",\n  "editor.add_sensor": "Add Sensor",\n  "editor.temp_mode": "Temperature Mode",\n  "editor.fan_speed": "Fan Speed",\n  "editor.apply": "Apply",\n  "editor.delete": "Delete",\n  "editor.cancel": "Cancel",\n  "editor.max": "Max",\n  "editor.min": "Min",\n  "editor.average": "Average",\n  "editor.no_sensors": "No sensors assigned",\n\n  "sensor.title": "Select Sensors",\n  "sensor.done": "Done",\n  "sensor.sleep": "Sleep",\n\n  "calibration.title": "Calibrating Fans",\n  "calibration.status": "Starting...",\n  "calibration.step": "Step",\n  "calibration.step_label": "Step 0/11",\n  "calibration.ready": "Ready!",\n  "calibration.errors": "Completed with errors",\n  "calibration.confirm": "Recalibrate all fans? This takes 1-2 minutes.",\n\n  "chart.temp_history": "Temperature History (24h)",\n  "chart.max_hdd_temp": "Max HDD Temp",\n  "chart.avg_pwm": "Avg PWM",\n\n  "fan.inv": "INV",\n  "fan.inverted": "INVERTED",\n  "fan.failsafe": "FAILSAFE",\n  "fan.standby": "STANDBY",\n  "fan.rpm": "RPM",\n\n  "discover.scan_error": "Scan error: ",\n  "discover.connection_error": "Connection error during scan",\n\n  "settings.title": "Settings",\n  "settings.language": "Language",\n  "settings.language_hint": "Select your preferred language",\n  "settings.temp_unit": "Temperature Unit",\n  "settings.temp_unit_hint": "Choose Celsius or Fahrenheit",\n  "settings.refresh": "Update Interval",\n  "settings.refresh_hint": "Reduce CPU usage by throttling updates",\n  "settings.refresh_realtime": "Realtime",\n  "settings.compact": "Compact Dashboard",\n  "settings.compact_hint": "Smaller cards for small screens",\n  "settings.on": "On",\n  "settings.off": "Off",\n  "settings.update": "System Update",\n  "settings.update_hint": "Check and apply updates from Git",\n  "settings.check_update": "Check for Updates",\n  "settings.checking": "Checking...",\n  "settings.up_to_date": "System is up to date",\n  "settings.update_error": "Failed to check for updates",\n  "settings.apply_update": "Apply Update & Restart",\n  "settings.updating": "Updating...",\n  "settings.update_applied": "Update applied. Container will restart...",\n  "settings.update_failed": "Update failed",\n  "settings.update_confirm": "Update will restart the container. Continue?",\n  "settings.update_available": "Update available",\n  "settings.current_version": "Current",\n  "settings.new_version": "New",\n  "settings.restarting": "Container is restarting...",\n  "settings.rebuilding": "Dependencies changed, rebuilding image...",\n  "settings.update_modal_title": "System Update",\n  "settings.step_pull": "Pulling latest code...",\n  "settings.step_deps": "Checking dependencies...",\n  "settings.step_deps_ok": "Dependencies unchanged",\n  "settings.step_restart": "Restarting container...",\n  "settings.update_success": "Update complete!",\n  "settings.restart_notice": "Container is restarting. Page will reload in 10 seconds...",\n  "settings.update_host_hint": "Run on host to apply update:",\n\n  "common.edit": "Edit",\n  "common.del": "Del",\n  "common.save": "Save",\n  "common.apply": "Apply",\n  "common.cancel": "Cancel",\n  "common.delete": "Delete",\n  "common.done": "Done",\n\n  "tooltip.auto_mode": "Auto mode: fan speed adjusts automatically based on temperature sensors and schedule",\n  "tooltip.manual_mode": "Manual mode: set fan speed manually with the slider",\n  "tooltip.fan_speed": "Set fan speed from 0% (off) to 100% (maximum)",\n  "tooltip.target_temp": "Target temperature - fan will adjust speed to maintain this temperature",\n  "tooltip.sensor_mode_max": "Use the highest temperature from all assigned sensors",\n  "tooltip.sensor_mode_min": "Use the lowest temperature from all assigned sensors",\n  "tooltip.sensor_mode_avg": "Use the average temperature from all assigned sensors",\n  "tooltip.schedule_grid": "Click or drag to select cells, then configure fan behavior for each time period",\n  "tooltip.inverted": "This fan has inverted PWM control - higher PWM values produce lower RPM",\n\n  "days.mon": "Mon",\n  "days.tue": "Tue",\n  "days.wed": "Wed",\n  "days.thu": "Thu",\n  "days.fri": "Fri",\n  "days.sat": "Sat",\n  "days.sun": "Sun",\n\n  "sensors.disks": "Disks",\n  "sensors.sensors_group": "Sensors",\n\n  "nav.dashboard": "Dashboard",\n  "nav.nodes": "Nodes",\n  "nodes.title": "Nodes",\n  "nodes.name_placeholder": "Node name",\n  "nodes.no_nodes": "No nodes connected",\n  "nodes.add": "Add Node",\n  "nodes.delete": "Delete",\n  "nodes.back": "Back",\n  "nodes.max_temp": "Max Temp",\n  "nodes.total_rpm": "Total RPM",\n  "nodes.fans": "Fans",\n  "nodes.confirm_delete": "Delete this node?",\n  "node.temperatures": "Temperatures",\n  "node.manual_mode": "Manual Mode",\n\n  "conflict.title": "Config Conflict",\n  "conflict.desc": "Agent config differs from server config.",\n  "conflict.no_config": "No config",\n  "conflict.server_config": "Server Config",\n  "conflict.agent_config": "Agent Config",\n  "conflict.apply_server": "Apply Server Config",\n  "conflict.keep_agent": "Keep Agent Config",\n  "conflict.manual_mode": "Manual Mode",\n  "conflict.manual_warning": "Agent is controlling fans locally.",\n  "conflict.switch_to_server": "Switch to Server Control",\n\n  "calibration.pwm_range": "PWM Range",\n  "calibration.pwm_range_hint": "Dead zone boundaries. Min = lowest PWM where fan spins. Max = PWM where fan reaches full speed. 0-100% slider maps only to this range.",\n  "calibration.min_pwm": "Min",\n  "calibration.max_pwm": "Max",\n  "calibration.curve_shape": "Curve Shape",\n  "calibration.lambda_hint": "Controls fan response curve. 1.0 = linear. Lower = fan ramps up faster at low %. Higher = fan stays quiet longer, ramps up near 100%.",\n\n  "nav.dashboard": "Dashboard",\n  "nav.nodes": "Nodes",\n  "nav.settings": "Settings",\n  "dashboard.empty": "Dashboard is empty",\n  "dashboard.empty_hint": "Click + to add monitoring cards",\n  "dashboard.add_card": "Add Card",\n  "dashboard.add_group": "Add Group",\n  "nodes.local_server": "My Server",\n  "nodes.fans": "fans",\n  "nodes.disks": "disks",\n\n  "picker.type": "Type",\n  "picker.fan": "🌀 Fan",\n  "picker.temperature": "🌡 Temperature",\n  "picker.disk": "💾 Disk",\n  "picker.system": "📊 System",\n  "picker.source": "Source",\n  "picker.my_server": "My Server (local)",\n  "picker.element": "Element",\n  "picker.add": "Add",\n  "picker.no_elements": "No elements found",\n  "picker.added": "added",\n  "picker.max_temp": "Max Temperature",\n  "picker.fans_summary": "Fans Summary",\n  "picker.edit_card": "Edit Card",\n  "picker.title": "Title",\n  "picker.title_placeholder": "Card title",\n  "picker.card_display": "Card Display",\n  "picker.close": "Close"\n}\n'

TEMPLATE_LANG_RU = '{\n  "app.title": "FanControl",\n  "app.subtitle": "Neon Cyberpunk Edition",\n\n  "setup.heading": "Начальная настройка системы",\n  "setup.description": "Конфигурация не найдена. Системе необходимо сканировать доступные шины данных для автоматического обнаружения вентиляторов и датчиков температуры.",\n  "setup.scan_btn": "Начать сканирование оборудования",\n  "setup.scanning": "Сканирование шины sysfs и запрос smartctl...",\n  "setup.results_title": "Оборудование обнаружено",\n  "setup.calibrate_hint": "Для завершения настройки необходимо откалибровать вентиляторы. Это занимает около 1-2 минут.",\n  "setup.calibrate_btn": "Начать калибровку вентиляторов",\n  "setup.calibrating": "Калибровка: определение кривых PWM/RPM...",\n  "setup.controllable": "Управляемый",\n  "setup.readonly": "Только чтение",\n  "setup.not_calibrated": "Не откалиброван",\n  "setup.fans_header": "Вентиляторы",\n  "setup.sensors_header": "Датчики температуры",\n  "setup.disks_header": "Диски хранения",\n  "setup.loading_fans": "Загрузка вентиляторов...",\n  "setup.no_fans": "Вентиляторы не обнаружены",\n  "setup.no_disks": "Диски не обнаружены",\n  "setup.no_hardware": "Оборудование не обнаружено",\n  "setup.calibrate_btn_short": "Перекалибровать",\n\n  "header.synced": "Синхронизировано",\n  "header.storage": "Хранилище",\n  "header.settings": "Настройки",\n\n  "inspector.select": "Выберите устройство",\n  "inspector.hint": "Нажмите на вентилятор для просмотра",\n  "inspector.fallback_id": "ID: неизвестен",\n  "inspector.fan_speed": "Скорость вентилятора",\n  "inspector.status": "Статус",\n  "inspector.mode": "Режим",\n  "inspector.hint_detail": "для просмотра управления и аналитики",\n  "inspector.fan_name": "Имя вентилятора",\n\n  "mode.manual": "Ручной",\n  "mode.auto": "Авто",\n\n  "status.nominal": "норма",\n  "status.warning": "внимание",\n  "status.critical": "критично",\n  "status.failsafe": "аварийный",\n  "status.standby": "ожидание",\n  "status.inverted": "инвертированный",\n  "status.no_sensor": "нет датчика",\n  "status.not_tested": "не тестирован",\n  "status.calibrating": "калибровка",\n  "status.not_connected": "не подключён",\n  "status.normal": "нормальный",\n  "status.manual": "ручной",\n  "status.off": "выкл",\n  "status.fixed": "фиксир.",\n  "status.low": "тихий",\n\n  "schedule.weekly": "Недельное расписание",\n  "schedule.incomplete": "Расписание неполное",\n  "schedule.no_sensor_title": "Датчики не назначены",\n  "schedule.no_sensor_hint": "Назначьте датчики в первой ячейке расписания или глобально ниже.",\n  "schedule.legend_auto": "Авто",\n  "schedule.legend_manual": "Ручной",\n  "schedule.legend_off": "Выкл",\n  "schedule.legend_empty": "Пусто",\n  "schedule.clear_all": "Очистить всё",\n  "schedule.fill_auto": "Заполнить пустые Авто",\n  "schedule.no_rules": "Правила не настроены",\n  "schedule.every_day": "Каждый день",\n  "schedule.weekdays": "Будни",\n  "schedule.weekends": "Выходные",\n  "schedule.days": "дней",\n  "schedule.periods": "периодов",\n  "schedule.period": "период",\n  "schedule.hours_short": "ч",\n  "schedule.missing": "Пропущено",\n  "schedule.empty_hours": "Пустые часы = вентилятор выключен.",\n\n  "editor.title": "Редактирование расписания",\n  "editor.period": "Период",\n  "editor.mode": "Режим",\n  "editor.target_temp": "Целевая температура",\n  "editor.sensors": "Датчики",\n  "editor.add_sensor": "Добавить датчик",\n  "editor.temp_mode": "Режим температуры",\n  "editor.fan_speed": "Скорость вентилятора",\n  "editor.apply": "Применить",\n  "editor.delete": "Удалить",\n  "editor.cancel": "Отмена",\n  "editor.max": "Макс",\n  "editor.min": "Мин",\n  "editor.average": "Средняя",\n  "editor.no_sensors": "Датчики не назначены",\n\n  "sensor.title": "Выбор датчиков",\n  "sensor.done": "Готово",\n  "sensor.sleep": "Сон",\n\n  "calibration.title": "Калибровка вентиляторов",\n  "calibration.status": "Запуск...",\n  "calibration.step": "Шаг",\n  "calibration.step_label": "Шаг 0/11",\n  "calibration.ready": "Готово!",\n  "calibration.errors": "Завершено с ошибками",\n  "calibration.confirm": "Перекалибровать все вентиляторы? Это займёт 1-2 минуты.",\n\n  "chart.temp_history": "История температур (24ч)",\n  "chart.max_hdd_temp": "Макс. темп. HDD",\n  "chart.avg_pwm": "Средний PWM",\n\n  "fan.inv": "ИНВ",\n  "fan.inverted": "ИНВЕРТИРОВАН",\n  "fan.failsafe": "АВАРИЙНЫЙ",\n  "fan.standby": "ОЖИДАНИЕ",\n  "fan.rpm": "об/мин",\n\n  "discover.scan_error": "Ошибка сканирования: ",\n  "discover.connection_error": "Ошибка подключения при сканировании",\n\n  "settings.title": "Настройки",\n  "settings.language": "Язык",\n  "settings.language_hint": "Выберите предпочитаемый язык",\n  "settings.temp_unit": "Единицы температуры",\n  "settings.temp_unit_hint": "Цельсий или Фаренгейт",\n  "settings.refresh": "Интервал обновления",\n  "settings.refresh_hint": "Снизить нагрузку CPU уменьшением частоты обновлений",\n  "settings.refresh_realtime": "Реалтайм",\n  "settings.compact": "Компактный режим",\n  "settings.compact_hint": "Уменьшенные карточки для маленьких экранов",\n  "settings.on": "Вкл",\n  "settings.off": "Выкл",\n  "settings.update": "Обновление системы",\n  "settings.update_hint": "Проверить и применить обновления из Git",\n  "settings.check_update": "Проверить обновления",\n  "settings.checking": "Проверка...",\n  "settings.up_to_date": "Система обновлена",\n  "settings.update_error": "Не удалось проверить обновления",\n  "settings.apply_update": "Применить и перезапустить",\n  "settings.updating": "Обновление...",\n  "settings.update_applied": "Обновление применено. Контейнер перезапускается...",\n  "settings.update_failed": "Обновление не удалось",\n  "settings.update_confirm": "Обновление перезапустит контейнер. Продолжить?",\n  "settings.update_available": "Доступно обновление",\n  "settings.current_version": "Текущая",\n  "settings.new_version": "Новая",\n  "settings.restarting": "Контейнер перезапускается...",\n  "settings.rebuilding": "Зависимости изменились, пересборка образа...",\n  "settings.update_modal_title": "Обновление системы",\n  "settings.step_pull": "Загрузка последних изменений...",\n  "settings.step_deps": "Проверка зависимостей...",\n  "settings.step_deps_ok": "Зависимости не изменились",\n  "settings.step_restart": "Перезапуск контейнера...",\n  "settings.update_success": "Обновление завершено!",\n  "settings.restart_notice": "Контейнер перезапускается. Страница обновится через 10 секунд...",\n  "settings.update_host_hint": "Выполните на хосте для применения:",\n\n  "common.edit": "Изм.",\n  "common.del": "Удл.",\n  "common.save": "Сохранить",\n  "common.apply": "Применить",\n  "common.cancel": "Отмена",\n  "common.delete": "Удалить",\n  "common.done": "Готово",\n\n  "tooltip.auto_mode": "Авто: скорость вентилятора регулируется автоматически на основе датчиков температуры и расписания",\n  "tooltip.manual_mode": "Ручной: установите скорость вентилятора вручную с помощью ползунка",\n  "tooltip.fan_speed": "Установите скорость от 0% (выкл) до 100% (максимум)",\n  "tooltip.target_temp": "Целевая температура — вентилятор будет поддерживать эту температуру",\n  "tooltip.sensor_mode_max": "Использовать максимальную температуру из всех назначенных датчиков",\n  "tooltip.sensor_mode_min": "Использовать минимальную температуру из всех назначенных датчиков",\n  "tooltip.sensor_mode_avg": "Использовать среднюю температуру из всех назначенных датчиков",\n  "tooltip.schedule_grid": "Нажмите или перетащите для выбора ячеек, затем настройте поведение вентилятора для каждого периода",\n  "tooltip.inverted": "Этот вентилятор имеет инвертированное управление PWM — более высокие значения PWM дают меньшие обороты",\n\n  "days.mon": "Пн",\n  "days.tue": "Вт",\n  "days.wed": "Ср",\n  "days.thu": "Чт",\n  "days.fri": "Пт",\n  "days.sat": "Сб",\n  "days.sun": "Вс",\n\n  "sensors.disks": "Диски",\n  "sensors.sensors_group": "Датчики",\n\n  "nav.dashboard": "Панель",\n  "nav.nodes": "Узлы",\n  "nodes.title": "Узлы",\n  "nodes.name_placeholder": "Имя узла",\n  "nodes.no_nodes": "Нет подключённых узлов",\n  "nodes.add": "Добавить узел",\n  "nodes.delete": "Удалить",\n  "nodes.back": "Назад",\n  "nodes.max_temp": "Макс. темп.",\n  "nodes.total_rpm": "Общий RPM",\n  "nodes.fans": "Вентиляторы",\n  "nodes.confirm_delete": "Удалить этот узел?",\n  "node.temperatures": "Температуры",\n  "node.manual_mode": "Ручной режим",\n\n  "conflict.title": "Конфликт конфигураций",\n  "conflict.desc": "Конфигурация агента отличается от серверной.",\n  "conflict.no_config": "Нет конфигурации",\n  "conflict.server_config": "Серверная конфигурация",\n  "conflict.agent_config": "Конфигурация агента",\n  "conflict.apply_server": "Применить серверную",\n  "conflict.keep_agent": "Оставить конфигурацию агента",\n  "conflict.manual_mode": "Ручной режим",\n  "conflict.manual_warning": "Агент управляет вентиляторами локально.",\n  "conflict.switch_to_server": "Переключить на серверное управление",\n\n  "calibration.pwm_range": "Диапазон ШИМ",\n  "calibration.pwm_range_hint": "Границы мёртвых зон. Мин = минимальный ШИМ при котором вентилятор крутится. Макс = ШИМ при котором вентилятор достигает максимума.",\n  "calibration.min_pwm": "Мин",\n  "calibration.max_pwm": "Макс",\n  "calibration.curve_shape": "Форма кривой",\n  "calibration.lambda_hint": "Управляет формой кривой вентилятора. 1.0 = линейно. Меньше = вентилятор быстрее набирает обороты на низких %. Больше = вентилятор дольше тихий.",\n\n  "nav.dashboard": "Дашборд",\n  "nav.nodes": "Узлы",\n  "nav.settings": "Настройки",\n  "dashboard.empty": "Дашборд пуст",\n  "dashboard.empty_hint": "Нажмите + чтобы добавить карточки мониторинга",\n  "dashboard.add_card": "Добавить карточку",\n  "dashboard.add_group": "Добавить группу",\n  "nodes.local_server": "Мой сервер",\n  "nodes.fans": "вентиляторов",\n  "nodes.disks": "дисков",\n\n  "picker.type": "Тип",\n  "picker.fan": "🌀 Вентилятор",\n  "picker.temperature": "🌡 Температура",\n  "picker.disk": "💾 Диск",\n  "picker.system": "📊 Система",\n  "picker.source": "Источник",\n  "picker.my_server": "Мой сервер (локально)",\n  "picker.element": "Элемент",\n  "picker.add": "Добавить",\n  "picker.no_elements": "Элементы не найдены",\n  "picker.added": "добавлено",\n  "picker.max_temp": "Макс. температура",\n  "picker.fans_summary": "Сводка по вентиляторам",\n  "picker.edit_card": "Редактировать карточку",\n  "picker.title": "Заголовок",\n  "picker.title_placeholder": "Название карточки",\n  "picker.card_display": "Отображение карточки",\n  "picker.close": "Закрыть"\n}\n'

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

from core.state import state, state_lock, CONFIG_VERSION

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
    'schedule', 'curve', 'calibration'
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

        if _cached_config_json is not None and current_mtime == _cached_config_mtime:
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
                state['dashboard'] = cfg.get('dashboard', {'groups': [], 'cards': []})

            logger.info('Configuration loaded successfully')

    except Exception as e:
        logger.error(f'Failed to load config: {e}', exc_info=True)

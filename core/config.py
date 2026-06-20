"""Configuration persistence — JSON config load/save with debounced writes."""

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from core.state import state, state_lock, CONFIG_VERSION

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
    """Actually write config to disk."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        config = {
            'config_version': CONFIG_VERSION,
            'initialized': state.get('initialized', False),
            'tested': state.get('tested', False),
            'language': state.get('language', 'en'),
            'fans': {},
            'dashboard': state.get('dashboard', {'groups': [], 'cards': []})
        }

        with state_lock:
            for fan_id, fan in state.get('fans', {}).items():
                config['fans'][fan_id] = {
                    field: fan.get(field)
                    for field in FAN_FIELDS
                    if field in fan
                }

        tmp_path = CONFIG_PATH.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
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
                state['dashboard'] = cfg.get('dashboard', {'groups': [], 'cards': []})

            logger.info('Configuration loaded successfully')

    except Exception as e:
        logger.error(f'Failed to load config: {e}', exc_info=True)

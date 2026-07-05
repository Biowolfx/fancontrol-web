"""Agent telemetry — data collection functions."""

import logging
from typing import Dict, Any

from core.state import state, state_lock, CONFIG_VERSION

logger = logging.getLogger('fancontrol')


def get_local_config() -> Dict[str, Any]:
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
            from core.dsm_fan import is_dsm_fan_available, get_all_schemes
            if is_dsm_fan_available():
                result = get_all_schemes()
                config['dsm_schemes'] = result.get('schemes', []) if result else []
                logger.info(f'Including {len(config["dsm_schemes"])} DSM schemes in config')
        except Exception as e:
            logger.debug(f'Could not load DSM schemes: {e}')
        return config


def get_telemetry() -> Dict[str, Any]:
    """Get current telemetry data for server transmission."""
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


def apply_server_config(config: Dict[str, Any]):
    """Apply config received from server to local state."""
    with state_lock:
        for fan_id, fan_cfg in config.get('fans', {}).items():
            if fan_id in state['fans']:
                for key in ('mode', 'target_temp', 'manual_pct', 'sensors',
                            'sensor_mode', 'schedule', 'inverted'):
                    if key in fan_cfg:
                        state['fans'][fan_id][key] = fan_cfg[key]

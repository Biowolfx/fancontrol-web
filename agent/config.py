"""Agent configuration — init, token management, config persistence."""

import json
import logging
import uuid
from pathlib import Path

from core.config import cfg
from core.state import state, state_lock

logger = logging.getLogger('fancontrol')


def init_agent_config():
    """Load agent config from config.json if not set via env vars.
    Auto-generates node_id if missing.

    Returns (server_url, node_id, node_name) as current values.
    """
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

    logger.info(f'[agent-config] config_path={config_path}, exists={config_path.exists()}, '
                f'server_url_in_file={config.get("server_url", "NONE")}')

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

    logger.info(f'[agent-config] SERVER_URL={server_url}, NODE_ID={node_id}, NODE_NAME={node_name}')
    return server_url, node_id, node_name


def init_token():
    """Generate or load API token for this agent.

    Returns the api_token string.
    """
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


def save_local_config():
    """Save current config to local config.json, preserving wizard fields."""
    config_path = cfg.data_dir / 'config.json'
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if config_path.exists():
            try:
                with open(config_path) as f:
                    existing = json.load(f)
            except Exception:
                pass

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


def persist_node_id(node_id: str, token: str):
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
    except Exception as e:
        logger.warning(f'Could not persist node_id/token to config: {e}')

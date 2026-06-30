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

from core.state import (
    state, CONFIG_VERSION, get_state,
    invalidate_state_cache, _init_complete,
)
from core.hardware import (
    discover_fans_and_sensors, discover_disks,
    refresh,
)
from core.control import (
    get_db_connection, loop,
)
from core.config import save_config, load_config, DATA_DIR, CONFIG_PATH

from server.routes import routes
from server.socket_handlers import register_handlers

# ============================================================================
# CONFIGURATION & INITIALIZATION
# ============================================================================

LOG_DIR = os.getenv('FANCONTROL_LOG_DIR', str(DATA_DIR / 'logs'))
try:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
except Exception:
    # Ignore permission errors during import (testing or restricted environments)
    pass

# Logger setup
logger = logging.getLogger('fancontrol')
logger.setLevel(logging.DEBUG)
fmt = logging.Formatter(
    '%(asctime)s | %(levelname)-7s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(fmt)
logger.addHandler(console_handler)

try:
    file_handler = RotatingFileHandler(
        f'{LOG_DIR}/fancontrol.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
except Exception:
    # If creating the file handler fails (permissions, missing dirs), continue
    # with console logging only — avoid raising during import.
    pass


# Flask & SocketIO
app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS_ORIGINS = os.getenv('FANCONTROL_CORS_ORIGINS', 'http://localhost:5059,http://127.0.0.1:5059').split(',')

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
register_handlers(socketio)

from agent.routes import agent_routes
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
            from server.node_registry import init_nodes_table
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
        return True
    try:
        import json
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        # Consider setup done if mode is set (even if initialized flag is missing/wrong)
        return not (cfg.get('initialized') or cfg.get('mode'))
    except Exception:
        return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description='FanControl Web')
    parser.add_argument('--mode', choices=['setup', 'server', 'agent'],
                       default=os.environ.get('MODE', 'server'),
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
            from installer.wizard import run_wizard
            run_wizard()
            return
        else:
            # Setup already done — read mode from saved config
            try:
                import json
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
                args.mode = saved.get('mode', 'server')
                logger.info(f'Setup already complete, switching to mode: {args.mode}')
            except Exception:
                args.mode = 'server'

    if args.mode == 'agent':
        # If no SERVER_URL set, try to load from config.json
        if not os.environ.get('SERVER_URL'):
            try:
                import json as _json
                with open(CONFIG_PATH) as f:
                    _cfg = _json.load(f)
                if _cfg.get('server_url'):
                    os.environ['SERVER_URL'] = _cfg['server_url']
                if _cfg.get('node_name'):
                    os.environ.setdefault('NODE_NAME', _cfg['node_name'])
                if _cfg.get('api_token'):
                    os.environ.setdefault('API_TOKEN', _cfg['api_token'])
                logger.info(f'Agent loaded config from {CONFIG_PATH}')
            except Exception as e:
                logger.warning(f'Could not load agent config: {e}')

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

    logger.info('Starting server on port 5059')
    socketio.run(app, host='0.0.0.0', port=5059, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
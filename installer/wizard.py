"""Setup wizard — Flask server for first-time configuration."""

import json
import os
import subprocess
import socket
import threading
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
    """Save configuration and restart container."""
    config = request.get_json()

    if config.get('mode') == 'agent':
        if not config.get('server_url'):
            return jsonify({'error': 'Server URL required'}), 400
        if not config.get('api_token'):
            return jsonify({'error': 'API token required'}), 400
        if not config.get('node_name'):
            return jsonify({'error': 'Node name required'}), 400

    config['initialized'] = True

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

    return jsonify({'status': 'saved', 'mode': config.get('mode', 'server')})


@app.route('/api/install', methods=['POST'])
def install():
    """Save config and restart container — matches frontend expectations."""
    global _install_status
    config = request.get_json()

    if config.get('mode') == 'agent':
        if not config.get('server_url'):
            return jsonify({'error': 'Server URL required'}), 400
        if not config.get('api_token'):
            return jsonify({'error': 'API token required'}), 400
        if not config.get('node_name'):
            return jsonify({'error': 'Node name required'}), 400

    config['initialized'] = True

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

    _install_status = {
        'progress': 10,
        'stage': 'Config saved',
        'message': 'Configuration saved to ' + str(CONFIG_PATH),
        'complete': False,
        'error': False,
    }

    threading.Thread(target=_do_restart, daemon=True).start()

    return jsonify({'status': 'installing'})


def _do_restart():
    global _install_status
    _install_status['progress'] = 30
    _install_status['stage'] = 'Restarting'
    _install_status['message'] = 'Saving configuration and restarting...'

    try:
        hostname = socket.gethostname()
        result = subprocess.run(
            ['docker', 'restart', hostname],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f'exit code {result.returncode}')
    except Exception as e:
        _install_status['progress'] = 50
        _install_status['stage'] = 'Restarting'
        _install_status['message'] = f'docker restart failed ({e}), exiting process...'
        import time
        time.sleep(2)
        os._exit(0)

    _install_status['progress'] = 100
    _install_status['stage'] = 'Complete'
    _install_status['message'] = 'Container is restarting. Page will refresh shortly.'
    _install_status['complete'] = True


@app.route('/api/status', methods=['GET'])
def status():
    """Return install progress — matches frontend polling."""
    return jsonify(_install_status)


@app.route('/api/restart', methods=['POST'])
def restart_container():
    """Restart the Docker container."""
    try:
        hostname = socket.gethostname()
        result = subprocess.run(
            ['docker', 'restart', hostname],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f'exit code {result.returncode}')
        return jsonify({'status': 'restarting'})
    except Exception as e:
        return jsonify({'error': str(e), 'manual_restart': True}), 500


@app.route('/api/validate-token', methods=['POST'])
def validate_token():
    """Validate server URL and API token (for agent setup)."""
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


def run_wizard():
    """Run the setup wizard on port 5059."""
    print('=' * 60)
    print('FanControl Web — Setup Wizard')
    print('Open http://localhost:5059 in your browser')
    print('=' * 60)
    app.run(host='0.0.0.0', port=5059, debug=False)


if __name__ == '__main__':
    run_wizard()

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
        if not config.get('api_token'):
            return jsonify({'error': 'API token required'}), 400
        if not config.get('node_name'):
            return jsonify({'error': 'Node name required'}), 400

    config['initialized'] = False

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
        if not config.get('api_token'):
            return jsonify({'error': 'API token required'}), 400
        if not config.get('node_name'):
            return jsonify({'error': 'Node name required'}), 400

    config['initialized'] = False

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


def run_wizard():
    print('=' * 60)
    print('FanControl Web — Setup Wizard')
    print('Open http://localhost:5059 in your browser')
    print('=' * 60)
    app.run(host='0.0.0.0', port=5059, debug=False)


if __name__ == '__main__':
    run_wizard()

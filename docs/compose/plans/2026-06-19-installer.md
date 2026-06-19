# Web UI Installer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a standalone web-based installation wizard for FanControl Web — language selection, component choice (server/agent), configuration, and Docker deployment.

**Architecture:** Standalone Python Flask app on port 5060. Tailwind CSS for UI. Installation logic clones repo, configures docker-compose, starts container.

**Tech Stack:** Python 3, Flask, Tailwind CSS (CDN), subprocess (docker/git)

**Spec:** `docs/compose/specs/2026-06-19-phase3-6-installer-design.md` [S3]

---

## File Structure

```
installer/
├── install.py              # Flask server + installation logic
├── templates/
│   └── setup.html          # Setup wizard UI (EN/RU)
└── requirements.txt        # flask only
```

---

## Task 1: Create installer package and Flask server

**Covers:** [S3]

**Files:**
- Create: `installer/`
- Create: `installer/requirements.txt`
- Create: `installer/install.py`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p installer/templates
echo "flask" > installer/requirements.txt
```

- [ ] **Step 2: Create installer/install.py**

```python
#!/usr/bin/env python3
"""FanControl Web — Installation Wizard"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder='templates')

INSTALL_DIR = Path(os.environ.get('FANCONTROL_INSTALL_DIR', '/opt/fancontrol-web'))
DATA_DIR = Path(os.environ.get('FANCONTROL_DATA_DIR', '/opt/fancontrol-web/data'))
GITHUB_REPO = 'biowolfx/fancontrol-web'
DOCKER_IMAGE = 'fancontrol-web'

install_status = {
    'step': 'idle',
    'progress': 0,
    'message': '',
    'error': None,
    'complete': False,
}


@app.route('/')
def index():
    return render_template('setup.html')


@app.route('/api/status')
def status():
    return jsonify(install_status)


@app.route('/api/install', methods=['POST'])
def install():
    if install_status['step'] == 'running':
        return jsonify({'error': 'Installation already in progress'}), 409

    config = request.get_json()
    thread = threading.Thread(target=_run_install, args=(config,), daemon=True)
    thread.start()
    return jsonify({'status': 'started'})


def _run_install(config):
    global install_status
    install_status = {'step': 'running', 'progress': 0, 'message': 'Starting...', 'error': None, 'complete': False}

    try:
        mode = config.get('mode', 'server')
        lang = config.get('lang', 'en')
        port = config.get('port', '5059')
        data_dir = config.get('data_dir', str(DATA_DIR))

        # Step 1: Check Docker
        install_status['message'] = 'Checking Docker...'
        install_status['progress'] = 10
        if not _check_docker():
            install_status['error'] = 'Docker is not installed or not running'
            install_status['step'] = 'error'
            return

        # Step 2: Try Docker image
        install_status['message'] = 'Trying Docker image...'
        install_status['progress'] = 20
        if _try_docker_image(config):
            install_status['message'] = 'Installation complete!'
            install_status['progress'] = 100
            install_status['complete'] = True
            install_status['step'] = 'done'
            return

        # Step 3: Git clone fallback
        install_status['message'] = 'Cloning repository...'
        install_status['progress'] = 30
        if not _git_clone():
            install_status['error'] = 'Failed to clone repository'
            install_status['step'] = 'error'
            return

        # Step 4: Configure
        install_status['message'] = 'Configuring...'
        install_status['progress'] = 60
        _configure(config)

        # Step 5: Build and start
        install_status['message'] = 'Building Docker image...'
        install_status['progress'] = 70
        if not _docker_build():
            install_status['error'] = 'Docker build failed'
            install_status['step'] = 'error'
            return

        install_status['message'] = 'Starting container...'
        install_status['progress'] = 90
        if not _docker_start(config):
            install_status['error'] = 'Failed to start container'
            install_status['step'] = 'error'
            return

        install_status['message'] = 'Installation complete!'
        install_status['progress'] = 100
        install_status['complete'] = True
        install_status['step'] = 'done'

    except Exception as e:
        install_status['error'] = str(e)
        install_status['step'] = 'error'


def _check_docker():
    try:
        result = subprocess.run(['docker', '--version'], capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def _try_docker_image(config):
    """Try to pull and run pre-built Docker image."""
    try:
        result = subprocess.run(
            ['docker', 'pull', DOCKER_IMAGE],
            capture_output=True, timeout=120
        )
        if result.returncode != 0:
            return False

        return _docker_start(config, image=DOCKER_IMAGE)
    except Exception:
        return False


def _git_clone():
    """Clone or update the repository."""
    try:
        if INSTALL_DIR.exists():
            result = subprocess.run(
                ['git', '-C', str(INSTALL_DIR), 'pull', '--ff-only'],
                capture_output=True, timeout=60
            )
            return result.returncode == 0

        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ['git', 'clone', f'https://github.com/{GITHUB_REPO}.git', str(INSTALL_DIR)],
            capture_output=True, timeout=120
        )
        return result.returncode == 0
    except Exception:
        return False


def _configure(config):
    """Generate docker-compose.yml from config."""
    mode = config.get('mode', 'server')
    port = config.get('port', '5059')
    data_dir = config.get('data_dir', str(DATA_DIR))

    if mode == 'server':
        compose = {
            'services': {
                'fancontrol': {
                    'build': '.',
                    'container_name': 'fancontrol-web',
                    'restart': 'unless-stopped',
                    'network_mode': 'host',
                    'privileged': True,
                    'cap_add': ['SYS_RAWIO', 'SYS_ADMIN'],
                    'volumes': [
                        '/sys:/sys:rw',
                        '/dev:/dev:rw',
                        f'{data_dir}:/app/data',
                        f'{INSTALL_DIR}:/repo',
                        '/var/run/docker.sock:/var/run/docker.sock',
                    ],
                    'environment': [
                        'FANCONTROL_CORS_ORIGINS=*',
                    ],
                }
            }
        }
    else:
        server_url = config.get('server_url', 'ws://localhost:5059')
        api_token = config.get('api_token', '')
        node_id = config.get('node_id', 'agent-1')
        node_name = config.get('node_name', 'Agent 1')

        compose = {
            'services': {
                'fancontrol-agent': {
                    'image': DOCKER_IMAGE,
                    'container_name': 'fancontrol-agent',
                    'restart': 'unless-stopped',
                    'network_mode': 'host',
                    'privileged': True,
                    'cap_add': ['SYS_RAWIO', 'SYS_ADMIN'],
                    'volumes': [
                        '/sys:/sys:rw',
                        '/dev:/dev:rw',
                        f'{data_dir}:/app/data',
                    ],
                    'environment': [
                        'MODE=agent',
                        f'SERVER_URL={server_url}',
                        f'API_TOKEN={api_token}',
                        f'NODE_ID={node_id}',
                        f'NODE_NAME={node_name}',
                    ],
                }
            }
        }

    compose_path = INSTALL_DIR / 'docker-compose.yml'
    with open(compose_path, 'w') as f:
        json.dump(compose, f, indent=2)


def _docker_build():
    """Build Docker image."""
    try:
        result = subprocess.run(
            ['docker', 'compose', 'build'],
            cwd=str(INSTALL_DIR),
            capture_output=True, timeout=300
        )
        return result.returncode == 0
    except Exception:
        return False


def _docker_start(config, image=None):
    """Start Docker container."""
    try:
        cmd = ['docker', 'compose', 'up', '-d']
        result = subprocess.run(
            cmd, cwd=str(INSTALL_DIR),
            capture_output=True, timeout=60
        )
        return result.returncode == 0
    except Exception:
        return False


if __name__ == '__main__':
    print(f'FanControl Web Installer')
    print(f'Open http://localhost:5060 in your browser')
    app.run(host='0.0.0.0', port=5060, debug=False)
```

- [ ] **Step 3: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "from installer.install import app; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd /home/impulse/fancontrol-web
git add installer/
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add web UI installer with Flask server"
```

---

## Task 2: Create setup wizard UI

**Covers:** [S3]

**Files:**
- Create: `installer/templates/setup.html`

- [ ] **Step 1: Create installer/templates/setup.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FanControl Web — Installer</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: #0a0a0f; font-family: 'Segoe UI', system-ui, sans-serif; }
        .glow-cyan { text-shadow: 0 0 10px rgba(0, 255, 255, 0.5); }
        .glow-green { text-shadow: 0 0 10px rgba(0, 255, 100, 0.5); }
        .step-card { background: rgba(20, 20, 35, 0.9); border: 1px solid rgba(0, 255, 255, 0.2); }
        .btn-cyber { background: linear-gradient(135deg, rgba(0,255,255,0.2), rgba(0,200,255,0.1)); border: 1px solid rgba(0,255,255,0.4); }
        .btn-cyber:hover { background: linear-gradient(135deg, rgba(0,255,255,0.3), rgba(0,200,255,0.2)); }
        .btn-active { background: rgba(0, 255, 255, 0.3); border-color: rgba(0, 255, 255, 0.8); }
        .progress-bar { background: linear-gradient(90deg, #00ffff, #00ff64); }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-4">
    <div class="w-full max-w-lg">
        <!-- Header -->
        <div class="text-center mb-8">
            <h1 class="text-3xl font-bold text-white glow-cyan">FanControl Web</h1>
            <p class="text-gray-400 mt-2">Installation Wizard</p>
        </div>

        <!-- Step Indicator -->
        <div class="flex justify-center mb-8 space-x-2" id="step-indicator">
            <div class="w-8 h-1 rounded bg-cyan-500" id="dot-1"></div>
            <div class="w-8 h-1 rounded bg-gray-700" id="dot-2"></div>
            <div class="w-8 h-1 rounded bg-gray-700" id="dot-3"></div>
            <div class="w-8 h-1 rounded bg-gray-700" id="dot-4"></div>
        </div>

        <!-- Step 1: Language -->
        <div id="step-lang" class="step-card rounded-xl p-6">
            <h2 class="text-xl font-bold text-white mb-4">Select Language / Выберите язык</h2>
            <div class="grid grid-cols-2 gap-4">
                <button onclick="selectLang('en')" id="lang-en"
                    class="btn-cyber rounded-lg p-4 text-center text-white font-semibold transition-all">
                    English
                </button>
                <button onclick="selectLang('ru')" id="lang-ru"
                    class="btn-cyber rounded-lg p-4 text-center text-white font-semibold transition-all">
                    Русский
                </button>
            </div>
        </div>

        <!-- Step 2: Component -->
        <div id="step-component" class="step-card rounded-xl p-6 hidden">
            <h2 class="text-xl font-bold text-white mb-4" data-i18n="component_title">Select Component</h2>
            <div class="grid grid-cols-2 gap-4">
                <button onclick="selectMode('server')" id="mode-server"
                    class="btn-cyber rounded-lg p-6 text-center transition-all">
                    <div class="text-2xl mb-2">🖥️</div>
                    <div class="text-white font-semibold" data-i18n="server">Server</div>
                    <div class="text-gray-400 text-sm mt-1" data-i18n="server_desc">Central dashboard + control</div>
                </button>
                <button onclick="selectMode('agent')" id="mode-agent"
                    class="btn-cyber rounded-lg p-6 text-center transition-all">
                    <div class="text-2xl mb-2">📡</div>
                    <div class="text-white font-semibold" data-i18n="agent">Agent</div>
                    <div class="text-gray-400 text-sm mt-1" data-i18n="agent_desc">Connect to server</div>
                </button>
            </div>
        </div>

        <!-- Step 3: Configuration -->
        <div id="step-config" class="step-card rounded-xl p-6 hidden">
            <h2 class="text-xl font-bold text-white mb-4" data-i18n="config_title">Configuration</h2>

            <!-- Server config -->
            <div id="config-server" class="space-y-4">
                <div>
                    <label class="block text-gray-400 text-sm mb-1" data-i18n="port">Port</label>
                    <input type="text" id="cfg-port" value="5059"
                        class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-gray-400 text-sm mb-1" data-i18n="data_dir">Data Directory</label>
                    <input type="text" id="cfg-data-dir" value="/opt/fancontrol-web/data"
                        class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white">
                </div>
            </div>

            <!-- Agent config -->
            <div id="config-agent" class="space-y-4 hidden">
                <div>
                    <label class="block text-gray-400 text-sm mb-1" data-i18n="server_url">Server URL</label>
                    <input type="text" id="cfg-server-url" placeholder="ws://192.168.1.100:5059"
                        class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-gray-400 text-sm mb-1" data-i18n="api_token">API Token</label>
                    <input type="text" id="cfg-api-token"
                        class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white">
                </div>
                <div>
                    <label class="block text-gray-400 text-sm mb-1" data-i18n="node_name">Node Name</label>
                    <input type="text" id="cfg-node-name" value="Agent 1"
                        class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-white">
                </div>
            </div>

            <button onclick="startInstall()"
                class="w-full mt-6 btn-cyber rounded-lg py-3 text-white font-semibold transition-all"
                data-i18n="install_btn">
                Install
            </button>
        </div>

        <!-- Step 4: Progress -->
        <div id="step-progress" class="step-card rounded-xl p-6 hidden">
            <h2 class="text-xl font-bold text-white mb-4" data-i18n="installing">Installing...</h2>
            <div class="w-full bg-gray-700 rounded-full h-3 mb-4">
                <div id="progress-bar" class="progress-bar h-3 rounded-full transition-all" style="width: 0%"></div>
            </div>
            <p id="progress-message" class="text-gray-400 text-center"></p>
            <div id="progress-error" class="text-red-400 text-center mt-4 hidden"></div>
            <div id="progress-complete" class="text-center mt-4 hidden">
                <p class="text-green-400 font-semibold glow-green" data-i18n="complete">Installation Complete!</p>
                <a id="dashboard-link" href="#" target="_blank"
                    class="inline-block mt-4 btn-cyber rounded-lg px-6 py-2 text-white"
                    data-i18n="open_dashboard">Open Dashboard</a>
            </div>
        </div>
    </div>

    <script>
    let selectedLang = 'en';
    let selectedMode = 'server';
    let pollInterval = null;

    const translations = {
        en: {
            component_title: 'Select Component',
            server: 'Server',
            server_desc: 'Central dashboard + control',
            agent: 'Agent',
            agent_desc: 'Connect to server',
            config_title: 'Configuration',
            port: 'Port',
            data_dir: 'Data Directory',
            server_url: 'Server URL',
            api_token: 'API Token',
            node_name: 'Node Name',
            install_btn: 'Install',
            installing: 'Installing...',
            complete: 'Installation Complete!',
            open_dashboard: 'Open Dashboard',
        },
        ru: {
            component_title: 'Выберите компонент',
            server: 'Сервер',
            server_desc: 'Центральная панель управления',
            agent: 'Агент',
            agent_desc: 'Подключение к серверу',
            config_title: 'Конфигурация',
            port: 'Порт',
            data_dir: 'Директория данных',
            server_url: 'URL сервера',
            api_token: 'API токен',
            node_name: 'Имя узла',
            install_btn: 'Установить',
            installing: 'Установка...',
            complete: 'Установка завершена!',
            open_dashboard: 'Открыть панель',
        }
    };

    function updateTexts() {
        const t = translations[selectedLang];
        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            if (t[key]) el.textContent = t[key];
        });
    }

    function showStep(n) {
        ['step-lang', 'step-component', 'step-config', 'step-progress'].forEach((id, i) => {
            document.getElementById(id).classList.toggle('hidden', i !== n);
            document.getElementById(`dot-${i + 1}`).className =
                `w-8 h-1 rounded ${i <= n ? 'bg-cyan-500' : 'bg-gray-700'}`;
        });
    }

    function selectLang(lang) {
        selectedLang = lang;
        document.getElementById('lang-en').className =
            `btn-cyber rounded-lg p-4 text-center text-white font-semibold transition-all ${lang === 'en' ? 'btn-active' : ''}`;
        document.getElementById('lang-ru').className =
            `btn-cyber rounded-lg p-4 text-center text-white font-semibold transition-all ${lang === 'ru' ? 'btn-active' : ''}`;
        updateTexts();
        setTimeout(() => showStep(1), 300);
    }

    function selectMode(mode) {
        selectedMode = mode;
        document.getElementById('mode-server').className =
            `btn-cyber rounded-lg p-6 text-center transition-all ${mode === 'server' ? 'btn-active' : ''}`;
        document.getElementById('mode-agent').className =
            `btn-cyber rounded-lg p-6 text-center transition-all ${mode === 'agent' ? 'btn-active' : ''}`;
        document.getElementById('config-server').classList.toggle('hidden', mode !== 'server');
        document.getElementById('config-agent').classList.toggle('hidden', mode !== 'agent');
        setTimeout(() => showStep(2), 300);
    }

    function startInstall() {
        const config = { mode: selectedMode, lang: selectedLang };

        if (selectedMode === 'server') {
            config.port = document.getElementById('cfg-port').value;
            config.data_dir = document.getElementById('cfg-data-dir').value;
        } else {
            config.server_url = document.getElementById('cfg-server-url').value;
            config.api_token = document.getElementById('cfg-api-token').value;
            config.node_name = document.getElementById('cfg-node-name').value;
            config.node_id = config.node_name.toLowerCase().replace(/\s+/g, '-');
        }

        showStep(3);

        fetch('/api/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config)
        }).then(r => r.json()).then(data => {
            if (data.error) {
                document.getElementById('progress-error').textContent = data.error;
                document.getElementById('progress-error').classList.remove('hidden');
                return;
            }
            pollStatus();
        });
    }

    function pollStatus() {
        pollInterval = setInterval(() => {
            fetch('/api/status').then(r => r.json()).then(data => {
                document.getElementById('progress-bar').style.width = data.progress + '%';
                document.getElementById('progress-message').textContent = data.message;

                if (data.error) {
                    clearInterval(pollInterval);
                    document.getElementById('progress-error').textContent = data.error;
                    document.getElementById('progress-error').classList.remove('hidden');
                }

                if (data.complete) {
                    clearInterval(pollInterval);
                    document.getElementById('progress-complete').classList.remove('hidden');
                    const port = document.getElementById('cfg-port')?.value || '5059';
                    document.getElementById('dashboard-link').href = `http://localhost:${port}`;
                }
            });
        }, 1000);
    }

    // Initialize
    updateTexts();
    selectLang('en');
    </script>
</body>
</html>
```

- [ ] **Step 2: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "from installer.install import app; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add installer/
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add setup wizard UI with EN/RU translations"
```

# Setup Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add setup wizard mode to FanControl Web — single Docker image shows wizard on first boot, then runs as server or agent.

**Architecture:** One Docker image with 3 modes (setup/server/agent). Auto-detect first boot via config.json existence. Setup wizard saves config and restarts container.

**Tech Stack:** Python 3, Flask, Tailwind CSS (CDN), vanilla JS

**Spec:** `docs/compose/specs/2026-06-19-setup-mode-design.md`

---

## File Structure

```
installer/
├── wizard.py              # Flask server for setup wizard
├── templates/
│   └── setup.html         # Setup wizard UI
└── __init__.py

app.py                     # Modify: add setup mode detection
Dockerfile                 # Modify: add installer to image
```

---

## Task 1: Update app.py — setup mode detection

**Covers:** [S2, S8]

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Read current app.py**

Read app.py to understand the current entry point structure. Key areas:
- `main()` function with argparse
- `_auto_init()` function
- Database/hardware initialization

- [ ] **Step 2: Add setup mode detection**

At the top of app.py, add setup mode check:

```python
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get('FANCONTROL_DATA_DIR', '/app/data')) / 'config.json'

def is_setup_needed():
    """Check if setup wizard should be shown."""
    return not CONFIG_PATH.exists()
```

Update `main()` function:

```python
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

    if args.mode == 'setup':
        from installer.wizard import run_wizard
        run_wizard()
        return

    # ... existing server/agent code ...
```

- [ ] **Step 3: Verify syntax**

```bash
cd /home/impulse/fancontrol-web && python3 -c "import app; print('OK')"
```

Note: Will fail due to Docker path, but syntax should be valid.

- [ ] **Step 4: Commit**

```bash
git add app.py
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add setup mode detection to app.py"
```

---

## Task 2: Create installer wizard Flask server

**Covers:** [S3, S4, S5]

**Files:**
- Create: `installer/__init__.py`
- Create: `installer/wizard.py`

- [ ] **Step 1: Create installer package**

```bash
touch installer/__init__.py
```

- [ ] **Step 2: Create installer/wizard.py**

```python
"""Setup wizard — Flask server for first-time configuration."""

import json
import os
import subprocess
import socket
from pathlib import Path
from flask import Flask, jsonify, request, render_template

app = Flask(__name__, template_folder='templates')

CONFIG_PATH = Path(os.environ.get('FANCONTROL_DATA_DIR', '/app/data')) / 'config.json'


@app.route('/')
def index():
    return render_template('setup.html')


@app.route('/api/config', methods=['POST'])
def save_config():
    """Save configuration and restart container."""
    config = request.get_json()

    # Validate required fields
    if config.get('mode') == 'agent':
        if not config.get('server_url'):
            return jsonify({'error': 'Server URL required'}), 400
        if not config.get('api_token'):
            return jsonify({'error': 'API token required'}), 400
        if not config.get('node_name'):
            return jsonify({'error': 'Node name required'}), 400

    # Add initialization flag
    config['initialized'] = True

    # Save config
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

    return jsonify({'status': 'saved', 'mode': config.get('mode', 'server')})


@app.route('/api/restart', methods=['POST'])
def restart_container():
    """Restart the Docker container."""
    try:
        # Get container hostname
        hostname = socket.gethostname()
        subprocess.run(['docker', 'restart', hostname], timeout=30)
        return jsonify({'status': 'restarting'})
    except Exception as e:
        return jsonify({'error': str(e), 'manual_restart': True}), 500


@app.route('/api/validate-token', methods=['POST'])
def validate_token():
    """Validate server URL and API token (for agent setup)."""
    data = request.get_json()
    server_url = data.get('server_url', '')
    api_token = data.get('api_token', '')

    # Simple validation — check if URL is reachable
    try:
        import urllib.parse
        parsed = urllib.parse.urlparse(server_url.replace('ws://', 'http://'))
        # Just check if host is reachable
        import socket as sock
        s = sock.socket(sock.AF_INET, sock.SOCK_STREAM)
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
```

- [ ] **Step 3: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "from installer.wizard import app; print('OK')"
```

- [ ] **Step 4: Commit**

```bash
git add installer/
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add setup wizard Flask server"
```

---

## Task 3: Create setup wizard UI

**Covers:** [S3, S6]

**Files:**
- Create: `installer/templates/setup.html`

- [ ] **Step 1: Create installer/templates/setup.html**

Create a modern setup wizard with:

1. **Header:** FanControl Web logo + "Setup Wizard"
2. **Step indicator:** 4 dots showing progress
3. **Step 1:** Language selection (EN/RU buttons)
4. **Step 2:** Mode selection (Server/Agent cards with icons)
5. **Step 3:** Configuration form (different fields per mode)
6. **Step 4:** Progress bar + restart countdown

Key features:
- Dark cyberpunk theme (matching FanControl style)
- Tailwind CSS via CDN
- Field validation before submit
- Descriptions for each field
- "Test Connection" button for agent URL
- Loading spinner during save
- Auto-restart countdown after save

The HTML should be self-contained with inline CSS/JS.

- [ ] **Step 2: Verify**

```bash
cd /home/impulse/fancontrol-web && python3 -c "from installer.wizard import app; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add installer/
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add setup wizard UI with Tailwind"
```

---

## Task 4: Update Dockerfile

**Covers:** [S7]

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Read current Dockerfile**

Read Dockerfile to understand current structure.

- [ ] **Step 2: Update Dockerfile**

Add installer to the image and update CMD:

```dockerfile
# ... existing FROM, RUN, COPY ...

# Copy installer
COPY installer/ /app/installer/

# ... existing COPY ...

# Default to setup mode (auto-detects first boot)
ENV MODE=setup

CMD ["gunicorn", "-k", "eventlet", "-w", "1", "--bind", "0.0.0.0:5059", "app:app"]
```

- [ ] **Step 3: Verify Dockerfile syntax**

```bash
cd /home/impulse/fancontrol-web && head -30 Dockerfile
```

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: add installer to Docker image"
```

---

## Task 5: Update docker-compose files

**Covers:** [S7]

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker-compose.agent.yml`

- [ ] **Step 1: Update docker-compose.yml**

Remove hardcoded MODE env var (now auto-detected):

```yaml
services:
  fancontrol:
    build: .
    container_name: fancontrol-web
    restart: unless-stopped
    network_mode: host
    privileged: true
    cap_add:
      - SYS_RAWIO
      - SYS_ADMIN
    volumes:
      - /sys:/sys:rw
      - /dev:/dev:rw
      - /volume1/docker/fancontrol-web/data:/data
      - /volume1/docker/fancontrol-web:/repo
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - FANCONTROL_CORS_ORIGINS=*
```

- [ ] **Step 2: Update docker-compose.agent.yml**

```yaml
services:
  fancontrol-agent:
    image: fancontrol-web
    container_name: fancontrol-agent
    restart: unless-stopped
    network_mode: host
    privileged: true
    cap_add:
      - SYS_RAWIO
      - SYS_ADMIN
    volumes:
      - /sys:/sys:rw
      - /dev:/dev:rw
      - ./data-agent:/data
    environment:
      - MODE=setup  # Auto-detects first boot
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml docker-compose.agent.yml
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "feat: update Docker Compose for setup mode"
```

---

## Task 6: Clean up old installer

**Covers:** [S8]

**Files:**
- Delete: `installer/install.py` (replaced by wizard.py)
- Delete: `installer/requirements.txt` (no longer needed)

- [ ] **Step 1: Remove old installer files**

```bash
rm -f installer/install.py installer/requirements.txt
```

- [ ] **Step 2: Commit**

```bash
git add -A installer/
GIT_AUTHOR_NAME="biowolfx" GIT_AUTHOR_EMAIL="biowolfx@gmail.com" \
GIT_COMMITTER_NAME="biowolfx" GIT_COMMITTER_EMAIL="biowolfx@gmail.com" \
git commit -m "chore: remove old standalone installer"
```

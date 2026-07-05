"""Agent WebSocket event handlers — respond to server commands."""

import logging

from core.state import state, state_lock, invalidate_state_cache
from agent.telemetry import get_local_config, apply_server_config
from agent.config import save_local_config, persist_node_id

logger = logging.getLogger('fancontrol')


def make_handlers(sio_ref):
    """Create handler functions bound to the socketio client instance.

    Returns a dict of event_name → handler_function.
    """
    def _on_connect():
        from agent.telemetry import get_local_config
        logger.info(f'Connected to server')
        state['server_connected'] = True
        invalidate_state_cache()

        sio_ref.emit('agent:connect', {
            'node_id': state.get('node_id'),
            'node_name': state.get('node_name'),
            'api_token': state.get('api_token'),
            'control_mode': state['control_mode'],
            'config': get_local_config(),
        })

    def _on_disconnect():
        logger.warning('Disconnected from server')
        state['server_connected'] = False
        invalidate_state_cache()

    def _on_config_push(data):
        with state_lock:
            if state['control_mode'] != 'server':
                logger.info('Config push ignored — in manual mode')
                return
            state['agent_config_snapshot'] = get_local_config()
            apply_server_config(data.get('config', {}))
            invalidate_state_cache()
            logger.info('Applied server config')
        save_local_config()

    def _on_set_control_mode(data):
        mode = data.get('mode', 'server')
        with state_lock:
            state['control_mode'] = mode
            invalidate_state_cache()
        logger.info(f'Control mode set to: {mode}')
        save_local_config()

    def _on_command(data):
        cmd = data.get('command')
        if cmd == 'set_fan':
            fan_id = data.get('fan_id')
            value = data.get('value')
            with state_lock:
                if fan_id in state['fans']:
                    state['fans'][fan_id]['manual_pct'] = value
                    state['fans'][fan_id]['mode'] = 'manual'
            invalidate_state_cache()
            from core.hardware import set_pwm
            set_pwm(fan_id, int(value * 255 // 100))

    def _on_node_id_push(data):
        new_node_id = data.get('node_id', '')
        new_token = data.get('token', '')
        changed = False

        if new_node_id and new_node_id != state.get('node_id'):
            logger.info(f'Received node_id from server: {state.get("node_id")} → {new_node_id}')
            state['node_id'] = new_node_id
            changed = True

        if new_token and new_token != state.get('api_token'):
            state['api_token'] = new_token
            changed = True

        if changed:
            persist_node_id(new_node_id, new_token)

    def _on_dsm_apply(data):
        scheme_type = data.get('scheme_type')
        entries = data.get('entries', [])
        logger.info(f'Received DSM scheme apply: {scheme_type} ({len(entries)} entries)')
        try:
            from core.dsm_fan import update_scheme_entry
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

    def _on_update(data):
        """Server requests agent to update itself — git pull + restart."""
        logger.info('=== AGENT UPDATE RECEIVED ===')
        import subprocess
        import os

        repo_dir = '/repo'

        git_dir = os.path.join(repo_dir, '.git')
        if not os.path.isdir(git_dir):
            logger.warning(f'Agent /repo is not a git repo. Cannot auto-update.')
            return

        def _do_update():
            try:
                logger.info('[agent-update] Fetching latest code...')
                result = subprocess.run(
                    ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
                )
                logger.info(f'[agent-update] fetch: rc={result.returncode}')

                reset = subprocess.run(
                    ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
                )
                logger.info(f'[agent-update] reset: rc={reset.returncode} {reset.stdout.strip()[:200]}')

                if reset.returncode != 0:
                    logger.error(f'[agent-update] Git reset failed: {reset.stderr[:200]}')
                    return

                # Verify new version
                try:
                    with open(os.path.join(repo_dir, 'core', 'state.py')) as f:
                        for line in f:
                            if 'CONFIG_VERSION' in line:
                                logger.info(f'[agent-update] New version: {line.strip()}')
                                break
                except Exception:
                    pass

                logger.info('[agent-update] Restarting container...')
                # Multiple restart methods for reliability
                try:
                    # Method 1: Docker API (if docker.sock mounted)
                    import urllib.request
                    import json as _json
                    container_id = open('/proc/self/cgroup').read().split('/')[-1].strip()
                    req = urllib.request.Request(
                        f'http://unix/containers/{container_id}/restart',
                        method='POST',
                        data=_json.dumps({'t': 1}).encode(),
                    )
                    import socket
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect('/var/run/docker.sock')
                    sock.send(req.to_bytes())
                    sock.recv(4096)
                    sock.close()
                    logger.info('[agent-update] Restarted via Docker API')
                    return
                except Exception as e:
                    logger.debug(f'[agent-update] Docker API restart failed: {e}')

                # Method 2: Kill own process
                import signal
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception as e:
                logger.error(f'[agent-update] Failed: {e}', exc_info=True)

        import threading
        threading.Thread(target=_do_update, daemon=True).start()

    return {
        'connect': _on_connect,
        'disconnect': _on_disconnect,
        'server:config_push': _on_config_push,
        'server:set_control_mode': _on_set_control_mode,
        'server:command': _on_command,
        'server:node_id_push': _on_node_id_push,
        'server:dsm:apply': _on_dsm_apply,
        'server:update': _on_update,
    }

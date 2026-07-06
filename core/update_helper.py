"""Shared update logic — git pull, sync /repo to /app, restart.

Used by both server (routes.py) and agent (handlers.py) to avoid
~100 lines of duplicated code.
"""

import logging
import os
import shutil
import subprocess
import threading

logger = logging.getLogger('fancontrol')

GIT_ENV = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}


def do_git_pull(repo_dir='/repo'):
    """Fetch + reset --hard origin/main.

    Returns (success, version_string) where version_string is the
    CONFIG_VERSION read from the repo after pull (or '' on failure).
    """
    # Step 1: fetch
    fetch = subprocess.run(
        ['git', '-C', repo_dir, 'fetch', 'origin', 'main'],
        capture_output=True, text=True, timeout=60, env=GIT_ENV,
    )
    if fetch.returncode != 0:
        logger.error(f'[update] git fetch failed: {fetch.stderr.strip()[:300]}')
        return False, ''

    # Step 2: reset
    reset = subprocess.run(
        ['git', '-C', repo_dir, 'reset', '--hard', 'origin/main'],
        capture_output=True, text=True, timeout=60, env=GIT_ENV,
    )
    output = (reset.stdout + '\n' + reset.stderr).strip()
    logger.info(f'[update] git reset: rc={reset.returncode}, output={output[:300]}')
    if reset.returncode != 0:
        return False, ''

    # Step 3: read version
    version = _read_version_from_repo(repo_dir)
    return True, version


def sync_repo_to_app(repo_dir='/repo', app_dir='/app'):
    """Copy changed files from /repo to /app.

    Returns list of synced item names for logging.
    """
    synced = []

    # Root-level files
    for f in os.listdir(repo_dir):
        if f.endswith('.py') or f.endswith('.txt') or f in ('Dockerfile', 'docker-compose.yml'):
            src = os.path.join(repo_dir, f)
            dst = os.path.join(app_dir, f)
            if os.path.isfile(src):
                shutil.copy2(src, dst)
                synced.append(f)

    # Subdirectories
    for d in ('templates', 'static', 'core', 'server', 'agent', 'installer', 'tests'):
        src = os.path.join(repo_dir, d)
        dst = os.path.join(app_dir, d)
        if os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            synced.append(f'{d}/')

    logger.info(f'[update] synced {len(synced)} items: {", ".join(synced[:15])}')
    return synced


def schedule_restart(delay=1.0):
    """Schedule os._exit(0) after delay. Triggers Docker restart: unless-stopped."""
    def _exit():
        logger.info('[update] os._exit(0) called')
        os._exit(0)
    threading.Timer(delay, _exit).start()
    logger.info(f'[update] restart scheduled in {delay}s')


def _read_version_from_repo(repo_dir):
    """Extract CONFIG_VERSION string from repo's core/state.py."""
    try:
        with open(os.path.join(repo_dir, 'core', 'state.py')) as f:
            for line in f:
                if 'CONFIG_VERSION' in line:
                    return line.strip()
    except Exception:
        pass
    return ''

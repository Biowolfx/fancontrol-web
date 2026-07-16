"""Thread lifecycle manager — single point for background task management.

Provides register/start/stop/restart for all background loops.
Each task is a simple callable that runs in a daemon thread with configurable interval.
"""

import logging
import threading
import time
from typing import Callable, Dict

logger = logging.getLogger('fancontrol')

_lock = threading.Lock()
_tasks: Dict[str, dict] = {}


def register(name: str, target: Callable, interval: float):
    """Register a background loop task. Does not start it."""
    with _lock:
        if name in _tasks:
            logger.warning(f'[lifecycle] Task {name} already registered, ignoring')
            return
        _tasks[name] = {
            'target': target,
            'interval': interval,
            'thread': None,
            'stop_event': threading.Event(),
            'started': False,
        }
    logger.debug(f'[lifecycle] Task {name} registered (interval={interval}s)')


def start(name: str):
    """Start a registered task. No-op if already running."""
    with _lock:
        task = _tasks.get(name)
        if not task:
            logger.error(f'[lifecycle] Task {name} not registered')
            return
        if task['started'] and task['thread'] and task['thread'].is_alive():
            return

        task['stop_event'].clear()

        def _wrapper():
            while not task['stop_event'].is_set():
                try:
                    task['target']()
                except Exception as e:
                    logger.error(f'[lifecycle] Task {name} error: {e}', exc_info=True)
                task['stop_event'].wait(task['interval'])

        t = threading.Thread(target=_wrapper, daemon=True, name=f'lifecycle-{name}')
        t.start()
        task['thread'] = t
        task['started'] = True
    logger.info(f'[lifecycle] Task {name} started (interval={task["interval"]}s)')


def stop(name: str):
    """Stop a running task."""
    with _lock:
        task = _tasks.get(name)
        if task and task['started']:
            task['stop_event'].set()
            task['started'] = False
            logger.info(f'[lifecycle] Task {name} stopped')


def restart(name: str):
    """Stop then start a task."""
    stop(name)
    time.sleep(0.1)
    start(name)


def status() -> Dict[str, dict]:
    """Return status of all registered tasks."""
    with _lock:
        return {
            name: {
                'running': t['thread'].is_alive() if t['thread'] else False,
                'interval': t['interval'],
                'started': t['started'],
            }
            for name, t in _tasks.items()
        }

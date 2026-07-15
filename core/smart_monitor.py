"""SMART monitoring — periodic disk attribute polling with SQLite storage."""

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from core.state import state, state_lock
from core.hardware import read_disk_smart

logger = logging.getLogger('fancontrol')

SMART_MONITOR_INTERVAL = 300  # seconds (5 min)
SMART_RETENTION_DAYS = 90

_db_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def init_smart_monitor(db_path: str):
    """Create smart_history table if it doesn't exist."""
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS smart_history (
                disk_id TEXT NOT NULL,
                attr_key TEXT NOT NULL,
                raw_value REAL NOT NULL,
                ts TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_smart_history_lookup
            ON smart_history(disk_id, attr_key, ts)
        ''')
        conn.commit()
        conn.close()
        logger.info('smart_history table initialized')
    except Exception as e:
        logger.error(f'Failed to init smart_history: {e}')


def _get_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def start_smart_monitor(db_path: str):
    """Start background SMART monitoring thread."""
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _stop_event.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop, args=(db_path,), daemon=True, name='smart-monitor'
    )
    _monitor_thread.start()
    logger.info('SMART monitor started')


def stop_smart_monitor():
    """Stop background SMART monitoring thread."""
    _stop_event.set()
    logger.info('SMART monitor stopped')


def enable_monitoring(disk_id: str):
    """Add disk to monitored set."""
    with state_lock:
        state['smart_monitored_disks'].add(disk_id)
    logger.info(f'SMART monitoring enabled for {disk_id}')


def disable_monitoring(disk_id: str):
    """Remove disk from monitored set."""
    with state_lock:
        state['smart_monitored_disks'].discard(disk_id)
    logger.info(f'SMART monitoring disabled for {disk_id}')


def get_monitored_disks() -> List[str]:
    """Return list of monitored disk IDs."""
    with state_lock:
        return list(state['smart_monitored_disks'])


def _monitor_loop(db_path: str):
    """Background loop: poll SMART for monitored disks, write to SQLite."""
    while not _stop_event.is_set():
        try:
            _monitor_tick(db_path)
        except Exception as e:
            logger.error(f'SMART monitor tick error: {e}', exc_info=True)
        _stop_event.wait(SMART_MONITOR_INTERVAL)


def _monitor_tick(db_path: str):
    """One tick: poll all monitored disks and write to DB."""
    monitored = get_monitored_disks()
    if not monitored:
        return

    with state_lock:
        hdd_sensors = dict(state.get('hdd_sensors', {}))
        dashboard = state.get('dashboard', {})

    # Build map: disk_id -> list of attr_keys to monitor
    # Key by source:sourceId to avoid agent cards overwriting server cards
    cards = dashboard.get('cards', [])
    disk_attr_map: Dict[str, List[str]] = {}
    disk_source_map: Dict[str, str] = {}  # disk_id -> source
    for card in cards:
        if card.get('type') != 'disk':
            continue
        did = card.get('sourceId')
        source = card.get('source', 'local')
        if did not in monitored:
            continue
        # Only monitor local disks (read_disk_smart runs on this server)
        if source != 'local':
            continue
        attr_keys = card.get('smartMonitored') or card.get('smartAttributes', [])
        if attr_keys:
            if did not in disk_attr_map:
                disk_attr_map[did] = []
            # Merge attr_keys (union) to avoid overwrite
            existing = set(disk_attr_map[did])
            for k in attr_keys:
                if k not in existing:
                    disk_attr_map[did].append(k)
                    existing.add(k)

    if not disk_attr_map:
        return

    now_iso = datetime.now().isoformat()
    rows = []

    for disk_id, attr_keys in disk_attr_map.items():
        disk_info = hdd_sensors.get(disk_id)
        if not disk_info:
            continue
        device = disk_info.get('dev_name') or disk_info.get('device', '')
        if not device:
            continue

        try:
            result = read_disk_smart(device)
            if 'error' in result:
                logger.warning(f'SMART monitor: {disk_id}: {result["error"]}')
                continue

            attr_type = result.get('attr_type', 'sata')
            attributes = result.get('attributes', [])

            if attr_type == 'sata':
                # attributes is a list of dicts with 'id' as int
                for attr in attributes:
                    attr_id_str = str(attr.get('id'))
                    if attr_id_str in attr_keys:
                        raw_val = attr.get('raw_num', 0)
                        rows.append((disk_id, attr_id_str, float(raw_val), now_iso))
            else:
                # NVMe: attributes is a dict keyed by name
                if isinstance(attributes, dict):
                    for key in attr_keys:
                        if key in attributes:
                            val = attributes[key].get('value', 0)
                            try:
                                val = float(val)
                            except (ValueError, TypeError):
                                val = 0
                            rows.append((disk_id, key, val, now_iso))

        except Exception as e:
            logger.error(f'SMART monitor: {disk_id} failed: {e}')

    if rows:
        _write_history(db_path, rows)
        logger.debug(f'SMART monitor: wrote {len(rows)} rows for {len(disk_attr_map)} disks')


def _write_history(db_path: str, rows: list):
    """Write SMART history rows to SQLite."""
    with _db_lock:
        try:
            conn = _get_db(db_path)
            conn.executemany(
                'INSERT INTO smart_history (disk_id, attr_key, raw_value, ts) VALUES (?, ?, ?, ?)',
                rows
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f'SMART history write error: {e}')


def get_smart_history(db_path: str, disk_id: str, attr_key: str,
                       from_ts: Optional[str] = None, to_ts: Optional[str] = None,
                       limit: int = 2000) -> list:
    """Query SMART history for a specific attribute."""
    query = 'SELECT raw_value, ts FROM smart_history WHERE disk_id = ? AND attr_key = ?'
    params: list = [disk_id, attr_key]

    if from_ts:
        query += ' AND ts >= ?'
        params.append(from_ts)
    if to_ts:
        query += ' AND ts <= ?'
        params.append(to_ts)

    query += ' ORDER BY ts ASC LIMIT ?'
    params.append(limit)

    with _db_lock:
        try:
            conn = _get_db(db_path)
            cursor = conn.execute(query, params)
            result = [{'value': row[0], 'ts': row[1]} for row in cursor.fetchall()]
            conn.close()
            return result
        except Exception as e:
            logger.error(f'SMART history query error: {e}')
            return []


def get_monitoring_start_date(db_path: str, disk_id: str) -> Optional[str]:
    """Get earliest monitoring timestamp for a disk."""
    with _db_lock:
        try:
            conn = _get_db(db_path)
            cursor = conn.execute(
                'SELECT MIN(ts) FROM smart_history WHERE disk_id = ?', (disk_id,)
            )
            row = cursor.fetchone()
            conn.close()
            return row[0] if row and row[0] else None
        except Exception as e:
            logger.error(f'SMART start date query error: {e}')
            return None


def cleanup_old_smart_data(db_path: str, retention_days: int = SMART_RETENTION_DAYS):
    """Remove SMART history older than retention_days."""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    with _db_lock:
        try:
            conn = _get_db(db_path)
            conn.execute('DELETE FROM smart_history WHERE ts < ?', (cutoff,))
            conn.commit()
            deleted = conn.total_changes
            conn.close()
            logger.info(f'Cleaned SMART history older than {retention_days} days')
        except Exception as e:
            logger.error(f'SMART cleanup error: {e}')

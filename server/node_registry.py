"""Node registry — SQLite storage for registered agents."""

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from core.config import DATA_DIR

logger = logging.getLogger('fancontrol')

_db_path = DATA_DIR / 'nodes.db'
_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


def _row_to_dict(row: sqlite3.Row) -> Dict:
    d = dict(row)
    for field in ('config', 'telemetry', 'agent_config_snapshot'):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def init_nodes_table():
    with _lock:
        conn = _get_conn()
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    api_token TEXT UNIQUE NOT NULL,
                    config TEXT DEFAULT '{}',
                    telemetry TEXT DEFAULT '{}',
                    control_mode TEXT DEFAULT 'server',
                    status TEXT DEFAULT 'offline',
                    last_seen TEXT,
                    agent_config_snapshot TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
        finally:
            conn.close()


def add_node(name: str, api_token: Optional[str] = None) -> Dict:
    if not api_token:
        api_token = uuid.uuid4().hex
    node_id = name.lower().replace(' ', '-')
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'INSERT INTO nodes (node_id, name, api_token) VALUES (?, ?, ?)',
                (node_id, name, api_token)
            )
            conn.commit()
            row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()


def get_node(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def get_node_by_token(api_token: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute('SELECT * FROM nodes WHERE api_token = ?', (api_token,)).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def list_nodes() -> List[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            rows = conn.execute('SELECT * FROM nodes ORDER BY created_at DESC').fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()


def delete_node(node_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            cursor = conn.execute('DELETE FROM nodes WHERE node_id = ?', (node_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def update_node_status(node_id: str, status: str, telemetry: Optional[Dict] = None) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            now = datetime.utcnow().isoformat()
            if telemetry is not None:
                conn.execute(
                    'UPDATE nodes SET status = ?, telemetry = ?, last_seen = ? WHERE node_id = ?',
                    (status, json.dumps(telemetry), now, node_id)
                )
            else:
                conn.execute(
                    'UPDATE nodes SET status = ?, last_seen = ? WHERE node_id = ?',
                    (status, now, node_id)
                )
            conn.commit()
            return conn.execute('SELECT changes()').fetchone()[0] > 0
        finally:
            conn.close()


def update_node_config(node_id: str, config: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'UPDATE nodes SET config = ? WHERE node_id = ?',
                (json.dumps(config), node_id)
            )
            conn.commit()
            return conn.execute('SELECT changes()').fetchone()[0] > 0
        finally:
            conn.close()


def update_node_control_mode(node_id: str, mode: str) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'UPDATE nodes SET control_mode = ? WHERE node_id = ?',
                (mode, node_id)
            )
            conn.commit()
            return conn.execute('SELECT changes()').fetchone()[0] > 0
        finally:
            conn.close()


def save_agent_snapshot(node_id: str, snapshot: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                'UPDATE nodes SET agent_config_snapshot = ? WHERE node_id = ?',
                (json.dumps(snapshot), node_id)
            )
            conn.commit()
            return conn.execute('SELECT changes()').fetchone()[0] > 0
        finally:
            conn.close()


def get_agent_snapshot(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                'SELECT agent_config_snapshot FROM nodes WHERE node_id = ?',
                (node_id,)
            ).fetchone()
            if row and row['agent_config_snapshot']:
                return json.loads(row['agent_config_snapshot'])
            return None
        finally:
            conn.close()

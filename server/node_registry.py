"""Node registry — SQLite storage for registered agents."""

import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from core.config import DATA_DIR

logger = logging.getLogger('fancontrol')

_db_path = DATA_DIR / 'nodes.db'
_lock = threading.Lock()
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Thread-local persistent connection with WAL pragmas."""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(_db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA journal_size_limit=10485760')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=5000')
        _local.conn = conn
    return conn


def _row_to_dict(row: sqlite3.Row) -> Dict:
    d = dict(row)
    for field in ('config', 'telemetry', 'agent_config_snapshot'):
        if d.get(field):
            try:
                d[field] = json.loads(d[field])
            except (json.JSONDecodeError, TypeError):
                logger.warning(f'Corrupted JSON in {field} for node {d.get("node_id", "?")}, using empty dict')
                d[field] = {}
    return d


def init_nodes_table():
    with _lock:
        conn = _get_conn()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                stable_id TEXT UNIQUE,
                name TEXT NOT NULL,
                api_token TEXT UNIQUE NOT NULL,
                ip TEXT DEFAULT '',
                port INTEGER DEFAULT 5059,
                config TEXT DEFAULT '{}',
                telemetry TEXT DEFAULT '{}',
                control_mode TEXT DEFAULT 'server',
                status TEXT DEFAULT 'offline',
                last_seen TEXT,
                agent_config_snapshot TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cols = [r[1] for r in conn.execute('PRAGMA table_info(nodes)').fetchall()]
        logger.info(f'[init_nodes_table] Existing columns: {cols}')
        
        migrations = [
            ('ip', "ALTER TABLE nodes ADD COLUMN ip TEXT DEFAULT ''"),
            ('port', "ALTER TABLE nodes ADD COLUMN port INTEGER DEFAULT 5059"),
            ('agent_version', "ALTER TABLE nodes ADD COLUMN agent_version TEXT DEFAULT ''"),
            ('pending_update', "ALTER TABLE nodes ADD COLUMN pending_update INTEGER DEFAULT 0"),
            ('auto_update', "ALTER TABLE nodes ADD COLUMN auto_update INTEGER DEFAULT 0"),
            # UNIQUE removed from ALTER TABLE — SQLite may not support it.
            # Uniqueness enforced by add_node() IntegrityError handling.
            ('stable_id', "ALTER TABLE nodes ADD COLUMN stable_id TEXT DEFAULT ''"),
        ]
        for col_name, sql in migrations:
            if col_name not in cols:
                try:
                    conn.execute(sql)
                    conn.commit()
                    logger.info(f'[init_nodes_table] Added column: {col_name}')
                except Exception as e:
                    logger.warning(f'[init_nodes_table] Column {col_name} migration failed: {e}')
        
        # Generate stable_id for existing nodes that don't have one
        try:
            for row in conn.execute("SELECT node_id FROM nodes WHERE stable_id IS NULL OR stable_id = ''").fetchall():
                sid = uuid.uuid4().hex[:12]
                conn.execute('UPDATE nodes SET stable_id = ? WHERE node_id = ?', (sid, row[0]))
                logger.info(f'[init_nodes_table] Generated stable_id={sid} for node {row[0]}')
        except Exception as e:
            logger.warning(f'[init_nodes_table] stable_id generation failed: {e}')
        
        conn.commit()


def add_node(name: str, api_token: Optional[str] = None, ip: str = '', port: int = 5059) -> Dict:
    if not api_token:
        api_token = uuid.uuid4().hex
    # Sanitize node_id: lowercase, replace spaces with hyphens, remove special chars
    node_id = re.sub(r'[^a-z0-9\-]', '', name.lower().replace(' ', '-'))
    if not node_id:
        node_id = f'node-{uuid.uuid4().hex[:8]}'
    
    with _lock:
        conn = _get_conn()
        # Try up to 3 times with unique stable_id
        for attempt in range(3):
            stable_id = uuid.uuid4().hex[:12]
            try:
                conn.execute(
                    'INSERT INTO nodes (node_id, stable_id, name, api_token, ip, port) VALUES (?, ?, ?, ?, ?, ?)',
                    (node_id, stable_id, name, api_token, ip, port)
                )
                conn.commit()
                row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
                return _row_to_dict(row)
            except sqlite3.IntegrityError as e:
                err_str = str(e).lower()
                if 'node_id' in err_str:
                    # node_id collision — add suffix
                    node_id = f'{node_id}-{uuid.uuid4().hex[:4]}'
                elif 'stable_id' in err_str:
                    # stable_id collision — retry with new one
                    continue
                elif 'api_token' in err_str:
                    # api_token collision — return existing node
                    existing = get_node_by_token(api_token)
                    if existing:
                        logger.info(f'add_node: api_token collision, returning existing node {existing["node_id"]}')
                        return existing
                    logger.error(f'add_node api_token collision but node not found')
                    raise
                else:
                    logger.error(f'add_node IntegrityError: {e}')
                    raise
            except Exception as e:
                logger.error(f'add_node failed: {e}')
                raise
        raise Exception('Failed to add node after 3 attempts')


def get_node(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT * FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_node_by_token(api_token: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT * FROM nodes WHERE api_token = ?', (api_token,)).fetchone()
        return _row_to_dict(row) if row else None


def get_node_by_stable_id(stable_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute('SELECT * FROM nodes WHERE stable_id = ?', (stable_id,)).fetchone()
        return _row_to_dict(row) if row else None


def list_nodes() -> List[Dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute('SELECT * FROM nodes ORDER BY created_at DESC').fetchall()
        return [_row_to_dict(r) for r in rows]


def delete_node(node_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        cursor = conn.execute('DELETE FROM nodes WHERE node_id = ?', (node_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_node(node_id: str, name: Optional[str] = None, ip: Optional[str] = None,
                port: Optional[int] = None, api_token: Optional[str] = None) -> bool:
    with _lock:
        conn = _get_conn()
        updates = []
        params = []
        if name is not None:
            updates.append('name = ?')
            params.append(name)
        if ip is not None:
            updates.append('ip = ?')
            params.append(ip)
        if port is not None:
            updates.append('port = ?')
            params.append(port)
        if api_token is not None:
            updates.append('api_token = ?')
            params.append(api_token)
        if not updates:
            return False
        params.append(node_id)
        cursor = conn.execute(
            f'UPDATE nodes SET {", ".join(updates)} WHERE node_id = ?',
            params
        )
        conn.commit()
        return cursor.rowcount > 0


def update_node_status(node_id: str, status: str, telemetry: Optional[Dict] = None) -> bool:
    with _lock:
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()
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


def update_node_config(node_id: str, config: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET config = ? WHERE node_id = ?',
            (json.dumps(config), node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def update_node_control_mode(node_id: str, mode: str) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET control_mode = ? WHERE node_id = ?',
            (mode, node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def update_node_flags(node_id: str, pending_update: Optional[bool] = None,
                      auto_update: Optional[bool] = None) -> bool:
    with _lock:
        conn = _get_conn()
        updates = []
        params = []
        if pending_update is not None:
            updates.append('pending_update = ?')
            params.append(1 if pending_update else 0)
        if auto_update is not None:
            updates.append('auto_update = ?')
            params.append(1 if auto_update else 0)
        if not updates:
            return False
        params.append(node_id)
        conn.execute(
            f'UPDATE nodes SET {", ".join(updates)} WHERE node_id = ?',
            params
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def update_node_version(node_id: str, version: str) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET agent_version = ? WHERE node_id = ?',
            (version, node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def save_agent_snapshot(node_id: str, snapshot: Dict) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute(
            'UPDATE nodes SET agent_config_snapshot = ? WHERE node_id = ?',
            (json.dumps(snapshot), node_id)
        )
        conn.commit()
        return conn.execute('SELECT changes()').fetchone()[0] > 0


def get_agent_snapshot(node_id: str) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            'SELECT agent_config_snapshot FROM nodes WHERE node_id = ?',
            (node_id,)
        ).fetchone()
        if row and row['agent_config_snapshot']:
            try:
                return json.loads(row['agent_config_snapshot'])
            except (json.JSONDecodeError, TypeError):
                logger.warning(f'Corrupted agent snapshot for {node_id}')
                return None
        return None

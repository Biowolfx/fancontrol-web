"""Additional unit tests — state versioning, SMART monitor, blueprints, models, stability."""

import os
import sys
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ============================================================================
# State Version Counter
# ============================================================================

class TestStateVersion:
    """Tests for state version counter and snapshot caching."""

    def test_bump_increments_version(self):
        from core.state import _state_version, bump_state_version
        old = _state_version
        bump_state_version()
        from core.state import _state_version as new_ver
        assert new_ver > old
        # Restore
        from core.state import _cached_state
        import core.state as st
        st._state_version = old

    def test_bump_invalidates_cache(self):
        from core.state import bump_state_version, get_state, _cached_state
        import core.state as st
        # Force cache population
        get_state()
        assert st._cached_state is not None
        bump_state_version()
        # Cache should be invalidated
        assert st._cached_state is None

    def test_get_state_returns_fresh_snapshot(self):
        from core.state import get_state
        s1 = get_state()
        s2 = get_state()
        # Should be different dict objects (not same reference)
        assert s1 is not s2
        # But same content
        assert s1.get('config_version') == s2.get('config_version')

    def test_state_snapshot_has_required_keys(self):
        from core.state import get_state
        s = get_state()
        required = ['fans', 'temp_sensors', 'hdd_sensors', 'config_version',
                     'nodes', 'dashboard', 'auto_register_agents', 'agent_mode']
        for key in required:
            assert key in s, f"Missing key: {key}"


# ============================================================================
# Mark State Dirty
# ============================================================================

class TestDirtyFlag:
    """Tests for mark_state_dirty."""

    def test_mark_dirty_sets_flag(self):
        from core.state import mark_state_dirty, _state_dirty
        import core.state as st
        st._state_dirty = False
        mark_state_dirty()
        assert st._state_dirty is True
        st._state_dirty = False


# ============================================================================
# SMART Monitor SQLite
# ============================================================================

class TestSmartMonitorSQLite:
    """Tests for SMART monitor data storage (with temp DB)."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        from core.smart_monitor import init_smart_monitor
        init_smart_monitor(db_path)
        yield db_path
        os.unlink(db_path)

    def test_init_creates_table(self, temp_db):
        import sqlite3
        conn = sqlite3.connect(temp_db)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert any('smart_history' in t[0] for t in tables)
        conn.close()

    def test_write_and_read_history(self, temp_db):
        from core.smart_monitor import _write_history, get_smart_history
        rows = [
            ('disk-1', '5', 100.0, '2026-01-01T00:00:00'),
            ('disk-1', '5', 101.0, '2026-01-01T00:05:00'),
            ('disk-1', '9', 5000.0, '2026-01-01T00:05:00'),
        ]
        _write_history(temp_db, rows)
        history = get_smart_history(temp_db, 'disk-1', '5')
        assert len(history) == 2
        assert history[0]['value'] == 100.0
        assert history[1]['value'] == 101.0

    def test_read_empty_history(self, temp_db):
        from core.smart_monitor import get_smart_history
        history = get_smart_history(temp_db, 'nonexistent', '5')
        assert history == []

    def test_start_date(self, temp_db):
        from core.smart_monitor import _write_history, get_monitoring_start_date
        rows = [('disk-1', '5', 100.0, '2026-06-01T10:00:00')]
        _write_history(temp_db, rows)
        start = get_monitoring_start_date(temp_db, 'disk-1')
        assert start is not None
        assert '2026-06-01' in start

    def test_start_date_empty(self, temp_db):
        from core.smart_monitor import get_monitoring_start_date
        assert get_monitoring_start_date(temp_db, 'nonexistent') is None

    def test_enable_disable_monitoring(self):
        from core.smart_monitor import enable_monitoring, disable_monitoring, get_monitored_disks
        enable_monitoring('disk-test')
        assert 'disk-test' in get_monitored_disks()
        disable_monitoring('disk-test')
        assert 'disk-test' not in get_monitored_disks()


# ============================================================================
# Flask Blueprint Routing
# ============================================================================

class TestBlueprintRouting:
    """Verify all critical endpoints are accessible after routes split."""

    @pytest.fixture
    def client(self):
        from app import app
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_index(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_state(self, client):
        resp = client.get('/api/state')
        assert resp.status_code == 200
        assert 'fans' in resp.get_json()

    def test_health(self, client):
        resp = client.get('/api/health')
        assert resp.status_code == 200

    def test_settings(self, client):
        resp = client.get('/api/settings')
        assert resp.status_code == 200

    def test_dashboard(self, client):
        resp = client.get('/api/dashboard')
        assert resp.status_code == 200

    def test_telemetry_rejects_no_token(self, client):
        resp = client.post('/api/agent/telemetry', json={})
        assert resp.status_code == 400

    def test_update_poll(self, client):
        resp = client.post('/api/update/poll', json={
            'agent_version': '0.0.0', 'node_id': 'test'
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'should_update' in data

    def test_smart_monitor_list(self, client):
        resp = client.get('/api/smart/monitor')
        assert resp.status_code == 200
        assert 'monitored' in resp.get_json()

    def test_smart_history_empty(self, client):
        resp = client.get('/api/smart/history/nonexistent?attr=5')
        assert resp.status_code == 200
        assert resp.get_json()['count'] == 0

    def test_debug_endpoint(self, client):
        resp = client.get('/api/debug')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'server' in data
        assert 'agents' in data

    def test_nodes_list(self, client):
        resp = client.get('/api/nodes')
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)


# ============================================================================
# Dataclasses
# ============================================================================

class TestDataclasses:
    """Tests for core/models.py dataclasses."""

    def test_fan_config_from_dict(self):
        from core.models import FanConfig
        d = {'id': 'fan1', 'label': 'CPU Fan', 'mode': 'auto', 'writable': True}
        fc = FanConfig.from_dict(d)
        assert fc.id == 'fan1'
        assert fc.mode == 'auto'

    def test_fan_config_defaults(self):
        from core.models import FanConfig
        fc = FanConfig()
        assert fc.mode == 'manual'
        assert fc.writable is False
        assert fc.sensors == []

    def test_node_info_from_dict(self):
        from core.models import NodeInfo
        d = {'node_id': 'agent-1', 'name': 'NAS Agent', 'ip': '192.168.0.101'}
        ni = NodeInfo.from_dict(d)
        assert ni.node_id == 'agent-1'
        assert ni.port == 5059

    def test_telemetry_payload_from_dict(self):
        from core.models import TelemetryPayload
        d = {
            'api_token': 'tok123',
            'node_id': 'agent-1',
            'telemetry': {
                'fans': {'fan1': {'rpm': 1000}},
                'temp_sensors': {'t1': {'value': 45}},
            }
        }
        tp = TelemetryPayload.from_dict(d)
        assert tp.api_token == 'tok123'
        assert tp.fans == {'fan1': {'rpm': 1000}}
        assert tp.temp_sensors == {'t1': {'value': 45}}

    def test_dashboard_card_from_dict(self):
        from core.models import DashboardCard
        d = {'id': 'picker-local-fan1', 'type': 'fan', 'source': 'local', 'sourceId': 'fan1'}
        dc = DashboardCard.from_dict(d)
        assert dc.type == 'fan'
        assert dc.colSpan == 3

    def test_dashboard_card_disk_defaults(self):
        from core.models import DashboardCard
        dc = DashboardCard(type='disk')
        assert dc.smartAttributes == []
        assert dc.monitoring is False


# ============================================================================
# Lifecycle Manager
# ============================================================================

class TestLifecycleManager:
    """Tests for core/lifecycle.py."""

    def test_register_and_status(self):
        from core.lifecycle import register, status
        import core.lifecycle as lc
        # Clean up any existing tasks
        lc._tasks.clear()
        
        register('test-task', lambda: None, 1.0)
        s = status()
        assert 'test-task' in s
        assert s['test-task']['started'] is False

    def test_start_and_stop(self):
        from core.lifecycle import register, start, stop, status
        import core.lifecycle as lc
        import time
        lc._tasks.clear()
        
        counter = [0]
        def increment():
            counter[0] += 1
        
        register('test-start-stop', increment, 0.1)
        start('test-start-stop')
        time.sleep(0.3)
        stop('test-start-stop')
        
        s = status()
        assert s['test-start-stop']['started'] is False
        assert counter[0] >= 1  # At least ran once
        lc._tasks.clear()

    def test_start_duplicate_is_noop(self):
        from core.lifecycle import register, start, status
        import core.lifecycle as lc
        import time
        lc._tasks.clear()
        
        register('test-noop', lambda: None, 1.0)
        start('test-noop')
        start('test-noop')  # Second call should be no-op
        
        s = status()
        assert s['test-noop']['started'] is True
        lc._tasks.clear()
        # Cleanup
        from core.lifecycle import stop
        stop('test-noop')

    def test_register_duplicate_is_noop(self):
        from core.lifecycle import register, status
        import core.lifecycle as lc
        lc._tasks.clear()
        
        register('test-dup', lambda: None, 1.0)
        register('test-dup', lambda: None, 2.0)  # Should be ignored
        
        s = status()
        assert s['test-dup']['interval'] == 1.0  # Original, not 2.0
        lc._tasks.clear()


# ============================================================================
# generate_stable_id
# ============================================================================

class TestStableId:
    """Tests for hardware ID generation."""

    def test_same_path_same_id(self):
        from core.hardware import generate_stable_id
        id1 = generate_stable_id('/dev/sda')
        id2 = generate_stable_id('/dev/sda')
        assert id1 == id2

    def test_different_path_different_id(self):
        from core.hardware import generate_stable_id
        id1 = generate_stable_id('/dev/sda')
        id2 = generate_stable_id('/dev/sdb')
        assert id1 != id2

    def test_namespace_isolates(self):
        from core.hardware import generate_stable_id
        id1 = generate_stable_id('/dev/sda', namespace='node-a')
        id2 = generate_stable_id('/dev/sda', namespace='node-b')
        assert id1 != id2

    def test_empty_namespace_same_as_no_namespace(self):
        from core.hardware import generate_stable_id
        id1 = generate_stable_id('/dev/sda')
        id2 = generate_stable_id('/dev/sda', namespace='')
        assert id1 == id2

    def test_format(self):
        from core.hardware import generate_stable_id
        id1 = generate_stable_id('/dev/sda')
        assert id1.startswith('dev-')
        assert len(id1) == 16  # 'dev-' + 12 hex chars\n

# ============================================================================
# Export & Diagnostics
# ============================================================================

class TestExportEndpoints:
    """Tests for CSV export and system dump."""

    @pytest.fixture
    def client(self):
        from pathlib import Path
        if not Path('/data').exists() or not os.access('/data', os.W_OK):
            pytest.skip('/data not writable')
        from app import app
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_csv_export(self, client):
        resp = client.get('/api/export/csv?hours=24')
        assert resp.status_code == 200
        assert resp.content_type == 'text/csv'
        data = resp.get_data(as_text=True)
        assert 'timestamp' in data
        assert 'avg_pwm' in data

    def test_csv_export_empty(self, client):
        resp = client.get('/api/export/csv?hours=1')
        assert resp.status_code == 200
        lines = resp.get_data(as_text=True).strip().split('\n')
        assert len(lines) >= 1  # At least header

    def test_system_dump(self, client):
        resp = client.get('/api/dump')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'version' in data
        assert 'timestamp' in data
        assert 'fans' in data
        assert 'nodes' in data
        assert 'recent_logs' in data

    def test_system_dump_has_structure(self, client):
        resp = client.get('/api/dump')
        data = resp.get_json()
        assert isinstance(data['fans'], dict)
        assert isinstance(data['nodes'], dict)
        assert isinstance(data['recent_logs'], list)


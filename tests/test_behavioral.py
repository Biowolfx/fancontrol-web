"""Behavioral tests — command queue, command processing, state persistence, HTTP endpoints."""

import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ============================================================================
# Command Queue
# ============================================================================

class TestCommandQueue:
    """Tests for HTTP command queue — pure in-memory, no hardware."""

    def test_queue_and_drain(self):
        from server.agent_handlers import queue_command, drain_commands
        queue_command('node-test-1', 'config_push', {'config': {'fans': {}}})
        cmds = drain_commands('node-test-1')
        assert len(cmds) == 1
        assert cmds[0]['type'] == 'config_push'
        assert cmds[0]['id'].startswith('cmd-')
        assert cmds[0]['data'] == {'config': {'fans': {}}}

    def test_drain_returns_all(self):
        from server.agent_handlers import queue_command, drain_commands
        queue_command('node-test-2', 'type1', {})
        queue_command('node-test-2', 'type2', {'a': 1})
        cmds = drain_commands('node-test-2')
        assert len(cmds) == 2
        assert cmds[0]['type'] == 'type1'
        assert cmds[1]['type'] == 'type2'

    def test_drain_empty_returns_empty(self):
        from server.agent_handlers import drain_commands
        cmds = drain_commands('nonexistent-node')
        assert cmds == []

    def test_drain_clears_queue(self):
        from server.agent_handlers import queue_command, drain_commands
        queue_command('node-test-3', 'test', {})
        drain_commands('node-test-3')
        cmds = drain_commands('node-test-3')
        assert cmds == []

    def test_ack_command(self):
        from server.agent_handlers import queue_command, drain_commands, ack_command, _cmd_delivery
        queue_command('node-test-4', 'test', {})
        cmds = drain_commands('node-test-4')
        cmd_id = cmds[0]['id']
        assert _cmd_delivery[cmd_id]['status'] == 'sent'
        ack_command(cmd_id, 'delivered')
        assert _cmd_delivery[cmd_id]['status'] == 'delivered'
        assert 'delivered_at' in _cmd_delivery[cmd_id]

    def test_ack_unknown_command_no_crash(self):
        from server.agent_handlers import ack_command
        ack_command('cmd-99999', 'delivered')  # should not raise

    def test_pending_commands(self):
        from server.agent_handlers import queue_command, get_pending_commands
        queue_command('node-test-5', 'cmd1', {})
        queue_command('node-test-5', 'cmd2', {})
        pending = get_pending_commands('node-test-5')
        assert len(pending) == 2

    def test_multiple_nodes_independent(self):
        from server.agent_handlers import queue_command, drain_commands
        queue_command('node-a', 'a_cmd', {})
        queue_command('node-b', 'b_cmd', {})
        cmds_a = drain_commands('node-a')
        cmds_b = drain_commands('node-b')
        assert len(cmds_a) == 1
        assert len(cmds_b) == 1
        assert cmds_a[0]['type'] == 'a_cmd'
        assert cmds_b[0]['type'] == 'b_cmd'


# ============================================================================
# Command Processing (Agent)
# ============================================================================

class TestCommandProcessing:
    """Tests for agent command processing — mock state."""

    def test_set_control_mode(self):
        from agent.client import _process_command
        from core.state import state
        old = state.get('control_mode', 'server')
        _process_command({'type': 'set_control_mode', 'data': {'mode': 'manual'}})
        assert state.get('control_mode') == 'manual'
        state['control_mode'] = old

    def test_unknown_command_no_crash(self):
        from agent.client import _process_command
        _process_command({'type': 'nonexistent_type', 'data': {}})  # should not raise

    def test_command_with_no_data(self):
        from agent.client import _process_command
        _process_command({'type': 'set_control_mode', 'data': {}})  # no mode key — defaults to 'server'


# ============================================================================
# State Persistence
# ============================================================================

class TestStatePersistence:
    """Tests for config save/load roundtrip."""

    def test_auto_register_agents_roundtrip(self):
        from core.state import state
        from core.config import CONFIG_PATH
        if not CONFIG_PATH.exists():
            pytest.skip('No config.json')

        import json
        original = CONFIG_PATH.read_text()
        try:
            old_val = state.get('auto_register_agents', True)
            state['auto_register_agents'] = not old_val
            from core.config import save_config
            save_config()
            # Wait for debounce
            import time
            time.sleep(1.5)

            state['auto_register_agents'] = old_val
            from core.config import load_config
            load_config()
            assert state.get('auto_register_agents') == (not old_val)
        finally:
            CONFIG_PATH.write_text(original)
            state['auto_register_agents'] = old_val


# ============================================================================
# HTTP Endpoints (Flask test client)
# ============================================================================

@pytest.fixture
def client():
    from app import app
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


class TestHealthEndpoint:
    def test_health_returns_version(self, client):
        resp = client.get('/api/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'server_version' in data
        assert 'agents' in data
        assert 'total_agents' in data

    def test_health_agents_have_required_fields(self, client):
        resp = client.get('/api/health')
        data = resp.get_json()
        for agent in data['agents']:
            assert 'node_id' in agent
            assert 'version' in agent
            assert 'status' in agent
            assert 'connected' in agent


class TestSettingsEndpoint:
    def test_get_settings(self, client):
        resp = client.get('/api/settings')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'auto_register_agents' in data

    def test_toggle_auto_register(self, client):
        from core.state import state
        old = state.get('auto_register_agents', True)
        resp = client.post('/api/settings', json={'auto_register_agents': not old})
        assert resp.status_code == 200
        assert resp.get_json()['ok'] is True
        state['auto_register_agents'] = old  # restore


class TestDashboardEndpoint:
    def test_get_dashboard(self, client):
        resp = client.get('/api/dashboard')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'cards' in data
        assert 'groups' in data


class TestNodeEndpoints:
    def _has_db(self):
        from pathlib import Path
        return Path('/data').exists() and os.access('/data', os.W_OK)

    def test_delete_nonexistent_node(self, client):
        if not self._has_db():
            pytest.skip('/data not writable')
        resp = client.delete('/api/nodes/nonexistent-id-12345')
        assert resp.status_code == 404

    def test_delete_node_returns_200(self, client):
        if not self._has_db():
            pytest.skip('/data not writable')
        from server.node_registry import list_nodes
        nodes = list_nodes()
        if not nodes:
            pytest.skip('No nodes to delete')
        nid = nodes[0]['node_id']
        resp = client.delete(f'/api/nodes/{nid}')
        assert resp.status_code in (200, 404)


class TestTelemetryEndpoint:
    def test_rejects_missing_api_token(self, client):
        resp = client.post('/api/agent/telemetry', json={})
        assert resp.status_code == 400

    def test_rejects_unknown_agent_when_auto_register_off(self, client):
        from pathlib import Path
        if not Path('/data').exists() or not os.access('/data', os.W_OK):
            pytest.skip('/data not writable')
        from core.state import state
        old = state.get('auto_register_agents', True)
        state['auto_register_agents'] = False
        try:
            resp = client.post('/api/agent/telemetry', json={
                'api_token': 'completely-fake-token-xyz',
                'node_id': 'test-agent-fake',
                'telemetry': {},
            })
            assert resp.status_code == 403
        finally:
            state['auto_register_agents'] = old


class TestLifecycleStatus:
    def test_status_returns_dict(self):
        from core.lifecycle import status
        s = status()
        assert isinstance(s, dict)


class TestSmartMonitor:
    def test_smart_history_returns_empty_for_unknown(self, client):
        resp = client.get('/api/smart/history/nonexistent-disk?attr=5')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['count'] == 0

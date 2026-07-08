"""Tests for node_registry — SQLite CRUD operations for agents."""

import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Override DATA_DIR before importing node_registry
import core.config
_original_data_dir = core.config.DATA_DIR


def setup_module():
    """Use a temp directory for test database."""
    global _tmpdir
    _tmpdir = tempfile.mkdtemp()
    core.config.DATA_DIR = type(_original_data_dir)(_tmpdir)
    # Re-import to pick up new DATA_DIR
    import server.node_registry as nr
    nr._db_path = core.config.DATA_DIR / 'nodes.db'
    # Reset thread-local connection
    nr._local.conn = None
    nr.init_nodes_table()


def teardown_module():
    core.config.DATA_DIR = _original_data_dir
    shutil.rmtree(_tmpdir, ignore_errors=True)


import server.node_registry as nr


class TestNodeRegistry:
    """Tests for node_registry CRUD operations."""

    def setup_method(self):
        # Clean slate for each test
        conn = nr._get_conn()
        conn.execute('DELETE FROM nodes')
        conn.commit()

    def test_add_node(self):
        node = nr.add_node('Test Agent', api_token='tok123', ip='192.168.1.1')
        assert node['name'] == 'Test Agent'
        assert node['api_token'] == 'tok123'
        assert node['ip'] == '192.168.1.1'
        assert node['node_id'] == 'test-agent'

    def test_add_node_auto_token(self):
        node = nr.add_node('Agent')
        assert len(node['api_token']) == 32  # uuid4 hex

    def test_get_node(self):
        nr.add_node('My Node', api_token='tok1')
        node = nr.get_node('my-node')
        assert node is not None
        assert node['name'] == 'My Node'

    def test_get_node_not_found(self):
        assert nr.get_node('nonexistent') is None

    def test_get_node_by_token(self):
        nr.add_node('Agent', api_token='mytoken')
        node = nr.get_node_by_token('mytoken')
        assert node is not None
        assert node['name'] == 'Agent'

    def test_get_node_by_token_not_found(self):
        assert nr.get_node_by_token('badtoken') is None

    def test_list_nodes(self):
        nr.add_node('A', api_token='a')
        nr.add_node('B', api_token='b')
        nodes = nr.list_nodes()
        assert len(nodes) == 2

    def test_delete_node(self):
        node = nr.add_node('ToDelete', api_token='del')
        nid = node['node_id']
        assert nr.get_node(nid) is not None
        assert nr.delete_node(nid) is True
        assert nr.get_node(nid) is None

    def test_delete_nonexistent(self):
        assert nr.delete_node('nope') is False

    def test_update_node_name(self):
        nr.add_node('Old Name', api_token='u1')
        assert nr.update_node('old-name', name='New Name') is True
        assert nr.get_node('old-name')['name'] == 'New Name'

    def test_update_node_ip(self):
        nr.add_node('Agent', api_token='u2')
        assert nr.update_node('agent', ip='10.0.0.1') is True
        assert nr.get_node('agent')['ip'] == '10.0.0.1'

    def test_update_node_nothing(self):
        nr.add_node('Agent', api_token='u3')
        assert nr.update_node('agent') is False  # no fields to update

    def test_update_node_status(self):
        nr.add_node('Agent', api_token='s1')
        assert nr.update_node_status('agent', 'online') is True
        assert nr.get_node('agent')['status'] == 'online'

    def test_update_node_status_with_telemetry(self):
        nr.add_node('Agent', api_token='s2')
        telemetry = {'fans': {'f1': {'rpm': 1000}}}
        nr.update_node_status('agent', 'online', telemetry)
        node = nr.get_node('agent')
        assert node['status'] == 'online'
        assert node['telemetry']['fans']['f1']['rpm'] == 1000

    def test_update_node_config(self):
        nr.add_node('Agent', api_token='c1')
        config = {'fans': {'f1': {'mode': 'auto'}}}
        nr.update_node_config('agent', config)
        node = nr.get_node('agent')
        assert node['config']['fans']['f1']['mode'] == 'auto'

    def test_update_node_control_mode(self):
        nr.add_node('Agent', api_token='m1')
        nr.update_node_control_mode('agent', 'manual')
        assert nr.get_node('agent')['control_mode'] == 'manual'

    def test_update_node_flags(self):
        nr.add_node('Agent', api_token='f1')
        nr.update_node_flags('agent', pending_update=True, auto_update=True)
        node = nr.get_node('agent')
        assert node['pending_update'] == 1
        assert node['auto_update'] == 1

    def test_update_node_flags_partial(self):
        nr.add_node('Agent', api_token='f2')
        nr.update_node_flags('agent', pending_update=True)
        node = nr.get_node('agent')
        assert node['pending_update'] == 1
        assert node['auto_update'] == 0  # unchanged

    def test_update_node_version(self):
        nr.add_node('Agent', api_token='v1')
        nr.update_node_version('agent', '3.12.59')
        assert nr.get_node('agent')['agent_version'] == '3.12.59'

    def test_save_and_get_snapshot(self):
        nr.add_node('Agent', api_token='snap1')
        snapshot = {'fans': {'f1': {'mode': 'manual'}}}
        nr.save_agent_snapshot('agent', snapshot)
        result = nr.get_agent_snapshot('agent')
        assert result == snapshot

    def test_get_snapshot_none(self):
        nr.add_node('Agent', api_token='snap2')
        assert nr.get_agent_snapshot('agent') is None

    def test_row_to_dict_json_parsing(self):
        """_row_to_dict parses JSON fields correctly."""
        nr.add_node('Agent', api_token='j1')
        nr.update_node_config('agent', {'key': 'value'})
        node = nr.get_node('agent')
        assert isinstance(node['config'], dict)
        assert node['config']['key'] == 'value'

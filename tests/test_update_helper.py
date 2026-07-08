"""Tests for update_helper — version reading and sync logic."""

import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.update_helper import _read_version_from_repo, sync_repo_to_app


class TestReadVersionFromRepo:
    """Tests for _read_version_from_repo."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_reads_version(self):
        state_py = os.path.join(self._tmpdir, 'core', 'state.py')
        os.makedirs(os.path.dirname(state_py))
        with open(state_py, 'w') as f:
            f.write('CONFIG_VERSION = "3.12.59"\n')
        result = _read_version_from_repo(self._tmpdir)
        assert result == 'CONFIG_VERSION = "3.12.59"'

    def test_no_version(self):
        os.makedirs(os.path.join(self._tmpdir, 'core'))
        with open(os.path.join(self._tmpdir, 'core', 'state.py'), 'w') as f:
            f.write('# no version here\n')
        result = _read_version_from_repo(self._tmpdir)
        assert result == ''

    def test_missing_file(self):
        result = _read_version_from_repo('/nonexistent/path')
        assert result == ''


class TestSyncRepoToApp:
    """Tests for sync_repo_to_app."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._repo = os.path.join(self._tmpdir, 'repo')
        self._app = os.path.join(self._tmpdir, 'app')
        os.makedirs(self._repo)
        os.makedirs(self._app)

    def teardown_method(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_syncs_python_files(self):
        with open(os.path.join(self._repo, 'app.py'), 'w') as f:
            f.write('print("hello")')
        with open(os.path.join(self._repo, 'readme.txt'), 'w') as f:
            f.write('readme')
        synced = sync_repo_to_app(self._repo, self._app)
        assert 'app.py' in synced
        assert 'readme.txt' in synced
        assert os.path.exists(os.path.join(self._app, 'app.py'))

    def test_syncs_directories(self):
        os.makedirs(os.path.join(self._repo, 'core'))
        with open(os.path.join(self._repo, 'core', 'state.py'), 'w') as f:
            f.write('version')
        synced = sync_repo_to_app(self._repo, self._app)
        assert 'core/' in synced
        assert os.path.exists(os.path.join(self._app, 'core', 'state.py'))

    def test_overwrites_existing(self):
        os.makedirs(os.path.join(self._app, 'core'))
        with open(os.path.join(self._app, 'core', 'state.py'), 'w') as f:
            f.write('old')
        os.makedirs(os.path.join(self._repo, 'core'))
        with open(os.path.join(self._repo, 'core', 'state.py'), 'w') as f:
            f.write('new')
        sync_repo_to_app(self._repo, self._app)
        with open(os.path.join(self._app, 'core', 'state.py')) as f:
            assert f.read() == 'new'

    def test_skips_non_essential_files(self):
        with open(os.path.join(self._repo, 'random.log'), 'w') as f:
            f.write('log')
        synced = sync_repo_to_app(self._repo, self._app)
        assert 'random.log' not in synced

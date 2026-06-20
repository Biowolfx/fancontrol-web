"""Integration tests — verify all modules import and work together."""

import os
import sys
import pytest

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_core_state_import():
    from core.state import state, get_state, CONFIG_VERSION
    assert isinstance(CONFIG_VERSION, str) and CONFIG_VERSION
    s = get_state()
    assert 'fans' in s
    assert 'initialized' in s


def test_core_hardware_import():
    from core.hardware import (
        discover_fans_and_sensors, discover_disks,
        set_pwm, refresh, CALIBRATION_STEPS
    )
    assert len(CALIBRATION_STEPS) == 11


def test_core_control_import():
    from core.control import loop, process_auto_mode, pwm_from_curve
    assert callable(loop)
    assert callable(process_auto_mode)


def test_core_calibration_import():
    from core.calibration import test_fans, _detect_inversion
    assert callable(test_fans)


def test_core_config_import():
    from core.config import save_config, load_config, CONFIG_PATH
    assert callable(save_config)


def test_core_sensors_import():
    from core.sensors import read_disk_temp, parse_smart_temp
    assert callable(read_disk_temp)


def test_server_routes_import():
    from server.routes import routes
    assert routes is not None


def test_agent_client_import():
    from agent.client import start_client, _telemetry_loop
    assert callable(start_client)


def test_agent_routes_import():
    from agent.routes import agent_routes
    assert agent_routes is not None


def test_app_import():
    from app import app
    assert app is not None
    assert app.name == 'app'

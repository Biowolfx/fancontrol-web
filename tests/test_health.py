"""Tests for check_fan_health — fan health monitoring (slowdown/stop detection)."""

import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.state import state, state_lock
from core.control import check_fan_health


def _reset_state():
    with state_lock:
        state['fans'] = {}


def _add_fan(fan_id='fan1', rpm=1000, pwm=50, writable=True, mode='manual',
             manual_pct=50, current_pct=50, target_pwm=50, calibration=None,
             health=None):
    """Add a test fan to state."""
    fan = {
        'id': fan_id,
        'label': f'Test Fan {fan_id}',
        'rpm': rpm,
        'writable': writable,
        'mode': mode,
        'manual_pct': manual_pct,
        'current_pct': current_pct,
        'target_pwm': target_pwm,
        'calibration': calibration or {},
    }
    if health:
        fan['health'] = health
    with state_lock:
        state['fans'][fan_id] = fan
    return fan


class TestCheckFanHealth:
    """Tests for check_fan_health function."""

    def setup_method(self):
        _reset_state()

    def teardown_method(self):
        _reset_state()

    # --- Stop detection ---

    def test_healthy_fan_not_changed(self):
        """Fan spinning normally → status stays healthy."""
        _add_fan(rpm=1000, pwm=50)
        check_fan_health()
        health = state['fans']['fan1'].get('health', {})
        assert health.get('status', 'healthy') == 'healthy'

    def test_stop_detected_after_10s(self):
        """Fan RPM=0 with PWM>5 → stopped after 10s."""
        _add_fan(rpm=0, pwm=50)
        # First call — starts the timer
        check_fan_health()
        health = state['fans']['fan1']['health']
        assert health['status'] == 'healthy'  # not yet stopped
        assert health['stopped_since'] is not None

        # Simulate 11 seconds passing
        health['stopped_since'] = time.time() - 11
        check_fan_health()
        assert state['fans']['fan1']['health']['status'] == 'stopped'

    def test_stop_not_triggered_when_pwm_zero(self):
        """Fan RPM=0 but PWM=0 → not stopped (intentionally off)."""
        _add_fan(rpm=0, pwm=0, manual_pct=0, current_pct=0, target_pwm=0)
        check_fan_health()
        health = state['fans']['fan1']['health']
        # stopped_since should NOT be set because should_be_spinning is False
        # (pwm=0 and baseline=0)
        assert health.get('stopped_since') is None

    def test_stop_not_triggered_when_mode_off(self):
        """Fan in 'off' mode → skipped entirely."""
        _add_fan(rpm=0, pwm=50, mode='off')
        check_fan_health()
        # No health dict should be created for off-mode fans
        health = state['fans']['fan1'].get('health', {})
        assert health.get('stopped_since') is None

    def test_stop_not_triggered_when_not_writable(self):
        """Non-writable fan → skipped."""
        _add_fan(rpm=0, pwm=50, writable=False)
        check_fan_health()
        health = state['fans']['fan1'].get('health', {})
        assert health.get('stopped_since') is None

    def test_stop_recovery(self):
        """Fan was stopped, now spinning again → healthy."""
        _add_fan(rpm=0, pwm=50)
        check_fan_health()
        # Force stopped status
        state['fans']['fan1']['health']['status'] = 'stopped'
        state['fans']['fan1']['health']['stopped_since'] = time.time() - 11

        # Fan starts spinning
        state['fans']['fan1']['rpm'] = 1000
        check_fan_health()
        assert state['fans']['fan1']['health']['status'] == 'healthy'

    def test_stop_via_baseline(self):
        """Fan RPM=0 with baseline>0 → detected via baseline."""
        _add_fan(rpm=0, pwm=0, manual_pct=0, current_pct=0, target_pwm=0,
                 health={'status': 'healthy', 'rpm_baseline': 1200,
                         'slowdown_since': None, 'stopped_since': None,
                         'last_service_date': None, 'calibration_required': False})
        check_fan_health()
        health = state['fans']['fan1']['health']
        # baseline > 0 → should_be_spinning = True
        assert health['stopped_since'] is not None

    # --- Slowdown detection ---

    def test_slowdown_detected_with_calibration(self):
        """Fan with calibration: RPM < 50% of expected → slowing after 15s."""
        _add_fan(rpm=200, pwm=50, calibration={'max_rpm': 2000})
        # expected = 2000 * 0.5 = 1000, actual=200 < 500 → slowing
        check_fan_health()
        health = state['fans']['fan1']['health']
        assert health['slowing_since'] is not None

        # After 16 seconds
        health['slowing_since'] = time.time() - 16
        check_fan_health()
        assert state['fans']['fan1']['health']['status'] == 'slowing'

    def test_slowdown_detected_without_calibration(self):
        """Fan without calibration: RPM drops below EMA baseline."""
        _add_fan(rpm=1000, pwm=50)
        # First few calls build baseline
        for _ in range(5):
            state['fans']['fan1']['rpm'] = 1000
            check_fan_health()

        baseline = state['fans']['fan1']['health']['rpm_baseline']
        assert baseline > 0

        # Now RPM drops to 40% of baseline
        state['fans']['fan1']['rpm'] = int(baseline * 0.3)
        check_fan_health()
        health = state['fans']['fan1']['health']
        assert health['slowing_since'] is not None

    def test_slowdown_recovery(self):
        """Fan was slowing, now RPM recovered → healthy."""
        _add_fan(rpm=1000, pwm=50, calibration={'max_rpm': 2000})
        check_fan_health()
        # Force slowing status
        state['fans']['fan1']['health']['status'] = 'slowing'
        state['fans']['fan1']['health']['slowing_since'] = time.time() - 20

        # RPM recovers
        state['fans']['fan1']['rpm'] = 1800
        check_fan_health()
        assert state['fans']['fan1']['health']['status'] == 'healthy'

    def test_no_slowdown_when_rpm_zero(self):
        """RPM=0 should not trigger slowdown (stop handles it)."""
        _add_fan(rpm=0, pwm=50, calibration={'max_rpm': 2000})
        check_fan_health()
        health = state['fans']['fan1']['health']
        assert health.get('slowing_since') is None

    # --- Needs calibration ---

    def test_needs_calibration_takes_priority(self):
        """calibration_required=True → status = needs_calibration."""
        _add_fan(rpm=1000, pwm=50,
                 health={'status': 'healthy', 'rpm_baseline': 0,
                         'slowdown_since': None, 'stopped_since': None,
                         'last_service_date': None, 'calibration_required': True})
        check_fan_health()
        assert state['fans']['fan1']['health']['status'] == 'needs_calibration'

    def test_needs_calibration_not_when_stopped(self):
        """calibration_required but fan is stopped → stays stopped."""
        _add_fan(rpm=0, pwm=50,
                 health={'status': 'healthy', 'rpm_baseline': 0,
                         'slowdown_since': None, 'stopped_since': time.time() - 15,
                         'last_service_date': None, 'calibration_required': True})
        check_fan_health()
        assert state['fans']['fan1']['health']['status'] == 'stopped'

    # --- Health dict initialization ---

    def test_health_dict_created_if_missing(self):
        """Fan without health dict → dict created with defaults."""
        _add_fan(rpm=0, pwm=0, manual_pct=0, current_pct=0, target_pwm=0)
        check_fan_health()
        health = state['fans']['fan1']['health']
        assert health['status'] == 'healthy'
        assert health.get('stopped_since') is None
        assert health.get('calibration_required', False) is False

    def test_health_dict_persists(self):
        """Health dict persists across calls."""
        _add_fan(rpm=1000, pwm=50)
        check_fan_health()
        state['fans']['fan1']['health']['last_service_date'] = '2026-01-01'
        check_fan_health()
        assert state['fans']['fan1']['health']['last_service_date'] == '2026-01-01'

    # --- EMA baseline ---

    def test_ema_baseline_builds_up(self):
        """Baseline builds up via EMA over multiple calls."""
        _add_fan(rpm=1000, pwm=50)
        for _ in range(20):
            state['fans']['fan1']['rpm'] = 1000
            check_fan_health()
        baseline = state['fans']['fan1']['health']['rpm_baseline']
        assert 900 < baseline < 1100  # Should converge to ~1000

    def test_ema_baseline_uses_max_rpm_when_available(self):
        """With calibration max_rpm, uses expected_rpm instead of EMA."""
        _add_fan(rpm=800, pwm=50, calibration={'max_rpm': 2000})
        check_fan_health()
        # expected = 2000 * 0.5 = 1000, actual=800 > 500 → not slowing
        health = state['fans']['fan1']['health']
        assert health.get('slowing_since') is None

    # --- Multiple fans ---

    def test_multiple_fans_independent(self):
        """Each fan's health is tracked independently."""
        _add_fan('fan1', rpm=1000, pwm=50)
        _add_fan('fan2', rpm=0, pwm=50)
        check_fan_health()
        assert state['fans']['fan1']['health']['status'] == 'healthy'
        assert state['fans']['fan2']['health'].get('stopped_since') is not None

    # --- SocketIO emission ---

    def test_emits_event_on_stop(self):
        """Status change to 'stopped' triggers socketio.emit."""
        mock_socketio = MagicMock()
        _add_fan(rpm=0, pwm=50)
        check_fan_health(mock_socketio)
        # Force stopped
        state['fans']['fan1']['health']['stopped_since'] = time.time() - 11
        check_fan_health(mock_socketio)
        mock_socketio.emit.assert_called()
        call_args = mock_socketio.emit.call_args
        assert call_args[0][0] == 'fan:health'

    def test_emits_cleared_on_recovery(self):
        """Recovery from stopped → healthy triggers fan:health:cleared."""
        mock_socketio = MagicMock()
        _add_fan(rpm=0, pwm=50)
        check_fan_health(mock_socketio)
        state['fans']['fan1']['health']['status'] = 'stopped'
        state['fans']['fan1']['health']['stopped_since'] = time.time() - 11
        # Fan recovers
        state['fans']['fan1']['rpm'] = 1000
        check_fan_health(mock_socketio)
        mock_socketio.emit.assert_called()
        call_args = mock_socketio.emit.call_args
        assert call_args[0][0] == 'fan:health:cleared'

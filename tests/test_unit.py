"""Unit tests for pure functions — no hardware, no network, no state mutation."""

import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ============================================================================
# core.control: process_auto_mode
# ============================================================================

from core.control import process_auto_mode, pwm_from_curve, _compare, MIN_PWM_PCT, MAX_PWM_PCT, SENSOR_FAILURE_TEMP


class TestProcessAutoMode:
    """Tests for process_auto_mode — pure function, no side effects."""

    def _fan(self, **overrides):
        """Build minimal fan dict with defaults."""
        base = {
            'sensors': ['temp:cpu'],
            'sensor_mode': 'max',
            'target_temp': 35,
        }
        base.update(overrides)
        return base

    def test_failsafe_returns_max(self):
        pct, status = process_auto_mode('fan1', self._fan(), 30, 35, failsafe=True)
        assert pct == MAX_PWM_PCT
        assert status == 'failsafe'

    def test_standby_no_schedule(self):
        pct, status = process_auto_mode('fan1', self._fan(), 30, 35, standby_mode=True)
        assert status == 'standby'
        assert 0 <= pct <= 100

    def test_standby_schedule_manual(self):
        sched = {'mode': 'manual', 'speed_pct': 60}
        pct, status = process_auto_mode('fan1', self._fan(), 30, 35, schedule_item=sched, standby_mode=True)
        assert pct == 60
        assert status == 'standby'

    def test_standby_schedule_off(self):
        sched = {'mode': 'off'}
        pct, status = process_auto_mode('fan1', self._fan(), 30, 35, schedule_item=sched, standby_mode=True)
        assert pct == 0

    def test_standby_schedule_auto(self):
        sched = {'mode': 'auto', 'target_temp': 35}
        pct, status = process_auto_mode('fan1', self._fan(), 30, 35, schedule_item=sched, standby_mode=True)
        assert status == 'standby'
        assert pct >= MIN_PWM_PCT

    def test_sensor_failure_critical(self):
        """Temp >= 99 means sensor failure → max cooling."""
        pct, status = process_auto_mode('fan1', self._fan(), SENSOR_FAILURE_TEMP, 35)
        assert pct == MAX_PWM_PCT
        assert status == 'critical'

    def test_cold_disk_min_pwm(self):
        """Delta <= -2 (30°C vs target 35°C → delta -5) → min PWM."""
        pct, status = process_auto_mode('fan1', self._fan(), 30, 35)
        assert pct == MIN_PWM_PCT
        assert status == 'nominal'

    def test_hot_disk_max_pwm(self):
        """Delta >= 6 (42°C vs target 35°C → delta 7) → max PWM."""
        pct, status = process_auto_mode('fan1', self._fan(), 42, 35)
        assert pct == MAX_PWM_PCT
        assert status == 'warning'

    def test_warm_disk_proportional(self):
        """Delta = 3 (38°C vs target 35°C → in range -2..6) → proportional."""
        pct, status = process_auto_mode('fan1', self._fan(), 38, 35)
        assert MIN_PWM_PCT < pct < MAX_PWM_PCT
        assert status == 'nominal'

    def test_warning_threshold(self):
        """Delta = 5 → warning status."""
        pct, status = process_auto_mode('fan1', self._fan(), 40, 35)
        assert status == 'warning'

    def test_exactly_at_target(self):
        """Delta = 0 → nominal, some PWM."""
        pct, status = process_auto_mode('fan1', self._fan(), 35, 35)
        assert MIN_PWM_PCT < pct < MAX_PWM_PCT
        assert status == 'nominal'


# ============================================================================
# core.control: pwm_from_curve
# ============================================================================

class TestPwmFromCurve:
    """Tests for pwm_from_curve — calibration curve interpolation."""

    def _fan(self, curve=None, cal=None):
        f = {}
        if curve is not None:
            f['curve'] = curve
        if cal is not None:
            f['calibration'] = cal
        return f

    def test_no_calibration_simple(self):
        """Without calibration: direct percent-to-pwm mapping."""
        result = pwm_from_curve(self._fan(), 50)
        assert result == 127  # 50 * 255 // 100 = 127 (integer division)

    def test_no_curve_simple(self):
        """Without curve: direct mapping."""
        result = pwm_from_curve(self._fan(cal={'min_pct': 20}), 50)
        assert result == 127

    def test_simple_linear_curve(self):
        """2-point curve: 0%→0, 100%→255."""
        curve = [
            {'pct': 0, 'pwm': 0},
            {'pct': 100, 'pwm': 255},
        ]
        cal = {'min_pct': 20, 'min_pwm': 0, 'max_pwm': 255, 'lambda': 1.0}
        result = pwm_from_curve(self._fan(curve=curve, cal=cal), 50)
        assert 125 <= result <= 130

    def test_clamps_to_min_pwm(self):
        """Below min_pct → bumped up to min_pct."""
        curve = [
            {'pct': 0, 'pwm': 0},
            {'pct': 100, 'pwm': 255},
        ]
        cal = {'min_pct': 25, 'min_pwm': 50, 'max_pwm': 255, 'lambda': 1.0}
        result = pwm_from_curve(self._fan(curve=curve, cal=cal), 10)
        assert result >= 50

    def test_lambda_curve(self):
        """Lambda < 1 makes curve concave-up (higher PWM at mid-range)."""
        curve = [
            {'pct': 0, 'pwm': 0},
            {'pct': 100, 'pwm': 255},
        ]
        cal_linear = {'min_pct': 0, 'min_pwm': 0, 'max_pwm': 255, 'lambda': 1.0}
        cal_quiet = {'min_pct': 0, 'min_pwm': 0, 'max_pwm': 255, 'lambda': 0.5}
        pwm_linear = pwm_from_curve(self._fan(curve=curve, cal=cal_linear), 50)
        pwm_quiet = pwm_from_curve(self._fan(curve=curve, cal=cal_quiet), 50)
        # lambda < 1: 0.5^0.5 = 0.707 → higher PWM at 50%
        assert pwm_quiet > pwm_linear

    def test_clamps_0_100_with_calibration(self):
        """Clamping only applies when calibration data exists."""
        curve = [
            {'pct': 0, 'pwm': 0},
            {'pct': 100, 'pwm': 255},
        ]
        cal = {'min_pct': 0, 'min_pwm': 0, 'max_pwm': 255, 'lambda': 1.0}
        result = pwm_from_curve(self._fan(curve=curve, cal=cal), -10)
        assert result >= 0
        result = pwm_from_curve(self._fan(curve=curve, cal=cal), 200)
        assert result <= 255


# ============================================================================
# core.control: _compare
# ============================================================================

class TestCompare:
    def test_gte(self):
        assert _compare(10, '>=', 10) is True
        assert _compare(9, '>=', 10) is False

    def test_gt(self):
        assert _compare(11, '>', 10) is True
        assert _compare(10, '>', 10) is False

    def test_lte(self):
        assert _compare(10, '<=', 10) is True
        assert _compare(11, '<=', 10) is False

    def test_lt(self):
        assert _compare(9, '<', 10) is True
        assert _compare(10, '<', 10) is False

    def test_invalid_op(self):
        assert _compare(10, '==', 10) is False

    def test_exception_safety(self):
        assert _compare(None, '>=', 10) is False


# ============================================================================
# core.hardware: generate_stable_id
# ============================================================================

from core.hardware import generate_stable_id, is_physical_disk, calculate_disk_health


class TestGenerateStableId:
    def test_format(self):
        result = generate_stable_id('/dev/sata1')
        assert result.startswith('dev-')
        assert len(result) == 16  # "dev-" + 12 hex chars

    def test_deterministic(self):
        assert generate_stable_id('/dev/sata1') == generate_stable_id('/dev/sata1')

    def test_different_paths_different_ids(self):
        assert generate_stable_id('/dev/sata1') != generate_stable_id('/dev/sata2')


# ============================================================================
# core.hardware: is_physical_disk
# ============================================================================

class TestIsPhysicalDisk:
    def test_sata(self):
        assert is_physical_disk('sata1') is True
        assert is_physical_disk('sata16') is True

    def test_nvme(self):
        assert is_physical_disk('nvme0n1') is True
        assert is_physical_disk('nvme1n1') is True

    def test_sd(self):
        assert is_physical_disk('sda') is True
        assert is_physical_disk('sdb') is True

    def test_rejects(self):
        assert is_physical_disk('loop0') is False
        assert is_physical_disk('dm-0') is False
        assert is_physical_disk('ram0') is False
        assert is_physical_disk('') is False


# ============================================================================
# core.hardware: calculate_disk_health
# ============================================================================

class TestCalculateDiskHealth:
    def test_standby(self):
        result = calculate_disk_health(0)
        assert result['status'] == 'standby'
        assert result['pct_fill'] == 0

    def test_cold(self):
        result = calculate_disk_health(25)
        assert result['color_zone'] == 'cyan'
        assert result['status'] == 'active'

    def test_warm(self):
        result = calculate_disk_health(40)
        assert result['color_zone'] == 'orange'

    def test_hot(self):
        result = calculate_disk_health(50)
        assert result['color_zone'] == 'red'

    def test_critical(self):
        result = calculate_disk_health(60)
        assert result['color_zone'] == 'critical'

    def test_clamps_high(self):
        result = calculate_disk_health(200)
        assert result['pct_fill'] == 100

    def test_clamps_low(self):
        result = calculate_disk_health(-5)
        assert result['status'] == 'standby'


# ============================================================================
# core.hardware: _extract_smart_raw_value
# ============================================================================

from core.hardware import _extract_smart_raw_value


class TestExtractSmartRawValue:
    def test_standard_line(self):
        line = '190 Airflow_Temperature_Cel 0x0022   065   053   000    Old_age   Always       -       35 (Min/Max 26/45)'
        assert _extract_smart_raw_value(line) == 35

    def test_temperature_celsius(self):
        line = '194 Temperature_Celsius     0x0022   035   047   000    Old_age   Always       -       35 (0 19 0 0 0)'
        assert _extract_smart_raw_value(line) == 35

    def test_no_dash(self):
        line = '190 Airflow_Temperature_Cel 0x0022   065   053   000    Old_age   Always       35'
        assert _extract_smart_raw_value(line) is None

    def test_empty_line(self):
        assert _extract_smart_raw_value('') is None


# ============================================================================
# core.hardware: _parse_disk_temp_preferred
# ============================================================================

from core.hardware import _parse_disk_temp_preferred


class TestParseDiskTempPreferred:
    def test_airflow_priority(self):
        output = (
            '190 Airflow_Temperature_Cel 0x0022   065   053   000    Old_age   Always       -       38 (Min/Max 26/45)\n'
            '194 Temperature_Celsius     0x0022   065   047   000    Old_age   Always       -       62 (0 19 0 0 0)\n'
        )
        temp, source = _parse_disk_temp_preferred(output)
        assert temp == 38
        assert source == 'airflow'

    def test_hda_priority_over_celsius(self):
        output = (
            '194 Temperature_Celsius     0x0022   065   047   000    Old_age   Always       -       62 (0 19 0 0 0)\n'
            '193 HDA_Temperature         0x0022   040   035   000    Old_age   Always       -       40\n'
        )
        temp, source = _parse_disk_temp_preferred(output)
        assert temp == 40
        assert source == 'hda'

    def test_smartctl_header(self):
        output = 'Current Drive Temperature:     37 C\n'
        temp, source = _parse_disk_temp_preferred(output)
        assert temp == 37
        assert source == 'smartctl_header'

    def test_celsius_fallback(self):
        output = '194 Temperature_Celsius     0x0022   035   047   000    Old_age   Always       -       35 (0 19 0 0 0)\n'
        temp, source = _parse_disk_temp_preferred(output)
        assert temp == 35
        assert source == 'celsius'

    def test_nvme_format(self):
        output = 'Temperature:                        37 Celsius\n'
        temp, source = _parse_disk_temp_preferred(output)
        assert temp == 37
        assert source == 'nvme'

    def test_empty_output(self):
        temp, source = _parse_disk_temp_preferred('')
        assert temp is None
        assert source == ''


# ============================================================================
# core.hardware: parse_smart_attributes
# ============================================================================

from core.hardware import parse_smart_attributes


class TestParseSmartAttributes:
    SAMPLE = (
        'SMART Attributes Data Structure revision number: 10\n'
        'Vendor Specific SMART Attributes with Thresholds:\n'
        'ID# ATTRIBUTE_NAME          FLAGS    VALUE WORST THRESH TYPE      UPDATED      FAILING_NOW RAW_VALUE\n'
        '  1 Raw_Read_Error_Rate     POSR--   200   200   051    Pre-fail  Always       -       0\n'
        '  9 Power_On_Hours          PO----   097   097   000    Old_age   Always       -       3150\n'
        '194 Temperature_Celsius     0x0022   035   047   000    Old_age   Always       -       35 (0 19 0 0 0)\n'
        '190 Airflow_Temperature_Cel 0x0022   065   053   000    Old_age   Always       -       35 (Min/Max 26/45)\n'
    )

    def test_parses_all_attributes(self):
        attrs = parse_smart_attributes(self.SAMPLE)
        assert len(attrs) == 4

    def test_attribute_fields(self):
        attrs = parse_smart_attributes(self.SAMPLE)
        first = attrs[0]
        assert first['id'] == 1
        assert first['name'] == 'Raw_Read_Error_Rate'
        assert first['value'] == 200

    def test_raw_num_extraction(self):
        attrs = parse_smart_attributes(self.SAMPLE)
        temp_attr = [a for a in attrs if a['name'] == 'Temperature_Celsius'][0]
        assert temp_attr['raw_num'] == 35

    def test_empty_output(self):
        assert parse_smart_attributes('') == []

    def test_no_attribute_section(self):
        assert parse_smart_attributes('some random text\nno attributes here') == []


# ============================================================================
# core.hardware: is_dsm_fan_available
# ============================================================================

from core.dsm_fan import is_dsm_fan_available


class TestIsDsmFanAvailable:
    def test_returns_bool(self):
        result = is_dsm_fan_available()
        assert isinstance(result, bool)


# ============================================================================
# core.config: Config dataclass
# ============================================================================

from core.config import Config


class TestConfig:
    def test_defaults(self):
        c = Config()
        assert c.mode in ('server', 'agent', 'setup')
        assert c.telemetry_interval == 5
        assert c.node_id == 'agent-1'
        assert isinstance(c.data_dir, type(Path('/')))

    def test_frozen(self):
        c = Config()
        with pytest.raises(AttributeError):
            c.mode = 'agent'

    def test_custom_values(self):
        c = Config(mode='agent', node_id='test-node', telemetry_interval=10)
        assert c.mode == 'agent'
        assert c.node_id == 'test-node'
        assert c.telemetry_interval == 10

    def test_log_dir_defaults_to_data_dir_logs(self):
        c = Config(data_dir=Path('/tmp/test'))
        assert c.log_dir == '/tmp/test/logs'

    def test_log_dir_override(self):
        c = Config(log_dir='/custom/logs')
        assert c.log_dir == '/custom/logs'

    def test_cors_origins_list(self):
        c = Config(cors_origins='http://a.com,http://b.com')
        assert c.cors_origins == ['http://a.com', 'http://b.com']

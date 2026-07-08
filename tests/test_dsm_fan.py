"""Tests for dsm_fan — DSM scheme parsing functions."""

import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.dsm_fan import _parse_fan_speed, _parse_entry, _parse_fan_config


class TestParseFanSpeed:
    """Tests for _parse_fan_speed — parse fan speed string to percentage."""

    def test_percent_format(self):
        assert _parse_fan_speed('20%40hz') == 20
        assert _parse_fan_speed('99%40hz') == 99
        assert _parse_fan_speed('0%40hz') == 0

    def test_plain_integer(self):
        assert _parse_fan_speed('50') == 50
        assert _parse_fan_speed('0') == 0
        assert _parse_fan_speed('100') == 100

    def test_raw_pwm_value(self):
        """Values > 100 are treated as raw PWM (0-255) and converted."""
        assert _parse_fan_speed('255') == 100
        assert _parse_fan_speed('127') == 49  # 127 * 100 / 255 ≈ 49

    def test_unknown(self):
        assert _parse_fan_speed('UNKNOWN') is None
        assert _parse_fan_speed(None) is None

    def test_invalid(self):
        assert _parse_fan_speed('abc') is None
        assert _parse_fan_speed('') is None


class TestParseEntry:
    """Tests for _parse_entry — parse XML element into dict."""

    def _elem(self, tag='disk_temperature', fan_speed='50', action='NONE', text='30'):
        """Create a mock XML element."""
        class MockElem:
            def __init__(self, tag, attrib, text):
                self.tag = tag
                self.attrib = attrib
                self.text = text
        return MockElem(tag, {'fan_speed': fan_speed, 'action': action}, text)

    def test_basic_entry(self):
        elem = self._elem()
        result = _parse_entry(elem)
        assert result['sensor_type'] == 'disk_temperature'
        assert result['fan_speed'] == '50'
        assert result['fan_speed_pct'] == 50
        assert result['action'] == 'NONE'
        assert result['threshold_temp'] == '30'

    def test_cpu_temperature(self):
        elem = self._elem(tag='cpu_temperature', fan_speed='75', text='60')
        result = _parse_entry(elem)
        assert result['sensor_type'] == 'cpu_temperature'
        assert result['fan_speed_pct'] == 75
        assert result['threshold_temp'] == '60'

    def test_percent_format_in_entry(self):
        elem = self._elem(fan_speed='30%40hz')
        result = _parse_entry(elem)
        assert result['fan_speed_pct'] == 30

    def test_shutdown_action(self):
        elem = self._elem(action='SHUTDOWN', text='55')
        result = _parse_entry(elem)
        assert result['action'] == 'SHUTDOWN'


class TestParseFanConfig:
    """Tests for _parse_fan_config — parse fan_config element."""

    def _fan_config(self, type_name='DUAL_MODE_LOW', entries_xml=''):
        """Create a mock fan_config XML element."""
        class MockElem:
            def __init__(self, tag, attrib, text, children):
                self.tag = tag
                self.attrib = attrib
                self.text = text
                self._children = children
            def __iter__(self):
                return iter(self._children)

        children = []
        for entry in entries_xml:
            tag, speed, action, temp = entry
            children.append(MockElem(tag, {'fan_speed': speed, 'action': action}, temp, []))

        return MockElem('fan_config', {'type': type_name, 'period': '24x7',
                                        'hibernation_speed': '20%40hz'}, None, children)

    def test_basic_config(self):
        fc = self._fan_config(entries_xml=[
            ('disk_temperature', '50', 'NONE', '30'),
            ('disk_temperature', '70', 'NONE', '40'),
        ])
        result = _parse_fan_config(fc)
        assert result['type'] == 'DUAL_MODE_LOW'
        assert result['period'] == '24x7'
        assert result['hibernation_speed'] == '20%40hz'
        assert len(result['entries']) == 2
        assert result['entries'][0]['index'] == 0
        assert result['entries'][1]['index'] == 1

    def test_empty_config(self):
        fc = self._fan_config(entries_xml=[])
        result = _parse_fan_config(fc)
        assert result['entries'] == []

    def test_mixed_entry_types(self):
        fc = self._fan_config(entries_xml=[
            ('disk_temperature', '40', 'NONE', '35'),
            ('cpu_temperature', '60', 'SHUTDOWN', '80'),
        ])
        result = _parse_fan_config(fc)
        assert len(result['entries']) == 2
        assert result['entries'][0]['sensor_type'] == 'disk_temperature'
        assert result['entries'][1]['sensor_type'] == 'cpu_temperature'
        assert result['entries'][1]['action'] == 'SHUTDOWN'


class TestParseScemd:
    """Tests for _parse_scemd with actual XML files."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp()
        self._scemd_path = Path(self._tmpdir) / 'scemd.xml'

    def teardown_method(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write_scemd(self, content):
        self._scemd_path.write_text(content)

    def test_official_format(self):
        self._write_scemd('''<?xml version="1.0"?>
<scemd hw_version="DS920+">
  <fan_config type="DUAL_MODE_LOW" period="24x7" hibernation_speed="20%40hz">
    <disk_temperature fan_speed="50" action="NONE">30</disk_temperature>
    <disk_temperature fan_speed="70" action="NONE">40</disk_temperature>
  </fan_config>
  <fan_config type="DUAL_MODE_HIGH" period="24x7">
    <cpu_temperature fan_speed="80" action="NONE">60</cpu_temperature>
  </fan_config>
</scemd>''')
        with patch('core.dsm_fan.SCEMD_PATH', str(self._scemd_path)):
            from core.dsm_fan import get_all_schemes
            result = get_all_schemes()

        assert result is not None
        assert result['hw_version'] == 'DS920+'
        assert len(result['schemes']) == 2
        assert result['schemes'][0]['type'] == 'DUAL_MODE_LOW'
        assert len(result['schemes'][0]['entries']) == 2
        assert result['schemes'][1]['type'] == 'DUAL_MODE_HIGH'

    def test_flat_format(self):
        self._write_scemd('''<?xml version="1.0"?>
<scemd>
  <disk_temperature fan_speed="30" action="NONE">25</disk_temperature>
  <disk_temperature fan_speed="50" action="NONE">35</disk_temperature>
</scemd>''')
        with patch('core.dsm_fan.SCEMD_PATH', str(self._scemd_path)):
            from core.dsm_fan import get_all_schemes
            result = get_all_schemes()

        assert result is not None
        assert len(result['schemes']) == 1
        assert result['schemes'][0]['type'] == 'FLAT'
        assert len(result['schemes'][0]['entries']) == 2

    def test_missing_file(self):
        with patch('core.dsm_fan.SCEMD_PATH', '/nonexistent/scemd.xml'):
            from core.dsm_fan import get_all_schemes
            result = get_all_schemes()
        assert result is None

"""Disk temperature sensors — re-exports from hardware module.

read_disk_temp() and parse_smart_temp() live in core/hardware.py
to avoid circular imports. This module re-exports them for
consumers that think of these as sensor operations.
"""

from core.hardware import read_disk_temp, parse_smart_temp  # noqa: F401

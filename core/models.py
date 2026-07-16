"""Data models — structured types for core entities.

Provides type safety and IDE support for the most critical data structures.
These are lightweight wrappers — they coexist with the existing dict-based state
and can be used incrementally.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FanConfig:
    """Fan configuration — calibration, sensors, control mode."""
    id: str = ''
    label: str = ''
    writable: bool = False
    mode: str = 'manual'  # 'manual' | 'auto' | 'off' | 'schedule'
    sensors: List[str] = field(default_factory=list)
    sensor_mode: str = 'max'  # 'max' | 'min' | 'avg'
    target_temp: float = 35.0
    inverted: bool = False
    control_method: str = 'hwmon'  # 'hwmon' | 'dsm_scemd'
    curve: List[Dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'FanConfig':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class NodeInfo:
    """Node runtime info — connection, version, telemetry."""
    node_id: str = ''
    stable_id: str = ''
    name: str = ''
    ip: str = ''
    port: int = 5059
    status: str = 'offline'  # 'online' | 'offline' | 'pending'
    control_mode: str = 'server'
    agent_version: str = ''
    auto_update: bool = False
    pending_update: bool = False
    last_seen: str = ''

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'NodeInfo':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TelemetryPayload:
    """Agent telemetry payload — received from agent HTTP POST."""
    api_token: str = ''
    node_id: str = ''
    version: str = ''
    fans: Dict[str, Any] = field(default_factory=dict)
    temp_sensors: Dict[str, Any] = field(default_factory=dict)
    hdd_sensors: Dict[str, Any] = field(default_factory=dict)
    control_method: str = 'server'

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'TelemetryPayload':
        t = d.get('telemetry', {})
        return cls(
            api_token=d.get('api_token', ''),
            node_id=d.get('node_id', ''),
            version=d.get('version', ''),
            fans=t.get('fans', {}),
            temp_sensors=t.get('temp_sensors', {}),
            hdd_sensors=t.get('hdd_sensors', {}),
            control_method=t.get('control_mode', 'server'),
        )


@dataclass
class DashboardCard:
    """Dashboard card configuration."""
    id: str = ''
    type: str = ''  # 'fan' | 'temperature' | 'disk' | 'system'
    source: str = 'local'
    sourceId: str = ''
    label: str = ''
    col: int = 0
    row: int = 0
    colSpan: int = 3
    rowSpan: int = 1
    groupId: str = ''
    lockSize: bool = False
    # Fan-specific
    showRpm: bool = True
    showMode: bool = False
    showSensors: bool = False
    showTarget: bool = False
    # Disk-specific
    smartAttributes: List[str] = field(default_factory=list)
    smartMonitored: List[str] = field(default_factory=list)
    smartUnits: Dict[str, str] = field(default_factory=dict)
    monitoring: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'DashboardCard':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

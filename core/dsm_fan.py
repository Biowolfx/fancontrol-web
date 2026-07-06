"""DSM fan control via scemd.xml — fallback for official kernel xpenology.

Supports two scemd.xml formats:
1. Official Synology: <fan_config type="DUAL_MODE_LOW" ...> wrapping child elements
2. Flat format: <disk_temperature fan_speed="..." action="..."> directly under <scemd>

Fan speed values can be:
- "20%40hz" (percentage + Hz notation)
- "255" (raw 0-255 PWM value)
- "20" (plain percentage)
"""

import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger('fancontrol')

SCEMD_PATH = '/usr/syno/etc.defaults/scemd.xml'
SCEMD_BACKUP = '/usr/syno/etc.defaults/scemd.xml.bak'

# Known fan_config types in priority order
KNOWN_SCHEME_TYPES = [
    'DUAL_MODE_HIGH',
    'DUAL_MODE_LOW',
    'FULL_SPEED',
    'STOP',
]

# Module-level cache for scemd.xml parsed tree
_scemd_cache = None
_scemd_cache_mtime = 0.0


def is_dsm_fan_available():
    """Check if scemd.xml exists and can be used for fan control."""
    return Path(SCEMD_PATH).exists() and os.access(SCEMD_PATH, os.R_OK | os.W_OK)


def _invalidate_scemd_cache():
    """Invalidate cached scemd.xml tree. Called after writes."""
    global _scemd_cache, _scemd_cache_mtime
    _scemd_cache = None
    _scemd_cache_mtime = 0.0


def _parse_scemd():
    """Parse scemd.xml and return the tree (cached by file mtime)."""
    global _scemd_cache, _scemd_cache_mtime
    try:
        mtime = Path(SCEMD_PATH).stat().st_mtime
    except OSError:
        return None

    if _scemd_cache is not None and mtime == _scemd_cache_mtime:
        import copy
        return copy.deepcopy(_scemd_cache)

    try:
        tree = ET.parse(SCEMD_PATH)
        _scemd_cache = tree
        _scemd_cache_mtime = mtime
        return tree
    except ET.ParseError as e:
        logger.error(f'Failed to parse {SCEMD_PATH}: {e}')
        return None
    except Exception as e:
        logger.error(f'Error reading {SCEMD_PATH}: {e}')
        return None


def _parse_fan_speed(raw):
    """Parse fan_speed string to integer percentage (0-100).

    Handles formats: '20%40hz', '99%40hz', '255', '20', 'UNKNOWN', etc.
    """
    if raw is None or raw == 'UNKNOWN':
        return None
    # "20%40hz" or "99%40hz" format
    m = re.match(r'^(\d+)%', str(raw))
    if m:
        return int(m.group(1))
    # Plain integer (0-255 raw or 0-100 percentage)
    try:
        val = int(raw)
        if val > 100:
            return int(val * 100 / 255)
        return val
    except (ValueError, TypeError):
        return None


def _parse_entry(elem):
    """Parse a single disk_temperature or cpu_temperature element into a dict."""
    fan_speed_raw = elem.attrib.get('fan_speed', '')
    return {
        'sensor_type': elem.tag,
        'fan_speed': fan_speed_raw,
        'fan_speed_pct': _parse_fan_speed(fan_speed_raw),
        'action': elem.attrib.get('action', 'NONE'),
        'threshold_temp': elem.text.strip() if elem.text else '0',
        'index': None,  # set by caller
    }


def _parse_fan_config(fc):
    """Parse a <fan_config> element into a scheme dict."""
    entries = []
    for child in fc:
        if child.tag in ('disk_temperature', 'cpu_temperature'):
            entries.append(_parse_entry(child))

    for i, e in enumerate(entries):
        e['index'] = i

    return {
        'type': fc.attrib.get('type', 'UNKNOWN'),
        'period': fc.attrib.get('period', ''),
        'threshold': fc.attrib.get('threshold', ''),
        'hibernation_speed': fc.attrib.get('hibernation_speed', 'UNKNOWN'),
        'entries': entries,
    }


def get_all_schemes():
    """Parse scemd.xml and return all fan_config schemes.

    Returns dict with 'schemes' list and 'hw_version'.
    Handles both official format (<fan_config> wrappers) and flat format.
    """
    tree = _parse_scemd()
    if tree is None:
        return None

    root = tree.getroot()
    result = {'schemes': [], 'hw_version': None}

    # Check for hw_version on root or child elements
    hw = root.attrib.get('hw_version')
    if hw:
        result['hw_version'] = hw

    # Format 1: Official Synology — <fan_config type="..."> wrapping children
    fan_configs = list(root.iter('fan_config'))
    if fan_configs:
        for fc in fan_configs:
            fc_type = fc.attrib.get('type', 'UNKNOWN')
            hw = fc.attrib.get('hw_version')
            if hw:
                result['hw_version'] = hw
            scheme = _parse_fan_config(fc)
            result['schemes'].append(scheme)
        return result

    # Format 2: Flat — disk_temperature/cpu_temperature directly under root
    flat_entries = []
    for child in root:
        if child.tag in ('disk_temperature', 'cpu_temperature'):
            flat_entries.append(_parse_entry(child))

    if flat_entries:
        for i, e in enumerate(flat_entries):
            e['index'] = i
        result['schemes'].append({
            'type': 'FLAT',
            'period': '',
            'threshold': '',
            'hibernation_speed': 'UNKNOWN',
            'entries': flat_entries,
        })

    return result


def get_dsm_fan_info():
    """Get simplified info about DSM fan control (backward-compatible).

    Returns dict with 'modes' list for legacy callers.
    """
    info = get_all_schemes()
    if info is None:
        return None

    modes = []
    for scheme in info.get('schemes', []):
        for entry in scheme.get('entries', []):
            modes.append({
                'type': entry['sensor_type'],
                'mode': scheme['type'].lower(),
                'fan_speed': entry.get('fan_speed_pct', 0) or 0,
            })

    return {'modes': modes, 'hw_version': info.get('hw_version')}


def get_scheme(scheme_type):
    """Get a single scheme by type (e.g. 'DUAL_MODE_LOW')."""
    info = get_all_schemes()
    if info is None:
        return None
    for s in info['schemes']:
        if s['type'] == scheme_type:
            return s
    return None


def get_active_scheme_type():
    """Determine which fan_config scheme is currently active.

    Reads current CPU and disk temperatures and finds which scheme's
    thresholds match. Falls back to DUAL_MODE_LOW if unknown.
    """
    info = get_all_schemes()
    if not info or not info['schemes']:
        return None

    # Read current temperatures
    cpu_temp = _read_current_cpu_temp()
    disk_temp = _read_current_max_disk_temp()

    best_match = None
    for scheme in info['schemes']:
        if _scheme_matches_temps(scheme, cpu_temp, disk_temp):
            best_match = scheme['type']
            break

    return best_match or 'DUAL_MODE_LOW'


def _read_current_cpu_temp():
    """Read current CPU temperature from hwmon or thermal zone."""
    try:
        for tz in Path('/sys/class/thermal').glob('thermal_zone*'):
            try:
                temp = int(tz.read_text().strip()) / 1000
                return temp
            except (ValueError, OSError):
                continue
    except Exception:
        pass
    return 40  # fallback guess


def _read_current_max_disk_temp():
    """Read max disk temperature from hwmon sensors."""
    try:
        for hw in Path('/sys/class/hwmon').iterdir():
            for temp_file in hw.glob('temp*_input'):
                try:
                    val = int(temp_file.read_text().strip()) / 1000
                    if 20 < val < 80:
                        return val
                except (ValueError, OSError):
                    continue
    except Exception:
        pass
    return 35  # fallback guess


def _scheme_matches_temps(scheme, cpu_temp, disk_temp):
    """Check if current temps match this scheme's temperature thresholds."""
    for entry in scheme.get('entries', []):
        try:
            threshold = float(entry.get('threshold_temp', '0'))
        except (ValueError, TypeError):
            continue

        if entry['sensor_type'] == 'cpu_temperature' and cpu_temp >= threshold:
            return True
        if entry['sensor_type'] == 'disk_temperature' and disk_temp >= threshold:
            return True
    return False


def update_scheme_entry(scheme_type, index, fan_speed_pct=None, action=None, threshold_temp=None):
    """Update a single entry in a scheme.

    Args:
        scheme_type: e.g. 'DUAL_MODE_LOW'
        index: entry index within the scheme
        fan_speed_pct: new fan speed percentage (0-100)
        action: new action ('NONE', 'SHUTDOWN')
        threshold_temp: new threshold temperature
    """
    tree = _parse_scemd()
    if tree is None:
        return False

    root = tree.getroot()
    changed = False

    # Backup on first write
    _ensure_backup()

    # Find the fan_config element
    fc = _find_fan_config(root, scheme_type)
    if fc is None:
        logger.error(f'Scheme {scheme_type} not found in scemd.xml')
        return False

    # Find the entry by index
    entries = [c for c in fc if c.tag in ('disk_temperature', 'cpu_temperature')]
    if index < 0 or index >= len(entries):
        logger.error(f'Entry index {index} out of range (0-{len(entries)-1})')
        return False

    elem = entries[index]

    if fan_speed_pct is not None:
        new_speed = str(max(0, min(100, int(fan_speed_pct))))
        old_speed = elem.attrib.get('fan_speed', '')
        if old_speed != new_speed:
            elem.attrib['fan_speed'] = new_speed
            changed = True
            logger.info(f'{scheme_type}[{index}] fan_speed: {old_speed} -> {new_speed}')

    if action is not None:
        old_action = elem.attrib.get('action', 'NONE')
        if old_action != action:
            elem.attrib['action'] = action
            changed = True
            logger.info(f'{scheme_type}[{index}] action: {old_action} -> {action}')

    if threshold_temp is not None:
        old_temp = elem.text.strip() if elem.text else '0'
        new_temp = str(int(threshold_temp))
        if old_temp != new_temp:
            elem.text = new_temp
            changed = True
            logger.info(f'{scheme_type}[{index}] threshold: {old_temp} -> {new_temp}')

    if not changed:
        return True

    return _write_and_restart(tree)


def update_scheme(scheme_type, entries):
    """Replace all entries in a scheme.

    Args:
        scheme_type: e.g. 'DUAL_MODE_LOW'
        entries: list of dicts with keys: sensor_type, fan_speed, action, threshold_temp
    """
    tree = _parse_scemd()
    if tree is None:
        return False

    root = tree.getroot()
    _ensure_backup()

    fc = _find_fan_config(root, scheme_type)
    if fc is None:
        logger.error(f'Scheme {scheme_type} not found')
        return False

    # Remove existing temperature children
    for child in list(fc):
        if child.tag in ('disk_temperature', 'cpu_temperature'):
            fc.remove(child)

    # Add new entries
    for entry in entries:
        tag = entry.get('sensor_type', 'disk_temperature')
        elem = ET.SubElement(fc, tag)
        elem.attrib['fan_speed'] = str(entry.get('fan_speed', '20'))
        elem.attrib['action'] = entry.get('action', 'NONE')
        elem.text = str(entry.get('threshold_temp', '0'))

    logger.info(f'Updated scheme {scheme_type} with {len(entries)} entries')
    return _write_and_restart(tree)


def set_dsm_fan_speed(percent):
    """Set fan speed via scemd.xml for ALL schemes. Restarts scemd service."""
    tree = _parse_scemd()
    if tree is None:
        logger.error('Cannot set DSM fan speed: scemd.xml not parseable')
        return False

    root = tree.getroot()
    percent = max(0, min(100, int(percent)))
    _ensure_backup()

    changed = False

    # Update fan_config children (official format)
    for fc in root.iter('fan_config'):
        for child in fc:
            if child.tag in ('disk_temperature', 'cpu_temperature'):
                old = child.attrib.get('fan_speed', '')
                child.attrib['fan_speed'] = str(percent)
                if old != str(percent):
                    changed = True

    # Update flat format (no fan_config wrapper)
    for child in root:
        if child.tag in ('disk_temperature', 'cpu_temperature') and child.getparent() is root:
            old = child.attrib.get('fan_speed', '')
            child.attrib['fan_speed'] = str(percent)
            if old != str(percent):
                changed = True

    if not changed:
        logger.info(f'DSM fan speed already at {percent}%')
        return True

    return _write_and_restart(tree)


def _find_fan_config(root, scheme_type):
    """Find a <fan_config> element by its type attribute."""
    for fc in root.iter('fan_config'):
        if fc.attrib.get('type') == scheme_type:
            return fc
    return None


def _ensure_backup():
    """Create backup of scemd.xml if not already done."""
    if not Path(SCEMD_BACKUP).exists():
        try:
            import shutil
            shutil.copy2(SCEMD_PATH, SCEMD_BACKUP)
            logger.info(f'Backed up {SCEMD_PATH} to {SCEMD_BACKUP}')
        except Exception as e:
            logger.warning(f'Failed to backup scemd.xml: {e}')


def _write_and_restart(tree):
    """Write tree back to scemd.xml, verify integrity, then restart service."""
    try:
        tree.write(SCEMD_PATH, encoding='unicode', xml_declaration=False)
        logger.info(f'Wrote {SCEMD_PATH}')
        _invalidate_scemd_cache()
    except Exception as e:
        logger.error(f'Failed to write {SCEMD_PATH}: {e}')
        return False

    # Verify written XML is parseable before restarting scemd
    try:
        from xml.etree import ElementTree
        ElementTree.parse(SCEMD_PATH)
    except Exception as e:
        logger.error(f'Written XML is corrupt, restoring backup: {e}')
        _restore_backup()
        return False

    return _restart_scemd()


def _restore_backup():
    """Restore scemd.xml from backup."""
    import shutil
    backup = Path(SCEMD_BACKUP)
    if backup.exists():
        try:
            shutil.copy2(backup, SCEMD_PATH)
            _invalidate_scemd_cache()
            logger.info(f'Restored {SCEMD_PATH} from backup')
        except Exception as e:
            logger.error(f'Failed to restore backup: {e}')
    else:
        logger.error('No backup available to restore')


def _restart_scemd():
    """Restart the scemd service to apply fan settings."""
    for cmd in [
        ['systemctl', 'restart', 'scemd'],
        ['synoservice', '--restart', 'scemd'],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                logger.info(f'scemd restarted successfully via {cmd[0]}')
                return True
            else:
                logger.warning(f'{cmd[0]} failed: {result.stderr}')
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning(f'{cmd[0]} error: {e}')
            continue

    logger.error('Failed to restart scemd service')
    return False

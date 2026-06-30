"""DSM fan control via scemd.xml — fallback for official kernel xpenology."""

import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger('fancontrol')

SCEMD_PATH = '/usr/syno/etc.defaults/scemd.xml'
SCEMD_BACKUP = '/usr/syno/etc.defaults/scemd.xml.bak'


def is_dsm_fan_available():
    """Check if scemd.xml exists and can be used for fan control."""
    return Path(SCEMD_PATH).exists() and os.access(SCEMD_PATH, os.R_OK | os.W_OK)


def _parse_scemd():
    """Parse scemd.xml and return the tree."""
    try:
        tree = ET.parse(SCEMD_PATH)
        return tree
    except ET.ParseError as e:
        logger.error(f'Failed to parse {SCEMD_PATH}: {e}')
        return None
    except Exception as e:
        logger.error(f'Error reading {SCEMD_PATH}: {e}')
        return None


def get_dsm_fan_info():
    """Get info about DSM fan control configuration."""
    tree = _parse_scemd()
    if tree is None:
        return None

    root = tree.getroot()
    info = {'modes': [], 'hw_version': None}

    # Find CPU temperature section
    for section in root.iter('cpu_temperature'):
        mode = section.attrib.get('DUAL_MODE_LOW') or section.attrib.get('DUAL_MODE_HIGH')
        hw = section.attrib.get('hw_version')
        if hw:
            info['hw_version'] = hw
        if mode is not None:
            info['modes'].append({
                'type': 'cpu',
                'mode': 'low',
                'fan_speed': int(mode) if mode.isdigit() else 0,
            })

    for section in root.iter('disk_temperature'):
        mode = section.attrib.get('DUAL_MODE_LOW') or section.attrib.get('DUAL_MODE_HIGH')
        if mode is not None:
            info['modes'].append({
                'type': 'disk',
                'mode': 'low',
                'fan_speed': int(mode) if mode.isdigit() else 0,
            })

    return info


def set_dsm_fan_speed(percent):
    """
    Set fan speed via scemd.xml. Sets ALL fan modes (cpu + disk, low + high)
    to the same percentage. Restarts scemd service to apply.
    """
    tree = _parse_scemd()
    if tree is None:
        logger.error('Cannot set DSM fan speed: scemd.xml not parseable')
        return False

    root = tree.getroot()
    percent = max(0, min(100, int(percent)))

    changed = False

    # Backup on first write
    if not Path(SCEMD_BACKUP).exists():
        try:
            import shutil
            shutil.copy2(SCEMD_PATH, SCEMD_BACKUP)
            logger.info(f'Backed up {SCEMD_PATH} to {SCEMD_BACKUP}')
        except Exception as e:
            logger.warning(f'Failed to backup scemd.xml: {e}')

    # Update cpu_temperature sections
    for section in root.iter('cpu_temperature'):
        for attr in ('DUAL_MODE_LOW', 'DUAL_MODE_HIGH'):
            if attr in section.attrib:
                old = section.attrib[attr]
                section.attrib[attr] = str(percent)
                if old != str(percent):
                    changed = True
                    logger.info(f'cpu_temperature {attr}: {old} -> {percent}')

        # Also update individual temperature entries
        for temp_elem in section.findall('temperature'):
            old_speed = temp_elem.attrib.get('fan_speed')
            if old_speed is not None:
                temp_elem.attrib['fan_speed'] = str(percent)
                if old_speed != str(percent):
                    changed = True

    # Update disk_temperature sections
    for section in root.iter('disk_temperature'):
        for attr in ('DUAL_MODE_LOW', 'DUAL_MODE_HIGH'):
            if attr in section.attrib:
                old = section.attrib[attr]
                section.attrib[attr] = str(percent)
                if old != str(percent):
                    changed = True
                    logger.info(f'disk_temperature {attr}: {old} -> {percent}')

        for temp_elem in section.findall('temperature'):
            old_speed = temp_elem.attrib.get('fan_speed')
            if old_speed is not None:
                temp_elem.attrib['fan_speed'] = str(percent)
                if old_speed != str(percent):
                    changed = True

    if not changed:
        logger.info(f'DSM fan speed already at {percent}%')
        return True

    # Write back
    try:
        tree.write(SCEMD_PATH, encoding='unicode', xml_declaration=False)
        logger.info(f'Wrote {SCEMD_PATH} with fan_speed={percent}%')
    except Exception as e:
        logger.error(f'Failed to write {SCEMD_PATH}: {e}')
        return False

    # Restart scemd service
    return _restart_scemd()


def _restart_scemd():
    """Restart the scemd service to apply fan settings."""
    # Try DSM 7 first, then DSM 6
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

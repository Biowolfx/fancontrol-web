"""Detect kernel type: official Synology vs custom ARC loader."""

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger('fancontrol')

KERNEL_UNKNOWN = 'unknown'
KERNEL_OFFICIAL = 'official'
KERNEL_CUSTOM = 'custom'


def detect_kernel_type():
    """
    Detect whether running on official Synology kernel or custom ARC kernel.
    Returns one of: 'official', 'custom', 'unknown'
    """
    # Method 1: Check /proc/version for ARC or custom kernel markers
    try:
        with open('/proc/version', 'r') as f:
            version_str = f.read().strip()
        logger.info(f'[kernel] /proc/version: {version_str}')

        # Custom ARC kernels often contain specific markers
        if any(marker in version_str.lower() for marker in ['arc', 'junior', 'arpl', 'rr']):
            logger.info('[kernel] Detected custom kernel via /proc/version markers')
            return KERNEL_CUSTOM
    except Exception as e:
        logger.warning(f'[kernel] Cannot read /proc/version: {e}')

    # Method 2: Check if Synology proprietary fan modules exist
    try:
        result = subprocess.run(
            ['lsmod'], capture_output=True, text=True, timeout=5
        )
        modules = result.stdout.lower()
        # syno_hddtemp or syno_fan indicate official kernel with Synology drivers
        if 'syno_fan' in modules or 'syno_hddtemp' in modules:
            logger.info('[kernel] Detected official kernel via Synology modules')
            return KERNEL_OFFICIAL
    except Exception:
        pass

    # Method 3: Check /sys/module for Synology-specific modules
    syno_modules = list(Path('/sys/module').glob('syno_*'))
    if syno_modules:
        logger.info(f'[kernel] Detected official kernel via /sys/module/syno_*: {[m.name for m in syno_modules]}')
        return KERNEL_OFFICIAL

    # Method 4: Check for hwmon pwm* files (custom kernel usually has them)
    try:
        hwmon_dir = Path('/sys/class/hwmon')
        for hw in hwmon_dir.iterdir():
            if list(hw.glob('pwm*')):
                logger.info('[kernel] Detected custom kernel via hwmon pwm* files')
                return KERNEL_CUSTOM
    except Exception:
        pass

    # Method 5: Check DSM version + architecture
    try:
        result = subprocess.run(
            ['uname', '-r'], capture_output=True, text=True, timeout=5
        )
        kernel_release = result.stdout.strip()
        logger.info(f'[kernel] uname -r: {kernel_release}')

        # Official Synology kernels have specific version patterns
        # e.g., 4.4.302+ (DSM 7.1) or 4.4.180+ (DSM 6.2)
        # Custom kernels may differ
        if '+' in kernel_release:
            # Could be either — need more signals
            pass
    except Exception:
        pass

    # Method 6: Check if scemd.xml exists (always present on official DSM)
    scemd_exists = Path('/usr/syno/etc.defaults/scemd.xml').exists()
    pwm_exists = any(Path('/sys/class/hwmon').glob('*/pwm*')) if Path('/sys/class/hwmon').exists() else False

    if scemd_exists and not pwm_exists:
        logger.info('[kernel] Detected official kernel: scemd.xml present, no hwmon pwm*')
        return KERNEL_OFFICIAL
    elif pwm_exists:
        logger.info('[kernel] Detected custom kernel: hwmon pwm* files present')
        return KERNEL_CUSTOM
    elif scemd_exists:
        logger.info('[kernel] Detected official kernel: scemd.xml present')
        return KERNEL_OFFICIAL

    logger.warning('[kernel] Could not determine kernel type, assuming unknown')
    return KERNEL_UNKNOWN


def get_kernel_info():
    """Get detailed kernel information."""
    info = {
        'type': detect_kernel_type(),
        'version': '',
        'has_hwmon_pwm': False,
        'has_scemd': False,
        'has_ipmi': False,
        'syno_modules': [],
    }

    try:
        result = subprocess.run(['uname', '-a'], capture_output=True, text=True, timeout=5)
        info['version'] = result.stdout.strip()
    except Exception:
        pass

    info['has_hwmon_pwm'] = bool(list(Path('/sys/class/hwmon').glob('*/pwm*'))) if Path('/sys/class/hwmon').exists() else False
    info['has_scemd'] = Path('/usr/syno/etc.defaults/scemd.xml').exists()
    info['has_ipmi'] = bool(list(Path('/dev').glob('ipmi*')))

    info['syno_modules'] = [m.name for m in Path('/sys/module').glob('syno_*')] if Path('/sys/module').exists() else []

    return info

import copy
import hashlib
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from core.state import state, state_lock

logger = logging.getLogger('fancontrol')

executor = ThreadPoolExecutor(max_workers=8)

HWMON_DIR = Path(os.getenv('FANCONTROL_HWMON_DIR', '/sys/class/hwmon'))

CALIBRATION_STEPS = [
    0, 13, 26, 38, 51, 64, 77, 89, 102, 115, 128,
    140, 153, 166, 179, 191, 204, 217, 230, 242, 255
]
CALIBRATION_SETTLE_TIME = 5


def generate_stable_id(path: str) -> str:
    """Generate stable, safe ID from hardware path using SHA256 hash"""
    hash_obj = hashlib.sha256(path.encode())
    return f"dev-{hash_obj.hexdigest()[:12]}"


def discover_fans_and_sensors() -> Tuple[Dict, Dict]:
    """
    Scan /sys/class/hwmon for fans and temperature sensors.
    Returns (fans_dict, temp_sensors_dict)
    """
    logger.info('=' * 50)
    logger.info('SCANNING HARDWARE MONITORS')
    
    fans = {}
    temps = {}
    
    for hw_path in sorted(HWMON_DIR.iterdir()):
        try:
            chip_name = "unknown"
            name_file = hw_path / 'name'
            if name_file.exists():
                chip_name = name_file.read_text().strip()
            
            logger.info(f'  Chip: {hw_path.name} ({chip_name})')
            
            # Discover PWM fans
            for pwm_file in sorted(hw_path.glob('pwm*')):
                if '_' in pwm_file.name:
                    continue
                
                try:
                    pwm_num = re.search(r'\d+', pwm_file.name).group()
                    fan_input = hw_path / f'fan{pwm_num}_input'
                    
                    if not fan_input.exists():
                        continue
                    
                    label_file = hw_path / f'fan{pwm_num}_label'
                    if label_file.exists():
                        label = label_file.read_text().strip()
                    else:
                        label = f'Fan {pwm_num}'
                    
                    writable = os.access(str(pwm_file), os.W_OK)
                    
                    try:
                        current_rpm = int(fan_input.read_text().strip())
                    except (ValueError, OSError):
                        current_rpm = 0
                    
                    fan_path_str = f'{hw_path.name}/{pwm_file.name}'
                    fan_id = generate_stable_id(fan_path_str)
                    
                    fans[fan_id] = {
                        'id': fan_id,
                        'label': label,
                        'hw_path': fan_path_str,
                        'pwm_path': str(pwm_file),
                        'fan_path': str(fan_input),
                        'rpm': current_rpm,
                        'pwm_value': 0,
                        'writable': writable,
                        'inverted': False,
                        'min_rpm': 0,
                        'max_rpm': 0,
                        'manual_pct': 50,
                        'sensors': [],
                        'sensor_mode': 'max',
                        'target_temp': 31,
                        'mode': 'manual',
                        'status': 'not_tested',
                        'target_pwm': 50,
                        'current_pct': 50,
                        'raw_pwm': 128,
                        'last_update': 0.0,
                        'schedule': [],
                        'curve': [],
                        'calibration': {}
                    }
                    
                except Exception as e:
                    logger.warning(f'    Error reading fan {pwm_file}: {e}')
                    continue
            
            # Discover temperature sensors
            for temp_file in sorted(hw_path.glob('temp*_input')):
                try:
                    temp_name = temp_file.name.replace('_input', '')
                    
                    label_file = hw_path / f'{temp_name}_label'
                    if label_file.exists():
                        label = label_file.read_text().strip()
                    else:
                        label = 'Temp'
                    
                    try:
                        temp_value = int(temp_file.read_text().strip()) // 1000
                    except (ValueError, OSError):
                        temp_value = 0
                    
                    temp_path_str = f'{hw_path.name}/{temp_name}'
                    temp_id = generate_stable_id(temp_path_str)
                    
                    temps[temp_id] = {
                        'id': temp_id,
                        'path': str(temp_file),
                        'label': label,
                        'value': temp_value
                    }
                    
                except Exception as e:
                    logger.warning(f'    Error reading temp sensor {temp_file}: {e}')
                    continue
                    
        except Exception as e:
            logger.warning(f'  Skipped {hw_path.name}: {e}')
            continue
    
    logger.info(f'  Found: {len(fans)} fans, {len(temps)} temp sensors')
    return fans, temps


def is_physical_disk(dev_name: str) -> bool:
    """Check if device name represents a physical disk"""
    patterns = [
        r'^sata\d+$',
        r'^nvme\d+n\d+$',
        r'^sd[a-z]$',
        r'^sd[a-z]{2,}$',
    ]
    
    if any(re.match(p, dev_name) for p in patterns):
        return True
    
    if any(dev_name.startswith(p) for p in ['hd', 'xvd', 'vd']):
        if not re.search(r'\d$', dev_name):
            return True
    
    return False


def calculate_disk_health(temp: float) -> Dict[str, Any]:
    """Calculate disk health metrics for UI display"""
    if temp <= 0:
        return {'pct_fill': 0, 'color_zone': 'unknown', 'status': 'standby'}
    
    temp = max(10, min(80, temp))
    pct_fill = max(0, min(100, int((temp - 20) / (60 - 20) * 100)))
    
    if temp <= 35:
        color_zone = 'cyan'
    elif temp <= 45:
        color_zone = 'orange'
    elif temp <= 55:
        color_zone = 'red'
    else:
        color_zone = 'critical'
    
    return {'pct_fill': pct_fill, 'color_zone': color_zone, 'status': 'active'}


def parse_smart_temp(output: str) -> Optional[int]:
    """Parse temperature from smartctl output"""
    for line in output.split('\n'):
        if 'Temperature_Celsius' in line:
            match = re.search(r'(\d+)\s*\(', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100:
                    return temp
            
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70:
                    return temp
    
    for line in output.split('\n'):
        if 'Airflow_Temperature_Cel' in line:
            numbers = re.findall(r'\b(\d{2,3})\b', line)
            for num in numbers:
                temp = int(num)
                if 15 < temp < 70:
                    return temp
    
    return None


def read_disk_temp(disk_identifier: str) -> Tuple[Optional[float], bool]:
    """
    Read temperature from a disk.
    Returns (temperature_celsius, is_standby)
    """
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()
        
        if not is_physical_disk(clean_name):
            return None, False
        
        cmd = ['smartctl', '-A', '-n', 'standby', f'/dev/{clean_name}']
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode == 2:
                return None, True
            
            if result.returncode != 0:
                cmd2 = ['smartctl', '-A', '-n', 'standby', '-d', 'sat', f'/dev/{clean_name}']
                result = subprocess.run(cmd2, capture_output=True, text=True, timeout=10)
                
                if result.returncode == 2:
                    return None, True
                if result.returncode != 0:
                    return None, False
            
            if clean_name.startswith('nvme'):
                for line in result.stdout.split('\n'):
                    if 'Temperature:' in line:
                        match = re.search(r'(\d+)\s*Celsius', line)
                        if match:
                            return float(match.group(1)), False
            
            temp = parse_smart_temp(result.stdout)
            if temp is not None:
                return float(temp), False
                
        except subprocess.TimeoutExpired:
            logger.debug(f'Timeout reading {disk_identifier}')
            return None, False
            
    except Exception as e:
        logger.error(f'Error reading disk {disk_identifier}: {e}')
    
    return None, False


def discover_disks() -> Dict[str, Dict]:
    """
    Discover physical disks in the system.
    Returns cached data if polling is already in progress.
    """
    logger.info('=' * 50)
    logger.info('DISK DISCOVERY')
    
    with state_lock:
        if state.get('disks_polling'):
            logger.warning('Disk polling already in progress, returning cached data')
            return copy.deepcopy(state['hdd_sensors'])
        state['disks_polling'] = True
    
    try:
        disks = {}
        discovered_devices = set()
        
        # Method 1: lsblk
        try:
            result = subprocess.run(
                ['lsblk', '-nd', '-o', 'NAME,TYPE,TRAN'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0].strip()
                    dtype = parts[1].strip()
                    
                    if dtype == 'disk' and is_physical_disk(name):
                        skip_prefixes = ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']
                        if not any(name.startswith(p) for p in skip_prefixes):
                            discovered_devices.add(name)
                            
        except Exception as e:
            logger.warning(f'lsblk failed: {e}')
            
            try:
                for dev_path in Path('/sys/block').iterdir():
                    name = dev_path.name
                    if is_physical_disk(name):
                        skip_prefixes = ['loop', 'ram', 'zram', 'dm-', 'md', 'sr', 'iscsi', 'synoboot']
                        if not any(name.startswith(p) for p in skip_prefixes):
                            discovered_devices.add(name)
            except Exception as e2:
                logger.warning(f'/sys/block fallback failed: {e2}')
        
        # Read temperatures in parallel
        futures_map = {
            dev: executor.submit(read_disk_temp, dev)
            for dev in sorted(discovered_devices)
        }
        
        for dev, future in futures_map.items():
            try:
                result = future.result(timeout=10)
                temp, standby = result if result else (None, False)
                
                disk_id = generate_stable_id(f'/dev/{dev}')
                
                if dev.startswith('sata'):
                    disk_label = f'SATA {dev.replace("sata", "")}'
                    disk_type = 'sata'
                elif dev.startswith('nvme'):
                    disk_label = f'NVMe {dev}'
                    disk_type = 'nvme'
                else:
                    disk_label = f'Disk {dev}'
                    disk_type = 'sata'
                
                health = calculate_disk_health(temp if temp else 0)
                
                disks[disk_id] = {
                    'id': disk_id,
                    'label': disk_label,
                    'device': f'/dev/{dev}',
                    'dev_name': dev,
                    'temp': temp if temp else 0,
                    'standby': standby,
                    'type': disk_type,
                    'pct_fill': health['pct_fill'],
                    'color_zone': health['color_zone'],
                    'health_status': health['status']
                }
                
            except FutureTimeout:
                logger.warning(f'Timeout polling disk {dev}')
            except Exception as e:
                logger.error(f'Failed to poll disk {dev}: {e}')
        
        logger.info(f'  Discovered: {len(disks)} disks')
        return disks
    
    finally:
        with state_lock:
            state['disks_polling'] = False


def set_pwm(key: str, raw_pwm: int, raw: bool = False):
    """
    Set PWM value. When raw=True, writes physical value directly without
    inversion handling or RPM reading (used during calibration).
    """
    with state_lock:
        fan = state['fans'].get(key)
        if not fan or not fan.get('pwm_path', '').startswith('/sys/class/hwmon/'):
            return
        
        val = max(0, min(255, int(raw_pwm)))
        
        if not raw:
            fan['raw_pwm'] = val
        
        physical_pwm = (255 - val) if (not raw and fan.get('inverted')) else val
        
        try:
            Path(fan['pwm_path']).write_text(str(physical_pwm))
            fan['pwm_value'] = val
            
            if not raw:
                try:
                    rpm_raw = Path(fan['fan_path']).read_text().strip()
                    rpm_val = int(rpm_raw) if rpm_raw.isdigit() else 0
                    if rpm_val > 0:
                        fan['rpm'] = rpm_val
                except Exception:
                    pass
                
                fan['last_update'] = time.monotonic()
            
        except Exception as e:
            logger.error(f'PWM write error {key}: {e}')


def refresh():
    """Update temperature and RPM readings"""
    with state_lock:
        temp_items = [(k, v.copy()) for k, v in state['temp_sensors'].items()]
        fan_items = [(k, v.copy()) for k, v in state['fans'].items()]
    
    for key, sensor in temp_items:
        try:
            value = int(Path(sensor['path']).read_text().strip()) // 1000
        except Exception:
            continue
        
        with state_lock:
            if key in state['temp_sensors']:
                state['temp_sensors'][key]['value'] = value
    
    def poll_fan(item):
        k, fan = item
        try:
            rpm = int(Path(fan['fan_path']).read_text().strip())
        except Exception:
            rpm = None
        return k, rpm
    
    futures = [executor.submit(poll_fan, item) for item in fan_items]
    for future in futures:
        try:
            key, rpm = future.result(timeout=2)
            if rpm is not None:
                with state_lock:
                    if key in state['fans']:
                        state['fans'][key]['rpm'] = rpm
        except Exception:
            pass

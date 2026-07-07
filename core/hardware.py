import copy
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from core.state import state, state_lock

logger = logging.getLogger('fancontrol')

_has_smartctl = shutil.which('smartctl') is not None
if not _has_smartctl:
    logger.warning('smartctl not found — SMART data and smartctl-based temp reading unavailable')

executor = ThreadPoolExecutor(max_workers=16)

from core.config import cfg
HWMON_DIR = cfg.hwmon_dir

CALIBRATION_STEPS = [
    0, 25, 51, 76, 102, 127, 153, 178, 204, 229, 255
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
                        'calibration': {},
                        'health': {
                            'status': 'healthy',
                            'rpm_baseline': 0,
                            'slowdown_since': None,
                            'stopped_since': None,
                            'last_service_date': None,
                            'calibration_required': False,
                        }
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

    # Fallback: DSM scemd.xml fan control (official kernel xpenology)
    if not fans:
        from core.dsm_fan import is_dsm_fan_available, get_dsm_fan_info
        if is_dsm_fan_available():
            logger.info('  No hwmon fans found, trying DSM scemd.xml...')
            dsm_info = get_dsm_fan_info()
            if dsm_info:
                fan_id = generate_stable_id('dsm-fan-0')
                fans[fan_id] = {
                    'id': fan_id,
                    'label': f'DSM Fan ({dsm_info.get("hw_version", "unknown")})',
                    'hw_path': 'dsm-scemd',
                    'pwm_path': '',
                    'fan_path': '',
                    'rpm': 0,
                    'pwm_value': 0,
                    'writable': True,
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
                    'calibration': {},
                    'control_method': 'dsm_scemd',
                    'health': {
                        'status': 'healthy',
                        'rpm_baseline': 0,
                        'slowdown_since': None,
                        'stopped_since': None,
                        'last_service_date': None,
                        'calibration_required': False,
                    }
                }
                modes = dsm_info.get('modes', [])
                current_speed = modes[0].get('fan_speed', 0) if modes else 0
                logger.info(f'  DSM fan detected: {fan_id}, current speed: {current_speed}%')
            else:
                logger.info('  DSM scemd.xml found but could not parse fan info')

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


SMART_ATTRIBUTE_META = {
    1: {"name": "Raw_Read_Error_Rate", "criticality": "important", "description": "Частота ошибок чтения", "tooltip": "Рост указывает на деградацию поверхности диска или проблемы с головками."},
    2: {"name": "Throughput_Performance", "criticality": "info", "description": "Производительность", "tooltip": "Общая производительность диска. Снижение может указывать на фрагментацию."},
    3: {"name": "Spin_Up_Time", "criticality": "info", "description": "Время раскрутки", "tooltip": "Время запуска шпинделя. Рост может указывать на износ механики."},
    4: {"name": "Start_Stop_Count", "criticality": "info", "description": "Количество запусков", "tooltip": "Сколько раз диск включался/выключался. Нормальный износ."},
    5: {"name": "Reallocated_Sector_Ct", "criticality": "critical", "description": "Переназначенные сектора", "tooltip": "Количество переназначенных секторов. Рост означает физическую деградацию поверхности диска. Рост > 0 требует замены диска."},
    7: {"name": "Seek_Error_Rate", "criticality": "important", "description": "Частота ошибок позиционирования", "tooltip": "Рост указывает на проблемы с блоком головок или фрагментацией."},
    8: {"name": "Seek_Time_Performance", "criticality": "info", "description": "Время позиционирования", "tooltip": "Среднее время поиска. Снижение = механический износ."},
    9: {"name": "Power_On_Hours", "criticality": "info", "description": "Часы работы", "tooltip": "Общее время работы диска. Нормальный износ, ресурс 30000-50000 часов.", "unit": "hours", "unit_divisor": 1},
    10: {"name": "Spin_Retry_Count", "criticality": "critical", "description": "Повторы раскрутки", "tooltip": "Количество повторных попыток раскрутки шпинделя. Рост = механическая проблема, замена обязательна."},
    11: {"name": "Calibration_Retry_Count", "criticality": "important", "description": "Повторы калибровки", "tooltip": "Неудачные попытки калибровки головок. Рост может привести к ошибкам чтения."},
    12: {"name": "Power_Cycle_Count", "criticality": "info", "description": "Циклы включения", "tooltip": "Количество включений/выключений питания."},
    13: {"name": "Read_Soft_Error_Rate", "criticality": "info", "description": "Программные ошибки чтения", "tooltip": "Ошибки, исправленные ECC. Временные ошибки, обычно не критичны."},
    15: {"name": "Seek_Time_Performance", "criticality": "info", "description": "Время позиционирования", "tooltip": "Среднее время поиска. Снижение = механический износ."},
    17: {"name": "Power_On_Hours", "criticality": "info", "description": "Часы работы", "tooltip": "Общее время работы диска в часах.", "unit": "hours", "unit_divisor": 1},
    18: {"name": "Available_Spare_Threshold", "criticality": "info", "description": "Порог доступного запаса", "tooltip": "Минимально допустимый процент резервных блоков. При достижении — замена обязательна."},
    19: {"name": "Available_Spare", "criticality": "critical", "description": "Доступный запас", "tooltip": "Текущий процент резервных блоков. 0% = ресурс исчерпан, замена обязательна."},
    22: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки", "tooltip": "Количество перемещений головок."},
    170: {"name": "Grown_Failing_Block_Ct", "criticality": "critical", "description": "Выросшие坏块", "tooltip": "Блоки, отмеченные как坏块 после изготовления. Рост = деградация поверхности."},
    171: {"name": "Program_Fail_Count", "criticality": "important", "description": "Ошибки записи", "tooltip": "Неудачные попытки записи. Рост может указывать на проблемы с NAND (SSD)."},
    172: {"name": "Erase_Fail_Count", "criticality": "important", "description": "Ошибки стирания", "tooltip": "Неудачные попытки стирания. Рост = проблема с ячейками памяти (SSD)."},
    173: {"name": "Wear_Leveling_Count", "criticality": "important", "description": "Уровень износа", "tooltip": "Минимальный износ блоков. Для SSD: рост = приближение к концу ресурса."},
    175: {"name": "Program_Fail_Count_Chip", "criticality": "important", "description": "Ошибки программы (чип)", "tooltip": "Ошибки записи на уровне чипа. Рост = проблема с ячейками."},
    176: {"name": "Erase_Fail_Count_Chip", "criticality": "important", "description": "Ошибки стирания (чип)", "tooltip": "Ошибки стирания на уровне чипа. Рост = проблема с ячейками."},
    177: {"name": "Wear_Leveling_Count", "criticality": "important", "description": "Износ блоков", "tooltip": "Количество перераспределенных блоков. Для SSD."},
    178: {"name": "Used_Rsvd_Blk_Ct_Chip", "criticality": "critical", "description": "Использовано резервных блоков", "tooltip": "Резервные блоки исчерпываются. 0 резерва = замена обязательна."},
    179: {"name": "Used_Rsvd_Blk_Ct_Tot", "criticality": "critical", "description": "Всего использовано резервных", "tooltip": "Общее число использованных резервных блоков."},
    180: {"name": "Unused_Rsvd_Blk_Ct_Chip", "criticality": "info", "description": "Свободных резервных блоков", "tooltip": "Остаток резервных блоков. Чем меньше — тем ближе замена."},
    181: {"name": "Program_Fail_Cnt_Total", "criticality": "important", "description": "Всего ошибок записи", "tooltip": "Суммарные ошибки записи за весь срок службы."},
    182: {"name": "Erase_Fail_Count_Total", "criticality": "important", "description": "Всего ошибок стирания", "tooltip": "Суммарные ошибки стирания за весь срок службы."},
    183: {"name": "Runtime_Bad_Block", "criticality": "critical", "description": "坏块 при работе", "tooltip": "坏块, обнаруженные во время работы. Рост = деградация."},
    184: {"name": "End_Ecc_Error", "criticality": "critical", "description": "Исправленные ECC ошибки", "tooltip": "ECC-исправленные ошибки. Рост = проблема с памятью."},
    187: {"name": "Airflow_Temperature_Cel", "criticality": "important", "description": "Температура воздуха", "tooltip": "Температура воздушного потока у диска. Оптимально: 25-45°C."},
    188: {"name": "G_Sense_Error_Rate", "criticality": "important", "description": "Ошибки от вибрации", "tooltip": "Ошибки, вызванные ударами/вибрацией. Рост = физическое повреждение."},
    190: {"name": "Airflow_Temperature_Cel", "criticality": "important", "description": "Температура воздушного потока", "tooltip": "Температура воздуха у диска. Оптимально: 25-45°C. Выше 50°C — перегрев."},
    191: {"name": "G_Sense_Error_Rate", "criticality": "important", "description": "Ошибки от удара", "tooltip": "Ошибки, вызванные ударами/вибрацией. Рост = физическое повреждение."},
    192: {"name": "Power-Off_Retract_Count", "criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество аварийных отключений питания. Рост = риск повреждения головок."},
    193: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки", "tooltip": "Количество перемещений головок. Нормальный износ."},
    194: {"name": "Temperature_Celsius", "criticality": "important", "description": "Температура", "tooltip": "Текущая температура диска. Оптимально: 25-45°C. Выше 50°C — перегрев."},
    195: {"name": "Hardware_ECC_Recovered", "criticality": "info", "description": "ECC восстановления", "tooltip": "Ошибки, исправленные аппаратным ECC. Временные, обычно не критичны."},
    196: {"name": "Reallocated_Event_Count", "criticality": "critical", "description": "События переназначения", "tooltip": "Количество событий переназначения секторов. Рост = деградация."},
    197: {"name": "Current_Pending_Sector", "criticality": "critical", "description": "Ожидающие сектора", "tooltip": "Сектора, ожидающие перераспределения. Рост может привести к потере данных."},
    198: {"name": "Offline_Uncorrectable", "criticality": "critical", "description": "Неисправимые сектора", "tooltip": "Сектора, которые невозможно прочитать/исправить. Рост = немедленная замена диска."},
    199: {"name": "UDMA_CRC_Error_Count", "criticality": "important", "description": "CRC ошибки интерфейса", "tooltip": "Ошибки checksum интерфейса SATA. Проверьте кабель."},
    200: {"name": "Multi_Zone_Error_Rate", "criticality": "important", "description": "Ошибки по зонам", "tooltip": "Ошибки записи в несколько зон. Рост = деградация поверхности."},
    201: {"name": "Soft_Read_Error_Rate", "criticality": "info", "description": "Программные ошибки чтения", "tooltip": "Ошибки чтения, требующие повтора. Временные."},
    202: {"name": "High_Fly_Writes", "criticality": "critical", "description": "Записи на высоте", "tooltip": "Записи, выполненные вне зоны контакта головки. Рост = риск потери данных."},
    203: {"name": "Run_Out_Cancel", "criticality": "critical", "description": "Отмена из-за нехватки ресурса", "tooltip": "Операции отменены из-за нехватки резервных блоков. Замена обязательна."},
    204: {"name": "Soft_ECC_Correction", "criticality": "info", "description": "Программные ECC исправления", "tooltip": "ECC-исправленные ошибки. Временные, обычно не критичны."},
    205: {"name": "Thermal_Asperity_Rate", "criticality": "important", "description": "Термические помехи", "tooltip": "Ошибки из-за температуры. Рост = перегрев."},
    206: {"name": "Flying_Height", "criticality": "important", "description": "Высота полёта головки", "tooltip": "Расстояние головки от пластин. Снижение = риск контакта."},
    207: {"name": "Spin_Try_Count", "criticality": "critical", "description": "Попытки раскрутки", "tooltip": "Количество попыток раскрутки шпинделя. Рост = механическая проблема."},
    208: {"name": "Spin_Retry_Count", "criticality": "critical", "description": "Повторы раскрутки", "tooltip": "Неудачные попытки раскрутки. Замена обязательна."},
    209: {"name": "Offline_Seek_Perform", "criticality": "info", "description": "Offline позиционирование", "tooltip": "Производительность позиционирования в offline."},
    210: {"name": "Tap_Retry_Count", "criticality": "critical", "description": "Повторы поиска", "tooltip": "Неудачные попытки позиционирования. Рост = деградация."},
    220: {"name": "Power-Off_Retract_Count", "criticality": "important", "description": "Аварийные выключения (head)", "tooltip": "Количество аварийных уборок головок."},
    222: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки (head)", "tooltip": "Количество циклов загрузки/выгрузки головок."},
    223: {"name": "Temperature_Celsius", "criticality": "important", "description": "Температура", "tooltip": "Текущая температура диска. Оптимально: 25-45°C."},
    224: {"name": "G_Sense_Error_Rate", "criticality": "important", "description": "Ошибки от вибрации", "tooltip": "Ошибки, вызванные ударами/вибрацией."},
    225: {"name": "Power-Off_Retract_Count", "criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество аварийных отключений питания."},
    226: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки", "tooltip": "Количество перемещений головок."},
    227: {"name": "Temperature_Celsius", "criticality": "important", "description": "Температура (extended)", "tooltip": "Текущая температура диска."},
    230: {"name": "Head_Flying_Hours", "criticality": "info", "description": "Часы работы головок", "tooltip": "Общее время полёта головок над пластинами. Нормальный износ.", "unit": "hours", "unit_divisor": 1},
    231: {"name": "Head_Flying_Hours", "criticality": "info", "description": "Часы работы головок", "tooltip": "Общее время полёта головок над пластинами.", "unit": "hours", "unit_divisor": 1},
    232: {"name": "Total_LBAs_Written", "criticality": "info", "description": "Всего записано (LBA)", "tooltip": "Общее количество записанных блоков. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    233: {"name": "Total_LBAs_Read", "criticality": "info", "description": "Всего прочитано (LBA)", "tooltip": "Общее количество прочитанных блоков. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    234: {"name": "Read_Error_Retry_Rate", "criticality": "important", "description": "Повторы чтения", "tooltip": "Количество повторных попыток чтения. Рост = деградация."},
    235: {"name": "Hardware_ECC_Recovered", "criticality": "info", "description": "ECC восстановления (v2)", "tooltip": "Ошибки, исправленные аппаратным ECC."},
    240: {"name": "Head_Flying_Hours", "criticality": "info", "description": "Часы полёта головок", "tooltip": "Общее время работы головок над пластинами в часах. Нормальный износ, ресурс 30000-50000 часов.", "unit": "hours", "unit_divisor": 1},
    241: {"name": "Total_LBAs_Written", "criticality": "info", "description": "Всего записано данных", "tooltip": "Общий объём записанных данных на диск. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    242: {"name": "Total_LBAs_Read", "criticality": "info", "description": "Всего прочитано данных", "tooltip": "Общий объём прочитанных данных с диска. Конвертируется в ГБ.", "unit": "bytes", "unit_divisor": 512},
    243: {"name": "Read_Error_Retry_Rate", "criticality": "important", "description": "Повторы чтения (v2)", "tooltip": "Количество повторных попыток чтения. Рост = деградация."},
    244: {"name": "Free_Fall_Sector_Count", "criticality": "important", "description": "Сектора при падении", "tooltip": "Количество ошибок при свободном падении. Рост = физическое повреждение."},
}

_smart_cache: Dict[str, Dict] = {}
_smart_cache_time: Dict[str, float] = {}
_smart_cache_lock = threading.Lock()
SMART_CACHE_TTL = 60  # seconds


def parse_smart_attributes(output: str) -> list:
    """Parse all SMART attributes from smartctl -A output."""
    attributes = []
    in_attribute_section = False

    for line in output.split('\n'):
        line = line.strip()

        if 'ID# ATTRIBUTE_NAME' in line or 'ATTRIBUTE_NAME' in line:
            in_attribute_section = True
            continue

        if not in_attribute_section:
            continue

        if not line or line.startswith('===') or line.startswith('SMART'):
            if attributes:
                break
            continue

        parts = line.split()
        if len(parts) < 10:
            continue

        try:
            attr_id = int(parts[0])
        except ValueError:
            continue

        attr_name = parts[1]
        try:
            flag = int(parts[2], 16) if parts[2].startswith('0x') else int(parts[2])
        except (ValueError, IndexError):
            flag = 0

        try:
            value = int(parts[3])
            worst = int(parts[4])
            thresh = int(parts[5])
        except (ValueError, IndexError):
            continue

        raw_value = parts[9] if len(parts) > 9 else '0'
        try:
            raw_num = int(re.sub(r'[^0-9]', '', raw_value) or '0')
        except ValueError:
            raw_num = 0

        meta = SMART_ATTRIBUTE_META.get(attr_id, {})
        criticality = meta.get('criticality', 'info')

        if thresh > 0 and value <= thresh:
            status = 'critical'
        elif thresh > 0 and value <= thresh * 1.5:
            status = 'warning'
        else:
            status = 'ok'

        attributes.append({
            'id': attr_id,
            'name': attr_name,
            'flag': flag,
            'value': value,
            'worst': worst,
            'threshold': thresh,
            'raw': raw_value,
            'raw_num': raw_num,
            'criticality': criticality,
            'description': meta.get('description', attr_name),
            'tooltip': meta.get('tooltip', f'SMART атрибут #{attr_id}'),
            'status': status,
            'unit': meta.get('unit'),
            'unit_divisor': meta.get('unit_divisor'),
        })

    return attributes


def parse_nvme_smart(output: str) -> dict:
    """Parse NVMe SMART attributes from smartctl output."""
    attributes = {}
    patterns = {
        'critical_warning': r'Critical Warning\s*:\s*(.+)',
        'temperature': r'Temperature:\s+(\d+)\s+Celsius',
        'available_spare': r'Available Spare:\s+(\d+)%',
        'available_spare_threshold': r'Available Spare Threshold:\s+(\d+)%',
        'percentage_used': r'Percentage Used:\s+(\d+)%',
        'data_units_read': r'Data Units Read:\s+([\d,]+)',
        'data_units_written': r'Data Units Written:\s+([\d,]+)',
        'host_reads': r'Host Read Commands:\s+([\d,]+)',
        'host_writes': r'Host Write Commands:\s+([\d,]+)',
        'controller_busy_time': r'Controller Busy Time:\s+([\d,]+)',
        'power_cycles': r'Power Cycles:\s+([\d,]+)',
        'power_on_hours': r'Power On Hours:\s+([\d,]+)',
        'unsafe_shutdowns': r'Unsafe Shutdowns:\s+(\d+)',
        'media_errors': r'Media and Data Integrity Errors:\s+(\d+)',
        'error_log_entries': r'Error Information Log Entries:\s+(\d+)',
        'warning_temp_time': r'Warning Comp\. Temp\. Time:\s+(\d+)',
        'critical_comp_time': r'Critical Comp\. Temp\. Time:\s+(\d+)',
    }

    nvme_meta = {
        'critical_warning': {"criticality": "critical", "description": "Критические ошибки", "tooltip": "Критические ошибки в работе накопителя. Любое ненулевое значение = замена обязательна. Может указывать на перегрев, критический износ или ошибки питания."},
        'temperature': {"criticality": "important", "description": "Температура", "tooltip": "Текущая температура NVMe диска. Оптимально: 25-45°C. Выше 50°C — перегрев, снижение производительности."},
        'available_spare': {"criticality": "critical", "description": "Доступный запас", "tooltip": "Процент резервных блоков для подмены вышедших из строя ячеек. При приближении к порогу — задумайтесь о замене. 0% = ресурс исчерпан, замена обязательна."},
        'available_spare_threshold': {"criticality": "critical", "description": "Порог запаса", "tooltip": "Пороговое значение Available Spare. При достижении этого значения состояние диска считается критическим."},
        'percentage_used': {"criticality": "critical", "description": "Износ NAND", "tooltip": "Уровень износа накопителя в процентах. Зависит от Available Spare и Available Spare Threshold. 100% = ресурс исчерпан."},
        'data_units_read': {"criticality": "info", "description": "Прочитано данных", "tooltip": "Количество прочитанных блоков (1 блок = 512 байт). Информационный параметр. Конвертируется в ГБ.", "unit": "nvme_blocks", "unit_divisor": 512 * 1000},
        'data_units_written': {"criticality": "info", "description": "Записано данных", "tooltip": "Количество записанных блоков (1 блок = 512 байт). Информационный параметр. Конвертируется в ГБ.", "unit": "nvme_blocks", "unit_divisor": 512 * 1000},
        'host_reads': {"criticality": "info", "description": "Операций чтения", "tooltip": "Количество выполненных операций чтения (1 единица ≈ 1 МБ). Информационный параметр."},
        'host_writes': {"criticality": "info", "description": "Операций записи", "tooltip": "Количество выполненных операций записи (1 единица ≈ 1 МБ). Информационный параметр."},
        'controller_busy_time': {"criticality": "info", "description": "Время контроллера", "tooltip": "Время в минутах, когда контроллер был занят обслуживанием запросов системы."},
        'power_cycles': {"criticality": "info", "description": "Циклы включения", "tooltip": "Количество циклов включения/выключения. Нормальный износ."},
        'power_on_hours': {"criticality": "info", "description": "Часы работы", "tooltip": "Общее наработанное время в часах. Нормальный износ, ресурс 30000-50000 часов.", "unit": "hours", "unit_divisor": 1},
        'unsafe_shutdowns': {"criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество небезопасных отключений питания. Рост = риск повреждения данных и NAND-ячеек."},
        'media_errors': {"criticality": "critical", "description": "Ошибки носителя", "tooltip": "Ошибки целостности данных. Рост = проблема с NAND, замена обязательна."},
        'error_log_entries': {"criticality": "important", "description": "Записи журнала ошибок", "tooltip": "Количество записей в журнале ошибок. Рост = повторяющиеся проблемы."},
        'warning_temp_time': {"criticality": "info", "description": "Время при перегреве", "tooltip": "Время работы (в минутах) при высокой температуре. Рост = перегрев."},
        'critical_comp_time': {"criticality": "critical", "description": "Время при крит. темп.", "tooltip": "Время работы (в минутах) при критической температуре. Рост = сильный перегрев, замена обязательна."},
    }

    for key, pattern in patterns.items():
        match = re.search(pattern, output)
        if match:
            value_str = match.group(1).replace(',', '')
            try:
                value = int(value_str)
            except ValueError:
                value = 0

            meta = nvme_meta.get(key, {})
            attributes[key] = {
                'value': value,
                'criticality': meta.get('criticality', 'info'),
                'description': meta.get('description', key),
                'tooltip': meta.get('tooltip', ''),
                'unit': meta.get('unit'),
                'unit_divisor': meta.get('unit_divisor'),
            }

    return attributes


def read_disk_smart(disk_identifier: str) -> dict:
    """
    Read full SMART data for a disk.
    Returns dict with device info, attributes, and metadata.
    Tries multiple access methods: direct, SAT, MegaRAID.
    """
    if not _has_smartctl:
        return {'error': 'smartctl not installed'}
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()

        if not is_physical_disk(clean_name):
            return {'error': 'Not a physical disk'}

        is_nvme = clean_name.startswith('nvme')

        # Extract disk index from name (e.g., sda=0, sdb=1)
        disk_index = -1
        if clean_name.startswith('sd'):
            disk_index = ord(clean_name[2]) - ord('a')
        elif clean_name.startswith('nvme'):
            try:
                disk_index = int(clean_name.split('n')[0].replace('nvme', ''))
            except (ValueError, IndexError):
                pass

        # Try multiple access methods in order
        attempts = []

        if is_nvme:
            attempts.append(['smartctl', '-A', '-i', f'/dev/{clean_name}'])
            attempts.append(['smartctl', '-A', '-i', '-d', 'nvme', f'/dev/{clean_name}'])
        else:
            # 1. Direct access
            attempts.append(['smartctl', '-A', '-i', f'/dev/{clean_name}'])
            # 2. SAT passthrough
            attempts.append(['smartctl', '-A', '-i', '-d', 'sat', f'/dev/{clean_name}'])
            # 3-4. RAID controllers only for sdX devices
            if clean_name.startswith('sd') and disk_index >= 0:
                attempts.append(['smartctl', '-A', '-i', '-d', f'megaraid,{disk_index}', f'/dev/sda'])
                attempts.append(['smartctl', '-A', '-i', '-d', f'areca,{disk_index + 1}', '/dev/arcmsr0'])

        output = ''
        used_cmd = None
        for cmd in attempts:
            try:
                logger.info(f'SMART attempt: {" ".join(cmd)}')
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                stdout = result.stdout or ''

                # Check if output has actual SMART data (not just device info)
                has_device_info = 'Model Family' in stdout or 'Device Model' in stdout or 'Serial Number' in stdout
                has_smart_attrs = 'SMART Attributes' in stdout or 'SMART overall-health' in stdout or 'Raw_Read_Error_Rate' in stdout
                has_nvme_smart = 'SMART/Health' in stdout or 'Available Spare' in stdout

                if has_device_info and (has_smart_attrs or has_nvme_smart):
                    output = stdout
                    used_cmd = cmd
                    logger.info(f'SMART success with attrs: {" ".join(cmd)}')
                    break
                elif has_device_info:
                    logger.info(f'SMART: device info found but no attributes with {" ".join(cmd)}, trying next...')
                elif result.returncode == 0 and stdout.strip():
                    logger.debug(f'SMART: some output but no recognized data: {" ".join(cmd)}')
                if result.stderr:
                    logger.debug(f'SMART stderr: {result.stderr[:200]}')
            except subprocess.TimeoutExpired:
                logger.debug(f'SMART timeout: {" ".join(cmd)}')
                continue

        # If no method found attributes, try one more time with -a (all SMART data)
        if not output:
            for base_dev in [f'/dev/{clean_name}', '/dev/sda']:
                try:
                    cmd = ['smartctl', '-a', base_dev]
                    logger.info(f'SMART fallback -a: {" ".join(cmd)}')
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                    stdout = result.stdout or ''
                    if 'SMART Attributes' in stdout or 'SMART overall-health' in stdout or 'SMART/Health' in stdout:
                        output = stdout
                        used_cmd = cmd
                        logger.info(f'SMART -a success: {" ".join(cmd)}')
                        break
                except Exception:
                    pass

        if not output:
            return {'error': 'smartctl failed — no SMART data available (disk may be behind RAID controller)'}

        device_info = {}
        for line in output.split('\n'):
            if 'Device Model:' in line or 'Model Family:' in line:
                device_info['model'] = line.split(':', 1)[1].strip()
            if 'Serial Number:' in line:
                device_info['serial'] = line.split(':', 1)[1].strip()
            if 'Firmware Version:' in line:
                device_info['firmware'] = line.split(':', 1)[1].strip()
            if 'User Capacity:' in line:
                device_info['capacity'] = line.split(':', 1)[1].strip()

        if is_nvme:
            attributes = parse_nvme_smart(output)
            attr_type = 'nvme'
        else:
            attributes = parse_smart_attributes(output)
            attr_type = 'sata'

        return {
            'device': f'/dev/{clean_name}',
            'device_info': device_info,
            'attributes': attributes,
            'attr_type': attr_type,
            'access_method': ' '.join(used_cmd) if used_cmd else 'unknown',
        }

    except Exception as e:
        logger.error(f'Error reading SMART for {disk_identifier}: {e}')
        return {'error': str(e)}


def read_disk_temp(disk_identifier: str) -> Tuple[Optional[float], bool]:
    """
    Read temperature from a disk.
    Returns (temperature_celsius, is_standby)
    Tries multiple access methods: smartctl direct, SAT, sysfs, SMART attributes.
    Prefers Airflow_Temperature_Cel over Temperature_Celsius for accuracy.
    """
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()

        if not is_physical_disk(clean_name):
            return None, False

        # Method 1: smartctl (skipped if not installed — falls through to hdparm/sysfs)
        if _has_smartctl:
            attempts = []
            attempts.append(['smartctl', '-a', '-n', 'standby', f'/dev/{clean_name}'])
            attempts.append(['smartctl', '-a', '-n', 'standby', '-d', 'sat', f'/dev/{clean_name}'])
            # MegaRAID/Areca only for sdX devices (behind RAID controllers)
            # Skip for Synology proprietary sataX/nvmeX — never behind RAID
            if clean_name.startswith('sd'):
                disk_index = ord(clean_name[2]) - ord('a')
                attempts.append(['smartctl', '-a', '-n', 'standby', '-d', f'megaraid,{disk_index}', '/dev/sda'])
                attempts.append(['smartctl', '-a', '-n', 'standby', '-d', f'areca,{disk_index + 1}', '/dev/arcmsr0'])

            for cmd in attempts:
                try:
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)

                    if result.returncode == 2:
                        logger.info(f'DISK TEMP: {clean_name} standby via {" ".join(cmd)}')
                        return None, True  # standby

                    stdout = result.stdout or ''
                    if not stdout:
                        continue

                    temp, source = _parse_disk_temp_preferred(stdout)
                    if temp is not None:
                        logger.info(f'DISK TEMP: {clean_name} = {temp}°C (source={source}) via {" ".join(cmd)}')
                        for line in stdout.split('\n'):
                            if any(kw in line for kw in ['Airflow', 'HDA_Temp', 'Temperature_Celsius',
                                                          'Current Drive Temperature', 'temperature',
                                                          '190 ', '194 ', '194\t', 'SMART Attributes']):
                                logger.info(f'DISK TEMP ATTR: {clean_name} — {line.strip()[:150]}')
                        return float(temp), False
                    else:
                        for line in stdout.split('\n'):
                            if any(kw in line for kw in ['Temperature', 'Airflow', 'temperature', 'temp', 'Celsius', '190', '194']):
                                logger.info(f'DISK TEMP DEBUG: {clean_name} — {line.strip()[:150]}')

                except subprocess.TimeoutExpired:
                    continue

    except Exception as e:
        logger.debug(f'Error reading disk temp for {disk_identifier}: {e}')

    # Method 2: hdparm (some drives expose temp via ATA IDENTIFY)
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()
        result = subprocess.run(
            ['hdparm', '-I', f'/dev/{clean_name}'],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.split('\n'):
                low = line.lower()
                if 'temperature' in low:
                    match = re.search(r'(-?\d{1,3})\s*(?:°|deg|C)', line, re.IGNORECASE)
                    if not match:
                        match = re.search(r':\s*(-?\d{1,3})', line)
                    if match:
                        temp = int(match.group(1))
                        if 10 < temp < 80:
                            logger.info(f'DISK TEMP: {clean_name} = {temp}°C via hdparm')
                            return float(temp), False
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        pass

    # Method 3: sysfs temperature
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()
        sysfs_temp = _read_sysfs_temp(clean_name)
        if sysfs_temp is not None:
            logger.info(f'DISK TEMP: {clean_name} = {sysfs_temp}°C via sysfs')
            return sysfs_temp, False
    except Exception:
        pass

    return None, False


def _extract_smart_raw_value(line: str) -> Optional[int]:
    """Extract the RAW_VALUE from a smartctl SMART attribute line.

    Format: ID# ATTRIBUTE_NAME FLAGS VALUE WORST THRESH TYPE UPDATED FAILING_NOW RAW_VALUE
    Example: 190 Airflow_Temperature_Cel 0x0022 065 053 000 Old_age Always - 35 (Min/Max 26/45)

    The RAW_VALUE (35) is the actual temperature. The VALUE field (065) is a
    normalized 0-253 scale — NOT the temperature. Previous code used findall()
    which grabbed 065 first, reporting 65°C instead of 35°C.
    """
    # The raw value comes after the FAILING_NOW column (always `-` or a flag)
    match = re.search(r'\s-\s+(\d{1,3})\b', line)
    if match:
        return int(match.group(1))
    return None


def _parse_disk_temp_preferred(output: str) -> Tuple[Optional[int], str]:
    """Parse temperature from smartctl output.
    Priority:
    1. Airflow_Temperature_Cel — actual air temp near disk (best)
    2. HDA_Temperature — head/disk assembly temp
    3. Current Drive Temperature header
    4. Temperature_Celsius attribute — last resort (may be controller/IC temp)
    Returns (temperature, source_label)."""
    lines = output.split('\n')

    for line in lines:
        if 'Airflow_Temperature_Cel' in line:
            raw = _extract_smart_raw_value(line)
            if raw and 10 < raw < 80:
                return raw, 'airflow'

    for line in lines:
        if 'HDA_Temperature' in line:
            raw = _extract_smart_raw_value(line)
            if raw and 10 < raw < 80:
                return raw, 'hda'

    for line in lines:
        if 'Current Drive Temperature' in line:
            match = re.search(r':\s*(\d+)', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100:
                    return temp, 'smartctl_header'

    for line in lines:
        if 'Temperature_Celsius' in line:
            raw = _extract_smart_raw_value(line)
            if raw and 0 < raw < 100:
                return raw, 'celsius'

    # Pass 5: NVMe "Temperature: 37 Celsius" (non-SMART-attribute format)
    for line in lines:
        if 'Temperature:' in line and 'Celsius' in line:
            match = re.search(r'Temperature:\s+(\d+)', line)
            if match:
                temp = int(match.group(1))
                if 0 < temp < 100:
                    return temp, 'nvme'

    return None, ''


def _read_sysfs_temp(dev_name: str) -> Optional[float]:
    """Try to read temperature from sysfs."""
    import glob as _glob
    # Try common sysfs temperature paths
    patterns = [
        f'/sys/block/{dev_name}/device/scsi_disk/*/temperature',
        f'/sys/block/{dev_name}/device/scsi_disk/*/hwmon/hwmon*/temp1_input',
        f'/sys/block/{dev_name}/hwmon/hwmon*/temp1_input',
    ]
    for pattern in patterns:
        try:
            for path in _glob.glob(pattern):
                with open(path) as f:
                    val = int(f.read().strip())
                    if val > 0:
                        return val / 1000.0 if val > 200 else float(val)
        except Exception:
            continue
    return None


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
        if not fan:
            return

        # Check if fan has hwmon path (standard Linux PWM)
        pwm_path = fan.get('pwm_path', '')
        if pwm_path.startswith('/sys/class/hwmon/'):
            _set_pwm_hwmon(fan, raw_pwm, raw, key)
        elif fan.get('control_method') == 'dsm_scemd':
            _set_pwm_dsm(fan, raw_pwm, raw)
        else:
            return


def _set_pwm_hwmon(fan, raw_pwm, raw, key):
    """Set PWM via standard Linux hwmon sysfs."""
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


def _set_pwm_dsm(fan, raw_pwm, raw):
    """Set fan speed via DSM scemd.xml (0-255 maps to 0-100%)."""
    from core.dsm_fan import set_dsm_fan_speed

    val = max(0, min(255, int(raw_pwm)))
    if not raw:
        fan['raw_pwm'] = val

    percent = int(val * 100 / 255)
    set_dsm_fan_speed(percent)
    fan['pwm_value'] = val
    fan['last_update'] = time.monotonic()


def refresh():
    """Update temperature and RPM readings.
    
    Reads sysfs without holding state_lock, then batch-updates under a
    single lock acquisition instead of per-sensor locks.
    """
    with state_lock:
        temp_paths = [(k, v['path']) for k, v in state['temp_sensors'].items()]
        fan_paths = [(k, v['fan_path']) for k, v in state['fans'].items()]

    # Read all sysfs without holding lock
    temp_updates = {}
    for key, path in temp_paths:
        try:
            temp_updates[key] = int(Path(path).read_text().strip()) // 1000
        except Exception:
            pass

    def poll_fan(item):
        k, path = item
        try:
            return k, int(Path(path).read_text().strip())
        except Exception:
            return k, None

    futures = [executor.submit(poll_fan, item) for item in fan_paths]
    fan_updates = {}
    for future in futures:
        try:
            key, rpm = future.result(timeout=2)
            if rpm is not None:
                fan_updates[key] = rpm
        except Exception:
            pass

    # Single lock for all writes
    with state_lock:
        for k, v in temp_updates.items():
            if k in state['temp_sensors']:
                state['temp_sensors'][k]['value'] = v
        for k, rpm in fan_updates.items():
            if k in state['fans']:
                state['fans'][k]['rpm'] = rpm


def get_system_info():
    """Get system info: uptime, CPU, memory."""
    info = {}

    # Uptime
    try:
        with open('/proc/uptime') as f:
            uptime_sec = float(f.read().split()[0])
        days = int(uptime_sec // 86400)
        hours = int((uptime_sec % 86400) // 3600)
        mins = int((uptime_sec % 3600) // 60)
        info['uptime'] = f"{days}d {hours}h {mins}m"
        info['uptime_seconds'] = uptime_sec
    except Exception:
        info['uptime'] = '--'
        info['uptime_seconds'] = 0

    # CPU load
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        info['cpu_load'] = round(load1 / cpu_count * 100, 1)
    except Exception:
        info['cpu_load'] = 0

    # Memory
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ('MemTotal:', 'MemAvailable:'):
                    mem[parts[0]] = int(parts[1])
        total = mem.get('MemTotal:', 1)
        avail = mem.get('MemAvailable:', 0)
        info['mem_total_mb'] = round(total / 1024)
        info['mem_used_mb'] = round((total - avail) / 1024)
        info['mem_percent'] = round((total - avail) / total * 100, 1)
    except Exception:
        info['mem_percent'] = 0

    return info

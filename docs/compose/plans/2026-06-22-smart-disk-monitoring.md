# SMART Disk Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add SMART attribute monitoring to disk cards — click to view all SMART data, checkboxes to select which attributes display on the card, with tooltips, color gradation by criticality, and live refresh.

**Architecture:** New `GET /api/disks/<id>/smart` endpoint parses full `smartctl -A` output into structured JSON with metadata (description, criticality, tooltip). Frontend adds ⚙ button to disk cards, SMART detail modal on click, and checkbox-based attribute selection persisted per card.

**Tech Stack:** Python (smartctl parsing), Flask routes, JavaScript (modal UI, localStorage persistence)

---

### Task 1: Backend — SMART attribute parser and API endpoint

**Files:**
- Modify: `core/hardware.py` (add `parse_smart_attributes()`, `read_disk_smart()`)
- Modify: `server/routes.py` (add `GET /api/disks/<id>/smart`)
- Modify: `core/state.py` (bump CONFIG_VERSION to 3.5.16)

- [ ] **Step 1: Add SMART attribute metadata dict to `core/hardware.py`**

Add after line 212 (after `parse_smart_temp` function):

```python
SMART_ATTRIBUTE_META = {
    1: {"name": "Raw_Read_Error_Rate", "criticality": "important", "description": "Частота ошибок чтения", "tooltip": "Рост указывает на деградацию поверхности диска или проблемы с головками."},
    2: {"name": "Throughput_Performance", "criticality": "info", "description": "Производительность", "tooltip": "Общая производительность диска. Снижение может указывать на фрагментацию."},
    3: {"name": "Spin_Up_Time", "criticality": "info", "description": "Время раскрутки", "tooltip": "Время запуска шпинделя. Рост может указывать на износ механики."},
    4: {"name": "Start_Stop_Count", "criticality": "info", "description": "Количество запусков", "tooltip": "Сколько раз диск включался/выключался. Нормальный износ."},
    5: {"name": "Reallocated_Sector_Ct", "criticality": "critical", "description": "Переназначенные сектора", "tooltip": "Количество переназначенных секторов. Рост означает физическую деградацию поверхности диска. Рост > 0 требует замены диска."},
    7: {"name": "Seek_Error_Rate", "criticality": "important", "description": "Частота ошибок позиционирования", "tooltip": "Рост указывает на проблемы с блоком головок или фрагментацией."},
    8: {"name": "Seek_Time_Performance", "criticality": "info", "description": "Время позиционирования", "tooltip": "Среднее время поиска. Снижение = механический износ."},
    9: {"name": "Power_On_Hours", "criticality": "info", "description": "Часы работы", "tooltip": "Общее время работы диска в часах. Нормальный износ, ресурс 30000-50000 часов."},
    10: {"name": "Spin_Retry_Count", "criticality": "critical", "description": "Повторы раскрутки", "tooltip": "Количество повторных попыток раскрутки шпинделя. Рост = механическая проблема, замена обязательна."},
    11: {"name": "Calibration_Retry_Count", "criticality": "important", "description": "Повторы калибровки", "tooltip": "Неудачные попытки калибровки головок. Рост может привести к ошибкам чтения."},
    12: {"name": "Power_Cycle_Count", "criticality": "info", "description": "Циклы включения", "tooltip": "Количество включений/выключений питания."},
    13: {"name": "Read_Soft_Error_Rate", "criticality": "info", "description": "Программные ошибки чтения", "tooltip": "Ошибки, исправленные ECC. Временные ошибки, обычно не критичны."},
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
    187: {"name": "Unknown_Attribute", "criticality": "info", "description": "Неизвестный атрибут", "tooltip": "Проприетарный атрибут производителя."},
    188: {"name": "Unknown_Attribute", "criticality": "info", "description": "Неизвестный атрибут", "tooltip": "Проприетарный атрибут производителя."},
    190: {"name": "Airflow_Temperature_Cel", "criticality": "important", "description": "Температура воздушного потока", "tooltip": "Температура воздуха у диска. Оптимально: 25-45°C. Выше 50°C — перегрев."},
    191: {"name": "G_Sense_Error_Rate", "criticality": "important", "description": "Ошибки от удара", "tooltip": "Ошибки, вызванные ударами/вибрацией. Рост = физическое повреждение."},
    192: {"name": "Power-Off_Retract_Count", "criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество аварийных отключений питания. Рост = риск повреждения головок."},
    193: {"name": "Load_Cycle_Count", "criticality": "info", "description": "Циклы загрузки", "tooltip": "Количество перемещений головок. Нормальный износ."},
    194: {"name": "Temperature_Celsius", "criticality": "important", "description": "Температура", "tooltip": "Текущая температура диска. Оптимально: 25-45°C. Выше 50°C — перегрев."},
    195: {"name": "Hardware_ECC_Recovered", "criticality": "info", "description": "ECC восстановления", "tooltip": "Ошибки, исправленные аппаратным ECC. Временные, обычно не критичны."},
    196: {"name": "Reallocated_Event_Count", "criticality": "critical", "description": "События переназначения", "tooltip": "Количество событий переназначения секторов. Рост = деградация."},
    197: {"name": "Current_Pending_Sector", "criticality": "critical", "description": "Ожидающие сектора", "tooltip": "Сектора, ожидающие перераспределения. Рост может привести к потере данных."},
    198: {"name": "Offline_Uncorrectable", "criticality": "critical", "description": "Неисправимые сектора", "tooltip": "Сектора, которые невозможно прочитать/исправить. Рост = немедленная замена диска."},
    199: {"name": "UDMA_CRC_Error_Count", "criticality": "important", "description": "CRC ошибки интерфейса", "tooltip": "Ошибки_checksum интерфейса SATA. Проверьте кабель."},
    200: {"name": "Multi_Zone_Error_Rate", "criticality": "important", "description": "Ошибки по зонам", "tooltip": "Ошибки записи в несколько зон. Рост = деградация поверхности."},
    201: {"name": "Soft_Read_Error_Rate", "criticality": "info", "description": "Программные ошибки чтения", "tooltip": "Ошибки чтения, требующие повтора. Временные."},
}


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
        })

    return attributes


def parse_nvme_smart(output: str) -> dict:
    """Parse NVMe SMART attributes from smartctl output."""
    attributes = {}
    patterns = {
        'temperature': r'Temperature:\s+(\d+)\s+Celsius',
        'available_spare': r'Available Spare:\s+(\d+)%',
        'available_spare_threshold': r'Available Spare Threshold:\s+(\d+)%',
        'percentage_used': r'Percentage Used:\s+(\d+)%',
        'data_units_read': r'Data Units Read:\s+([\d,]+)',
        'data_units_written': r'Data Units Written:\s+([\d,]+)',
        'host_reads': r'Host Reads:\s+([\d,]+)',
        'host_writes': r'Host Writes:\s+([\d,]+)',
        'unsafe_shutdowns': r'Unsafe Shutdowns:\s+(\d+)',
        'media_errors': r'Media and Data Integrity Errors:\s+(\d+)',
        'error_log_entries': r'Error Information Log Entries:\s+(\d+)',
        'warning_temp_time': r'Warning Comp. Temp. Time:\s+(\d+)',
        'critical_comp_time': r'Critical Comp. Time:\s+(\d+)',
    }

    nvme_meta = {
        'temperature': {"criticality": "important", "description": "Температура", "tooltip": "Текущая температура NVMe диска. Оптимально: 25-45°C."},
        'available_spare': {"criticality": "critical", "description": "Доступный запас", "tooltip": "Процент резервных блоков. 0% = ресурс исчерпан, замена обязательна."},
        'percentage_used': {"criticality": "critical", "description": "Износ NAND", "tooltip": "Процент износа NAND-памяти. 100% = ресурс исчерпан."},
        'data_units_read': {"criticality": "info", "description": "Прочитано данных", "tooltip": "Общий объём чтения. Информационный параметр."},
        'data_units_written': {"criticality": "info", "description": "Записано данных", "tooltip": "Общий объём записи. Информационный параметр."},
        'unsafe_shutdowns': {"criticality": "important", "description": "Аварийные выключения", "tooltip": "Количество аварийных отключений. Рост = риск повреждения данных."},
        'media_errors': {"criticality": "critical", "description": "Ошибки носителя", "tooltip": "Ошибки целостности данных. Рост = проблема с NAND, замена обязательна."},
        'error_log_entries': {"criticality": "important", "description": "Записи журнала ошибок", "tooltip": "Количество записей в журнале ошибок. Рост = повторяющиеся проблемы."},
        'warning_temp_time': {"criticality": "info", "description": "Время при предупреждении о температуре", "tooltip": "Минуты работы выше предельной температуры."},
        'critical_comp_time': {"criticality": "critical", "description": "Время критической температуры", "tooltip": "Минуты работы при критической температуре. Рост = перегрев."},
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
            }

    return attributes


def read_disk_smart(disk_identifier: str) -> dict:
    """
    Read full SMART data for a disk.
    Returns dict with device info, attributes, and metadata.
    """
    try:
        clean_name = disk_identifier.replace('/dev/', '').strip()

        if not is_physical_disk(clean_name):
            return {'error': 'Not a physical disk'}

        is_nvme = clean_name.startswith('nvme')

        cmd = ['smartctl', '-A', '-i', f'/dev/{clean_name}']
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode != 0:
                cmd2 = ['smartctl', '-A', '-i', '-d', 'sat', f'/dev/{clean_name}']
                result = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return {'error': 'Timeout reading SMART data'}

        if result.returncode != 0 and not result.stdout:
            return {'error': f'smartctl failed with code {result.returncode}'}

        output = result.stdout

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
        }

    except Exception as e:
        logger.error(f'Error reading SMART for {disk_identifier}: {e}')
        return {'error': str(e)}
```

- [ ] **Step 2: Add SMART cache to `core/hardware.py`**

Add after the `SMART_ATTRIBUTE_META` dict:

```python
_smart_cache: Dict[str, Dict] = {}
_smart_cache_time: Dict[str, float] = {}
SMART_CACHE_TTL = 60  # seconds
```

- [ ] **Step 3: Add API endpoint to `server/routes.py`**

Add after the `/api/discover` route (after line 112):

```python
@routes.route('/api/disks/<disk_id>/smart')
def api_get_disk_smart(disk_id):
    """Get full SMART data for a specific disk"""
    import time as _time
    from core.hardware import read_disk_smart, _smart_cache, _smart_cache_time, SMART_CACHE_TTL

    now = _time.monotonic()
    if disk_id in _smart_cache and (now - _smart_cache_time.get(disk_id, 0)) < SMART_CACHE_TTL:
        return jsonify(_smart_cache[disk_id])

    with state_lock:
        disk = state.get('hdd_sensors', {}).get(disk_id)
        if not disk:
            return jsonify({'error': 'Disk not found'}), 404

        device = disk.get('device', '')
        if not device:
            return jsonify({'error': 'No device path'}), 404

    result = read_disk_smart(device)

    if 'error' not in result:
        _smart_cache[disk_id] = result
        _smart_cache_time[disk_id] = now

    return jsonify(result)
```

- [ ] **Step 4: Import the new functions in `server/routes.py`**

Update line 20 in `server/routes.py`:

```python
from core.hardware import discover_fans_and_sensors, discover_disks, set_pwm, refresh, read_disk_smart
```

- [ ] **Step 5: Bump version in `core/state.py`**

Change line 8 in `core/state.py`:

```python
CONFIG_VERSION = "3.5.16"
```

- [ ] **Step 6: Test the API endpoint**

Run the server and test:
```bash
curl http://localhost:5059/api/disks/dev-sda/smart
```

Expected: JSON with `device`, `device_info`, `attributes` array, `attr_type` field.

---

### Task 2: Frontend — SMART modal HTML and CSS

**Files:**
- Modify: `templates/index.html` (add SMART modal HTML)

- [ ] **Step 1: Add SMART detail modal to `index.html`**

Add after the Card Configure Modal (after line 860), before the Group Creator Modal:

```html
    <!-- SMART Detail Modal -->
    <div id="smart-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">
        <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-lg font-bold text-white" id="smart-modal-title">SMART Data</h3>
                <div class="flex items-center gap-2">
                    <button onclick="refreshSmartData()" class="text-gray-400 hover:text-neon-cyan text-sm transition-colors" title="Обновить">🔄</button>
                    <button onclick="hideSmartModal()" class="text-gray-400 hover:text-white text-lg">&times;</button>
                </div>
            </div>
            <div id="smart-device-info" class="text-xs text-gray-400 mb-3"></div>
            <div id="smart-attributes-container" class="flex-1 overflow-y-auto space-y-1"></div>
            <div class="flex gap-2 mt-4 pt-4 border-t border-gray-700">
                <button onclick="saveSmartSelection()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm">Сохранить выбор</button>
                <button onclick="hideSmartModal()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm">Закрыть</button>
            </div>
        </div>
    </div>
```

- [ ] **Step 2: Test modal renders**

Open browser, inspect DOM — verify `smart-modal` exists but is hidden.

---

### Task 3: Frontend — SMART API client and modal logic

**Files:**
- Modify: `templates/js/main.js` (add SMART functions)

- [ ] **Step 1: Add SMART state variables and API fetch function**

Add after the `hideCardConfig` function (after line 885):

```javascript
let _smartModalDiskId = null;
let _smartAttributes = [];
let _smartAttrType = 'sata';

async function fetchDiskSmart(diskId) {
    try {
        const resp = await fetch(`/api/disks/${diskId}/smart`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return await resp.json();
    } catch (e) {
        console.error('SMART fetch error:', e);
        return null;
    }
}
```

- [ ] **Step 2: Add SMART modal show/hide functions**

Add after `fetchDiskSmart`:

```javascript
function showSmartModal(diskId) {
    _smartModalDiskId = diskId;
    const disk = currentState?.hdd_sensors?.[diskId];
    const title = document.getElementById('smart-modal-title');
    if (title && disk) {
        title.textContent = `SMART — ${disk.label || disk.dev_name}`;
    }
    document.getElementById('smart-modal')?.classList.remove('hidden');
    refreshSmartData();
}

function hideSmartModal() {
    document.getElementById('smart-modal')?.classList.add('hidden');
    _smartModalDiskId = null;
}

async function refreshSmartData() {
    if (!_smartModalDiskId) return;
    const container = document.getElementById('smart-attributes-container');
    if (!container) return;

    container.innerHTML = '<div class="text-center text-gray-400 py-4">Загрузка...</div>';

    const data = await fetchDiskSmart(_smartModalDiskId);
    if (!data || data.error) {
        container.innerHTML = `<div class="text-center text-red-400 py-4">${data?.error || 'Ошибка загрузки SMART данных'}</div>`;
        return;
    }

    const infoEl = document.getElementById('smart-device-info');
    if (infoEl && data.device_info) {
        const info = data.device_info;
        infoEl.textContent = [info.model, info.serial, info.firmware, info.capacity].filter(Boolean).join(' | ');
    }

    _smartAttrType = data.attr_type || 'sata';
    _smartAttributes = data.attributes || [];

    renderSmartAttributes();
}
```

- [ ] **Step 3: Add SMART attribute rendering with checkboxes and tooltips**

Add after `refreshSmartData`:

```javascript
function renderSmartAttributes() {
    const container = document.getElementById('smart-attributes-container');
    if (!container) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === _smartModalDiskId);
    const selectedIds = card?.smartAttributes || [];

    if (_smartAttrType === 'nvme') {
        renderNvmeAttributes(container, selectedIds);
    } else {
        renderSataAttributes(container, selectedIds);
    }
}

function renderSataAttributes(container, selectedIds) {
    if (!_smartAttributes.length) {
        container.innerHTML = '<div class="text-center text-gray-400 py-4">Нет SMART атрибутов</div>';
        return;
    }

    container.innerHTML = _smartAttributes.map(attr => {
        const statusColor = attr.status === 'critical' ? 'text-red-400' :
                           attr.status === 'warning' ? 'text-yellow-400' : 'text-neon-green';
        const statusBg = attr.status === 'critical' ? 'bg-red-500/10' :
                        attr.status === 'warning' ? 'bg-yellow-500/10' : 'bg-green-500/10';
        const critBadge = attr.criticality === 'critical' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">КРИТИЧНЫЙ</span>' :
                         attr.criticality === 'important' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">ВАЖНЫЙ</span>' : '';
        const checked = selectedIds.includes(String(attr.id)) ? 'checked' : '';

        return `
        <div class="flex items-center gap-3 p-2 rounded ${statusBg} hover:bg-white/5 transition-colors group"
             title="${escapeHtml(attr.tooltip)}">
            <input type="checkbox" data-smart-id="${attr.id}" ${checked}
                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan shrink-0">
            <div class="flex-1 min-w-0">
                <div class="flex items-center">
                    <span class="text-xs text-gray-500 w-8">${attr.id}</span>
                    <span class="text-sm text-gray-200 truncate">${escapeHtml(attr.description)}</span>
                    ${critBadge}
                </div>
                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>
            </div>
            <div class="text-right shrink-0">
                <div class="text-sm font-mono ${statusColor}">${attr.value}</div>
                <div class="text-[10px] text-gray-500">worst:${attr.worst} thr:${attr.threshold}</div>
            </div>
            <div class="text-right shrink-0 w-16">
                <div class="text-xs text-gray-400 font-mono">${attr.raw}</div>
            </div>
        </div>`;
    }).join('');
}

function renderNvmeAttributes(container, selectedIds) {
    const attrs = _smartAttributes;
    if (!Object.keys(attrs).length) {
        container.innerHTML = '<div class="text-center text-gray-400 py-4">Нет NVMe атрибутов</div>';
        return;
    }

    container.innerHTML = Object.entries(attrs).map(([key, attr]) => {
        const statusColor = attr.criticality === 'critical' ? 'text-red-400' :
                           attr.criticality === 'important' ? 'text-yellow-400' : 'text-neon-green';
        const critBadge = attr.criticality === 'critical' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">КРИТИЧНЫЙ</span>' :
                         attr.criticality === 'important' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">ВАЖНЫЙ</span>' : '';
        const checked = selectedIds.includes(key) ? 'checked' : '';

        return `
        <div class="flex items-center gap-3 p-2 rounded bg-green-500/5 hover:bg-white/5 transition-colors"
             title="${escapeHtml(attr.tooltip)}">
            <input type="checkbox" data-smart-key="${key}" ${checked}
                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan shrink-0">
            <div class="flex-1 min-w-0">
                <div class="flex items-center">
                    <span class="text-sm text-gray-200 truncate">${escapeHtml(attr.description)}</span>
                    ${critBadge}
                </div>
                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>
            </div>
            <div class="text-right shrink-0">
                <div class="text-sm font-mono ${statusColor}">${attr.value}${key === 'temperature' ? '°C' : key.includes('percentage') || key.includes('spare') ? '%' : ''}</div>
            </div>
        </div>`;
    }).join('');
}
```

- [ ] **Step 4: Add save selection function**

Add after `renderNvmeAttributes`:

```javascript
function saveSmartSelection() {
    if (!_smartModalDiskId) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === _smartModalDiskId);
    if (!card) return;

    const checkboxes = document.querySelectorAll('#smart-attributes-container input[type="checkbox"]');
    const selected = [];
    checkboxes.forEach(cb => {
        if (cb.checked) {
            selected.push(cb.dataset.smartId || cb.dataset.smartKey);
        }
    });

    card.smartAttributes = selected;
    setPickerCards(saved);
    updateCardDetails(_smartModalDiskId);
    hideSmartModal();
}
```

- [ ] **Step 5: Test SMART modal functions**

In browser console, call `showSmartModal('dev-sda')` — verify modal opens and loads SMART data.

---

### Task 4: Frontend — Wire disk card click and ⚙ button

**Files:**
- Modify: `templates/js/main.js` (modify card rendering and click handler)

- [ ] **Step 1: Add ⚙ button to disk cards**

In `templates/js/main.js`, change line 685-687 from:

```javascript
    const configBtn = type === 'fan'
        ? `<button onclick="event.stopPropagation(); showCardConfig('${id}')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Configure">⚙</button>`
        : '';
```

To:

```javascript
    const configBtn = type === 'fan'
        ? `<button onclick="event.stopPropagation(); showCardConfig('${id}')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Configure">⚙</button>`
        : type === 'disk'
        ? `<button onclick="event.stopPropagation(); showSmartModal('${id}')" class="text-gray-600 hover:text-neon-purple text-xs transition-colors" title="SMART">⚙</button>`
        : '';
```

- [ ] **Step 2: Add click handler for disk cards to show SMART modal**

Find the card click handler in `main.js`. Search for the click event listener on card elements. The cards are created in `addPickerCard()` around line 691-715. Add a click handler for disk cards.

After line 715 (`canvas.appendChild(el);`), before `updateCardDetails(id);`, add:

```javascript
    if (type === 'disk') {
        el.addEventListener('click', (e) => {
            if (_cardDragOccurred || e.target.closest('button')) return;
            showSmartModal(id);
        });
    }
```

- [ ] **Step 3: Test disk card click**

Click a disk card in the dashboard — verify SMART modal opens with attribute data.

---

### Task 5: Frontend — Update `updateCardDetails` for disk cards

**Files:**
- Modify: `templates/js/main.js` (modify `updateCardDetails` function)

- [ ] **Step 1: Add disk card details rendering**

In `updateCardDetails` function (around line 918-931), change:

```javascript
    if (card.type !== 'fan') {
        detailsEl.innerHTML = '';
        return;
    }
```

To:

```javascript
    if (card.type === 'disk') {
        updateDiskCardDetails(card, detailsEl);
        return;
    }
    if (card.type !== 'fan') {
        detailsEl.innerHTML = '';
        return;
    }
```

- [ ] **Step 2: Add `updateDiskCardDetails` function**

Add after `updateCardDetails` function:

```javascript
function updateDiskCardDetails(card, detailsEl) {
    if (!card.smartAttributes?.length) {
        detailsEl.innerHTML = '';
        return;
    }

    const diskData = currentState?.hdd_sensors?.[card.sourceId];
    if (!diskData) {
        detailsEl.innerHTML = '';
        return;
    }

    let html = '';

    for (const attrKey of card.smartAttributes) {
        const attrId = parseInt(attrKey);
        if (!isNaN(attrId)) {
            const cachedSmart = _smartCache?.[card.sourceId];
            if (cachedSmart?.attributes) {
                const attr = cachedSmart.attributes.find(a => a.id === attrId);
                if (attr) {
                    const color = attr.status === 'critical' ? 'text-red-400' :
                                 attr.status === 'warning' ? 'text-yellow-400' : 'text-neon-green';
                    html += `<div class="text-xs mt-1" title="${escapeHtml(attr.tooltip)}">
                        <span class="text-gray-500">${escapeHtml(attr.description)}:</span>
                        <span class="${color} font-mono">${attr.raw}</span>
                    </div>`;
                }
            }
        } else {
            const cachedSmart = _smartCache?.[card.sourceId];
            if (cachedSmart?.attributes?.[attrKey]) {
                const attr = cachedSmart.attributes[attrKey];
                const color = attr.criticality === 'critical' ? 'text-red-400' :
                             attr.criticality === 'important' ? 'text-yellow-400' : 'text-neon-green';
                const unit = attrKey === 'temperature' ? '°C' :
                            attrKey.includes('percentage') || attrKey.includes('spare') ? '%' : '';
                html += `<div class="text-xs mt-1" title="${escapeHtml(attr.tooltip)}">
                    <span class="text-gray-500">${escapeHtml(attr.description)}:</span>
                    <span class="${color} font-mono">${attr.value}${unit}</span>
                </div>`;
            }
        }
    }

    detailsEl.innerHTML = html;
}
```

- [ ] **Step 3: Add SMART cache reference**

Add near the top of the file (after the `_smartModalDiskId` variables):

```javascript
let _smartCache = {};
```

Update `refreshSmartData` to cache the result:

In the `refreshSmartData` function, after `const data = await fetchDiskSmart(_smartModalDiskId);`, add:

```javascript
    _smartCache[_smartModalDiskId] = data;
```

- [ ] **Step 4: Add periodic refresh for disk card SMART details**

In the `updateUI` function's disk update section (around line 1086-1091), after updating temperature, add SMART detail refresh for cards that have selected attributes:

```javascript
        document.querySelectorAll('[data-disk-id]').forEach(el => {
            const id = el.dataset.diskId;
            if (currentState?.hdd_sensors?.[id]) {
                el.textContent = currentState.hdd_sensors[id].temp || '--';
            }
        });
        getPickerCards().filter(c => c.type === 'disk' && c.smartAttributes?.length).forEach(c => {
            if (_smartCache[c.sourceId]) {
                const cardEl = document.querySelector(`[data-card-id="${c.id}"]`);
                if (cardEl) {
                    const detailsEl = cardEl.querySelector('.card-details');
                    if (detailsEl) updateDiskCardDetails(c, detailsEl);
                }
            }
        });
```

- [ ] **Step 5: Test disk card details**

Select some SMART attributes in the modal, save, verify they appear on the card.

---

### Task 6: Backend — Cache invalidation and smartctl refresh on modal open

**Files:**
- Modify: `server/routes.py` (add force refresh parameter)
- Modify: `templates/js/main.js` (pass refresh param)

- [ ] **Step 1: Add refresh parameter to API**

In `server/routes.py`, update the `api_get_disk_smart` function to accept a `refresh` query parameter:

```python
@routes.route('/api/disks/<disk_id>/smart')
def api_get_disk_smart(disk_id):
    """Get full SMART data for a specific disk"""
    import time as _time
    from core.hardware import read_disk_smart, _smart_cache, _smart_cache_time, SMART_CACHE_TTL

    force_refresh = request.args.get('refresh', '0') == '1'
    now = _time.monotonic()

    if not force_refresh and disk_id in _smart_cache and (now - _smart_cache_time.get(disk_id, 0)) < SMART_CACHE_TTL:
        return jsonify(_smart_cache[disk_id])

    with state_lock:
        disk = state.get('hdd_sensors', {}).get(disk_id)
        if not disk:
            return jsonify({'error': 'Disk not found'}), 404

        device = disk.get('device', '')
        if not device:
            return jsonify({'error': 'No device path'}), 404

    result = read_disk_smart(device)

    if 'error' not in result:
        _smart_cache[disk_id] = result
        _smart_cache_time[disk_id] = now

    return jsonify(result)
```

- [ ] **Step 2: Update frontend to pass refresh param**

In `templates/js/main.js`, update `fetchDiskSmart`:

```javascript
async function fetchDiskSmart(diskId, forceRefresh = false) {
    try {
        const url = forceRefresh
            ? `/api/disks/${diskId}/smart?refresh=1`
            : `/api/disks/${diskId}/smart`;
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return await resp.json();
    } catch (e) {
        console.error('SMART fetch error:', e);
        return null;
    }
}
```

Update `refreshSmartData` to force refresh:

```javascript
async function refreshSmartData() {
    if (!_smartModalDiskId) return;
    const container = document.getElementById('smart-attributes-container');
    if (!container) return;

    container.innerHTML = '<div class="text-center text-gray-400 py-4">Загрузка...</div>';

    const data = await fetchDiskSmart(_smartModalDiskId, true);
    if (!data || data.error) {
        container.innerHTML = `<div class="text-center text-red-400 py-4">${data?.error || 'Ошибка загрузки SMART данных'}</div>`;
        return;
    }

    _smartCache[_smartModalDiskId] = data;

    const infoEl = document.getElementById('smart-device-info');
    if (infoEl && data.device_info) {
        const info = data.device_info;
        infoEl.textContent = [info.model, info.serial, info.firmware, info.capacity].filter(Boolean).join(' | ');
    }

    _smartAttrType = data.attr_type || 'sata';
    _smartAttributes = data.attributes || [];

    renderSmartAttributes();
}
```

- [ ] **Step 3: Test refresh behavior**

Open SMART modal — should force-refresh data. Click 🔄 — should refresh again.

---

### Task 7: Final integration test and version bump

**Files:**
- Modify: `core/state.py` (verify version)

- [ ] **Step 1: Verify all changes work together**

1. Open dashboard — disk cards should show ⚙ button
2. Click ⚙ on disk card — SMART modal opens with all attributes
3. Check some attributes, click "Сохранить выбор"
4. Verify selected attributes appear on the card
5. Click 🔄 in modal — data refreshes
6. Hover over attribute — tooltip shows description and criticality
7. Color gradation: green/yellow/red based on status
8. Close and reopen page — selected attributes persist

- [ ] **Step 2: Verify version is 3.5.16**

Check `core/state.py` has `CONFIG_VERSION = "3.5.16"`.

- [ ] **Step 3: Commit all changes**

```bash
git add core/hardware.py server/routes.py templates/js/main.js templates/index.html core/state.py
git commit -m "feat: SMART disk monitoring — view all attributes, select which to display on cards (v3.5.16)"
```

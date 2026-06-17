/**
 * FanControl Web v3.2.0 - Neon Cyberpunk Edition
 * Main JavaScript Application
 */

// ============================================================================
// GLOBAL STATE
// ============================================================================

let chart = null;
let currentFanId = null;
let allSensors = [];
let fanConfigs = {};
let isDragging = false;
let wizardStep = 'intro';
let currentState = null;
let lastChartUpdate = 0;
const CHART_UPDATE_INTERVAL = 60000;

// Schedule state
let scheduleData = {};
let scheduleSelection = [];
let isDraggingSchedule = false;
let dragStartCell = null;
let editingCells = [];
let scheduleEditorSensors = [];
let expandedRuleGroups = new Set();

// ============================================================================
// I18N SYSTEM
// ============================================================================

let currentLang = localStorage.getItem('fancontrol_lang') || 'en';
let translations = {};

async function loadLang(code) {
    try {
        const resp = await fetch(`/api/lang/${code}`);
        if (resp.ok) {
            translations = await resp.json();
            currentLang = code;
            localStorage.setItem('fancontrol_lang', code);
            applyTranslations();
            return true;
        }
    } catch (e) {
        console.error('[i18n] Failed to load lang:', code, e);
    }
    return false;
}

function t(key, fallback) {
    return translations[key] || fallback || key;
}

function applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (key && translations[key]) {
            el.textContent = translations[key];
        }
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        const key = el.getAttribute('data-i18n-title');
        if (key && translations[key]) {
            el.title = translations[key];
        }
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        if (key && translations[key]) {
            el.placeholder = translations[key];
        }
    });
    // Update page title
    if (translations['app.title']) {
        document.title = `${translations['app.title']} v3.2.0`;
    }
}

// ============================================================================
// UTILITIES
// ============================================================================

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ============================================================================
// SOCKET.IO CONNECTION
// ============================================================================

console.log('[FanControl] Establishing Socket.IO connection...');
const socket = io();

socket.on('connect', () => {
    console.log('[FanControl] Socket connected');
});

socket.on('update', (data) => {
    currentState = data;
    updateUI(data);
});

socket.on('hardware_discovered', (data) => {
    console.log('[FanControl] Hardware discovered:', data);
    if (wizardStep === 'intro' || wizardStep === 'scanning') {
        renderDiscoveredHardware(data);
        wizardStep = 'results';
    }
});

socket.on('test_progress', (progress) => {
    console.log('[FanControl] Calibration progress:', progress);
    updateCalibrationModal(progress);
});

socket.on('test_complete', (result) => {
    console.log('[FanControl] Calibration complete:', result);
    hideCalibrationModal();
    
    if (result.success && result.initialized) {
        wizardStep = 'done';
        showMainScreen();
    }
});

// ============================================================================
// UI UPDATE FUNCTIONS
// ============================================================================

function updateUI(data) {
    if (!data) return;
    
    // Show appropriate screen
    if (!data.initialized) {
        showSetupScreen();
        if (data.hardware_scanned && wizardStep === 'intro') {
            renderDiscoveredHardware({
                fans: data.fans,
                temps: data.temp_sensors,
                disks: data.hdd_sensors
            });
            wizardStep = 'results';
            document.getElementById('discover-btn').disabled = false;
            document.getElementById('discover-loader').classList.add('hidden');
        }
        return;
    }
    
    showMainScreen();
    
    // Update indicators
    updateFailsafeIndicator(data.failsafe);
    updateStandbyIndicator(data.standby_mode);
    
    // Build fan list if needed
    if (data.fans && Object.keys(data.fans).length > 0) {
        buildFanList(data.fans);
    }
    
    // Build disks list
    if (data.hdd_sensors) {
        buildDisksList(data.hdd_sensors);
    }
    
    // Build sensor list for popup
    buildSensorList(data);
    
    // Update inspector if a fan is selected
    if (currentFanId && data.fans && data.fans[currentFanId]) {
        updateInspector(data.fans[currentFanId]);
    }
    
    // Update chart
    updateChart();
}

function showSetupScreen() {
    document.getElementById('setup-screen').classList.remove('hidden');
    document.getElementById('main-screen').classList.add('hidden');
}

function showMainScreen() {
    document.getElementById('setup-screen').classList.add('hidden');
    document.getElementById('main-screen').classList.remove('hidden');
}

function updateFailsafeIndicator(failsafe) {
    const el = document.getElementById('failsafe-indicator');
    if (failsafe) {
        el.classList.remove('hidden');
    } else {
        el.classList.add('hidden');
    }
}

function updateStandbyIndicator(standby) {
    const el = document.getElementById('standby-indicator');
    if (standby) {
        el.classList.remove('hidden');
    } else {
        el.classList.add('hidden');
    }
}

// ============================================================================
// FAN LIST (Left Panel)
// ============================================================================

function buildFanList(fans) {
    const container = document.getElementById('fan-list');
    if (!container) return;
    
    let html = '';
    
    for (const [fanId, fan] of Object.entries(fans)) {
        const isSelected = fanId === currentFanId;
        const borderColor = isSelected ? 'border-neon-purple' : 'border-cyber-accent';
        const bgColor = isSelected ? 'bg-cyber-accent' : 'bg-cyber-card';
        
        html += `
            <div id="fan-card-${escapeHtml(fanId)}" 
                 class="fan-card ${bgColor} border ${borderColor} rounded-lg p-3 cursor-pointer 
                        hover:border-neon-purple transition-all duration-200"
                 onclick="selectFan('${escapeHtml(fanId)}')">
                <div class="flex items-center justify-between mb-1">
                    <span class="text-sm font-semibold text-white truncate">${escapeHtml(fan.label)}</span>
                    <div class="flex items-center gap-1">
                        ${fan.inverted ? `<span class="text-xs px-1.5 py-0.5 rounded bg-cyan-900 bg-opacity-30 text-neon-cyan">${t('fan.inv', 'INV')}</span>` : ''}
                        <span class="text-xs px-1.5 py-0.5 rounded ${getStatusBadgeClass(fan.status)}">${t('status.' + fan.status, fan.status)}</span>
                    </div>
                </div>
                <div class="flex items-center justify-between text-xs">
                    <span class="text-gray-500">${t('mode.' + (fan.mode || 'manual'), fan.mode || 'manual')}</span>
                    <span class="font-mono text-neon-cyan" id="fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0} ${t('fan.rpm', 'RPM')}</span>
                </div>
            </div>
        `;
    }
    
    container.innerHTML = html || `<div class="text-center text-gray-500 py-8">${t('setup.no_fans', 'No fans detected')}</div>`;
}

function updateFanListStatus(fans) {
    for (const [fanId, fan] of Object.entries(fans)) {
        const rpmEl = document.getElementById(`fan-rpm-${fanId}`);
        if (rpmEl) {
            rpmEl.textContent = `${fan.rpm || 0} RPM`;
        }
        
        const card = document.getElementById(`fan-card-${fanId}`);
        if (card && fanId === currentFanId) {
            card.classList.add('border-neon-purple', 'bg-cyber-accent');
        }
    }
}

function selectFan(fanId) {
    currentFanId = fanId;
    
    // Update card highlights
    document.querySelectorAll('.fan-card').forEach(card => {
        card.classList.remove('border-neon-purple', 'bg-cyber-accent');
        card.classList.add('border-cyber-accent', 'bg-cyber-card');
    });
    
    const selectedCard = document.getElementById(`fan-card-${fanId}`);
    if (selectedCard) {
        selectedCard.classList.add('border-neon-purple', 'bg-cyber-accent');
        selectedCard.classList.remove('border-cyber-accent', 'bg-cyber-card');
    }
    
    // Show inspector
    if (currentState && currentState.fans && currentState.fans[fanId]) {
        updateInspector(currentState.fans[fanId]);
    }
}

function getStatusBadgeClass(status) {
    const classes = {
        'nominal': 'bg-green-900 bg-opacity-30 text-neon-green',
        'warning': 'bg-orange-900 bg-opacity-30 text-neon-orange',
        'critical': 'bg-red-900 bg-opacity-30 text-neon-red',
        'failsafe': 'bg-red-900 bg-opacity-50 text-neon-red',
        'standby': 'bg-blue-900 bg-opacity-30 text-blue-400',
        'inverted': 'bg-cyan-900 bg-opacity-30 text-neon-cyan',
        'no_sensor': 'bg-yellow-900 bg-opacity-30 text-neon-orange',
        'not_tested': 'bg-gray-700 text-gray-400',
        'calibrating': 'bg-purple-900 bg-opacity-30 text-neon-purple',
    };
    return classes[status] || 'bg-gray-700 text-gray-400';
}

// ============================================================================
// INSPECTOR (Right Panel)
// ============================================================================

function updateInspector(fan) {
    // Show inspector, hide empty state
    document.getElementById('inspector-empty').classList.add('hidden');
    document.getElementById('inspector-fan').classList.remove('hidden');
    
    // Update title
    document.getElementById('inspector-title').textContent = fan.label;
    document.getElementById('inspector-subtitle').textContent = `ID: ${fan.id || 'unknown'}`;
    
    // Update fan name
    document.getElementById('fan-name').textContent = fan.label;
    
    // Update status badge
    const statusBadge = document.getElementById('fan-status-badge');
    statusBadge.textContent = t('status.' + fan.status, fan.status || 'unknown');
    statusBadge.className = `text-xs px-2 py-0.5 rounded-full ${getStatusBadgeClass(fan.status)}`;
    
    // Update inverted badge
    const invertedBadge = document.getElementById('fan-inverted-badge');
    if (invertedBadge) {
        invertedBadge.classList.toggle('hidden', !fan.inverted);
    }
    
    // Update mode badge
    const modeBadge = document.getElementById('fan-mode-badge');
    const mode = fan.mode || 'manual';
    modeBadge.textContent = t('mode.' + mode, mode).toUpperCase();
    modeBadge.className = mode === 'auto' 
        ? 'text-xs px-2 py-0.5 rounded-full bg-cyan-900 bg-opacity-30 text-neon-cyan'
        : 'text-xs px-2 py-0.5 rounded-full bg-purple-900 bg-opacity-30 text-neon-purple';
    
    // Update RPM
    document.getElementById('fan-rpm-display').textContent = fan.rpm || 0;
    
    // Update RPM color
    const rpmDisplay = document.getElementById('fan-rpm-display');
    rpmDisplay.classList.remove('text-neon-cyan', 'text-neon-orange', 'text-neon-red');
    if (fan.rpm > (fan.max_rpm * 0.8 || 1500)) {
        rpmDisplay.classList.add('text-neon-orange');
    } else if (fan.status === 'failsafe' || fan.status === 'critical') {
        rpmDisplay.classList.add('text-neon-red');
    } else {
        rpmDisplay.classList.add('text-neon-cyan');
    }
    
    // Update slider (only if not dragging)
    if (!isDragging) {
        const slider = document.getElementById('pwm-slider');
        slider.value = fan.current_pct || fan.manual_pct || 50;
        slider.disabled = (mode === 'auto');
        document.getElementById('pwm-value-display').textContent = `${fan.current_pct || fan.manual_pct || 50}%`;
    }
    
    // Update mode buttons
    const btnManual = document.getElementById('btn-mode-manual');
    const btnAuto = document.getElementById('btn-mode-auto');
    
    if (mode === 'manual') {
        btnManual.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-purple';
        btnAuto.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan';
    } else {
        btnManual.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple hover:border-neon-purple';
        btnAuto.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-cyan bg-opacity-20 text-neon-cyan border border-neon-cyan border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-cyan';
    }
    
    // Show/hide auto settings
    document.getElementById('auto-settings').style.display = (mode === 'auto') ? 'block' : 'none';
    
    // Render schedule grid when in auto mode
    if (mode === 'auto') {
        setTimeout(() => renderScheduleGrid(), 50);
    }
    
    // Store config
    if (!fanConfigs[currentFanId]) fanConfigs[currentFanId] = {};
    fanConfigs[currentFanId].sensors = fan.sensors || [];
    fanConfigs[currentFanId].target_temp = fan.target_temp || 31;
    fanConfigs[currentFanId].mode = mode;
    fanConfigs[currentFanId].sensor_mode = fan.sensor_mode || 'max';
}

// ============================================================================
// FAN CONTROL ACTIONS
// ============================================================================

function setFanMode(mode) {
    if (!currentFanId) return;
    
    // Update local state immediately for instant UI feedback
    if (currentState?.fans?.[currentFanId]) {
        currentState.fans[currentFanId].mode = mode;
    }
    if (fanConfigs[currentFanId]) {
        fanConfigs[currentFanId].mode = mode;
    }
    
    // Update button styles immediately
    const btnManual = document.getElementById('btn-mode-manual');
    const btnAuto = document.getElementById('btn-mode-auto');
    if (btnManual && btnAuto) {
        if (mode === 'manual') {
            btnManual.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-purple';
            btnAuto.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan';
        } else {
            btnManual.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple hover:border-neon-purple';
            btnAuto.className = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-cyan bg-opacity-20 text-neon-cyan border border-neon-cyan border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-cyan';
        }
    }
    
    document.getElementById('auto-settings').style.display = (mode === 'auto') ? 'block' : 'none';
    if (mode === 'auto') {
        setTimeout(() => renderScheduleGrid(), 50);
    }
    
    sendControl({
        action: 'set_fan_config',
        fan: currentFanId,
        fan_mode: mode
    });
}

function sendControl(payload) {
    fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .catch(err => console.error('Control error:', err));
}

// ============================================================================
// PWM SLIDER
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    const slider = document.getElementById('pwm-slider');
    if (!slider) return;
    
    slider.addEventListener('input', (e) => {
        document.getElementById('pwm-value-display').textContent = `${e.target.value}%`;
    });
    
    slider.addEventListener('mousedown', () => {
        isDragging = true;
    });
    
    slider.addEventListener('mouseup', (e) => {
        isDragging = false;
        applyPWM(e.target.value);
    });
    
    slider.addEventListener('touchend', (e) => {
        isDragging = false;
        applyPWM(e.target.value);
    });
});

function applyPWM(value) {
    if (!currentFanId) return;
    
    sendControl({
        action: 'set_fan_pwm',
        fan: currentFanId,
        pwm: parseInt(value)
    });
}

// ============================================================================
// SENSOR POPUP
// ============================================================================

function buildSensorList(data) {
    allSensors = [];
    
    if (data.hdd_sensors) {
        for (const [id, disk] of Object.entries(data.hdd_sensors)) {
            allSensors.push({
                id: `hdd:${id}`,
                label: disk.label,
                temp: disk.temp,
                standby: disk.standby,
                group: 'sensors.disks'
            });
        }
    }
    
    if (data.temp_sensors) {
        for (const [id, sensor] of Object.entries(data.temp_sensors)) {
            allSensors.push({
                id: `temp:${id}`,
                label: sensor.label,
                temp: sensor.value,
                standby: false,
                group: 'sensors.sensors_group'
            });
        }
    }
}

function toggleSensorPopup() {
    const popup = document.getElementById('sensor-popup');
    const list = document.getElementById('sensor-popup-list');
    
    if (!popup || !list) return;
    
    if (popup.classList.contains('hidden')) {
        // Build list
        const currentSensors = fanConfigs[currentFanId]?.sensors || [];
        
        // Group sensors
        const groups = {};
        allSensors.forEach(s => {
            if (!groups[s.group]) groups[s.group] = [];
            groups[s.group].push(s);
        });
        
        let html = '';
        for (const [group, sensors] of Object.entries(groups)) {
            html += `<div class="text-xs font-semibold text-gray-500 uppercase mb-2">${t(group, group)}</div>`;
            sensors.forEach(s => {
                const checked = currentSensors.includes(s.id);
                html += `
                    <label class="flex items-center gap-2 py-1.5 cursor-pointer hover:bg-cyber-accent rounded px-2">
                        <input type="checkbox" value="${escapeHtml(s.id)}" ${checked ? 'checked' : ''} 
                               class="accent-neon-purple">
                        <span class="text-sm text-gray-300">${escapeHtml(s.label)}</span>
                        <span class="text-xs text-gray-500 ml-auto">
                            ${s.standby ? t('sensor.sleep', 'Sleep') : s.temp + '°C'}
                        </span>
                    </label>
                `;
            });
        }
        
        list.innerHTML = html;
        popup.classList.remove('hidden');
    } else {
        closeSensorPopup();
    }
}

function closeSensorPopupForContext() {
    const popup = document.getElementById('sensor-popup');
    if (!popup) return;
    
    if (popup._scheduleMode) {
        toggleScheduleSensorPopup();
    } else {
        closeSensorPopup();
    }
}

function closeSensorPopup() {
    const popup = document.getElementById('sensor-popup');
    if (!popup) return;
    
    // Collect checked sensors
    const checked = popup.querySelectorAll('input[type=checkbox]:checked');
    const sensors = Array.from(checked).map(cb => cb.value);
    
    if (currentFanId) {
        if (!fanConfigs[currentFanId]) fanConfigs[currentFanId] = {};
        fanConfigs[currentFanId].sensors = sensors;
        
        sendControl({
            action: 'set_fan_config',
            fan: currentFanId,
            sensors: sensors
        });
        
        // Update no-sensor warning and sensor mode section
        const mode = fanConfigs[currentFanId]?.mode || 'manual';
        const noSensorWarning = document.getElementById('no-sensor-warning');
        const sensorModeSection = document.getElementById('sensor-mode-section');
        if (noSensorWarning) {
            noSensorWarning.classList.toggle('hidden', sensors.length > 0 || mode !== 'auto');
        }
        if (sensorModeSection) {
            sensorModeSection.classList.toggle('hidden', sensors.length <= 1);
        }
    }
    
    popup.classList.add('hidden');
}

// ============================================================================
// CHART (ApexCharts)
// ============================================================================

function updateChart() {
    const now = Date.now();
    if (now - lastChartUpdate < CHART_UPDATE_INTERVAL) return;
    
    const chartContainer = document.getElementById('temp-chart');
    if (!chartContainer || chartContainer.offsetParent === null) return;
    
    lastChartUpdate = now;
    
    fetch('/api/history?hours=24')
        .then(r => r.json())
        .then(data => {
            if (!data.has_data) return;
            
            const series = [
                {
                    name: t('chart.max_hdd_temp', 'Max HDD Temp'),
                    data: data.timestamps.map((ts, i) => ({
                        x: new Date(ts).getTime(),
                        y: data.temps[i]
                    }))
                },
                {
                    name: t('chart.avg_pwm', 'Avg PWM'),
                    data: data.timestamps.map((ts, i) => ({
                        x: new Date(ts).getTime(),
                        y: data.pwm[i]
                    }))
                }
            ];
            
            if (!chart) {
                chart = new ApexCharts(chartContainer, {
                    chart: {
                        type: 'line',
                        height: 250,
                        background: 'transparent',
                        foreColor: '#9ca3af',
                        toolbar: { show: false },
                        zoom: { enabled: false },
                        animations: {
                            enabled: true,
                            easing: 'easeinout',
                            speed: 800
                        }
                    },
                    theme: { mode: 'dark' },
                    stroke: {
                        curve: 'smooth',
                        width: [2, 1.5],
                        dashArray: [0, 5]
                    },
                    colors: ['#ff2d55', '#00f0ff'],
                    fill: {
                        type: 'gradient',
                        gradient: {
                            shade: 'dark',
                            type: 'vertical',
                            opacityFrom: 0.3,
                            opacityTo: 0
                        }
                    },
                    markers: {
                        size: 0,
                        hover: { size: 4 }
                    },
                    grid: {
                        borderColor: '#1a1f2e',
                        strokeDashArray: 4
                    },
                    xaxis: {
                        type: 'datetime',
                        labels: {
                            style: { colors: '#6b7280' }
                        }
                    },
                    yaxis: [
                        {
                            title: { text: '°C', style: { color: '#ff2d55' } },
                            labels: { style: { colors: '#6b7280' } }
                        },
                        {
                            opposite: true,
                            title: { text: '%', style: { color: '#00f0ff' } },
                            labels: { style: { colors: '#6b7280' } },
                            min: 0,
                            max: 100
                        }
                    ],
                    legend: {
                        position: 'top',
                        labels: { colors: '#9ca3af' }
                    },
                    tooltip: {
                        theme: 'dark',
                        x: { format: 'HH:mm' }
                    }
                });
                
                chart.render();
            } else {
                chart.updateSeries(series);
            }
        })
        .catch(err => console.error('Chart error:', err));
}

// Update chart every 60 seconds
setInterval(updateChart, 60000);

// ============================================================================
// DISKS LIST (Left Panel Bottom)
// ============================================================================

function buildDisksList(disks) {
    const container = document.getElementById('disks-mini-list');
    if (!container) return;
    
    let html = '';
    
    for (const [id, disk] of Object.entries(disks)) {
        const pct = disk.pct_fill || 0;
        const colorMap = {
            'cyan': 'bg-neon-cyan',
            'orange': 'bg-neon-orange',
            'red': 'bg-neon-red',
            'critical': 'bg-neon-red animate-pulse',
            'unknown': 'bg-gray-600'
        };
        const barColor = colorMap[disk.color_zone] || 'bg-gray-600';
        
        html += `
            <div class="flex items-center gap-2">
                <span class="text-xs text-gray-400 w-14 truncate">${escapeHtml(disk.label)}</span>
                <div class="flex-1 h-1.5 bg-cyber-accent rounded-full overflow-hidden">
                    <div class="h-full ${barColor} rounded-full progress-fill" style="width: ${pct}%"></div>
                </div>
                <span class="text-xs font-mono w-10 text-right ${getTempColorClass(disk.temp)}">
                    ${disk.standby ? t('sensor.sleep', 'Sleep') : disk.temp > 0 ? disk.temp + '°' : '--'}
                </span>
            </div>
        `;
    }
    
    container.innerHTML = html || `<div class="text-xs text-gray-500">${t('setup.no_disks', 'No disks detected')}</div>`;
}

function getTempColorClass(temp) {
    if (temp <= 0) return 'text-gray-500';
    if (temp <= 35) return 'text-neon-cyan';
    if (temp <= 45) return 'text-neon-orange';
    return 'text-neon-red';
}

// ============================================================================
// SETUP WIZARD
// ============================================================================

function runDiscovery() {
    console.log('[FanControl] Starting hardware discovery...');
    
    document.getElementById('discover-btn').disabled = true;
    document.getElementById('discover-loader').classList.remove('hidden');
    wizardStep = 'scanning';
    
    fetch('/api/discover', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            document.getElementById('discover-btn').disabled = false;
            document.getElementById('discover-loader').classList.add('hidden');
            
            if (data.status === 'ok') {
                renderDiscoveredHardware(data);
                wizardStep = 'results';
                
                document.getElementById('setup-step-intro').classList.add('hidden');
                document.getElementById('setup-step-results').classList.remove('hidden');
            } else {
                alert('Scan error: ' + data.message);
                wizardStep = 'intro';
            }
        })
        .catch(err => {
            console.error('Discovery error:', err);
            alert('Connection error during scan');
            document.getElementById('discover-btn').disabled = false;
            document.getElementById('discover-loader').classList.add('hidden');
            wizardStep = 'intro';
        });
}

function renderDiscoveredHardware(data) {
    const container = document.getElementById('discovered-devices');
    if (!container) return;
    
    let html = '';
    
    // Fans section
    if (data.fans && Object.keys(data.fans).length > 0) {
        html += '<h4 class="text-sm font-semibold text-neon-cyan mb-2">🌀 Fans</h4>';
        for (const [id, fan] of Object.entries(data.fans)) {
            html += `
                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">
                    <div>
                        <span class="text-sm text-white">${escapeHtml(fan.label)}</span>
                        <span class="text-xs text-gray-500 ml-2">${fan.writable ? '✅ Controllable' : '⚠️ Read-only'}</span>
                    </div>
                    <span class="text-xs bg-orange-900 bg-opacity-30 text-neon-orange px-2 py-0.5 rounded">Not calibrated</span>
                </div>
            `;
        }
    }
    
    // Sensors section
    if (data.temps && Object.keys(data.temps).length > 0) {
        html += '<h4 class="text-sm font-semibold text-neon-green mb-2 mt-4">🌡️ Temperature Sensors</h4>';
        for (const [id, sensor] of Object.entries(data.temps)) {
            html += `
                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">
                    <span class="text-sm text-white">${escapeHtml(sensor.label)}</span>
                    <span class="text-sm font-mono text-neon-cyan">${sensor.value || 0}°C</span>
                </div>
            `;
        }
    }
    
    // Disks section
    if (data.disks && Object.keys(data.disks).length > 0) {
        html += '<h4 class="text-sm font-semibold text-neon-purple mb-2 mt-4">💾 Storage Disks</h4>';
        for (const [id, disk] of Object.entries(data.disks)) {
            html += `
                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">
                    <span class="text-sm text-white">${escapeHtml(disk.label)} <span class="text-xs text-gray-500">(${escapeHtml(disk.type)})</span></span>
                    <span class="text-sm font-mono ${getTempColorClass(disk.temp)}">
                            ${disk.standby ? t('sensor.sleep', 'Sleep') : disk.temp > 0 ? disk.temp + '°C' : '--'}
                    </span>
                </div>
            `;
        }
    }
    
    container.innerHTML = html || `<p class="text-gray-500">${t('setup.no_hardware', 'No hardware detected')}</p>`;
    
    // Show calibrate button if fans found
    if (data.fans && Object.keys(data.fans).length > 0) {
        document.getElementById('setup-step-action').classList.remove('hidden');
    }
}

function runCalibration() {
    console.log('[FanControl] Starting calibration...');
    
    document.getElementById('calibrate-btn').disabled = true;
    document.getElementById('calibrate-loader').classList.remove('hidden');
    wizardStep = 'calibrating';
    
    document.getElementById('calibration-modal').classList.remove('hidden');
    document.getElementById('calibration-status').textContent = 'Starting...';
    document.getElementById('calibration-progress-bar').style.width = '0%';
    document.getElementById('calibration-step').textContent = 'Step 0/11';
    
    fetch('/api/initialize', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            console.log('[FanControl] Calibration initiated:', data);
        })
        .catch(err => {
            console.error('Calibration error:', err);
            hideCalibrationModal();
            document.getElementById('calibrate-btn').disabled = false;
            document.getElementById('calibrate-loader').classList.add('hidden');
        });
}

function updateCalibrationModal(progress) {
    const modal = document.getElementById('calibration-modal');
    if (modal.classList.contains('hidden')) {
        modal.classList.remove('hidden');
    }
    
    document.getElementById('calibration-status').textContent = progress.status;
    document.getElementById('calibration-step').textContent = 
        `Step ${progress.step}/${progress.total}`;
    
    const pct = progress.total > 0 ? (progress.step / progress.total * 100) : 0;
    document.getElementById('calibration-progress-bar').style.width = `${pct}%`;
}

function hideCalibrationModal() {
    document.getElementById('calibration-modal').classList.add('hidden');
}

function startCalibration() {
    if (!confirm(t('calibration.confirm', 'Recalibrate all fans? This takes 1-2 minutes.'))) return;
    
    document.getElementById('calibration-modal').classList.remove('hidden');
    document.getElementById('calibration-status').textContent = 'Starting...';
    document.getElementById('calibration-progress-bar').style.width = '0%';
    document.getElementById('calibration-step').textContent = 'Step 0/11';
    
    fetch('/api/initialize', { method: 'POST' })
        .catch(err => console.error('Calibration error:', err));
}

// ============================================================================
// SCHEDULE GRID
// ============================================================================

const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DAY_KEYS = ['days.mon', 'days.tue', 'days.wed', 'days.thu', 'days.fri', 'days.sat', 'days.sun'];

function tDay(idx) {
    return t(DAY_KEYS[idx], DAY_LABELS[idx]);
}

function renderScheduleGrid() {
    const container = document.getElementById('schedule-grid');
    if (!container) return;
    
    const fan = currentState?.fans?.[currentFanId];
    const schedule = fan?.schedule || [];
    scheduleData = {};
    schedule.forEach(item => {
        const key = `${item.day}_${item.time_start}`;
        scheduleData[key] = item;
    });
    
    // Build color map for cells
    const colorMap = {};
    const groups = {};
    schedule.forEach(item => {
        const key = ruleKey(item);
        if (!groups[key]) groups[key] = [];
        groups[key].push(item);
    });
    const groupKeys = Object.keys(groups);
    groupKeys.forEach((gk, idx) => {
        const color = getRuleColor(idx);
        groups[gk].forEach(item => {
            const cellKey = `${item.day}_${item.time_start}`;
            colorMap[cellKey] = color;
        });
    });
    
    let html = '<table class="border-collapse" style="border-spacing: 1px;">';
    
    // Header row: empty corner + 24 hours
    html += '<tr><th class="w-12 h-5"></th>';
    for (let h = 0; h < 24; h++) {
        html += `<th class="h-5 px-0 text-[10px] text-gray-500 font-normal" style="width:18px">${h}</th>`;
    }
    html += '</tr>';
    
    // Day rows
    for (let d = 0; d < DAYS.length; d++) {
        const day = DAYS[d];
        html += `<tr><td class="w-12 h-5 text-[10px] text-gray-400 font-semibold pr-1 text-right align-middle">${tDay(d)}</td>`;
        
        for (let h = 0; h < 24; h++) {
            const timeStr = String(h).padStart(2, '0') + ':00';
            const key = `${day}_${timeStr}`;
            const item = scheduleData[key];
            
            let bgStyle = 'background:#1f2937';
            if (item) {
                const cm = colorMap[key];
                if (cm) {
                    bgStyle = `background:${cm.hex}`;
                } else {
                    bgStyle = item.mode === 'auto' ? 'background:#15803d' : item.mode === 'manual' ? 'background:#c2410c' : 'background:#991b1b';
                }
            }
            
            html += `<td class="cursor-pointer schedule-cell transition-colors duration-75"
                         data-day="${day}" data-hour="${h}"
                         onmousedown="onScheduleMouseDown(event,'${day}',${h})"
                         onmouseenter="onScheduleMouseEnter(event,'${day}',${h})"
                         title="${tDay(d)} ${timeStr}${item ? ' [' + t('mode.' + item.mode, item.mode) + ']' : ''}"
                         style="width:18px;height:18px;${bgStyle}"></td>`;
        }
        html += '</tr>';
    }
    
    html += '</table>';
    container.innerHTML = html;
    
    renderScheduleRules();
    validateSchedule();
}

const RULE_COLORS = [
    { hex: '#15803d', dot: '#4ade80', text: '#86efac' },
    { hex: '#c2410c', dot: '#fb923c', text: '#fdba74' },
    { hex: '#991b1b', dot: '#f87171', text: '#fca5a5' },
    { hex: '#1d4ed8', dot: '#60a5fa', text: '#93c5fd' },
    { hex: '#7e22ce', dot: '#c084fc', text: '#d8b4fe' },
    { hex: '#a16207', dot: '#facc15', text: '#fde047' },
    { hex: '#be185d', dot: '#f472b6', text: '#f9a8d4' },
    { hex: '#0f766e', dot: '#2dd4bf', text: '#5eead4' },
];

function getRuleColor(idx) {
    if (idx < RULE_COLORS.length) return RULE_COLORS[idx];
    // Generate colors via HSL for groups beyond 8
    const hue = (idx * 137) % 360;
    const hex = `hsl(${hue}, 60%, 35%)`;
    const dot = `hsl(${hue}, 70%, 65%)`;
    const text = `hsl(${hue}, 70%, 80%)`;
    return { hex, dot, text };
}

function ruleKey(item) {
    return JSON.stringify({
        mode: item.mode,
        target_temp: item.target_temp,
        speed_pct: item.speed_pct,
        sensors: (item.sensors || []).sort(),
        sensor_mode: item.sensor_mode
    });
}

function renderScheduleRules() {
    const container = document.getElementById('schedule-rules');
    if (!container) return;
    
    const fan = currentState?.fans?.[currentFanId];
    const schedule = fan?.schedule || [];
    
    if (schedule.length === 0) {
        container.innerHTML = `<p class="text-xs text-gray-500 italic">${t('schedule.no_rules', 'No rules configured')}</p>`;
        return;
    }
    
    // Group by identical settings
    const groups = {};
    schedule.forEach(item => {
        const key = ruleKey(item);
        if (!groups[key]) groups[key] = { item, cells: [] };
        groups[key].cells.push(item);
    });
    
    const groupList = Object.values(groups);
    
    let html = '<div class="space-y-1">';
    groupList.forEach((group, gIdx) => {
        const color = getRuleColor(gIdx);
        const item = group.item;
        const cells = group.cells;
        
        let settings = '';
        if (item.mode === 'auto') {
            const sensorNames = (item.sensors || []).map(s => {
                const sen = allSensors.find(x => x.id === s);
                return sen ? sen.label : s.split(':').pop();
            });
            settings = `${item.target_temp || 31}°C`;
            if (sensorNames.length > 0) {
                settings += ` · ${sensorNames.join(', ')}`;
                if (item.sensor_mode && sensorNames.length > 1) {
                    settings += ` (${item.sensor_mode})`;
                }
            }
        } else if (item.mode === 'manual') {
            settings = `${item.speed_pct ?? 50}%`;
        } else {
            settings = 'off';
        }
        
        // Group cells by day to build sub-periods
        const byDay = {};
        cells.forEach(c => {
            if (!byDay[c.day]) byDay[c.day] = [];
            byDay[c.day].push(c);
        });
        
        // Build contiguous time ranges per day
        const subPeriods = [];
        for (const [day, dayCells] of Object.entries(byDay)) {
            const hours = dayCells.map(c => parseInt(c.time_start)).sort((a, b) => a - b);
            let start = hours[0], prev = hours[0];
            for (let i = 1; i < hours.length; i++) {
                if (hours[i] === prev + 1) {
                    prev = hours[i];
                } else {
                    subPeriods.push({ day, from: start, to: prev });
                    start = hours[i];
                    prev = hours[i];
                }
            }
            subPeriods.push({ day, from: start, to: prev });
        }
        subPeriods.sort((a, b) => {
            const d = DAYS.indexOf(a.day) - DAYS.indexOf(b.day);
            return d !== 0 ? d : a.from - b.from;
        });
        
        const modeIcon = item.mode === 'auto' ? '🌡️' : item.mode === 'manual' ? '🎮' : '⏻';
        
        html += `
            <div class="bg-cyber-accent rounded-lg overflow-hidden">
                <div class="flex items-center gap-2 px-3 py-2">
                    <span class="w-3 h-3 rounded-full flex-shrink-0" style="background:${color.dot}"></span>
                    <span class="text-xs flex-shrink-0">${modeIcon}</span>
                    <div class="flex-1 min-w-0 cursor-pointer" onclick="toggleRuleGroup(${gIdx})">
                        <span class="text-xs font-semibold" style="color:${color.text}">${escapeHtml(settings)}</span>
                        <span class="text-[10px] text-gray-500 ml-2">${cells.length}h</span>
                    </div>
                    <button onclick="editRuleGroup(${gIdx}); event.stopPropagation()" 
                            class="text-[10px] text-gray-400 hover:text-neon-cyan px-1.5 py-0.5 rounded hover:bg-cyber-bg transition-all flex-shrink-0">
                        Edit
                    </button>
                    <button onclick="deleteRuleGroup(${gIdx}); event.stopPropagation()" 
                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-bg transition-all flex-shrink-0">
                        Del
                    </button>
                    <span id="rule-chevron-${gIdx}" class="text-[10px] text-gray-500 transition-transform duration-200 cursor-pointer" onclick="toggleRuleGroup(${gIdx})">▸</span>
                </div>
                <div id="rule-subperiods-${gIdx}" class="hidden border-t border-gray-700">
        `;
        
        subPeriods.forEach((sp, sIdx) => {
            const dayLabel = tDay(DAYS.indexOf(sp.day));
            const fromStr = String(sp.from).padStart(2, '0') + ':00';
            const toStr = String(sp.to + 1).padStart(2, '0') + ':00';
            
            html += `
                <div class="flex items-center gap-2 px-3 py-1.5 hover:bg-cyber-bg transition-all">
                    <span class="w-2 h-2 rounded-full flex-shrink-0" style="background:${color.dot}; opacity:0.6"></span>
                    <span class="text-[11px] text-gray-300 flex-1">${dayLabel} ${fromStr}–${toStr}</span>
                    <button onclick="editSinglePeriod('${sp.day}', ${sp.from}, ${sp.to}); event.stopPropagation()" 
                            class="text-[10px] text-gray-400 hover:text-neon-cyan px-1.5 py-0.5 rounded hover:bg-cyber-accent transition-all">
                        Edit
                    </button>
                    <button onclick="deleteSinglePeriod('${sp.day}', ${sp.from}, ${sp.to}); event.stopPropagation()" 
                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-accent transition-all">
                        Del
                    </button>
                </div>
            `;
        });
        
        html += `
                </div>
            </div>
        `;
    });
    html += '</div>';
    container.innerHTML = html;
    container._groups = groupList;
    
    // Restore expanded state
    expandedRuleGroups.forEach(idx => {
        const el = document.getElementById(`rule-subperiods-${idx}`);
        const chevron = document.getElementById(`rule-chevron-${idx}`);
        if (el) {
            el.classList.remove('hidden');
            if (chevron) chevron.textContent = '▾';
        }
    });
}

function toggleRuleGroup(idx) {
    const el = document.getElementById(`rule-subperiods-${idx}`);
    const chevron = document.getElementById(`rule-chevron-${idx}`);
    if (!el) return;
    el.classList.toggle('hidden');
    if (el.classList.contains('hidden')) {
        expandedRuleGroups.delete(idx);
    } else {
        expandedRuleGroups.add(idx);
    }
    if (chevron) chevron.textContent = el.classList.contains('hidden') ? '▸' : '▾';
}

function editSinglePeriod(day, fromHour, toHour) {
    const cells = [];
    for (let h = fromHour; h <= toHour; h++) {
        cells.push({ day, hour: h });
    }
    openScheduleEditor(cells);
}

function deleteSinglePeriod(day, fromHour, toHour) {
    for (let h = fromHour; h <= toHour; h++) {
        const key = `${day}_${String(h).padStart(2, '0')}:00`;
        delete scheduleData[key];
    }
    applyScheduleToFan();
}

function editRuleGroup(idx) {
    const container = document.getElementById('schedule-rules');
    const group = container._groups[idx];
    if (!group) return;
    const cells = group.cells.map(c => ({ day: c.day, hour: parseInt(c.time_start) }));
    openScheduleEditor(cells);
}

function deleteRuleGroup(idx) {
    const container = document.getElementById('schedule-rules');
    const group = container._groups[idx];
    if (!group) return;
    group.cells.forEach(cell => {
        const key = `${cell.day}_${cell.time_start}`;
        delete scheduleData[key];
    });
    expandedRuleGroups.delete(idx);
    applyScheduleToFan();
}

function onScheduleMouseDown(e, day, hour) {
    e.preventDefault();
    isDraggingSchedule = true;
    dragStartCell = { day, hour };
    scheduleSelection = [{ day, hour }];
    highlightSelection();
}

function onScheduleMouseEnter(e, day, hour) {
    if (!isDraggingSchedule || !dragStartCell) return;
    
    const startH = dragStartCell.hour;
    const startD = DAYS.indexOf(dragStartCell.day);
    const endD = DAYS.indexOf(day);
    const minD = Math.min(startD, endD);
    const maxD = Math.max(startD, endD);
    
    scheduleSelection = [];
    
    if (minD === maxD) {
        // Same day: select hour range
        const hFrom = Math.min(startH, hour);
        const hTo = Math.max(startH, hour);
        for (let h = hFrom; h <= hTo; h++) {
            scheduleSelection.push({ day: DAYS[minD], hour: h });
        }
    } else {
        // Cross-day: select ALL hours on each day in range
        for (let d = minD; d <= maxD; d++) {
            for (let h = 0; h < 24; h++) {
                scheduleSelection.push({ day: DAYS[d], hour: h });
            }
        }
    }
    highlightSelection();
}

function highlightSelection() {
    clearHighlight();
    for (const cell of scheduleSelection) {
        const el = document.querySelector(`.schedule-cell[data-day="${cell.day}"][data-hour="${cell.hour}"]`);
        if (el) {
            el.style.outline = '2px solid #00f0ff';
            el.style.outlineOffset = '-1px';
            el.style.zIndex = '1';
        }
    }
}

function clearHighlight() {
    document.querySelectorAll('.schedule-cell').forEach(el => {
        el.style.outline = '';
        el.style.outlineOffset = '';
        el.style.zIndex = '';
    });
}

document.addEventListener('mouseup', () => {
    if (!isDraggingSchedule) return;
    isDraggingSchedule = false;
    
    if (scheduleSelection.length === 1) {
        openScheduleEditor([scheduleSelection[0]]);
    } else if (scheduleSelection.length > 1) {
        openScheduleEditor([...scheduleSelection]);
    }
    scheduleSelection = [];
    clearHighlight();
});

// ============================================================================
// SCHEDULE EDITOR
// ============================================================================

function openScheduleEditor(cells) {
    editingCells = cells;
    scheduleEditorSensors = [];
    
    const editor = document.getElementById('schedule-editor');
    editor.classList.remove('hidden');
    
    // Build human-readable period description
    document.getElementById('schedule-editor-cells').textContent = describeCells(cells);
    
    // Get existing data from first cell
    const key = `${cells[0].day}_${String(cells[0].hour).padStart(2, '0')}:00`;
    const existing = scheduleData[key];
    
    if (existing) {
        setScheduleMode(existing.mode);
        document.getElementById('sched-target-temp').value = existing.target_temp || 31;
        document.getElementById('sched-speed-slider').value = existing.speed_pct ?? 50;
        document.getElementById('sched-speed-value').textContent = `${existing.speed_pct ?? 50}%`;
        scheduleEditorSensors = [...(existing.sensors || [])];
        if (existing.sensor_mode) setScheduleSensorMode(existing.sensor_mode);
    } else {
        setScheduleMode('auto');
        document.getElementById('sched-target-temp').value = 31;
        document.getElementById('sched-speed-slider').value = 50;
        document.getElementById('sched-speed-value').textContent = '50%';
        
        // Auto-fill sensors from first existing schedule item
        const fan = currentState?.fans?.[currentFanId];
        const schedule = fan?.schedule || [];
        if (schedule.length > 0) {
            const first = schedule[0];
            scheduleEditorSensors = [...(first.sensors || [])];
            if (first.sensor_mode) setScheduleSensorMode(first.sensor_mode);
        }
    }
    
    updateScheduleEditorSensors();
}

function setScheduleMode(mode) {
    const modes = ['auto', 'manual', 'off'];
    const activeClass = 'bg-neon-cyan bg-opacity-20 text-neon-cyan border-neon-cyan border-opacity-30';
    const inactiveClass = 'bg-cyber-accent text-gray-400 border-gray-700 hover:bg-cyan-900 hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan';
    
    modes.forEach(m => {
        const btn = document.getElementById(`sched-btn-${m}`);
        if (btn) btn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${m === mode ? activeClass : inactiveClass}`;
    });
    
    document.getElementById('sched-auto-settings').classList.toggle('hidden', mode !== 'auto');
    document.getElementById('sched-manual-settings').classList.toggle('hidden', mode !== 'manual');
}

function setScheduleSensorMode(sensorMode) {
    const modes = ['max', 'min', 'avg'];
    const activeClass = 'bg-neon-cyan bg-opacity-20 text-neon-cyan border-neon-cyan border-opacity-30';
    const inactiveClass = 'bg-cyber-accent text-gray-400 border-gray-700 hover:bg-cyan-900 hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan';
    
    modes.forEach(m => {
        const btn = document.getElementById(`sched-btn-sensor-${m}`);
        if (btn) btn.className = `flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border ${m === sensorMode ? activeClass : inactiveClass}`;
    });
}

function updateScheduleEditorSensors() {
    const container = document.getElementById('sched-sensor-tags');
    if (!container) return;
    
    if (scheduleEditorSensors.length === 0) {
        container.innerHTML = `<span class="text-xs text-gray-500 italic">${t('editor.no_sensors', 'No sensors assigned')}</span>`;
        document.getElementById('sched-sensor-mode-section').classList.add('hidden');
        return;
    }
    
    container.innerHTML = scheduleEditorSensors.map(s => {
        const sensor = allSensors.find(x => x.id === s);
        const label = sensor ? sensor.label : s;
        return `
            <span class="inline-flex items-center gap-1 bg-cyber-accent text-gray-300 text-xs px-2 py-1 rounded-full">
                ${escapeHtml(label)}
                <button onclick="removeScheduleSensor('${escapeHtml(s)}')" class="text-neon-red hover:text-red-400 ml-1">&times;</button>
            </span>
        `;
    }).join('');
    
    document.getElementById('sched-sensor-mode-section').classList.toggle('hidden', scheduleEditorSensors.length <= 1);
}

function removeScheduleSensor(sensorId) {
    scheduleEditorSensors = scheduleEditorSensors.filter(s => s !== sensorId);
    updateScheduleEditorSensors();
}

function toggleScheduleSensorPopup() {
    const popup = document.getElementById('sensor-popup');
    const list = document.getElementById('sensor-popup-list');
    if (!popup || !list) return;
    
    if (popup.classList.contains('hidden')) {
        const groups = {};
        allSensors.forEach(s => {
            if (!groups[s.group]) groups[s.group] = [];
            groups[s.group].push(s);
        });
        
        let html = '';
        for (const [group, sensors] of Object.entries(groups)) {
            html += `<div class="text-xs font-semibold text-gray-500 uppercase mb-2">${t(group, group)}</div>`;
            sensors.forEach(s => {
                const checked = scheduleEditorSensors.includes(s.id);
                html += `
                    <label class="flex items-center gap-2 py-1.5 cursor-pointer hover:bg-cyber-accent rounded px-2">
                        <input type="checkbox" value="${escapeHtml(s.id)}" ${checked ? 'checked' : ''} 
                               class="accent-neon-purple">
                        <span class="text-sm text-gray-300">${escapeHtml(s.label)}</span>
                        <span class="text-xs text-gray-500 ml-auto">
                            ${s.standby ? t('sensor.sleep', 'Sleep') : s.temp + '°C'}
                        </span>
                    </label>
                `;
            });
        }
        
        list.innerHTML = html;
        popup.classList.remove('hidden');
        
        // Override close behavior for schedule context
        popup._scheduleMode = true;
    } else {
        // Collect checked sensors
        const checked = popup.querySelectorAll('input[type=checkbox]:checked');
        scheduleEditorSensors = Array.from(checked).map(cb => cb.value);
        updateScheduleEditorSensors();
        popup.classList.add('hidden');
        popup._scheduleMode = false;
    }
}

function saveScheduleEdit() {
    const mode = document.querySelector('#sched-btn-auto.bg-neon-cyan') ? 'auto'
        : document.querySelector('#sched-btn-manual.bg-neon-cyan') ? 'manual' : 'off';
    
    const newItems = editingCells.map(cell => {
        const key = `${cell.day}_${String(cell.hour).padStart(2, '0')}:00`;
        const item = {
            day: cell.day,
            time_start: String(cell.hour).padStart(2, '0') + ':00',
            time_end: String(cell.hour).padStart(2, '0') + ':59',
            mode: mode
        };
        
        if (mode === 'auto') {
            item.target_temp = parseInt(document.getElementById('sched-target-temp').value) || 31;
            item.sensors = [...scheduleEditorSensors];
            const activeSensorMode = document.querySelector('#sched-btn-sensor-max.bg-neon-cyan') ? 'max'
                : document.querySelector('#sched-btn-sensor-min.bg-neon-cyan') ? 'min' : 'avg';
            item.sensor_mode = activeSensorMode;
        } else if (mode === 'manual') {
            item.speed_pct = parseInt(document.getElementById('sched-speed-slider').value) || 50;
        }
        
        scheduleData[key] = item;
        return item;
    });
    
    closeScheduleEditor();
    applyScheduleToFan();
}

function deleteScheduleEdit() {
    for (const cell of editingCells) {
        const key = `${cell.day}_${String(cell.hour).padStart(2, '0')}:00`;
        delete scheduleData[key];
    }
    closeScheduleEditor();
    applyScheduleToFan();
}

function closeScheduleEditor() {
    document.getElementById('schedule-editor').classList.add('hidden');
    editingCells = [];
}

function clearSchedule() {
    scheduleData = {};
    applyScheduleToFan();
}

function fillScheduleDefaults() {
    const fan = currentState?.fans?.[currentFanId];
    const defaultSensors = fan?.sensors || [];
    const defaultSensorMode = fan?.sensor_mode || 'max';
    const defaultTemp = fan?.target_temp || 31;
    
    for (const day of DAYS) {
        for (let hour = 0; hour < 24; hour++) {
            const key = `${day}_${String(hour).padStart(2, '0')}:00`;
            if (!scheduleData[key]) {
                scheduleData[key] = {
                    day: day,
                    time_start: String(hour).padStart(2, '0') + ':00',
                    time_end: String(hour).padStart(2, '0') + ':59',
                    mode: 'auto',
                    target_temp: defaultTemp,
                    sensors: [...defaultSensors],
                    sensor_mode: defaultSensorMode
                };
            }
        }
    }
    applyScheduleToFan();
}

function applyScheduleToFan() {
    const schedule = Object.values(scheduleData);
    
    // Update local state immediately so render sees new data
    if (currentState?.fans?.[currentFanId]) {
        currentState.fans[currentFanId].schedule = schedule;
    }
    
    sendControl({
        action: 'set_fan_config',
        fan: currentFanId,
        schedule: schedule
    });
    renderScheduleGrid();
}

function describeCells(cells) {
    if (cells.length === 0) return '';
    if (cells.length === 1) {
        return `${tDay(DAYS.indexOf(cells[0].day))} ${String(cells[0].hour).padStart(2, '0')}:00`;
    }
    
    const days = [...new Set(cells.map(c => c.day))].sort((a, b) => DAYS.indexOf(a) - DAYS.indexOf(b));
    const hours = [...new Set(cells.map(c => c.hour))].sort((a, b) => a - b);
    
    let dayStr = '';
    if (days.length === 7) {
        dayStr = t('schedule.every_day', 'Every day');
    } else if (days.length === 5 && !days.includes('sat') && !days.includes('sun')) {
        dayStr = t('schedule.weekdays', 'Weekdays');
    } else if (days.length === 2 && days.includes('sat') && days.includes('sun')) {
        dayStr = t('schedule.weekends', 'Weekends');
    } else if (days.length <= 3) {
        dayStr = days.map(d => tDay(DAYS.indexOf(d))).join(', ');
    } else {
        dayStr = `${days.length} days`;
    }
    
    if (hours.length === 24) {
        return `${dayStr}, 00:00-23:59`;
    }
    
    const minH = String(Math.min(...hours)).padStart(2, '0');
    const maxH = String(Math.max(...hours) + 1).padStart(2, '0');
    return `${dayStr}, ${minH}:00-${maxH.length > 5 ? '00:00 next day' : maxH + ':00'}`;
}

function validateSchedule() {
    const fan = currentState?.fans?.[currentFanId];
    const schedule = fan?.schedule || [];
    const coverage = document.getElementById('schedule-coverage');
    const warning = document.getElementById('schedule-incomplete-warning');
    const detail = document.getElementById('schedule-incomplete-detail');
    
    if (!coverage) return;
    
    const total = 7 * 24;
    const filled = schedule.length;
    const pct = Math.round((filled / total) * 100);
    
    coverage.textContent = `${filled}/${total} (${pct}%)`;
    coverage.className = pct === 100 ? 'text-xs text-neon-green' : 'text-xs text-neon-orange';
    
    if (pct < 100) {
        const emptyDays = [];
        for (let i = 0; i < DAYS.length; i++) {
            const dayHours = schedule.filter(s => s.day === DAYS[i]).length;
            if (dayHours < 24) emptyDays.push(tDay(i));
        }
        warning.classList.remove('hidden');
        detail.textContent = `${t('schedule.missing', 'Missing')}: ${emptyDays.join(', ')}. ${t('schedule.empty_hours', 'Empty hours = fan off.')}`;
    } else {
        warning.classList.add('hidden');
    }
}

// ============================================================================
// SETTINGS & LANGUAGE
// ============================================================================

function toggleSettings() {
    const overlay = document.getElementById('settings-overlay');
    const panel = document.getElementById('settings-panel');
    if (!overlay || !panel) return;
    
    const isOpen = !panel.classList.contains('hidden');
    if (isOpen) {
        overlay.classList.add('hidden');
        panel.classList.add('hidden');
    } else {
        overlay.classList.remove('hidden');
        panel.classList.remove('hidden');
        updateLangButtons();
    }
}

function updateLangButtons() {
    const enBtn = document.getElementById('lang-btn-en');
    const ruBtn = document.getElementById('lang-btn-ru');
    const setupEn = document.getElementById('setup-lang-en');
    const setupRu = document.getElementById('setup-lang-ru');
    
    const activeClass = 'bg-neon-cyan bg-opacity-20 text-neon-cyan border-neon-cyan border-opacity-30';
    const inactiveClass = 'bg-cyber-accent text-gray-400 border-gray-700 hover:text-white';
    
    if (enBtn) enBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${currentLang === 'en' ? activeClass : inactiveClass}`;
    if (ruBtn) ruBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${currentLang === 'ru' ? activeClass : inactiveClass}`;
    if (setupEn) setupEn.className = `text-xs px-2 py-1 rounded border transition-all ${currentLang === 'en' ? activeClass : inactiveClass}`;
    if (setupRu) setupRu.className = `text-xs px-2 py-1 rounded border transition-all ${currentLang === 'ru' ? activeClass : inactiveClass}`;
}

async function switchLanguage(code) {
    if (code === currentLang) return;
    
    const success = await loadLang(code);
    if (success) {
        updateLangButtons();
        // Save to server config
        fetch('/api/language', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language: code })
        }).catch(() => {});
        
        // Re-render dynamic content
        if (currentFanId) {
            const fan = currentState?.fans?.[currentFanId];
            if (fan) updateInspector(fan);
        }
    }
}

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', async () => {
    console.log('[FanControl] v3.2.0 - Neon Cyberpunk Edition initialized');
    
    // Load language
    await loadLang(currentLang);
    updateLangButtons();
    
    // Click outside to close sensor popup (stop propagation to avoid closing editor underneath)
    document.getElementById('sensor-popup')?.addEventListener('click', function(e) {
        e.stopPropagation();
        if (e.target === this) {
            closeSensorPopupForContext();
        }
    });
    
    // Click outside to close schedule editor (only if sensor popup is not open)
    document.getElementById('schedule-editor')?.addEventListener('click', function(e) {
        if (e.target === this && document.getElementById('sensor-popup')?.classList.contains('hidden')) {
            closeScheduleEditor();
        }
    });
    
    // Schedule speed slider
    document.getElementById('sched-speed-slider')?.addEventListener('input', (e) => {
        document.getElementById('sched-speed-value').textContent = `${e.target.value}%`;
    });
    
    // Initial chart load (after short delay to ensure DOM is ready)
    setTimeout(updateChart, 2000);
});

console.log('[FanControl] main.js loaded successfully');
/**
 * FanControl Web v3.0.1 - Neon Cyberpunk Edition
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
                    <span class="text-xs px-1.5 py-0.5 rounded ${getStatusBadgeClass(fan.status)}">${escapeHtml(fan.status)}</span>
                </div>
                <div class="flex items-center justify-between text-xs">
                    <span class="text-gray-500">${escapeHtml(fan.mode || 'manual')}</span>
                    <span class="font-mono text-neon-cyan" id="fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0} RPM</span>
                </div>
            </div>
        `;
    }
    
    container.innerHTML = html || '<div class="text-center text-gray-500 py-8">No fans detected</div>';
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
    statusBadge.textContent = fan.status || 'unknown';
    statusBadge.className = `text-xs px-2 py-0.5 rounded-full ${getStatusBadgeClass(fan.status)}`;
    
    // Update mode badge
    const modeBadge = document.getElementById('fan-mode-badge');
    const mode = fan.mode || 'manual';
    modeBadge.textContent = mode.toUpperCase();
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
    
    // Update target temp
    document.getElementById('target-temp-input').value = fan.target_temp || 31;
    
    // Update sensor tags
    updateSensorTags(fan);
    
    // Store config
    if (!fanConfigs[currentFanId]) fanConfigs[currentFanId] = {};
    fanConfigs[currentFanId].sensors = fan.sensors || [];
    fanConfigs[currentFanId].target_temp = fan.target_temp || 31;
    fanConfigs[currentFanId].mode = mode;
}

function updateSensorTags(fan) {
    const container = document.getElementById('sensor-tags');
    if (!container) return;
    
    const sensors = fan.sensors || [];
    
    if (sensors.length === 0) {
        container.innerHTML = '<span class="text-xs text-gray-500 italic">No sensors assigned</span>';
        return;
    }
    
    container.innerHTML = sensors.map(s => {
        const sensor = allSensors.find(x => x.id === s);
        const label = sensor ? sensor.label : s;
        return `
            <span class="inline-flex items-center gap-1 bg-cyber-accent text-gray-300 text-xs px-2 py-1 rounded-full">
                ${escapeHtml(label)}
                <button onclick="removeSensor('${escapeHtml(s)}')" class="text-neon-red hover:text-red-400 ml-1">&times;</button>
            </span>
        `;
    }).join('');
}

// ============================================================================
// FAN CONTROL ACTIONS
// ============================================================================

function setFanMode(mode) {
    if (!currentFanId) return;
    
    sendControl({
        action: 'set_fan_config',
        fan: currentFanId,
        fan_mode: mode
    });
}

function saveTargetTemp() {
    if (!currentFanId) return;
    
    const temp = parseInt(document.getElementById('target-temp-input').value);
    if (isNaN(temp) || temp < 20 || temp > 60) return;
    
    sendControl({
        action: 'set_fan_config',
        fan: currentFanId,
        target_temp: temp
    });
}

function removeSensor(sensorId) {
    if (!currentFanId) return;
    
    const sensors = (fanConfigs[currentFanId]?.sensors || []).filter(s => s !== sensorId);
    
    sendControl({
        action: 'set_fan_config',
        fan: currentFanId,
        sensors: sensors
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
                group: 'Disks'
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
                group: 'Sensors'
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
            html += `<div class="text-xs font-semibold text-gray-500 uppercase mb-2">${escapeHtml(group)}</div>`;
            sensors.forEach(s => {
                const checked = currentSensors.includes(s.id);
                html += `
                    <label class="flex items-center gap-2 py-1.5 cursor-pointer hover:bg-cyber-accent rounded px-2">
                        <input type="checkbox" value="${escapeHtml(s.id)}" ${checked ? 'checked' : ''} 
                               class="accent-neon-purple">
                        <span class="text-sm text-gray-300">${escapeHtml(s.label)}</span>
                        <span class="text-xs text-gray-500 ml-auto">
                            ${s.standby ? 'Sleep' : s.temp + '°C'}
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
                    name: 'Max HDD Temp',
                    data: data.timestamps.map((ts, i) => ({
                        x: new Date(ts).getTime(),
                        y: data.temps[i]
                    }))
                },
                {
                    name: 'Avg PWM',
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
                    ${disk.standby ? 'Sleep' : disk.temp > 0 ? disk.temp + '°' : '--'}
                </span>
            </div>
        `;
    }
    
    container.innerHTML = html || '<div class="text-xs text-gray-500">No disks detected</div>';
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
                        ${disk.standby ? 'Sleep' : disk.temp > 0 ? disk.temp + '°C' : '--'}
                    </span>
                </div>
            `;
        }
    }
    
    container.innerHTML = html || '<p class="text-gray-500">No hardware detected</p>';
    
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
    if (!confirm('Recalibrate all fans? This takes 1-2 minutes.')) return;
    
    document.getElementById('calibration-modal').classList.remove('hidden');
    document.getElementById('calibration-status').textContent = 'Starting...';
    document.getElementById('calibration-progress-bar').style.width = '0%';
    document.getElementById('calibration-step').textContent = 'Step 0/11';
    
    fetch('/api/initialize', { method: 'POST' })
        .catch(err => console.error('Calibration error:', err));
}

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    console.log('[FanControl] v3.0.1 - Neon Cyberpunk Edition initialized');
    
    // Click outside to close sensor popup
    document.getElementById('sensor-popup')?.addEventListener('click', function(e) {
        if (e.target === this) closeSensorPopup();
    });
    
    // Initial chart load (after short delay to ensure DOM is ready)
    setTimeout(updateChart, 2000);
});

console.log('[FanControl] main.js loaded successfully');
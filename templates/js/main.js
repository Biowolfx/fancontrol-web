/**
 * FanControl Web v3.4.1 - Neon Cyberpunk Edition
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
const RELOAD_DELAY = 10000;
const SCHEDULE_CELL_SIZE = 18;

const BTN_ACTIVE = 'bg-neon-cyan bg-opacity-20 text-neon-cyan border-neon-cyan border-opacity-30';
const BTN_INACTIVE = 'bg-cyber-accent text-gray-400 border-gray-700 hover:text-white';
const BTN_MANUAL_ACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-purple bg-opacity-20 text-neon-purple border border-neon-purple border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-purple';
const BTN_MANUAL_INACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-purple hover:bg-opacity-20 hover:text-neon-purple hover:border-neon-purple';
const BTN_AUTO_ACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-neon-cyan bg-opacity-20 text-neon-cyan border border-neon-cyan border-opacity-30 hover:bg-opacity-40 hover:shadow-neon-cyan';
const BTN_AUTO_INACTIVE = 'py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 bg-cyber-accent text-gray-400 border border-gray-700 hover:bg-neon-cyan hover:bg-opacity-20 hover:text-neon-cyan hover:border-neon-cyan';

// ============================================================================
// PERSISTENT SETTINGS
// ============================================================================

const settingsDefaults = {
    tempUnit: 'celsius',
    refreshInterval: 0,
    compactMode: false,
    autoUpdateCheck: 21600000
};

let _cachedSettings = null;
let _settingsCacheTime = 0;
const SETTINGS_CACHE_TTL = 1000;

function getSettings() {
    const now = Date.now();
    if (_cachedSettings && (now - _settingsCacheTime) < SETTINGS_CACHE_TTL) {
        return _cachedSettings;
    }
    try {
        const raw = localStorage.getItem('fancontrol_settings');
        _cachedSettings = raw ? { ...settingsDefaults, ...JSON.parse(raw) } : { ...settingsDefaults };
    } catch { _cachedSettings = { ...settingsDefaults }; }
    _settingsCacheTime = now;
    return _cachedSettings;
}

function saveSettings(partial) {
    const s = getSettings();
    Object.assign(s, partial);
    localStorage.setItem('fancontrol_settings', JSON.stringify(s));
    _cachedSettings = s;
    _settingsCacheTime = Date.now();
    return s;
}

function formatTemp(celsius) {
    if (celsius == null || celsius === 0) return '--';
    const s = getSettings();
    if (s.tempUnit === 'fahrenheit') {
        return Math.round(celsius * 9 / 5 + 32) + '°F';
    }
    return celsius + '°C';
}

function getTempUnitSymbol() {
    return getSettings().tempUnit === 'fahrenheit' ? '°F' : '°C';
}

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
    const ver = currentState?.config_version;
    if (translations['app.title'] && ver) {
        document.title = `${translations['app.title']} v${ver}`;
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

function show(el) { if (el) el.classList.remove('hidden'); }
function hide(el) { if (el) el.classList.add('hidden'); }
function toggle(el, visible) { if (el) el.classList.toggle('hidden', !visible); }

function setDiscoverButtonState(loading) {
    const btn = document.getElementById('discover-btn');
    const loader = document.getElementById('discover-loader');
    if (btn) btn.disabled = loading;
    if (loader) toggle(loader, loading);
}

// ============================================================================
// SOCKET.IO CONNECTION
// ============================================================================

console.log('[FanControl] Establishing Socket.IO connection...');
const socket = io();
window.socket = socket;

let serverAvailable = true;

socket.on('disconnect', () => {
    serverAvailable = false;
    showServerUnavailable();
});

socket.on('connect', () => {
    serverAvailable = true;
    hideServerUnavailable();
});

function showServerUnavailable() {
    const banner = document.getElementById('server-unavailable-banner');
    if (banner) banner.classList.remove('hidden');
}

function hideServerUnavailable() {
    const banner = document.getElementById('server-unavailable-banner');
    if (banner) banner.classList.add('hidden');
}

let lastUIUpdate = 0;
socket.on('update', (data) => {
    currentState = data;
    if (data.test_progress && data.testing) {
        updateCalibrationModal(data.test_progress);
    }
    const interval = getSettings().refreshInterval;
    if (interval === 0) {
        updateUI(data);
    } else {
        const now = Date.now();
        if (now - lastUIUpdate >= interval) {
            lastUIUpdate = now;
            updateUI(data);
        }
    }
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

socket.on('hidden_sensors', (data) => {
    _hiddenSensors = data.hiddenSensors || [];
    buildServerTree();
});

socket.on('test_complete', (result) => {
    console.log('[FanControl] Calibration complete:', result);
    hideCalibrationModal();
    
    if (result.success) {
        wizardStep = 'done';
        currentState = { ...currentState, initialized: true, tested: true };
        showMainScreen();
    }
});

// ============================================================================
// UI UPDATE FUNCTIONS
// ============================================================================

function updateUI(data) {
    if (!data) return;
    
    // Update version displays
    const ver = data.config_version || '';
    const headerVer = document.getElementById('header-version');
    if (headerVer && ver) headerVer.textContent = `v${ver}`;
    const versionLink = document.getElementById('version-link');
    if (versionLink && ver) versionLink.textContent = `FanControl Web v${ver}`;
    
    // Show appropriate screen
    if (!data.initialized || !data.tested) {
        showSetupScreen();
        if (data.hardware_scanned && wizardStep === 'intro') {
            renderDiscoveredHardware({
                fans: data.fans,
                temps: data.temp_sensors,
                disks: data.hdd_sensors
            });
            wizardStep = 'results';
            setDiscoverButtonState(false);
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

    // Refresh server tree
    if (_dashboardLoaded) buildServerTree();

    // Dashboard live updates handled by startPickerLiveUpdate
}

function showSetupScreen() {
    document.getElementById('setup-screen').classList.remove('hidden');
    document.getElementById('main-screen').classList.add('hidden');
    // Close settings panel if open
    const overlay = document.getElementById('settings-overlay');
    const panel = document.getElementById('settings-panel');
    if (overlay) overlay.classList.add('hidden');
    if (panel) panel.classList.add('hidden');
}

function showMainScreen() {
    const mainScreen = document.getElementById('main-screen');
    const wasOnSetup = mainScreen?.classList.contains('hidden');

    document.getElementById('setup-screen').classList.add('hidden');
    mainScreen?.classList.remove('hidden');
    if (!currentState || !currentState.testing) {
        hideCalibrationModal();
    }
    if (wasOnSetup) showView('dashboard');
    updateCanvasColumns();
    loadPickerCards().then(() => {
        buildServerTree();
        startPickerLiveUpdate();
    });
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

// ============================================================================
// NODE TREE
// ============================================================================

function buildServerTree() {
    const container = document.getElementById('server-tree');
    if (!container) return;

    let html = '';

    // Local server
    html += renderLocalServerTree();

    // Remote nodes
    for (const node of nodesData) {
        html += renderRemoteNodeTree(node);
    }

    container.innerHTML = html || `<div class="text-center text-gray-500 py-4 text-xs">${t('nodes.no_nodes', 'No nodes connected')}</div>`;

    _collapsedNodes.forEach(nodeId => {
        const children = document.getElementById(`node-children-${nodeId}`);
        if (children) children.classList.add('hidden');
    });
}

function getHiddenSensors() {
    return _hiddenSensors || [];
}

function setHiddenSensors(hidden) {
    _hiddenSensors = hidden;
    scheduleDashboardSave();
}

function hideSensor(sensorId) {
    const el = document.querySelector(`[data-sensor-id="${sensorId}"]`);
    if (el) {
        el.style.transition = 'opacity 0.3s, max-height 0.3s, margin 0.3s, padding 0.3s';
        el.style.overflow = 'hidden';
        el.style.opacity = '0';
        el.style.maxHeight = '0';
        el.style.marginTop = '0';
        el.style.marginBottom = '0';
        el.style.paddingTop = '0';
        el.style.paddingBottom = '0';
        setTimeout(() => {
            const hidden = getHiddenSensors();
            if (!hidden.includes(sensorId)) {
                setHiddenSensors([...hidden, sensorId]);
            }
            buildServerTree();
        }, 320);
    } else {
        const hidden = getHiddenSensors();
        if (!hidden.includes(sensorId)) {
            setHiddenSensors([...hidden, sensorId]);
        }
        buildServerTree();
    }
}

function restoreSensor(sensorId) {
    setHiddenSensors(getHiddenSensors().filter(id => id !== sensorId));
    buildServerTree();
}

function restoreAllSensors() {
    setHiddenSensors([]);
    buildServerTree();
}

function renderLocalServerTree() {
    if (!currentState || !currentState.fans) return '';

    const fans = currentState.fans;
    const temps = currentState.temp_sensors || {};
    const disks = currentState.hdd_sensors || {};
    const hidden = getHiddenSensors();

    const visibleFans = Object.entries(fans).filter(([id]) => !hidden.includes(`fan:${id}`));
    const visibleTemps = Object.entries(temps).filter(([id]) => !hidden.includes(`temp:${id}`));
    const visibleDisks = Object.entries(disks).filter(([id]) => !hidden.includes(`disk:${id}`));
    const hiddenFans = Object.entries(fans).filter(([id]) => hidden.includes(`fan:${id}`));
    const hiddenTemps = Object.entries(temps).filter(([id]) => hidden.includes(`temp:${id}`));
    const hiddenDisks = Object.entries(disks).filter(([id]) => hidden.includes(`disk:${id}`));
    const hasHidden = hiddenFans.length + hiddenTemps.length + hiddenDisks.length > 0;

    let html = `
        <div class="node-group" data-node="local">
            <div class="flex items-center gap-2 p-2 rounded hover:bg-cyber-accent cursor-pointer node-header"
                 onclick="toggleNodeGroup('local')">
                <span class="text-neon-cyan text-xs">▼</span>
                <span class="text-sm font-semibold text-white">🖥 ${t('nodes.local_server', 'My Server')}</span>
                <span class="ml-auto text-xs bg-green-900 bg-opacity-30 text-neon-green px-1.5 py-0.5 rounded">${visibleFans.length} ${t('nodes.fans', 'fans')}</span>
            </div>
            <div class="node-children ml-4 space-y-px" id="node-children-local">
    `;

    for (const [fanId, fan] of visibleFans) {
        const isSelected = fanId === currentFanId;
        html += `
            <div data-sensor-id="fan:${escapeHtml(fanId)}" class="flex items-center gap-1.5 p-1 rounded cursor-pointer transition-all group ${isSelected ? 'bg-cyber-accent border-l-2 border-neon-purple' : 'hover:bg-cyber-accent border-l-2 border-transparent'}"
                 onclick="selectFanFromTree('${escapeHtml(fanId)}', 'local')">
                <span class="text-xs">🌀</span>
                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(fan.label)}</span>
                <span class="ml-auto text-xs font-mono text-neon-cyan" id="tree-fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0}</span>
                <button onclick="event.stopPropagation(); hideSensor('fan:${escapeHtml(fanId)}')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>
            </div>
        `;
    }

    for (const [sensorId, sensor] of visibleTemps) {
        html += `
            <div data-sensor-id="temp:${escapeHtml(sensorId)}" class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                <span class="text-xs">🌡</span>
                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(sensor.label)}</span>
                <span class="ml-auto text-xs font-mono text-neon-green">${sensor.value || 0}°C</span>
                <button onclick="event.stopPropagation(); hideSensor('temp:${escapeHtml(sensorId)}')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>
            </div>
        `;
    }

    for (const [diskId, disk] of visibleDisks) {
        html += `
            <div data-sensor-id="disk:${escapeHtml(diskId)}" class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                <span class="text-xs">💾</span>
                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(disk.label || diskId)}</span>
                <span class="ml-auto text-xs font-mono ${getTempColorClass(disk.temp)}">${disk.temp > 0 ? disk.temp + '°C' : '--'}</span>
                <button onclick="event.stopPropagation(); hideSensor('disk:${escapeHtml(diskId)}')" class="text-gray-600 hover:text-red-400 text-[10px] opacity-0 group-hover:opacity-100 transition-opacity px-0.5">×</button>
            </div>
        `;
    }

    if (hasHidden) {
        const totalHidden = hiddenFans.length + hiddenTemps.length + hiddenDisks.length;
        const isHiddenExpanded = !_collapsedNodes.has('local-hidden');
        const arrowChar = isHiddenExpanded ? '▼' : '▶';
        html += `
            <div class="mt-1 border-t border-gray-700/50 pt-1">
                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent cursor-pointer"
                     onclick="toggleNodeGroup('local-hidden')">
                    <span class="text-neon-cyan text-[10px]">${arrowChar}</span>
                    <span class="text-[10px] text-gray-500">Удалённые (${totalHidden})</span>
                    <button onclick="event.stopPropagation(); restoreAllSensors()" class="ml-auto text-[10px] text-gray-600 hover:text-neon-green px-1">↺ все</button>
                </div>
                <div class="node-children ml-4 space-y-px ${isHiddenExpanded ? '' : 'hidden'}" id="node-children-local-hidden">
        `;

        for (const [fanId, fan] of hiddenFans) {
            html += `
                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                    <span class="text-xs opacity-50">🌀</span>
                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(fan.label)}</span>
                    <button onclick="restoreSensor('fan:${escapeHtml(fanId)}')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="Восстановить">↺</button>
                </div>
            `;
        }
        for (const [sensorId, sensor] of hiddenTemps) {
            html += `
                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                    <span class="text-xs opacity-50">🌡</span>
                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(sensor.label)}</span>
                    <button onclick="restoreSensor('temp:${escapeHtml(sensorId)}')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="Восстановить">↺</button>
                </div>
            `;
        }
        for (const [diskId, disk] of hiddenDisks) {
            html += `
                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                    <span class="text-xs opacity-50">💾</span>
                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(disk.label || diskId)}</span>
                    <button onclick="restoreSensor('disk:${escapeHtml(diskId)}')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="Восстановить">↺</button>
                </div>
            `;
        }

        html += `</div></div>`;
    }

    html += `</div></div>`;
    return html;
}

function renderRemoteNodeTree(node) {
    const telemetry = node.telemetry || {};
    const fans = telemetry.fans || {};
    const temps = telemetry.temp_sensors || {};
    const fanCount = Object.keys(fans).length;
    const statusColor = node.status === 'online' ? 'text-neon-green' : 'text-gray-500';
    const statusDot = node.status === 'online' ? 'bg-neon-green' : 'bg-gray-500';

    let html = `
        <div class="node-group" data-node="${escapeHtml(node.node_id)}">
            <div class="flex items-center gap-2 p-2 rounded hover:bg-cyber-accent cursor-pointer node-header"
                 onclick="toggleNodeGroup('${escapeHtml(node.node_id)}')">
                <span class="w-2 h-2 ${statusDot} rounded-full"></span>
                <span class="text-sm font-semibold text-white">🖥 ${escapeHtml(node.name)}</span>
                <span class="ml-auto text-xs ${statusColor}">${node.status}</span>
            </div>
            <div class="node-children ml-4 space-y-0.5 hidden" id="node-children-${escapeHtml(node.node_id)}">
    `;

    for (const [fanId, fan] of Object.entries(fans)) {
        html += `
            <div class="flex items-center gap-2 p-1.5 rounded cursor-pointer hover:bg-cyber-accent"
                 onclick="selectNodeFan('${escapeHtml(node.node_id)}', '${escapeHtml(fanId)}')">
                <span class="text-xs">🌀</span>
                <span class="text-xs text-gray-300 truncate">${escapeHtml(fan.label || fanId)}</span>
                <span class="ml-auto text-xs font-mono text-neon-cyan">${fan.rpm || 0}</span>
            </div>
        `;
    }

    for (const [sensorId, sensor] of Object.entries(temps)) {
        html += `
            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">
                <span class="text-xs">🌡</span>
                <span class="text-xs text-gray-300 truncate">${escapeHtml(sensor.label || sensorId)}</span>
                <span class="ml-auto text-xs font-mono text-neon-green">${sensor.value || 0}°C</span>
            </div>
        `;
    }

    html += `</div></div>`;
    return html;
}

let _collapsedNodes = new Set(JSON.parse(localStorage.getItem('fc_collapsed_nodes') || '[]'));

function toggleNodeGroup(nodeId) {
    const children = document.getElementById(`node-children-${nodeId}`);
    if (children) {
        children.classList.toggle('hidden');
        if (children.classList.contains('hidden')) {
            _collapsedNodes.add(nodeId);
        } else {
            _collapsedNodes.delete(nodeId);
        }
        localStorage.setItem('fc_collapsed_nodes', JSON.stringify([..._collapsedNodes]));
    }
}

function selectFanFromTree(fanId, source) {
    currentFanId = fanId;

    // Show inspector view
    showView('inspector');

    // Update inspector
    if (source === 'local' && currentState && currentState.fans && currentState.fans[fanId]) {
        updateInspector(currentState.fans[fanId]);
    }

    // Rebuild server tree to highlight selected
    buildServerTree();
}

function selectNodeFan(nodeId, fanId) {
    console.log('[FanControl] Select node fan:', nodeId, fanId);
}

// ============================================================================
// DASHBOARD CARDS
// ============================================================================

function showCardPicker() {
    const modal = document.getElementById('card-picker-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    populatePickerSources();
    updatePickerElements();
}

function hideCardPicker() {
    const modal = document.getElementById('card-picker-modal');
    if (modal) modal.classList.add('hidden');
}

function populatePickerSources() {
    const select = document.getElementById('picker-source');
    if (!select) return;
    select.innerHTML = '<option value="local">My Server (local)</option>';
    for (const node of nodesData) {
        select.innerHTML += `<option value="${escapeHtml(node.node_id)}">${escapeHtml(node.name || node.node_id)}</option>`;
    }
}

function updatePickerElements() {
    const type = document.getElementById('picker-type')?.value;
    const source = document.getElementById('picker-source')?.value;
    const container = document.getElementById('picker-elements');
    if (!container) return;

    let elements = [];

    if (source === 'local') {
        if (type === 'fan' && currentState?.fans) {
            elements = Object.entries(currentState.fans).map(([id, f]) => ({ id, label: f.label || id, extra: `${f.rpm || 0} RPM` }));
        } else if (type === 'temperature' && currentState?.temp_sensors) {
            elements = Object.entries(currentState.temp_sensors).map(([id, s]) => ({ id, label: s.label || id, extra: `${s.value || 0}°C` }));
        } else if (type === 'disk' && currentState?.hdd_sensors) {
            elements = Object.entries(currentState.hdd_sensors).map(([id, d]) => ({ id, label: d.label || id, extra: `${d.temp || 0}°C` }));
        } else if (type === 'system') {
            elements = [
                { id: 'max_temp', label: 'Max Temperature', extra: `${currentState?.max_hdd_temp || '--'}°C` },
                { id: 'fans_summary', label: 'Fans Summary', extra: '' },
            ];
        }
    } else {
        const node = nodesData.find(n => n.node_id === source);
        if (node?.telemetry) {
            const tel = node.telemetry;
            if (type === 'fan' && tel.fans) {
                elements = Object.entries(tel.fans).map(([id, f]) => ({ id, label: f.label || id, extra: `${f.rpm || 0} RPM` }));
            } else if (type === 'temperature' && tel.temp_sensors) {
                elements = Object.entries(tel.temp_sensors).map(([id, s]) => ({ id, label: s.label || id, extra: `${s.value || 0}°C` }));
            } else if (type === 'disk' && tel.hdd_sensors) {
                elements = Object.entries(tel.hdd_sensors).map(([id, d]) => ({ id, label: d.label || id, extra: `${d.temp || 0}°C` }));
            }
        }
    }

    container.innerHTML = elements.length > 0
        ? elements.map(el => {
            const cardId = `picker-${source}-${el.id}`;
            const exists = document.querySelector(`[data-card-id="${cardId}"]`);
            return `<label class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">
                <input type="checkbox" value="${escapeHtml(el.id)}" data-label="${escapeHtml(el.label)}" class="picker-checkbox rounded" ${exists ? 'checked disabled' : ''}>
                <span class="text-xs ${exists ? 'text-gray-500 line-through' : 'text-gray-300'}">${escapeHtml(el.label)}</span>
                <span class="ml-auto text-xs text-gray-500">${exists ? 'added' : el.extra}</span>
            </label>`;
        }).join('')
        : '<div class="text-xs text-gray-500 text-center py-4">No elements found</div>';
}

function addSelectedCards() {
    const type = document.getElementById('picker-type')?.value;
    const source = document.getElementById('picker-source')?.value;
    const checkboxes = document.querySelectorAll('.picker-checkbox:checked');
    if (!checkboxes.length) return;

    const saved = getPickerCards();

    checkboxes.forEach(cb => {
        const cardId = `picker-${source}-${cb.value}`;
        if (document.querySelector(`[data-card-id="${cardId}"]`)) return;
        if (saved.some(c => c.id === cardId)) return;

        const label = cb.dataset.label || cb.value;
        const colSpan = 3;
        const pos = findNextPosition(saved, colSpan);
        const cardData = { id: cardId, type, source, sourceId: cb.value, label, col: pos.col, row: pos.row, colSpan };
        renderPickerCard(cardData);
        saved.push(cardData);
    });

    setPickerCards(saved);
    document.getElementById('dashboard-empty')?.classList.add('hidden');
    hideCardPicker();
    startPickerLiveUpdate();
}

function renderPickerCard(card) {
    const { id, type, source, sourceId, label } = card;
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;

    let icon = '📊';
    let colorClass = 'text-neon-cyan';
    let valueHtml = '';

    if (type === 'fan') {
        icon = '🌀';
        colorClass = 'text-neon-cyan';
        valueHtml = `<div class="text-2xl font-bold font-mono ${colorClass}" data-fan-id="${sourceId}" data-source="${source}">--</div>
            <div class="text-xs text-gray-500 mt-1">RPM</div>`;
    } else if (type === 'temperature') {
        icon = '🌡';
        colorClass = 'text-neon-green';
        valueHtml = `<div class="text-2xl font-bold font-mono ${colorClass}" data-temp-id="${sourceId}" data-source="${source}">--</div>
            <div class="text-xs text-gray-500 mt-1">°C</div>`;
    } else if (type === 'disk') {
        icon = '💾';
        colorClass = 'text-neon-purple';
        valueHtml = `<div class="text-2xl font-bold font-mono ${colorClass}" data-disk-id="${sourceId}" data-source="${source}">--</div>
            <div class="text-xs text-gray-500 mt-1">°C</div>`;
    } else {
        valueHtml = `<div class="text-2xl font-bold font-mono text-neon-cyan">--</div>`;
    }

    const configBtn = type === 'fan'
        ? `<button onclick="event.stopPropagation(); showCardConfig('${id}')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Configure">⚙</button>`
        : type === 'disk'
        ? `<button onclick="event.stopPropagation(); showSmartModal('${id}')" class="text-gray-600 hover:text-neon-purple text-xs transition-colors" title="SMART">⚙</button>`
        : '';
    const editBtn = `<button onclick="event.stopPropagation(); showCardEdit('${id}')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Edit name">✎</button>`;
    const removeBtn = `<button onclick="event.stopPropagation(); removePickerCard('${id}')" class="text-gray-600 hover:text-red-400 text-xs transition-colors">×</button>`;

    const el = document.createElement('div');
    el.className = 'bg-cyber-card border border-cyber-accent rounded-xl p-4 transition-all hover:border-neon-cyan/50 hover:shadow-neon-cyan/10 hover:shadow-lg cursor-grab active:cursor-grabbing';
    el.setAttribute('data-card-id', id);
    el.innerHTML = `
        <div class="flex items-center justify-between mb-3">
            <div class="flex items-center gap-2">
                <span class="text-gray-600 text-xs select-none">⠿</span>
                <span class="text-lg">${icon}</span>
                <span class="text-sm text-gray-300 font-medium truncate">${escapeHtml(label)}</span>
            </div>
            <div class="flex items-center gap-1">
                ${configBtn}${editBtn}${removeBtn}
            </div>
        </div>
        ${valueHtml}
        <div class="card-details"></div>
        <div class="card-resize-handle"></div>`;

    el.addEventListener('mousedown', onCardMouseDown);

    if (!card.col || !card.row) {
        const saved = getPickerCards().filter(c => c.id !== card.id);
        const pos = findNextPosition(saved, card.colSpan || 3);
        card.col = pos.col;
        card.row = pos.row;
    }
    el.style.gridColumn = `${card.col} / span ${card.colSpan || 3}`;
    el.style.gridRow = `${card.row} / span ${card.rowSpan || 1}`;
    el.style.position = 'relative';
    el.style.alignSelf = 'stretch';

    canvas.appendChild(el);

    const resizeHandle = el.querySelector('.card-resize-handle');
    if (resizeHandle) {
        resizeHandle.addEventListener('mousedown', (e) => onCardResizeStart(e, id));
    }

    if (type === 'disk') {
        el.addEventListener('click', (e) => {
            if (_cardDragOccurred || e.target.closest('button')) return;
            showSmartModal(id);
        });
    }

    updateCardDetails(id);
}

let _cardDragOccurred = false;
let _dropTarget = null;

let _cardResizing = null;
let _cardResizeStartX = 0;
let _cardResizeStartY = 0;
let _cardResizeStartW = 0;
let _cardResizeStartH = 0;

function onCardResizeStart(e, cardId) {
    e.preventDefault();
    e.stopPropagation();
    const el = document.querySelector(`[data-card-id="${cardId}"]`);
    if (!el) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);

    _cardResizing = { cardId, el, col: card?.col, row: card?.row };
    _cardResizeStartX = e.clientX;
    _cardResizeStartY = e.clientY;
    _cardResizeStartW = el.offsetWidth;
    _cardResizeStartH = el.offsetHeight;

    el.setAttribute('draggable', 'false');
    document.body.style.cursor = 'se-resize';
    document.body.style.userSelect = 'none';

    document.addEventListener('mousemove', onCardResizeMove);
    document.addEventListener('mouseup', onCardResizeEnd);
}

function getCanvasCols() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return 12;
    const style = getComputedStyle(canvas);
    return style.gridTemplateColumns.split(' ').length || 12;
}

function updateCanvasColumns() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;
    const w = window.innerWidth;
    let cols = 4;
    if (w >= 1280) cols = 12;
    else if (w >= 1024) cols = 8;
    else if (w >= 640) cols = 6;
    canvas.style.display = 'grid';
    canvas.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    canvas.style.gridAutoRows = '100px';
    canvas.style.gap = '8px';
    canvas.style.position = 'relative';
}

function onCardResizeMove(e) {
    if (!_cardResizing) return;
    const el = _cardResizing.el;
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;

    const dx = e.clientX - _cardResizeStartX;
    const dy = e.clientY - _cardResizeStartY;
    const cols = getCanvasCols();
    const gap = 8;
    const padL = parseInt(getComputedStyle(canvas).paddingLeft) || 16;
    const padR = parseInt(getComputedStyle(canvas).paddingRight) || 16;
    const contentW = canvas.offsetWidth - padL - padR;
    const colWidth = (contentW - (cols - 1) * gap) / cols;
    const rowHeight = 100;
    const rowStep = rowHeight + gap;

    const newW = _cardResizeStartW + dx;
    const newH = _cardResizeStartH + dy;
    const newColSpan = Math.max(1, Math.min(cols, Math.round(newW / (colWidth + gap))));
    const newRowSpan = Math.max(1, Math.min(8, Math.round(newH / rowStep)));

    el.style.gridColumn = `${_cardResizing.col || 'auto'} / span ${newColSpan}`;
    el.style.gridRow = `${_cardResizing.row || 'auto'} / span ${newRowSpan}`;
    el._resizeColSpan = newColSpan;
    el._resizeRowSpan = newRowSpan;
}

function onCardResizeEnd(e) {
    if (!_cardResizing) return;
    const el = _cardResizing.el;
    const cardId = _cardResizing.cardId;

    let colSpan = el._resizeColSpan || 3;
    let rowSpan = el._resizeRowSpan || 1;

    document.body.style.cursor = '';
    document.body.style.userSelect = '';

    document.removeEventListener('mousemove', onCardResizeMove);
    document.removeEventListener('mouseup', onCardResizeEnd);

    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (card) {
        if (isCellOccupied(card.col, card.row, colSpan, rowSpan, cardId)) {
            const free = findFreePosition(saved, colSpan, rowSpan, cardId);
            card.col = free.col;
            card.row = free.row;
        }
        card.colSpan = colSpan;
        card.rowSpan = rowSpan;
        el.style.gridColumn = `${card.col} / span ${colSpan}`;
        el.style.gridRow = `${card.row} / span ${rowSpan}`;
        setPickerCards(saved);
    }

    _cardResizing = null;
    updateCanvasMinHeight();
}

function getGridCell(canvas, x, y) {
    const rect = canvas.getBoundingClientRect();
    const cs = getComputedStyle(canvas);
    const padL = parseFloat(cs.paddingLeft) || 16;
    const padT = parseFloat(cs.paddingTop) || 16;
    const padR = parseFloat(cs.paddingRight) || 16;
    const cols = getCanvasCols();
    const gap = 8;
    const contentW = rect.width - padL - padR;
    const colW = (contentW - (cols - 1) * gap) / cols;
    const rowStep = 100 + gap;
    const offset = x - rect.left - padL;
    const col = Math.max(1, Math.min(cols, Math.floor(offset / (colW + gap)) + 1));
    const row = Math.max(1, Math.floor((y - rect.top - padT) / rowStep) + 1);
    return { col, row };
}

function findNextPosition(savedCards, colSpan) {
    const cols = getCanvasCols();
    const occupied = new Set();
    for (const c of savedCards) {
        const cs = c.col || 1;
        const rs = c.row || 1;
        const sp = c.colSpan || 3;
        const sr = c.rowSpan || 1;
        for (let r = rs; r < rs + sr; r++) {
            for (let c2 = cs; c2 < cs + sp; c2++) {
                occupied.add(`${c2},${r}`);
            }
        }
    }
    for (let row = 1; row <= 20; row++) {
        for (let col = 1; col <= cols - colSpan + 1; col++) {
            let fits = true;
            for (let c2 = col; c2 < col + colSpan && fits; c2++) {
                if (occupied.has(`${c2},${row}`)) fits = false;
            }
            if (fits) return { col, row };
        }
    }
    return { col: 1, row: 1 };
}

let _cardMouseDown = null;
let _cardDragClone = null;

function onCardMouseDown(e) {
    if (e.target.closest('button') || e.target.closest('input') || e.target.closest('.card-resize-handle')) return;
    if (e.button !== 0) return;
    const cardEl = e.target.closest('[data-card-id]');
    if (!cardEl || cardEl.closest('[data-group-id]')) return;
    e.preventDefault();

    const cardId = cardEl.dataset.cardId;
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    const rect = cardEl.getBoundingClientRect();
    const offsetX = e.clientX - rect.left;
    const offsetY = e.clientY - rect.top;

    _cardMouseDown = {
        cardId, cardEl, card,
        startX: e.clientX, startY: e.clientY,
        offsetX, offsetY, dragging: false
    };

    console.log(`[DOWN] card=${cardId} pos(col=${card.col},row=${card.row}) span(col=${card.colSpan||3},row=${card.rowSpan||1}) offset(X=${Math.round(offsetX)},Y=${Math.round(offsetY)}) cardRect(left=${Math.round(rect.left)},top=${Math.round(rect.top)},w=${Math.round(rect.width)},h=${Math.round(rect.height)})`);

    document.addEventListener('mousemove', onCardMouseMove);
    document.addEventListener('mouseup', onCardMouseUp);
}

function onCardMouseMove(e) {
    if (!_cardMouseDown) return;
    const dx = Math.abs(e.clientX - _cardMouseDown.startX);
    const dy = Math.abs(e.clientY - _cardMouseDown.startY);
    if (!_cardMouseDown.dragging && (dx < 4 && dy < 4)) return;

    if (!_cardMouseDown.dragging) {
        _cardMouseDown.dragging = true;
        _cardMouseDown.cardEl.classList.add('opacity-40');
        _cardDragOccurred = true;

        _cardDragClone = _cardMouseDown.cardEl.cloneNode(true);
        _cardDragClone.classList.remove('opacity-40');
        _cardDragClone.style.cssText = `
            position:fixed;z-index:10000;pointer-events:none;
            width:${_cardMouseDown.cardEl.offsetWidth}px;
            opacity:0.85;
            box-shadow:0 8px 32px rgba(0,0,0,0.4);
            transition:none;
        `;
        document.body.appendChild(_cardDragClone);
    }

    const cloneW = _cardMouseDown.cardEl.offsetWidth;
    const cloneH = _cardMouseDown.cardEl.offsetHeight;
    _cardDragClone.style.left = (e.clientX - _cardMouseDown.offsetX) + 'px';
    _cardDragClone.style.top = (e.clientY - _cardMouseDown.offsetY) + 'px';

    const canvas = document.getElementById('dashboard-canvas');
    const cell = getGridCell(canvas, e.clientX, e.clientY);
    const card = _cardMouseDown.card;
    const colSpan = card.colSpan || 3;
    const rowSpan = card.rowSpan || 1;
    const cols = getCanvasCols();

    const cardCol = card.col || 1;
    const cardRow = card.row || 1;
    const inCardArea = cell.col >= cardCol && cell.col < cardCol + colSpan
                    && cell.row >= cardRow && cell.row < cardRow + rowSpan;

    let newCol = inCardArea ? cardCol : cell.col;
    let newRow = inCardArea ? cardRow : cell.row;
    newCol = Math.max(1, Math.min(cols - colSpan + 1, newCol));
    newRow = Math.max(1, newRow);
    const occupied = isCellOccupied(newCol, newRow, colSpan, rowSpan, card.id);

    if (!_cardDropPreview) {
        _cardDropPreview = document.createElement('div');
        _cardDropPreview.style.cssText = 'position:absolute;pointer-events:none;z-index:10;border:2px dashed;border-radius:12px;transition:all 0.1s ease;';
    }

    const padLeft = parseInt(getComputedStyle(canvas).paddingLeft) || 16;
    const padTop = parseInt(getComputedStyle(canvas).paddingTop) || 16;
    const padRight = parseInt(getComputedStyle(canvas).paddingRight) || 16;
    const contentW = canvas.offsetWidth - padLeft - padRight;
    const gap = 8;
    const colW = (contentW - (cols - 1) * gap) / cols;
    const rowH = 100;
    _cardDropPreview.style.left = (padLeft + (newCol - 1) * (colW + gap)) + 'px';
    _cardDropPreview.style.top = (padTop + (newRow - 1) * (rowH + gap)) + 'px';
    _cardDropPreview.style.width = (colSpan * colW + (colSpan - 1) * gap) + 'px';
    _cardDropPreview.style.height = (rowSpan * (rowH + gap) - gap) + 'px';
    _cardDropPreview.style.borderColor = occupied ? '#ef4444' : '#06b6d4';
    _cardDropPreview.style.background = occupied ? 'rgba(239,68,68,0.08)' : 'rgba(6,182,212,0.08)';
    _cardDropPreview.style.display = 'block';

    if (!_cardDropPreview.parentElement) {
        canvas.appendChild(_cardDropPreview);
    }

    _dropTarget = { col: newCol, row: newRow, occupied };

    console.log(`[MOVE] card=${card.id} stored(col=${cardCol},row=${cardRow}) span(${colSpan}x${rowSpan}) cursor(col=${cell.col},row=${cell.row}) inArea=${inCardArea} → new(col=${newCol},row=${newRow}) occ=${occupied}`);

    const groupEl = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-group-id]');
    document.querySelectorAll('[data-group-id].drag-hover').forEach(el => el.classList.remove('drag-hover'));
    if (groupEl && !groupEl.contains(_cardMouseDown.cardEl)) {
        groupEl.classList.add('drag-hover');
        groupEl.style.borderColor = '#a855f7';
        groupEl.style.background = 'rgba(168,85,247,0.1)';
    }
}

function onCardMouseUp(e) {
    document.removeEventListener('mousemove', onCardMouseMove);
    document.removeEventListener('mouseup', onCardMouseUp);

    if (_cardDragClone) {
        _cardDragClone.remove();
        _cardDragClone = null;
    }
    if (_cardDropPreview) {
        _cardDropPreview.style.display = 'none';
    }

    document.querySelectorAll('[data-group-id].drag-hover').forEach(el => {
        el.classList.remove('drag-hover');
        el.style.borderColor = '';
        el.style.background = '';
    });

    if (!_cardMouseDown) return;

    const { cardEl, card, dragging } = _cardMouseDown;
    cardEl.classList.remove('opacity-40');

    if (dragging && _dropTarget) {
        const groupEl = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-group-id]');
        if (groupEl && !groupEl.contains(cardEl)) {
            const groupCards = groupEl.querySelector('.group-cards');
            if (groupCards) {
                const saved = getPickerCards();
                const cardData = saved.find(c => c.id === card.id);
                if (cardData) {
                    cardData.groupId = groupEl.dataset.groupId;
                    setPickerCards(saved);
                }
                groupCards.appendChild(cardEl);
                cardEl.classList.remove('cursor-grab');
                cardEl.classList.add('cursor-default');
            }
        } else {
            const saved = getPickerCards();
            const cardData = saved.find(c => c.id === card.id);
            if (cardData) {
                const oldCol = cardData.col, oldRow = cardData.row;
                let newCol = _dropTarget.col;
                let newRow = _dropTarget.row;
                const colSp = cardData.colSpan || 3;
                const rowSp = cardData.rowSpan || 1;
                const occupied = isCellOccupied(newCol, newRow, colSp, rowSp, card.id);
                if (occupied) {
                    const free = findFreePosition(saved, colSp, rowSp, card.id);
                    newCol = free.col;
                    newRow = free.row;
                }
                console.log(`[DROP] card=${card.id} from(col=${oldCol},row=${oldRow}) target(col=${_dropTarget.col},row=${_dropTarget.row}) occupied=${occupied} → placed(col=${newCol},row=${newRow})`);
                cardData.col = newCol;
                cardData.row = newRow;
                cardEl.style.gridColumn = `${newCol} / span ${colSp}`;
                cardEl.style.gridRow = `${newRow} / span ${rowSp}`;
                setPickerCards(saved);
                updateCanvasMinHeight();
            }
        }
    }

    _cardMouseDown = null;
    _dropTarget = null;
    setTimeout(() => { _cardDragOccurred = false; }, 50);
}

function isCellOccupied(col, row, colSpan, rowSpan, excludeCardId) {
    const saved = getPickerCards();
    for (const c of saved) {
        if (c.id === excludeCardId || !c.col || !c.row) continue;
        const cs = c.col, rs = c.row;
        const ce = cs + (c.colSpan || 3) - 1;
        const re = rs + (c.rowSpan || 1) - 1;
        const ne = col + colSpan - 1;
        const nr = row + rowSpan - 1;
        if (col <= ce && ne >= cs && row <= re && nr >= rs) return true;
    }
    const canvas = document.getElementById('dashboard-canvas');
    if (canvas) {
        const cols = getCanvasCols();
        const cs2 = getComputedStyle(canvas);
        const padL = parseFloat(cs2.paddingLeft) || 16;
        const padT = parseFloat(cs2.paddingTop) || 16;
        const padR = parseFloat(cs2.paddingRight) || 16;
        const contentW = canvas.offsetWidth - padL - padR;
        const gap = 8;
        const colW = (contentW - (cols - 1) * gap) / cols;
        const rowH = 100;
        const ne = col + colSpan - 1;
        const nr = row + rowSpan - 1;
        for (const gEl of canvas.querySelectorAll('[data-group-id]')) {
            const rect = gEl.getBoundingClientRect();
            const cRect = canvas.getBoundingClientRect();
            const gColStart = Math.max(1, Math.round((rect.left - cRect.left - padL) / (colW + gap)) + 1);
            const gColEnd = Math.max(gColStart, Math.round((rect.right - cRect.left - padL) / (colW + gap)));
            const gRowStart = Math.max(1, Math.round((rect.top - cRect.top - padT) / (rowH + gap)) + 1);
            const gRowEnd = Math.max(gRowStart, Math.round((rect.bottom - cRect.top - padT) / (rowH + gap)));
            if (col <= gColEnd && ne >= gColStart && row <= gRowEnd && nr >= gRowStart) return true;
        }
    }
    return false;
}

function findFreePosition(savedCards, colSpan, rowSpan, excludeCardId) {
    const cols = getCanvasCols();
    if (colSpan > cols) colSpan = cols;
    const occupied = new Set();
    for (const c of savedCards) {
        if (c.id === excludeCardId || !c.col || !c.row) continue;
        const cs = c.col, rs = c.row;
        const sp = c.colSpan || 3, sr = c.rowSpan || 1;
        for (let r = rs; r < rs + sr; r++) {
            for (let c2 = cs; c2 < cs + sp; c2++) {
                occupied.add(`${c2},${r}`);
            }
        }
    }
    const canvas = document.getElementById('dashboard-canvas');
    if (canvas) {
        const cs2 = getComputedStyle(canvas);
        const padL = parseFloat(cs2.paddingLeft) || 16;
        const padT = parseFloat(cs2.paddingTop) || 16;
        const padR = parseFloat(cs2.paddingRight) || 16;
        const contentW = canvas.offsetWidth - padL - padR;
        const gap = 8;
        const colW = (contentW - (cols - 1) * gap) / cols;
        const rowH = 100;
        for (const gEl of canvas.querySelectorAll('[data-group-id]')) {
            const rect = gEl.getBoundingClientRect();
            const cRect = canvas.getBoundingClientRect();
            const gColStart = Math.max(1, Math.round((rect.left - cRect.left - padL) / (colW + gap)) + 1);
            const gColEnd = Math.max(gColStart, Math.round((rect.right - cRect.left - padL) / (colW + gap)));
            const gRowStart = Math.max(1, Math.round((rect.top - cRect.top - padT) / (rowH + gap)) + 1);
            const gRowEnd = Math.max(gRowStart, Math.round((rect.bottom - cRect.top - padT) / (rowH + gap)));
            for (let r = gRowStart; r <= gRowEnd; r++) {
                for (let c2 = gColStart; c2 <= gColEnd; c2++) {
                    occupied.add(`${c2},${r}`);
                }
            }
        }
    }
    for (let row = 1; row <= 50; row++) {
        for (let col = 1; col <= cols - colSpan + 1; col++) {
            let fits = true;
            for (let r = row; r < row + rowSpan && fits; r++) {
                for (let c = col; c < col + colSpan && fits; c++) {
                    if (occupied.has(`${c},${r}`)) fits = false;
                }
            }
            if (fits) return { col, row };
        }
    }
    return { col: 1, row: 1 };
}

let _cardDropPreview = null;

function getDragAfterElement(container, x, y) {
    const cards = [...container.querySelectorAll('[data-card-id]:not(.opacity-40), [data-group-id]:not(.opacity-40)')];
    let closest = null;
    let closestDist = Infinity;
    for (const child of cards) {
        const box = child.getBoundingClientRect();
        const cx = box.left + box.width / 2;
        const cy = box.top + box.height / 2;
        const dist = Math.hypot(x - cx, y - cy);
        if (dist < closestDist) {
            closestDist = dist;
            closest = child;
        }
    }
    if (!closest) return null;
    const box = closest.getBoundingClientRect();
    const isAfter = x > box.left + box.width / 2 || y > box.top + box.height / 2;
    return isAfter ? closest.nextElementSibling : closest;
}
function saveCardOrder() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;
    const ordered = [...canvas.querySelectorAll('[data-card-id]')].map(el => el.dataset.cardId);
    const saved = getPickerCards();
    const orderedCards = ordered.map(id => saved.find(c => c.id === id)).filter(Boolean);
    setPickerCards(orderedCards);
}

function removePickerCard(cardId) {
    const el = document.querySelector(`[data-card-id="${cardId}"]`);
    if (el) el.remove();
    const saved = getPickerCards().filter(c => c.id !== cardId);
    setPickerCards(saved);
    if (!saved.length) document.getElementById('dashboard-empty')?.classList.remove('hidden');
    updateCanvasMinHeight();
}

let _editingCardId = null;

function showCardEdit(cardId) {
    _editingCardId = cardId;
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    const modal = document.getElementById('card-edit-modal');
    const labelInput = document.getElementById('card-edit-label');

    labelInput.value = card.label || '';

    modal.classList.remove('hidden');
    labelInput.focus();
}

function hideCardEdit() {
    const modal = document.getElementById('card-edit-modal');
    if (modal) modal.classList.add('hidden');
    _editingCardId = null;
}

function saveCardEdit() {
    if (!_editingCardId) return;

    const label = document.getElementById('card-edit-label').value.trim();
    if (!label) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === _editingCardId);
    if (!card) return;

    card.label = label;
    setPickerCards(saved);

    const cardEl = document.querySelector(`[data-card-id="${_editingCardId}"]`);
    if (cardEl) {
        const labelEl = cardEl.querySelector('.text-sm.text-gray-300');
        if (labelEl) labelEl.textContent = label;
    }

    hideCardEdit();
}

let _configuringCardId = null;

function showCardConfig(cardId) {
    _configuringCardId = cardId;
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card || card.type !== 'fan') return;

    const modal = document.getElementById('card-config-modal');
    const container = document.getElementById('card-config-options');

    const fanData = getFanData(card.source, card.sourceId);
    if (!fanData) return;

    const options = [
        { key: 'rpm', label: 'RPM', checked: card.showRpm !== false },
        { key: 'mode', label: 'Mode', checked: card.showMode === true },
        { key: 'sensors', label: 'Sensors', checked: card.showSensors === true },
        { key: 'target', label: 'Target Temp', checked: card.showTarget === true },
    ];

    container.innerHTML = options.map(opt => `
        <label class="flex items-center gap-3 p-2 rounded hover:bg-cyber-accent cursor-pointer">
            <input type="checkbox" data-option="${opt.key}" ${opt.checked ? 'checked' : ''}
                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan">
            <span class="text-sm text-gray-300">${opt.label}</span>
        </label>
    `).join('');

    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', () => toggleCardOption(cardId, cb.dataset.option, cb.checked));
    });

    modal.classList.remove('hidden');
}

function hideCardConfig() {
    const modal = document.getElementById('card-config-modal');
    if (modal) modal.classList.add('hidden');
    _configuringCardId = null;
}

let _smartModalCardId = null;
let _smartModalDiskId = null;
let _smartAttributes = [];
let _smartAttrType = 'sata';
let _smartCache = {};

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

function showSmartModal(cardId) {
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    _smartModalCardId = cardId;
    _smartModalDiskId = card.sourceId;
    const disk = currentState?.hdd_sensors?.[card.sourceId];
    const title = document.getElementById('smart-modal-title');
    if (title && disk) {
        title.textContent = `SMART — ${disk.label || disk.dev_name}`;
    }
    document.getElementById('smart-modal')?.classList.remove('hidden');
    refreshSmartData();
}

function hideSmartModal() {
    document.getElementById('smart-modal')?.classList.add('hidden');
    _smartModalCardId = null;
    _smartModalDiskId = null;
}

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

function renderSmartAttributes() {
    const container = document.getElementById('smart-attributes-container');
    if (!container) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === _smartModalCardId);
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

    const saved = getPickerCards();
    const card = saved.find(c => c.id === _smartModalCardId);
    const smartUnits = card?.smartUnits || {};

    container.innerHTML = _smartAttributes.map(attr => {
        const statusColor = attr.status === 'critical' ? 'text-red-400' :
                           attr.status === 'warning' ? 'text-yellow-400' : 'text-neon-green';
        const statusBg = attr.status === 'critical' ? 'bg-red-500/10' :
                        attr.status === 'warning' ? 'bg-yellow-500/10' : 'bg-green-500/10';
        const critBadge = attr.criticality === 'critical' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">КРИТИЧНЫЙ</span>' :
                         attr.criticality === 'important' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">ВАЖНЫЙ</span>' : '';
        const checked = selectedIds.includes(String(attr.id)) ? 'checked' : '';

        let unitHtml = '';
        if (attr.unit === 'bytes') {
            const currentUnit = smartUnits[attr.id] || 'raw';
            unitHtml = `
                <select data-smart-unit="${attr.id}" onchange="onSmartUnitChange(${attr.id}, this.value)"
                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">
                    <option value="raw" ${currentUnit === 'raw' ? 'selected' : ''}>Raw</option>
                    <option value="bytes" ${currentUnit === 'bytes' ? 'selected' : ''}>Байты</option>
                    <option value="kb" ${currentUnit === 'kb' ? 'selected' : ''}>КБ</option>
                    <option value="mb" ${currentUnit === 'mb' ? 'selected' : ''}>МБ</option>
                    <option value="gb" ${currentUnit === 'gb' ? 'selected' : ''}>ГБ</option>
                    <option value="tb" ${currentUnit === 'tb' ? 'selected' : ''}>ТБ</option>
                </select>`;
        } else if (attr.unit === 'hours') {
            const currentUnit = smartUnits[attr.id] || 'raw';
            unitHtml = `
                <select data-smart-unit="${attr.id}" onchange="onSmartUnitChange(${attr.id}, this.value)"
                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">
                    <option value="raw" ${currentUnit === 'raw' ? 'selected' : ''}>Часы</option>
                    <option value="days" ${currentUnit === 'days' ? 'selected' : ''}>Дни</option>
                    <option value="months" ${currentUnit === 'months' ? 'selected' : ''}>Месяцы</option>
                </select>`;
        }

        let displayValue = attr.raw;
        if (attr.unit === 'bytes' && attr.unit_divisor) {
            const unit = smartUnits[attr.id] || 'raw';
            if (unit !== 'raw') {
                displayValue = formatBytes(parseInt(attr.raw_num || attr.raw) * attr.unit_divisor, unit);
            }
        } else if (attr.unit === 'hours') {
            const unit = smartUnits[attr.id] || 'raw';
            if (unit === 'days') {
                displayValue = (parseInt(attr.raw || '0') / 24).toFixed(1) + ' дн';
            } else if (unit === 'months') {
                displayValue = (parseInt(attr.raw || '0') / 720).toFixed(1) + ' мес';
            }
        }

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
                    ${unitHtml}
                </div>
                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>
            </div>
            <div class="text-right shrink-0">
                <div class="text-sm font-mono ${statusColor}">${attr.value}</div>
                <div class="text-[10px] text-gray-500">worst:${attr.worst} thr:${attr.threshold}</div>
            </div>
            <div class="text-right shrink-0 w-20">
                <div class="text-xs text-gray-400 font-mono">${displayValue}</div>
            </div>
        </div>`;
    }).join('');
}

function formatBytes(bytes, unit) {
    if (isNaN(bytes) || bytes === 0) return '0';
    const units = { 'kb': 1024, 'mb': 1024*1024, 'gb': 1024*1024*1024, 'tb': 1024*1024*1024*1024 };
    const divisor = units[unit] || 1;
    const result = bytes / divisor;
    if (result >= 1000) return result.toFixed(0);
    if (result >= 100) return result.toFixed(1);
    return result.toFixed(2);
}

function onSmartUnitChange(attrId, unit) {
    if (!_smartModalCardId) return;
    const saved = getPickerCards();
    const card = saved.find(c => c.id === _smartModalCardId);
    if (!card) return;

    if (!card.smartUnits) card.smartUnits = {};
    card.smartUnits[attrId] = unit;
    setPickerCards(saved);
    renderSmartAttributes();
}

function renderNvmeAttributes(container, selectedIds) {
    const attrs = _smartAttributes;
    if (!Object.keys(attrs).length) {
        container.innerHTML = '<div class="text-center text-gray-400 py-4">Нет NVMe атрибутов</div>';
        return;
    }

    const saved = getPickerCards();
    const card = saved.find(c => c.id === _smartModalCardId);
    const smartUnits = card?.smartUnits || {};

    container.innerHTML = Object.entries(attrs).map(([key, attr]) => {
        const statusColor = attr.criticality === 'critical' ? 'text-red-400' :
                           attr.criticality === 'important' ? 'text-yellow-400' : 'text-neon-green';
        const critBadge = attr.criticality === 'critical' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">КРИТИЧНЫЙ</span>' :
                         attr.criticality === 'important' ? '<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">ВАЖНЫЙ</span>' : '';
        const checked = selectedIds.includes(key) ? 'checked' : '';

        let unitHtml = '';
        let displayValue = attr.value;

        if (attr.unit === 'nvme_blocks') {
            const currentUnit = smartUnits[key] || 'raw';
            unitHtml = `
                <select data-smart-unit="${key}" onchange="onSmartUnitChange('${key}', this.value)"
                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">
                    <option value="raw" ${currentUnit === 'raw' ? 'selected' : ''}>Raw</option>
                    <option value="bytes" ${currentUnit === 'bytes' ? 'selected' : ''}>Байты</option>
                    <option value="kb" ${currentUnit === 'kb' ? 'selected' : ''}>КБ</option>
                    <option value="mb" ${currentUnit === 'mb' ? 'selected' : ''}>МБ</option>
                    <option value="gb" ${currentUnit === 'gb' ? 'selected' : ''}>ГБ</option>
                    <option value="tb" ${currentUnit === 'tb' ? 'selected' : ''}>ТБ</option>
                </select>`;
            if (currentUnit !== 'raw' && attr.unit_divisor) {
                displayValue = formatBytes(attr.value * attr.unit_divisor, currentUnit);
            }
        } else if (attr.unit === 'hours') {
            const currentUnit = smartUnits[key] || 'raw';
            unitHtml = `
                <select data-smart-unit="${key}" onchange="onSmartUnitChange('${key}', this.value)"
                    class="text-[10px] bg-cyber-bg border border-gray-600 rounded px-1 py-0.5 text-gray-300 ml-1">
                    <option value="raw" ${currentUnit === 'raw' ? 'selected' : ''}>Часы</option>
                    <option value="days" ${currentUnit === 'days' ? 'selected' : ''}>Дни</option>
                    <option value="months" ${currentUnit === 'months' ? 'selected' : ''}>Месяцы</option>
                </select>`;
            if (currentUnit === 'days') {
                displayValue = (parseInt(attr.value || '0') / 24).toFixed(1);
            } else if (currentUnit === 'months') {
                displayValue = (parseInt(attr.value || '0') / 720).toFixed(1);
            }
        }

        let suffix = '';
        if (key === 'temperature') suffix = '°C';
        else if (key === 'percentage_used' || key === 'available_spare' || key === 'available_spare_threshold') suffix = '%';
        else if (key === 'controller_busy_time' || key === 'warning_temp_time' || key === 'critical_comp_time') suffix = ' мин';
        else if (attr.unit === 'hours' && (smartUnits[key] || 'raw') === 'days') suffix = ' дн';
        else if (attr.unit === 'hours' && (smartUnits[key] || 'raw') === 'months') suffix = ' мес';

        return `
        <div class="flex items-center gap-3 p-2 rounded bg-green-500/5 hover:bg-white/5 transition-colors"
             title="${escapeHtml(attr.tooltip)}">
            <input type="checkbox" data-smart-key="${key}" ${checked}
                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan shrink-0">
            <div class="flex-1 min-w-0">
                <div class="flex items-center">
                    <span class="text-sm text-gray-200 truncate">${escapeHtml(attr.description)}</span>
                    ${critBadge}
                    ${unitHtml}
                </div>
                <div class="text-[10px] text-gray-500 truncate">${escapeHtml(attr.tooltip)}</div>
            </div>
            <div class="text-right shrink-0">
                <div class="text-sm font-mono ${statusColor}">${displayValue}${suffix}</div>
            </div>
        </div>`;
    }).join('');
}

function saveSmartSelection() {
    if (!_smartModalCardId) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === _smartModalCardId);
    if (!card) return;

    const checkboxes = document.querySelectorAll('#smart-attributes-container input[type="checkbox"]');
    const selected = [];
    checkboxes.forEach(cb => {
        if (cb.checked) {
            selected.push(cb.dataset.smartId || cb.dataset.smartKey);
        }
    });

    const unitSelects = document.querySelectorAll('#smart-attributes-container select[data-smart-unit]');
    const units = {};
    unitSelects.forEach(sel => {
        const attrId = sel.dataset.smartUnit;
        units[attrId] = sel.value;
    });

    card.smartAttributes = selected;
    card.smartUnits = units;
    setPickerCards(saved);
    updateCardDetails(_smartModalCardId);
    hideSmartModal();
    saveDashboardToServer();
}

function toggleCardOption(cardId, option, enabled) {
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    if (option === 'rpm') card.showRpm = enabled;
    else if (option === 'mode') card.showMode = enabled;
    else if (option === 'sensors') card.showSensors = enabled;
    else if (option === 'target') card.showTarget = enabled;

    setPickerCards(saved);
    updateCardDetails(cardId);
}

function getFanData(source, sourceId) {
    if (source === 'local') return currentState?.fans?.[sourceId] || null;
    const node = nodesData.find(n => n.node_id === source);
    return node?.telemetry?.fans?.[sourceId] || null;
}

function getSensorLabel(sensorId) {
    if (sensorId.startsWith('hdd:')) {
        const id = sensorId.slice(4);
        return currentState?.hdd_sensors?.[id]?.label || id;
    } else if (sensorId.startsWith('temp:')) {
        const id = sensorId.slice(5);
        return currentState?.temp_sensors?.[id]?.label || id;
    }
    return sensorId;
}

function updateCardDetails(cardId) {
    const cardEl = document.querySelector(`[data-card-id="${cardId}"]`);
    if (!cardEl) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    const detailsEl = cardEl.querySelector('.card-details');
    if (!detailsEl) return;

    if (card.type === 'disk') {
        updateDiskCardDetails(card, detailsEl);
        return;
    }
    if (card.type !== 'fan') {
        detailsEl.innerHTML = '';
        return;
    }

    const fanData = getFanData(card.source, card.sourceId);
    if (!fanData) {
        detailsEl.innerHTML = '';
        return;
    }

    let html = '';

    if (card.showMode) {
        const mode = fanData.mode || 'manual';
        const modeClass = mode === 'auto' ? 'text-neon-green' : 'text-neon-cyan';
        const modeLabel = mode === 'auto' ? 'AUTO' : 'MANUAL';
        html += `<div class="text-xs ${modeClass} mt-1">${modeLabel}</div>`;
    }

    if (card.showTarget && fanData.mode === 'auto') {
        html += `<div class="text-xs text-gray-500 mt-1">Target: ${fanData.target_temp || '--'}°C</div>`;
    }

    if (card.showSensors && fanData.sensors && fanData.sensors.length > 0) {
        const sensorLabels = fanData.sensors.map(s => getSensorLabel(s)).join(', ');
        html += `<div class="text-xs text-gray-500 mt-1 truncate" title="${escapeHtml(sensorLabels)}">Sensors: ${escapeHtml(sensorLabels)}</div>`;
    }

    detailsEl.innerHTML = html;
}

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
    const smartUnits = card.smartUnits || {};

    for (const attrKey of card.smartAttributes) {
        const attrId = parseInt(attrKey);
        if (!isNaN(attrId)) {
            const cachedSmart = _smartCache?.[card.sourceId];
            if (cachedSmart?.attributes) {
                const attr = cachedSmart.attributes.find(a => a.id === attrId);
                if (attr) {
                    const color = attr.status === 'critical' ? 'text-red-400' :
                                 attr.status === 'warning' ? 'text-yellow-400' : 'text-neon-green';
                    let displayValue = attr.raw;
                    if (attr.unit === 'bytes' && attr.unit_divisor) {
                        const unit = smartUnits[attr.id] || 'raw';
                        if (unit !== 'raw') {
                            displayValue = formatBytes(parseInt(attr.raw_num || attr.raw) * attr.unit_divisor, unit) + ' ' + getUnitLabel(unit);
                        }
                    } else if (attr.unit === 'hours') {
                        const unit = smartUnits[attr.id] || 'raw';
                        if (unit === 'days') {
                            displayValue = (parseInt(attr.raw || '0') / 24).toFixed(1) + ' дн';
                        } else if (unit === 'months') {
                            displayValue = (parseInt(attr.raw || '0') / 720).toFixed(1) + ' мес';
                        }
                    } else if (attr.unit === 'nvme_blocks') {
                        const unit = smartUnits[attr.id] || 'raw';
                        if (unit !== 'raw') {
                            displayValue = formatBytes(attr.value * (attr.unit_divisor || 1), unit) + ' ' + getUnitLabel(unit);
                        }
                    }
                    html += `<div class="text-xs mt-1" title="${escapeHtml(attr.tooltip)}">
                        <span class="text-gray-500">${escapeHtml(attr.description)}:</span>
                        <span class="${color} font-mono">${displayValue}</span>
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

function getUnitLabel(unit) {
    const labels = { 'bytes': 'Б', 'kb': 'КБ', 'mb': 'МБ', 'gb': 'ГБ', 'tb': 'ТБ' };
    return labels[unit] || '';
}

let _pickerCards = null;
let _pickerGroups = null;
let _hiddenSensors = null;
let _dashboardLoaded = false;
let _dashboardSaveTimer = null;

async function loadDashboardFromServer() {
    try {
        const resp = await fetch('/api/dashboard');
        if (resp.ok) {
            const data = await resp.json();
            _pickerCards = data.cards || [];
            _pickerGroups = data.groups || [];
            _hiddenSensors = data.hiddenSensors || [];
            _dashboardLoaded = true;
            return;
        }
    } catch (e) {}
    _pickerCards = [];
    _pickerGroups = [];
    _hiddenSensors = [];
    _dashboardLoaded = true;
}

function getPickerCards() {
    return _pickerCards || [];
}

function setPickerCards(cards) {
    _pickerCards = cards;
    scheduleDashboardSave();
}

function getPickerGroups() {
    return _pickerGroups || [];
}

function setPickerGroups(groups) {
    _pickerGroups = groups;
    scheduleDashboardSave();
}

function scheduleDashboardSave() {
    if (_dashboardSaveTimer) clearTimeout(_dashboardSaveTimer);
    _dashboardSaveTimer = setTimeout(saveDashboardToServer, 500);
}

async function saveDashboardToServer() {
    try {
        await fetch('/api/dashboard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cards: _pickerCards || [], groups: _pickerGroups || [], hiddenSensors: _hiddenSensors || [] })
        });
    } catch (e) {}
}

async function loadPickerCards() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;

    await loadDashboardFromServer();

    if (!canvas._groupHandlersAttached) {
        canvas.addEventListener('dragover', onGroupDragOver);
        canvas.addEventListener('drop', onGroupDropOutside);
        canvas._groupHandlersAttached = true;
    }

    const groups = getPickerGroups();
    if (groups.length) {
        groups.forEach(g => {
            if (!document.querySelector(`[data-group-id="${g.id}"]`)) {
                renderDashboardGroup(g);
            }
        });
    }

    const cards = getPickerCards();
    if (!cards.length && !groups.length) return;

    let positionsChanged = false;
    cards.forEach(c => {
        if (document.querySelector(`[data-card-id="${c.id}"]`)) return;
        if (!c.col || !c.row) { positionsChanged = true; }
        renderPickerCard(c);
        if (c.groupId) {
            const groupEl = document.querySelector(`[data-group-id="${c.groupId}"] .group-cards`);
            const cardEl = document.querySelector(`[data-card-id="${c.id}"]`);
            if (groupEl && cardEl) {
                groupEl.appendChild(cardEl);
                cardEl.classList.remove('cursor-grab');
                cardEl.classList.add('cursor-default');
            }
        }
    });

    for (const c of cards) {
        if (c.groupId) continue;
        const colSp = c.colSpan || 3;
        const rowSp = c.rowSpan || 1;
        if (isCellOccupied(c.col, c.row, colSp, rowSp, c.id)) {
            const free = findFreePosition(cards, colSp, rowSp, c.id);
            c.col = free.col;
            c.row = free.row;
            const el = document.querySelector(`[data-card-id="${c.id}"]`);
            if (el) {
                el.style.gridColumn = `${c.col} / span ${colSp}`;
                el.style.gridRow = `${c.row} / span ${rowSp}`;
            }
            positionsChanged = true;
        }
    }
    if (positionsChanged) setPickerCards(cards);
    document.getElementById('dashboard-empty')?.classList.add('hidden');
    startPickerLiveUpdate();
    prefetchSmartForCards();
    updateCanvasMinHeight();
}

function updateCanvasMinHeight() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;
    let maxRow = 0;
    for (const c of getPickerCards()) {
        if (!c.row) continue;
        const bottom = c.row + (c.rowSpan || 1) - 1;
        if (bottom > maxRow) maxRow = bottom;
    }
    for (const gEl of canvas.querySelectorAll('[data-group-id]')) {
        const rect = gEl.getBoundingClientRect();
        const cRect = canvas.getBoundingClientRect();
        const cs = getComputedStyle(canvas);
        const padT = parseFloat(cs.paddingTop) || 16;
        const rowH = 100;
        const gap = 8;
        const gRowEnd = Math.max(1, Math.round((rect.bottom - cRect.top - padT) / (rowH + gap)) + 1);
        if (gRowEnd > maxRow) maxRow = gRowEnd;
    }
    const minRows = Math.max(maxRow + 5, 8);
    const rowH = 100;
    const gap = 8;
    const padY = 32;
    canvas.style.minHeight = (minRows * (rowH + gap) - gap + padY) + 'px';
}

async function prefetchSmartForCards() {
    const cards = getPickerCards().filter(c => c.type === 'disk' && c.smartAttributes?.length);
    for (const card of cards) {
        if (_smartCache[card.sourceId]) continue;
        try {
            const data = await fetchDiskSmart(card.sourceId);
            if (data && !data.error) {
                _smartCache[card.sourceId] = data;
                updateCardDetails(card.id);
            }
        } catch (e) {}
    }
}

let _pickerLiveTimer = null;

function startPickerLiveUpdate() {
    if (_pickerLiveTimer) return;
    _pickerLiveTimer = setInterval(() => {
        document.querySelectorAll('[data-fan-id]').forEach(el => {
            const src = el.dataset.source;
            const id = el.dataset.fanId;
            let fan = null;
            if (src === 'local' && currentState?.fans?.[id]) {
                fan = currentState.fans[id];
            } else {
                const node = nodesData.find(n => n.node_id === src);
                fan = node?.telemetry?.fans?.[id];
            }
            if (fan) {
                el.textContent = fan.rpm || 0;
                const cardEl = el.closest('[data-card-id]');
                if (cardEl) updateCardDetails(cardEl.dataset.cardId);
            }
        });
        document.querySelectorAll('[data-temp-id]').forEach(el => {
            const src = el.dataset.source;
            const id = el.dataset.tempId;
            let val = null;
            if (src === 'local' && currentState?.temp_sensors?.[id]) {
                val = currentState.temp_sensors[id].value;
            } else {
                const node = nodesData.find(n => n.node_id === src);
                val = node?.telemetry?.temp_sensors?.[id]?.value;
            }
            if (val != null) el.textContent = val;
        });
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
    }, 2000);
}

function showGroupCreator() {
    const modal = document.getElementById('group-creator-modal');
    if (modal) modal.classList.remove('hidden');
}

function hideGroupCreator() {
    const modal = document.getElementById('group-creator-modal');
    if (modal) modal.classList.add('hidden');
}

function createGroup() {
    const name = document.getElementById('group-name-input')?.value?.trim();
    if (!name) return;

    const group = {
        id: 'group-' + Date.now(),
        name: name,
    };

    const groups = getPickerGroups();
    groups.push(group);
    setPickerGroups(groups);

    renderDashboardGroup(group);
    hideGroupCreator();
    document.getElementById('dashboard-empty')?.classList.add('hidden');
}

function renderDashboardGroup(group) {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;

    const el = document.createElement('div');
    el.className = 'dashboard-group bg-cyber-bg border-2 border-dashed border-gray-700 rounded-xl p-3 transition-colors hover:border-neon-purple/50 relative';
    el.setAttribute('data-group-id', group.id);
    el.setAttribute('draggable', 'true');
    el.style.gridColumn = `span ${group.colSpan || getCanvasCols()}`;
    el.style.display = 'flex';
    el.style.flexDirection = 'column';
    if (group.minHeight) el.style.minHeight = group.minHeight;

    el.innerHTML = `
        <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-2">
                <span class="text-gray-600 text-xs select-none cursor-grab">⠿</span>
                <span class="text-xs text-gray-400 font-medium cursor-pointer hover:text-white transition-colors" onclick="startGroupRename('${group.id}')">${escapeHtml(group.name)}</span>
            </div>
            <button onclick="removePickerGroup('${group.id}')" class="text-gray-600 hover:text-red-400 text-xs transition-colors">×</button>
        </div>
        <div class="group-cards flex flex-wrap gap-2 flex-1"></div>
        <div class="group-resize-handle absolute bottom-0 right-0 w-4 h-4 cursor-ns-resize opacity-30 hover:opacity-80 transition-opacity"></div>`;

    el.addEventListener('dragstart', onGroupDragStart);
    el.addEventListener('dragover', onGroupCardDragOver);
    el.addEventListener('drop', onGroupDrop);
    el.addEventListener('dragleave', onGroupDragLeave);
    el.addEventListener('dragend', onGroupDragEnd);

    const handle = el.querySelector('.group-resize-handle');
    handle.addEventListener('mousedown', (e) => startGroupResize(e, group.id));

    canvas.appendChild(el);
}

function removePickerGroup(groupId) {
    const el = document.querySelector(`[data-group-id="${groupId}"]`);
    if (!el) return;

    const cards = el.querySelectorAll('[data-card-id]');
    const canvas = document.getElementById('dashboard-canvas');
    cards.forEach(card => {
        card.classList.remove('cursor-grab');
        canvas.appendChild(card);
    });

    el.remove();

    const saved = getPickerGroups().filter(g => g.id !== groupId);
    setPickerGroups(saved);

    const allCards = getPickerCards();
    allCards.forEach(c => { if (c.groupId === groupId) delete c.groupId; });
    setPickerCards(allCards);

    if (!saved.length && !document.querySelector('[data-card-id]')) {
        document.getElementById('dashboard-empty')?.classList.remove('hidden');
    }
    updateCanvasMinHeight();
}

function onGroupDragLeave(e) {
    this.classList.remove('border-neon-purple', 'bg-purple-900/10');
}

function onGroupDropOutside(e) {
    if (_draggedGroup) {
        _draggedGroup.classList.remove('opacity-40');
        _draggedGroup = null;
    }
}

function onGroupDrop(e) {
    e.preventDefault();
    e.stopPropagation();
    this.classList.remove('border-neon-purple', 'bg-purple-900/10');

    const cardId = e.dataTransfer.getData('text/plain');
    const groupId = e.dataTransfer.getData('text/group');
    if (!cardId && !groupId) return;

    if (cardId) {
        const cardEl = document.querySelector(`[data-card-id="${cardId}"]`);
        const groupCards = this.querySelector('.group-cards');
        if (!cardEl || !groupCards) return;

        const saved = getPickerCards();
        const cardData = saved.find(c => c.id === cardId);
        if (cardData) {
            cardData.groupId = this.dataset.groupId;
            setPickerCards(saved);
        }

        groupCards.appendChild(cardEl);
        cardEl.classList.remove('cursor-grab');
        cardEl.classList.add('cursor-default');
    }
}

let _resizingGroupId = null;
let _resizeStartY = 0;
let _resizeStartH = 0;

function startGroupResize(e, groupId) {
    e.preventDefault();
    e.stopPropagation();
    _resizingGroupId = groupId;
    const el = document.querySelector(`[data-group-id="${groupId}"]`);
    if (!el) return;
    _resizeStartY = e.clientY;
    _resizeStartH = el.offsetHeight;
    document.addEventListener('mousemove', onGroupResize);
    document.addEventListener('mouseup', stopGroupResize);
}

function onGroupResize(e) {
    if (!_resizingGroupId) return;
    const el = document.querySelector(`[data-group-id="${_resizingGroupId}"]`);
    if (!el) return;
    const h = Math.max(100, _resizeStartH + (e.clientY - _resizeStartY));
    el.style.minHeight = h + 'px';
}

function stopGroupResize() {
    if (!_resizingGroupId) return;
    const groups = getPickerGroups();
    const group = groups.find(g => g.id === _resizingGroupId);
    const el = document.querySelector(`[data-group-id="${_resizingGroupId}"]`);
    if (group && el) {
        group.minHeight = el.style.minHeight;
        setPickerGroups(groups);
    }
    _resizingGroupId = null;
    document.removeEventListener('mousemove', onGroupResize);
    document.removeEventListener('mouseup', stopGroupResize);
}

let _draggedGroup = null;
let _groupDropTarget = null;

function onGroupDragStart(e) {
    if (e.target.closest('.group-resize-handle') || e.target.closest('button') || e.target.closest('input')) return;
    _draggedGroup = this;
    _groupDropTarget = null;
    this.classList.add('opacity-40');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/group', this.dataset.groupId);
}

function onGroupDragOver(e) {
    if (!_draggedGroup) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const canvas = document.getElementById('dashboard-canvas');
    _groupDropTarget = getDragAfterElement(canvas, e.clientX, e.clientY);
}

function onGroupDragEnd() {
    if (_draggedGroup) {
        if (_groupDropTarget !== undefined) {
            const canvas = document.getElementById('dashboard-canvas');
            if (_groupDropTarget) {
                canvas.insertBefore(_draggedGroup, _groupDropTarget);
            } else {
                canvas.appendChild(_draggedGroup);
            }
        }
        _draggedGroup.classList.remove('opacity-40');
        _draggedGroup = null;
        saveGroupOrder();
    }
}

function saveGroupOrder() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;
    const ordered = [...canvas.querySelectorAll('[data-group-id]')].map(el => el.dataset.groupId);
    const saved = getPickerGroups();
    const orderedGroups = ordered.map(id => saved.find(g => g.id === id)).filter(Boolean);
    setPickerGroups(orderedGroups);
}

function startGroupRename(groupId) {
    const el = document.querySelector(`[data-group-id="${groupId}"]`);
    if (!el) return;
    const nameSpan = el.querySelector('.flex.items-center.justify-between span');
    if (!nameSpan) return;

    const groups = getPickerGroups();
    const group = groups.find(g => g.id === groupId);
    if (!group) return;

    const input = document.createElement('input');
    input.type = 'text';
    input.value = group.name;
    input.className = 'bg-cyber-bg border border-neon-purple rounded px-1 py-0 text-xs text-white w-32';
    input.onblur = () => finishGroupRename(groupId, input.value);
    input.onkeydown = (e) => { if (e.key === 'Enter') input.blur(); if (e.key === 'Escape') { input.value = group.name; input.blur(); } };

    nameSpan.replaceWith(input);
    input.focus();
    input.select();
}

function finishGroupRename(groupId, newName) {
    newName = newName.trim();
    if (!newName) return;

    const groups = getPickerGroups();
    const group = groups.find(g => g.id === groupId);
    if (!group) return;

    group.name = newName;
    setPickerGroups(groups);

    const el = document.querySelector(`[data-group-id="${groupId}"]`);
    if (el) {
        const input = el.querySelector('input[type="text"]');
        if (input) {
            const span = document.createElement('span');
            span.className = 'text-xs text-gray-400 font-medium cursor-pointer hover:text-white transition-colors';
            span.setAttribute('onclick', `startGroupRename('${groupId}')`);
            span.textContent = newName;
            input.replaceWith(span);
        }
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
        const pct = fan.current_pct != null ? fan.current_pct : (fan.manual_pct != null ? fan.manual_pct : 50);
        slider.value = pct;
        slider.disabled = (mode === 'auto');
        document.getElementById('pwm-value-display').textContent = `${pct}%`;
    }
    
    // Update mode buttons
    const btnManual = document.getElementById('btn-mode-manual');
    const btnAuto = document.getElementById('btn-mode-auto');
    
    if (mode === 'manual') {
        btnManual.className = BTN_MANUAL_ACTIVE;
        btnAuto.className = BTN_AUTO_INACTIVE;
    } else {
        btnManual.className = BTN_MANUAL_INACTIVE;
        btnAuto.className = BTN_AUTO_ACTIVE;
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

    // Calibration params
    const cal = fan.calibration || {};
    const minPwmEl = document.getElementById('cal-min-pwm');
    const maxPwmEl = document.getElementById('cal-max-pwm');
    const lambdaEl = document.getElementById('cal-lambda');
    if (minPwmEl) {
        minPwmEl.value = cal.min_pwm || 0;
        document.getElementById('cal-min-pwm-val').textContent = cal.min_pwm || 0;
    }
    if (maxPwmEl) {
        maxPwmEl.value = cal.max_pwm || 255;
        document.getElementById('cal-max-pwm-val').textContent = cal.max_pwm || 255;
    }
    if (lambdaEl) {
        lambdaEl.value = (cal.lambda || 1.0) * 10;
        document.getElementById('cal-lambda-val').textContent = (cal.lambda || 1.0).toFixed(1);
    }
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
            btnManual.className = BTN_MANUAL_ACTIVE;
            btnAuto.className = BTN_AUTO_INACTIVE;
        } else {
            btnManual.className = BTN_MANUAL_INACTIVE;
            btnAuto.className = BTN_AUTO_ACTIVE;
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
    updateCanvasColumns();
    window.addEventListener('resize', updateCanvasColumns);

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
    const hidden = getHiddenSensors();

    if (data.hdd_sensors) {
        for (const [id, disk] of Object.entries(data.hdd_sensors)) {
            if (hidden.includes(`disk:${id}`)) continue;
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
            if (hidden.includes(`temp:${id}`)) continue;
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
                            ${s.standby ? t('sensor.sleep', 'Sleep') : formatTemp(s.temp)}
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
                            title: { text: getTempUnitSymbol(), style: { color: '#ff2d55' } },
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
                    ${disk.standby ? t('sensor.sleep', 'Sleep') : disk.temp > 0 ? formatTemp(disk.temp) : '--'}
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
    
    setDiscoverButtonState(true);
    wizardStep = 'scanning';
    
    fetch('/api/discover', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            setDiscoverButtonState(false);
            
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
            setDiscoverButtonState(false);
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
                    <span class="text-sm font-mono text-neon-cyan">${formatTemp(sensor.value)}</span>
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
                            ${disk.standby ? t('sensor.sleep', 'Sleep') : disk.temp > 0 ? formatTemp(disk.temp) : '--'}
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

function updateCalibrationParam(param, value) {
    if (!currentFanId || !currentState || !currentState.fans) return;
    const fan = currentState.fans[currentFanId];
    if (!fan) return;

    if (!fan.calibration) fan.calibration = {};

    if (param === 'lambda') {
        fan.calibration.lambda = parseFloat(value);
        document.getElementById('cal-lambda-val').textContent = parseFloat(value).toFixed(1);
    } else if (param === 'min_pwm') {
        fan.calibration.min_pwm = parseInt(value);
        document.getElementById('cal-min-pwm-val').textContent = value;
    } else if (param === 'max_pwm') {
        fan.calibration.max_pwm = parseInt(value);
        document.getElementById('cal-max-pwm-val').textContent = value;
    }

    saveFanCalibration(currentFanId, fan.calibration);
}

function saveFanCalibration(fanId, calibration) {
    fetch('/api/fan/' + fanId + '/calibration', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(calibration)
    }).catch(err => console.error('Save calibration error:', err));
}

function startCalibration() {
    if (!confirm(t('calibration.confirm', 'Recalibrate all fans? This takes 1-2 minutes.'))) return;
    
    document.getElementById('calibration-modal').classList.remove('hidden');
    document.getElementById('calibration-status').textContent = 'Starting...';
    document.getElementById('calibration-progress-bar').style.width = '0%';
    document.getElementById('calibration-step').textContent = 'Step 0/21';
    
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
        html += `<th class="h-5 px-0 text-[10px] text-gray-500 font-normal" style="width:${SCHEDULE_CELL_SIZE}px">${h}</th>`;
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
                         style="width:${SCHEDULE_CELL_SIZE}px;height:${SCHEDULE_CELL_SIZE}px;${bgStyle}"></td>`;
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
            settings = `${formatTemp(item.target_temp || 31)}`;
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
    
    modes.forEach(m => {
        const btn = document.getElementById(`sched-btn-${m}`);
        if (btn) btn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${m === mode ? BTN_ACTIVE : BTN_INACTIVE}`;
    });
    
    document.getElementById('sched-auto-settings').classList.toggle('hidden', mode !== 'auto');
    document.getElementById('sched-manual-settings').classList.toggle('hidden', mode !== 'manual');
}

function setScheduleSensorMode(sensorMode) {
    const modes = ['max', 'min', 'avg'];
    
    modes.forEach(m => {
        const btn = document.getElementById(`sched-btn-sensor-${m}`);
        if (btn) btn.className = `flex-1 py-2 px-3 rounded-lg text-xs font-semibold transition-all duration-300 border ${m === sensorMode ? BTN_ACTIVE : BTN_INACTIVE}`;
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
                            ${s.standby ? t('sensor.sleep', 'Sleep') : formatTemp(s.temp)}
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
        updateSettingsUI();
        autoCheckUpdate();
    }
}

function updateLangButtons() {
    const enBtn = document.getElementById('lang-btn-en');
    const ruBtn = document.getElementById('lang-btn-ru');
    const setupEn = document.getElementById('setup-lang-en');
    const setupRu = document.getElementById('setup-lang-ru');
    
    if (enBtn) enBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${currentLang === 'en' ? BTN_ACTIVE : BTN_INACTIVE}`;
    if (ruBtn) ruBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${currentLang === 'ru' ? BTN_ACTIVE : BTN_INACTIVE}`;
    if (setupEn) setupEn.className = `text-xs px-2 py-1 rounded border transition-all ${currentLang === 'en' ? BTN_ACTIVE : BTN_INACTIVE}`;
    if (setupRu) setupRu.className = `text-xs px-2 py-1 rounded border transition-all ${currentLang === 'ru' ? BTN_ACTIVE : BTN_INACTIVE}`;
    
    updateSettingsUI();
}

function updateSettingsUI() {
    const s = getSettings();
    
    // Temperature unit buttons
    const celsiusBtn = document.getElementById('unit-btn-celsius');
    const fahrBtn = document.getElementById('unit-btn-fahrenheit');
    if (celsiusBtn) celsiusBtn.className = `flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${s.tempUnit === 'celsius' ? BTN_ACTIVE : BTN_INACTIVE}`;
    if (fahrBtn) fahrBtn.className = `flex-1 py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${s.tempUnit === 'fahrenheit' ? BTN_ACTIVE : BTN_INACTIVE}`;
    
    // Refresh interval buttons
    [0, 1000, 5000].forEach(v => {
        const btn = document.getElementById(`refresh-btn-${v}`);
        if (btn) btn.className = `flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border ${s.refreshInterval === v ? BTN_ACTIVE : BTN_INACTIVE}`;
    });
    
    // Compact mode toggle
    const compactBtn = document.getElementById('compact-toggle');
    if (compactBtn) {
        compactBtn.className = s.compactMode
            ? `w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${BTN_ACTIVE}`
            : `w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border ${BTN_INACTIVE}`;
        compactBtn.querySelector('span').textContent = s.compactMode ? t('settings.on', 'On') : t('settings.off', 'Off');
    }
    
    // Apply compact mode to body
    document.body.classList.toggle('compact-mode', s.compactMode);
    
    // Auto-update interval buttons
    [0, 21600000, 43200000, 86400000].forEach(v => {
        const btn = document.getElementById(`autoupd-btn-${v}`);
        if (btn) btn.className = `flex-1 py-1.5 px-2 rounded-lg text-[10px] font-semibold transition-all duration-300 border ${s.autoUpdateCheck === v ? BTN_ACTIVE : BTN_INACTIVE}`;
    });
}

function setTempUnit(unit) {
    saveSettings({ tempUnit: unit });
    updateSettingsUI();
    // Re-render current data
    if (currentState) updateUI(currentState);
}

function setRefreshInterval(ms) {
    saveSettings({ refreshInterval: ms });
    updateSettingsUI();
}

function toggleCompactMode() {
    const s = getSettings();
    saveSettings({ compactMode: !s.compactMode });
    updateSettingsUI();
}

function setAutoUpdateInterval(ms) {
    saveSettings({ autoUpdateCheck: ms });
    updateSettingsUI();
    scheduleAutoUpdate();
}

let _autoUpdateTimer = null;
function scheduleAutoUpdate() {
    if (_autoUpdateTimer) { clearInterval(_autoUpdateTimer); _autoUpdateTimer = null; }
    const ms = getSettings().autoUpdateCheck;
    if (ms > 0) {
        _autoUpdateTimer = setInterval(() => { _updateChecked = false; autoCheckUpdate(); }, ms);
    }
}

async function checkForUpdates() {
    const btn = document.getElementById('update-check-btn');
    const result = document.getElementById('update-result');
    const applyBtn = document.getElementById('update-apply-btn');
    
    if (btn) {
        btn.disabled = true;
        btn.querySelector('span').textContent = t('settings.checking', 'Checking...');
    }
    if (result) result.classList.add('hidden');
    if (applyBtn) {
        applyBtn.classList.add('hidden');
        applyBtn.disabled = true;
        applyBtn.className = 'hidden w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border bg-cyber-accent text-gray-500 border-gray-700 mt-2';
    }
    
    try {
        const resp = await fetch('/api/update/check');
        const data = await resp.json();
        
        const badge = document.getElementById('update-badge');
        
        if (data.has_update) {
            if (badge) badge.classList.remove('hidden');
            if (result) {
                result.classList.remove('hidden');
                result.className = 'text-xs mt-2 p-3 rounded-lg bg-green-900 bg-opacity-20 border border-green-800 text-neon-green';
                result.innerHTML = `
                    <div class="font-semibold mb-2">${t('settings.update_available', 'Update available')}</div>
                    <div class="flex justify-between mb-1"><span class="text-gray-400">${t('settings.current_version', 'Current')}:</span><span class="font-mono">${escapeHtml(data.current_version || '?')}</span></div>
                    <div class="flex justify-between mb-1"><span class="text-gray-400">${t('settings.new_version', 'New')}:</span><span class="font-mono text-white font-bold">${escapeHtml(data.remote_version || '?')}</span></div>
                    ${data.commit_message ? `<div class="mt-2 pt-2 border-t border-green-800 text-gray-300">${escapeHtml(data.commit_message)}</div>` : ''}`;
            }
            if (applyBtn) {
                applyBtn.classList.remove('hidden');
                applyBtn.disabled = false;
                applyBtn.className = 'w-full py-2 px-3 rounded-lg text-sm font-semibold transition-all duration-300 border mt-2 bg-green-900 bg-opacity-30 text-neon-green border-green-700 hover:bg-opacity-50';
            }
        } else {
            if (badge) badge.classList.add('hidden');
            if (result) {
                result.classList.remove('hidden');
                result.className = 'text-xs mt-2 p-3 rounded-lg bg-cyber-accent border border-cyber-accent text-gray-400';
                result.textContent = t('settings.up_to_date', 'System is up to date');
            }
        }
        return data.has_update;
    } catch (e) {
        if (result) {
            result.classList.remove('hidden');
            result.className = 'text-xs mt-2 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red';
            result.textContent = t('settings.update_error', 'Failed to check for updates');
        }
        return false;
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.querySelector('span').textContent = t('settings.check_update', 'Check for Updates');
        }
    }
}

let _updateChecked = false;

function openUpdateModal() {
    const modal = document.getElementById('update-modal');
    const steps = document.getElementById('update-modal-steps');
    const progress = document.getElementById('update-modal-progress');
    const result = document.getElementById('update-modal-result');
    const applyBtn = document.getElementById('update-modal-apply');
    const closeBtn = document.getElementById('update-modal-close');
    
    steps.innerHTML = `
        <div id="upd-step-pull" class="flex items-center gap-3 text-sm">
            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-pull-icon">1</span>
            <span class="text-gray-300">${t('settings.step_pull', 'Pulling latest code...')}</span>
        </div>
        <div id="upd-step-restart" class="flex items-center gap-3 text-sm opacity-40">
            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-restart-icon">2</span>
            <span class="text-gray-300">${t('settings.step_restart', 'Restarting container...')}</span>
        </div>
    `;
    
    progress.classList.add('hidden');
    result.classList.add('hidden');
    applyBtn.disabled = false;
    applyBtn.classList.remove('hidden');
    closeBtn.classList.remove('hidden');
    
    modal.classList.remove('hidden');
}

function closeUpdateModal() {
    document.getElementById('update-modal').classList.add('hidden');
}

function setStepState(step, state) {
    const el = document.getElementById(`upd-step-${step}`);
    const icon = document.getElementById(`upd-step-${step}-icon`);
    if (!el || !icon) return;
    
    el.classList.remove('opacity-40');
    
    if (state === 'active') {
        icon.className = 'w-5 h-5 rounded-full border-2 border-neon-cyan flex-shrink-0 flex items-center justify-center text-[10px] text-neon-cyan animate-pulse';
        icon.innerHTML = '⟳';
    } else if (state === 'done') {
        icon.className = 'w-5 h-5 rounded-full bg-neon-green flex-shrink-0 flex items-center justify-center text-[10px] text-black';
        icon.innerHTML = '✓';
    } else if (state === 'error') {
        icon.className = 'w-5 h-5 rounded-full bg-neon-red flex-shrink-0 flex items-center justify-center text-[10px] text-white';
        icon.innerHTML = '✕';
    }
}

async function startUpdate() {
    const applyBtn = document.getElementById('update-modal-apply');
    const progress = document.getElementById('update-modal-progress');
    const bar = document.getElementById('update-modal-bar');
    const result = document.getElementById('update-modal-result');
    const closeBtn = document.getElementById('update-modal-close');
    
    applyBtn.classList.add('hidden');
    closeBtn.classList.add('hidden');
    progress.classList.remove('hidden');
    bar.style.width = '10%';
    
    // Step 1: Git pull
    setStepState('pull', 'active');
    bar.style.width = '20%';
    
    try {
        const resp = await fetch('/api/update/apply', { method: 'POST' });
        const data = await resp.json();
        
        if (data.status === 'error') {
            setStepState('pull', 'error');
            bar.style.width = '100%';
            bar.className = 'bg-neon-red h-2 rounded-full transition-all duration-500';
            result.classList.remove('hidden');
            result.className = 'text-sm mb-4 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red';
            result.textContent = data.message || t('settings.update_failed', 'Update failed');
            applyBtn.classList.remove('hidden');
            applyBtn.disabled = false;
            closeBtn.classList.remove('hidden');
            return;
        }
        
        // Step 1 done
        setStepState('pull', 'done');
        bar.style.width = '50%';
        
        // Step 2: Restart (entrypoint syncs code from /repo)
        setStepState('restart', 'active');
        bar.style.width = '80%';
        
        // Show restart notification
        result.classList.remove('hidden');
        result.className = 'text-sm mb-4 p-3 rounded-lg bg-green-900 bg-opacity-20 border border-green-800 text-neon-green';
        result.innerHTML = `
            <div class="font-semibold mb-1">${t('settings.update_success', 'Update complete!')}</div>
            <div class="text-gray-400">${t('settings.restart_notice', 'Container is restarting. Page will reload in 10 seconds...')}</div>
        `;
        
        bar.style.width = '100%';
        setStepState('restart', 'done');
        
        // Reload after delay
        setTimeout(() => { window.location.reload(); }, RELOAD_DELAY);
        
    } catch (e) {
        setStepState('pull', 'error');
        bar.style.width = '100%';
        bar.className = 'bg-neon-red h-2 rounded-full transition-all duration-500';
        result.classList.remove('hidden');
        result.className = 'text-sm mb-4 p-3 rounded-lg bg-red-900 bg-opacity-30 border border-red-700 text-neon-red';
        result.textContent = t('settings.update_error', 'Failed to apply update');
        applyBtn.classList.remove('hidden');
        applyBtn.disabled = false;
        closeBtn.classList.remove('hidden');
    }
}
async function autoCheckUpdate() {
    if (_updateChecked) return;
    _updateChecked = true;
    await checkForUpdates();
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
    console.log('[FanControl] Neon Cyberpunk Edition initialized');

    window.addEventListener('beforeunload', () => {
        if (_dashboardSaveTimer) {
            clearTimeout(_dashboardSaveTimer);
            saveDashboardToServer();
        }
    });

    // Load language
    await loadLang(currentLang);
    updateLangButtons();
    updateSettingsUI();
    
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
    
    // Auto-check for updates in background (5s after load)
    setTimeout(() => autoCheckUpdate(), 5000);
    
    // Schedule periodic auto-check
    scheduleAutoUpdate();
    
    // Load nodes for multi-node dashboard
    loadNodes();
});

// ============================================================================
// NODE MANAGEMENT (Multi-node Dashboard)
// ============================================================================

let currentView = 'dashboard';
let selectedNodeId = null;
let nodesData = [];

async function loadNodes() {
    try {
        const resp = await fetch('/api/nodes');
        nodesData = await resp.json();
        renderNodeSidebar();
        renderNodesOverview();
    } catch (e) {
        console.error('[FanControl] Failed to load nodes:', e);
    }
}

function renderNodeSidebar() {
    const container = document.getElementById('node-list');
    if (!container) return;
    
    let html = '';
    for (const node of nodesData) {
        const statusDot = node.status === 'online' ? 'bg-green-400' : 'bg-gray-500';
        const modeIcon = node.control_mode === 'manual' ? ' <span class="text-yellow-400" title="Manual mode">&#9888;</span>' : '';
        const isActive = selectedNodeId === node.node_id;
        
        html += `
            <div class="flex items-center gap-2 p-2 rounded cursor-pointer transition-all ${isActive ? 'bg-cyan-900/30 border border-cyan-500/30' : 'hover:bg-gray-800/50 border border-transparent'}"
                 onclick="selectNode('${escapeHtml(node.node_id)}')">
                <div class="w-2 h-2 rounded-full ${statusDot} flex-shrink-0"></div>
                <div class="flex-1 min-w-0">
                    <div class="text-white text-sm truncate">${escapeHtml(node.name)}${modeIcon}</div>
                    <div class="text-gray-500 text-xs">${node.status}${node.ip ? ' &middot; ' + escapeHtml(node.ip) : ''}</div>
                </div>
            </div>
        `;
    }
    
    if (nodesData.length === 0) {
        html = `<div class="text-gray-500 text-sm text-center py-4">${t('nodes.no_nodes', 'No nodes connected')}</div>`;
    }
    
    container.innerHTML = html;
}

function renderNodesOverview() {
    const container = document.getElementById('nodes-grid');
    if (!container) return;
    
    let html = '';
    for (const node of nodesData) {
        const telemetry = node.telemetry || {};
        const fans = telemetry.fans || {};
        const temps = telemetry.temp_sensors || {};
        const tempValues = Object.values(temps).map(s => (s && s.value) || 0);
        const maxTemp = tempValues.length > 0 ? Math.max(...tempValues) : 0;
        const totalRPM = Object.values(fans).reduce((sum, f) => sum + ((f && f.rpm) || 0), 0);
        
        html += `
            <div class="bg-gray-900/50 border border-gray-700 rounded-xl p-4 cursor-pointer hover:border-cyan-500/50 transition-all"
                 onclick="selectNode('${escapeHtml(node.node_id)}')">
                <div class="flex items-center justify-between mb-3">
                    <h3 class="text-white font-semibold">${escapeHtml(node.name)}</h3>
                    <div class="flex items-center gap-2">
                        <span class="text-xs ${node.status === 'online' ? 'text-green-400' : 'text-gray-500'}">${node.status}</span>
                        ${node.control_mode === 'manual' ? '<span class="text-yellow-400 text-xs">&#9888; Manual</span>' : ''}
                    </div>
                </div>
                <div class="grid grid-cols-2 gap-2 text-sm">
                    <div class="text-gray-400">${t('nodes.max_temp', 'Max Temp')}</div>
                    <div class="text-white text-right">${maxTemp}&deg;C</div>
                    <div class="text-gray-400">${t('nodes.total_rpm', 'Total RPM')}</div>
                    <div class="text-white text-right">${totalRPM}</div>
                    <div class="text-gray-400">${t('nodes.fans', 'Fans')}</div>
                    <div class="text-white text-right">${Object.keys(fans).length}</div>
                </div>
            </div>
        `;
    }
    
    if (nodesData.length === 0) {
        html = `<div class="text-gray-500 text-center py-8 col-span-2">${t('nodes.no_nodes', 'No nodes connected. Add a node to get started.')}</div>`;
    }
    
    container.innerHTML = html;
}

function selectNode(nodeId) {
    selectedNodeId = nodeId;
    currentView = 'node-detail';
    showView('node-detail');
    loadNodeDetail(nodeId);
}

async function loadNodeDetail(nodeId) {
    try {
        const resp = await fetch(`/api/nodes/${nodeId}`);
        const node = await resp.json();
        renderNodeDetail(node);
    } catch (e) {
        console.error('[FanControl] Failed to load node detail:', e);
    }
}

function renderNodeDetail(node) {
    const container = document.getElementById('node-detail-content');
    if (!container) return;
    
    const telemetry = node.telemetry || {};
    const fans = telemetry.fans || {};
    const temps = telemetry.temp_sensors || {};
    
    let fansHtml = '';
    for (const [id, fan] of Object.entries(fans)) {
        const pwm = (fan && fan.pwm_value) || 0;
        fansHtml += `
            <div class="bg-gray-800/50 rounded-lg p-3">
                <div class="flex justify-between text-sm">
                    <span class="text-gray-400">${escapeHtml(id)}</span>
                    <span class="text-white">${(fan && fan.rpm) || 0} RPM</span>
                </div>
                <div class="mt-1 bg-gray-700 rounded-full h-2">
                    <div class="bg-cyan-500 h-2 rounded-full" style="width: ${pwm / 255 * 100}%"></div>
                </div>
            </div>
        `;
    }
    
    let tempsHtml = '';
    for (const [id, temp] of Object.entries(temps)) {
        tempsHtml += `
            <div class="flex justify-between text-sm">
                <span class="text-gray-400">${escapeHtml(id)}</span>
                <span class="text-white">${(temp && temp.value) || 0}&deg;C</span>
            </div>
        `;
    }
    
    container.innerHTML = `
        <div class="flex items-center justify-between mb-6">
            <div>
                <h2 class="text-xl font-bold text-white">${escapeHtml(node.name)}</h2>
                <p class="text-gray-400 text-sm">${node.node_id} &middot; ${node.status} &middot; ${node.control_mode || 'auto'} mode</p>
            </div>
            <div class="flex gap-2">
                <button onclick="deleteNode('${escapeHtml(node.node_id)}')"
                    class="px-3 py-1 bg-red-900/30 border border-red-500/30 rounded text-red-400 text-sm hover:bg-red-900/50 transition-all">
                    ${t('nodes.delete', 'Delete')}
                </button>
                <button onclick="showView('nodes')"
                    class="px-3 py-1 bg-gray-800 border border-gray-600 rounded text-gray-300 text-sm hover:bg-gray-700 transition-all">
                    ${t('nodes.back', 'Back')}
                </button>
            </div>
        </div>
        
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div>
                <h3 class="text-white font-semibold mb-3">${t('nodes.fans', 'Fans')}</h3>
                <div class="space-y-2">${fansHtml || '<div class="text-gray-500 text-sm">No fan data</div>'}</div>
            </div>
            <div>
                <h3 class="text-white font-semibold mb-3">${t('node.temperatures', 'Temperatures')}</h3>
                <div class="space-y-2">${tempsHtml || '<div class="text-gray-500 text-sm">No temperature data</div>'}</div>
            </div>
        </div>
    `;
}

function showView(view) {
    currentView = view;

    const canvas = document.getElementById('dashboard-canvas-container');
    const inspector = document.getElementById('inspector-container');
    const addBtn = document.getElementById('dashboard-add-btn');
    const groupBtn = document.getElementById('dashboard-group-btn');

    if (view === 'dashboard') {
        if (canvas) canvas.classList.remove('hidden');
        if (inspector) inspector.classList.add('hidden');
        if (addBtn) addBtn.classList.remove('hidden');
        if (groupBtn) groupBtn.classList.remove('hidden');
    } else if (view === 'inspector') {
        if (canvas) canvas.classList.add('hidden');
        if (inspector) inspector.classList.remove('hidden');
        if (addBtn) addBtn.classList.add('hidden');
        if (groupBtn) groupBtn.classList.add('hidden');
    }

    // Update nav button styles
    const dashBtn = document.getElementById('nav-dashboard-btn');
    if (dashBtn) {
        if (view === 'dashboard') {
            dashBtn.classList.add('text-neon-cyan', 'border-neon-cyan');
            dashBtn.classList.remove('text-gray-500', 'border-transparent');
        } else {
            dashBtn.classList.remove('text-neon-cyan', 'border-neon-cyan');
            dashBtn.classList.add('text-gray-500', 'border-transparent');
        }
    }
}

async function addNode() {
    const input = document.getElementById('new-node-name');
    const name = input?.value?.trim();
    if (!name) return;
    
    try {
        const resp = await fetch('/api/nodes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        if (resp.ok) {
            input.value = '';
            loadNodes();
        }
    } catch (e) {
        console.error('[FanControl] Failed to add node:', e);
    }
}

async function deleteNode(nodeId) {
    if (!confirm(t('nodes.confirm_delete', 'Delete this node?'))) return;
    try {
        await fetch(`/api/nodes/${nodeId}`, { method: 'DELETE' });
        if (selectedNodeId === nodeId) {
            selectedNodeId = null;
            showView('nodes');
        }
        loadNodes();
    } catch (e) {
        console.error('[FanControl] Failed to delete node:', e);
    }
}

socket.on('node:update', (data) => {
    const idx = nodesData.findIndex(n => n.node_id === data.node_id);
    if (idx >= 0) {
        nodesData[idx].status = data.status;
        nodesData[idx].name = data.name || nodesData[idx].name;
        if (data.ip) nodesData[idx].ip = data.ip;
        if (data.control_mode) nodesData[idx].control_mode = data.control_mode;
    }
    renderNodeSidebar();
    renderNodesOverview();
});

socket.on('node:telemetry', (data) => {
    const idx = nodesData.findIndex(n => n.node_id === data.node_id);
    if (idx >= 0) {
        nodesData[idx].telemetry = data.telemetry;
    }
    renderNodeSidebar();
    renderNodesOverview();
    if (selectedNodeId === data.node_id && currentView === 'node-detail') {
        loadNodeDetail(data.node_id);
    }
});

// ============================================================================
// CONFIG SYNC & CONFLICT MANAGEMENT
// ============================================================================

let conflictData = null;

socket.on('node:conflict', (data) => {
    console.warn('[FanControl] Node conflict:', data);
    conflictData = data;
    const idx = nodesData.findIndex(n => n.node_id === data.node_id);
    if (idx >= 0) {
        nodesData[idx].control_mode = 'manual';
    }
    renderNodeSidebar();
    showConflictModal(data);
});

socket.on('node:mode_changed', (data) => {
    const idx = nodesData.findIndex(n => n.node_id === data.node_id);
    if (idx >= 0) {
        nodesData[idx].control_mode = data.mode;
    }
    renderNodeSidebar();
    renderNodesOverview();
    if (data.mode === 'manual') {
        showManualModeWarning(data.node_id);
    }
});

function showConflictModal(data) {
    const modal = document.getElementById('conflict-modal');
    if (!modal) return;

    document.getElementById('conflict-node-name').textContent = data.name || data.node_id;

    const serverFans = (data.server_config || {}).fans || {};
    let serverHtml = '';
    for (const [id, fan] of Object.entries(serverFans)) {
        serverHtml += `<div class="text-sm"><span class="text-gray-400">${escapeHtml(id)}:</span> <span class="text-white">mode=${fan.mode}, temp=${fan.target_temp}°C</span></div>`;
    }
    document.getElementById('conflict-server-config').innerHTML = serverHtml || `<div class="text-gray-500 text-sm">${t('conflict.no_config', 'No config')}</div>`;

    const agentFans = (data.agent_config || {}).fans || {};
    let agentHtml = '';
    for (const [id, fan] of Object.entries(agentFans)) {
        agentHtml += `<div class="text-sm"><span class="text-gray-400">${escapeHtml(id)}:</span> <span class="text-white">mode=${fan.mode}, temp=${fan.target_temp}°C</span></div>`;
    }
    document.getElementById('conflict-agent-config').innerHTML = agentHtml || `<div class="text-gray-500 text-sm">${t('conflict.no_config', 'No config')}</div>`;

    modal.classList.remove('hidden');
}

function hideConflictModal() {
    document.getElementById('conflict-modal')?.classList.add('hidden');
    conflictData = null;
}

async function applyServerConfig() {
    if (!conflictData) return;
    try {
        await fetch(`/api/nodes/${conflictData.node_id}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: conflictData.server_config })
        });
        hideConflictModal();
    } catch (e) {
        console.error('Failed to apply server config:', e);
    }
}

async function keepAgentConfig() {
    if (!conflictData) return;
    try {
        await fetch(`/api/nodes/${conflictData.node_id}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: conflictData.agent_config })
        });
        hideConflictModal();
    } catch (e) {
        console.error('Failed to keep agent config:', e);
    }
}

function showManualModeWarning(nodeId) {
    const node = nodesData.find(n => n.node_id === nodeId);
    if (!node) return;
    const warning = document.getElementById('manual-mode-warning');
    if (!warning) return;

    document.getElementById('manual-mode-node-name').textContent = node.name || nodeId;
    document.getElementById('manual-mode-switch-btn').onclick = () => switchToServerMode(nodeId);
    warning.classList.remove('hidden');
}

function hideManualModeWarning() {
    document.getElementById('manual-mode-warning')?.classList.add('hidden');
}

async function switchToServerMode(nodeId) {
    try {
        await fetch(`/api/nodes/${nodeId}/mode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'server' })
        });
        hideManualModeWarning();
    } catch (e) {
        console.error('Failed to switch mode:', e);
    }
}

async function pushConfigToNode(nodeId) {
    try {
        const resp = await fetch('/api/state');
        const state = await resp.json();
        await fetch(`/api/nodes/${nodeId}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: { fans: state.fans } })
        });
    } catch (e) {
        console.error('Failed to push config:', e);
    }
}

console.log('[FanControl] main.js loaded successfully');
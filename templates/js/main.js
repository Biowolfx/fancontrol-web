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
    const ver = currentState?.config_version || '3.3.0';
    if (translations['app.title']) {
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

    // Refresh node tree if on nodes tab
    if (currentView === 'nodes') {
        buildNodeTree();
    }

    // Refresh dashboard if on dashboard tab
    if (currentView === 'dashboard') {
        renderDashboard();
    }
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
    document.getElementById('setup-screen').classList.add('hidden');
    document.getElementById('main-screen').classList.remove('hidden');
    if (!currentState || !currentState.testing) {
        hideCalibrationModal();
    }
    // Show dashboard view by default
    showView(currentView || 'dashboard');
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

function buildNodeTree() {
    const container = document.getElementById('node-tree');
    if (!container) return;

    let html = '';

    // Local server
    html += renderLocalServerTree();

    // Remote nodes
    for (const node of nodesData) {
        html += renderRemoteNodeTree(node);
    }

    container.innerHTML = html || `<div class="text-center text-gray-500 py-4 text-sm">${t('nodes.no_nodes', 'No nodes connected')}</div>`;
}

function renderLocalServerTree() {
    if (!currentState || !currentState.fans) return '';

    const fans = currentState.fans;
    const temps = currentState.temp_sensors || {};
    const disks = currentState.hdd_sensors || {};
    const fanCount = Object.keys(fans).length;
    const diskCount = Object.keys(disks).length;

    let html = `
        <div class="node-group" data-node="local">
            <div class="flex items-center gap-2 p-2 rounded hover:bg-cyber-accent cursor-pointer node-header"
                 onclick="toggleNodeGroup('local')">
                <span class="text-neon-cyan text-xs">▼</span>
                <span class="text-sm font-semibold text-white">🖥 ${t('nodes.local_server', 'My Server')}</span>
                <span class="ml-auto text-xs bg-green-900 bg-opacity-30 text-neon-green px-1.5 py-0.5 rounded">${fanCount} ${t('nodes.fans', 'fans')}</span>
            </div>
            <div class="node-children ml-4 space-y-0.5" id="node-children-local">
    `;

    for (const [fanId, fan] of Object.entries(fans)) {
        const isSelected = fanId === currentFanId;
        html += `
            <div class="flex items-center gap-2 p-1.5 rounded cursor-pointer transition-all ${isSelected ? 'bg-cyber-accent border-l-2 border-neon-purple' : 'hover:bg-cyber-accent border-l-2 border-transparent'}"
                 onclick="selectFanFromTree('${escapeHtml(fanId)}', 'local')">
                <span class="text-xs">🌀</span>
                <span class="text-xs text-gray-300 truncate">${escapeHtml(fan.label)}</span>
                <span class="ml-auto text-xs font-mono text-neon-cyan" id="tree-fan-rpm-${escapeHtml(fanId)}">${fan.rpm || 0}</span>
            </div>
        `;
    }

    for (const [sensorId, sensor] of Object.entries(temps)) {
        html += `
            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">
                <span class="text-xs">🌡</span>
                <span class="text-xs text-gray-300 truncate">${escapeHtml(sensor.label)}</span>
                <span class="ml-auto text-xs font-mono text-neon-green">${sensor.value || 0}°C</span>
            </div>
        `;
    }

    if (diskCount > 0) {
        html += `
            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">
                <span class="text-xs">💾</span>
                <span class="text-xs text-gray-300">${diskCount} ${t('nodes.disks', 'disks')}</span>
            </div>
        `;
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

function toggleNodeGroup(nodeId) {
    const children = document.getElementById(`node-children-${nodeId}`);
    if (children) {
        children.classList.toggle('hidden');
    }
}

function selectFanFromTree(fanId, source) {
    currentFanId = fanId;
    if (currentState && currentState.fans && currentState.fans[fanId]) {
        updateInspector(currentState.fans[fanId]);
    }
    buildNodeTree();
}

function selectNodeFan(nodeId, fanId) {
    console.log('[FanControl] Select node fan:', nodeId, fanId);
}

// ============================================================================
// CUSTOM DASHBOARD
// ============================================================================

let dashboardState = { groups: [], cards: [] };

function renderDashboard() {
    const cardsContainer = document.getElementById('dashboard-cards');
    const groupsContainer = document.getElementById('dashboard-groups');
    const emptyState = document.getElementById('dashboard-empty');

    if (!cardsContainer) return;

    const hasCards = dashboardState.cards.length > 0;
    const hasGroups = dashboardState.groups.length > 0;

    if (emptyState) {
        emptyState.classList.toggle('hidden', hasCards || hasGroups);
    }

    // Render groups
    if (groupsContainer) {
        groupsContainer.innerHTML = dashboardState.groups.map(group => `
            <div class="dashboard-group absolute border-2 border-dashed border-gray-600 rounded-lg p-2 mb-4"
                 style="left:${group.x}px; top:${group.y}px; width:${group.w}px; min-height:${group.h}px;"
                 data-group-id="${group.id}">
                <div class="flex items-center justify-between mb-2">
                    <span class="text-xs font-semibold text-gray-400">${escapeHtml(group.name)}</span>
                    <button onclick="removeGroup('${group.id}')" class="text-gray-600 hover:text-red-400 text-xs">×</button>
                </div>
            </div>
        `).join('');
    }

    // Render cards
    cardsContainer.innerHTML = dashboardState.cards.map(card => renderDashboardCard(card)).join('');
}

function renderDashboardCard(card) {
    const liveData = getCardLiveData(card);
    const sourceName = card.source === 'local' ? 'Local' : (nodesData.find(n => n.node_id === card.source)?.name || card.source);

    let bodyContent = '';
    if (card.type === 'fan') {
        bodyContent = `
            <div class="text-xl font-bold font-mono text-neon-cyan">${liveData.rpm || 0} <span class="text-xs text-gray-500">RPM</span></div>
            <div class="text-sm text-gray-400">${liveData.pct || 0}% · ${liveData.mode || 'manual'}</div>
        `;
    } else if (card.type === 'temperature') {
        const temp = liveData.value || 0;
        const color = temp > 70 ? 'text-neon-red' : temp > 50 ? 'text-neon-orange' : 'text-neon-green';
        bodyContent = `<div class="text-xl font-bold font-mono ${color}">${temp}°C</div>`;
    } else if (card.type === 'disk') {
        bodyContent = `<div class="text-xl font-bold font-mono text-neon-purple">${liveData.temp || 0}°C</div>`;
    } else {
        bodyContent = `<div class="text-xl font-bold font-mono text-neon-cyan">${liveData.value || '--'}</div>`;
    }

    return `
        <div class="dashboard-card absolute bg-cyber-card border border-cyber-accent rounded-lg overflow-hidden cursor-move"
             style="left:${card.x}px; top:${card.y}px; width:${card.w}px; height:${card.h}px;"
             data-card-id="${card.id}"
             onmousedown="startDragCard(event, '${card.id}')">
            <div class="flex items-center justify-between px-2 py-1 bg-cyber-accent border-b border-cyber-accent">
                <span class="text-xs text-gray-400 truncate">${escapeHtml(card.label)} — ${sourceName}</span>
                <button onclick="event.stopPropagation(); removeCard('${card.id}')" class="text-gray-600 hover:text-red-400 text-xs ml-1">×</button>
            </div>
            <div class="p-3">
                ${bodyContent}
            </div>
        </div>
    `;
}

// ============================================================================
// DASHBOARD DRAG AND DROP
// ============================================================================

let draggedCardId = null;
let dragOffset = { x: 0, y: 0 };

function startDragCard(event, cardId) {
    // Don't drag if clicking remove button
    if (event.target.tagName === 'BUTTON') return;

    draggedCardId = cardId;
    const card = dashboardState.cards.find(c => c.id === cardId);
    if (!card) return;

    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();

    dragOffset.x = event.clientX - rect.left - card.x;
    dragOffset.y = event.clientY - rect.top - card.y;

    document.addEventListener('mousemove', onDragCard);
    document.addEventListener('mouseup', onDropCard);
    event.preventDefault();
}

function onDragCard(event) {
    if (!draggedCardId) return;
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();

    const card = dashboardState.cards.find(c => c.id === draggedCardId);
    if (!card) return;

    card.x = Math.max(0, event.clientX - rect.left - dragOffset.x);
    card.y = Math.max(0, event.clientY - rect.top - dragOffset.y);

    const el = document.querySelector(`[data-card-id="${draggedCardId}"]`);
    if (el) {
        el.style.left = card.x + 'px';
        el.style.top = card.y + 'px';
    }
}

function onDropCard() {
    if (draggedCardId) {
        saveDashboard();
    }
    draggedCardId = null;
    document.removeEventListener('mousemove', onDragCard);
    document.removeEventListener('mouseup', onDropCard);
}

function removeCard(cardId) {
    dashboardState.cards = dashboardState.cards.filter(c => c.id !== cardId);
    saveDashboard();
    renderDashboard();
}

function removeGroup(groupId) {
    dashboardState.cards.forEach(card => {
        if (card.group_id === groupId) card.group_id = null;
    });
    dashboardState.groups = dashboardState.groups.filter(g => g.id !== groupId);
    saveDashboard();
    renderDashboard();
}

function saveDashboard() {
    fetch('/api/dashboard', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(dashboardState)
    }).catch(err => console.error('Save dashboard error:', err));
}

function loadDashboard() {
    fetch('/api/dashboard')
        .then(r => r.json())
        .then(data => {
            dashboardState = data;
            if (currentView === 'dashboard') renderDashboard();
        })
        .catch(err => console.error('Load dashboard error:', err));
}

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
        select.innerHTML += `<option value="${escapeHtml(node.node_id)}">${escapeHtml(node.name)}</option>`;
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
                { id: 'cpu_temp', label: 'CPU Temperature', extra: '' },
                { id: 'uptime', label: 'Uptime', extra: '' },
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
        ? elements.map(el => `
            <label class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">
                <input type="checkbox" value="${escapeHtml(el.id)}" data-label="${escapeHtml(el.label)}" class="picker-checkbox rounded">
                <span class="text-xs text-gray-300">${escapeHtml(el.label)}</span>
                <span class="ml-auto text-xs text-gray-500">${el.extra}</span>
            </label>
        `).join('')
        : '<div class="text-xs text-gray-500 text-center py-4">No elements found</div>';
}

function addSelectedCards() {
    const type = document.getElementById('picker-type')?.value;
    const source = document.getElementById('picker-source')?.value;
    const checkboxes = document.querySelectorAll('.picker-checkbox:checked');

    let offsetX = 20;
    let offsetY = 20;

    // Find existing cards to offset new ones
    if (dashboardState.cards.length > 0) {
        const lastCard = dashboardState.cards[dashboardState.cards.length - 1];
        offsetX = lastCard.x + lastCard.w + 20;
        offsetY = lastCard.y;
        // Wrap to next row if too wide
        if (offsetX > 600) {
            offsetX = 20;
            offsetY = lastCard.y + lastCard.h + 20;
        }
    }

    checkboxes.forEach(cb => {
        const card = {
            id: 'card-' + Date.now() + '-' + Math.random().toString(36).substr(2, 5),
            type: type,
            source: source,
            element_id: cb.value,
            label: cb.dataset.label,
            x: offsetX,
            y: offsetY,
            w: 200,
            h: 120,
            group_id: null
        };
        dashboardState.cards.push(card);
        offsetX += 220;
        if (offsetX > 600) {
            offsetX = 20;
            offsetY += 140;
        }
    });

    saveDashboard();
    renderDashboard();
    hideCardPicker();
}

function showGroupCreator() {
    const modal = document.getElementById('group-creator-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    const input = document.getElementById('group-name-input');
    if (input) { input.value = ''; input.focus(); }
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
        x: 20,
        y: dashboardState.cards.length * 150 + 20,
        w: 400,
        h: 200
    };

    dashboardState.groups.push(group);
    saveDashboard();
    renderDashboard();
    hideGroupCreator();
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

    // Toggle left panel containers
    const nodeTree = document.getElementById('node-tree-container');
    if (nodeTree) nodeTree.classList.toggle('hidden', view !== 'nodes');

    // Toggle right panel: canvas vs inspector
    const canvas = document.getElementById('dashboard-canvas-container');
    const inspector = document.getElementById('dashboard-view');
    const addBtn = document.getElementById('dashboard-add-btn');
    const groupBtn = document.getElementById('dashboard-group-btn');

    if (canvas) canvas.classList.toggle('hidden', view !== 'dashboard');
    if (inspector) inspector.classList.toggle('hidden', view === 'dashboard');
    if (addBtn) addBtn.classList.toggle('hidden', view !== 'dashboard');
    if (groupBtn) groupBtn.classList.toggle('hidden', view !== 'dashboard');

    // Update tab styles
    document.querySelectorAll('.nav-item').forEach(el => {
        const isActive = el.dataset.view === view;
        if (isActive) {
            el.classList.remove('text-gray-500', 'border-transparent');
            el.classList.add('text-neon-cyan', 'border-neon-cyan');
        } else {
            el.classList.add('text-gray-500', 'border-transparent');
            el.classList.remove('text-neon-cyan', 'border-neon-cyan');
        }
    });

    if (view === 'nodes') buildNodeTree();
    if (view === 'dashboard') renderDashboard();
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
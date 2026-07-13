/**
 * FanControl Web v3.4.1 - Neon Cyberpunk Edition
 * Main JavaScript Application
 */

import {
    store, i18n, CHART_UPDATE_INTERVAL, RELOAD_DELAY, SCHEDULE_CELL_SIZE, SPARKLINE_MAX,
    BTN_ACTIVE, BTN_INACTIVE,
    schedule, dashboard, cardDrag, cardResize, cardEdit,
    smart, groupDrag, timers, dsm, logging, update, conflict, debug, sparklineHistory,
} from './store.js';
import { escapeHtml, fanIcon, toggle, formatTemp, formatBytes, getUnitLabel, getTempColorClass, getSettings, saveSettings, showToast, dismissToast } from './utils.js';
import { loadLang, t } from './i18n.js';
import { healthIcon, buildSensorCheckboxList, setModeButtonStyles } from './render-helpers.js';
import { registerSocketHandlers } from './socket-handlers.js';
import { updateChart } from './charts.js';

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

// Register all socket event handlers centrally
registerSocketHandlers(socket, {
    showServerUnavailable,
    hideServerUnavailable,
    updateUI,
    buildServerTree,
    updateCalibrationModal,
    renderDiscoveredHardware,
    hideCalibrationModal,
    showMainScreen,
    renderUpdateAgentProgress,
    checkAgentsDone,
    renderAgentLogsModal,
    renderNodesOverview,
    loadNodeDetail,
    showConflictModal,
    showManualModeWarning,
    loadNodes,
    startCardPulse,
    stopCardPulse,
});

function showServerUnavailable() {
    const banner = document.getElementById('server-unavailable-banner');
    if (banner) banner.classList.remove('hidden');
}

function hideServerUnavailable() {
    const banner = document.getElementById('server-unavailable-banner');
    if (banner) banner.classList.add('hidden');
}

// ============================================================================
// UI UPDATE FUNCTIONS
// ============================================================================

function updateUI(data) {
    if (!data) return;
    
    // Update version displays
    const ver = data.config_version || store.state?.config_version || '';
    const headerVer = document.getElementById('header-version');
    if (headerVer && ver) headerVer.textContent = `v${ver}`;
    const versionLink = document.getElementById('version-link');
    if (versionLink && ver) versionLink.textContent = `FanControl Web v${ver}`;
    
    // Show appropriate screen — use store.state for initialized/tested check
    // to avoid flashing setup screen on partial socket updates
    const initialized = data.initialized ?? store.state?.initialized;
    const tested = data.tested ?? store.state?.tested;
    if (!initialized || !tested) {
        if (store.wasOnMainScreen) {
            return;
        }
        showSetupScreen();
        if (data.hardware_scanned && store.wizardStep === 'intro') {
            renderDiscoveredHardware({
                fans: data.fans,
                temps: data.temp_sensors,
                disks: data.hdd_sensors
            });
            store.wizardStep = 'results';
            setDiscoverButtonState(false);
        }
        return;
    }
    
    showMainScreen();
    
    // Update indicators
    updateFailsafeIndicator(data.failsafe);
    updateStandbyIndicator(data.standby_mode);
    
    // Build fan list only when empty; otherwise update in-place to preserve pulse timers
    if (data.fans && Object.keys(data.fans).length > 0) {
        const container = document.getElementById('fan-list');
        const existingCount = container ? container.querySelectorAll('.fan-card').length : 0;
        if (existingCount === 0) {
            buildFanList(data.fans);
        }
        // Always update health classes (works on both new and existing cards)
        updateFanHealthClasses(data.fans);
        // DEBUG: log fan health status
        for (const [fid, f] of Object.entries(data.fans)) {
            if (f.health && f.health.status !== 'healthy') {
                console.log(`[fan-health] ${fid}: status=${f.health.status} rpm=${f.rpm} writable=${f.writable} mode=${f.mode}`);
            }
        }
    }
    
    // Build disks list
    if (data.hdd_sensors) {
        buildDisksList(data.hdd_sensors);
    }
    
    // Build sensor list for popup
    buildSensorList(data);
    
    // Update inspector if a fan is selected
    if (store.currentFanId && data.fans && data.fans[store.currentFanId]) {
        updateInspector(data.fans[store.currentFanId]);
    }
    
    // Update chart
    updateChart();

    // Refresh server tree
    if (dashboard.loaded) buildServerTree();

    // Dashboard live updates handled by startPickerLiveUpdate
}

function showSetupScreen() {
    const setupScreen = document.getElementById('setup-screen');
    const mainScreen = document.getElementById('main-screen');
    if (setupScreen) setupScreen.classList.remove('hidden');
    if (mainScreen) mainScreen.classList.add('hidden');
    stopPickerLiveUpdate();
    stopSystemUpdate();
    // Close settings panel if open
    const overlay = document.getElementById('settings-overlay');
    const panel = document.getElementById('settings-panel');
    if (overlay) overlay.classList.add('hidden');
    if (panel) panel.classList.add('hidden');
}

function showMainScreen() {
    store.wasOnMainScreen = true;
    const mainScreen = document.getElementById('main-screen');
    const wasOnSetup = mainScreen?.classList.contains('hidden');

    document.getElementById('setup-screen')?.classList.add('hidden');
    mainScreen?.classList.remove('hidden');
    if (!store.state || !store.state.testing) {
        hideCalibrationModal();
    }
    if (wasOnSetup) showView('dashboard');
    updateCanvasColumns();
    if (wasOnSetup) {
        loadPickerCards().then(() => {
            buildServerTree();
            startPickerLiveUpdate();
            startSystemUpdate();
        });
    } else {
        if (!dashboard.liveTimer) startPickerLiveUpdate();
        startSystemUpdate();
    }
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
        const isSelected = fanId === store.currentFanId;
        const borderColor = isSelected ? 'border-neon-purple' : 'border-cyber-accent';
        const bgColor = isSelected ? 'bg-cyber-accent' : 'bg-cyber-card';
        const healthStatus = fan.health?.status || 'healthy';
        const healthClass = healthStatus === 'stopped' ? 'fan-alert-stopped' :
                            healthStatus === 'slowing' ? 'fan-alert-slowing' :
                            healthStatus === 'needs_calibration' ? 'fan-alert-needs-calibration' : '';
        // Remove transition-all when health alert is active so CSS animation works
        const transitionClass = healthClass ? '' : 'transition-all duration-200';

        html += `
            <div id="fan-card-${escapeHtml(fanId)}"
                 class="fan-card ${bgColor} border ${borderColor} ${healthClass} rounded-lg px-3 py-2.5 pb-2 cursor-pointer
                        hover:border-neon-purple ${transitionClass}"
                 onclick="selectFan('${escapeHtml(fanId)}')">
                <div class="flex items-center justify-between mb-1">
                    <span class="text-sm font-semibold text-white truncate">${escapeHtml(fan.label)}</span>
                    <div class="flex items-center gap-1">
                        ${fan.inverted ? `<span class="text-xs px-1.5 py-0.5 rounded bg-cyan-900 bg-opacity-30 text-neon-cyan">${t('fan.inv', 'INV')}</span>` : ''}
                        <span class="text-xs px-1.5 py-0.5 rounded ${getStatusBadgeClass(fan.health?.status || fan.status)}">${t('status.' + (fan.health?.status || fan.status), fan.health?.status || fan.status)}</span>
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

    // Start pulse on any cards that already have health alerts
    for (const [fanId, fan] of Object.entries(fans)) {
        const healthStatus = fan.health?.status || 'healthy';
        if (healthStatus !== 'healthy') {
            const card = document.getElementById(`fan-card-${fanId}`);
            if (card) startCardPulse(card, healthStatus);
        }
    }
}

function updateFanHealthClasses(fans) {
    const healthClasses = ['fan-alert-stopped', 'fan-alert-slowing', 'fan-alert-needs-calibration'];
    for (const [fanId, fan] of Object.entries(fans)) {
        const healthStatus = fan.health?.status || 'healthy';

        // Find all cards for this fan — both fan-card-* and picker cards
        const fanSpan = document.querySelector(`[data-fan-id="${fanId}"]`);
        const cards = [];
        if (fanSpan) {
            const pickerCard = fanSpan.closest('[data-card-id]');
            if (pickerCard) cards.push(pickerCard);
        }
        const fanCard = document.getElementById(`fan-card-${fanId}`);
        if (fanCard) cards.push(fanCard);

        for (const card of cards) {
            const hasAny = healthClasses.some(c => card.classList.contains(c));

            if (healthStatus !== 'healthy' && !hasAny) {
                card.classList.remove('transition-all', 'duration-200');
                healthClasses.forEach(c => card.classList.remove(c));
                card.classList.add(`fan-alert-${healthStatus}`);
                card.setAttribute('data-fan-health', healthStatus);
                startCardPulse(card, healthStatus);
            } else if (healthStatus === 'healthy' && hasAny) {
                healthClasses.forEach(c => card.classList.remove(c));
                card.classList.add('transition-all', 'duration-200');
                card.setAttribute('data-fan-health', 'healthy');
                stopCardPulse(card);
            }

            // Update status badge
            const badge = card.querySelector('.text-xs.px-1\\.5');
            if (badge) {
                const ds = fan.health?.status || fan.status;
                badge.textContent = t('status.' + ds, ds);
                badge.className = `text-xs px-1.5 py-0.5 rounded ${getStatusBadgeClass(ds)}`;
            }
        }
    }
}

const _cardPulseTimers = new Map();

function startCardPulse(card, status) {
    stopCardPulse(card);
    const color = status === 'stopped' ? '#ef4444' : '#facc15';
    const dim   = status === 'stopped' ? '#450a0a' : '#422006';
    let on = true;
    function tick() {
        on = !on;
        card.style.setProperty('outline', on ? `3px solid ${color}` : `3px solid ${dim}`, 'important');
        card.style.setProperty('outline-offset', '-3px', 'important');
    }
    tick(); // immediate first frame
    const timer = setInterval(tick, 750);
    _cardPulseTimers.set(card.id, timer);
    console.log(`[fan-health] pulse timer started for ${card.id}, color=${color}`);
}

function stopCardPulse(card) {
    const t = _cardPulseTimers.get(card.id);
    if (t) { clearInterval(t); _cardPulseTimers.delete(card.id); }
    card.style.removeProperty('outline');
    card.style.removeProperty('outline-offset');
}

function selectFan(fanId) {
    store.currentFanId = fanId;
    
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
    if (store.state && store.state.fans && store.state.fans[fanId]) {
        updateInspector(store.state.fans[fanId]);
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
    for (const node of store.nodesData) {
        html += renderRemoteNodeTree(node);
    }

    container.innerHTML = html || `<div class="text-center text-gray-500 py-4 text-xs">${t('nodes.no_nodes', 'No nodes connected')}</div>`;

    _collapsedNodes.forEach(nodeId => {
        const children = document.getElementById(`node-children-${nodeId}`);
        if (children) children.classList.add('hidden');
    });
}

function getHiddenSensors() {
    return dashboard.hiddenSensors || [];
}

function setHiddenSensors(hidden) {
    dashboard.hiddenSensors = hidden;
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
    if (!store.state || !store.state.fans) return '';

    const fans = store.state.fans;
    const temps = store.state.temp_sensors || {};
    const disks = store.state.hdd_sensors || {};
    const hidden = getHiddenSensors();

    const visibleFans = Object.entries(fans).filter(([id]) => !hidden.includes(`fan:${id}`));
    const visibleTemps = Object.entries(temps).filter(([id]) => !hidden.includes(`temp:${id}`));
    const visibleDisks = Object.entries(disks).filter(([id]) => !hidden.includes(`disk:${id}`));
    const hiddenFans = Object.entries(fans).filter(([id]) => hidden.includes(`fan:${id}`));
    const hiddenTemps = Object.entries(temps).filter(([id]) => hidden.includes(`temp:${id}`));
    const hiddenDisks = Object.entries(disks).filter(([id]) => hidden.includes(`disk:${id}`));
    const hasHidden = hiddenFans.length + hiddenTemps.length + hiddenDisks.length > 0;

    const serverVer = store.state?.config_version || '';

    let html = `
        <div class="node-group" data-node="local">
            <div class="p-2 rounded hover:bg-cyber-accent cursor-pointer node-header group"
                 onclick="toggleNodeGroup('local')">
                <div class="flex items-center gap-1.5">
                    <span class="w-2 h-2 bg-neon-cyan rounded-full flex-shrink-0"></span>
                    <span class="text-sm font-semibold text-white truncate flex-1">${escapeHtml(store.state.server_name || t('nodes.local_server', 'My Server'))}</span>
                    ${serverVer ? `<span class="text-[10px] text-gray-600" title="${escapeHtml(serverVer)}">${escapeHtml(serverVer)}</span>` : ''}
                    <button onclick="event.stopPropagation(); openServerNameEdit()"
                            class="w-4 h-4 flex items-center justify-center text-gray-400 hover:text-neon-cyan rounded text-[10px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Rename">✎</button>
                </div>
                <div class="flex items-center gap-2 mt-0.5 ml-3.5">
                    <span class="text-[10px] text-neon-green">online</span>
                    ${visibleFans.length > 0 ? `<span class="text-[10px] text-gray-500">· ${visibleFans.length} ${t('nodes.fans', 'fans')}</span>` : ''}
                    ${Object.keys(temps).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(temps).length} ${t('nodes.sensors', 'sensors')}</span>` : ''}
                    ${Object.keys(disks).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(disks).length} ${t('nodes.disks', 'disks')}</span>` : ''}
                </div>
            </div>
            <div class="node-children ml-4 space-y-px" id="node-children-local">
    `;

    for (const [fanId, fan] of visibleFans) {
        const isSelected = fanId === store.currentFanId;
        const fanHealth = fan.health?.status || 'healthy';
        const _healthIcon = healthIcon(fan);
        html += `
            <div data-sensor-id="fan:${escapeHtml(fanId)}" class="flex items-center gap-1.5 p-1 rounded cursor-pointer transition-all group ${isSelected ? 'bg-cyber-accent border-l-2 border-neon-purple' : 'hover:bg-cyber-accent border-l-2 border-transparent'}"
                 onclick="selectFanFromTree('${escapeHtml(fanId)}', 'local')">
                ${fanIcon(fan)}
                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(fan.label)}</span>
                ${_healthIcon}
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
                    <span class="text-[10px] text-gray-500">${t('nodes.hidden', 'Hidden')} (${totalHidden})</span>
                    <button onclick="event.stopPropagation(); restoreAllSensors()" class="ml-auto text-[10px] text-gray-600 hover:text-neon-green px-1">↺ ${t('nodes.all', 'all')}</button>
                </div>
                <div class="node-children ml-4 space-y-px ${isHiddenExpanded ? '' : 'hidden'}" id="node-children-local-hidden">
        `;

        for (const [fanId, fan] of hiddenFans) {
            html += `
                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                    <span class="opacity-50">${fanIcon(fan)}</span>
                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(fan.label)}</span>
                    <button onclick="restoreSensor('fan:${escapeHtml(fanId)}')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="${t('nodes.restore', 'Restore')}">↺</button>
                </div>
            `;
        }
        for (const [sensorId, sensor] of hiddenTemps) {
            html += `
                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                    <span class="text-xs opacity-50">🌡</span>
                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(sensor.label)}</span>
                    <button onclick="restoreSensor('temp:${escapeHtml(sensorId)}')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="${t('nodes.restore', 'Restore')}">↺</button>
                </div>
            `;
        }
        for (const [diskId, disk] of hiddenDisks) {
            html += `
                <div class="flex items-center gap-1.5 p-1 rounded hover:bg-cyber-accent group">
                    <span class="text-xs opacity-50">💾</span>
                    <span class="text-xs text-gray-500 truncate flex-1">${escapeHtml(disk.label || diskId)}</span>
                    <button onclick="restoreSensor('disk:${escapeHtml(diskId)}')" class="text-gray-600 hover:text-neon-green text-[10px] px-0.5" title="${t('nodes.restore', 'Restore')}">↺</button>
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
    const disks = telemetry.hdd_sensors || {};
    const fanCount = Object.keys(fans).length;
    const statusDot = node.status === 'online' ? 'bg-neon-green' : 'bg-gray-500';

    const serverVer = store.state?.config_version || '';
    const agentVer = node.agent_version || '';
    const updateStarted = node.update_started;

    let versionBadge = '';
    if (updateStarted) {
        const elapsed = Math.round((Date.now() / 1000) - updateStarted);
        if (elapsed > 180) {
            node.update_started = null;
        } else {
            versionBadge = `<span class="text-[10px] px-1 py-0.5 rounded bg-cyan-900/50 text-neon-cyan border border-cyan-700/50 animate-pulse" title="Updating... ${elapsed}s">⟳ ${elapsed}s</span>`;
        }
    }
    if (!versionBadge && agentVer && serverVer && agentVer !== serverVer) {
        versionBadge = `<span class="text-[10px] px-1 py-0.5 rounded bg-orange-900/50 text-orange-400 border border-orange-700/50 cursor-pointer hover:bg-orange-800/50" onclick="event.stopPropagation(); updateSingleAgent('${escapeHtml(node.node_id)}')" title="Server: ${escapeHtml(serverVer)} — ${t('nodes.click_to_update', 'click to update')}">↑ ${escapeHtml(agentVer)}</span>`;
    } else if (!versionBadge && agentVer) {
        versionBadge = `<span class="text-[10px] text-gray-600" title="${escapeHtml(agentVer)}">${escapeHtml(agentVer)}</span>`;
    }

    let html = `
        <div class="node-group" data-node="${escapeHtml(node.node_id)}">
            <div class="p-2 rounded hover:bg-cyber-accent cursor-pointer node-header group"
                 onclick="toggleNodeGroup('${escapeHtml(node.node_id)}')">
                <div class="flex items-center gap-1.5">
                    <span class="w-2 h-2 ${statusDot} rounded-full flex-shrink-0"></span>
                    <span class="text-sm font-semibold text-white truncate flex-1">${escapeHtml(node.name)}</span>
                    ${versionBadge}
                    <button onclick="event.stopPropagation(); showNodeSettings('${escapeHtml(node.node_id)}')"
                            class="w-4 h-4 flex items-center justify-center text-gray-400 hover:text-neon-cyan rounded text-[10px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Settings">&#9881;</button>
                    <button onclick="event.stopPropagation(); deleteNode('${escapeHtml(node.node_id)}')"
                            class="w-4 h-4 flex items-center justify-center text-gray-400 hover:text-red-400 rounded text-[10px] flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" title="Delete">✕</button>
                </div>
                <div class="flex items-center gap-2 mt-0.5 ml-3.5">
                    <span class="text-[10px] ${node.status === 'online' ? 'text-neon-green' : 'text-gray-500'}">${node.status}</span>
                    ${fanCount > 0 ? `<span class="text-[10px] text-gray-500">· ${fanCount} ${t('nodes.fans', 'fans')}</span>` : ''}
                    ${Object.keys(temps).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(temps).length} ${t('nodes.sensors', 'sensors')}</span>` : ''}
                    ${Object.keys(disks).length > 0 ? `<span class="text-[10px] text-gray-500">· ${Object.keys(disks).length} ${t('nodes.disks', 'disks')}</span>` : ''}
                </div>
            </div>
            <div class="node-children ml-4 space-y-0.5 ${_collapsedNodes.has(node.node_id) ? 'hidden' : ''}" id="node-children-${escapeHtml(node.node_id)}">
    `;

    for (const [fanId, fan] of Object.entries(fans)) {
        const cleanLabel = (fan.label || fanId).replace(/\s*\(Synology-[^)]+\)/, '');
        const isDsm = fan.control_method === 'dsm_scemd';
        const fanHealth = fan.health?.status || 'healthy';
        const _healthIcon = healthIcon(fan);
        html += `
            <div class="flex items-center gap-2 p-1.5 rounded cursor-pointer hover:bg-cyber-accent"
                 onclick="selectNodeFan('${escapeHtml(node.node_id)}', '${escapeHtml(fanId)}')">
                ${fanIcon(fan)}
                <span class="text-xs text-gray-300 truncate flex-1">${escapeHtml(cleanLabel)}${isDsm ? ' <span class="text-blue-400 text-[10px]">DSM</span>' : ''}</span>
                ${_healthIcon}
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

    for (const [diskId, disk] of Object.entries(disks)) {
        html += `
            <div class="flex items-center gap-2 p-1.5 rounded hover:bg-cyber-accent cursor-pointer">
                <span class="text-xs">💾</span>
                <span class="text-xs text-gray-300 truncate">${escapeHtml(disk.label || diskId)}</span>
                <span class="ml-auto text-xs font-mono ${getTempColorClass(disk.temp)}">${disk.temp > 0 ? disk.temp + '°C' : '--'}</span>
            </div>
        `;
    }

    if (fanCount === 0 && Object.keys(temps).length === 0 && Object.keys(disks).length === 0) {
        html += `<div class="text-xs text-gray-600 p-1.5">${t('node.no_telemetry', 'No telemetry')}</div>`;
    }

    html += `</div></div>`;
    return html;
}

let _collapsedNodes;
try { _collapsedNodes = new Set(JSON.parse(localStorage.getItem('fc_collapsed_nodes') || '[]')); }
catch(e) { _collapsedNodes = new Set(); }

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
    store.currentFanId = fanId;

    // Check if this is a DSM fan — open scheme editor instead of inspector
    if (store.state && store.state.fans && store.state.fans[fanId]) {
        const fan = store.state.fans[fanId];
        if (fan.control_method === 'dsm_scemd') {
            showView('dsm-scheme');
            buildServerTree();
            return;
        }
    }

    // Show inspector view
    showView('inspector');

    // Update inspector
    if (source === 'local' && store.state && store.state.fans && store.state.fans[fanId]) {
        updateInspector(store.state.fans[fanId]);
    }

    // Rebuild server tree to highlight selected
    buildServerTree();
}

function selectNodeFan(nodeId, fanId) {
    // Check if this is a DSM fan on a remote node
    const node = store.nodesData.find(n => n.node_id === nodeId);
    if (node && node.telemetry && node.telemetry.fans && node.telemetry.fans[fanId]) {
        const fan = node.telemetry.fans[fanId];
        if (fan.control_method === 'dsm_scemd') {
            store.currentRemoteNodeId = nodeId;
            showView('dsm-scheme');
            renderDsmSchemeEditor(nodeId);
            return;
        }
    }
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
    select.innerHTML = `<option value="local">${t('picker.my_server', 'My Server (local)')}</option>`;
    for (const node of store.nodesData) {
        const sourceId = node.stable_id || node.node_id;
        select.innerHTML += `<option value="${escapeHtml(sourceId)}">${escapeHtml(node.name || node.node_id)}</option>`;
    }
}

function updatePickerElements() {
    const type = document.getElementById('picker-type')?.value;
    const source = document.getElementById('picker-source')?.value;
    const container = document.getElementById('picker-elements');
    if (!container) return;

    let elements = [];

    if (source === 'local') {
        if (type === 'fan' && store.state?.fans) {
            elements = Object.entries(store.state.fans).map(([id, f]) => ({ id, label: f.label || id, extra: `${f.rpm || 0} RPM` }));
        } else if (type === 'temperature' && store.state?.temp_sensors) {
            elements = Object.entries(store.state.temp_sensors).map(([id, s]) => ({ id, label: s.label || id, extra: `${s.value || 0}°C` }));
        } else if (type === 'disk' && store.state?.hdd_sensors) {
            elements = Object.entries(store.state.hdd_sensors).map(([id, d]) => ({ id, label: d.label || id, extra: `${d.temp || 0}°C` }));
        } else if (type === 'system') {
            elements = [
                { id: 'max_temp', label: t('picker.max_temp', 'Макс. температура'), extra: `${store.state?.max_hdd_temp || '--'}°C` },
                { id: 'fans_summary', label: t('picker.fans_summary', 'Сводка по вентиляторам'), extra: '' },
            ];
        }
    } else {
        const node = store.nodesData.find(n => (n.stable_id || n.node_id) === source);
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
                <span class="ml-auto text-xs text-gray-500">${exists ? t('picker.added', 'добавлено') : el.extra}</span>
            </label>`;
        }).join('')
        : `<div class="text-xs text-gray-500 text-center py-4">${t('picker.no_elements', 'Элементы не найдены')}</div>`;
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
        const pos = findFreePosition(saved, colSpan, 1, null);
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
    let fanData = null;

    if (type === 'fan') {
        fanData = getFanData(source, sourceId);
        const fanStatus = fanData?.status || 'unknown';
        const healthStatus = fanData?.health?.status || 'healthy';
        const rpm = fanData?.rpm || 0;

        // Health-based colors override operational status
        let dotColor, fanColor;
        if (healthStatus === 'stopped') {
            dotColor = 'red'; fanColor = '#ef4444';
        } else if (healthStatus === 'slowing' || healthStatus === 'needs_calibration') {
            dotColor = 'yellow'; fanColor = '#facc15';
        } else if (fanStatus === 'running') {
            dotColor = 'green'; fanColor = '#22d3ee';
        } else {
            dotColor = 'yellow'; fanColor = '#facc15';
        }

        const animDuration = rpm > 0 ? Math.max(0.2, 2 - (rpm / 1500)) : 0;
        const animStyle = rpm > 0 ? `animation: fan-spin ${animDuration}s linear infinite` : '';
        icon = `<svg class="w-8 h-8 inline-block" data-fan-anim-id="${sourceId}" data-fan-source="${source}" viewBox="0 0 100 100" style="${animStyle}">
            <g fill="${fanColor}" opacity="0.9">
                <path d="M50 50 Q30 20 50 5 Q70 20 50 50"/>
                <path d="M50 50 Q80 30 95 50 Q80 70 50 50"/>
                <path d="M50 50 Q70 80 50 95 Q30 80 50 50"/>
                <path d="M50 50 Q20 70 5 50 Q20 30 50 50"/>
            </g>
            <circle cx="50" cy="50" r="6" fill="${fanColor}" opacity="0.6"/>
        </svg> <span class="status-dot ${dotColor}"></span>`;
        colorClass = 'text-neon-cyan';
        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-fan-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">RPM</span></div>`;
        valueHtml += renderSparkline(`fan:${source}:${sourceId}`, '#22d3ee');
    } else if (type === 'temperature') {
        icon = '🌡';
        colorClass = 'text-neon-green';
        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-temp-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">°C</span></div>`;
        valueHtml += renderSparkline(`temp:${source}:${sourceId}`, '#4ade80');
    } else if (type === 'disk') {
        icon = '💾';
        colorClass = 'text-neon-purple';
        valueHtml = `<div class="flex items-baseline gap-2"><span class="text-2xl font-bold font-mono ${colorClass}" data-disk-id="${sourceId}" data-source="${source}">--</span><span class="text-xs text-gray-500">°C</span></div>`;
        valueHtml += renderSparkline(`disk:${source}:${sourceId}`, '#c084fc');
    } else if (type === 'system') {
        icon = '🖥';
        colorClass = 'text-yellow-400';
        valueHtml = `
        <div class="space-y-2 mt-1">
            <div class="flex justify-between text-xs">
                <span class="text-gray-500">Uptime</span>
                <span class="text-gray-300 font-mono" data-system-field="uptime">--</span>
            </div>
            <div>
                <div class="flex justify-between text-xs mb-1">
                    <span class="text-gray-500">CPU</span>
                    <span class="text-gray-300 font-mono" data-system-field="cpu">--%</span>
                </div>
                <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">
                    <div class="h-full bg-cyan-400 rounded-full transition-all duration-500" data-system-bar="cpu" style="width:0%"></div>
                </div>
            </div>
            <div>
                <div class="flex justify-between text-xs mb-1">
                    <span class="text-gray-500">RAM</span>
                    <span class="text-gray-300 font-mono" data-system-field="mem">--%</span>
                </div>
                <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">
                    <div class="h-full bg-purple-400 rounded-full transition-all duration-500" data-system-bar="mem" style="width:0%"></div>
                </div>
            </div>
        </div>`;
    } else {
        valueHtml = `<div class="text-2xl font-bold font-mono text-neon-cyan">--</div>`;
    }

    const configBtn = type === 'fan'
        ? `<button onclick="event.stopPropagation(); showCardConfig('${id}')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Configure">⚙</button>`
        : type === 'disk'
        ? `<button onclick="event.stopPropagation(); showSmartModal('${id}')" class="text-gray-600 hover:text-neon-purple text-xs transition-colors" title="SMART">⚙</button>`
            + `<button onclick="event.stopPropagation(); showSmartHistory('${id}')" class="text-gray-600 hover:text-neon-green text-xs transition-colors" title="History">📈</button>`
            + `<button onclick="event.stopPropagation(); showCardConfig('${id}')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Config">🔧</button>`
        : '';
    const lockIcon = card.lockSize ? '🔒' : '🔓';
    const lockClass = card.lockSize ? 'text-neon-cyan' : 'text-gray-600';
    const lockBtn = `<button onclick="event.stopPropagation(); toggleCardLockSize('${id}')" class="lock-size-btn ${lockClass} hover:text-neon-cyan text-xs transition-colors" title="Lock/Unlock size">${lockIcon}</button>`;
    const editBtn = `<button onclick="event.stopPropagation(); showCardEdit('${id}')" class="text-gray-600 hover:text-neon-cyan text-xs transition-colors" title="Edit name">✎</button>`;
    const removeBtn = `<button onclick="event.stopPropagation(); removePickerCard('${id}')" class="text-gray-600 hover:text-red-400 text-xs transition-colors">×</button>`;

    const el = document.createElement('div');
    const gradientClass = `card-gradient-${type}`;
    // Add health alert class and start pulse for fan cards
    const healthAlertClass = (type === 'fan' && fanData?.health?.status && fanData.health.status !== 'healthy')
        ? `fan-alert-${fanData.health.status}` : '';
    el.className = `border border-cyber-accent rounded-xl p-4 transition-[border-color,box-shadow,background-image] duration-200 hover:border-neon-cyan/50 hover:shadow-neon-cyan/10 hover:shadow-lg cursor-grab active:cursor-grabbing ${gradientClass} ${healthAlertClass}`;
    el.setAttribute('data-card-id', id);
    el.setAttribute('data-fan-health', type === 'fan' ? (fanData?.health?.status || 'healthy') : '');
    el.innerHTML = `
        <div class="card-content overflow-hidden h-full flex flex-col">
            <div class="flex items-center justify-between mb-1">
                <div class="flex items-center gap-2">
                    <span class="text-gray-600 text-xs select-none">⠿</span>
                    <span class="text-lg">${icon}</span>
                    <span class="text-sm text-gray-300 font-medium truncate">${escapeHtml(label)}</span>
                </div>
            <div class="flex items-center gap-1">
                ${configBtn}${lockBtn}${editBtn}${removeBtn}
            </div>
            </div>
            ${valueHtml}
            <div class="card-details flex-1 overflow-y-auto min-h-0"></div>
        </div>
        <div class="card-resize-handle"></div>`;

    el.addEventListener('mousedown', onCardMouseDown);

    if (!card.col || !card.row) {
        const saved = getPickerCards().filter(c => c.id !== card.id);
        const pos = findFreePosition(saved, card.colSpan || 3, 1, card.id);
        card.col = pos.col;
        card.row = pos.row;
    }
    el.style.gridColumn = `${card.col} / span ${card.colSpan || 3}`;
    el.style.gridRow = `${card.row} / span ${card.rowSpan || 1}`;
    el.style.position = 'relative';
    el.style.alignSelf = 'stretch';
    el.style.minWidth = '0';

    canvas.appendChild(el);

    // Start pulse for fan cards with health alerts
    if (type === 'fan' && healthAlertClass) {
        startCardPulse(el, fanData.health.status);
    }

    const resizeHandle = el.querySelector('.card-resize-handle');
    if (resizeHandle) {
        resizeHandle.addEventListener('mousedown', (e) => onCardResizeStart(e, id));
        if (card.lockSize) resizeHandle.style.display = 'none';
    }
    if (card.lockSize) el.style.cursor = 'default';

    if (type === 'disk') {
        el.addEventListener('click', (e) => {
            if (cardDrag.occurred || e.target.closest('button')) return;
            showSmartModal(id);
        });
    }

    updateCardDetails(id);
}

function snapCardToGrid(cardEl) {
    const cardId = cardEl.dataset?.cardId;
    if (!cardId) return;
    if (cardDrag.mouseDown?.cardId === cardId || cardResize.resizing?.cardId === cardId) return;
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;
    const current = card.rowSpan || 1;
    const needed = computeMinRows(cardEl);

    if (needed !== current) {
        const delta = needed - current;
        const oldBottom = card.row + current;
        const cardColStart = card.col || 1;
        const cardColEnd = cardColStart + (card.colSpan || 3) - 1;
        card.rowSpan = needed;
        cardEl.style.gridRow = `${card.row} / span ${needed}`;

        for (const c of saved) {
            if (c.id === card.id || !c.col || !c.row) continue;
            const cColStart = c.col;
            const cColEnd = cColStart + (c.colSpan || 3) - 1;
            if (c.row >= oldBottom && cColStart <= cardColEnd && cColEnd >= cardColStart) {
                c.row += delta;
                const el = document.querySelector(`[data-card-id="${c.id}"]`);
                if (el) el.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;
            }
        }

        setPickerCards(saved);
    }
}

function toggleCardLockSize(cardId) {
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;
    card.lockSize = !card.lockSize;
    setPickerCards(saved);
    const el = document.querySelector(`[data-card-id="${cardId}"]`);
    if (!el) return;
    const btn = el.querySelector('.lock-size-btn');
    if (btn) {
        btn.textContent = card.lockSize ? '🔒' : '🔓';
        btn.className = card.lockSize
            ? 'lock-size-btn text-neon-cyan hover:text-neon-cyan text-xs transition-colors'
            : 'lock-size-btn text-gray-600 hover:text-neon-cyan text-xs transition-colors';
    }
    const handle = el.querySelector('.card-resize-handle');
    if (handle) handle.style.display = card.lockSize ? 'none' : '';
    el.style.cursor = card.lockSize ? 'default' : 'grab';
}
function computeMinRows(el) {
    const contentEl = el.querySelector('.card-content');
    el.style.alignSelf = 'start';
    if (contentEl) { contentEl.style.height = 'auto'; contentEl.style.overflow = 'visible'; }
    void el.offsetHeight;
    const contentH = contentEl ? contentEl.scrollHeight : 0;
    const padV = parseFloat(getComputedStyle(el).paddingTop) + parseFloat(getComputedStyle(el).paddingBottom);
    el.style.alignSelf = 'stretch';
    if (contentEl) { contentEl.style.height = ''; contentEl.style.overflow = ''; }
    for (let r = 1; r <= 10; r++) {
        if (contentH <= r * 100 - padV - 2 + 10) return r;
    }
    return 10;
}

function onCardResizeStart(e, cardId) {
    e.preventDefault();
    e.stopPropagation();
    const el = document.querySelector(`[data-card-id="${cardId}"]`);
    if (!el) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (card?.lockSize) return;

    cardResize.minRowSpan = computeMinRows(el);

    cardResize.resizing = { cardId, el, col: card?.col, row: card?.row };
    cardResize.startX = e.clientX;
    cardResize.startY = e.clientY;
    cardResize.startW = el.offsetWidth;
    cardResize.startH = el.offsetHeight;

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
    if (!cardResize.resizing) return;
    const el = cardResize.resizing.el;
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return;

    const dx = e.clientX - cardResize.startX;
    const dy = e.clientY - cardResize.startY;
    const cols = getCanvasCols();
    const gap = 8;
    const padL = parseInt(getComputedStyle(canvas).paddingLeft) || 16;
    const padR = parseInt(getComputedStyle(canvas).paddingRight) || 16;
    const contentW = canvas.offsetWidth - padL - padR;
    const colWidth = (contentW - (cols - 1) * gap) / cols;
    const rowHeight = 100;
    const rowStep = rowHeight + gap;

    const newW = cardResize.startW + dx;
    const newH = cardResize.startH + dy;
    const newColSpan = Math.max(2, Math.min(cols, Math.round(newW / (colWidth + gap))));
    const newRowSpan = Math.max(cardResize.minRowSpan, Math.min(8, Math.round(newH / rowStep)));

    el.style.gridColumn = `${cardResize.resizing.col || 'auto'} / span ${newColSpan}`;
    el.style.gridRow = `${cardResize.resizing.row || 'auto'} / span ${newRowSpan}`;
    el._resizeColSpan = newColSpan;
    el._resizeRowSpan = newRowSpan;
}

function onCardResizeEnd(e) {
    if (!cardResize.resizing) return;
    const el = cardResize.resizing.el;
    const cardId = cardResize.resizing.cardId;

    let colSpan = el._resizeColSpan || 3;
    let rowSpan = el._resizeRowSpan || 1;

    document.body.style.cursor = '';
    document.body.style.userSelect = '';

    document.removeEventListener('mousemove', onCardResizeMove);
    document.removeEventListener('mouseup', onCardResizeEnd);

    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (card) {
        if (rowSpan < cardResize.minRowSpan) rowSpan = cardResize.minRowSpan;
        const cols = getCanvasCols();
        if (card.col + colSpan - 1 > cols) colSpan = cols - card.col + 1;

        card.colSpan = colSpan;
        card.rowSpan = rowSpan;
        resolveOverlaps(saved, cardId);

        for (const c of saved) {
            if (c.id === cardId) continue;
            const el2 = document.querySelector(`[data-card-id="${c.id}"]`);
            if (el2) {
                el2.style.gridColumn = `${c.col} / span ${c.colSpan || 3}`;
                el2.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;
            }
        }
        el.style.gridColumn = `${card.col} / span ${colSpan}`;
        el.style.gridRow = `${card.row} / span ${rowSpan}`;
        setPickerCards(saved);
    }

    cardResize.resizing = null;
    cardDrag.occurred = true;
    setTimeout(() => { cardDrag.occurred = false; }, 200);
    updateCanvasMinHeight();
}


function _computeGridCache() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return null;
    const style = getComputedStyle(canvas);
    const padL = parseFloat(style.paddingLeft) || 16;
    const padT = parseFloat(style.paddingTop) || 16;
    const padR = parseFloat(style.paddingRight) || 16;
    const contentW = canvas.offsetWidth - padL - padR;
    const cols = parseInt(style.gridTemplateColumns?.split(' ')?.length || 12);
    const gap = parseFloat(style.gap) || 8;
    const colW = (contentW - (cols - 1) * gap) / cols;
    const rowH = 100;
    return { cols, padL, padT, padR, gap, colW, rowH };
}

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
    if (card.lockSize) return;

    const rect = cardEl.getBoundingClientRect();
    const offsetX = e.clientX - rect.left;
    const offsetY = e.clientY - rect.top;

    const gridColMatch = cardEl.style.gridColumn?.match(/(\d+)\s*\/\s*span\s+(\d+)/);
    const gridRowMatch = cardEl.style.gridRow?.match(/(\d+)\s*\/\s*span\s+(\d+)/);
    const domColSpan = gridColMatch ? parseInt(gridColMatch[2]) : (card.colSpan || 3);
    const domRowSpan = gridRowMatch ? parseInt(gridRowMatch[2]) : (card.rowSpan || 1);
    const domCol = gridColMatch ? parseInt(gridColMatch[1]) : (card.col || 1);
    const domRow = gridRowMatch ? parseInt(gridRowMatch[1]) : (card.row || 1);

    cardDrag.mouseDown = {
        cardId, cardEl, card,
        startX: e.clientX, startY: e.clientY,
        offsetX, offsetY, dragging: false,
        colSpan: domColSpan,
        rowSpan: domRowSpan,
        cardCol: domCol,
        cardRow: domRow
    };
    cardDrag.gridCache = _computeGridCache();

    console.log(`[DOWN] card=${cardId} pos(col=${card.col},row=${card.row}) span(col=${card.colSpan||3},row=${card.rowSpan||1}) offset(X=${Math.round(offsetX)},Y=${Math.round(offsetY)}) cardRect(left=${Math.round(rect.left)},top=${Math.round(rect.top)},w=${Math.round(rect.width)},h=${Math.round(rect.height)})`);

    document.addEventListener('mousemove', onCardMouseMove);
    document.addEventListener('mouseup', onCardMouseUp);
}

function onCardMouseMove(e) {
    if (!cardDrag.mouseDown) return;
    const dx = Math.abs(e.clientX - cardDrag.mouseDown.startX);
    const dy = Math.abs(e.clientY - cardDrag.mouseDown.startY);
    if (!cardDrag.mouseDown.dragging && (dx < 4 && dy < 4)) return;

    if (!cardDrag.mouseDown.dragging) {
        cardDrag.mouseDown.dragging = true;
        cardDrag.mouseDown.cardEl.classList.add('opacity-40');
        cardDrag.occurred = true;

        const canvas = document.getElementById('dashboard-canvas');
        const cs = getComputedStyle(canvas);
        const padL = parseFloat(cs.paddingLeft) || 16;
        const padT = parseFloat(cs.paddingTop) || 16;
        const padR = parseFloat(cs.paddingRight) || 16;
        const contentW = canvas.offsetWidth - padL - padR;
        const cols = getCanvasCols();
        const gap = 8;
        const colW = (contentW - (cols - 1) * gap) / cols;
        const cardW = cardDrag.mouseDown.cardEl.offsetWidth;
        const rowH = 100;
        const rowStep = rowH + gap;
        cardDrag.mouseDown.gridSnapshot = {
            padL, padT, cardW, cardElH: cardDrag.mouseDown.cardEl.offsetHeight, cols, gap, colW, rowH, rowStep,
            canvasLeft: canvas.getBoundingClientRect().left,
            canvasTop: canvas.getBoundingClientRect().top
        };

        cardDrag.dragClone = cardDrag.mouseDown.cardEl.cloneNode(true);
        cardDrag.dragClone.classList.remove('opacity-40');
        cardDrag.dragClone.style.cssText = `
            position:fixed;z-index:10000;pointer-events:none;
            width:${cardDrag.mouseDown.cardEl.offsetWidth}px;
            height:${cardDrag.mouseDown.cardEl.offsetHeight}px;
            opacity:0.85;
            box-shadow:0 8px 32px rgba(0,0,0,0.4);
            transition:none;
            overflow:hidden;
        `;
        document.body.appendChild(cardDrag.dragClone);
    }

    const cloneW = cardDrag.mouseDown.cardEl.offsetWidth;
    const cloneH = cardDrag.mouseDown.cardEl.offsetHeight;
    cardDrag.dragClone.style.left = (e.clientX - cardDrag.mouseDown.offsetX) + 'px';
    cardDrag.dragClone.style.top = (e.clientY - cardDrag.mouseDown.offsetY) + 'px';

    const canvas = document.getElementById('dashboard-canvas');
    const card = cardDrag.mouseDown.card;
    const colSpan = cardDrag.mouseDown.colSpan;
    const rowSpan = cardDrag.mouseDown.rowSpan;
    const cols = getCanvasCols();
    const snap = cardDrag.mouseDown.gridSnapshot;

    const cardCol = cardDrag.mouseDown.cardCol;
    const cardRow = cardDrag.mouseDown.cardRow;

    const cardLeft = snap.canvasLeft + snap.padL + (cardCol - 1) * (snap.colW + snap.gap);
    const cardTop = snap.canvasTop + snap.padT + (cardRow - 1) * snap.rowStep;
    const cardWidth = snap.cardW || (colSpan * snap.colW + (colSpan - 1) * snap.gap);
    const cardHeight = snap.cardElH || (rowSpan * snap.rowStep - snap.gap);
    const cardCenterX = cardLeft + cardWidth / 2;
    const cardCenterY = cardTop + cardHeight / 2;
    const halfW = cardWidth / 2;
    const halfH = cardHeight / 2;

    const relX = e.clientX - cardCenterX;
    const relY = e.clientY - cardCenterY;

    let newCol, newRow;
    if (Math.abs(relX) <= halfW) {
        newCol = cardCol;
    } else {
        const offset = e.clientX - snap.canvasLeft - snap.padL;
        newCol = Math.max(1, Math.min(cols - colSpan + 1, Math.floor(offset / (snap.colW + snap.gap)) + 1));
    }
    if (Math.abs(relY) <= halfH) {
        newRow = cardRow;
    } else {
        const offset = e.clientY - snap.canvasTop - snap.padT;
        newRow = Math.max(1, Math.floor(offset / snap.rowStep) + 1);
    }
    const occupied = isCellOccupied(newCol, newRow, colSpan, rowSpan, card.id);

    if (!cardDrag.dropPreview) {
        cardDrag.dropPreview = document.createElement('div');
        cardDrag.dropPreview.style.cssText = 'position:fixed;pointer-events:none;z-index:9999;border:2px dashed #06b6d4;border-radius:12px;transition:none;background:rgba(6,182,212,0.08);';
        document.body.appendChild(cardDrag.dropPreview);
    }

    cardDrag.dropPreview.style.left = (snap.canvasLeft + snap.padL + (newCol - 1) * (snap.colW + snap.gap)) + 'px';
    cardDrag.dropPreview.style.top = (snap.canvasTop + snap.padT + (newRow - 1) * snap.rowStep) + 'px';
    cardDrag.dropPreview.style.width = (colSpan * snap.colW + (colSpan - 1) * snap.gap) + 'px';
    cardDrag.dropPreview.style.height = (rowSpan * snap.rowStep - snap.gap) + 'px';
    cardDrag.dropPreview.style.borderColor = occupied ? '#ef4444' : '#06b6d4';
    cardDrag.dropPreview.style.background = occupied ? 'rgba(239,68,68,0.08)' : 'rgba(6,182,212,0.08)';
    cardDrag.dropPreview.style.display = 'block';

    cardDrag.dropTarget = { col: newCol, row: newRow, occupied };

    console.log(`[MOVE] card=${card.id} stored(col=${cardCol},row=${cardRow}) span(${colSpan}x${rowSpan}) relX=${Math.round(relX)},relY=${Math.round(relY)} halfW=${Math.round(halfW)},halfH=${Math.round(halfH)} → new(col=${newCol},row=${newRow}) occ=${occupied}`);

    const groupEl = document.elementFromPoint(e.clientX, e.clientY)?.closest('[data-group-id]');
    document.querySelectorAll('[data-group-id].drag-hover').forEach(el => el.classList.remove('drag-hover'));
    if (groupEl && !groupEl.contains(cardDrag.mouseDown.cardEl)) {
        groupEl.classList.add('drag-hover');
        groupEl.style.borderColor = '#a855f7';
        groupEl.style.background = 'rgba(168,85,247,0.1)';
    }
}

function onCardMouseUp(e) {
    document.removeEventListener('mousemove', onCardMouseMove);
    document.removeEventListener('mouseup', onCardMouseUp);

    if (cardDrag.dragClone) {
        cardDrag.dragClone.remove();
        cardDrag.dragClone = null;
    }
    if (cardDrag.dropPreview) {
        cardDrag.dropPreview.style.display = 'none';
    }

    document.querySelectorAll('[data-group-id].drag-hover').forEach(el => {
        el.classList.remove('drag-hover');
        el.style.borderColor = '';
        el.style.background = '';
    });

    if (!cardDrag.mouseDown) return;

    const { cardEl, card, dragging } = cardDrag.mouseDown;
    const totalDx = Math.abs(e.clientX - cardDrag.mouseDown.startX);
    const totalDy = Math.abs(e.clientY - cardDrag.mouseDown.startY);
    if (totalDx > 2 || totalDy > 2) cardDrag.occurred = true;
    cardEl.classList.remove('opacity-40');

    if (dragging && cardDrag.dropTarget) {
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
                    let newCol = cardDrag.dropTarget.col;
                    let newRow = cardDrag.dropTarget.row;
                    const colSp = cardData.colSpan || 3;
                    const rowSp = cardData.rowSpan || 1;
                    const cols = getCanvasCols();
                    if (newCol + colSp - 1 > cols) newCol = cols - colSp + 1;
                    cardData._isDrag = true;
                    cardData.col = newCol;
                    cardData.row = newRow;
                    resolveOverlaps(saved, card.id);
                console.log(`[DROP] card=${card.id} from(col=${oldCol},row=${oldRow}) target(col=${newCol},row=${newRow})`);
                for (const c of saved) {
                    const el2 = document.querySelector(`[data-card-id="${c.id}"]`);
                    if (el2) {
                        el2.style.gridColumn = `${c.col} / span ${c.colSpan || 3}`;
                        el2.style.gridRow = `${c.row} / span ${c.rowSpan || 1}`;
                    }
                }
                setPickerCards(saved);
                updateCanvasMinHeight();
            }
        }
    }

    cardDrag.mouseDown = null;
    cardDrag.dropTarget = null;
    cardDrag.gridCache = null;
    setTimeout(() => { cardDrag.occurred = false; }, 200);
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
        const g = cardDrag.gridCache || _computeGridCache();
        if (g) {
            const ne = col + colSpan - 1;
            const nr = row + rowSpan - 1;
            for (const gEl of canvas.querySelectorAll('[data-group-id]')) {
                const rect = gEl.getBoundingClientRect();
                const cRect = canvas.getBoundingClientRect();
                const gColStart = Math.max(1, Math.round((rect.left - cRect.left - g.padL) / (g.colW + g.gap)) + 1);
                const gColEnd = Math.max(gColStart, Math.round((rect.right - cRect.left - g.padL) / (g.colW + g.gap)));
                const gRowStart = Math.max(1, Math.round((rect.top - cRect.top - g.padT) / (g.rowH + g.gap)) + 1);
                const gRowEnd = Math.max(gRowStart, Math.round((rect.bottom - cRect.top - g.padT) / (g.rowH + g.gap)));
                if (col <= gColEnd && ne >= gColStart && row <= gRowEnd && nr >= gRowStart) return true;
            }
        }
    }
    return false;
}

function resolveOverlaps(saved, cardId) {
    const cols = getCanvasCols();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;
    delete card._isDrag;

    function overlaps(a, b) {
        if (!a.col || !a.row || !b.col || !b.row) return false;
        const aCe = a.col + (a.colSpan || 3) - 1, aRe = a.row + (a.rowSpan || 1) - 1;
        const bCe = b.col + (b.colSpan || 3) - 1, bRe = b.row + (b.rowSpan || 1) - 1;
        return a.col <= bCe && aCe >= b.col && a.row <= bRe && aRe >= b.row;
    }

    function pushRight(anchor, target) {
        const anchorCe = anchor.col + (anchor.colSpan || 3) - 1;
        target.col = anchorCe + 1;
    }

    const affected = new Set([cardId]);
    let iter = 0;
    let changed = true;
    while (changed && iter < 50) {
        changed = false;
        iter++;
        for (const c of saved) {
            if (!c.col || !c.row || affected.has(c.id)) continue;
            for (const aId of affected) {
                const a = saved.find(x => x.id === aId);
                if (a && overlaps(a, c)) {
                    pushRight(a, c);
                    affected.add(c.id);
                    changed = true;
                    break;
                }
            }
        }
    }
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
function removePickerCard(cardId) {
    const el = document.querySelector(`[data-card-id="${cardId}"]`);
    if (el) el.remove();
    const saved = getPickerCards().filter(c => c.id !== cardId);
    setPickerCards(saved);
    if (!saved.length) document.getElementById('dashboard-empty')?.classList.remove('hidden');
    updateCanvasMinHeight();
}

function showCardEdit(cardId) {
    cardEdit.editingCardId = cardId;
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
    cardEdit.editingCardId = null;
}

function saveCardEdit() {
    if (!cardEdit.editingCardId) return;

    const label = document.getElementById('card-edit-label').value.trim();
    if (!label) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardEdit.editingCardId);
    if (!card) return;

    card.label = label;
    setPickerCards(saved);

    const cardEl = document.querySelector(`[data-card-id="${cardEdit.editingCardId}"]`);
    if (cardEl) {
        const labelEl = cardEl.querySelector('.text-sm.text-gray-300');
        if (labelEl) labelEl.textContent = label;
    }

    hideCardEdit();
}

function showCardConfig(cardId) {
    cardEdit.configuringCardId = cardId;
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    if (card.type === 'disk') {
        showDiskCardConfig(card);
        return;
    }
    if (card.type !== 'fan') return;

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
    cardEdit.configuringCardId = null;
}

function showDiskCardConfig(card) {
    const modal = document.getElementById('card-config-modal');
    const container = document.getElementById('card-config-options');
    const titleEl = modal.querySelector('h3');
    const isMonitored = card.monitoring === true;

    if (titleEl) titleEl.textContent = t('smart.monitoring', 'Мониторинг SMART');

    container.innerHTML = `
        <label class="flex items-center gap-3 p-2 rounded hover:bg-cyber-accent cursor-pointer">
            <input type="checkbox" id="disk-monitoring-toggle" ${isMonitored ? 'checked' : ''}
                   class="rounded border-gray-600 bg-cyber-bg text-neon-cyan focus:ring-neon-cyan"
                   onchange="toggleDiskMonitoring('${card.id}', this.checked)">
            <span class="text-sm text-gray-300">${t('smart.monitoring', 'Мониторинг SMART')}</span>
        </label>
        <div class="text-[10px] text-gray-500 ml-8 mt-1">${t('smart.monitoring.desc', 'Запись значений каждые 5 минут')}</div>
    `;

    modal.classList.remove('hidden');
}

window.toggleDiskMonitoring = async function(cardId, enabled) {
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    card.monitoring = enabled;
    setPickerCards(saved);
    saveDashboardToServer();

    try {
        await fetch('/api/smart/monitor', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ disk_id: card.sourceId, enable: enabled })
        });
        showToast(enabled
            ? t('smart.monitoring.on', 'Мониторинг включён')
            : t('smart.monitoring.off', 'Мониторинг выключен'));
    } catch (e) {
        console.error('Failed to toggle monitoring:', e);
    }
};

// ============================================================================
// SMART HISTORY
// ============================================================================

let smartHistory = { cardId: null, diskId: null, source: null, attrKey: null, range: '1d', chart: null };

window.showSmartHistory = function(cardId) {
    const saved = getPickerCards();
    const card = saved.find(c => c.id === cardId);
    if (!card) return;

    smartHistory.cardId = cardId;
    smartHistory.diskId = card.sourceId;
    smartHistory.source = card.source || 'local';
    smartHistory.attrKey = null;
    smartHistory.range = '1d';

    document.getElementById('smart-history-title').textContent =
        `SMART — ${card.label || card.sourceId}`;

    // Populate attribute selector
    const select = document.getElementById('smart-history-attr');
    const attrs = card.smartAttributes || [];
    const cachedSmart = smart.cache?.[`${card.source || 'local'}:${card.sourceId}`];

    let optionsHtml = '';
    for (const key of attrs) {
        let label = key;
        if (cachedSmart) {
            if (cachedSmart.attr_type === 'sata' && cachedSmart.attributes) {
                const attr = cachedSmart.attributes.find(a => String(a.id) === key);
                if (attr) label = attr.description || attr.name || key;
            } else if (cachedSmart.attributes?.[key]) {
                label = cachedSmart.attributes[key].description || key;
            }
        }
        optionsHtml += `<option value="${key}">${label}</option>`;
    }
    select.innerHTML = optionsHtml;
    smartHistory.attrKey = attrs[0] || null;

    // Highlight active range button
    updateRangeButtons('1d');

    document.getElementById('smart-history-modal').classList.remove('hidden');

    // Load start date and chart
    loadSmartHistoryStartDate();
    loadSmartHistoryData();
};

window.hideSmartHistory = function() {
    document.getElementById('smart-history-modal').classList.add('hidden');
    if (smartHistory.chart) {
        smartHistory.chart.destroy();
        smartHistory.chart = null;
    }
};

window.setSmartHistoryRange = function(range) {
    smartHistory.range = range;
    updateRangeButtons(range);
    loadSmartHistoryData();
};

function updateRangeButtons(active) {
    document.querySelectorAll('#smart-history-range-btns .range-btn').forEach(btn => {
        if (btn.dataset.range === active) {
            btn.className = 'range-btn px-2 py-0.5 text-[10px] rounded bg-neon-cyan text-black font-semibold';
        } else {
            btn.className = 'range-btn px-2 py-0.5 text-[10px] rounded bg-cyber-accent text-gray-400 hover:text-white';
        }
    });
}

window.loadSmartHistoryData = async function() {
    const select = document.getElementById('smart-history-attr');
    smartHistory.attrKey = select?.value;
    if (!smartHistory.attrKey || !smartHistory.diskId) return;

    const now = new Date();
    let fromTs = null;
    switch (smartHistory.range) {
        case '1m':  fromTs = new Date(now - 60 * 1000); break;
        case '10m': fromTs = new Date(now - 10 * 60 * 1000); break;
        case '30m': fromTs = new Date(now - 30 * 60 * 1000); break;
        case '1h':  fromTs = new Date(now - 60 * 60 * 1000); break;
        case '1d':  fromTs = new Date(now - 24 * 60 * 60 * 1000); break;
        case '1w':  fromTs = new Date(now - 7 * 24 * 60 * 60 * 1000); break;
        case '1M':  fromTs = new Date(now - 30 * 24 * 60 * 60 * 1000); break;
        case 'all': fromTs = null; break;
    }

    const params = new URLSearchParams({ attr: smartHistory.attrKey });
    if (fromTs) params.set('from', fromTs.toISOString());

    try {
        const resp = await fetch(`/api/smart/history/${smartHistory.diskId}?${params}`);
        const data = await resp.json();
        renderSmartHistoryChart(data.history || []);
    } catch (e) {
        console.error('Failed to load SMART history:', e);
    }
};

function loadSmartHistoryStartDate() {
    fetch(`/api/smart/history/${smartHistory.diskId}/start`)
        .then(r => r.json())
        .then(data => {
            const el = document.getElementById('smart-history-start-date');
            if (data.start_date) {
                const d = new Date(data.start_date);
                el.textContent = `${t('smart.history.started', 'Мониторинг с')}: ${d.toLocaleDateString('ru-RU')} ${d.toLocaleTimeString('ru-RU')}`;
                el.classList.remove('hidden');
            } else {
                el.textContent = t('smart.history.not_started', 'Мониторинг не запущен');
                el.classList.add('hidden');
            }
        })
        .catch(() => {});
}

function renderSmartHistoryChart(history) {
    const chartEl = document.getElementById('smart-history-chart');
    const emptyEl = document.getElementById('smart-history-empty');

    if (!history.length) {
        chartEl.innerHTML = '';
        emptyEl.classList.remove('hidden');
        return;
    }
    emptyEl.classList.add('hidden');

    const chartData = history.map(p => ({ x: new Date(p.ts).getTime(), y: p.value }));

    if (smartHistory.chart) {
        smartHistory.chart.destroy();
    }

    smartHistory.chart = new ApexCharts(chartEl, {
        chart: {
            type: 'line',
            height: 250,
            background: 'transparent',
            foreColor: '#9ca3af',
            toolbar: { show: false },
            animations: { enabled: true, easing: 'easeinout', speed: 500 }
        },
        theme: { mode: 'dark' },
        stroke: { curve: 'smooth', width: 2 },
        series: [{ name: smartHistory.attrKey, data: chartData }],
        xaxis: {
            type: 'datetime',
            labels: { style: { colors: '#6b7280', fontSize: '10px' } },
            axisBorder: { color: '#374151' }
        },
        yaxis: {
            labels: { style: { colors: '#6b7280', fontSize: '10px' } }
        },
        grid: { borderColor: '#1f2937' },
        colors: ['#30d158'],
        tooltip: {
            theme: 'dark',
            x: { format: 'dd MMM HH:mm' }
        }
    });
    smartHistory.chart.render();
}

async function fetchDiskSmart(diskId, forceRefresh = false, source = 'local', nodeId = null) {
    try {
        let url;
        if (source === 'local') {
            url = forceRefresh
                ? `/api/disks/${diskId}/smart?refresh=1`
                : `/api/disks/${diskId}/smart`;
        } else {
            url = forceRefresh
                ? `/api/nodes/${source}/disks/${diskId}/smart?refresh=1`
                : `/api/nodes/${source}/disks/${diskId}/smart`;
        }
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

    smart.modalCardId = cardId;
    smart.modalDiskId = card.sourceId;
    smart.modalSource = card.source || 'local';

    let disk;
    if (smart.modalSource === 'local') {
        disk = store.state?.hdd_sensors?.[card.sourceId];
    } else {
        const node = findNode(smart.modalSource);
        disk = node?.telemetry?.hdd_sensors?.[card.sourceId];
    }

    const title = document.getElementById('smart-modal-title');
    if (title && disk) {
        title.textContent = `SMART — ${disk.label || disk.dev_name || card.sourceId}`;
    } else if (title) {
        title.textContent = `SMART — ${card.sourceId}`;
    }
    document.getElementById('smart-modal')?.classList.remove('hidden');
    refreshSmartData();
}

function hideSmartModal() {
    document.getElementById('smart-modal')?.classList.add('hidden');
    smart.modalCardId = null;
    smart.modalDiskId = null;
    smart.modalSource = 'local';
}

async function refreshSmartData() {
    if (!smart.modalDiskId) return;
    const container = document.getElementById('smart-attributes-container');
    if (!container) return;
    
    // Use generation counter to detect stale fetches
    const gen = ++smart.fetchGeneration || 0;

    container.innerHTML = `<div class="text-center text-gray-400 py-4">${t('smart.loading', 'Loading...')}</div>`;

    const data = await fetchDiskSmart(smart.modalDiskId, true, smart.modalSource);
    if (!data || data.error) {
        if (gen !== smart.fetchGeneration) return; // stale
        container.innerHTML = `<div class="text-center text-red-400 py-4">${data?.error || t('smart.load_error', 'SMART data load error')}</div>`;
        return;
    }

    if (gen !== smart.fetchGeneration) return; // stale — modal was reopened
    smart.cache[`${smart.modalSource}:${smart.modalDiskId}`] = data;

    const infoEl = document.getElementById('smart-device-info');
    if (infoEl && data.device_info) {
        const info = data.device_info;
        infoEl.textContent = [info.model, info.serial, info.firmware, info.capacity].filter(Boolean).join(' | ');
    }

    smart.attrType = data.attr_type || 'sata';
    smart.attributes = data.attributes || [];

    renderSmartAttributes();
}

function renderSmartAttributes() {
    const container = document.getElementById('smart-attributes-container');
    if (!container) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === smart.modalCardId);
    const selectedIds = card?.smartAttributes || [];

    if (smart.attrType === 'nvme') {
        renderNvmeAttributes(container, selectedIds);
    } else {
        renderSataAttributes(container, selectedIds);
    }
}

function renderSataAttributes(container, selectedIds) {
    if (!smart.attributes.length) {
        container.innerHTML = `<div class="text-center text-gray-400 py-4">${t('smart.no_attributes', 'No SMART attributes')}</div>`;
        return;
    }

    const saved = getPickerCards();
    const card = saved.find(c => c.id === smart.modalCardId);
    const smartUnits = card?.smartUnits || {};

    container.innerHTML = smart.attributes.map(attr => {
        // Unified color: threshold breach (status) overrides static importance (criticality)
        const severity = (attr.status === 'critical' || attr.status === 'warning') ? attr.status : (attr.criticality || 'info');
        const statusColor = severity === 'critical' ? 'text-red-400' :
                           severity === 'warning' || severity === 'important' ? 'text-yellow-400' : 'text-neon-green';
        const statusBg = severity === 'critical' ? 'bg-red-500/10' :
                        severity === 'warning' || severity === 'important' ? 'bg-yellow-500/10' : 'bg-green-500/10';
        const critBadge = attr.criticality === 'critical' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">${t('smart.critical', 'CRITICAL')}</span>` :
                         attr.criticality === 'important' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">${t('smart.important', 'IMPORTANT')}</span>` : '';
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
                displayValue = (parseInt(attr.raw || '0') / 24).toFixed(1) + t('smart.unit.days_short', ' дн');
            } else if (unit === 'months') {
                displayValue = (parseInt(attr.raw || '0') / 720).toFixed(1) + t('smart.unit.months_short', ' мес');
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

function onSmartUnitChange(attrId, unit) {
    if (!smart.modalCardId) return;
    const saved = getPickerCards();
    const card = saved.find(c => c.id === smart.modalCardId);
    if (!card) return;

    if (!card.smartUnits) card.smartUnits = {};
    card.smartUnits[attrId] = unit;
    setPickerCards(saved);
    renderSmartAttributes();
}

function renderNvmeAttributes(container, selectedIds) {
    const attrs = smart.attributes;
    if (!Object.keys(attrs).length) {
        container.innerHTML = `<div class="text-center text-gray-400 py-4">${t('smart.no_nvme_attributes', 'No NVMe attributes')}</div>`;
        return;
    }

    const saved = getPickerCards();
    const card = saved.find(c => c.id === smart.modalCardId);
    const smartUnits = card?.smartUnits || {};

    container.innerHTML = Object.entries(attrs).map(([key, attr]) => {
        const severity = (attr.status === 'critical' || attr.status === 'warning') ? attr.status : (attr.criticality || 'info');
        const statusColor = severity === 'critical' ? 'text-red-400' :
                           severity === 'warning' || severity === 'important' ? 'text-yellow-400' : 'text-neon-green';
        const critBadge = attr.criticality === 'critical' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-red-500/20 text-red-300 ml-1">${t('smart.critical', 'CRITICAL')}</span>` :
                         attr.criticality === 'important' ? `<span class="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-300 ml-1">${t('smart.important', 'IMPORTANT')}</span>` : '';
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
        else if (attr.unit === 'hours' && (smartUnits[key] || 'raw') === 'days') suffix = t('smart.unit.days_short', ' дн');
        else if (attr.unit === 'hours' && (smartUnits[key] || 'raw') === 'months') suffix = t('smart.unit.months_short', ' мес');

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
    if (!smart.modalCardId) return;

    const saved = getPickerCards();
    const card = saved.find(c => c.id === smart.modalCardId);
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
    updateCardDetails(smart.modalCardId);
    const cardEl = document.querySelector(`[data-card-id="${smart.modalCardId}"]`);
    if (cardEl) snapCardToGrid(cardEl);
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
    const el = document.querySelector(`[data-card-id="${cardId}"]`);
    if (el) snapCardToGrid(el);
}

function findNode(source) {
    // Find node by stable_id or node_id. Returns node object or null.
    if (!source || source === 'local') return null;
    return store.nodesData.find(n => n.stable_id === source || n.node_id === source) || null;
}

function getFanData(source, sourceId) {
    if (source === 'local') return store.state?.fans?.[sourceId] || null;
    const node = store.nodesData.find(n => (n.stable_id || n.node_id) === source);
    return node?.telemetry?.fans?.[sourceId] || null;
}

function getSensorLabel(sensorId) {
    if (sensorId.startsWith('hdd:')) {
        const id = sensorId.slice(4);
        return store.state?.hdd_sensors?.[id]?.label || id;
    } else if (sensorId.startsWith('temp:')) {
        const id = sensorId.slice(5);
        return store.state?.temp_sensors?.[id]?.label || id;
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
        return;
    }

    const fanData = getFanData(card.source, card.sourceId);
    if (!fanData) return;

    let html = '';
    if (card.showMode) {
        const mode = fanData.mode || 'manual';
        const modeClass = mode === 'auto' ? 'text-neon-green' : 'text-neon-cyan';
        const modeLabel = mode === 'auto' ? t('mode.auto', 'AUTO') : t('mode.manual', 'MANUAL');
        html += `<div class="text-xs ${modeClass} mt-1">${modeLabel}</div>`;
    }
    if (card.showTarget && fanData.mode === 'auto') {
        html += `<div class="text-xs text-gray-500 mt-1">${t('inspector.target', 'Target:')} ${fanData.target_temp || '--'}°C</div>`;
    }
    if (card.showSensors && fanData.sensors && fanData.sensors.length > 0) {
        const sensorLabels = fanData.sensors.map(s => getSensorLabel(s)).join(', ');
        html += `<div class="text-xs text-gray-500 mt-1 truncate" title="${escapeHtml(sensorLabels)}">${t('inspector.sensors', 'Sensors:')} ${escapeHtml(sensorLabels)}</div>`;
    }

    detailsEl.innerHTML = html;
}

function updateDiskCardDetails(card, detailsEl) {
    if (!card.smartAttributes?.length) {
        return;
    }

    // diskData check removed — SMART attributes render from smart.cache independently
    // of hdd_sensors state loading. This prevents race condition where live update
    // skips rendering because hdd_sensors hasn't loaded yet.

    let html = '';
    const smartUnits = card.smartUnits || {};

    for (const attrKey of card.smartAttributes) {
        const attrId = parseInt(attrKey);
        const cacheKey = `${card.source || 'local'}:${card.sourceId}`;
        if (!isNaN(attrId)) {
            const cachedSmart = smart.cache?.[cacheKey];
            if (cachedSmart?.attributes) {
                const attr = cachedSmart.attributes.find(a => a.id === attrId);
                if (attr) {
                    const severity = (attr.status === 'critical' || attr.status === 'warning') ? attr.status : (attr.criticality || 'info');
                    const color = severity === 'critical' ? 'text-red-400' :
                                 severity === 'warning' || severity === 'important' ? 'text-yellow-400' : 'text-neon-green';
                    let displayValue = attr.raw;
                    if (attr.unit === 'bytes' && attr.unit_divisor) {
                        const unit = smartUnits[attr.id] || 'raw';
                        if (unit !== 'raw') {
                            displayValue = formatBytes(parseInt(attr.raw_num || attr.raw) * attr.unit_divisor, unit) + ' ' + getUnitLabel(unit);
                        }
                    } else if (attr.unit === 'hours') {
                        const unit = smartUnits[attr.id] || 'raw';
                        if (unit === 'days') {
                            displayValue = (parseInt(attr.raw || '0') / 24).toFixed(1) + t('smart.unit.days_short', ' дн');
                        } else if (unit === 'months') {
                            displayValue = (parseInt(attr.raw || '0') / 720).toFixed(1) + t('smart.unit.months_short', ' мес');
                        }
                    } else if (attr.unit === 'nvme_blocks') {
                        const unit = smartUnits[attr.id] || 'raw';
                        if (unit !== 'raw') {
                            displayValue = formatBytes(attr.value * (attr.unit_divisor || 1), unit) + ' ' + getUnitLabel(unit);
                        }
                    }
                    html += `<div class="text-xs mt-1 flex items-center" title="${escapeHtml(attr.tooltip)}" data-spark-attr="${attrKey}">
                        <span class="${color}">${escapeHtml(attr.description)}:</span>
                        <span class="text-neon-green font-mono ml-1">${displayValue}</span>
                    </div>`;
                }
            }
        } else {
            const cachedSmart = smart.cache?.[`${card.source || 'local'}:${card.sourceId}`];
            if (cachedSmart?.attributes?.[attrKey]) {
                const attr = cachedSmart.attributes[attrKey];
                const severity = (attr.status === 'critical' || attr.status === 'warning') ? attr.status : (attr.criticality || 'info');
                const color = severity === 'critical' ? 'text-red-400' :
                             severity === 'warning' || severity === 'important' ? 'text-yellow-400' : 'text-neon-green';
                let displayValue = attr.value;
                let suffix = attrKey === 'temperature' ? '°C' :
                            attrKey.includes('percentage') || attrKey.includes('spare') ? '%' : '';
                if (attr.unit === 'nvme_blocks' && attr.unit_divisor) {
                    const unit = smartUnits[attrKey] || 'raw';
                    if (unit !== 'raw') {
                        displayValue = formatBytes(attr.value * attr.unit_divisor, unit);
                        suffix = ' ' + getUnitLabel(unit);
                    }
                } else if (attr.unit === 'hours') {
                    const unit = smartUnits[attrKey] || 'raw';
                    if (unit === 'days') {
                        displayValue = (parseInt(attr.value || '0') / 24).toFixed(1);
                        suffix = t('smart.unit.days_short', ' дн');
                    } else if (unit === 'months') {
                        displayValue = (parseInt(attr.value || '0') / 720).toFixed(1);
                        suffix = t('smart.unit.months_short', ' мес');
                    }
                }
                html += `<div class="text-xs mt-1 flex items-center" title="${escapeHtml(attr.tooltip)}" data-spark-attr="${attrKey}">
                    <span class="${color}">${escapeHtml(attr.description)}:</span>
                    <span class="text-neon-green font-mono ml-1">${displayValue}${suffix}</span>
                </div>`;
            }
        }
    }

    if (html) detailsEl.innerHTML = html;

    // Load sparklines for monitored disks
    if (card.monitoring) {
        prefetchSmartSparklines(card);
    }
}

function prefetchSmartSparklines(card) {
    const cacheKey = `${card.source || 'local'}:${card.sourceId}`;
    for (const attrKey of (card.smartAttributes || [])) {
        const sparkKey = `${cacheKey}:${attrKey}`;
        if (smart.historyCache[sparkKey]) continue;

        const now = new Date();
        const from = new Date(now - 24 * 60 * 60 * 1000).toISOString();
        fetch(`/api/smart/history/${card.sourceId}?attr=${attrKey}&from=${from}`)
            .then(r => r.json())
            .then(data => {
                if (data.history?.length) {
                    smart.historyCache[sparkKey] = data.history;
                    renderSmartCardSparklines(card);
                }
            })
            .catch(() => {});
    }
}

function renderSmartCardSparklines(card) {
    const cardEl = document.querySelector(`[data-card-id="${card.id}"]`);
    if (!cardEl) return;
    const detailsEl = cardEl.querySelector('.card-details');
    if (!detailsEl) return;

    // Add sparkline SVGs after each attribute row
    const rows = detailsEl.querySelectorAll('[data-spark-attr]');
    rows.forEach(row => {
        const attrKey = row.dataset.sparkAttr;
        const cacheKey = `${card.source || 'local'}:${card.sourceId}`;
        const sparkKey = `${cacheKey}:${attrKey}`;
        const history = smart.historyCache[sparkKey];
        if (!history || history.length < 2) return;

        let existing = row.querySelector('.smart-sparkline');
        if (existing) return;

        const values = history.map(h => h.value);
        const min = Math.min(...values);
        const max = Math.max(...values);
        const range = max - min || 1;
        const w = 60, h = 16;

        const points = values.map((v, i) => {
            const x = (i / (values.length - 1)) * w;
            const y = h - ((v - min) / range) * (h - 2) - 1;
            return `${x},${y}`;
        }).join(' ');

        const color = min < max ? '#30d158' : '#6b7280';
        const svg = `<svg width="${w}" height="${h}" class="smart-sparkline ml-1 shrink-0 opacity-50">
            <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>`;

        row.appendChild(document.createRange().createContextualFragment(svg));
    });
}

function pushSparkline(key, value) {
    if (!sparklineHistory[key]) sparklineHistory[key] = [];
    sparklineHistory[key].push(value);
    if (sparklineHistory[key].length > SPARKLINE_MAX) sparklineHistory[key].shift();
}

function getSparkline(key) {
    return sparklineHistory[key] || [];
}

function renderSparkline(key, color = '#22d3ee', width = 120, height = 30) {
    const data = getSparkline(key);
    if (data.length < 2) return '';
    
    const min = Math.min(...data);
    const max = Math.max(...data);
    const range = max - min || 1;
    
    const points = data.map((v, i) => {
        const x = (i / (data.length - 1)) * width;
        const y = height - ((v - min) / range) * (height - 4) - 2;
        return `${x},${y}`;
    }).join(' ');
    
    return `<svg width="${width}" height="${height}" class="mt-2 opacity-60">
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
}

async function loadDashboardFromServer() {
    try {
        const resp = await fetch('/api/dashboard');
        if (resp.ok) {
            const data = await resp.json();
            dashboard.cards = data.cards || [];
            dashboard.groups = data.groups || [];
            dashboard.hiddenSensors = data.hiddenSensors || [];
            dashboard.loaded = true;
            return;
        }
    } catch (e) { console.warn('Dashboard load failed:', e); }
    dashboard.cards = [];
    dashboard.groups = [];
    dashboard.hiddenSensors = [];
    dashboard.loaded = true;
}

function getPickerCards() {
    return dashboard.cards || [];
}

function setPickerCards(cards) {
    dashboard.cards = cards;
    scheduleDashboardSave();
}

function getPickerGroups() {
    return dashboard.groups || [];
}

function setPickerGroups(groups) {
    dashboard.groups = groups;
    scheduleDashboardSave();
}

function scheduleDashboardSave() {
    if (dashboard.saveTimer) clearTimeout(dashboard.saveTimer);
    dashboard.saveTimer = setTimeout(saveDashboardToServer, 500);
}

async function saveDashboardToServer() {
    try {
        await fetch('/api/dashboard', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cards: dashboard.cards || [], groups: dashboard.groups || [], hiddenSensors: dashboard.hiddenSensors || [] })
        });
    } catch (e) { console.warn('Dashboard save failed:', e); }
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
    // Fetch all SMART data in parallel for faster initial load
    // Cache key = source:sourceId to avoid mixing local/remote data for same disk
    const promises = cards
        .filter(c => !smart.cache[`${c.source || 'local'}:${c.sourceId}`])
        .map(async (card) => {
            try {
                const data = await fetchDiskSmart(card.sourceId, false, card.source || 'local');
                if (data && !data.error) {
                    smart.cache[`${card.source || 'local'}:${card.sourceId}`] = data;
                    updateCardDetails(card.id);
                }
            } catch (e) { console.warn('SMART prefetch failed:', e); }
        });
    await Promise.all(promises);
}

function startPickerLiveUpdate() {
    if (dashboard.liveTimer) return;
    dashboard.liveTimer = setInterval(() => {
        document.querySelectorAll('[data-fan-id]').forEach(el => {
            const src = el.dataset.source;
            const id = el.dataset.fanId;
            let fan = null;
            if (src === 'local' && store.state?.fans?.[id]) {
                fan = store.state.fans[id];
            } else {
                const node = findNode(src);
                fan = node?.telemetry?.fans?.[id];
            }
            if (fan) {
                el.textContent = fan.rpm || 0;
                pushSparkline(`fan:${src}:${id}`, fan.rpm || 0);
                const cardEl = el.closest('[data-card-id]');
                if (cardEl) updateCardDetails(cardEl.dataset.cardId);
                const dot = cardEl?.querySelector('.status-dot');
                if (dot) {
                    const s = fan.status || 'unknown';
                    dot.className = 'status-dot ' + (s === 'running' ? 'green' : (s === 'failsafe' || s === 'critical') ? 'red' : 'yellow');
                }
                const animEl = document.querySelector(`[data-fan-anim-id="${id}"][data-fan-source="${src}"]`);
                if (animEl) {
                    const rpm = fan.rpm || 0;
                    const dur = rpm > 0 ? Math.max(0.2, 2 - (rpm / 1500)) : 0;
                    animEl.style.animation = rpm > 0 ? `fan-spin ${dur}s linear infinite` : 'none';
                    const fanColor = fan.status === 'running' ? '#22d3ee' : (fan.status === 'failsafe' || fan.status === 'critical') ? '#ef4444' : '#facc15';
                    animEl.querySelectorAll('path, circle').forEach(p => p.setAttribute('fill', fanColor));
                }
            }
        });
        document.querySelectorAll('[data-temp-id]').forEach(el => {
            const src = el.dataset.source;
            const id = el.dataset.tempId;
            let val = null;
            if (src === 'local' && store.state?.temp_sensors?.[id]) {
                val = store.state.temp_sensors[id].value;
            } else {
                const node = findNode(src);
                val = node?.telemetry?.temp_sensors?.[id]?.value;
            }
            if (val != null) el.textContent = val;
            pushSparkline(`temp:${src}:${id}`, val);
        });
        document.querySelectorAll('[data-disk-id]').forEach(el => {
            const id = el.dataset.diskId;
            const src = el.dataset.source;
            let temp = null;
            if (src === 'local') {
                temp = store.state?.hdd_sensors?.[id]?.temp;
            } else {
                const node = findNode(src);
                temp = node?.telemetry?.hdd_sensors?.[id]?.temp;
            }
            if (temp != null) {
                el.textContent = temp || '--';
                pushSparkline(`disk:${src}:${id}`, temp || 0);
            }
        });
        getPickerCards().filter(c => c.type === 'disk' && c.smartAttributes?.length).forEach(c => {
            if (smart.cache[`${c.source || 'local'}:${c.sourceId}`]) {
                const cardEl = document.querySelector(`[data-card-id="${c.id}"]`);
                if (cardEl) {
                    const detailsEl = cardEl.querySelector('.card-details');
                    if (detailsEl) updateDiskCardDetails(c, detailsEl);
                }
            }
        });
    }, 2000);
}

function stopPickerLiveUpdate() {
    if (dashboard.liveTimer) {
        clearInterval(dashboard.liveTimer);
        dashboard.liveTimer = null;
    }
}

function startSystemUpdate() {
    if (timers.system) return;
    timers.system = setInterval(async () => {
        try {
            const resp = await fetch('/api/system');
            const data = await resp.json();
            document.querySelectorAll('[data-system-field="uptime"]').forEach(el => el.textContent = data.uptime || '--');
            document.querySelectorAll('[data-system-field="cpu"]').forEach(el => el.textContent = (data.cpu_load || 0) + '%');
            document.querySelectorAll('[data-system-field="mem"]').forEach(el => el.textContent = (data.mem_percent || 0) + '%');
            document.querySelectorAll('[data-system-bar="cpu"]').forEach(el => el.style.width = (data.cpu_load || 0) + '%');
            document.querySelectorAll('[data-system-bar="mem"]').forEach(el => el.style.width = (data.mem_percent || 0) + '%');
        } catch(e) {}
    }, 5000);
}
function stopSystemUpdate() {
    if (timers.system) { clearInterval(timers.system); timers.system = null; }
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

function onGroupCardDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
}

function onGroupDragLeave(e) {
    this.classList.remove('border-neon-purple', 'bg-purple-900/10');
}

function onGroupDropOutside(e) {
    if (groupDrag.draggedGroup) {
        groupDrag.draggedGroup.classList.remove('opacity-40');
        groupDrag.draggedGroup = null;
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

function startGroupResize(e, groupId) {
    e.preventDefault();
    e.stopPropagation();
    groupDrag.resizingGroupId = groupId;
    const el = document.querySelector(`[data-group-id="${groupId}"]`);
    if (!el) return;
    groupDrag.resizeStartY = e.clientY;
    groupDrag.resizeStartH = el.offsetHeight;
    document.addEventListener('mousemove', onGroupResize);
    document.addEventListener('mouseup', stopGroupResize);
}

function onGroupResize(e) {
    if (!groupDrag.resizingGroupId) return;
    const el = document.querySelector(`[data-group-id="${groupDrag.resizingGroupId}"]`);
    if (!el) return;
    const h = Math.max(100, groupDrag.resizeStartH + (e.clientY - groupDrag.resizeStartY));
    el.style.minHeight = h + 'px';
}

function stopGroupResize() {
    if (!groupDrag.resizingGroupId) return;
    const groups = getPickerGroups();
    const group = groups.find(g => g.id === groupDrag.resizingGroupId);
    const el = document.querySelector(`[data-group-id="${groupDrag.resizingGroupId}"]`);
    if (group && el) {
        group.minHeight = el.style.minHeight;
        setPickerGroups(groups);
    }
    groupDrag.resizingGroupId = null;
    document.removeEventListener('mousemove', onGroupResize);
    document.removeEventListener('mouseup', stopGroupResize);
}

function onGroupDragStart(e) {
    if (e.target.closest('.group-resize-handle') || e.target.closest('button') || e.target.closest('input')) return;
    groupDrag.draggedGroup = this;
    groupDrag.dropTarget = null;
    this.classList.add('opacity-40');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/group', this.dataset.groupId);
}

function onGroupDragOver(e) {
    if (!groupDrag.draggedGroup) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const canvas = document.getElementById('dashboard-canvas');
    groupDrag.dropTarget = getDragAfterElement(canvas, e.clientX, e.clientY);
}

function onGroupDragEnd() {
    if (groupDrag.draggedGroup) {
        if (groupDrag.dropTarget !== undefined) {
            const canvas = document.getElementById('dashboard-canvas');
            if (groupDrag.dropTarget) {
                canvas.insertBefore(groupDrag.draggedGroup, groupDrag.dropTarget);
            } else {
                canvas.appendChild(groupDrag.draggedGroup);
            }
        }
        groupDrag.draggedGroup.classList.remove('opacity-40');
        groupDrag.draggedGroup = null;
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
        'stopped': 'bg-red-900 bg-opacity-50 text-neon-red',
        'slowing': 'bg-yellow-900 bg-opacity-30 text-yellow-400',
        'needs_calibration': 'bg-yellow-900 bg-opacity-30 text-yellow-400',
    };
    return classes[status] || 'bg-gray-700 text-gray-400';
}

// ============================================================================
// INSPECTOR (Right Panel)
// ============================================================================

function updateInspector(fan) {
    document.getElementById('inspector-empty')?.classList.add('hidden');
    document.getElementById('inspector-fan')?.classList.remove('hidden');

    const inspectorTitle = document.getElementById('inspector-title');
    if (inspectorTitle) inspectorTitle.textContent = fan.label;
    const inspectorSubtitle = document.getElementById('inspector-subtitle');
    if (inspectorSubtitle) inspectorSubtitle.textContent = `ID: ${fan.id || 'unknown'}`;

    const fanName = document.getElementById('fan-name');
    if (fanName) fanName.textContent = fan.label;
    
    const statusBadge = document.getElementById('fan-status-badge');
    if (statusBadge) {
        statusBadge.textContent = t('status.' + fan.status, fan.status || 'unknown');
        statusBadge.className = `text-xs px-2 py-0.5 rounded-full ${getStatusBadgeClass(fan.status)}`;
    }
    
    const invertedBadge = document.getElementById('fan-inverted-badge');
    if (invertedBadge) {
        invertedBadge.classList.toggle('hidden', !fan.inverted);
    }
    
    const modeBadge = document.getElementById('fan-mode-badge');
    const mode = fan.mode || 'manual';
    if (modeBadge) {
        modeBadge.textContent = t('mode.' + mode, mode).toUpperCase();
        modeBadge.className = mode === 'auto' 
            ? 'text-xs px-2 py-0.5 rounded-full bg-cyan-900 bg-opacity-30 text-neon-cyan'
            : 'text-xs px-2 py-0.5 rounded-full bg-purple-900 bg-opacity-30 text-neon-purple';
    }
    
    const rpmDisplay = document.getElementById('fan-rpm-display');
    if (rpmDisplay) {
        rpmDisplay.textContent = fan.rpm || 0;
        rpmDisplay.classList.remove('text-neon-cyan', 'text-neon-orange', 'text-neon-red');
        if (fan.rpm > (fan.max_rpm * 0.8 || 1500)) {
            rpmDisplay.classList.add('text-neon-orange');
        } else if (fan.status === 'failsafe' || fan.status === 'critical') {
            rpmDisplay.classList.add('text-neon-red');
        } else {
            rpmDisplay.classList.add('text-neon-cyan');
        }
    }
    
    if (!store.isDragging) {
        const slider = document.getElementById('pwm-slider');
        const pct = fan.current_pct != null ? fan.current_pct : (fan.manual_pct != null ? fan.manual_pct : 50);
        if (slider) {
            slider.value = pct;
            slider.disabled = (mode === 'auto');
        }
        const pwmValueDisplay = document.getElementById('pwm-value-display');
        if (pwmValueDisplay) pwmValueDisplay.textContent = `${pct}%`;
    }
    
    setModeButtonStyles(mode);
    
    const autoSettings = document.getElementById('auto-settings');
    if (autoSettings) {
        autoSettings.style.display = (mode === 'auto') ? 'block' : 'none';
    }
    
    // Render schedule grid when in auto mode
    if (mode === 'auto') {
        setTimeout(() => renderScheduleGrid(), 50);
    }
    
    // Store config
    if (!store.fanConfigs[store.currentFanId]) store.fanConfigs[store.currentFanId] = {};
    store.fanConfigs[store.currentFanId].sensors = fan.sensors || [];
    store.fanConfigs[store.currentFanId].target_temp = fan.target_temp || 31;
    store.fanConfigs[store.currentFanId].mode = mode;
    store.fanConfigs[store.currentFanId].sensor_mode = fan.sensor_mode || 'max';

    // Calibration params
    const cal = fan.calibration || {};
    const minPwmEl = document.getElementById('cal-min-pwm');
    const maxPwmEl = document.getElementById('cal-max-pwm');
    const lambdaEl = document.getElementById('cal-lambda');
    if (minPwmEl) {
        minPwmEl.value = cal.min_pwm || 0;
        const calMinPwmVal = document.getElementById('cal-min-pwm-val');
        if (calMinPwmVal) calMinPwmVal.textContent = cal.min_pwm || 0;
    }
    if (maxPwmEl) {
        maxPwmEl.value = cal.max_pwm || 255;
        const calMaxPwmVal = document.getElementById('cal-max-pwm-val');
        if (calMaxPwmVal) calMaxPwmVal.textContent = cal.max_pwm || 255;
    }
    if (lambdaEl) {
        lambdaEl.value = (cal.lambda || 1.0) * 10;
        const calLambdaVal = document.getElementById('cal-lambda-val');
        if (calLambdaVal) calLambdaVal.textContent = (cal.lambda || 1.0).toFixed(1);
    }

    // Health & Service section
    const health = fan.health || {};
    const serviceSection = document.getElementById('fan-service-section');
    if (serviceSection) {
        const lastService = health.last_service_date;
        const needsCal = health.calibration_required;
        const hStatus = health.status;

        let svcHtml = '';
        if (lastService) {
            svcHtml += `<div class="text-xs text-gray-500">${t('fan.service_date', 'Last service')}: ${new Date(lastService).toLocaleDateString()}</div>`;
        }
        if (hStatus === 'stopped' || hStatus === 'slowing' || needsCal) {
            svcHtml += `<button onclick="showServiceFanModal('${escapeHtml(fan.id)}')" class="mt-2 px-3 py-1.5 bg-yellow-900/30 border border-yellow-700/50 rounded text-xs text-yellow-400 hover:bg-yellow-800/50 transition">
                ${t('fan.service', 'Service')} / ${t('fan.replace', 'Replace')}
            </button>`;
        }
        if (needsCal) {
            svcHtml += `<div class="mt-2 px-2 py-1 rounded bg-yellow-900/20 border border-yellow-700/30 text-xs text-yellow-400">
                ⚠ ${t('fan.calibration_required', 'Calibration required after service')}
                <button onclick="startFanCalibration('${escapeHtml(fan.id)}')" class="ml-2 underline hover:text-yellow-300">${t('inspector.calibrate', 'Calibrate')}</button>
            </div>`;
        }
        serviceSection.innerHTML = svcHtml;
    }
}

// ============================================================================
// FAN CONTROL ACTIONS
// ============================================================================

function showServiceFanModal(fanId) {
    const fan = store.state?.fans?.[fanId];
    if (!fan) return;

    const overlay = document.createElement('div');
    overlay.className = 'fixed inset-0 bg-black/60 z-50 flex items-center justify-center';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

    const today = new Date().toISOString().split('T')[0];

    overlay.innerHTML = `
        <div class="bg-gray-900 border border-gray-700 rounded-xl p-5 w-80 shadow-2xl">
            <h3 class="text-white font-semibold mb-3">${t('fan.service', 'Service')} / ${t('fan.replace', 'Replace')}</h3>
            <p class="text-xs text-gray-400 mb-4">${escapeHtml(fan.label)}</p>
            <div class="mb-4">
                <label class="text-xs text-gray-500 block mb-1">${t('fan.service_date', 'Date')}</label>
                <input type="date" id="service-date" value="${today}" class="w-full bg-gray-800 border border-gray-600 rounded px-2 py-1.5 text-xs text-white">
            </div>
            <div class="flex gap-2">
                <button onclick="recordFanService('${escapeHtml(fanId)}', 'service', this)" class="flex-1 px-3 py-2 bg-yellow-900/30 border border-yellow-700/50 rounded text-xs text-yellow-400 hover:bg-yellow-800/50 transition">
                    ${t('fan.service', 'Service')}
                </button>
                <button onclick="recordFanService('${escapeHtml(fanId)}', 'replace', this)" class="flex-1 px-3 py-2 bg-orange-900/30 border border-orange-700/50 rounded text-xs text-orange-400 hover:bg-orange-800/50 transition">
                    ${t('fan.replace', 'Replace')}
                </button>
            </div>
            <button onclick="this.closest('.fixed').remove()" class="w-full mt-2 px-3 py-1.5 text-xs text-gray-500 hover:text-gray-300 transition">
                ${t('common.cancel', 'Cancel')}
            </button>
        </div>
    `;
    document.body.appendChild(overlay);
}

async function recordFanService(fanId, action, btnEl) {
    const dateEl = document.getElementById('service-date');
    const date = dateEl ? dateEl.value : new Date().toISOString().split('T')[0];

    try {
        const resp = await fetch(`/api/fan/${fanId}/service`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action, date})
        });
        const data = await resp.json();
        if (data.status === 'ok') {
            // Close modal
            const overlay = btnEl?.closest('.fixed');
            if (overlay) overlay.remove();

            showToast(t('fan.service_done', 'Service recorded. Calibration recommended.'), 'warning', [
                {label: t('inspector.calibrate', 'Calibrate'), onclick: () => startFanCalibration(fanId)},
                {label: t('common.later', 'Later'), onclick: () => {}, secondary: true}
            ]);

            // Update local state
            if (store.state?.fans?.[fanId]) {
                store.state.fans[fanId].health = data.health;
            }
            if (store.currentFanId === fanId) {
                updateInspector(store.state.fans[fanId]);
            }
            buildFanList(store.state.fans || {});
            buildServerTree();
        }
    } catch (e) {
        showToast(t('common.error', 'Error'), 'error');
    }
}

async function startFanCalibration(fanId) {
    try {
        const resp = await fetch('/api/test/start', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({fan: fanId})
        });
        if (resp.ok) {
            showToast(t('calibration.started', 'Calibration started...'), 'info');
        }
    } catch (e) {
        showToast(t('common.error', 'Error'), 'error');
    }
}

function setFanMode(mode) {
    if (!store.currentFanId) return;
    
    // Update local state immediately for instant UI feedback
    if (store.state?.fans?.[store.currentFanId]) {
        store.state.fans[store.currentFanId].mode = mode;
    }
    if (store.fanConfigs[store.currentFanId]) {
        store.fanConfigs[store.currentFanId].mode = mode;
    }
    
    // Update button styles immediately
    setModeButtonStyles(mode);
    
    document.getElementById('auto-settings').style.display = (mode === 'auto') ? 'block' : 'none';
    if (mode === 'auto') {
        setTimeout(() => renderScheduleGrid(), 50);
    }
    
    sendControl({
        action: 'set_fan_config',
        fan: store.currentFanId,
        fan_mode: mode
    });
}

function sendControl(payload) {
    fetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    })
    .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    })
    .catch(err => {
        console.error('Control error:', err);
        showToast(t('toast.control_error', 'Control command failed'), 'error');
    });
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
        const el = document.getElementById('pwm-value-display');
        if (el) el.textContent = `${e.target.value}%`;
    });
    
    slider.addEventListener('mousedown', () => {
        store.isDragging = true;
    });
    
    slider.addEventListener('mouseup', (e) => {
        store.isDragging = false;
        applyPWM(e.target.value);
    });
    
    slider.addEventListener('touchend', (e) => {
        store.isDragging = false;
        applyPWM(e.target.value);
    });
});

function applyPWM(value) {
    if (!store.currentFanId) return;
    
    sendControl({
        action: 'set_fan_pwm',
        fan: store.currentFanId,
        pwm: parseInt(value)
    });
}

// ============================================================================
// SENSOR POPUP
// ============================================================================

function buildSensorList(data) {
    store.allSensors = [];
    const hidden = getHiddenSensors();

    if (data.hdd_sensors) {
        for (const [id, disk] of Object.entries(data.hdd_sensors)) {
            if (hidden.includes(`disk:${id}`)) continue;
            store.allSensors.push({
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
            store.allSensors.push({
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
        const currentSensors = store.fanConfigs[store.currentFanId]?.sensors || [];
        list.innerHTML = buildSensorCheckboxList(store.allSensors, currentSensors);
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
    
    if (store.currentFanId) {
        if (!store.fanConfigs[store.currentFanId]) store.fanConfigs[store.currentFanId] = {};
        store.fanConfigs[store.currentFanId].sensors = sensors;
        
        sendControl({
            action: 'set_fan_config',
            fan: store.currentFanId,
            sensors: sensors
        });
        
        // Update no-sensor warning and sensor mode section
        const mode = store.fanConfigs[store.currentFanId]?.mode || 'manual';
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

// ============================================================================
// SETUP WIZARD
// ============================================================================

function runDiscovery() {
    console.log('[FanControl] Starting hardware discovery...');
    
    setDiscoverButtonState(true);
    store.wizardStep = 'scanning';
    
    fetch('/api/discover', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            setDiscoverButtonState(false);
            
            if (data.status === 'ok') {
                renderDiscoveredHardware(data);
                store.wizardStep = 'results';
                
                document.getElementById('setup-step-intro')?.classList.add('hidden');
                document.getElementById('setup-step-results')?.classList.remove('hidden');
            } else {
                alert(t('discover.scan_error', 'Scan error: ') + data.message);
                store.wizardStep = 'intro';
            }
        })
        .catch(err => {
            console.error('Discovery error:', err);
            alert(t('discover.connection_error', 'Connection error'));
            setDiscoverButtonState(false);
            store.wizardStep = 'intro';
        });
}

function renderDiscoveredHardware(data) {
    const container = document.getElementById('discovered-devices');
    if (!container) return;
    
    let html = '';
    
    // Kernel info banner
    if (data.kernel_info) {
        const ki = data.kernel_info;
        const isCustom = ki.type === 'custom';
        const kernelColor = isCustom ? 'text-neon-green' : 'text-neon-orange';
        const kernelLabel = isCustom ? 'Custom ARC' : ki.type === 'official' ? 'Official Synology' : 'Unknown';
        const fanMethod = ki.has_hwmon_pwm ? 'hwmon (PWM)' : ki.has_scemd ? 'scemd.xml (DSM API)' : 'none';
        html += `<div class="bg-cyber-accent rounded-lg p-3 mb-4 text-xs">
            <div class="flex justify-between mb-1">
                <span class="text-gray-400">Kernel:</span>
                <span class="${kernelColor} font-semibold">${kernelLabel}</span>
            </div>
            <div class="flex justify-between mb-1">
                <span class="text-gray-400">Fan control:</span>
                <span class="text-white">${fanMethod}</span>
            </div>
            ${ki.version ? `<div class="text-gray-500 mt-1 truncate" title="${escapeHtml(ki.version)}">${escapeHtml(ki.version)}</div>` : ''}
        </div>`;
    }
    
    // Fans section
    if (data.fans && Object.keys(data.fans).length > 0) {
        html += '<h4 class="text-sm font-semibold text-neon-cyan mb-2">🌀 Fans</h4>';
        for (const [id, fan] of Object.entries(data.fans)) {
            const cleanLabel = fan.label.replace(/\s*\(Synology-[^)]+\)/, '');
            const isDsm = fan.control_method === 'dsm_scemd';
            html += `
                <div class="flex items-center justify-between bg-cyber-accent rounded-lg p-3 mb-1">
                    <div>
                        <span class="text-sm text-white">${escapeHtml(cleanLabel)}</span>
                        <span class="text-xs text-gray-500 ml-2">${fan.writable ? 'Controllable' : 'Read-only'}</span>
                        ${isDsm ? '<span class="text-xs bg-blue-900 bg-opacity-30 text-blue-400 px-2 py-0.5 rounded ml-2">DSM</span>' : ''}
                    </div>
                    ${!isDsm ? '<span class="text-xs bg-orange-900 bg-opacity-30 text-neon-orange px-2 py-0.5 rounded">Not calibrated</span>' : ''}
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
    
    // Determine available control modes
    const actionDiv = document.getElementById('setup-step-action');
    const controlSelect = document.getElementById('control-mode-select');
    const hwmonBtn = document.getElementById('btn-hwmon');
    const dsmBtn = document.getElementById('btn-dsm');
    const hint = document.getElementById('mode-unavailable-hint');
    
    const kernelInfo = data.kernel_info || {};
    const hasHwmon = kernelInfo.has_hwmon_pwm;
    const hasDsm = kernelInfo.has_scemd;
    const hasFans = data.fans && Object.keys(data.fans).length > 0;
    
    // Always show mode selection when fans are detected
    if (hasFans && (hasHwmon || hasDsm)) {
        controlSelect.classList.remove('hidden');
        document.getElementById('hwmon-action')?.classList.add('hidden');
        document.getElementById('dsm-action')?.classList.add('hidden');
        actionDiv.classList.remove('hidden');
        
        // HWMon button state
        if (hasHwmon) {
            hwmonBtn.classList.remove('opacity-40', 'cursor-not-allowed', 'pointer-events-none');
            hwmonBtn.disabled = false;
        } else {
            hwmonBtn.classList.add('opacity-40', 'cursor-not-allowed', 'pointer-events-none');
            hwmonBtn.disabled = true;
        }
        
        // DSM button state
        if (hasDsm) {
            dsmBtn.classList.remove('opacity-40', 'cursor-not-allowed', 'pointer-events-none');
            dsmBtn.disabled = false;
        } else {
            dsmBtn.classList.add('opacity-40', 'cursor-not-allowed', 'pointer-events-none');
            dsmBtn.disabled = true;
        }
        
        // Show hint if one mode unavailable
        if (hasHwmon && !hasDsm) {
            hint.textContent = 'DSM schemes not found — only hwmon control available.';
            hint.classList.remove('hidden');
        } else if (!hasHwmon && hasDsm) {
            hint.textContent = 'hwmon PWM not available on this kernel — only DSM scheme control available.';
            hint.classList.remove('hidden');
        } else {
            hint.classList.add('hidden');
        }
    } else if (hasFans && !hasHwmon && !hasDsm) {
        // Fans but no control method
        controlSelect.classList.add('hidden');
        document.getElementById('hwmon-action')?.classList.add('hidden');
        document.getElementById('dsm-action')?.classList.add('hidden');
        actionDiv.classList.remove('hidden');
        hint.textContent = 'No fan control method available.';
        hint.classList.remove('hidden');
    } else {
        // No fans
        controlSelect.classList.add('hidden');
        document.getElementById('hwmon-action')?.classList.add('hidden');
        document.getElementById('dsm-action')?.classList.add('hidden');
        actionDiv.classList.add('hidden');
    }
}

function selectControlMode(mode) {
    const hwmonAction = document.getElementById('hwmon-action');
    const dsmAction = document.getElementById('dsm-action');
    const hwmonBtn = document.getElementById('btn-hwmon');
    const dsmBtn = document.getElementById('btn-dsm');
    
    hwmonAction.classList.add('hidden');
    dsmAction.classList.add('hidden');
    
    if (mode === 'hwmon') {
        hwmonBtn.classList.add('card-selected');
        dsmBtn.classList.remove('card-selected');
        hwmonAction.classList.remove('hidden');
    } else {
        dsmBtn.classList.add('card-selected');
        hwmonBtn.classList.remove('card-selected');
        dsmAction.classList.remove('hidden');
    }
}

function applyDsmAndContinue() {
    // Skip calibration, go straight to DSM scheme editor
    fetch('/api/skip-calibration', { method: 'POST' }).catch(err => console.error('Skip calibration error:', err));
    fetch('/api/dsm/fan-speed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speed: 50 })
    }).catch(err => console.error('DSM fan speed error:', err));
    store.wizardStep = 'done';
    store.state.initialized = true; store.state.tested = true;
    showMainScreen();
    setTimeout(() => showView('dsm-scheme'), 500);
}

function skipCalibration() {
    console.log('[FanControl] Skipping calibration — monitoring-only mode');
    fetch('/api/skip-calibration', { method: 'POST' })
        .catch(err => console.error('Skip calibration error:', err));
    store.wizardStep = 'done';
    store.state.initialized = true; store.state.tested = true;
    showMainScreen();
}

function applyDsmFanSpeed() {
    const speed = parseInt(document.getElementById('dsm-speed-slider').value);
    console.log(`[FanControl] Setting DSM fan speed to ${speed}%`);
    
    fetch('/api/dsm/fan-speed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speed })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'ok') {
            fetch('/api/skip-calibration', { method: 'POST' }).catch(err => console.error('Skip calibration error:', err));
            store.wizardStep = 'done';
            store.state.initialized = true; store.state.tested = true;
            showMainScreen();
        } else {
            alert('Error: ' + (data.message || t('toast.speed_failed', 'Failed to set fan speed')));
        }
    })
    .catch(err => {
        console.error('DSM fan speed error:', err);
        alert(t('toast.speed_failed', 'Failed to set fan speed'));
    });
}

// ============================================================================
// DSM SCHEME EDITOR
// ============================================================================

async function renderDsmSchemeEditor(remoteNodeId) {
    const container = document.getElementById('dsm-scheme-inner');
    if (!container) return;

    container.innerHTML = `<div class="text-gray-500 text-center py-8">${t('dsm.loading', 'Loading DSM schemes...')}</div>`;

    try {
        let schemesData, activeData;

        if (remoteNodeId) {
            // Remote node — use schemes from node state
            const node = store.nodesData.find(n => n.node_id === remoteNodeId);
            if (!node) {
                container.innerHTML = `<div class="text-red-400 text-center py-8">${t('dsm.node_not_found', 'Node not found')}</div>`;
                return;
            }
            schemesData = { status: 'ok', schemes: node.telemetry?.dsm_schemes || node.config?.dsm_schemes || node.dsm_schemes || [] };
            activeData = { active_scheme: null };
        } else {
            // Local server
            const [schemesResp, activeResp] = await Promise.all([
                fetch('/api/dsm/schemes'),
                fetch('/api/dsm/active')
            ]);
            schemesData = await schemesResp.json();
            activeData = await activeResp.json();
        }

        if (schemesData.status !== 'ok') {
            container.innerHTML = `<div class="text-red-400 text-center py-8">${schemesData.message || 'Failed to load schemes'}</div>`;
            return;
        }

        dsm.schemes = schemesData.schemes || [];
        dsm.activeScheme = activeData.active_scheme || null;

        if (dsm.schemes.length === 0) {
            container.innerHTML = `<div class="text-gray-500 text-center py-8">${t('dsm.no_schemes', 'No fan schemes found in scemd.xml')}</div>`;
            return;
        }

        let html = `
            <div class="max-w-4xl mx-auto">
                <div class="flex items-center justify-between mb-6">
                    <h2 class="text-xl font-bold text-white">DSM Fan Schemes</h2>
                    <button onclick="showView('dashboard')" class="text-gray-400 hover:text-white text-sm">
                        &larr; ${t('dsm.back', 'Back to Dashboard')}
                    </button>
                </div>
        `;

        for (const scheme of dsm.schemes) {
            const isActive = scheme.type === dsm.activeScheme;
            const schemeLabel = _schemeLabel(scheme.type);

            html += `
                <div class="mb-6 bg-gray-900/50 border ${isActive ? 'border-green-500/50' : 'border-gray-700'} rounded-xl p-4">
                    <div class="flex items-center justify-between mb-3">
                        <div class="flex items-center gap-3">
                            <h3 class="text-white font-semibold">${schemeLabel}</h3>
                            ${isActive ? `<span class="text-xs bg-green-900/50 text-green-400 px-2 py-0.5 rounded">${t('dsm.active', 'Active')}</span>` : ''}
                            ${scheme.hibernation_speed === 'STOP' ? '<span class="text-xs bg-yellow-900/50 text-yellow-400 px-2 py-0.5 rounded">Hibernation: STOP</span>' : ''}
                        </div>
                        <button onclick="applyDsmScheme('${escapeHtml(scheme.type)}')"
                                class="px-3 py-1 bg-neon-cyan/20 border border-neon-cyan/50 text-neon-cyan text-xs rounded hover:bg-neon-cyan/30 transition-all">
                            ${t('dsm.apply', 'Apply')}
                        </button>
                    </div>
            `;

            if (scheme.entries.length > 0) {
                html += `
                    <table class="w-full text-sm">
                        <thead>
                            <tr class="text-gray-400 text-xs border-b border-gray-700">
                                <th class="text-left py-2">${t('dsm.col_sensor', 'Sensor')}</th>
                                <th class="text-left py-2">${t('dsm.col_speed', 'Speed')}</th>
                                <th class="text-left py-2">${t('dsm.col_action', 'Action')}</th>
                                <th class="text-left py-2">${t('dsm.col_threshold', 'Threshold')}</th>
                                <th class="text-right py-2">${t('dsm.col_edit', 'Edit')}</th>
                            </tr>
                        </thead>
                        <tbody>
                `;

                for (let i = 0; i < scheme.entries.length; i++) {
                    const entry = scheme.entries[i];
                    const isLast = i === scheme.entries.length - 1;
                    const sensorLabel = entry.sensor_type === 'cpu_temperature' ? 'CPU' : 'Disk';
                    const speedDisplay = entry.fan_speed || '--';
                    const actionClass = entry.action === 'SHUTDOWN' ? 'text-red-400' : 'text-gray-300';
                    const threshold = entry.threshold_temp + '°C';

                    html += `
                        <tr class="border-b border-gray-800 hover:bg-gray-800/30">
                            <td class="py-2">
                                <span class="px-1.5 py-0.5 rounded text-xs ${entry.sensor_type === 'cpu_temperature' ? 'bg-blue-900/50 text-blue-300' : 'bg-purple-900/50 text-purple-300'}">${sensorLabel}</span>
                            </td>
                            <td class="py-2 text-white font-mono">${escapeHtml(speedDisplay)}</td>
                            <td class="py-2 ${actionClass}">${escapeHtml(entry.action)}</td>
                            <td class="py-2 text-gray-300">${threshold}</td>
                            <td class="py-2 text-right">
                                <button onclick="editDsmEntry('${escapeHtml(scheme.type)}', ${i})"
                                        class="text-gray-500 hover:text-neon-cyan text-xs px-1">✎</button>
                            </td>
                        </tr>
                    `;
                }

                html += '</tbody></table>';
            } else {
                html += `<div class="text-gray-500 text-xs py-2">${t('dsm.no_entries', 'No entries')}</div>`;
            }

            html += '</div>';
        }

        html += '</div>';
        container.innerHTML = html;

    } catch (e) {
        container.innerHTML = `<div class="text-red-400 text-center py-8">Error loading DSM schemes: ${e.message}</div>`;
    }
}

function _schemeLabel(type) {
    const labels = {
        'DUAL_MODE_HIGH': 'High Performance',
        'DUAL_MODE_LOW': 'Quiet Mode',
        'FULL_SPEED': 'Full Speed',
        'STOP': 'Stop (Fan Off)',
        'FLAT': 'Flat Config',
    };
    return labels[type] || type;
}

async function editDsmEntry(schemeType, index) {
    const scheme = dsm.schemes.find(s => s.type === schemeType);
    if (!scheme || !scheme.entries[index]) return;

    const entry = scheme.entries[index];
    const newSpeed = prompt(`Fan speed % for ${entry.sensor_type} (threshold ${entry.threshold_temp}°C):`, entry.fan_speed || '20');
    if (newSpeed === null) return;

    const newAction = prompt(`Action (NONE or SHUTDOWN):`, entry.action || 'NONE');
    if (newAction === null) return;

    const newThreshold = prompt(`Threshold temperature °C:`, entry.threshold_temp || '0');
    if (newThreshold === null) return;

    // Update locally first (works for both local and remote)
    entry.fan_speed = parseInt(newSpeed) || 20;
    entry.action = newAction.toUpperCase() === 'SHUTDOWN' ? 'SHUTDOWN' : 'NONE';
    entry.threshold_temp = parseInt(newThreshold) || 0;

    if (store.currentRemoteNodeId) {
        // Remote — local edit only, applied when user clicks "Apply"
        renderDsmSchemeEditor(store.currentRemoteNodeId);
        return;
    }

    // Local — persist to scemd.xml immediately
    try {
        const resp = await fetch(`/api/dsm/scheme/${schemeType}/entry/${index}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                fan_speed_pct: parseInt(newSpeed) || 20,
                action: newAction.toUpperCase() === 'SHUTDOWN' ? 'SHUTDOWN' : 'NONE',
                threshold_temp: parseInt(newThreshold) || 0
            })
        });
        if (resp.ok) {
            renderDsmSchemeEditor();
        } else {
            const err = await resp.json();
            alert(err.message || t('dsm.entry_failed', 'Failed to update entry'));
        }
    } catch (e) {
        alert('Error: ' + e.message);
    }
}

async function applyDsmScheme(schemeType) {
    try {
        if (store.currentRemoteNodeId) {
            // Remote node — push scheme via WebSocket
            const node = store.nodesData.find(n => n.node_id === store.currentRemoteNodeId);
            const scheme = (node?.telemetry?.dsm_schemes || node?.config?.dsm_schemes || node?.dsm_schemes || []).find(s => s.type === schemeType);
            if (!scheme) {
                showToast(t('dsm.node_not_found', 'Node not found'), 'error');
                return;
            }
            socket.emit('server:dsm:apply', {
                node_id: store.currentRemoteNodeId,
                scheme_type: schemeType,
                entries: scheme.entries.map((e, i) => ({
                    index: i,
                    fan_speed_pct: e.fan_speed,
                    action: e.action,
                    threshold_temp: e.threshold_temp,
                })),
            });
            showToast(t('dsm.apply_remote', 'Scheme applied to remote agent'), 'success');
        } else {
            // Local server
            const resp = await fetch('/api/dsm/apply', { method: 'POST' });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(t('dsm.apply_ok', 'Scheme applied successfully'), 'success');
            } else {
                showToast(data.message || t('dsm.apply_failed', 'Failed to apply scheme'), 'error');
            }
        }
    } catch (e) {
        showToast(t('dsm.apply_failed', 'Failed to apply scheme') + ': ' + e.message, 'error');
    }
}

function runCalibration() {
    console.log('[FanControl] Starting calibration...');
    
    document.getElementById('calibrate-btn').disabled = true;
    document.getElementById('calibrate-loader')?.classList.remove('hidden');
    store.wizardStep = 'calibrating';
    
    const numPoints = parseInt(document.getElementById('calibration-points')?.value || '11');
    
    document.getElementById('calibration-modal')?.classList.remove('hidden');
    const _el1 = document.getElementById('calibration-status'); if (_el1) _el1.textContent = t('calibration.starting', 'Starting...');
    const _el2 = document.getElementById('calibration-progress-bar'); if (_el2) _el2.style.width = '0%';
    const _el3 = document.getElementById('calibration-step'); if (_el3) _el3.textContent = t('calibration.step_label', 'Step 0/${total}').replace('${current}', '0').replace('${total}', numPoints);
    
    fetch('/api/initialize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ num_points: numPoints })
    })
        .then(r => r.json())
        .then(data => {
            console.log('[FanControl] Calibration initiated:', data);
        })
        .catch(err => {
            console.error('Calibration error:', err);
            hideCalibrationModal();
            document.getElementById('calibrate-btn').disabled = false;
            document.getElementById('calibrate-loader')?.classList.add('hidden');
        });
}

function updateCalibrationModal(progress) {
    const modal = document.getElementById('calibration-modal');
    if (modal.classList.contains('hidden')) {
        modal.classList.remove('hidden');
    }
    
    const _el4 = document.getElementById('calibration-status'); if (_el4) _el4.textContent = progress.status;
    const _el5 = document.getElementById('calibration-step'); if (_el5) _el5.textContent = t('calibration.step_label', 'Step ${current}/${total}').replace('${current}', progress.step).replace('${total}', progress.total);
    
    const pct = progress.total > 0 ? (progress.step / progress.total * 100) : 0;
    const _el6 = document.getElementById('calibration-progress-bar'); if (_el6) _el6.style.width = `${pct}%`;
}

function hideCalibrationModal() {
    document.getElementById('calibration-modal')?.classList.add('hidden');
}

function updateCalibrationParam(param, value) {
    if (!store.currentFanId || !store.state || !store.state.fans) return;
    const fan = store.state.fans[store.currentFanId];
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

    saveFanCalibration(store.currentFanId, fan.calibration);
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
    
    const numPoints = parseInt(document.getElementById('calibration-points-settings')?.value || '11');
    
    document.getElementById('calibration-modal')?.classList.remove('hidden');
    const _el7 = document.getElementById('calibration-status'); if (_el7) _el7.textContent = t('calibration.starting', 'Starting...');
    const _el8 = document.getElementById('calibration-progress-bar'); if (_el8) _el8.style.width = '0%';
    const _el9 = document.getElementById('calibration-step'); if (_el9) _el9.textContent = t('calibration.step_label', 'Step 0/${total}').replace('${current}', '0').replace('${total}', numPoints);
    
    fetch('/api/initialize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ num_points: numPoints })
    })
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
    
    const fan = store.state?.fans?.[store.currentFanId];
    const fanSchedule = fan?.schedule || [];
    schedule.data = {};
    fanSchedule.forEach(item => {
        const key = `${item.day}_${item.time_start}`;
        schedule.data[key] = item;
    });
    
    // Build color map for cells
    const colorMap = {};
    const groups = {};
    fanSchedule.forEach(item => {
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
            const item = schedule.data[key];
            
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
        sensors: [...(item.sensors || [])].sort(),
        sensor_mode: item.sensor_mode
    });
}

function renderScheduleRules() {
    const container = document.getElementById('schedule-rules');
    if (!container) return;
    
    const fan = store.state?.fans?.[store.currentFanId];
    const fanSchedule = fan?.schedule || [];
    
    if (fanSchedule.length === 0) {
        container.innerHTML = `<p class="text-xs text-gray-500 italic">${t('schedule.no_rules', 'No rules configured')}</p>`;
        return;
    }
    
    // Group by identical settings
    const groups = {};
    fanSchedule.forEach(item => {
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
                const sen = store.allSensors.find(x => x.id === s);
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
                        ${t('schedule.edit', 'Edit')}
                    </button>
                    <button onclick="deleteRuleGroup(${gIdx}); event.stopPropagation()"
                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-bg transition-all flex-shrink-0">
                        ${t('schedule.delete', 'Del')}
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
                        ${t('schedule.edit', 'Edit')}
                    </button>
                    <button onclick="deleteSinglePeriod('${sp.day}', ${sp.from}, ${sp.to}); event.stopPropagation()"
                            class="text-[10px] text-gray-400 hover:text-neon-red px-1.5 py-0.5 rounded hover:bg-cyber-accent transition-all">
                        ${t('schedule.delete', 'Del')}
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
    schedule.expandedRuleGroups.forEach(idx => {
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
        schedule.expandedRuleGroups.delete(idx);
    } else {
        schedule.expandedRuleGroups.add(idx);
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
        delete schedule.data[key];
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
        delete schedule.data[key];
    });
    schedule.expandedRuleGroups.delete(idx);
    applyScheduleToFan();
}

function onScheduleMouseDown(e, day, hour) {
    e.preventDefault();
    schedule.isDragging = true;
    schedule.dragStartCell = { day, hour };
    schedule.selection = [{ day, hour }];
    highlightSelection();
}

function onScheduleMouseEnter(e, day, hour) {
    if (!schedule.isDragging || !schedule.dragStartCell) return;
    
    const startH = schedule.dragStartCell.hour;
    const startD = DAYS.indexOf(schedule.dragStartCell.day);
    const endD = DAYS.indexOf(day);
    const minD = Math.min(startD, endD);
    const maxD = Math.max(startD, endD);
    
    schedule.selection = [];
    
    if (minD === maxD) {
        // Same day: select hour range
        const hFrom = Math.min(startH, hour);
        const hTo = Math.max(startH, hour);
        for (let h = hFrom; h <= hTo; h++) {
            schedule.selection.push({ day: DAYS[minD], hour: h });
        }
    } else {
        // Cross-day: select ALL hours on each day in range
        for (let d = minD; d <= maxD; d++) {
            for (let h = 0; h < 24; h++) {
                schedule.selection.push({ day: DAYS[d], hour: h });
            }
        }
    }
    highlightSelection();
}

function highlightSelection() {
    clearHighlight();
    for (const cell of schedule.selection) {
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
    if (!schedule.isDragging) return;
    schedule.isDragging = false;
    
    if (schedule.selection.length === 1) {
        openScheduleEditor([schedule.selection[0]]);
    } else if (schedule.selection.length > 1) {
        openScheduleEditor([...schedule.selection]);
    }
    schedule.selection = [];
    clearHighlight();
});

// ============================================================================
// SCHEDULE EDITOR
// ============================================================================

function openScheduleEditor(cells) {
    schedule.editingCells = cells;
    schedule.editorSensors = [];
    
    const editor = document.getElementById('schedule-editor');
    editor.classList.remove('hidden');
    
    // Build human-readable period description
    document.getElementById('schedule-editor-cells').textContent = describeCells(cells);
    
    // Get existing data from first cell
    const key = `${cells[0].day}_${String(cells[0].hour).padStart(2, '0')}:00`;
    const existing = schedule.data[key];
    
    if (existing) {
        setScheduleMode(existing.mode);
        document.getElementById('sched-target-temp').value = existing.target_temp || 31;
        document.getElementById('sched-speed-slider').value = existing.speed_pct ?? 50;
        document.getElementById('sched-speed-value').textContent = `${existing.speed_pct ?? 50}%`;
        schedule.editorSensors = [...(existing.sensors || [])];
        if (existing.sensor_mode) setScheduleSensorMode(existing.sensor_mode);
    } else {
        setScheduleMode('auto');
        document.getElementById('sched-target-temp').value = 31;
        document.getElementById('sched-speed-slider').value = 50;
        document.getElementById('sched-speed-value').textContent = '50%';
        
        // Auto-fill sensors from first existing schedule item
        const fan = store.state?.fans?.[store.currentFanId];
        const fanSchedule = fan?.schedule || [];
        if (fanSchedule.length > 0) {
            const first = fanSchedule[0];
            schedule.editorSensors = [...(first.sensors || [])];
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
    
    if (schedule.editorSensors.length === 0) {
        container.innerHTML = `<span class="text-xs text-gray-500 italic">${t('editor.no_sensors', 'No sensors assigned')}</span>`;
        document.getElementById('sched-sensor-mode-section').classList.add('hidden');
        return;
    }
    
    container.innerHTML = schedule.editorSensors.map(s => {
        const sensor = store.allSensors.find(x => x.id === s);
        const label = sensor ? sensor.label : s;
        return `
            <span class="inline-flex items-center gap-1 bg-cyber-accent text-gray-300 text-xs px-2 py-1 rounded-full">
                ${escapeHtml(label)}
                <button onclick="removeScheduleSensor('${escapeHtml(s)}')" class="text-neon-red hover:text-red-400 ml-1">&times;</button>
            </span>
        `;
    }).join('');
    
    document.getElementById('sched-sensor-mode-section').classList.toggle('hidden', schedule.editorSensors.length <= 1);
}

function removeScheduleSensor(sensorId) {
    schedule.editorSensors = schedule.editorSensors.filter(s => s !== sensorId);
    updateScheduleEditorSensors();
}

function toggleScheduleSensorPopup() {
    const popup = document.getElementById('sensor-popup');
    const list = document.getElementById('sensor-popup-list');
    if (!popup || !list) return;
    
    if (popup.classList.contains('hidden')) {
        list.innerHTML = buildSensorCheckboxList(store.allSensors, schedule.editorSensors);
        popup.classList.remove('hidden');
        
        // Override close behavior for schedule context
        popup._scheduleMode = true;
    } else {
        // Collect checked sensors
        const checked = popup.querySelectorAll('input[type=checkbox]:checked');
        schedule.editorSensors = Array.from(checked).map(cb => cb.value);
        updateScheduleEditorSensors();
        popup.classList.add('hidden');
        popup._scheduleMode = false;
    }
}

function saveScheduleEdit() {
    const mode = document.querySelector('#sched-btn-auto.bg-neon-cyan') ? 'auto'
        : document.querySelector('#sched-btn-manual.bg-neon-cyan') ? 'manual' : 'off';
    
    const newItems = schedule.editingCells.map(cell => {
        const key = `${cell.day}_${String(cell.hour).padStart(2, '0')}:00`;
        const item = {
            day: cell.day,
            time_start: String(cell.hour).padStart(2, '0') + ':00',
            time_end: String(cell.hour).padStart(2, '0') + ':59',
            mode: mode
        };
        
        if (mode === 'auto') {
            item.target_temp = parseInt(document.getElementById('sched-target-temp').value) || 31;
            item.sensors = [...schedule.editorSensors];
            const activeSensorMode = document.querySelector('#sched-btn-sensor-max.bg-neon-cyan') ? 'max'
                : document.querySelector('#sched-btn-sensor-min.bg-neon-cyan') ? 'min' : 'avg';
            item.sensor_mode = activeSensorMode;
        } else if (mode === 'manual') {
            item.speed_pct = parseInt(document.getElementById('sched-speed-slider').value) || 50;
        }
        
        schedule.data[key] = item;
        return item;
    });
    
    closeScheduleEditor();
    applyScheduleToFan();
}

function deleteScheduleEdit() {
    for (const cell of schedule.editingCells) {
        const key = `${cell.day}_${String(cell.hour).padStart(2, '0')}:00`;
        delete schedule.data[key];
    }
    closeScheduleEditor();
    applyScheduleToFan();
}

function closeScheduleEditor() {
    document.getElementById('schedule-editor').classList.add('hidden');
    schedule.editingCells = [];
}

function clearSchedule() {
    schedule.data = {};
    applyScheduleToFan();
}

function fillScheduleDefaults() {
    const fan = store.state?.fans?.[store.currentFanId];
    const defaultSensors = fan?.sensors || [];
    const defaultSensorMode = fan?.sensor_mode || 'max';
    const defaultTemp = fan?.target_temp || 31;
    
    for (const day of DAYS) {
        for (let hour = 0; hour < 24; hour++) {
            const key = `${day}_${String(hour).padStart(2, '0')}:00`;
            if (!schedule.data[key]) {
                schedule.data[key] = {
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
    const fanSchedule = Object.values(schedule.data);
    
    // Update local state immediately so render sees new data
    if (store.state?.fans?.[store.currentFanId]) {
        store.state.fans[store.currentFanId].schedule = fanSchedule;
    }
    
    sendControl({
        action: 'set_fan_config',
        fan: store.currentFanId,
        schedule: fanSchedule
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
        dayStr = t('schedule.days', '${count} days').replace('${count}', days.length);
    }
    
    if (hours.length === 24) {
        return `${dayStr}, 00:00-23:59`;
    }
    
    const minH = String(Math.min(...hours)).padStart(2, '0');
    const maxH = String(Math.max(...hours) + 1).padStart(2, '0');
    return `${dayStr}, ${minH}:00-${maxH.length > 5 ? '00:00 next day' : maxH + ':00'}`;
}

function validateSchedule() {
    const fan = store.state?.fans?.[store.currentFanId];
    const fanSchedule = fan?.schedule || [];
    const coverage = document.getElementById('schedule-coverage');
    const warning = document.getElementById('schedule-incomplete-warning');
    const detail = document.getElementById('schedule-incomplete-detail');
    
    if (!coverage) return;
    
    const total = 7 * 24;
    const filled = fanSchedule.length;
    const pct = Math.round((filled / total) * 100);
    
    coverage.textContent = `${filled}/${total} (${pct}%)`;
    coverage.className = pct === 100 ? 'text-xs text-neon-green' : 'text-xs text-neon-orange';
    
    if (pct < 100) {
        const emptyDays = [];
        for (let i = 0; i < DAYS.length; i++) {
            const dayHours = fanSchedule.filter(s => s.day === DAYS[i]).length;
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
        fetchLogSettings();
        fetchTelegramStatus();
        autoCheckUpdate();
    }
}

function updateLangButtons() {
    const enBtn = document.getElementById('lang-btn-en');
    const ruBtn = document.getElementById('lang-btn-ru');
    const setupEn = document.getElementById('setup-lang-en');
    const setupRu = document.getElementById('setup-lang-ru');
    
    if (enBtn) enBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${i18n.currentLang === 'en' ? BTN_ACTIVE : BTN_INACTIVE}`;
    if (ruBtn) ruBtn.className = `flex-1 py-2.5 px-4 rounded-lg text-sm font-semibold transition-all duration-300 border ${i18n.currentLang === 'ru' ? BTN_ACTIVE : BTN_INACTIVE}`;
    if (setupEn) setupEn.className = `text-xs px-2 py-1 rounded border transition-all ${i18n.currentLang === 'en' ? BTN_ACTIVE : BTN_INACTIVE}`;
    if (setupRu) setupRu.className = `text-xs px-2 py-1 rounded border transition-all ${i18n.currentLang === 'ru' ? BTN_ACTIVE : BTN_INACTIVE}`;
    
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

// ============================================================================
// LOGGING LEVEL
// ============================================================================

async function fetchLogSettings() {
    try {
        const resp = await fetch('/api/logging');
        const data = await resp.json();
        logging.level = data.level || 'INFO';
        logging.retention = data.retention_days || 30;
        updateLogLevelButtons();
        updateRetentionButtons();
    } catch (err) { console.error('Failed to fetch log settings:', err); }
}

function updateLogLevelButtons() {
    ['DEBUG', 'INFO', 'WARNING', 'ERROR'].forEach(level => {
        const btn = document.getElementById(`log-btn-${level}`);
        if (btn) {
            btn.className = `flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border ${logging.level === level ? BTN_ACTIVE : BTN_INACTIVE}`;
        }
    });
}

async function setLogLevel(level) {
    try {
        const resp = await fetch('/api/logging', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ level })
        });
        if (resp.ok) {
            logging.level = level;
            updateLogLevelButtons();
        }
    } catch (err) { console.error('Failed to set log level:', err); }
}

function updateRetentionButtons() {
    [7, 14, 30, 60, 90, 180, 365].forEach(days => {
        const btn = document.getElementById(`retention-btn-${days}`);
        if (btn) {
            btn.className = `flex-1 py-2 px-2 rounded-lg text-xs font-semibold transition-all duration-300 border min-w-[40px] ${logging.retention === days ? BTN_ACTIVE : BTN_INACTIVE}`;
        }
    });
}

async function setLogRetention(days) {
    try {
        const resp = await fetch('/api/logging', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ retention_days: days })
        });
        if (resp.ok) {
            logging.retention = days;
            updateRetentionButtons();
        }
    } catch (err) { console.error('Failed to set log retention:', err); }
}

function setTempUnit(unit) {
    saveSettings({ tempUnit: unit });
    updateSettingsUI();
    // Re-render current data
    if (store.state) updateUI(store.state);
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

function scheduleAutoUpdate() {
    if (timers.autoUpdate) { clearInterval(timers.autoUpdate); timers.autoUpdate = null; }
    const ms = getSettings().autoUpdateCheck;
    if (ms > 0) {
        timers.autoUpdate = setInterval(() => { update.checked = false; autoCheckUpdate(); }, ms);
    }
}

// ─── Telegram Notifications ──────────────────────────────────────────

let tgConfig = { configured: false, enabled: false, events: {} };

async function fetchTelegramStatus() {
    try {
        const resp = await fetch('/api/telegram/status');
        tgConfig = await resp.json();
        // Update UI
        const toggle = document.getElementById('tg-enabled');
        const tokenInput = document.getElementById('tg-bot-token');
        const chatInput = document.getElementById('tg-chat-id');
        if (toggle) toggle.checked = tgConfig.enabled;
        if (tokenInput) tokenInput.value = tgConfig.has_token ? '••••••••' : '';
        if (chatInput) chatInput.value = tgConfig.has_chat_id ? (store.state?.telegram_chat_id || '') : '';
        // Update event checkboxes
        const events = tgConfig.events || {};
        const fanCb = document.getElementById('tg-evt-fan');
        const agentCb = document.getElementById('tg-evt-agent');
        const updateCb = document.getElementById('tg-evt-update');
        if (fanCb) fanCb.checked = events.fan_health !== false;
        if (agentCb) agentCb.checked = events.agent_status !== false;
        if (updateCb) updateCb.checked = events.updates !== false;
    } catch (err) { console.error('Failed to fetch Telegram status:', err); }
}

function saveTelegramConfig() {
    const enabled = document.getElementById('tg-enabled')?.checked || false;
    const tokenInput = document.getElementById('tg-bot-token')?.value || '';
    const chatId = document.getElementById('tg-chat-id')?.value || '';
    const events = {
        fan_health: document.getElementById('tg-evt-fan')?.checked !== false,
        agent_status: document.getElementById('tg-evt-agent')?.checked !== false,
        updates: document.getElementById('tg-evt-update')?.checked !== false,
    };

    const body = { enabled, events };
    // Only send token/chat_id if they're not placeholder
    if (tokenInput && tokenInput !== '••••••••') body.bot_token = tokenInput;
    if (chatId) body.chat_id = chatId;

    fetch('/api/telegram/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }).then(r => r.json()).then(data => {
        if (data.status === 'ok') {
            showToast('Telegram config saved', 'success');
            fetchTelegramStatus();
        } else {
            showToast(data.message || 'Save failed', 'error');
        }
    }).catch(err => showToast('Save failed: ' + err.message, 'error'));
}

async function testTelegram() {
    const btn = document.getElementById('tg-test-btn');
    const result = document.getElementById('tg-test-result');
    btn.disabled = true;
    btn.textContent = '⏳ Sending...';
    result.classList.add('hidden');

    try {
        const resp = await fetch('/api/telegram/test', { method: 'POST' });
        const data = await resp.json();
        result.classList.remove('hidden');
        if (data.status === 'ok') {
            result.className = 'text-xs text-center text-neon-green';
            result.textContent = '✓ Message sent!';
        } else {
            result.className = 'text-xs text-center text-neon-red';
            result.textContent = '✗ ' + (data.message || 'Failed');
        }
    } catch (err) {
        result.classList.remove('hidden');
        result.className = 'text-xs text-center text-neon-red';
        result.textContent = '✗ ' + err.message;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '📱 <span data-i18n="settings.telegram_test">Отправить тест</span>';
        setTimeout(() => { result.classList.add('hidden'); }, 5000);
    }
}

window.saveTelegramConfig = saveTelegramConfig;
window.testTelegram = testTelegram;

// ─── End Telegram ────────────────────────────────────────────────────

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

function copyAgentToken() {
    const token = document.getElementById('agent-token-value').textContent;
    if (token && navigator.clipboard) {
        navigator.clipboard.writeText(token).then(() => showToast(t('toast.token_copied', 'Token copied!'), 'success'));
    }
}

function openUpdateModal() {
    const modal = document.getElementById('update-modal');
    const steps = document.getElementById('update-modal-steps');
    const progress = document.getElementById('update-modal-progress');
    const result = document.getElementById('update-modal-result');
    const applyBtn = document.getElementById('update-modal-apply');
    const closeBtn = document.getElementById('update-modal-close');
    
    const onlineAgentCount = store.nodesData.filter(n => n.status === 'online').length;
    const agentStep = onlineAgentCount > 0
        ? `<div id="upd-step-agents" class="flex items-center gap-3 text-sm opacity-40">
            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-agents-icon">1</span>
            <span class="text-gray-300">${t('settings.step_agents', 'Updating agents...')}</span>
        </div>`
        : '';
    const waitStep = onlineAgentCount > 0
        ? `<div id="upd-step-wait" class="flex items-center gap-3 text-sm opacity-40">
            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-wait-icon">2</span>
            <span class="text-gray-300">${t('update.wait_agents', 'Waiting for agents...')}</span>
        </div>`
        : '';
    const serverStepNum = onlineAgentCount > 0 ? '3' : '1';
    const restartStepNum = onlineAgentCount > 0 ? '4' : '2';
    steps.innerHTML = `
        ${agentStep}
        ${waitStep}
        <div id="upd-step-pull" class="flex items-center gap-3 text-sm ${onlineAgentCount > 0 ? 'opacity-40' : ''}">
            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-pull-icon">${serverStepNum}</span>
            <span class="text-gray-300">${t('settings.step_pull', 'Pulling latest code...')}</span>
        </div>
        <div id="upd-step-restart" class="flex items-center gap-3 text-sm opacity-40">
            <span class="w-5 h-5 rounded-full border-2 border-gray-600 flex-shrink-0 flex items-center justify-center text-[10px]" id="upd-step-restart-icon">${restartStepNum}</span>
            <span class="text-gray-300">${t('settings.step_restart', 'Restarting container...')}</span>
        </div>
    `;

    // Show agents list if there are online agents
    const agentsSection = document.getElementById('update-modal-agents');
    const agentsList = document.getElementById('update-modal-agents-list');
    const onlineAgents = store.nodesData.filter(n => n.status === 'online');
    if (agentsSection) {
        if (onlineAgents.length > 0) {
            agentsSection.classList.remove('hidden');
            if (agentsList) {
                agentsList.innerHTML = onlineAgents.map(agent => {
                    const ver = agent.agent_version || '—';
                    const serverVer = store.state?.config_version || '?';
                    const needsUpdate = ver !== '—' && serverVer !== '?' && ver !== serverVer;
                    const checked = agent.auto_update ? 'checked' : '';
                    return `
                        <div class="flex items-center justify-between py-1.5 px-2 rounded bg-cyber-accent border border-gray-700">
                            <div class="flex items-center gap-2 min-w-0">
                                <span class="w-2 h-2 rounded-full ${needsUpdate ? 'bg-orange-400' : 'bg-neon-green'} flex-shrink-0"></span>
                                <span class="text-xs text-gray-300 truncate">${escapeHtml(agent.name || agent.node_id)}</span>
                                <span class="text-[10px] ${needsUpdate ? 'text-orange-400' : 'text-gray-500'}">${ver}</span>
                            </div>
                            <label class="flex items-center gap-1 cursor-pointer flex-shrink-0">
                                <input type="checkbox" class="accent-neon-cyan w-3.5 h-3.5" ${checked}
                                    onchange="toggleAgentAutoUpdate('${agent.node_id}', this.checked)">
                                <span class="text-[10px] text-gray-400">auto</span>
                            </label>
                        </div>`;
                }).join('');
            }
        } else {
            agentsSection.classList.add('hidden');
        }
    }
    
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

async function toggleAgentAutoUpdate(nodeId, enabled) {
    try {
        await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/auto-update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        // Update local state so startUpdate() respects the change immediately
        const node = store.nodesData.find(n => n.node_id === nodeId);
        if (node) node.auto_update = enabled;
    } catch (err) { console.error('Failed to toggle auto-update:', err); }
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


function checkAgentsDone() {
    const pending = Object.entries(update.agentStates).filter(([_, s]) =>
        !['synced', 'error', 'skipped'].includes(s.status));
    if (pending.length === 0 && update.resolve) {
        update.resolve();
        update.resolve = null;
    }
}

function renderUpdateAgentProgress() {
    const el = document.getElementById('update-modal-agents-progress');
    if (!el) return;
    const serverVer = store.state?.config_version || '?';
    let html = '';
    for (const [nid, st] of Object.entries(update.agentStates)) {
        const node = store.nodesData.find(n => n.node_id === nid);
        const name = node?.name || nid;
        let statusIcon = '', statusText = '', actions = '';
        switch (st.status) {
            case 'pending':
                statusIcon = '<span class="w-2 h-2 rounded-full bg-gray-500 animate-pulse"></span>';
                statusText = '<span class="text-gray-400">' + t('update.sending', 'Sending...') + '</span>';
                break;
            case 'pulling':
                statusIcon = '<span class="w-2 h-2 rounded-full bg-neon-cyan animate-pulse"></span>';
                statusText = `<span class="text-neon-cyan">${t('update.pulling', 'Pulling code...')} ${st.version || ''}</span>`;
                break;
            case 'synced':
                statusIcon = '<span class="w-2 h-2 rounded-full bg-neon-green"></span>';
                statusText = `<span class="text-neon-green">${t('update.synced', 'Synced, restarting...')}</span>`;
                break;
            case 'error':
                statusIcon = '<span class="w-2 h-2 rounded-full bg-neon-red"></span>';
                statusText = `<span class="text-neon-red">${t('update.error', 'Error')}: ${escapeHtml(st.message || 'unknown')}</span>`;
                actions = `<button onclick="retryAgentUpdate('${nid}')" class="text-[10px] text-neon-cyan hover:underline ml-1">${t('update.retry', 'Retry')}</button>
                    <button onclick="skipAgentUpdate('${nid}')" class="text-[10px] text-gray-500 hover:underline ml-1">${t('update.skip', 'Skip')}</button>`;
                break;
            case 'skipped':
                statusIcon = '<span class="w-2 h-2 rounded-full bg-gray-600"></span>';
                statusText = `<span class="text-gray-500">${t('update.skipped', 'Skipped')}</span>`;
                break;
            case 'version_mismatch':
                statusIcon = '<span class="w-2 h-2 rounded-full bg-yellow-400"></span>';
                statusText = `<span class="text-yellow-400">${t('update.version_mismatch', 'Version mismatch after update')}</span>`;
                actions = `<button onclick="retryAgentUpdate('${nid}')" class="text-[10px] text-neon-cyan hover:underline ml-1">${t('update.retry', 'Retry')}</button>
                    <button onclick="skipAgentUpdate('${nid}')" class="text-[10px] text-gray-500 hover:underline ml-1">${t('update.skip', 'Skip')}</button>`;
                break;
        }
        html += `<div class="flex items-center gap-2 py-1 px-2 rounded bg-cyber-accent border border-gray-700 text-xs">
            ${statusIcon}
            <span class="text-gray-300 truncate flex-1">${escapeHtml(name)}</span>
            ${statusText}
            ${actions}
            <button onclick="requestAgentLogs('${nid}')" class="text-[10px] text-gray-500 hover:text-neon-cyan ml-1" title="${t('update.view_logs', 'View logs')}">📋</button>
        </div>`;
    }
    el.innerHTML = html;
}

function retryAgentUpdate(nodeId) {
    update.agentStates[nodeId] = { status: 'pending' };
    renderUpdateAgentProgress();
    fetch('/api/update/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ node_ids: [nodeId] }),
    }).then(r => r.json()).then(data => {
        if (data.already_ok && data.already_ok.includes(nodeId)) {
            update.agentStates[nodeId] = { status: 'skipped' };
            renderUpdateAgentProgress();
            checkAgentsDone();
        }
    }).catch(err => console.error('Agent update retry error:', err));
}

function skipAgentUpdate(nodeId) {
    if (update.agentStates[nodeId]) {
        update.agentStates[nodeId].status = 'skipped';
    }
    renderUpdateAgentProgress();
    checkAgentsDone();
}

function requestAgentLogs(nodeId) {
    fetch(`/api/nodes/${encodeURIComponent(nodeId)}/request-logs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lines: 150 }),
    }).catch(() => {
        showToast(t('update.logs_failed', 'Failed to request logs'), 'error');
    });
}

function renderAgentLogsModal(nodeId, lines) {
    const node = store.nodesData.find(n => n.node_id === nodeId);
    const name = node?.name || nodeId;
    const overlay = document.createElement('div');
    overlay.className = 'fixed inset-0 bg-black/70 z-[90] flex items-center justify-center';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    overlay.innerHTML = `
        <div class="bg-gray-900 border border-gray-700 rounded-xl w-[700px] max-h-[80vh] flex flex-col shadow-2xl">
            <div class="flex items-center justify-between px-4 py-3 border-b border-gray-700">
                <h3 class="text-white font-semibold text-sm">📋 ${escapeHtml(name)} — ${t('update.agent_logs', 'Logs')}</h3>
                <div class="flex gap-2">
                    <button onclick="requestAgentLogs('${nodeId}')" class="text-xs text-gray-400 hover:text-neon-cyan">🔄 ${t('discovery.refresh', 'Refresh')}</button>
                    <button onclick="this.closest('.fixed').remove()" class="text-gray-400 hover:text-white text-lg">&times;</button>
                </div>
            </div>
            <pre class="flex-1 overflow-auto p-4 text-[11px] text-gray-300 font-mono whitespace-pre-wrap">${escapeHtml(lines.join(''))}</pre>
        </div>
    `;
    document.body.appendChild(overlay);
}

async function startUpdate() {
    const applyBtn = document.getElementById('update-modal-apply');
    const progress = document.getElementById('update-modal-progress');
    const bar = document.getElementById('update-modal-bar');
    const result = document.getElementById('update-modal-result');
    const closeBtn = document.getElementById('update-modal-close');

    const serverVer = store.state?.config_version || '?';
    const onlineAgents = store.nodesData.filter(n => n.status === 'online');
    // Only update agents with auto_update enabled
    // auto_update may be boolean or integer (0/1) from SQLite
    const outdatedAgents = onlineAgents.filter(n => {
        const au = n.auto_update;
        return au && au !== 0 && au !== '0';
    });

    applyBtn.classList.add('hidden');
    closeBtn.classList.add('hidden');
    progress.classList.remove('hidden');
    bar.style.width = '10%';

    // Step 1: Send update to agents (while server is still running)
    if (outdatedAgents.length > 0) {
        setStepState('agents', 'active');
        bar.style.width = '5%';

        // Initialize agent states
        update.agentStates = {};
        outdatedAgents.forEach(n => {
            update.agentStates[n.node_id] = { status: 'pending' };
        });
        renderUpdateAgentProgress();

        try {
            const agentResp = await fetch('/api/update/agents', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ node_ids: outdatedAgents.map(n => n.node_id) }),
            });
            const agentData = await agentResp.json();

            // Mark agents already at correct version as skipped (not pending)
            if (agentData.already_ok) {
                agentData.already_ok.forEach(nid => {
                    if (update.agentStates[nid]) {
                        update.agentStates[nid] = { status: 'skipped' };
                    }
                });
            }
            // Mark agents with no SID as pending (will update via polling)
            if (agentData.no_sid) {
                agentData.no_sid.forEach(nid => {
                    if (update.agentStates[nid]) {
                        update.agentStates[nid] = { status: 'pending' };
                    }
                });
            }

            setStepState('agents', 'done');
            bar.style.width = '15%';

            // Step 2: Wait for all agents in real-time via WebSocket
            setStepState('wait', 'active');
            result.classList.remove('hidden');
            result.className = 'text-sm mb-4 p-3 rounded-lg bg-cyan-900/20 border border-cyan-800 text-neon-cyan';
            result.innerHTML = `<div class="font-semibold mb-1">${t('update.waiting_agents', 'Waiting for agents to update...')}</div>
                <div id="update-modal-agents-progress" class="space-y-1 mt-2"></div>`;
            renderUpdateAgentProgress();

            // Wait for all agents to finish (no timeout)
            await new Promise((resolve) => {
                update.resolve = resolve;
                // Also check immediately in case all already done
                checkAgentsDone();
            });

            const skipped = Object.values(update.agentStates).filter(s => s.status === 'skipped').length;
            const errors = Object.values(update.agentStates).filter(s => s.status === 'error').length;
            setStepState('wait', errors > 0 && skipped === 0 ? 'error' : 'done');

            if (errors > 0 && skipped === 0) {
                result.innerHTML += `<div class="text-yellow-400 text-xs mt-2">${t('update.agents_errors', 'Some agents had errors. Skip them or retry, then continue.')}</div>`;
                bar.style.width = '35%';
                // Show continue button so user can proceed with server update
                const continueBtn = document.createElement('button');
                continueBtn.textContent = t('update.continue_server', 'Continue server update');
                continueBtn.className = 'mt-2 px-3 py-1.5 bg-neon-cyan/20 border border-neon-cyan/50 rounded text-xs text-neon-cyan hover:bg-neon-cyan/30 transition';
                continueBtn.onclick = () => {
                    continueBtn.remove();
                    startServerUpdate(bar, result, applyBtn, closeBtn);
                };
                result.appendChild(continueBtn);
                closeBtn.classList.remove('hidden');
                return;
            }

            bar.style.width = '35%';
        } catch (e) {
            console.error('Failed to notify agents:', e);
            setStepState('agents', 'error');
        }
    }

    // Step 3: Git pull on server
    await startServerUpdate(bar, result, applyBtn, closeBtn);
}

async function startServerUpdate(bar, result, applyBtn, closeBtn) {
    setStepState('pull', 'active');
    bar.style.width = '40%';

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

        setStepState('pull', 'done');
        bar.style.width = '60%';

        // Step 4: Restart
        setStepState('restart', 'active');
        bar.style.width = '80%';

        result.classList.remove('hidden');
        result.className = 'text-sm mb-4 p-3 rounded-lg bg-green-900 bg-opacity-20 border border-green-800 text-neon-green';
        result.innerHTML = `
            <div class="font-semibold mb-1">${t('settings.update_success', 'Update complete!')}</div>
            <div class="text-gray-400">${t('settings.restart_notice', 'Container is restarting. Page will reload in 10 seconds...')}</div>
        `;

        bar.style.width = '100%';
        setStepState('restart', 'done');
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

function openUpdateAgentsModal() {
    const serverVer = store.state?.config_version || '?';
    const outdated = store.nodesData.filter(n =>
        n.status === 'online' && n.agent_version && n.agent_version !== serverVer);

    if (outdated.length === 0) {
        showToast(t('nodes.all_up_to_date', 'All agents are up to date'), 'info');
        return;
    }

    // Open the full update modal with agent progress tracking
    openUpdateModal();
}

async function updateAgentsNow(nodeIds) {
    try {
        showToast(t('nodes.updating_agents', 'Sending update to agents...'), 'info');
        const resp = await fetch('/api/update/agents', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ node_ids: nodeIds }),
            signal: AbortSignal.timeout(30000),
        });
        if (!resp.ok) {
            const text = await resp.text().catch(() => '');
            const msg = text.includes('<!doctype') ? `Server error (${resp.status})` : text;
            showToast(msg || `HTTP ${resp.status}`, 'error');
            return;
        }
        const data = await resp.json();
        if (data.status === 'ok') {
            showToast(
                t('nodes.agents_update_sent', 'Update sent to {count} agent(s)').replace('{count}', data.updated.length),
                'success'
            );
        } else {
            showToast(data.message || t('toast.update_failed', 'Update failed'), 'error');
        }
    } catch (e) {
        showToast(t('common.error', 'Error') + ': ' + e.message, 'error');
    }
}

function updateSingleAgent(nodeId) {
    updateAgentsNow([nodeId]);
}

async function autoCheckUpdate() {
    if (update.checked) return;
    update.checked = true;
    await checkForUpdates();
}

async function switchLanguage(code) {
    if (code === i18n.currentLang) return;
    
    const success = await loadLang(code);
    if (success) {
        updateLangButtons();
        // Save to server config
        fetch('/api/language', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language: code })
        }).catch(err => console.error('Language save error:', err));
        
        // Re-render dynamic content
        if (store.currentFanId) {
            const fan = store.state?.fans?.[store.currentFanId];
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
        if (dashboard.saveTimer) {
            clearTimeout(dashboard.saveTimer);
            saveDashboardToServer();
        }
    });

    // Load language
    await loadLang(i18n.currentLang);
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

async function loadNodes() {
    try {
        const resp = await fetch('/api/nodes');
        store.nodesData = await resp.json();
        // Remove picker cards for deleted nodes
        const validSources = new Set(['local', ...store.nodesData.map(n => n.stable_id || n.node_id)]);
        const cards = getPickerCards();
        const cleaned = cards.filter(c => validSources.has(c.source || 'local'));
        if (cleaned.length !== cards.length) {
            setPickerCards(cleaned);
            console.log(`[FanControl] Removed ${cards.length - cleaned.length} cards for deleted nodes`);
        }
        buildServerTree();
        renderNodesOverview();
    } catch (e) {
        console.error('[FanControl] Failed to load nodes:', e);
    }
}

// renderNodeSidebar removed — nodes are rendered via buildServerTree/renderRemoteNodeTree

function renderNodesOverview() {
    const container = document.getElementById('nodes-grid-inner');
    if (!container) return;
    
    let html = '';
    for (const node of store.nodesData) {
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
                        ${node.control_mode === 'manual' ? `<span class="text-yellow-400 text-xs">&#9888; ${t('node.detail.manual', 'Manual')}</span>` : ''}
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
    
    if (store.nodesData.length === 0) {
        html = `<div class="text-gray-500 text-center py-8 col-span-2">${t('nodes.no_nodes', 'No nodes connected. Add a node to get started.')}</div>`;
    }
    
    container.innerHTML = html;
}

function selectNode(nodeId) {
    store.selectedNodeId = nodeId;
    store.currentView = 'node-detail';
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
    const container = document.getElementById('node-detail-inner');
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
                <div class="space-y-2">${fansHtml || `<div class="text-gray-500 text-sm">${t('node.detail.no_fans', 'No fan data')}</div>`}</div>
            </div>
            <div>
                <h3 class="text-white font-semibold mb-3">${t('node.temperatures', 'Temperatures')}</h3>
                <div class="space-y-2">${tempsHtml || `<div class="text-gray-500 text-sm">${t('node.detail.no_temps', 'No temperature data')}</div>`}</div>
            </div>
        </div>
    `;
}

function showView(view) {
    store.currentView = view;

    const canvas = document.getElementById('dashboard-canvas-container');
    const inspector = document.getElementById('inspector-container');
    const addBtn = document.getElementById('dashboard-add-btn');
    const groupBtn = document.getElementById('dashboard-group-btn');
    const nodesGrid = document.getElementById('nodes-grid');
    const nodeDetail = document.getElementById('node-detail-content');
    const dsmScheme = document.getElementById('dsm-scheme-container');

    // Hide all views first
    [canvas, inspector, nodesGrid, nodeDetail, dsmScheme].forEach(el => {
        if (el) el.classList.add('hidden');
    });
    [addBtn, groupBtn].forEach(el => {
        if (el) el.classList.add('hidden');
    });

    // Show the requested view
    if (view === 'dashboard') {
        if (canvas) canvas.classList.remove('hidden');
        if (addBtn) addBtn.classList.remove('hidden');
        if (groupBtn) groupBtn.classList.remove('hidden');
    } else if (view === 'inspector') {
        if (inspector) inspector.classList.remove('hidden');
    } else if (view === 'nodes') {
        if (nodesGrid) nodesGrid.classList.remove('hidden');
        renderNodesOverview();
    } else if (view === 'node-detail') {
        if (nodeDetail) nodeDetail.classList.remove('hidden');
    } else if (view === 'dsm-scheme') {
        if (dsmScheme) dsmScheme.classList.remove('hidden');
        renderDsmSchemeEditor(store.currentRemoteNodeId);
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
    const nameInput = document.getElementById('new-node-name');
    const ipInput = document.getElementById('new-node-ip');
    const name = nameInput?.value?.trim();
    const ip = ipInput?.value?.trim();
    if (!name && !ip) return;

    try {
        let resp;
        if (ip) {
            // Add by IP — probes agent automatically
            resp = await fetch('/api/nodes/add-by-ip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: name || ip, ip })
            });
        } else {
            resp = await fetch('/api/nodes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
        }
        if (resp.ok) {
            nameInput.value = '';
            ipInput.value = '';
            loadNodes();
        } else {
            const err = await resp.json().catch(() => ({}));
            showToast(err.error || t('toast.add_node_failed', 'Failed to add node'), 'error');
        }
    } catch (e) {
        console.error('[FanControl] Failed to add node:', e);
        showToast(t('toast.add_node_failed', 'Failed to add node') + ': ' + e.message, 'error');
    }
}

async function deleteNode(nodeId) {
    if (!confirm(t('nodes.confirm_delete', 'Delete this node?'))) return;
    try {
        const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, { method: 'DELETE' });
        if (resp.ok) {
            if (store.selectedNodeId === nodeId) {
                store.selectedNodeId = null;
                showView('nodes');
            }
            loadNodes();
        } else {
            const err = await resp.json().catch(() => ({}));
            console.error('[FanControl] Delete failed:', resp.status, err);
            showToast(t('toast.delete_failed', 'Delete failed') + ': ' + (err.error || resp.status), 'error');
        }
    } catch (e) {
        console.error('[FanControl] Failed to delete node:', e);
        showToast(t('toast.delete_failed', 'Delete failed') + ': ' + e.message, 'error');
    }
}

function showNodeSettings(nodeId) {
    const node = store.nodesData.find(n => n.node_id === nodeId);
    if (!node) return;
    document.getElementById('node-settings-id').value = nodeId;
    document.getElementById('node-settings-name').value = node.name || '';
    document.getElementById('node-settings-ip').value = node.ip || '';
    document.getElementById('node-settings-port').value = node.port || 5059;
    const versionEl = document.getElementById('node-settings-version');
    if (versionEl) {
        const serverVer = store.state?.config_version || '?';
        const agentVer = node.agent_version || '—';
        const needsUpdate = agentVer !== '—' && serverVer !== '?' && agentVer !== serverVer;
        versionEl.textContent = agentVer;
        versionEl.className = needsUpdate
            ? 'text-sm text-orange-400'
            : agentVer !== '—' ? 'text-sm text-neon-green' : 'text-sm text-gray-500';
    }
    const autoUpdateCb = document.getElementById('node-settings-auto-update');
    if (autoUpdateCb) {
        autoUpdateCb.checked = !!node.auto_update;
        autoUpdateCb.onchange = async () => {
            try {
                await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/auto-update`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: autoUpdateCb.checked }),
                });
            } catch (e) {
                console.error('Failed to toggle auto-update:', e);
            }
        };
    }
    document.getElementById('node-settings-modal').classList.remove('hidden');
}

function hideNodeSettings() {
    document.getElementById('node-settings-modal').classList.add('hidden');
}

function openServerNameEdit() {
    const input = document.getElementById('server-name-input');
    input.value = store.state.server_name || '';
    document.getElementById('server-name-modal').classList.remove('hidden');
    input.focus();
    input.select();
}

function hideServerNameModal() {
    document.getElementById('server-name-modal').classList.add('hidden');
}

async function saveServerName() {
    const name = document.getElementById('server-name-input').value.trim();
    if (!name) { showToast(t('toast.name_required', 'Name required'), 'error'); return; }

    try {
        const resp = await fetch('/api/server-name', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        if (resp.ok) {
            hideServerNameModal();
            store.state.server_name = name;
            showToast(t('toast.server_renamed', 'Server renamed'), 'success');
        } else {
            const err = await resp.json().catch(() => ({}));
            showToast(err.error || t('toast.save_failed', 'Save failed'), 'error');
        }
    } catch (e) {
        showToast(t('toast.save_failed', 'Save failed') + ': ' + e.message, 'error');
    }
}

async function saveNodeSettings() {
    const nodeId = document.getElementById('node-settings-id').value;
    const name = document.getElementById('node-settings-name').value.trim();
    const ip = document.getElementById('node-settings-ip').value.trim();
    const port = parseInt(document.getElementById('node-settings-port').value) || 5059;
    if (!name) { showToast(t('toast.name_required', 'Name required'), 'error'); return; }

    try {
        const resp = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, ip, port })
        });
        if (resp.ok) {
            hideNodeSettings();
            loadNodes();
        } else {
            const err = await resp.json().catch(() => ({}));
            showToast(err.error || t('toast.save_failed', 'Save failed'), 'error');
        }
    } catch (e) {
        showToast(t('toast.save_failed', 'Save failed') + ': ' + e.message, 'error');
    }
}

async function scanForAgents() {
    const btn = document.getElementById('scan-agents-btn');
    const list = document.getElementById('discovered-agents-list');
    if (!list) return;

    btn.disabled = true;
    btn.textContent = '...';
    list.classList.remove('hidden');
    list.innerHTML = `<div class="text-gray-500 text-xs py-1">${t('discovery.scanning', 'Scanning network...')}</div>`;

    try {
        const [discoverResp, discoveredResp, subnetResp] = await Promise.all([
            fetch('/api/nodes/discover'),
            fetch('/api/discovered'),
            fetch('/api/nodes/scan-subnet', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
        ]);

        const scanResults = await discoverResp.json();
        const pendingAgents = await discoveredResp.json();
        const subnetResults = await subnetResp.json();

        // Merge results: SSDP + subnet scan, deduplicate by IP
        const merged = new Map();
        for (const a of (Array.isArray(scanResults) ? scanResults : [])) {
            if (a.ip) merged.set(a.ip, a);
        }
        for (const a of (Array.isArray(subnetResults) ? subnetResults : [])) {
            if (a.ip && !merged.has(a.ip)) merged.set(a.ip, a);
        }
        const allAgents = [...merged.values()];

        let html = '';

        // Show merged scan results
        if (allAgents.length > 0) {
            for (const agent of allAgents) {
                const label = agent.already_registered
                    ? `<span class="text-neon-green">online</span> ${escapeHtml(agent.name || agent.node_id)}`
                    : escapeHtml(agent.name || agent.node_id);
                const btnLabel = agent.already_registered ? t('discovery.refresh', 'Refresh') : t('discovery.add', '+ Add');
                const onclick = agent.already_registered
                    ? `loadNodes(); showToast(t('toast.node_refreshed', 'Node refreshed'), 'success')`
                    : `acceptDiscoveredAgent('${escapeHtml(agent.node_id)}', '${escapeHtml(agent.ip || '')}')`;
                html += `
                    <div class="flex items-center justify-between bg-gray-800/50 rounded p-1.5 text-xs">
                        <span class="text-white truncate">${label} <span class="text-gray-500">${escapeHtml(agent.ip || '')}</span></span>
                        <button onclick="${onclick}" class="text-neon-cyan hover:text-cyan-300 px-1">${btnLabel}</button>
                    </div>
                `;
            }
        }

        // Also show pending discovered agents
        if (pendingAgents && pendingAgents.length > 0) {
            for (const agent of pendingAgents) {
                if (!allAgents.find(a => a.node_id === agent.node_id)) {
                    html += `
                        <div class="flex items-center justify-between bg-gray-800/50 rounded p-1.5 text-xs">
                            <span class="text-white truncate">${escapeHtml(agent.name || agent.node_id)} <span class="text-gray-500">${escapeHtml(agent.ip || '')}</span></span>
                            <button onclick="acceptDiscoveredAgent('${escapeHtml(agent.node_id)}', '${escapeHtml(agent.ip || '')}')" class="text-neon-cyan hover:text-cyan-300 px-1">${t('discovery.add', '+ Add')}</button>
                        </div>
                    `;
                }
            }
        }

        if (!html) {
            html = '<div class="text-gray-500 text-xs py-1">';
            html += t('discovery.no_agents', 'No agents found. Use IP field below to add manually.');
            html += '</div>';
        }

        list.innerHTML = html;
    } catch (e) {
        list.innerHTML = `<div class="text-red-400 text-xs py-1">Scan failed: ${e.message}</div>`;
    }

    btn.disabled = false;
    btn.textContent = '\uD83D\uDD0D';
}

async function acceptDiscoveredAgent(nodeId, ip) {
    try {
        const url = ip ? `/api/discovered/${nodeId}/accept?ip=${encodeURIComponent(ip)}` : `/api/discovered/${nodeId}/accept`;
        const resp = await fetch(url, { method: 'POST' });
        const data = await resp.json();
        if (resp.ok) {
            if (data.message === 'Agent already registered') {
                showToast(t('toast.agent_already_registered', 'Agent already registered'), 'info');
            } else {
                showToast(t('toast.agent_added', 'Agent added! Reconnecting...'), 'success');
            }
            loadNodes();
        } else {
            showToast(t('toast.agent_add_error', 'Failed to add agent') + ': ' + (data.error || ''), 'error');
        }
    } catch (e) {
        showToast(t('toast.agent_add_error', 'Failed to add agent'), 'error');
    }
}

function dismissAgentForever(nodeId) {
    let dismissed;
    try { dismissed = JSON.parse(localStorage.getItem('fc_dismissed_agents') || '[]'); }
    catch(e) { dismissed = []; }
    if (!dismissed.includes(nodeId)) {
        dismissed.push(nodeId);
        localStorage.setItem('fc_dismissed_agents', JSON.stringify(dismissed));
    }
    showToast(t('toast.dismissed', 'Won\'t remind again'), 'success');
}

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
    conflict.data = null;
}

async function applyServerConfig() {
    if (!conflict.data) return;
    try {
        await fetch(`/api/nodes/${conflict.data.node_id}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: conflict.data.server_config })
        });
        hideConflictModal();
    } catch (e) {
        console.error('Failed to apply server config:', e);
    }
}

async function keepAgentConfig() {
    if (!conflict.data) return;
    try {
        await fetch(`/api/nodes/${conflict.data.node_id}/config`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: conflict.data.agent_config })
        });
        hideConflictModal();
    } catch (e) {
        console.error('Failed to keep agent config:', e);
    }
}

function showManualModeWarning(nodeId) {
    const node = store.nodesData.find(n => n.node_id === nodeId);
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

// ============================================================================
// DEBUG PANEL
// ============================================================================

function toggleDebugPanel() {
    debug.open = !debug.open;
    const panel = document.getElementById('debug-panel');
    const btn = document.querySelector('[title="Debug"]');
    if (debug.open) {
        panel.classList.remove('hidden');
        btn.classList.add('hidden');
        renderDebugPanel();
    } else {
        panel.classList.add('hidden');
        btn.classList.remove('hidden');
    }
}

function renderDebugPanel() {
    if (!debug.open) return;
    const el = document.getElementById('debug-content');
    if (!el) return;

    const saved = getPickerCards();
    const fans = store.state?.fans || {};
    const temps = store.state?.temp_sensors || {};
    const disks = store.state?.hdd_sensors || {};

    let html = '';

    // Connection status
    html += `<div class="mb-3"><span class="text-neon-cyan">Socket.IO:</span> ${socket?.connected ? '✅ connected' : '❌ disconnected'}</div>`;

    // Cards
    html += `<div class="mb-3"><span class="text-neon-cyan">Cards (${saved.length}):</span></div>`;
    for (const card of saved) {
        const el2 = document.querySelector(`[data-card-id="${card.id}"]`);
        const w = el2 ? el2.offsetWidth : 0;
        const h = el2 ? el2.offsetHeight : 0;
        html += `<div class="ml-2 mb-1">`;
        html += `<span class="text-gray-500">${card.type}</span> `;
        html += `<span class="text-white">${card.label || card.id.slice(-8)}</span> `;
        html += `<span class="text-yellow-400">${card.colSpan || 3}x${card.rowSpan || 1}</span> `;
        html += `<span class="text-gray-600">pos(${card.col},${card.row})</span> `;
        html += `<span class="text-gray-600">${w}x${h}px</span>`;
        if (card.lockSize) html += ` <span class="text-red-400">🔒</span>`;
        html += `</div>`;
    }

    // Fans
    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Fans (${Object.keys(fans).length}):</span></div>`;
    for (const [id, fan] of Object.entries(fans)) {
        const spark = getSparkline(`fan:local:${id}`);
        const last = spark.length ? spark[spark.length - 1] : '--';
        html += `<div class="ml-2 mb-1">`;
        html += `<span class="text-white">${fan.label || id.slice(-8)}</span> `;
        html += `<span class="text-cyan-400">${fan.rpm || 0} RPM</span> `;
        html += `<span class="text-gray-600">mode=${fan.mode}</span> `;
        html += `<span class="text-gray-600">spark=${last}</span>`;
        html += `</div>`;
    }

    // Temps
    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Temps (${Object.keys(temps).length}):</span></div>`;
    for (const [id, sensor] of Object.entries(temps)) {
        html += `<div class="ml-2 mb-1">`;
        html += `<span class="text-white">${sensor.label || id}</span> `;
        html += `<span class="text-green-400">${sensor.value || '--'}°C</span>`;
        html += `</div>`;
    }

    // Disks
    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Disks (${Object.keys(disks).length}):</span></div>`;
    for (const [id, disk] of Object.entries(disks)) {
        html += `<div class="ml-2 mb-1">`;
        html += `<span class="text-white">${disk.name || id}</span> `;
        html += `<span class="text-purple-400">${disk.temp || '--'}°C</span>`;
        html += `</div>`;
    }

    // Sparkline stats
    const sparkKeys = Object.keys(sparklineHistory);
    html += `<div class="mb-3 mt-3"><span class="text-neon-cyan">Sparklines (${sparkKeys.length}):</span></div>`;
    for (const key of sparkKeys.slice(0, 10)) {
        const data = sparklineHistory[key];
        html += `<div class="ml-2 mb-1"><span class="text-gray-500">${key}:</span> <span class="text-gray-400">${data.length} pts, last=${data[data.length-1]}</span></div>`;
    }

    el.innerHTML = html;
    setTimeout(() => { if (debug.open) renderDebugPanel(); }, 500);
}


// ============================================================================
// WINDOW EXPORTS (for onclick handlers in HTML)
// ============================================================================

window.selectFan = selectFan;
window.setFanMode = setFanMode;
window.sendControl = sendControl;
window.toggleSettings = toggleSettings;
window.showView = showView;
window.addNode = addNode;
window.scanForAgents = scanForAgents;
window.openUpdateModal = openUpdateModal;
window.openUpdateAgentsModal = openUpdateAgentsModal;
window.copyAgentToken = copyAgentToken;
window.showCardPicker = showCardPicker;
window.showGroupCreator = showGroupCreator;
window.hideCardPicker = hideCardPicker;
window.addSelectedCards = addSelectedCards;
window.hideCardEdit = hideCardEdit;
window.saveCardEdit = saveCardEdit;
window.hideCardConfig = hideCardConfig;
window.showCardConfig = showCardConfig;
window.refreshSmartData = refreshSmartData;
window.showSmartModal = showSmartModal;
window.hideSmartModal = hideSmartModal;
window.saveSmartSelection = saveSmartSelection;
window.hideGroupCreator = hideGroupCreator;
window.createGroup = createGroup;
window.toggleDebugPanel = toggleDebugPanel;
window.runDiscovery = runDiscovery;
window.selectControlMode = selectControlMode;
window.runCalibration = runCalibration;
window.applyDsmAndContinue = applyDsmAndContinue;
window.openServerNameEdit = openServerNameEdit;
window.switchLanguage = switchLanguage;
window.setTempUnit = setTempUnit;
window.setRefreshInterval = setRefreshInterval;
window.toggleCompactMode = toggleCompactMode;
window.checkForUpdates = checkForUpdates;
window.applyServerConfig = applyServerConfig;
window.keepAgentConfig = keepAgentConfig;
window.hideConflictModal = hideConflictModal;
window.saveNodeSettings = saveNodeSettings;
window.hideNodeSettings = hideNodeSettings;
window.saveServerName = saveServerName;
window.hideServerNameModal = hideServerNameModal;
window.hideManualModeWarning = hideManualModeWarning;
window.showServiceFanModal = showServiceFanModal;
window.recordFanService = recordFanService;
window.startCalibration = startCalibration;
window.clearSchedule = clearSchedule;
window.fillScheduleDefaults = fillScheduleDefaults;
window.closeSensorPopupForContext = closeSensorPopupForContext;
window.setScheduleMode = setScheduleMode;
window.setScheduleSensorMode = setScheduleSensorMode;
window.toggleScheduleSensorPopup = toggleScheduleSensorPopup;
window.saveScheduleEdit = saveScheduleEdit;
window.deleteScheduleEdit = deleteScheduleEdit;
window.closeScheduleEditor = closeScheduleEditor;
window.setLogLevel = setLogLevel;
window.setLogRetention = setLogRetention;
window.setAutoUpdateInterval = setAutoUpdateInterval;
window.startUpdate = startUpdate;
window.closeUpdateModal = closeUpdateModal;
window.selectFanFromTree = selectFanFromTree;
window.selectNodeFan = selectNodeFan;
window.selectNode = selectNode;
window.toggleNodeGroup = toggleNodeGroup;
window.showNodeSettings = showNodeSettings;
window.deleteNode = deleteNode;
window.restoreSensor = restoreSensor;
window.hideSensor = hideSensor;
window.startGroupRename = startGroupRename;
window.removePickerGroup = removePickerGroup;
window.updateSingleAgent = updateSingleAgent;
window.acceptDiscoveredAgent = acceptDiscoveredAgent;
window.dismissAgentForever = dismissAgentForever;
window.dismissToast = dismissToast;
window.retryAgentUpdate = retryAgentUpdate;
window.skipAgentUpdate = skipAgentUpdate;
window.requestAgentLogs = requestAgentLogs;
window.applyDsmScheme = applyDsmScheme;
window.editDsmEntry = editDsmEntry;
window.startFanCalibration = startFanCalibration;
window.editSinglePeriod = editSinglePeriod;
window.deleteSinglePeriod = deleteSinglePeriod;
window.removeScheduleSensor = removeScheduleSensor;
window.toggleRuleGroup = toggleRuleGroup;
window.editRuleGroup = editRuleGroup;
window.deleteRuleGroup = deleteRuleGroup;
window.updateCalibrationParam = updateCalibrationParam;
window.onSmartUnitChange = onSmartUnitChange;
window.toggleAgentAutoUpdate = toggleAgentAutoUpdate;
window.updatePickerElements = updatePickerElements;
window.onScheduleMouseDown = onScheduleMouseDown;
window.onScheduleMouseEnter = onScheduleMouseEnter;
window.removePickerCard = removePickerCard;

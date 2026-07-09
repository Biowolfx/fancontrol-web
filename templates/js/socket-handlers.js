/**
 * FanControl Web — Centralized Socket.IO event handlers
 * All socket.on() registrations in one place.
 */

import { store, dashboard, update, conflict } from './store.js';
import { t } from './i18n.js';
import { getSettings, showToast } from './utils.js';

// These functions are defined in main.js and will be passed in
// via the registerSocketHandlers() call at the bottom.
let _fns = {};

/**
 * Register all socket event handlers.
 * @param {SocketIOClient.Socket} socket - the socket.io instance
 * @param {Object} fns - object containing all needed functions from main.js
 */
export function registerSocketHandlers(socket, fns) {
    _fns = fns;

    socket.on('disconnect', () => {
        store.serverAvailable = false;
        _fns.showServerUnavailable();
    });

    socket.on('connect', () => {
        store.serverAvailable = true;
        _fns.hideServerUnavailable();
    });

    socket.on('update', (data) => {
        try {
        // Merge partial updates into store.state (don't replace)
        if (data != null && typeof data === 'object') Object.assign(store.state, data);
        // Sync node data from server state
        if (data.nodes) {
            const nodeEntries = Object.entries(data.nodes);
            for (const [nid, ndata] of nodeEntries) {
                const idx = store.nodesData.findIndex(n => n.node_id === nid);
                if (idx >= 0) {
                    Object.assign(store.nodesData[idx], ndata);
                } else {
                    store.nodesData.push(ndata);
                }
            }
            _fns.buildServerTree();
        }
        if (data.test_progress && data.testing) {
            _fns.updateCalibrationModal(data.test_progress);
        }
        const interval = getSettings().refreshInterval;
        if (interval === 0) {
            _fns.updateUI(data);
        } else {
            const now = Date.now();
            if (now - store.lastUIUpdate >= interval) {
                store.lastUIUpdate = now;
                _fns.updateUI(data);
            }
        }
        // Show update button in sidebar for agent mode
        const agentUpdateSection = document.getElementById('agent-update-section');
        if (agentUpdateSection) {
            agentUpdateSection.classList.toggle('hidden', !data.agent_mode);
        }
        // Show "Update Agents" button if any agent has outdated version (server mode only)
        const updateAgentsOutdated = document.getElementById('update-agents-outdated-section');
        if (updateAgentsOutdated && !data.agent_mode) {
            const serverVer = data.config_version || '';
            const outdatedCount = Object.values(data.nodes || {})
                .filter(n => n.status === 'online' && n.agent_version && n.agent_version !== serverVer).length;
            updateAgentsOutdated.classList.toggle('hidden', outdatedCount === 0);
            const countEl = document.getElementById('outdated-agents-count');
            if (countEl) {
                countEl.textContent = outdatedCount > 0 ? outdatedCount : '';
            }
        }
        // Hide "Add Node" section in agent mode (no server features)
        const addNodeSection = document.getElementById('add-node-section');
        if (addNodeSection) {
            addNodeSection.classList.toggle('hidden', !!data.agent_mode);
        }
        // Show agent token in sidebar only in agent mode
        const agentTokenSection = document.getElementById('agent-token-section');
        const agentTokenBanner = document.getElementById('agent-token-banner');
        const hasToken = data.api_token && data.api_token.length > 0;
        if (agentTokenSection) {
            agentTokenSection.classList.toggle('hidden', !data.agent_mode || !hasToken);
            if (hasToken) document.getElementById('agent-token-value').textContent = data.api_token;
        }
        // Hide the big banner — token is already in sidebar
        if (agentTokenBanner) {
            agentTokenBanner.classList.add('hidden');
        }
        // DSM scheme view is accessed by clicking DSM fans in tree — no nav button needed
        } catch (e) { console.error('[FanControl] Error in update handler:', e); }
    });

    socket.on('hardware_discovered', (data) => {
        console.log('[FanControl] Hardware discovered:', data);
        if (store.wizardStep === 'intro' || store.wizardStep === 'scanning') {
            _fns.renderDiscoveredHardware(data);
            store.wizardStep = 'results';
        }
    });

    socket.on('test_progress', (progress) => {
        console.log('[FanControl] Calibration progress:', progress);
        _fns.updateCalibrationModal(progress);
    });

    socket.on('hidden_sensors', (data) => {
        dashboard.hiddenSensors = data.hiddenSensors || [];
        _fns.buildServerTree();
    });

    socket.on('test_complete', (result) => {
        console.log('[FanControl] Calibration complete:', result);
        _fns.hideCalibrationModal();
        if (result.success) {
            store.wizardStep = 'done';
            store.state = { ...store.state, initialized: true, tested: true };
            _fns.showMainScreen();
        }
    });

    // Agent update listeners (registered once)
    socket.on('agent:update_progress', (data) => {
        const { node_id, status, message, version } = data;
        update.agentStates[node_id] = { status, message, version };
        _fns.renderUpdateAgentProgress();
        _fns.checkAgentsDone();
    });
    socket.on('agent:logs', (data) => {
        _fns.renderAgentLogsModal(data.node_id, data.lines);
    });

    // Node events
    socket.on('node:update', (data) => {
        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);
        if (idx >= 0) {
            store.nodesData[idx].status = data.status;
            store.nodesData[idx].name = data.name || store.nodesData[idx].name;
            if (data.ip) store.nodesData[idx].ip = data.ip;
            if (data.control_mode) store.nodesData[idx].control_mode = data.control_mode;
        }
        _fns.buildServerTree();
        _fns.renderNodesOverview();
    });

    socket.on('node:telemetry', (data) => {
        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);
        if (idx >= 0) {
            store.nodesData[idx].telemetry = data.telemetry;
        } else {
            // Node not yet in store.nodesData — fetch fresh list
            _fns.loadNodes();
            return;
        }
        _fns.buildServerTree();
        _fns.renderNodesOverview();
        if (store.selectedNodeId === data.node_id && store.currentView === 'node-detail') {
            _fns.loadNodeDetail(data.node_id);
        }
    });

    socket.on('node:conflict', (data) => {
        console.warn('[FanControl] Node conflict:', data);
        conflict.data = data;
        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);
        if (idx >= 0) {
            store.nodesData[idx].control_mode = 'manual';
        }
        _fns.buildServerTree();
        _fns.showConflictModal(data);
    });

    socket.on('node:mode_changed', (data) => {
        const idx = store.nodesData.findIndex(n => n.node_id === data.node_id);
        if (idx >= 0) {
            store.nodesData[idx].control_mode = data.mode;
        }
        _fns.buildServerTree();
        _fns.renderNodesOverview();
        if (data.mode === 'manual') {
            _fns.showManualModeWarning(data.node_id);
        }
    });

    socket.on('node:discovered', (data) => {
        if (data.already_connected) {
            // Agent auto-registered via WebSocket — already connected, just notify
            showToast(t('toast.agent_connected', 'Agent connected') + ': ' + data.name + ' (' + data.ip + ')', 'success');
            _fns.loadNodes();
        } else {
            // SSDP-discovered agent — check if dismissed
            const dismissed = JSON.parse(localStorage.getItem('fc_dismissed_agents') || '[]');
            if (dismissed.includes(data.node_id)) return;
            const msg = t('toast.new_agent', 'New agent: ') + data.name + ' (' + data.ip + ')';
            showToast(msg, 'warning', [
                { label: t('toast.add', 'Add'), onclick: `acceptDiscoveredAgent('${data.node_id}')` },
                { label: t('toast.dismiss', 'Don\'t remind'), onclick: `dismissAgentForever('${data.node_id}')`, secondary: true },
            ]);
        }
    });

    socket.on('server:name_changed', (data) => {
        if (data.name) {
            store.state.server_name = data.name;
            _fns.buildServerTree();
        }
    });
}

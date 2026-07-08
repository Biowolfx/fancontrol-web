/**
 * FanControl Web — Shared rendering helpers
 * Deduplicates HTML template patterns used across main.js.
 */

import { t } from './i18n.js';
import { escapeHtml, formatTemp } from './utils.js';
import { BTN_MANUAL_ACTIVE, BTN_MANUAL_INACTIVE, BTN_AUTO_ACTIVE, BTN_AUTO_INACTIVE } from './store.js';

/**
 * Render a health status icon for a fan.
 * Used in renderLocalServerTree() and renderRemoteNodeTree().
 * @param {Object} fan - fan object with health.status
 * @returns {string} HTML string for the health icon
 */
export function healthIcon(fan) {
    const fanHealth = fan.health?.status || 'healthy';
    if (fanHealth === 'stopped') return '<span class="text-red-400 text-[10px] ml-1 alert-pulse" title="' + t('fan.health.stopped', 'Fan stopped') + '">⛔</span>';
    if (fanHealth === 'slowing') return '<span class="text-yellow-400 text-[10px] ml-1 alert-pulse" title="' + t('fan.health.slowing', 'Fan slowing — bearing wear') + '">⚠</span>';
    if (fanHealth === 'needs_calibration') return '<span class="text-yellow-400 text-[10px] ml-1 alert-pulse" title="' + t('fan.health.needs_calibration', 'Calibration required') + '">⚠</span>';
    return '';
}

/**
 * Build the HTML for a sensor checkbox list (used in sensor popups).
 * @param {Array} sensors - all sensors (from store.allSensors)
 * @param {Array} checkedIds - IDs of currently checked sensors
 * @returns {string} HTML string
 */
export function buildSensorCheckboxList(sensors, checkedIds) {
    const groups = {};
    sensors.forEach(s => {
        if (!groups[s.group]) groups[s.group] = [];
        groups[s.group].push(s);
    });
    
    let html = '';
    for (const [group, slist] of Object.entries(groups)) {
        html += `<div class="text-xs font-semibold text-gray-500 uppercase mb-2">${t(group, group)}</div>`;
        slist.forEach(s => {
            const checked = checkedIds.includes(s.id);
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
    return html;
}

/**
 * Set manual/auto button styles based on current mode.
 * Used in updateInspector() and setFanMode().
 * @param {string} mode - 'manual' or 'auto'
 */
export function setModeButtonStyles(mode) {
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
}

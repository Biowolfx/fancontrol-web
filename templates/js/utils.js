/**
 * FanControl Web — Pure utility functions
 * No state dependencies, no side effects (except DOM helpers).
 */

import { settings, settingsDefaults } from './store.js';
import { t } from './i18n.js';

export function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

export function fanIcon(fan, size = 'xs') {
    const sizeClass = size === 'xs' ? 'w-3 h-3' : 'w-4 h-4';
    const rpm = fan.rpm || 0;
    const isDsm = fan.control_method === 'dsm_scemd';
    if (isDsm) {
        return `<svg class="${sizeClass} inline-block flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"/></svg>`;
    }
    const healthStatus = fan.health?.status || 'healthy';
    let color;
    if (healthStatus === 'stopped') color = '#ef4444';
    else if (healthStatus === 'slowing' || healthStatus === 'needs_calibration') color = '#facc15';
    else color = rpm > 0 ? '#22d3ee' : '#4b5563';
    const dur = rpm > 0 ? Math.max(0.3, 3 - rpm / 500) : 0;
    const anim = rpm > 0 ? `style="animation: fan-spin ${dur}s linear infinite"` : '';
    return `<svg class="${sizeClass} inline-block flex-shrink-0" viewBox="0 0 100 100" ${anim}><g fill="${color}" opacity="0.9"><path d="M50 50 Q30 20 50 5 Q70 20 50 50"/><path d="M50 50 Q80 30 95 50 Q80 70 50 50"/><path d="M50 50 Q70 80 50 95 Q30 80 50 50"/><path d="M50 50 Q20 70 5 50 Q20 30 50 50"/></g><circle cx="50" cy="50" r="6" fill="${color}" opacity="0.6"/></svg>`;
}

export function show(el) { if (el) el.classList.remove('hidden'); }
export function hide(el) { if (el) el.classList.add('hidden'); }
export function toggle(el, visible) { if (el) el.classList.toggle('hidden', !visible); }

export function formatTemp(celsius) {
    if (celsius == null) return '--';
    const s = getSettings();
    if (s.tempUnit === 'fahrenheit') {
        return Math.round(celsius * 9 / 5 + 32) + '°F';
    }
    return celsius + '°C';
}

export function getTempUnitSymbol() {
    return getSettings().tempUnit === 'fahrenheit' ? '°F' : '°C';
}

export function formatBytes(bytes, unit) {
    if (isNaN(bytes) || bytes === 0) return '0';
    const units = { 'kb': 1024, 'mb': 1024*1024, 'gb': 1024*1024*1024, 'tb': 1024*1024*1024*1024 };
    const divisor = units[unit] || 1;
    const result = bytes / divisor;
    if (result >= 1000) return result.toFixed(0);
    if (result >= 100) return result.toFixed(1);
    return result.toFixed(2);
}

export function getUnitLabel(unit) {
    const labels = { 'bytes': t('smart.unit.bytes', 'B'), 'kb': t('smart.unit.kb', 'KB'), 'mb': t('smart.unit.mb', 'MB'), 'gb': t('smart.unit.gb', 'GB'), 'tb': t('smart.unit.tb', 'TB') };
    return labels[unit] || '';
}

export function getTempColorClass(temp) {
    if (temp <= 0) return 'text-gray-500';
    if (temp <= 35) return 'text-neon-cyan';
    if (temp <= 45) return 'text-neon-orange';
    return 'text-neon-red';
}

export function getSettings() {
    const now = Date.now();
    if (settings._cache && (now - settings._cacheTime) < settings.CACHE_TTL) {
        return settings._cache;
    }
    try {
        const raw = localStorage.getItem('fancontrol_settings');
        settings._cache = raw ? { ...settingsDefaults, ...JSON.parse(raw) } : { ...settingsDefaults };
    } catch { settings._cache = { ...settingsDefaults }; }
    settings._cacheTime = now;
    return settings._cache;
}

export function saveSettings(partial) {
    const s = getSettings();
    Object.assign(s, partial);
    localStorage.setItem('fancontrol_settings', JSON.stringify(s));
    settings._cache = s;
    settings._cacheTime = Date.now();
    return s;
}

export function showToast(message, type = 'info', actions = []) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    let html = `<span>${escapeHtml(message)}</span>`;
    actions.forEach(action => {
        html += `<button class="toast-btn ${action.secondary ? 'toast-btn-secondary' : ''}" onclick="${action.onclick}">${escapeHtml(action.label)}</button>`;
    });

    toast.innerHTML = html;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100px)';
        setTimeout(() => toast.remove(), 300);
    }, 8000);
}

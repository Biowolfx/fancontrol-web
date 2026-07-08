/**
 * FanControl Web — i18n system
 */

import { i18n, store } from './store.js';

export async function loadLang(code) {
    try {
        const resp = await fetch(`/api/lang/${code}`);
        if (resp.ok) {
            i18n.translations = await resp.json();
            i18n.currentLang = code;
            localStorage.setItem('fancontrol_lang', code);
            applyTranslations();
            return true;
        }
    } catch (e) {
        console.error('[i18n] Failed to load lang:', code, e);
    }
    return false;
}

export function t(key, fallback) {
    return i18n.translations[key] || fallback || key;
}

export function applyTranslations() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (key && i18n.translations[key]) {
            el.textContent = i18n.translations[key];
        }
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        const key = el.getAttribute('data-i18n-title');
        if (key && i18n.translations[key]) {
            el.title = i18n.translations[key];
        }
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        const key = el.getAttribute('data-i18n-placeholder');
        if (key && i18n.translations[key]) {
            el.placeholder = i18n.translations[key];
        }
    });
    const ver = store.state?.config_version;
    if (i18n.translations['app.title'] && ver) {
        document.title = `${i18n.translations['app.title']} v${ver}`;
    }
}

/* Dashboard prototype: overview card + small charts (ApexCharts)
 * Relies on global `socket` from main.js and Tailwind CSS styles.
 */

(() => {
  const MAX_POINTS = 120;
  let tempSeries = [];
  let chartTemp = null;
  let grid = null;

  function createOverviewCard() {
    return `
      <div class="grid-stack-item" gs-w="6" gs-h="3" data-gs-id="overview">
        <div class="grid-stack-item-content dashboard-card bg-cyber-card border border-cyber-accent rounded-xl p-4" tabindex="0" role="region" aria-label="Overview card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-lg font-semibold text-white">Overview</h3>
            <div class="flex items-center gap-2">
              <div class="text-xs text-gray-400">Realtime</div>
              <button onclick="window.openCardSettings('overview')" aria-label="Open card settings" class="text-xs text-gray-400 hover:text-neon-cyan">⚙</button>
            </div>
          </div>
          <div class="grid grid-cols-3 gap-4">
            <div>
              <div class="text-sm text-gray-400">Max Temp</div>
              <div id="overview-max-temp" class="text-2xl font-bold text-neon-cyan">-- °C</div>
            </div>
            <div>
              <div class="text-sm text-gray-400">Total RPM</div>
              <div id="overview-total-rpm" class="text-2xl font-bold text-neon-purple">--</div>
            </div>
            <div>
              <div class="text-sm text-gray-400">Avg PWM</div>
              <div id="overview-avg-pwm" class="text-2xl font-bold text-neon-green">-- %</div>
            </div>
          </div>
          <div class="mt-4">
            <div id="overview-temp-chart" class="h-36"></div>
          </div>
        </div>
      </div>
    `;
  }

  function createFansCard() {
    return `
      <div class="grid-stack-item" gs-w="3" gs-h="3" data-gs-id="fans">
        <div class="grid-stack-item-content dashboard-card bg-cyber-card border border-cyber-accent rounded-xl p-4" tabindex="0" role="region" aria-label="Fans card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-lg font-semibold text-white">Fans</h3>
            <div class="flex items-center gap-2">
              <div class="text-xs text-gray-400">Status</div>
              <button onclick="window.openCardSettings('fans')" aria-label="Open card settings" class="text-xs text-gray-400 hover:text-neon-cyan">⚙</button>
            </div>
          </div>
          <div id="fans-list" class="space-y-2 max-h-48 overflow-y-auto text-sm text-gray-300"></div>
        </div>
      </div>
    `;
  }

  function createDisksCard() {
    return `
      <div class="grid-stack-item" gs-w="3" gs-h="2" data-gs-id="disks">
        <div class="grid-stack-item-content dashboard-card bg-cyber-card border border-cyber-accent rounded-xl p-4" tabindex="0" role="region" aria-label="Disks card">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-lg font-semibold text-white">Disks</h3>
            <div class="flex items-center gap-2">
              <div class="text-xs text-gray-400">Health</div>
              <button onclick="window.openCardSettings('disks')" aria-label="Open card settings" class="text-xs text-gray-400 hover:text-neon-cyan">⚙</button>
            </div>
          </div>
          <div id="disks-list" class="space-y-2 text-sm text-gray-300"></div>
        </div>
      </div>
    `;
  }

  function createSensorsCard() {
    return `
      <div class="grid-stack-item" gs-w="3" gs-h="2" data-gs-id="sensors">
        <div class="grid-stack-item-content dashboard-card bg-cyber-card border border-cyber-accent rounded-xl p-4" role="region" aria-label="Sensors">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-lg font-semibold text-white">Sensors</h3>
            <div class="text-xs text-gray-400">Temps</div>
          </div>
          <div id="sensors-list" class="space-y-2 text-sm text-gray-300"></div>
        </div>
      </div>
    `;
  }

  function createLogsCard() {
    return `
      <div class="grid-stack-item" gs-w="6" gs-h="2" data-gs-id="logs">
        <div class="grid-stack-item-content dashboard-card bg-cyber-card border border-cyber-accent rounded-xl p-4">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-lg font-semibold text-white">Events</h3>
            <div class="text-xs text-gray-400">Recent</div>
          </div>
          <div id="events-list" class="max-h-32 overflow-y-auto text-xs text-gray-300"></div>
        </div>
      </div>
    `;
  }

  function updateEventsFromHistory(hours) {
    fetch(`/api/history?hours=${hours}`).then(r => r.ok ? r.json() : null).then(data => {
      if (!data) return;
      const ts = data.timestamps || [];
      const temps = data.temps || [];
      const pwm = data.pwm || [];
      const events = document.getElementById('events-list');
      if (!events) return;
      const lines = [];
      for (let i = Math.max(0, ts.length - 10); i < ts.length; i++) {
        const time = new Date(ts[i]).toLocaleString();
        const t = temps[i] || '--';
        const p = pwm[i] || '--';
        lines.push(`<div class="flex justify-between"><div class="text-xs text-gray-400">${time}</div><div class="text-xs font-mono">T:${t}°C P:${p}</div></div>`);
      }
      events.innerHTML = lines.reverse().join('');
    }).catch(e => console.debug('events fetch failed', e));
  }

  function initCharts() {
    const opts = {
      chart: { type: 'area', height: 160, toolbar: { show: false }, animations: { enabled: false } },
      series: [{ name: 'Max Temp', data: [] }],
      stroke: { curve: 'smooth' },
      grid: { show: false },
      markers: { size: 0 },
      xaxis: { type: 'datetime', labels: { show: false } },
      yaxis: { show: true, labels: { style: { colors: ['#9ca3af'] } } },
      tooltip: { enabled: true }
    };

    const el = document.querySelector('#overview-temp-chart');
    if (el) {
      chartTemp = new ApexCharts(el, opts);
      chartTemp.render();
    }
  }

  function initGrid() {
    const el = document.querySelector('#dashboard-canvas');
    if (!el) return;

    if (window.GridStack) {
      grid = GridStack.init({
        cellHeight: 80,
        minRow: 1,
        float: true,
        resizable: { handles: 'se' }
      }, '#dashboard-canvas');

      // Try load from localStorage first, then server, otherwise seed defaults
      const saved = localStorage.getItem('fc_dashboard_layout');
      if (saved) {
        try {
          const layout = JSON.parse(saved);
          grid.load(layout);
        } catch (e) { console.debug('Failed to load layout from localStorage', e); }
      } else {
        // attempt load from server
        fetch('/api/dashboard').then(r => r.ok ? r.json() : null).then(json => {
          if (json && Array.isArray(json.cards) && json.cards.length) {
            try { grid.load(json.cards); } catch(e) { console.debug('grid.load(server) failed', e); seedDefaults(); }
          } else {
            seedDefaults();
          }
        }).catch(e => { console.debug('dashboard GET failed', e); seedDefaults(); });
      }

      grid.on('change', () => saveLayout());
    } else {
      // fallback: inject static cards
      const container = document.getElementById('dashboard-cards');
      container.innerHTML = createOverviewCard() + createFansCard() + createDisksCard() + createLogsCard();
      initCharts();
    }
  }

  function seedDefaults() {
    if (!grid) return;
    try {
      grid.removeAll();
    } catch (e) {}
    grid.addWidget(createOverviewCard());
    grid.addWidget(createFansCard());
    grid.addWidget(createDisksCard());
    grid.addWidget(createLogsCard());
  }

  async function saveLayout() {
    if (!grid) return;
    try {
      const layout = grid.save(false, true);
      localStorage.setItem('fc_dashboard_layout', JSON.stringify(layout));
      // Try to persist on server
      try {
        await fetch('/api/dashboard', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cards: layout })
        });
      } catch (e) { console.debug('server save failed', e); }
    } catch (e) { console.debug('saveLayout failed', e); }
  }

  function resetLayout() {
    localStorage.removeItem('fc_dashboard_layout');
    // Remove server-side layout
    fetch('/api/dashboard', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cards: [] }) }).catch(()=>{});
    if (grid) {
      grid.removeAll();
      grid.addWidget(createOverviewCard());
      grid.addWidget(createFansCard());
      grid.addWidget(createDisksCard());
      grid.addWidget(createLogsCard());
    }
    initCharts();
  }

  // Helpers for fan cards
  const fanCharts = {};
  const fanSeries = {};
  const cardTimers = {};

  function slugify(s) {
    return String(s).replace(/[^a-z0-9_-]+/gi, '-');
  }

  function createFanCardHtml(fanId, fan) {
    const id = slugify(fanId);
    const label = escapeHtml(fan.label || fanId);
    return `
      <div class="grid-stack-item" gs-w="2" gs-h="2" data-gs-id="fan-${id}">
        <div class="grid-stack-item-content dashboard-card bg-cyber-card border border-cyber-accent rounded-xl p-3" tabindex="0" role="region" aria-label="Fan ${label}">
          <div class="flex items-center justify-between mb-2">
            <div class="text-sm font-semibold text-white truncate">${label}</div>
            <div class="flex items-center gap-2">
              <div id="fan-rpm-${id}" class="text-xs font-mono text-neon-cyan">${fan.rpm||0} RPM</div>
              <button onclick="window.openCardSettings('fan-${id}')" aria-label="Open fan settings" class="text-xs text-gray-400 hover:text-neon-cyan">⚙</button>
            </div>
          </div>
          <div id="fan-chart-${id}" class="h-20"></div>
        </div>
      </div>
    `;
  }

  function initFanChart(fanId) {
    const id = slugify(fanId);
    const el = document.getElementById(`fan-chart-${id}`);
    if (!el) return;
    if (fanCharts[fanId]) return;
    fanSeries[fanId] = [];
    const opts = {
      chart: { type: 'area', height: 80, toolbar: { show: false }, animations: { enabled: false } },
      series: [{ name: 'RPM', data: [] }],
      stroke: { curve: 'smooth', width: 2 },
      grid: { show: false },
      markers: { size: 0 },
      xaxis: { type: 'datetime', labels: { show: false } },
      yaxis: { show: false },
      tooltip: { enabled: false }
    };
    try {
      const c = new ApexCharts(el, opts);
      c.render();
      fanCharts[fanId] = c;
    } catch (e) { console.debug('initFanChart failed', e); }
  }

  function startCardTimer(cardKey, ms) {
    stopCardTimer(cardKey);
    if (!ms || ms <= 0) return;
    try {
      // poll /api/state and update
      const id = setInterval(async () => {
        try {
          const res = await fetch('/api/state');
          if (!res.ok) return;
          const data = await res.json();
          updateOverview(data);
        } catch (e) { console.debug('cardTimer fetch failed', e); }
      }, ms);
      cardTimers[cardKey] = id;
    } catch (e) { console.debug('startCardTimer failed', e); }
  }

  function stopCardTimer(cardKey) {
    try {
      const t = cardTimers[cardKey];
      if (t) { clearInterval(t); delete cardTimers[cardKey]; }
    } catch (e) { console.debug('stopCardTimer failed', e); }
  }

  function updateOverview(data) {
    if (!data) return;
    const maxTemp = data.max_hdd_temp || 0;
    const fans = data.fans || {};
    const totalRpm = Object.values(fans).reduce((acc, f) => acc + (f.rpm || 0), 0);
    const avgPwm = Object.values(fans).length ? Math.round(Object.values(fans).reduce((acc, f) => acc + (f.current_pct || 0), 0) / Object.values(fans).length) : 0;

    const elTemp = document.getElementById('overview-max-temp');
    const elRpm = document.getElementById('overview-total-rpm');
    const elPwm = document.getElementById('overview-avg-pwm');
    if (elTemp) elTemp.textContent = (maxTemp > 0 ? `${maxTemp}°C` : '--');
    if (elRpm) elRpm.textContent = totalRpm || '--';
    if (elPwm) elPwm.textContent = avgPwm ? `${avgPwm}%` : '--';

    const now = Date.now();
    tempSeries.push([now, maxTemp || 0]);
    if (tempSeries.length > MAX_POINTS) tempSeries.shift();
    try { if (chartTemp) chartTemp.updateSeries([{ data: tempSeries }]); } catch (e) {}

    // update fans list and per-fan cards
    const fansList = document.getElementById('fans-list');
    if (fansList) {
      fansList.innerHTML = Object.values(fans).map(f => `<div class="flex justify-between"><div>${escapeHtml(f.label || f.id)}</div><div class="font-mono">${f.rpm||0} RPM</div></div>`).join('');
    }

    // per-fan cards with mini-charts
    for (const [fanId, f] of Object.entries(fans)) {
      const id = slugify(fanId);
      if (!document.getElementById(`fan-chart-${id}`)) {
        // create card
        try {
          if (grid) {
            grid.addWidget(createFanCardHtml(fanId, f));
          } else {
            const container = document.getElementById('dashboard-cards');
            container.insertAdjacentHTML('beforeend', createFanCardHtml(fanId, f));
          }
          initFanChart(fanId);
        } catch (e) { console.debug('add fan card failed', e); }
      }

      // update RPM display and chart
      const rpmEl = document.getElementById(`fan-rpm-${id}`);
      if (rpmEl) rpmEl.textContent = `${f.rpm||0} RPM`;

      // update chart series
      if (fanCharts[fanId]) {
        const now = Date.now();
        fanSeries[fanId].push([now, f.rpm || 0]);
        if (fanSeries[fanId].length > 60) fanSeries[fanId].shift();
        try { fanCharts[fanId].updateSeries([{ data: fanSeries[fanId] }]); } catch (e) {}
      }
    }

    // update disks
    const disksList = document.getElementById('disks-list');
    if (disksList) {
      const disks = Object.values(data.hdd_sensors || {});
      disksList.innerHTML = disks.map(d => `<div class="flex justify-between"><div>${escapeHtml(d.label||d.dev_name||d.device)}</div><div>${d.temp||'--'}°C</div></div>`).join('');
    }

    applyThresholds(data);
  }

  function getCardSettings(key) {
    try {
      const all = JSON.parse(localStorage.getItem('fc_card_settings') || '{}');
      return all[key] || { threshold: null, refresh: 0 };
    } catch (e) { return { threshold: null, refresh: 0 }; }
  }

  function setCardSettings(key, settings) {
    try {
      const all = JSON.parse(localStorage.getItem('fc_card_settings') || '{}');
      all[key] = settings;
      localStorage.setItem('fc_card_settings', JSON.stringify(all));
      // update timer
      if (settings && settings.refresh && settings.refresh > 0) startCardTimer(key, settings.refresh);
      else stopCardTimer(key);
    } catch (e) { console.debug('setCardSettings failed', e); }
  }

  function applyThresholds(stateData) {
    try {
      // overview threshold
      const overviewSettings = getCardSettings('overview');
      if (overviewSettings && overviewSettings.threshold) {
        const maxTemp = stateData.max_hdd_temp || 0;
        const el = document.getElementById('overview-card');
        if (el) {
          if (maxTemp >= overviewSettings.threshold) {
            el.querySelector('.grid-stack-item-content')?.classList.add('border-neon-red');
          } else {
            el.querySelector('.grid-stack-item-content')?.classList.remove('border-neon-red');
          }
        }
      }

      // per-fan thresholds
      const fans = stateData.fans || {};
      for (const [fanId, f] of Object.entries(fans)) {
        const key = `fan-${fanId}`;
        const s = getCardSettings(key);
        const id = slugify(fanId);
        const el = document.querySelector(`[data-gs-id="fan-${id}"]`);
        if (!el) continue;
        const content = el.querySelector('.grid-stack-item-content');
        if (!content) continue;
        if (s && s.threshold && (f.rpm || 0) >= s.threshold) {
          content.classList.add('border-neon-red');
          maybeAlertThreshold(`Fan ${f.label || fanId} RPM ${f.rpm} >= ${s.threshold}`);
        } else {
          content.classList.remove('border-neon-red');
          clearAlertForKey(`fan-${fanId}`);
        }
      }
    } catch (e) { console.debug('applyThresholds failed', e); }
  }

  const alerted = new Set();

  function maybeAlertThreshold(message) {
    if (alerted.has(message)) return;
    alerted.add(message);
    const banner = document.getElementById('threshold-banner');
    const text = document.getElementById('threshold-banner-text');
    if (text) text.textContent = message;
    if (banner) banner.classList.remove('hidden');
    // auto-hide after 10s
    setTimeout(() => { if (banner) banner.classList.add('hidden'); }, 10000);
  }

  function clearAlertForKey(key) {
    // remove any alerts that mention this key
    for (const m of Array.from(alerted)) {
      if (m.includes(key) || m.toLowerCase().includes(key.toLowerCase())) {
        alerted.delete(m);
      }
    }
  }

  function fetchHistory(hours) {
    fetch(`/api/history?hours=${hours}`).then(r => {
      if (!r.ok) throw new Error('history fetch failed');
      return r.json();
    }).then(data => {
      // ApexCharts expects series of [x,y] pairs; server returns timestamps and temps
      const timestamps = data.timestamps || [];
      const temps = data.temps || [];
      const series = timestamps.map((ts,i) => [new Date(ts).getTime(), temps[i]||0]);
      tempSeries = series.slice(-MAX_POINTS);
      try { if (chartTemp) chartTemp.updateSeries([{ data: tempSeries }]); } catch(e){}
    }).catch(e => console.debug('fetchHistory error', e));
  }

  // Initialize on DOM ready
  function init() {
    initGrid();
    initCharts();

    document.getElementById('save-layout-btn')?.addEventListener('click', saveLayout);
    document.getElementById('reset-layout-btn')?.addEventListener('click', resetLayout);
    document.getElementById('history-range')?.addEventListener('change', (e) => { fetchHistory(e.target.value); updateEventsFromHistory(e.target.value); });

    document.getElementById('save-preset-btn')?.addEventListener('click', async () => {
      const name = prompt('Preset name:');
      if (!name) return;
      const layout = grid ? grid.save(false, true) : JSON.parse(localStorage.getItem('fc_dashboard_layout')||'[]');
      await fetch('/api/dashboard/presets', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ name, cards: layout }) });
      await loadPresets();
      alert('Preset saved');
    });

    document.getElementById('del-preset-btn')?.addEventListener('click', async () => {
      const sel = document.getElementById('preset-select');
      const name = sel?.value;
      if (!name) return alert('Select a preset');
      if (!confirm(`Delete preset '${name}'?`)) return;
      await fetch(`/api/dashboard/presets/${encodeURIComponent(name)}`, { method: 'DELETE' });
      await loadPresets();
    });

    document.getElementById('preset-select')?.addEventListener('change', async (e) => {
      const name = e.target.value;
      if (!name) return;
      // load preset from server
      const res = await fetch('/api/dashboard');
      if (!res.ok) return;
      const json = await res.json();
      const presets = json.presets || [];
      const preset = presets.find(p => p.name === name);
      if (preset && Array.isArray(preset.cards)) {
        try { grid.load(preset.cards); saveLayout(); } catch (e) { console.debug('load preset failed', e); }
      }
    });

    loadPresets();

    // Export / Import handlers
    document.getElementById('export-presets-btn')?.addEventListener('click', exportPresets);
    const importInput = document.getElementById('import-presets-input');
    importInput?.addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) importPresetsFile(f);
    });

    if (window.socket) {
      window.socket.on('update', (data) => { try { updateOverview(data); } catch(e){} });
    }

    // load initial history
    const sel = document.getElementById('history-range');
    const hours = sel ? sel.value : 24;
    fetchHistory(hours);
    updateEventsFromHistory(hours);

    // Card settings modal wiring
    const modal = document.getElementById('card-settings-modal');
    const inputTh = document.getElementById('card-threshold');
    const inputRf = document.getElementById('card-refresh');
    let currentCardKey = null;
    document.getElementById('card-settings-cancel')?.addEventListener('click', () => { modal.classList.add('hidden'); });
    document.getElementById('card-settings-save')?.addEventListener('click', () => {
      if (!currentCardKey) return;
      const th = parseFloat(inputTh.value) || null;
      const rf = parseInt(inputRf.value) || 0;
      setCardSettings(currentCardKey, { threshold: th, refresh: rf });
      modal.classList.add('hidden');
      alert('Card settings saved');
    });

    // Expose openCardSettings for external use
    window.openCardSettings = function(cardKey) {
      currentCardKey = cardKey;
      const s = getCardSettings(cardKey) || {};
      inputTh.value = s.threshold || '';
      inputRf.value = s.refresh || 0;
      modal.classList.remove('hidden');
    };

    // Observe charts for lazy-render
    observeCharts();
  }

  async function loadPresets() {
    try {
      const res = await fetch('/api/dashboard/presets');
      if (!res.ok) return;
      const json = await res.json();
      const presets = json.presets || [];
      const sel = document.getElementById('preset-select');
      if (!sel) return;
      sel.innerHTML = '<option value="">Default</option>' + presets.map(p => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`).join('');
    } catch (e) { console.debug('loadPresets failed', e); }
  }

  // Export presets as JSON file
  async function exportPresets() {
    try {
      const res = await fetch('/api/dashboard/presets');
      if (!res.ok) return alert('Failed to fetch presets');
      const json = await res.json();
      const data = JSON.stringify(json.presets || [], null, 2);
      const blob = new Blob([data], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'fancontrol-presets.json';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { console.debug('exportPresets failed', e); alert('Export failed'); }
  }

  // Import presets from JSON file (client-side) and POST to server
  async function importPresetsFile(file) {
    try {
      const txt = await file.text();
      const arr = JSON.parse(txt);
      if (!Array.isArray(arr)) return alert('Invalid preset file');
      for (const p of arr) {
        if (p.name && p.cards) {
          await fetch('/api/dashboard/presets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) });
        }
      }
      await loadPresets();
      alert('Presets imported');
    } catch (e) { console.debug('importPresetsFile failed', e); alert('Import failed'); }
  }

  // IntersectionObserver to lazy-render charts
  const chartObserver = ('IntersectionObserver' in window) ? new IntersectionObserver((entries) => {
    for (const ent of entries) {
      if (ent.isIntersecting) {
        const el = ent.target;
        // render ApexChart instances if any pending
        try {
          if (el.id === 'overview-temp-chart' && chartTemp) {
            chartTemp.render().catch(()=>{});
          }
          // fan charts
          const fanChartEl = el.querySelector && el.querySelector('.apexcharts-canvas');
          // we rely on initFanChart to render when element exists
        } catch (e) {}
        chartObserver.unobserve(el);
      }
    }
  }, { root: null, threshold: 0.1 }) : null;

  function observeCharts() {
    try {
      const overview = document.getElementById('overview-temp-chart');
      if (overview && chartObserver) chartObserver.observe(overview);
      // fan chart containers
      document.querySelectorAll('[id^="fan-chart-"]').forEach(el => { if (chartObserver) chartObserver.observe(el); });
    } catch (e) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  function addWidget(html) {
    if (grid) return grid.addWidget(html);
    return null;
  }

  window.__fancontrol_dashboard = { updateOverview, saveLayout, resetLayout, addWidget };
})();

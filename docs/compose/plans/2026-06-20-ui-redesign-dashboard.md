# UI Redesign: Node Tree + Custom Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the multi-node UI with a node tree (Variant D) and a customizable dashboard canvas with drag-and-drop cards.

**Architecture:** Left panel gets tabs (Dashboard/Nodes/Settings). Nodes tab shows expandable tree of servers with fans/sensors. Dashboard tab is an empty canvas where users add cards from any server, resize freely, and group by name. All config persists in config.json.

**Tech Stack:** Vanilla JS, Tailwind CSS, HTML5 Drag API, CSS transforms for resize.

---

## Phase 1: Node Tree (Variant D)

### Task 1: Restructure Left Panel with Tabs

**Covers:** [S3]

**Files:**
- Modify: `templates/index.html` (left panel header area, lines ~177-233)

- [ ] **Step 1: Replace navigation tabs**

Find the current navigation tabs section (around line 196) and replace the two-button nav with three tabs:

```html
<!-- Navigation Tabs -->
<div class="flex border-b border-cyber-accent">
    <button class="nav-item flex-1 py-2.5 text-xs font-semibold transition-all duration-200 text-neon-cyan border-b-2 border-neon-cyan"
            data-view="dashboard" onclick="showView('dashboard')"
            data-i18n="nav.dashboard">
        Dashboard
    </button>
    <button class="nav-item flex-1 py-2.5 text-xs font-semibold transition-all duration-200 text-gray-500 hover:text-gray-300 border-b-2 border-transparent"
            data-view="nodes" onclick="showView('nodes')"
            data-i18n="nav.nodes">
        Nodes
    </button>
    <button class="nav-item flex-1 py-2.5 text-xs font-semibold transition-all duration-200 text-gray-500 hover:text-gray-300 border-b-2 border-transparent"
            data-view="settings" onclick="showView('settings')"
            data-i18n="nav.settings">
        Settings
    </button>
</div>
```

- [ ] **Step 2: Add Dashboard canvas container**

Replace the current "Fans List" section (around line 224) with:

```html
<!-- Dashboard Canvas (shown on Dashboard tab) -->
<div id="dashboard-canvas-container" class="flex-1 overflow-auto p-3">
    <div id="dashboard-canvas" class="relative min-h-full bg-cyber-bg rounded-lg border border-dashed border-gray-700">
        <div id="dashboard-empty" class="absolute inset-0 flex flex-col items-center justify-center text-gray-500">
            <div class="text-4xl mb-4">📊</div>
            <p class="text-sm" data-i18n="dashboard.empty">Dashboard is empty</p>
            <p class="text-xs text-gray-600 mt-1" data-i18n="dashboard.empty_hint">Click + to add monitoring cards</p>
        </div>
        <div id="dashboard-groups"></div>
        <div id="dashboard-cards"></div>
    </div>
    <button id="dashboard-add-btn" onclick="showCardPicker()"
            class="fixed bottom-6 right-6 w-12 h-12 bg-neon-cyan rounded-full text-black text-2xl font-bold shadow-lg hover:bg-cyan-400 transition-all z-40 hidden">
        +
    </button>
</div>

<!-- Node Tree (shown on Nodes tab) -->
<div id="node-tree-container" class="flex-1 overflow-y-auto p-3 space-y-2 hidden">
    <div id="node-tree"></div>
    <div class="border-t border-cyber-accent pt-2 mt-2">
        <div class="flex gap-2">
            <input id="new-node-name" type="text" 
                   class="flex-1 bg-cyber-bg border border-cyber-accent rounded px-2 py-1 text-xs text-white focus:border-neon-cyan focus:outline-none"
                   placeholder="Node name" data-i18n-placeholder="nodes.name_placeholder"
                   onkeydown="if(event.key==='Enter')addNode()">
            <button onclick="addNode()" 
                    class="px-2 py-1 bg-cyber-accent border border-cyber-accent rounded text-neon-cyan text-xs hover:bg-neon-cyan hover:bg-opacity-20 transition-all">
                +
            </button>
        </div>
    </div>
</div>
```

- [ ] **Step 3: Add translations**

In `static/lang/en.json`, add:
```json
"nav.dashboard": "Dashboard",
"nav.nodes": "Nodes",
"nav.settings": "Settings",
"dashboard.empty": "Dashboard is empty",
"dashboard.empty_hint": "Click + to add monitoring cards",
"dashboard.add_card": "Add Card",
"dashboard.add_group": "Add Group"
```

In `static/lang/ru.json`, add:
```json
"nav.dashboard": "Дашборд",
"nav.nodes": "Узлы",
"nav.settings": "Настройки",
"dashboard.empty": "Дашборд пуст",
"dashboard.empty_hint": "Нажмите + чтобы добавить карточки мониторинга",
"dashboard.add_card": "Добавить карточку",
"dashboard.add_group": "Добавить группу"
```

- [ ] **Step 4: Commit**

```bash
git add templates/index.html static/lang/en.json static/lang/ru.json
git commit -m "feat: restructure left panel with 3 tabs (Dashboard/Nodes/Settings)"
```

---

### Task 2: Build Node Tree View

**Covers:** [S3]

**Files:**
- Modify: `templates/js/main.js` (add buildNodeTree function)

- [ ] **Step 1: Add buildNodeTree function**

Add after the `buildFanList` function:

```javascript
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
    const tempCount = Object.keys(temps).length;
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
    // TODO: show node fan detail
    console.log('[FanControl] Select node fan:', nodeId, fanId);
}
```

- [ ] **Step 2: Update showView to toggle containers**

Find the `showView` function and update it to toggle between dashboard and node tree:

```javascript
function showView(view) {
    currentView = view;

    // Update tab styles
    document.querySelectorAll('.nav-item').forEach(btn => {
        const isActive = btn.dataset.view === view;
        btn.classList.toggle('text-neon-cyan', isActive);
        btn.classList.toggle('border-b-2', isActive);
        btn.classList.toggle('border-neon-cyan', isActive);
        btn.classList.toggle('text-gray-500', !isActive);
        btn.classList.toggle('border-transparent', !isActive);
    });

    // Toggle containers
    const dashboardContainer = document.getElementById('dashboard-canvas-container');
    const nodeTreeContainer = document.getElementById('node-tree-container');
    const dashboardView = document.getElementById('dashboard-view');
    const nodesView = document.getElementById('nodes-view');
    const nodeDetailView = document.getElementById('node-detail-view');
    const addBtn = document.getElementById('dashboard-add-btn');

    if (dashboardContainer) dashboardContainer.classList.toggle('hidden', view !== 'dashboard');
    if (nodeTreeContainer) nodeTreeContainer.classList.toggle('hidden', view !== 'nodes');
    if (dashboardView) dashboardView.classList.toggle('hidden', view !== 'dashboard');
    if (nodesView) nodesView.classList.toggle('hidden', view !== 'nodes');
    if (nodeDetailView) nodeDetailView.classList.toggle('hidden', view !== 'node-detail');
    if (addBtn) addBtn.classList.toggle('hidden', view !== 'dashboard');

    // Build node tree when switching to nodes tab
    if (view === 'nodes') {
        buildNodeTree();
    }

    // Build dashboard when switching to dashboard tab
    if (view === 'dashboard') {
        renderDashboard();
    }
}
```

- [ ] **Step 3: Update updateUI to refresh tree**

In the `updateUI` function, add tree refresh when on nodes tab:

```javascript
// After existing code, before the return:
if (currentView === 'nodes') {
    buildNodeTree();
}
```

- [ ] **Step 4: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: build node tree view with expandable servers and fans"
```

---

### Task 3: Node Status Indicators + Click-to-Inspect

**Covers:** [S3]

**Files:**
- Modify: `templates/js/main.js` (update node:update and node:telemetry handlers)

- [ ] **Step 1: Update node event handlers**

Find the `socket.on('node:update'` and `socket.on('node:telemetry'` handlers. After updating node data, rebuild the tree:

```javascript
socket.on('node:update', (data) => {
    const idx = nodesData.findIndex(n => n.node_id === data.node_id);
    if (idx >= 0) {
        nodesData[idx] = { ...nodesData[idx], ...data };
    }
    if (currentView === 'nodes') buildNodeTree();
});

socket.on('node:telemetry', (data) => {
    const idx = nodesData.findIndex(n => n.node_id === data.node_id);
    if (idx >= 0) {
        nodesData[idx].telemetry = data.telemetry;
    }
    if (currentView === 'nodes') buildNodeTree();
});
```

- [ ] **Step 2: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: node tree auto-updates on status and telemetry events"
```

---

## Phase 2: Custom Dashboard Canvas

### Task 4: Dashboard State + Config Persistence

**Covers:** [S4, S6, S7]

**Files:**
- Modify: `core/state.py` (add dashboard state)
- Modify: `core/config.py` (add dashboard to config)
- Modify: `server/routes.py` (add dashboard save endpoint)

- [ ] **Step 1: Add dashboard to state**

In `core/state.py`, add to the state dict initialization:

```python
'dashboard': {
    'groups': [],
    'cards': []
}
```

- [ ] **Step 2: Add dashboard to config save/load**

In `core/config.py`, in the `_do_save_config` function, add dashboard to the config dict:

```python
config = {
    'config_version': CONFIG_VERSION,
    'initialized': state.get('initialized', False),
    'tested': state.get('tested', False),
    'language': state.get('language', 'en'),
    'fans': {},
    'dashboard': state.get('dashboard', {'groups': [], 'cards': []})
}
```

In `load_config`, add:

```python
state['dashboard'] = cfg.get('dashboard', {'groups': [], 'cards': []})
```

- [ ] **Step 3: Add dashboard save endpoint**

In `server/routes.py`, add:

```python
@routes.route('/api/dashboard', methods=['POST'])
def api_save_dashboard():
    """Save dashboard layout (cards, groups, positions)."""
    data = request.get_json(silent=True) or {}
    with state_lock:
        state['dashboard'] = {
            'groups': data.get('groups', []),
            'cards': data.get('cards', [])
        }
    save_config()
    return jsonify({'status': 'saved'})

@routes.route('/api/dashboard', methods=['GET'])
def api_get_dashboard():
    """Get dashboard layout."""
    return jsonify(state.get('dashboard', {'groups': [], 'cards': []}))
```

- [ ] **Step 4: Commit**

```bash
git add core/state.py core/config.py server/routes.py
git commit -m "feat: add dashboard state, config persistence, and API endpoints"
```

---

### Task 5: Card Picker Modal

**Covers:** [S5]

**Files:**
- Modify: `templates/index.html` (add card picker modal)
- Modify: `templates/js/main.js` (add picker logic)

- [ ] **Step 1: Add card picker modal to HTML**

Before the closing `</body>` tag, add:

```html
<!-- Card Picker Modal -->
<div id="card-picker-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">
    <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-md w-full mx-4">
        <h3 class="text-lg font-bold text-white mb-4" data-i18n="dashboard.add_card">Add Card</h3>
        
        <div class="space-y-4">
            <div>
                <label class="text-xs text-gray-400 block mb-1">Type</label>
                <select id="picker-type" class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"
                        onchange="updatePickerElements()">
                    <option value="fan">🌀 Fan</option>
                    <option value="temperature">🌡 Temperature</option>
                    <option value="disk">💾 Disk</option>
                    <option value="system">📊 System</option>
                </select>
            </div>
            
            <div>
                <label class="text-xs text-gray-400 block mb-1">Source</label>
                <select id="picker-source" class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm"
                        onchange="updatePickerElements()">
                    <option value="local">My Server (local)</option>
                </select>
            </div>
            
            <div>
                <label class="text-xs text-gray-400 block mb-1">Element</label>
                <div id="picker-elements" class="max-h-48 overflow-y-auto space-y-1 bg-cyber-bg border border-cyber-accent rounded p-2">
                </div>
            </div>
        </div>
        
        <div class="flex gap-2 mt-6">
            <button onclick="hideCardPicker()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm">
                Cancel
            </button>
            <button onclick="addSelectedCards()" class="flex-1 py-2 rounded-lg bg-neon-cyan text-black font-semibold hover:bg-cyan-400 transition-all text-sm">
                Add
            </button>
        </div>
    </div>
</div>
```

- [ ] **Step 2: Add picker JavaScript**

```javascript
function showCardPicker() {
    document.getElementById('card-picker-modal').classList.remove('hidden');
    populatePickerSources();
    updatePickerElements();
}

function hideCardPicker() {
    document.getElementById('card-picker-modal').classList.add('hidden');
}

function populatePickerSources() {
    const select = document.getElementById('picker-source');
    select.innerHTML = '<option value="local">My Server (local)</option>';
    for (const node of nodesData) {
        select.innerHTML += `<option value="${escapeHtml(node.node_id)}">${escapeHtml(node.name)} (${escapeHtml(node.ip || 'unknown')})</option>`;
    }
}

function updatePickerElements() {
    const type = document.getElementById('picker-type').value;
    const source = document.getElementById('picker-source').value;
    const container = document.getElementById('picker-elements');
    
    let elements = [];
    
    if (source === 'local') {
        if (type === 'fan' && currentState?.fans) {
            elements = Object.entries(currentState.fans).map(([id, f]) => ({
                id, label: f.label || id, extra: `${f.rpm || 0} RPM`
            }));
        } else if (type === 'temperature' && currentState?.temp_sensors) {
            elements = Object.entries(currentState.temp_sensors).map(([id, s]) => ({
                id, label: s.label || id, extra: `${s.value || 0}°C`
            }));
        } else if (type === 'disk' && currentState?.hdd_sensors) {
            elements = Object.entries(currentState.hdd_sensors).map(([id, d]) => ({
                id, label: d.label || id, extra: `${d.temp || 0}°C`
            }));
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
                elements = Object.entries(tel.fans).map(([id, f]) => ({
                    id, label: f.label || id, extra: `${f.rpm || 0} RPM`
                }));
            } else if (type === 'temperature' && tel.temp_sensors) {
                elements = Object.entries(tel.temp_sensors).map(([id, s]) => ({
                    id, label: s.label || id, extra: `${s.value || 0}°C`
                }));
            } else if (type === 'disk' && tel.hdd_sensors) {
                elements = Object.entries(tel.hdd_sensors).map(([id, d]) => ({
                    id, label: d.label || id, extra: `${d.temp || 0}°C`
                }));
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
    const type = document.getElementById('picker-type').value;
    const source = document.getElementById('picker-source').value;
    const checkboxes = document.querySelectorAll('.picker-checkbox:checked');
    
    checkboxes.forEach(cb => {
        const card = {
            id: 'card-' + Date.now() + '-' + Math.random().toString(36).substr(2, 5),
            type: type,
            source: source,
            element_id: cb.value,
            label: cb.dataset.label,
            x: 20 + Math.random() * 100,
            y: 20 + Math.random() * 100,
            w: 200,
            h: 120,
            group_id: null
        };
        dashboardState.cards.push(card);
    });
    
    saveDashboard();
    renderDashboard();
    hideCardPicker();
}
```

- [ ] **Step 3: Add initial dashboard state**

At the top of main.js, add:

```javascript
let dashboardState = { groups: [], cards: [] };
```

- [ ] **Step 4: Commit**

```bash
git add templates/index.html templates/js/main.js
git commit -m "feat: add card picker modal for dashboard"
```

---

### Task 6: Dashboard Rendering + Drag and Drop

**Covers:** [S4]

**Files:**
- Modify: `templates/js/main.js` (add renderDashboard, drag functions)

- [ ] **Step 1: Add dashboard rendering**

```javascript
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
                 data-group-id="${group.id}"
                 ondragover="event.preventDefault(); this.classList.add('border-neon-cyan')"
                 ondragleave="this.classList.remove('border-neon-cyan')"
                 ondrop="dropCardToGroup(event, '${group.id}'); this.classList.remove('border-neon-cyan')">
                <div class="flex items-center justify-between mb-2">
                    <span class="text-xs font-semibold text-gray-400">${escapeHtml(group.name)}</span>
                    <button onclick="removeGroup('${group.id}')" class="text-gray-600 hover:text-red-400 text-xs">×</button>
                </div>
                <div class="group-cards" data-group-id="${group.id}"></div>
            </div>
        `).join('');
    }
    
    // Render cards
    cardsContainer.innerHTML = dashboardState.cards.map(card => renderDashboardCard(card)).join('');
}

function renderDashboardCard(card) {
    const liveData = getCardLiveData(card);
    const sourceName = card.source === 'local' ? 'Local' : (nodesData.find(n => n.node_id === card.source)?.name || card.source);
    
    return `
        <div class="dashboard-card absolute bg-cyber-card border border-cyber-accent rounded-lg overflow-hidden cursor-move select-none"
             style="left:${card.x}px; top:${card.y}px; width:${card.w}px; height:${card.h}px;"
             data-card-id="${card.id}"
             draggable="true"
             ondragstart="dragCard(event, '${card.id}')">
            <div class="flex items-center justify-between px-2 py-1 bg-cyber-accent border-b border-cyber-accent cursor-move"
                 onmousedown="startDragCard(event, '${card.id}')">
                <span class="text-xs text-gray-400 truncate">${escapeHtml(card.label)} — ${sourceName}</span>
                <button onclick="removeCard('${card.id}')" class="text-gray-600 hover:text-red-400 text-xs ml-1">×</button>
            </div>
            <div class="p-2 flex-1">
                ${renderCardContent(card, liveData)}
            </div>
            <div class="absolute bottom-0 right-0 w-4 h-4 cursor-se-resize opacity-50 hover:opacity-100"
                 onmousedown="startResizeCard(event, '${card.id}')">
                <svg viewBox="0 0 16 16" class="w-4 h-4 text-gray-500">
                    <path d="M14 14L14 8M14 14L8 14" stroke="currentColor" stroke-width="2" fill="none"/>
                </svg>
            </div>
        </div>
    `;
}

function renderCardContent(card, data) {
    if (card.type === 'fan') {
        return `
            <div class="text-lg font-bold font-mono text-neon-cyan">${data.rpm || 0} <span class="text-xs text-gray-500">RPM</span></div>
            <div class="text-xs text-gray-400">${data.pct || 0}% · ${data.mode || 'manual'}</div>
        `;
    } else if (card.type === 'temperature') {
        const temp = data.value || 0;
        const color = temp > 70 ? 'text-neon-red' : temp > 50 ? 'text-neon-orange' : 'text-neon-green';
        return `
            <div class="text-lg font-bold font-mono ${color}">${temp}°C</div>
            <div class="text-xs text-gray-400">${data.label || ''}</div>
        `;
    } else if (card.type === 'disk') {
        return `
            <div class="text-lg font-bold font-mono text-neon-purple">${data.temp || 0}°C</div>
            <div class="text-xs text-gray-400">${data.type || 'disk'}</div>
        `;
    } else if (card.type === 'system') {
        return `
            <div class="text-lg font-bold font-mono text-neon-cyan">${data.value || '--'}</div>
            <div class="text-xs text-gray-400">${data.label || ''}</div>
        `;
    }
    return '<div class="text-xs text-gray-500">Unknown card type</div>';
}

function getCardLiveData(card) {
    if (card.source === 'local') {
        if (card.type === 'fan' && currentState?.fans?.[card.element_id]) {
            const f = currentState.fans[card.element_id];
            return { rpm: f.rpm, pct: f.current_pct || f.manual_pct, mode: f.mode };
        } else if (card.type === 'temperature' && currentState?.temp_sensors?.[card.element_id]) {
            return currentState.temp_sensors[card.element_id];
        } else if (card.type === 'disk' && currentState?.hdd_sensors?.[card.element_id]) {
            return currentState.hdd_sensors[card.element_id];
        }
    } else {
        const node = nodesData.find(n => n.node_id === card.source);
        if (node?.telemetry) {
            if (card.type === 'fan') return node.telemetry.fans?.[card.element_id] || {};
            if (card.type === 'temperature') return node.telemetry.temp_sensors?.[card.element_id] || {};
            if (card.type === 'disk') return node.telemetry.hdd_sensors?.[card.element_id] || {};
        }
    }
    return {};
}
```

- [ ] **Step 2: Add drag and drop**

```javascript
let draggedCardId = null;
let dragOffset = { x: 0, y: 0 };

function dragCard(event, cardId) {
    event.dataTransfer.setData('text/plain', cardId);
}

function startDragCard(event, cardId) {
    draggedCardId = cardId;
    const card = dashboardState.cards.find(c => c.id === cardId);
    if (!card) return;
    
    const canvas = document.getElementById('dashboard-canvas');
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

function dropCardToGroup(event, groupId) {
    event.preventDefault();
    const cardId = event.dataTransfer.getData('text/plain');
    const card = dashboardState.cards.find(c => c.id === cardId);
    if (card) {
        card.group_id = groupId;
        saveDashboard();
        renderDashboard();
    }
}
```

- [ ] **Step 3: Add card resize**

```javascript
let resizedCardId = null;
let resizeStart = { x: 0, y: 0, w: 0, h: 0 };

function startResizeCard(event, cardId) {
    resizedCardId = cardId;
    const card = dashboardState.cards.find(c => c.id === cardId);
    if (!card) return;
    
    resizeStart = { x: event.clientX, y: event.clientY, w: card.w, h: card.h };
    
    document.addEventListener('mousemove', onResizeCard);
    document.addEventListener('mouseup', onResizeCardEnd);
    event.preventDefault();
    event.stopPropagation();
}

function onResizeCard(event) {
    if (!resizedCardId) return;
    const card = dashboardState.cards.find(c => c.id === resizedCardId);
    if (!card) return;
    
    card.w = Math.max(100, resizeStart.w + (event.clientX - resizeStart.x));
    card.h = Math.max(60, resizeStart.h + (event.clientY - resizeStart.y));
    
    const el = document.querySelector(`[data-card-id="${resizedCardId}"]`);
    if (el) {
        el.style.width = card.w + 'px';
        el.style.height = card.h + 'px';
    }
}

function onResizeCardEnd() {
    if (resizedCardId) {
        saveDashboard();
    }
    resizedCardId = null;
    document.removeEventListener('mousemove', onResizeCard);
    document.removeEventListener('mouseup', onResizeCardEnd);
}
```

- [ ] **Step 4: Add card/group CRUD**

```javascript
function removeCard(cardId) {
    dashboardState.cards = dashboardState.cards.filter(c => c.id !== cardId);
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
```

- [ ] **Step 5: Load dashboard on connect**

In the `socket.on('connect'` handler, add:

```javascript
loadDashboard();
```

- [ ] **Step 6: Update live data on socket update**

In the `socket.on('update'` handler, after `updateUI(data)`, add:

```javascript
if (currentView === 'dashboard') {
    renderDashboard();
}
```

- [ ] **Step 7: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: dashboard canvas with drag-and-drop cards and resize"
```

---

## Phase 3: Groups

### Task 7: Group Creation + Management

**Covers:** [S4]

**Files:**
- Modify: `templates/js/main.js` (add group functions)
- Modify: `templates/index.html` (add group button)

- [ ] **Step 1: Add group button to dashboard**

In the dashboard canvas container, add a group button next to the add button:

```html
<button id="dashboard-group-btn" onclick="showGroupCreator()"
        class="fixed bottom-6 right-20 w-12 h-12 bg-neon-purple rounded-full text-white text-lg shadow-lg hover:bg-purple-400 transition-all z-40 hidden">
    ⊞
</button>
```

- [ ] **Step 2: Add group creator modal**

```html
<!-- Group Creator Modal -->
<div id="group-creator-modal" class="hidden fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-80">
    <div class="bg-cyber-card border border-cyber-accent rounded-xl p-6 max-w-sm w-full mx-4">
        <h3 class="text-lg font-bold text-white mb-4" data-i18n="dashboard.add_group">Add Group</h3>
        <input id="group-name-input" type="text" placeholder="Group name (e.g., CPU Cooling)"
               class="w-full bg-cyber-bg border border-cyber-accent rounded px-3 py-2 text-white text-sm mb-4">
        <div class="flex gap-2">
            <button onclick="hideGroupCreator()" class="flex-1 py-2 rounded-lg border border-gray-600 text-gray-400 hover:text-white transition-all text-sm">
                Cancel
            </button>
            <button onclick="createGroup()" class="flex-1 py-2 rounded-lg bg-neon-purple text-white font-semibold hover:bg-purple-400 transition-all text-sm">
                Create
            </button>
        </div>
    </div>
</div>
```

- [ ] **Step 3: Add group JavaScript**

```javascript
function showGroupCreator() {
    document.getElementById('group-creator-modal').classList.remove('hidden');
    document.getElementById('group-name-input').value = '';
    document.getElementById('group-name-input').focus();
}

function hideGroupCreator() {
    document.getElementById('group-creator-modal').classList.add('hidden');
}

function createGroup() {
    const name = document.getElementById('group-name-input').value.trim();
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

function removeGroup(groupId) {
    // Move cards out of group
    dashboardState.cards.forEach(card => {
        if (card.group_id === groupId) card.group_id = null;
    });
    dashboardState.groups = dashboardState.groups.filter(g => g.id !== groupId);
    saveDashboard();
    renderDashboard();
}
```

- [ ] **Step 4: Toggle group button visibility**

In the `showView` function, update the addBtn toggle to also handle groupBtn:

```javascript
if (addBtn) addBtn.classList.toggle('hidden', view !== 'dashboard');
const groupBtn = document.getElementById('dashboard-group-btn');
if (groupBtn) groupBtn.classList.toggle('hidden', view !== 'dashboard');
```

- [ ] **Step 5: Commit**

```bash
git add templates/index.html templates/js/main.js
git commit -m "feat: add named groups for dashboard card organization"
```

---

## Phase 4: Polish

### Task 8: Card Hover Effects + Live Updates

**Covers:** [S4, S6]

**Files:**
- Modify: `templates/index.html` (add CSS for dashboard cards)

- [ ] **Step 1: Add dashboard card styles**

In the `<style>` section, add:

```css
.dashboard-card {
    transition: box-shadow 0.2s ease, border-color 0.2s ease;
}
.dashboard-card:hover {
    border-color: #06b6d4;
    box-shadow: 0 0 12px rgba(6, 182, 212, 0.2);
}
.dashboard-group {
    transition: border-color 0.2s ease;
}
.dashboard-group:hover {
    border-color: #a855f7;
}
```

- [ ] **Step 2: Commit**

```bash
git add templates/index.html
git commit -m "style: add hover effects for dashboard cards and groups"
```

---

### Task 9: Empty State Messaging

**Covers:** [S4]

**Files:**
- Modify: `templates/js/main.js` (improve empty states)

- [ ] **Step 1: Update empty state for nodes tab**

In `buildNodeTree`, the empty state is already handled. Verify it shows proper message.

- [ ] **Step 2: Commit (if changes needed)**

```bash
git add templates/js/main.js
git commit -m "feat: improve empty state messaging for dashboard and nodes"
```

---

### Task 10: Responsive Layout

**Covers:** [S3, S4]

**Files:**
- Modify: `templates/index.html` (adjust responsive classes)

- [ ] **Step 1: Ensure responsive behavior**

The left panel already has `w-full lg:w-80 xl:w-96`. The dashboard canvas uses `overflow-auto`. Verify on mobile that tabs work and canvas scrolls.

- [ ] **Step 2: Commit (if changes needed)**

```bash
git add templates/index.html
git commit -m "style: ensure responsive layout for node tree and dashboard"
```

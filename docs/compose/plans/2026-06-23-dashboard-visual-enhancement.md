# Dashboard Visual Enhancement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add mini sparklines, pulsating status indicators, and system widget to the dashboard for a richer, more filled appearance.

**Architecture:** Vanilla JS + SVG + CSS animations. No new dependencies. All changes in existing files (main.js, index.html, core/).

**Tech Stack:** JavaScript, SVG, CSS Keyframes, Flask backend (for system data)

## Global Constraints

- Bump `CONFIG_VERSION` in `core/state.py` after each visible change
- Bump `?v=VERSION` in `templates/index.html` for cache busting
- User communicates in Russian — commit messages in English
- Git identity not configured — use `-c user.name/user.email` inline flags
- Follow existing cyberpunk theme (neon-cyan, neon-green, neon-purple, bg-cyber-card)

---

## Feature 1: Mini Sparklines

### Task 1: Add sparkline history storage

**Files:**
- Modify: `templates/js/main.js` — add `_sparklineHistory` object and helper functions

**Description:**
Store last 20 values per sensor. Update on each live timer tick.

- [ ] **Step 1: Add history storage**

```javascript
// Near other global state variables
const _sparklineHistory = {};
const SPARKLINE_MAX = 20;

function pushSparkline(key, value) {
    if (!_sparklineHistory[key]) _sparklineHistory[key] = [];
    _sparklineHistory[key].push(value);
    if (_sparklineHistory[key].length > SPARKLINE_MAX) _sparklineHistory[key].shift();
}

function getSparkline(key) {
    return _sparklineHistory[key] || [];
}
```

- [ ] **Step 2: Add push calls in live update timer**

In `startPickerLiveUpdate`, after updating fan RPM values:
```javascript
pushSparkline(`fan:${source}:${id}`, fan.rpm || 0);
```

After updating temp sensor values:
```javascript
pushSparkline(`temp:${source}:${id}`, val);
```

After updating disk values:
```javascript
pushSparkline(`disk:${id}`, currentState.hdd_sensors[id].temp || 0);
```

- [ ] **Step 3: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: add sparkline history storage for dashboard cards"
```

---

### Task 2: Add sparkline SVG rendering

**Files:**
- Modify: `templates/js/main.js` — add `renderSparkline` function

**Description:**
Create SVG polyline from history data. Inline in card below the main value.

- [ ] **Step 1: Add renderSparkline function**

```javascript
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
```

- [ ] **Step 2: Add sparkline to card rendering**

In `renderPickerCard`, after the value HTML:
```javascript
const sparkKey = `${type}:${source}:${sourceId}`;
const sparkColor = type === 'fan' ? '#22d3ee' : type === 'temperature' ? '#4ade80' : '#c084fc';
valueHtml += renderSparkline(sparkKey, sparkColor);
```

- [ ] **Step 3: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: add SVG sparkline rendering to dashboard cards"
```

---

## Feature 2: Pulsating Status Indicators

### Task 3: Add CSS pulse animation

**Files:**
- Modify: `templates/index.html` — add `@keyframes` in `<style>` block

**Description:**
CSS animation for status dots.

- [ ] **Step 1: Add keyframes**

```css
@keyframes pulse-green {
    0%, 100% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.7); }
    50% { box-shadow: 0 0 0 6px rgba(74, 222, 128, 0); }
}
@keyframes pulse-red {
    0%, 100% { box-shadow: 0 0 0 0 rgba(248, 113, 113, 0.7); }
    50% { box-shadow: 0 0 0 6px rgba(248, 113, 113, 0); }
}
@keyframes pulse-yellow {
    0%, 100% { box-shadow: 0 0 0 0 rgba(250, 204, 21, 0.7); }
    50% { box-shadow: 0 0 0 6px rgba(250, 204, 21, 0); }
}
.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
}
.status-dot.green { background: #4ade80; animation: pulse-green 2s infinite; }
.status-dot.red { background: #f87171; animation: pulse-red 2s infinite; }
.status-dot.yellow { background: #facc15; animation: pulse-yellow 2s infinite; }
```

- [ ] **Step 2: Commit**

```bash
git add templates/index.html
git commit -m "feat: add CSS pulse animations for status indicators"
```

---

### Task 4: Add status dots to fan cards

**Files:**
- Modify: `templates/js/main.js` — update `renderPickerCard` and `updateCardDetails`

**Description:**
Add colored dot next to fan name based on status.

- [ ] **Step 1: Add status dot to renderPickerCard**

After the fan icon in the header:
```javascript
if (type === 'fan') {
    const fanData = getFanData(source, sourceId);
    const status = fanData?.status || 'unknown';
    const dotClass = status === 'running' ? 'green' : status === 'failsafe' || status === 'critical' ? 'red' : 'yellow';
    icon = `🌀 <span class="status-dot ${dotClass}"></span>`;
}
```

- [ ] **Step 2: Update status dot in live timer**

In `startPickerLiveUpdate`, after updating fan values:
```javascript
const dot = cardEl.querySelector('.status-dot');
if (dot) {
    const status = fan.status || 'unknown';
    dot.className = 'status-dot ' + (status === 'running' ? 'green' : status === 'failsafe' || status === 'critical' ? 'red' : 'yellow');
}
```

- [ ] **Step 3: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: add pulsating status dots to fan cards"
```

---

## Feature 3: System Widget

### Task 5: Add backend system info endpoint

**Files:**
- Modify: `server/routes.py` — add `/api/system` endpoint
- Modify: `core/hardware.py` — add `get_system_info()` function

**Description:**
Return uptime, CPU load, memory usage.

- [ ] **Step 1: Add get_system_info in hardware.py**

```python
def get_system_info():
    """Get system info: uptime, CPU, memory."""
    import os
    info = {}
    
    # Uptime
    try:
        with open('/proc/uptime') as f:
            uptime_sec = float(f.read().split()[0])
        days = int(uptime_sec // 86400)
        hours = int((uptime_sec % 86400) // 3600)
        mins = int((uptime_sec % 3600) // 60)
        info['uptime'] = f"{days}d {hours}h {mins}m"
        info['uptime_seconds'] = uptime_sec
    except:
        info['uptime'] = '--'
        info['uptime_seconds'] = 0
    
    # CPU load
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        info['cpu_load'] = round(load1 / cpu_count * 100, 1)
    except:
        info['cpu_load'] = 0
    
    # Memory
    try:
        with open('/proc/meminfo') as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ('MemTotal:', 'MemAvailable:'):
                    mem[parts[0]] = int(parts[1])
        total = mem.get('MemTotal:', 1)
        avail = mem.get('MemAvailable:', 0)
        info['mem_total_mb'] = round(total / 1024)
        info['mem_used_mb'] = round((total - avail) / 1024)
        info['mem_percent'] = round((total - avail) / total * 100, 1)
    except:
        info['mem_percent'] = 0
    
    return info
```

- [ ] **Step 2: Add API endpoint in routes.py**

```python
@routes.route('/api/system')
def api_get_system():
    from core.hardware import get_system_info
    return jsonify(get_system_info())
```

- [ ] **Step 3: Commit**

```bash
git add core/hardware.py server/routes.py
git commit -m "feat: add /api/system endpoint for uptime, CPU, memory"
```

---

### Task 6: Add system widget card type

**Files:**
- Modify: `templates/js/main.js` — add system card rendering

**Description:**
New card type "system" with uptime, CPU bar, RAM bar.

- [ ] **Step 1: Add system card to renderPickerCard**

```javascript
} else if (type === 'system') {
    icon = '🖥';
    colorClass = 'text-neon-yellow';
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
                    <div class="h-full bg-neon-cyan rounded-full transition-all duration-500" data-system-bar="cpu" style="width:0%"></div>
                </div>
            </div>
            <div>
                <div class="flex justify-between text-xs mb-1">
                    <span class="text-gray-500">RAM</span>
                    <span class="text-gray-300 font-mono" data-system-field="mem">--%</span>
                </div>
                <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">
                    <div class="h-full bg-neon-purple rounded-full transition-all duration-500" data-system-bar="mem" style="width:0%"></div>
                </div>
            </div>
        </div>`;
}
```

- [ ] **Step 2: Add system data update in live timer**

```javascript
// Fetch system info every 5 seconds
let _systemTimer = null;
function startSystemUpdate() {
    if (_systemTimer) return;
    _systemTimer = setInterval(async () => {
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
```

- [ ] **Step 3: Call startSystemUpdate from showMainScreen**

```javascript
function showMainScreen() {
    // ... existing code ...
    startSystemUpdate();
}
```

- [ ] **Step 4: Add system to card type picker**

In the "add card" modal, add system option:
```javascript
{ type: 'system', label: 'System Info', icon: '🖥' }
```

- [ ] **Step 5: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: add system widget card type with uptime, CPU, RAM bars"
```

---

## Execution Order

1. **Task 1** → sparkline storage (foundation)
2. **Task 2** → sparkline rendering (visual)
3. **Task 3** → CSS animations (foundation)
4. **Task 4** → status dots (visual)
5. **Task 5** → backend API (foundation)
6. **Task 6** → system widget (visual)

Tasks 1-2 and 3-4 are independent and can be parallelized.
Task 5-6 depend on each other but are independent from 1-4.

## Version Bump

After completing all tasks, bump `CONFIG_VERSION` to `3.5.110` and update `?v=` in index.html.

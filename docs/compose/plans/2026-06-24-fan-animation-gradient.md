# Animated Fan SVG + Card Gradients Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add animated SVG fan icon that rotates by RPM + subtle gradient backgrounds on all card types.

**Architecture:** SVG fan is a pure frontend addition (CSS animation + JS RPM-based duration). Gradients are CSS-only additions to existing card styles.

**Tech Stack:** Vanilla JS, CSS animations, SVG inline

## Global Constraints

- Version bump required with every visible change (CONFIG_VERSION + `?v=` cache buster)
- Git identity: use `-c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev"` inline flags
- User communicates in Russian
- No new dependencies

---

### Task 1: Animated SVG Fan Icon

**Files:**
- Modify: `templates/js/main.js:786-793` (fan card rendering in `renderPickerCard`)
- Modify: `templates/js/main.js:2309-2319` (fan RPM live update)
- Modify: `templates/index.html` (add `@keyframes fan-pulse` CSS animation)

**Interfaces:**
- Consumes: `fan.rpm`, `fan.status` from telemetry
- Produces: SVG element with `data-fan-anim-id` attribute for live updates

- [ ] **Step 1: Add fan SVG rendering in `renderPickerCard`**

Replace the `🌀` emoji block (lines 786-793) with SVG:

```javascript
if (type === 'fan') {
    const fanData = getFanData(source, sourceId);
    const fanStatus = fanData?.status || 'unknown';
    const rpm = fanData?.rpm || 0;
    const dotColor = fanStatus === 'running' ? 'green' : (fanStatus === 'failsafe' || fanStatus === 'critical') ? 'red' : 'yellow';
    const fanColor = fanStatus === 'running' ? '#22d3ee' : (fanStatus === 'failsafe' || fanStatus === 'critical') ? '#ef4444' : '#facc15';
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
}
```

- [ ] **Step 2: Update live RPM to also update fan animation**

In the `startPickerLiveUpdate` function where `data-fan-id` elements are updated (around line 2309), add animation update:

After `el.textContent = fan.rpm || 0;` add:

```javascript
const animEl = document.querySelector(`[data-fan-anim-id="${id}"][data-fan-source="${src}"]`);
if (animEl) {
    const rpm = fan.rpm || 0;
    const dur = rpm > 0 ? Math.max(0.2, 2 - (rpm / 1500)) : 0;
    animEl.style.animation = rpm > 0 ? `fan-spin ${dur}s linear infinite` : 'none';
    const fanColor = fan.status === 'running' ? '#22d3ee' : (fan.status === 'failsafe' || fan.status === 'critical') ? '#ef4444' : '#facc15';
    animEl.querySelectorAll('path, circle').forEach(p => p.setAttribute('fill', fanColor));
}
```

- [ ] **Step 3: Add CSS keyframes in `index.html`**

Add to the `<style>` section (before `</style>`):

```css
@keyframes fan-spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
}
```

- [ ] **Step 4: Test and verify**

Open browser, add fan card to dashboard, verify:
- Fan icon is SVG with 4 blades
- At 0 RPM: static, yellow color
- At >0 RPM: spinning, cyan color
- On failsafe: red color, spinning or static depending on RPM

- [ ] **Step 5: Commit**

```bash
git add templates/js/main.js templates/index.html
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "feat: animated SVG fan icon with RPM-based rotation speed"
```

---

### Task 2: Card Gradient Backgrounds

**Files:**
- Modify: `templates/index.html` (add gradient CSS classes)
- Modify: `templates/js/main.js:848` (apply gradient class to card element)

**Interfaces:**
- Consumes: card `type` field from card data
- Produces: CSS class on card element

- [ ] **Step 1: Add gradient CSS classes in `index.html`**

Add to `<style>` section:

```css
.card-gradient-fan {
    background: linear-gradient(135deg, rgba(34,211,238,0.08), rgba(34,211,238,0.02));
}
.card-gradient-fan:hover {
    background: linear-gradient(135deg, rgba(34,211,238,0.12), rgba(34,211,238,0.04));
}
.card-gradient-temp {
    background: linear-gradient(135deg, rgba(74,222,128,0.08), rgba(74,222,128,0.02));
}
.card-gradient-temp:hover {
    background: linear-gradient(135deg, rgba(74,222,128,0.12), rgba(74,222,128,0.04));
}
.card-gradient-disk {
    background: linear-gradient(135deg, rgba(192,132,252,0.08), rgba(192,132,252,0.02));
}
.card-gradient-disk:hover {
    background: linear-gradient(135deg, rgba(192,132,252,0.12), rgba(192,132,252,0.04));
}
.card-gradient-system {
    background: linear-gradient(135deg, rgba(250,204,21,0.08), rgba(250,204,21,0.02));
}
.card-gradient-system:hover {
    background: linear-gradient(135deg, rgba(250,204,21,0.12), rgba(250,204,21,0.04));
}
```

- [ ] **Step 2: Apply gradient class in `renderPickerCard`**

In `renderPickerCard` (around line 848), add gradient class based on type:

```javascript
const gradientClass = `card-gradient-${type}`;
el.className = `bg-cyber-card border border-cyber-accent rounded-xl p-4 transition-[border-color,box-shadow,background] duration-200 hover:border-neon-cyan/50 hover:shadow-neon-cyan/10 hover:shadow-lg cursor-grab active:cursor-grabbing ${gradientClass}`;
```

- [ ] **Step 3: Test and verify**

Open browser, verify:
- Fan cards have subtle cyan gradient
- Temp cards have subtle green gradient
- Disk cards have subtle purple gradient
- System cards have subtle yellow gradient
- Gradient intensifies on hover

- [ ] **Step 4: Commit**

```bash
git add templates/js/main.js templates/index.html
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "feat: subtle gradient backgrounds for dashboard cards by type"
```

---

### Task 3: Version Bump and Final Push

**Files:**
- Modify: `core/state.py` (CONFIG_VERSION)
- Modify: `templates/index.html` (cache buster)

- [ ] **Step 1: Bump version to 3.5.127**

```python
CONFIG_VERSION = "3.5.127"
```

- [ ] **Step 2: Update cache buster**

```html
<script src="/js/main.js?v=3.5.127"></script>
```

- [ ] **Step 3: Run tests**

```bash
python3 -m pytest tests/ -q
```

- [ ] **Step 4: Commit and push**

```bash
git add core/state.py templates/index.html
git -c user.name="MiMoCode" -c user.email="mimo@fancontrol.dev" commit -m "chore: bump version to 3.5.127"
git push
```

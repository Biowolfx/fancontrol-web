# Bug Fixes & Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix critical and high-severity bugs found during deep code analysis while preserving existing functionality.

**Architecture:** Targeted fixes in existing files — no new modules. Each task is a self-contained bugfix that can be deployed independently.

**Tech Stack:** Python 3.10, Flask, SocketIO, vanilla JavaScript

## Global Constraints

- Bump `CONFIG_VERSION` in `core/state.py` after each visible change
- Bump `?v=VERSION` in `templates/index.html` for cache busting
- User communicates in Russian — commit messages in English
- No lint/typecheck tooling configured — manual verification
- Git identity not configured globally — use `-c user.name/user.email` inline flags

---

## Phase 1: CRITICAL Fixes

### Task 1: Fix `get_state()` mutable cache return

**Files:**
- Modify: `core/state.py:60-71`

**Problem:** `get_state()` returns `_cached_state` directly. Callers can mutate it, corrupting the cache for all threads.

**Fix:** Return a shallow copy of the cached snapshot.

- [ ] **Step 1: Read current implementation**

```bash
grep -n "return _cached_state" core/state.py
```

- [ ] **Step 2: Apply fix**

```python
# core/state.py:60-71
def get_state() -> Dict[str, Any]:
    """Thread-safe snapshot of global state for API and Socket.IO."""
    global _cached_state, _cached_state_time
    now = time.monotonic()

    with state_lock:
        if _cached_state is not None and (now - _cached_state_time) < STATE_CACHE_TTL:
            return dict(_cached_state)  # Return shallow copy

        _cached_state = _build_state_snapshot()
        _cached_state_time = now
        return dict(_cached_state)  # Return shallow copy
```

- [ ] **Step 3: Verify fix**

```bash
python -c "from core.state import get_state; s1 = get_state(); s2 = get_state(); s1['new_key'] = 'test'; assert 'new_key' not in s2, 'Cache corrupted'"
```

Expected: No assertion error

- [ ] **Step 4: Commit**

```bash
git add core/state.py
git commit -m "fix: return shallow copy from get_state() to prevent cache mutation"
```

---

### Task 2: Define missing `onGroupCardDragOver` function

**Files:**
- Modify: `templates/js/main.js:2246` (add definition before usage)

**Problem:** `onGroupCardDragOver` is registered as event handler but never defined. Causes ReferenceError when dragging cards over groups.

**Fix:** Add the function definition near other group handlers.

- [ ] **Step 1: Find where group handlers are defined**

```bash
grep -n "function onGroupDrag" templates/js/main.js
```

- [ ] **Step 2: Add missing function**

```javascript
// templates/js/main.js — add before onGroupDragStart definition
function onGroupCardDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
}
```

- [ ] **Step 3: Verify no syntax errors**

```bash
node -c templates/js/main.js
```

Expected: No output (syntax OK)

- [ ] **Step 4: Commit**

```bash
git add templates/js/main.js
git commit -m "fix: add missing onGroupCardDragOver handler for group DnD"
```

---

### Task 3: Add `stopPickerLiveUpdate()` function

**Files:**
- Modify: `templates/js/main.js:2142-2191`

**Problem:** `_pickerLiveTimer` interval is never cleared. Runs forever even when dashboard is hidden.

**Fix:** Add `stopPickerLiveUpdate()` function and call it when leaving main screen.

- [ ] **Step 1: Add stop function after startPickerLiveUpdate**

```javascript
// templates/js/main.js — add after startPickerLiveUpdate function (after line 2191)
function stopPickerLiveUpdate() {
    if (_pickerLiveTimer) {
        clearInterval(_pickerLiveTimer);
        _pickerLiveTimer = null;
    }
}
```

- [ ] **Step 2: Find where main screen is hidden**

```bash
grep -n "showSetupScreen\|setup-screen\|dashboard-canvas-container.*hidden" templates/js/main.js | head -20
```

- [ ] **Step 3: Add stopPickerLiveUpdate call**

In `showSetupScreen()` or equivalent function that hides the dashboard, add:

```javascript
stopPickerLiveUpdate();
```

- [ ] **Step 4: Commit**

```bash
git add templates/js/main.js
git commit -m "fix: add stopPickerLiveUpdate to prevent interval leak"
```

---

## Phase 2: HIGH Fixes

### Task 4: Add `room=node_id` to agent config push

**Files:**
- Modify: `server/agent_handlers.py:78-80`

**Problem:** `socketio.emit('server:config_push', ...)` broadcasts to all clients instead of specific agent.

**Fix:** Add `room=node_id` parameter.

- [ ] **Step 1: Read current code**

```bash
grep -A3 "socketio.emit.*server:config_push" server/agent_handlers.py
```

- [ ] **Step 2: Apply fix**

```python
# server/agent_handlers.py:78-80
socketio.emit('server:config_push', {
    'config': server_config,
}, room=node_id)  # Add room parameter
```

- [ ] **Step 3: Commit**

```bash
git add server/agent_handlers.py
git commit -m "fix: emit config_push to specific agent room, not broadcast"
```

---

### Task 5: Add `set_pwm()` call in agent `_on_command`

**Files:**
- Modify: `agent/client.py:79-89`

**Problem:** Server commands update state but don't write to hardware immediately.

**Fix:** Call `set_pwm()` after updating state.

- [ ] **Step 1: Read current code**

```bash
grep -B2 -A10 "def _on_command" agent/client.py
```

- [ ] **Step 2: Apply fix**

```python
# agent/client.py:79-89
def _on_command(data):
    """Server sends a command (set_fan, etc.)."""
    cmd = data.get('command')
    if cmd == 'set_fan':
        fan_id = data.get('fan_id')
        value = data.get('value')
        with state_lock:
            if fan_id in state['fans']:
                state['fans'][fan_id]['manual_pct'] = value
                state['fans'][fan_id]['mode'] = 'manual'
        invalidate_state_cache()
        # Apply PWM immediately
        from core.hardware import set_pwm
        set_pwm(fan_id, value)
```

- [ ] **Step 3: Commit**

```bash
git add agent/client.py
git commit -m "fix: apply PWM immediately on server command instead of waiting for control loop"
```

---

### Task 6: Protect `_smart_cache` with lock

**Files:**
- Modify: `core/hardware.py:290-291`

**Problem:** Concurrent access from Flask threads can cause RuntimeError.

**Fix:** Add a threading.Lock for cache access.

- [ ] **Step 1: Find cache definitions**

```bash
grep -n "_smart_cache" core/hardware.py | head -10
```

- [ ] **Step 2: Add lock**

```python
# core/hardware.py — after imports
import threading
_smart_cache_lock = threading.Lock()
```

- [ ] **Step 3: Wrap cache access**

```python
# In api_get_disk_smart and other cache access points
with _smart_cache_lock:
    if disk_id in _smart_cache:
        return jsonify(_smart_cache[disk_id])
    # ... cache miss logic
```

- [ ] **Step 4: Commit**

```bash
git add core/hardware.py
git commit -m "fix: add lock for _smart_cache to prevent concurrent dict mutation"
```

---

### Task 7: Add authentication to update endpoint

**Files:**
- Modify: `server/routes.py:307-399`

**Problem:** `/api/update/apply` has no authentication — any client can trigger git reset.

**Fix:** Add simple token check via environment variable.

- [ ] **Step 1: Add token check at start of handler**

```python
# server/routes.py:307-315
@routes.route('/api/update/apply', methods=['POST'])
def api_update_apply():
    """Pull latest code, sync to /app, then exit process."""
    # Require update token
    update_token = os.environ.get('FANCONTROL_UPDATE_TOKEN')
    if update_token:
        provided = request.headers.get('X-Update-Token') or request.args.get('token')
        if provided != update_token:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401
    
    try:
        repo_dir = '/repo'
        # ... rest of handler
```

- [ ] **Step 2: Commit**

```bash
git add server/routes.py
git commit -m "fix: add optional token authentication to update endpoint"
```

---

### Task 8: Cache grid dimensions during drag operations

**Files:**
- Modify: `templates/js/main.js:1297-1332`

**Problem:** `isCellOccupied` calls `getComputedStyle` on every mousemove (60fps).

**Fix:** Pre-compute grid params at drag start, pass to functions.

- [ ] **Step 1: Find onCardMouseDown where drag starts**

```bash
grep -n "function onCardMouseDown" templates/js/main.js
```

- [ ] **Step 2: Add grid cache variables**

```javascript
// templates/js/main.js — near other card drag globals
let _dragGridCache = null;

function _computeGridCache() {
    const canvas = document.getElementById('dashboard-canvas');
    if (!canvas) return null;
    const style = getComputedStyle(canvas);
    const padL = parseFloat(style.paddingLeft) || 0;
    const padR = parseFloat(style.paddingRight) || 0;
    const contentW = canvas.offsetWidth - padL - padR;
    const cols = parseInt(style.gridTemplateColumns?.split(' ')?.length || 12);
    const gap = parseFloat(style.gap) || 8;
    const colW = (contentW - (cols - 1) * gap) / cols;
    return { cols, gap, colW, padL };
}
```

- [ ] **Step 3: Cache at drag start, clear at drag end**

In `onCardMouseDown`: `_dragGridCache = _computeGridCache();`
In `onCardMouseUp`: `_dragGridCache = null;`

- [ ] **Step 4: Use cache in isCellOccupied**

```javascript
function isCellOccupied(col, row, colSpan, rowSpan, excludeId) {
    const grid = _dragGridCache || _computeGridCache();
    if (!grid) return false;
    // Use grid.cols, grid.gap, grid.colW instead of re-querying DOM
    // ... rest of logic
}
```

- [ ] **Step 5: Commit**

```bash
git add templates/js/main.js
git commit -m "perf: cache grid dimensions during drag to avoid per-frame getComputedStyle"
```

---

## Phase 3: MEDIUM Fixes

### Task 9: Fix `escapeHtml` XSS vulnerability

**Files:**
- Modify: `templates/js/main.js:145-150`

**Problem:** `escapeHtml` doesn't escape single quotes, breaking inline onclick handlers.

**Fix:** Add single quote escaping and use regex-based approach.

- [ ] **Step 1: Replace escapeHtml implementation**

```javascript
// templates/js/main.js:145-150
function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}
```

- [ ] **Step 2: Commit**

```bash
git add templates/js/main.js
git commit -m "fix: escape single quotes in escapeHtml to prevent XSS in onclick handlers"
```

---

### Task 10: Fix `ruleKey` mutating sensors array

**Files:**
- Modify: `templates/js/main.js:3222-3230`

**Problem:** `.sort()` mutates the original `item.sensors` array in global state.

**Fix:** Sort a copy.

- [ ] **Step 1: Find the code**

```bash
grep -n "sensors.*sort" templates/js/main.js
```

- [ ] **Step 2: Apply fix**

```javascript
// Change from:
sensors: (item.sensors || []).sort()
// To:
sensors: [...(item.sensors || [])].sort()
```

- [ ] **Step 3: Commit**

```bash
git add templates/js/main.js
git commit -m "fix: sort sensors copy in ruleKey to prevent mutating global state"
```

---

### Task 11: Add null checks in `updateInspector`

**Files:**
- Modify: `templates/js/main.js:2469-2567`

**Problem:** DOM queries without null checks crash when inspector elements missing.

**Fix:** Add optional chaining.

- [ ] **Step 1: Find the function**

```bash
grep -n "function updateInspector" templates/js/main.js
```

- [ ] **Step 2: Add null guards**

```javascript
// Change:
document.getElementById('inspector-empty').classList.add('hidden')
// To:
document.getElementById('inspector-empty')?.classList.add('hidden')
```

- [ ] **Step 3: Repeat for all getElementById calls in the function**

- [ ] **Step 4: Commit**

```bash
git add templates/js/main.js
git commit -m "fix: add null guards in updateInspector to prevent crash when elements missing"
```

---

### Task 12: Fix `formatTemp(0)` returning '--'

**Files:**
- Modify: `templates/js/main.js:67`

**Problem:** 0°C is treated as "no data".

**Fix:** Only check for null/undefined.

- [ ] **Step 1: Find the function**

```bash
grep -n "function formatTemp" templates/js/main.js
```

- [ ] **Step 2: Apply fix**

```javascript
// Change from:
if (celsius == null || celsius === 0) return '--'
// To:
if (celsius == null) return '--'
```

- [ ] **Step 3: Commit**

```bash
git add templates/js/main.js
git commit -m "fix: treat 0°C as valid temperature in formatTemp"
```

---

### Task 13: Fix path traversal in `/api/lang/<code>`

**Files:**
- Modify: `server/routes.py:64-71`

**Problem:** `code` parameter not validated, could allow path traversal.

**Fix:** Validate format before file access.

- [ ] **Step 1: Find the route**

```bash
grep -n "api_get_lang" server/routes.py
```

- [ ] **Step 2: Add validation**

```python
# server/routes.py:64-71
import re

@routes.route('/api/lang/<code>')
def api_get_lang(code):
    # Validate language code format
    if not re.match(r'^[a-z]{2}$', code):
        return jsonify({'error': 'Invalid language code'}), 400
    
    lang_file = LANG_DIR / f'{code}.json'
    # ... rest of handler
```

- [ ] **Step 3: Commit**

```bash
git add server/routes.py
git commit -m "fix: validate language code format to prevent path traversal"
```

---

## Execution Order

1. **Phase 1 (Critical):** Tasks 1-3 — Fix immediately, deploy
2. **Phase 2 (High):** Tasks 4-8 — Fix within 1 day
3. **Phase 3 (Medium):** Tasks 9-13 — Fix within 1 week

## Testing Strategy

- **Phase 1:** Manual testing of affected features
- **Phase 2:** Verify no regressions in existing functionality
- **Phase 3:** Code review + manual testing

## Version Bumps

After completing each phase, bump `CONFIG_VERSION` and `?v=` in `index.html`:

```bash
# After Phase 1
# Update CONFIG_VERSION in core/state.py: X.Y.Z → X.Y.Z+1
# Update ?v=X.Y.Z in templates/index.html
```

# Fan Dead Zone Detection + Lambda Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dead zone detection during calibration, manual PWM offset sliders, and lambda curve shape parameter to each fan.

**Architecture:** Extend calibration to detect min_pwm/max_pwm boundaries. Store offset + lambda per-fan in config. Apply in control loop's pwm_from_curve(). UI: sliders in inspector panel with tooltips.

**Tech Stack:** Python (Flask, SocketIO), vanilla JS, Tailwind CSS, JSON config.

---

### Task 1: Extend Calibration Steps

**Covers:** [S3]

**Files:**
- Modify: `core/hardware.py:20`

- [ ] **Step 1: Update CALIBRATION_STEPS**

```python
# In core/hardware.py, line 20, replace:
CALIBRATION_STEPS = [0, 26, 51, 77, 102, 128, 153, 179, 204, 230, 255]
# With:
CALIBRATION_STEPS = [
    0, 13, 26, 38, 51, 64, 77, 89, 102, 115, 128,
    140, 153, 166, 179, 191, 204, 217, 230, 242, 255
]
```

- [ ] **Step 2: Commit**

```bash
git add core/hardware.py
git commit -m "feat: extend calibration to 21 steps for finer dead zone detection"
```

---

### Task 2: Add Dead Zone Detection

**Covers:** [S3]

**Files:**
- Modify: `core/calibration.py:15-63` (add new function after `_normalize_curve`)

- [ ] **Step 1: Add _detect_dead_zones function**

Add after the `_normalize_curve` function (after line 63):

```python
def _detect_dead_zones(raw: List[Dict], max_rpm: int) -> tuple:
    """
    Detect min_pwm (fan start) and max_pwm (saturation) from calibration data.
    Returns (min_pwm, max_pwm).
    """
    if not raw or max_rpm == 0:
        return 0, 255

    min_threshold = max_rpm * 0.05

    min_pwm = 0
    for pt in raw:
        if pt['rpm'] > min_threshold:
            min_pwm = pt['pwm']
            break

    max_pwm = 255
    for i in range(len(raw) - 1, 0, -1):
        if raw[i]['rpm'] > raw[i - 1]['rpm'] * 1.01:
            max_pwm = raw[i]['pwm']
            break

    logger.info(f'Dead zones: min_pwm={min_pwm}, max_pwm={max_pwm}')
    return min_pwm, max_pwm
```

- [ ] **Step 2: Commit**

```bash
git add core/calibration.py
git commit -m "feat: add dead zone detection (min_pwm, max_pwm) to calibration"
```

---

### Task 3: Store New Calibration Data

**Covers:** [S3, S7]

**Files:**
- Modify: `core/calibration.py:164-233` (update per-fan calibration result)

- [ ] **Step 1: Update calibration result storage**

In `test_fans()`, after the line `fan['curve'] = _normalize_curve(raw, is_inverted)` (line 205), add dead zone detection and store results.

Replace lines 207-224 with:

```python
            min_threshold = max_rpm * 0.05

            real_min = next(
                (pt for pt in fan['curve'] if pt['rpm'] > min_threshold),
                fan['curve'][0]
            )
            cal_min_pct = real_min['pct']

            detected_min_pwm, detected_max_pwm = _detect_dead_zones(raw, max_rpm)

            existing_cal = state.get('fans', {}).get(k, {}).get('calibration', {})
            fan.update({
                'min_rpm': real_min['rpm'],
                'max_rpm': max_rpm,
                'calibration': {
                    'min_rpm': real_min['rpm'],
                    'max_rpm': max_rpm,
                    'min_pct': cal_min_pct,
                    'inverted': fan['inverted'],
                    'min_pwm': existing_cal.get('min_pwm', detected_min_pwm),
                    'max_pwm': existing_cal.get('max_pwm', detected_max_pwm),
                    'lambda': existing_cal.get('lambda', 1.0),
                }
            })
```

- [ ] **Step 2: Commit**

```bash
git add core/calibration.py
git commit -m "feat: store min_pwm, max_pwm, lambda in calibration data"
```

---

### Task 4: Apply Offset + Lambda in Control Loop

**Covers:** [S4, S5]

**Files:**
- Modify: `core/control.py` (find `pwm_from_curve` function)

- [ ] **Step 1: Find and update pwm_from_curve**

First, find the function:

```bash
grep -n "def pwm_from_curve" core/control.py
```

Read the function to understand its current logic. Then add offset + lambda application at the end, before the return statement.

The function currently calculates a `raw_pwm` value (0-255). Add after that calculation, before the return:

```python
    cal = fan.get('calibration', {})
    min_pwm = cal.get('min_pwm', 0)
    max_pwm = cal.get('max_pwm', 255)
    lam = cal.get('lambda', 1.0)

    if lam != 1.0 and max_pwm > min_pwm:
        normalized = (raw_pwm - min_pwm) / (max_pwm - min_pwm) if max_pwm > min_pwm else 0
        normalized = max(0.0, min(1.0, normalized))
        normalized = normalized ** lam
        raw_pwm = normalized * (max_pwm - min_pwm) + min_pwm

    raw_pwm = max(min_pwm, min(max_pwm, int(raw_pwm)))
```

- [ ] **Step 2: Commit**

```bash
git add core/control.py
git commit -m "feat: apply PWM offset and lambda curve in control loop"
```

---

### Task 5: Add Inspector Sliders + Tooltips (HTML)

**Covers:** [S4, S5]

**Files:**
- Modify: `templates/index.html` (inspector panel section)

- [ ] **Step 1: Find inspector panel**

```bash
grep -n "inspector-content\|fan-name\|pwm-slider\|manual-controls" templates/index.html
```

Read the inspector section to find where to add the new controls.

- [ ] **Step 2: Add PWM Range sliders**

After the existing manual controls (pwm-slider section), add:

```html
<!-- PWM Range -->
<div class="mt-4">
    <label class="text-xs text-gray-400 flex items-center gap-1">
        PWM Range
        <span class="relative group">
            <span class="text-gray-500 cursor-help">&#x24D8;</span>
            <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-56 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50">
                Dead zone boundaries. Min = lowest PWM where fan spins. Max = PWM where fan reaches full speed. 0-100% slider maps only to this range.
            </span>
        </span>
    </label>
    <div class="flex items-center gap-2 mt-1">
        <span class="text-xs text-gray-500 w-8">Min</span>
        <input id="cal-min-pwm" type="range" min="0" max="255" value="0"
               class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"
               oninput="updateCalibrationParam('min_pwm', this.value)">
        <span id="cal-min-pwm-val" class="text-xs text-gray-400 w-8 text-right">0</span>
    </div>
    <div class="flex items-center gap-2 mt-1">
        <span class="text-xs text-gray-500 w-8">Max</span>
        <input id="cal-max-pwm" type="range" min="0" max="255" value="255"
               class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"
               oninput="updateCalibrationParam('max_pwm', this.value)">
        <span id="cal-max-pwm-val" class="text-xs text-gray-400 w-8 text-right">255</span>
    </div>
</div>

<!-- Lambda -->
<div class="mt-4">
    <label class="text-xs text-gray-400 flex items-center gap-1">
        Curve Shape
        <span class="relative group">
            <span class="text-gray-500 cursor-help">&#x24D8;</span>
            <span class="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 w-56 p-2 bg-gray-900 border border-gray-600 rounded-lg text-xs text-gray-300 hidden group-hover:block z-50">
                Controls fan response curve. 1.0 = linear. Lower = fan ramps up faster at low %. Higher = fan stays quiet longer, ramps up near 100%.
            </span>
        </span>
    </label>
    <div class="flex items-center gap-2 mt-1">
        <input id="cal-lambda" type="range" min="3" max="30" value="10"
               class="flex-1 h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500"
               oninput="updateCalibrationParam('lambda', this.value / 10)">
        <span id="cal-lambda-val" class="text-xs text-gray-400 w-10 text-right">1.0</span>
    </div>
</div>
```

- [ ] **Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat: add PWM range sliders and lambda slider with tooltips to inspector"
```

---

### Task 6: Wire Sliders in JavaScript

**Covers:** [S4, S5]

**Files:**
- Modify: `templates/js/main.js` (inspector update function + save function)

- [ ] **Step 1: Find updateInspector function**

```bash
grep -n "function updateInspector" templates/js/main.js
```

Read the function to understand how it populates inspector fields.

- [ ] **Step 2: Add calibration param update function**

Add after the `updateInspector` function or near the other calibration functions:

```javascript
function updateCalibrationParam(param, value) {
    if (!currentFanId || !currentState || !currentState.fans) return;
    const fan = currentState.fans[currentFanId];
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

    saveFanCalibration(currentFanId, fan.calibration);
}

function saveFanCalibration(fanId, calibration) {
    fetch('/api/fan/' + fanId + '/calibration', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(calibration)
    }).catch(err => console.error('Save calibration error:', err));
}
```

- [ ] **Step 3: Update updateInspector to populate sliders**

In the `updateInspector` function, after populating existing fields, add:

```javascript
    // Calibration params
    const cal = fan.calibration || {};
    const minPwmEl = document.getElementById('cal-min-pwm');
    const maxPwmEl = document.getElementById('cal-max-pwm');
    const lambdaEl = document.getElementById('cal-lambda');
    if (minPwmEl) {
        minPwmEl.value = cal.min_pwm || 0;
        document.getElementById('cal-min-pwm-val').textContent = cal.min_pwm || 0;
    }
    if (maxPwmEl) {
        maxPwmEl.value = cal.max_pwm || 255;
        document.getElementById('cal-max-pwm-val').textContent = cal.max_pwm || 255;
    }
    if (lambdaEl) {
        lambdaEl.value = (cal.lambda || 1.0) * 10;
        document.getElementById('cal-lambda-val').textContent = (cal.lambda || 1.0).toFixed(1);
    }
```

- [ ] **Step 4: Commit**

```bash
git add templates/js/main.js
git commit -m "feat: wire calibration sliders to save per-fan offset and lambda"
```

---

### Task 7: Add Backend Endpoint for Calibration Save

**Covers:** [S6]

**Files:**
- Modify: `server/routes.py` (add new route)

- [ ] **Step 1: Add calibration save endpoint**

Find where other fan-related routes are defined (search for `/api/fan/`). Add:

```python
@routes.route('/api/fan/<fan_id>/calibration', methods=['POST'])
def api_fan_calibration(fan_id):
    """Save calibration params (min_pwm, max_pwm, lambda) for a fan."""
    from core.config import save_config
    data = request.get_json(silent=True) or {}

    with state_lock:
        if fan_id not in state.get('fans', {}):
            return jsonify({'error': 'Fan not found'}), 404

        fan = state['fans'][fan_id]
        if 'calibration' not in fan:
            fan['calibration'] = {}

        for key in ('min_pwm', 'max_pwm', 'lambda'):
            if key in data:
                fan['calibration'][key] = data[key]

    save_config()
    return jsonify({'status': 'saved'})
```

- [ ] **Step 2: Commit**

```bash
git add server/routes.py
git commit -m "feat: add POST /api/fan/<id>/calibration endpoint"
```

---

### Task 8: Add Translations

**Covers:** [S4, S5]

**Files:**
- Modify: `static/lang/en.json`
- Modify: `static/lang/ru.json`

- [ ] **Step 1: Add English translations**

Add to `en.json`:

```json
"calibration.pwm_range": "PWM Range",
"calibration.pwm_range_hint": "Dead zone boundaries. Min = lowest PWM where fan spins. Max = PWM where fan reaches full speed.",
"calibration.min_pwm": "Min",
"calibration.max_pwm": "Max",
"calibration.curve_shape": "Curve Shape",
"calibration.lambda_hint": "Controls fan response curve. 1.0 = linear. Lower = fan ramps up faster at low %. Higher = fan stays quiet longer.",
"calibration.lambda_linear": "Linear"
```

- [ ] **Step 2: Add Russian translations**

Add to `ru.json`:

```json
"calibration.pwm_range": "Диапазон ШИМ",
"calibration.pwm_range_hint": "Границы мёртвых зон. Мин = минимальный ШИМ при котором вентилятор крутится. Макс = ШИМ при котором вентилятор достигает максимума.",
"calibration.min_pwm": "Мин",
"calibration.max_pwm": "Макс",
"calibration.curve_shape": "Форма кривой",
"calibration.lambda_hint": "Управляет формой кривой вентилятора. 1.0 = линейно. Меньше = вентилятор быстрее набирает обороты на низких %. Больше = вентилятор дольше тихий.",
"calibration.lambda_linear": "Линейно"
```

- [ ] **Step 3: Commit**

```bash
git add static/lang/en.json static/lang/ru.json
git commit -m "i18n: add translations for PWM range and lambda UI"
```

---

### Task 9: Verify Config Persistence

**Covers:** [S7]

**Files:**
- Verify: `core/config.py` (FAN_FIELDS list)

- [ ] **Step 1: Check FAN_FIELDS includes calibration**

```bash
grep -n "FAN_FIELDS\|calibration" core/config.py
```

The `calibration` field should already be in `FAN_FIELDS` (it's used for min_rpm/max_rpm). Verify that the new keys (min_pwm, max_pwm, lambda) are saved as part of the calibration dict.

- [ ] **Step 2: Test by reading a saved config**

After running on NAS, check that config.json contains:

```json
{
  "fans": {
    "dev-xxx": {
      "calibration": {
        "min_rpm": 800,
        "max_rpm": 3200,
        "min_pct": 20,
        "inverted": false,
        "min_pwm": 51,
        "max_pwm": 204,
        "lambda": 1.0
      }
    }
  }
}
```

- [ ] **Step 3: Commit (if any changes needed)**

```bash
git add core/config.py
git commit -m "fix: ensure calibration dict fields persist in config"
```

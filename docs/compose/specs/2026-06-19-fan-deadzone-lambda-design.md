# Fan Calibration Dead Zone Detection + Lambda

## [S1] Problem

Current calibration detects min_rpm/max_rpm but not the PWM boundaries where the fan starts and saturates. This means:
- Dead zones (PWM 0-50 where fan doesn't spin) waste the 0-100% slider range
- Users can't fine-tune how the fan responds to percentage changes
- Inverted fans may have non-linear response that's not compensated

## [S2] Solution Overview

Two new per-fan calibration parameters:

1. **PWM offset (min_pwm, max_pwm)** — detected during calibration, adjustable via sliders. Cuts dead zones so 0-100% maps only to the active range.
2. **Lambda** — power curve shape parameter. Controls how fan speed ramps across the active range. Stored per-fan in config.

## [S3] Extended Calibration

### Changes to `core/hardware.py`

Add fine-grained steps around boundaries:

```python
CALIBRATION_STEPS = [
    0, 13, 26, 38, 51, 64, 77, 89, 102, 115, 128,
    140, 153, 166, 179, 191, 204, 217, 230, 242, 255
]
```

21 steps (was 11). Same 5s settle time. Total calibration time: ~105s (was ~55s).

### Changes to `core/calibration.py`

After collecting raw data, detect boundaries:

```python
def _detect_dead_zones(raw, max_rpm):
    """Find min_pwm (fan start) and max_pwm (saturation)."""
    min_threshold = max_rpm * 0.05  # 5% of max RPM = "fan is spinning"
    
    # min_pwm: first PWM where RPM > threshold
    min_pwm = 0
    for pt in raw:
        if pt['rpm'] > min_threshold:
            min_pwm = pt['pwm']
            break
    
    # max_pwm: last PWM where RPM increase > 1% from previous
    max_pwm = 255
    for i in range(len(raw) - 1, 0, -1):
        if raw[i]['rpm'] > raw[i-1]['rpm'] * 1.01:
            max_pwm = raw[i]['pwm']
            break
    
    return min_pwm, max_pwm
```

Store in calibration data:
```python
fan['calibration'] = {
    'min_rpm': real_min['rpm'],
    'max_rpm': max_rpm,
    'min_pct': cal_min_pct,
    'inverted': fan['inverted'],
    'min_pwm': detected_min_pwm,  # NEW
    'max_pwm': detected_max_pwm,  # NEW
    'lambda': 1.0,                # NEW
}
```

## [S4] Manual PWM Offset

### UI (Inspector panel)

When a fan is selected, show in the inspector:

```
PWM Range
├─ Min PWM: [====●------] 51  (detected: 51)
├─ Max PWM: [========●--] 204 (detected: 204)
└─ Active range: 51-204 (was 0-255)
```

Sliders:
- **Min PWM**: 0-255, default = detected value
- **Max PWM**: 0-255, default = detected value
- Must satisfy: min_pwm < max_pwm
- Changing either slider recalculates the effective range

### Control loop changes (`core/control.py`)

In `pwm_from_curve()`, apply offset:

```python
def pwm_from_curve(fan, target_temp):
    # ... existing curve calculation ...
    raw_pwm = interpolated_pwm  # 0-255 from curve
    
    # Apply offset: map [0, 255] → [min_pwm, max_pwm]
    min_pwm = fan.get('calibration', {}).get('min_pwm', 0)
    max_pwm = fan.get('calibration', {}).get('max_pwm', 255)
    
    # Apply lambda
    lam = fan.get('calibration', {}).get('lambda', 1.0)
    normalized = raw_pwm / 255.0
    normalized = normalized ** lam
    raw_pwm = normalized * (max_pwm - min_pwm) + min_pwm
    
    return int(max(min_pwm, min(max_pwm, raw_pwm)))
```

## [S5] Lambda Parameter

### UI (Inspector panel)

```
Curve Shape (Lambda)
├─ [========●------] 1.0
├─ Tooltip: "Controls fan response curve. Left = aggressive at low speeds, Right = conservative"
```

Slider:
- Range: 0.3 - 3.0
- Default: 1.0
- Step: 0.1
- Tooltip on hover explaining purpose

### Effect on fan behavior

| lambda | 25% setting | 50% setting | 75% setting |
|--------|------------|------------|------------|
| 0.5    | PWM 104    | PWM 128    | PWM 166    |
| 1.0    | PWM 89     | PWM 128    | PWM 166    |
| 2.0    | PWM 104    | PWM 153    | PWM 185    |

(lambda < 1 = fan ramps up faster at low percentages)

## [S6] Data Flow

1. Calibration runs → detects min_pwm, max_pwm, stores in `fan['calibration']`
2. User adjusts sliders → saved to `fan['calibration']['min_pwm']`, `['max_pwm']`, `['lambda']`
3. `save_config()` persists to `/data/config.json`
4. Control loop reads calibration data → applies offset + lambda → sets PWM

## [S7] Config Format Change

Add to `FAN_FIELDS` in `core/config.py`:
```python
FAN_FIELDS = [
    # ... existing fields ...
    'calibration',  # already exists, now includes min_pwm, max_pwm, lambda
]
```

No schema migration needed — `calibration` is already a dict field. New keys are optional with defaults.

## [S8] Files to Modify

| File | Change |
|------|--------|
| `core/hardware.py` | Extend CALIBRATION_STEPS to 21 steps |
| `core/calibration.py` | Add `_detect_dead_zones()`, store min_pwm/max_pwm/lambda |
| `core/control.py` | Apply offset + lambda in `pwm_from_curve()` |
| `templates/index.html` | Add offset sliders + lambda slider to inspector panel |
| `templates/js/main.js` | Wire sliders to save calibration data |
| `static/lang/en.json` | Add translations for new UI elements |
| `static/lang/ru.json` | Add translations for new UI elements |

## [S9] Testing

1. Run calibration on NAS → verify min_pwm/max_pwm detected correctly
2. Adjust offset sliders → verify fan responds in new range
3. Adjust lambda → verify curve shape changes
4. Save config → restart → verify settings persist
5. Test inverted fan → verify offset works after inversion detection

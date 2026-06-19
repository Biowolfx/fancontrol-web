"""Fan calibration — PWM/RPM curve detection and inversion handling."""

import copy
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.state import state, state_lock, get_state
from core.hardware import set_pwm, CALIBRATION_STEPS, CALIBRATION_SETTLE_TIME, executor

logger = logging.getLogger('fancontrol')


def _detect_inversion(raw: List[Dict], fan_label: str) -> bool:
    """
    Detect if a fan has inverted PWM control using 3 methods.
    Returns True if inverted.
    """
    all_rpm = [pt['rpm'] for pt in raw]
    max_rpm = max(all_rpm)

    low_rpm_avg = sum(pt['rpm'] for pt in raw[:3]) / 3 if raw[:3] else 0
    high_rpm_avg = sum(pt['rpm'] for pt in raw[-3:]) / 3 if raw[-3:] else 0

    rpm_at_zero = raw[0]['rpm'] if raw else 0
    rpm_at_max = raw[-1]['rpm'] if raw else 0

    half = len(raw) // 2
    first_half_avg = sum(pt['rpm'] for pt in raw[:half]) / max(half, 1)
    second_half_avg = sum(pt['rpm'] for pt in raw[half:]) / max(len(raw) - half, 1)

    logger.info(
        f'Fan {fan_label} inversion check: '
        f'rpm@PWM0={rpm_at_zero}, rpm@PWM255={rpm_at_max}, '
        f'low3_avg={low_rpm_avg:.0f}, high3_avg={high_rpm_avg:.0f}, '
        f'first_half_avg={first_half_avg:.0f}, second_half_avg={second_half_avg:.0f}'
    )

    is_inverted = False
    if rpm_at_max > 0 and rpm_at_zero > rpm_at_max * 1.1:
        is_inverted = True
    elif high_rpm_avg > 0 and low_rpm_avg > high_rpm_avg * 1.1:
        is_inverted = True
    elif second_half_avg > 0 and first_half_avg > second_half_avg * 1.1:
        is_inverted = True

    logger.info(f'Fan {fan_label} inversion result: {"INVERTED" if is_inverted else "normal"}')
    return is_inverted


def _normalize_curve(raw: List[Dict], is_inverted: bool) -> List[Dict]:
    """
    Normalize calibration curve. If inverted, remap PWM so low pct = low RPM.
    Returns sorted curve.
    """
    if is_inverted:
        return sorted(
            [{'pwm': 255 - pt['pwm'], 'rpm': pt['rpm'], 'pct': 100 - pt['pct']}
             for pt in raw],
            key=lambda x: x['pwm']
        )
    return sorted(raw, key=lambda x: x['pwm'])


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


def test_fans(fan_key: Optional[str] = None, socketio=None, save_config_fn=None):
    """
    Calibrate fans by testing PWM/RPM curve.
    Uses parallel testing for efficiency.
    socketio: needed for emit during calibration.
    save_config_fn: callable to persist config on success.
    """
    test_successful = True

    with state_lock:
        if fan_key:
            if fan_key not in state['fans']:
                raise ValueError(f'Fan key not found: {fan_key}')
            fans_to_test = {fan_key: copy.deepcopy(state['fans'][fan_key])}
        else:
            fans_to_test = {
                k: copy.deepcopy(v)
                for k, v in state['fans'].items()
            }

    writable_fans = {k: f for k, f in fans_to_test.items() if f.get('writable')}

    if not writable_fans:
        logger.warning('No writable fans found for calibration')
        return

    for f in writable_fans.values():
        f['inverted'] = False
        f['status'] = 'calibrating'

    with state_lock:
        state['testing'] = True
        state['_pause_loop'] = True
        state['test_progress'] = {
            'status': 'Starting parallel calibration...',
            'step': 0,
            'total': len(CALIBRATION_STEPS),
            'current': 'All fans'
        }

    if socketio:
        socketio.emit('test_progress', state['test_progress'])

    pwm_steps = CALIBRATION_STEPS
    raw_data = {k: [] for k in writable_fans}

    try:
        for step_idx, pwm_value in enumerate(pwm_steps):
            pct = round(pwm_value * 100 / 255)

            with state_lock:
                state['test_progress'].update({
                    'step': step_idx + 1,
                    'status': f'Testing level {pct}%',
                    'current': 'Parallel mode'
                })
            if socketio:
                socketio.emit('test_progress', state['test_progress'])

            for k in writable_fans:
                with state_lock:
                    set_pwm(k, pwm_value, raw=True)

            time.sleep(CALIBRATION_SETTLE_TIME)

            def read_rpm(item):
                key, fan = item
                try:
                    rpm = int(Path(fan['fan_path']).read_text().strip())
                except Exception:
                    rpm = 0
                return key, rpm

            futures = [
                executor.submit(read_rpm, (k, f))
                for k, f in writable_fans.items()
            ]

            for future in futures:
                try:
                    key, rpm = future.result(timeout=2)
                    raw_data[key].append({
                        'pwm': pwm_value,
                        'rpm': rpm,
                        'pct': pct
                    })

                    if rpm is not None:
                        with state_lock:
                            if key in state['fans']:
                                state['fans'][key]['rpm'] = rpm

                except Exception as ex:
                    logger.error(f'RPM read error: {ex}')

            if socketio:
                socketio.emit('update', get_state())

        for k, fan in writable_fans.items():
            raw = raw_data.get(k, [])

            if not raw:
                logger.warning(f'Fan {fan.get("label", k)} ({k}): No RPM data')
                fan.update({
                    'status': 'not_connected',
                    'min_rpm': 0,
                    'max_rpm': 0,
                    'curve': [],
                    'calibration': {}
                })
                with state_lock:
                    set_pwm(k, 128, raw=True)
                continue

            all_rpm = [pt['rpm'] for pt in raw]
            max_rpm = max(all_rpm)

            if max_rpm == 0:
                logger.warning(f"Fan {fan.get('label', k)} ({k}): No RPM detected")
                fan.update({
                    'status': 'not_connected',
                    'min_rpm': 0,
                    'max_rpm': 0,
                    'curve': [],
                    'calibration': {}
                })
                with state_lock:
                    set_pwm(k, 128, raw=True)
                continue

            is_inverted = _detect_inversion(raw, fan.get('label', k))

            if is_inverted:
                fan['inverted'] = True
                fan['status'] = 'inverted'
            else:
                fan['inverted'] = False
                fan['status'] = 'normal'

            fan['curve'] = _normalize_curve(raw, is_inverted)

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

            set_pwm(k, 128)
            fan['manual_pct'] = 50
            fan['current_pct'] = 50
            fan['target_pwm'] = 50
            logger.info(
                f'Fan {fan.get("label", k)}: status={fan["status"]}, '
                f'min={fan["min_rpm"]}rpm, max={fan["max_rpm"]}rpm'
            )

        with state_lock:
            for k, fan in writable_fans.items():
                if k in state['fans']:
                    state['fans'][k].update(fan)

    except Exception as e:
        logger.error(f'Test error: {e}', exc_info=True)
        test_successful = False

    finally:
        with state_lock:
            state['testing'] = False
            state['tested'] = test_successful

            if test_successful:
                state['initialized'] = True
                if save_config_fn:
                    save_config_fn()
                logger.info('System initialized successfully')

            state['_pause_loop'] = False
            state['test_progress'] = {
                'status': 'Ready!' if test_successful else 'Completed with errors',
                'step': 0,
                'total': 0,
                'current': ''
            }
            current_initialized = state['initialized']

        if socketio:
            socketio.emit('test_progress', state['test_progress'])
            socketio.emit('test_complete', {
                'success': test_successful,
                'initialized': current_initialized
            })

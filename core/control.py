"""Control loop — fan temperature evaluation, PWM calculation, and main loop."""

import logging
import sqlite3
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

from core.state import state, state_lock, get_state
from core.hardware import (
    CALIBRATION_STEPS, CALIBRATION_SETTLE_TIME,
    executor,
    read_disk_temp, calculate_disk_health,
    set_pwm, refresh,
)
from core.config import DATA_DIR, DB_FILE

logger = logging.getLogger('fancontrol')

SENSOR_FAILURE_TEMP = 99

MIN_PWM_PCT = 20
MAX_PWM_PCT = 100

CONTROL_LOOP_INTERVAL = 5
UNINITIALIZED_POLL_INTERVAL = 10
TELEMETRY_LOG_INTERVAL = 300
LOG_CLEANUP_INTERVAL = 86400
DISK_POLL_COOLDOWN = 15


_db_local = threading.local()


def get_db_connection() -> sqlite3.Connection:
    """Thread-local persistent SQLite connection with WAL mode."""
    conn = getattr(_db_local, 'conn', None)
    if conn is not None:
        # Check if connection is still valid (detect stale connections from recycled threads)
        try:
            conn.execute('SELECT 1')
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _db_local.conn = None
            conn = None
    if conn is None:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA journal_size_limit=10485760')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=5000')
        _db_local.conn = conn
    return conn


def refresh_disks():
    """
    Update disk temperatures with health metrics - FULLY ATOMIC VERSION.
    Skips if disks_polling flag is set (concurrent discovery).
    All calculations inside single state_lock to prevent race conditions.
    """
    now = time.monotonic()
    
    with state_lock:
        if state.get('disks_polling'):
            return
        
        last_poll = state['last_hdd_poll']
        if last_poll > 0 and now - last_poll < DISK_POLL_COOLDOWN:
            return
        
        sensors_copy = {
            disk_id: info.copy()
            for disk_id, info in state['hdd_sensors'].items()
        }
    
    futures_map = {
        disk_id: executor.submit(read_disk_temp, info.get('dev_name', disk_id))
        for disk_id, info in sensors_copy.items()
    }
    
    updated_values = {}
    for disk_id, future in futures_map.items():
        try:
            result = future.result(timeout=3)
            temp, standby = result if isinstance(result, tuple) and len(result) == 2 else (None, False)
            
            if temp is not None:
                health = calculate_disk_health(temp)
                updated_values[disk_id] = {
                    'temp': temp,
                    'standby': standby,
                    'pct_fill': health['pct_fill'],
                    'color_zone': health['color_zone'],
                    'health_status': health['status']
                }
        except FutureTimeout:
            logger.debug(f'Timeout polling disk {disk_id}')
        except Exception as e:
            logger.error(f'Poll error for {disk_id}: {e}')
    
    # SINGLE ATOMIC BLOCK
    with state_lock:
        for disk_id, data in updated_values.items():
            if disk_id in state['hdd_sensors']:
                state['hdd_sensors'][disk_id].update(data)
        
        state['last_hdd_poll'] = time.monotonic()
        
        active_temps = [
            v['temp'] for v in state['hdd_sensors'].values()
            if v.get('temp', 0) > 0 and not v.get('standby')
        ]
        
        all_standby = (
            len(state['hdd_sensors']) > 0 and
            all(v.get('standby', False) for v in state['hdd_sensors'].values())
        )
        
        if active_temps:
            state['max_hdd_temp'] = max(active_temps)
            state['failsafe'] = False
            state['standby_mode'] = False
        elif all_standby:
            state['max_hdd_temp'] = 0
            state['failsafe'] = False
            state['standby_mode'] = True
        else:
            state['max_hdd_temp'] = 0
            state['failsafe'] = True
            state['standby_mode'] = False


def fan_temp(fan: Dict, override_sensors: Optional[List] = None, 
             override_sensor_mode: Optional[str] = None) -> float:
    """Calculate effective temperature for a fan based on assigned sensors.
    
    Reads sensor values under lock without copying entire dicts —
    only accesses the specific sensor_ids needed.
    """
    sensors = override_sensors if override_sensors is not None else fan.get('sensors', [])
    mode = override_sensor_mode if override_sensor_mode is not None else fan.get('sensor_mode', 'max')
    
    if not sensors:
        return SENSOR_FAILURE_TEMP
    
    temps = []
    
    with state_lock:
        for sensor_id in sensors:
            temp = _extract_sensor_temp(sensor_id)
            if temp > 0:
                temps.append(temp)
    
    if not temps:
        return SENSOR_FAILURE_TEMP
    
    if mode == 'max':
        return max(temps)
    elif mode == 'min':
        return min(temps)
    else:
        return sum(temps) / len(temps)


def _extract_sensor_temp(sensor_id: str) -> float:
    """Extract temperature for a single sensor_id. Must be called under state_lock."""
    if sensor_id.startswith('hdd:'):
        disk_id = sensor_id.split(':', 1)[1]
        return state['hdd_sensors'].get(disk_id, {}).get('temp', 0)
    if sensor_id.startswith('temp:'):
        temp_id = sensor_id.split(':', 1)[1]
        return state['temp_sensors'].get(temp_id, {}).get('value', 0)
    # Legacy format — try both
    if sensor_id in state['hdd_sensors']:
        return state['hdd_sensors'][sensor_id].get('temp', 0)
    if sensor_id in state['temp_sensors']:
        return state['temp_sensors'][sensor_id].get('value', 0)
    return 0


def pwm_from_curve(fan: Dict, target_pct: float) -> int:
    """
    Convert target percentage to PWM value using calibration curve.
    Applies dead zone offset and lambda curve shape.
    """
    curve = fan.get('curve', [])
    cal = fan.get('calibration', {})
    
    if not cal or len(curve) < 2:
        return int(target_pct * 255 // 100)
    
    target_pct = max(0.0, min(100.0, float(target_pct)))
    min_pct = float(cal.get('min_pct', 0))
    
    if 0.0 < target_pct < min_pct:
        target_pct = min_pct
    
    raw_pwm = None
    for i in range(len(curve) - 1):
        a, b = curve[i], curve[i + 1]
        if min(a['pct'], b['pct']) <= target_pct <= max(a['pct'], b['pct']):
            if a['pct'] == b['pct']:
                raw_pwm = a['pwm']
                break
            ratio = (target_pct - a['pct']) / (b['pct'] - a['pct'])
            raw_pwm = a['pwm'] + (b['pwm'] - a['pwm']) * ratio
            break
    
    if raw_pwm is None:
        raw_pwm = curve[-1]['pwm']

    min_pwm = cal.get('min_pwm', 0)
    max_pwm = cal.get('max_pwm', 255)
    lam = cal.get('lambda', 1.0)

    if lam != 1.0 and max_pwm > min_pwm:
        span = max_pwm - min_pwm
        normalized = (raw_pwm - min_pwm) / span
        normalized = max(0.0, min(1.0, normalized))
        normalized = normalized ** lam
        raw_pwm = normalized * span + min_pwm

    return max(min_pwm, min(max_pwm, int(round(raw_pwm))))


def process_auto_mode(fan_id: str, fan: Dict, current_temp: float,
                      target_temp: float, schedule_item: Optional[Dict] = None,
                      failsafe: bool = False, standby_mode: bool = False) -> Tuple[int, str]:
    """
    PURE FUNCTION: Calculates target PWM percentage and status.
    Does NOT write to hardware or modify state.
    
    Args:
        fan_id: Fan identifier (for logging only)
        fan: Fan configuration dictionary
        current_temp: Current effective temperature
        target_temp: Target temperature
        schedule_item: Current schedule rule if applied (optional)
        failsafe: Whether the system is in failsafe mode
        standby_mode: Whether disks are in standby
    
    Returns:
        (target_pct: int, status: str)
    """
    if failsafe:
        return MAX_PWM_PCT, 'failsafe'

    if standby_mode:
        status = 'standby'
        
        if schedule_item:
            sm = schedule_item.get('mode', 'auto')
            if sm == 'manual':
                target_pct = schedule_item.get('speed_pct', 50)
            elif sm == 'off':
                target_pct = 0
            else:
                sched_target = schedule_item.get('target_temp', target_temp)
                target_pct = max(MIN_PWM_PCT, min(40, 
                    sched_target - current_temp + 30))
        else:
            target_pct = 25
        
        return int(target_pct), status

    # 3. Temperature sensor failure - MAXIMUM COOLING for hardware safety!
    if current_temp >= SENSOR_FAILURE_TEMP:
        return MAX_PWM_PCT, 'critical'

    # 4. Normal operation (Proportional curve)
    delta = current_temp - target_temp
    
    if delta <= -2:
        target_pct = MIN_PWM_PCT
    elif delta >= 6:
        target_pct = MAX_PWM_PCT
    else:
        target_pct = MIN_PWM_PCT + (delta + 2) * (MAX_PWM_PCT - MIN_PWM_PCT) // 8

    status = 'warning' if delta > 4 else 'nominal'
    return int(target_pct), status


def _evaluate_fan_mode(fan_id: str, fan: Dict, sys_failsafe: bool, sys_standby: bool) -> Tuple[int, str]:
    """
    Evaluate target_pct and status for a fan based on its mode and schedule.
    Returns (target_pct, status).
    """
    mode = fan.get('mode', 'manual')
    
    if mode == 'manual':
        raw_pct = fan.get('manual_pct', 50)
        status = fan.get('status', 'nominal')
        if status not in ['nominal', 'warning', 'critical', 'standby', 'failsafe', 'inverted', 'no_sensor']:
            status = 'nominal'
        return raw_pct, status
    
    if mode != 'auto':
        return 50, 'nominal'
    
    schedule = fan.get('schedule', [])
    schedule_applied = False
    
    if schedule:
        now_dt = datetime.now()
        current_day = now_dt.strftime('%a').lower()
        current_time = now_dt.strftime('%H:%M')
        
        for item in schedule:
            day = item.get('day', 'all')
            if day == 'all':
                days = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
            elif day == 'weekday':
                days = ['mon', 'tue', 'wed', 'thu', 'fri']
            elif day == 'weekend':
                days = ['sat', 'sun']
            else:
                days = [day]
            
            if current_day in days and item['time_start'] <= current_time <= item['time_end']:
                schedule_applied = True
                sm = item.get('mode', 'auto')
                
                if sm == 'off':
                    return 0, 'off'
                elif sm == 'manual':
                    return item.get('speed_pct', 50), 'manual'
                else:
                    item_sensors = item.get('sensors') or fan.get('sensors', [])
                    item_sensor_mode = item.get('sensor_mode') or fan.get('sensor_mode', 'max')
                    current_temp = fan_temp(fan, item_sensors, item_sensor_mode)
                    target_temp = item.get('target_temp', fan.get('target_temp', 31))
                    target_pct, status = process_auto_mode(
                        fan_id, fan, current_temp, target_temp, item,
                        failsafe=sys_failsafe, standby_mode=sys_standby
                    )
                    if status == 'critical' and not item_sensors:
                        status = 'no_sensor'
                    return target_pct, status
                break
    
    if not schedule_applied:
        current_temp = fan_temp(fan)
        target_temp = fan.get('target_temp', 31)
        target_pct, status = process_auto_mode(
            fan_id, fan, current_temp, target_temp,
            failsafe=sys_failsafe, standby_mode=sys_standby
        )
        if status == 'critical' and not fan.get('sensors'):
            status = 'no_sensor'
        return target_pct, status
    
    return 50, 'nominal'


def log_telemetry():
    """Log telemetry data to SQLite.
    
    Reads fan/disk counts directly without state_lock — GIL protects
    dict iteration. Values may be 1 cycle stale, acceptable for logging.
    """
    try:
        with state_lock:
            fans = dict(state.get('fans', {}))
            disk_count = len(state.get('hdd_sensors', {}))
            max_temp = state.get('max_hdd_temp', 0)

        fan_count = len(fans)
        if fan_count > 0:
            avg_pwm = sum(
                f.get('raw_pwm', f.get('pwm_value', 0))
                for f in fans.values()
            ) // fan_count
            avg_rpm = sum(
                f.get('rpm', 0)
                for f in fans.values()
            ) // fan_count
        else:
            avg_pwm = 0
            avg_rpm = 0

        with get_db_connection() as conn:
            conn.execute(
                'INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    datetime.now().isoformat(),
                    'auto',
                    avg_pwm,
                    avg_rpm,
                    max_temp,
                    fan_count,
                    disk_count
                )
            )
            conn.commit()

    except sqlite3.OperationalError as e:
        logger.error(f'SQLite write error: {e}')


def cleanup_logs(retention_days: int = 30):
    """Remove old log entries"""
    try:
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with get_db_connection() as conn:
            conn.execute('DELETE FROM logs WHERE ts < ?', (cutoff,))
            conn.commit()
        logger.info(f'Cleaned logs older than {retention_days} days')
    except sqlite3.OperationalError as e:
        logger.error(f'Failed to cleanup logs: {e}')


def check_fan_health(socketio=None):
    """Detect fan slowdown (bearing wear) and stop, emit alerts on status changes."""
    now = time.time()
    changes = []

    with state_lock:
        fans = dict(state.get('fans', {}))
        for fan_id, fan in fans.items():
            if not fan.get('writable'):
                continue
            if fan.get('mode') == 'off':
                continue

            health = fan.setdefault('health', {
                'status': 'healthy',
                'rpm_baseline': 0,
                'slowdown_since': None,
                'stopped_since': None,
                'last_service_date': None,
                'calibration_required': False,
            })
            old_status = health.get('status', 'healthy')
            rpm = fan.get('rpm', 0) or 0
            pwm_from_pct = fan.get('current_pct', 0) or 0
            pwm_from_manual = fan.get('manual_pct', 0) or 0
            pwm_from_target = fan.get('target_pwm', 0) or 0
            pwm = max(pwm_from_pct, pwm_from_manual, pwm_from_target)
            cal = fan.get('calibration', {})
            max_rpm = cal.get('max_rpm', 0)
            new_status = old_status

            logger.debug(f'[health] {fan.get("label", fan_id)}: rpm={rpm} pwm={pwm} '
                         f'pct={pwm_from_pct} manual={pwm_from_manual} target={pwm_from_target} '
                         f'baseline={health.get("rpm_baseline", 0):.0f} status={old_status} '
                         f'stopped_since={health.get("stopped_since")}')

            should_be_spinning = pwm > 5 or health.get('rpm_baseline', 0) > 0
            if rpm < 10 and should_be_spinning:
                if health.get('stopped_since') is None:
                    health['stopped_since'] = now
                if now - health['stopped_since'] >= 10:
                    new_status = 'stopped'
            else:
                if health.get('stopped_since') is not None:
                    health['stopped_since'] = None
                if old_status == 'stopped':
                    new_status = 'healthy'

            if new_status != 'stopped' and rpm > 0 and pwm > 0:
                if max_rpm > 0:
                    expected_rpm = max_rpm * (pwm / 100)
                else:
                    if old_status in ('healthy', 'slowing'):
                        if health.get('rpm_baseline', 0) == 0:
                            health['rpm_baseline'] = rpm
                        else:
                            health['rpm_baseline'] = 0.9 * health['rpm_baseline'] + 0.1 * rpm
                    expected_rpm = health.get('rpm_baseline', 0)

                if expected_rpm > 0 and rpm < expected_rpm * 0.5:
                    if health.get('slowing_since') is None:
                        health['slowing_since'] = now
                    if now - health['slowing_since'] >= 15:
                        new_status = 'slowing'
                else:
                    if health.get('slowing_since') is not None:
                        health['slowing_since'] = None
                    if old_status == 'slowing':
                        new_status = 'healthy'

            if health.get('calibration_required') and new_status != 'stopped':
                new_status = 'needs_calibration'

            health['status'] = new_status

            if new_status != old_status:
                changes.append((fan_id, fan.get('label', fan_id), old_status, new_status))

    # Emit alerts outside of the lock
    if changes:
        # Telegram notifications
        tg_enabled = state.get('telegram_enabled', False)
        tg_events = state.get('telegram_events', {})
        tg_fan = tg_enabled and tg_events.get('fan_health', True)

        for fan_id, label, old_s, new_s in changes:
            if new_s == 'stopped':
                if socketio:
                    socketio.emit('fan:health', {
                        'fan_id': fan_id, 'node_id': 'local',
                        'status': 'stopped', 'label': label,
                        'message': f'Вентилятор {label} остановлен!',
                    })
                if tg_fan:
                    from core.telegram import send_message
                    send_message(f'⛔ <b>Вентилятор остановлен!</b>\n{label} ({fan_id})')
                logger.warning(f'Fan STOPPED: {label} ({fan_id})')
            elif new_s == 'slowing':
                if socketio:
                    socketio.emit('fan:health', {
                        'fan_id': fan_id, 'node_id': 'local',
                        'status': 'slowing', 'label': label,
                        'message': f'Вентилятор {label} замедляется (износ подшипника)',
                    })
                if tg_fan:
                    from core.telegram import send_message
                    send_message(f'⚠️ <b>Вентилятор замедляется</b>\n{label} — износ подшипника')
                logger.warning(f'Fan SLOWING: {label} ({fan_id})')
            elif new_s == 'needs_calibration':
                if socketio:
                    socketio.emit('fan:health', {
                        'fan_id': fan_id, 'node_id': 'local',
                        'status': 'needs_calibration', 'label': label,
                        'message': f'Вентилятор {label} требует калибровки',
                    })
                if tg_fan:
                    from core.telegram import send_message
                    send_message(f'🔧 <b>Требуется калибровка</b>\n{label}')
            elif new_s == 'healthy' and old_s in ('stopped', 'slowing', 'needs_calibration'):
                if socketio:
                    socketio.emit('fan:health:cleared', {
                        'fan_id': fan_id, 'node_id': 'local',
                    })
                if tg_fan:
                    from core.telegram import send_message
                    send_message(f'✅ <b>Вентилятор восстановлен</b>\n{label}')
                logger.info(f'Fan recovered: {label} ({fan_id}) → healthy')


_failed_calibration_logged = False
_last_cleanup = time.monotonic()


def loop(socketio=None):
    """
    Main control loop.
    Architecture: 
    - Pure functions calculate target_pct and status
    - Loop applies PWM and updates state centrally
    """
    global _failed_calibration_logged, _last_cleanup
    last_log = time.monotonic()
    
    while True:
        try:
            if state.get('testing'):
                refresh()
                refresh_disks()
                if socketio:
                    socketio.emit('update', get_state())
                time.sleep(2)
                continue
            
            if state.get('_pause_loop'):
                time.sleep(1)
                continue
            
            if not state.get('initialized'):
                if state.get('hardware_scanned'):
                    refresh()
                    refresh_disks()
                    if socketio:
                        socketio.emit('update', get_state())
                
                if not state.get('tested', True):
                    if not _failed_calibration_logged:
                        logger.warning("Calibration failed. Fix hardware and restart.")
                        _failed_calibration_logged = True
                    time.sleep(UNINITIALIZED_POLL_INTERVAL)
                    continue
                
                time.sleep(2)
                continue
            
            refresh()
            refresh_disks()

            check_fan_health(socketio)

            with state_lock:
                fans_snapshot = {k: v.copy() for k, v in state['fans'].items()}
                sys_failsafe = state.get('failsafe', False)
                sys_standby = state.get('standby_mode', False)
            
            updated_fans_metrics = {}
            
            for fan_id, fan in fans_snapshot.items():
                if not fan.get('writable'):
                    continue
                
                target_pct, status = _evaluate_fan_mode(fan_id, fan, sys_failsafe, sys_standby)
                mode = fan.get('mode', 'manual')
                
                if mode not in ('manual', 'auto'):
                    continue
                
                calculated_pwm = pwm_from_curve(fan, float(target_pct))
                set_pwm(fan_id, calculated_pwm)
                
                updated_fans_metrics[fan_id] = {
                    'current_pct': target_pct,
                    'target_pwm': target_pct,
                    'mode': mode,
                    'status': status,
                    'inverted': fan.get('inverted', False)
                }
            
            with state_lock:
                for fan_id, metrics in updated_fans_metrics.items():
                    if fan_id in state['fans']:
                        state['fans'][fan_id].update(metrics)
            
            if socketio:
                socketio.emit('update', get_state())
                try:
                    check_alerts(socketio)
                except Exception:
                    logger.debug('check_alerts raised', exc_info=True)
            
            current_time = time.monotonic()
            if current_time - last_log > TELEMETRY_LOG_INTERVAL:
                try:
                    log_telemetry()
                except Exception as e:
                    logger.error(f'Logging error: {e}')
                last_log = current_time
            
            if current_time - _last_cleanup > LOG_CLEANUP_INTERVAL:
                cleanup_logs(state.get('log_retention_days', 30))
                _last_cleanup = current_time
            
            time.sleep(CONTROL_LOOP_INTERVAL)
            
        except Exception as e:
            logger.error(f'Loop error: {e}', exc_info=True)
            time.sleep(CONTROL_LOOP_INTERVAL)


def _compare(val, cmp, threshold):
    try:
        if cmp == '>=':
            return val >= threshold
        if cmp == '>':
            return val > threshold
        if cmp == '<=':
            return val <= threshold
        if cmp == '<':
            return val < threshold
        return False
    except Exception:
        return False


def check_alerts(socketio):
    """Evaluate registered alerts in `state['alerts']` and emit 'alert' events."""
    with state_lock:
        alerts = dict(state.get('alerts', {}))
        fired = state.setdefault('_alerts_fired', {})
        fans = dict(state.get('fans', {}))
        max_temp = state.get('max_hdd_temp', 0)
        hdds = dict(state.get('hdd_sensors', {}))

    for key, a in alerts.items():
        a_type = a.get('type')
        target = a.get('target')
        threshold = a.get('threshold')
        cmp = a.get('cmp', '>=')
        level = a.get('level', 'warning')
        message = a.get('message') or f'Alert {key}'

        if threshold is None:
            continue

        value = None
        if a_type == 'fan' and target:
            f = fans.get(target)
            if f:
                value = f.get('rpm') or f.get('current_pct') or 0
        elif a_type in ('overview', 'max_temp'):
            value = max_temp
        elif a_type == 'disk' and target:
            d = hdds.get(target)
            if d:
                value = d.get('temp')
        else:
            with state_lock:
                value = state.get(target)

        if value is None:
            continue

        triggered = _compare(value, cmp, threshold)
        previously = fired.get(key, False)

        if triggered and not previously:
            payload = {
                'key': key,
                'level': level,
                'message': message,
                'value': value,
                'threshold': threshold
            }
            socketio.emit('alert', payload)
            with state_lock:
                fired[key] = True
        elif not triggered and previously:
            payload = {'key': key, 'cleared': True}
            socketio.emit('alert:cleared', payload)
            with state_lock:
                fired[key] = False

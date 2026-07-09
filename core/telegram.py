"""Telegram notifications — send alerts via Bot API.

Zero new dependencies — uses urllib.request (stdlib).
Rate-limited to 1 msg/sec to avoid Telegram flood limits.
"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error

logger = logging.getLogger('fancontrol')

_api_url = 'https://api.telegram.org/bot{token}/sendMessage'
_bot_token = ''
_chat_id = ''
_last_send = 0.0
_lock = threading.Lock()
_min_interval = 1.0


def configure(bot_token, chat_id):
    """Set bot token and chat ID at runtime."""
    global _bot_token, _chat_id
    _bot_token = (bot_token or '').strip()
    _chat_id = (chat_id or '').strip()
    if _bot_token and _chat_id:
        logger.info(f'[TG] Configured: chat_id={_chat_id}')
    else:
        logger.info('[TG] Not configured (missing token or chat_id)')


def is_configured():
    """Check if Telegram is properly configured."""
    return bool(_bot_token and _chat_id)


def send_message(text, parse_mode='HTML'):
    """Send message to Telegram. Thread-safe, rate-limited.

    Returns True on success, False on failure.
    """
    global _last_send
    if not is_configured():
        return False

    with _lock:
        elapsed = time.time() - _last_send
        if elapsed < _min_interval:
            time.sleep(_min_interval - elapsed)
        _last_send = time.time()

    try:
        url = _api_url.format(token=_bot_token)
        payload = json.dumps({
            'chat_id': _chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': True,
        }).encode('utf-8')
        req = urllib.request.Request(
            url, data=payload,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get('ok'):
                logger.info(f'[TG] Sent: {text[:80]}...')
                return True
            else:
                error_code = result.get('error_code', '?')
                description = result.get('description', '')
                logger.warning(f'[TG] API error {error_code}: {description}')
                return False
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='ignore')[:200]
        logger.error(f'[TG] HTTP {e.code}: {body}')
        return False
    except Exception as e:
        logger.error(f'[TG] Send failed: {e}')
        return False

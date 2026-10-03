"""Telegram delivery; record state only after confirmed success."""
import logging
import os
import re
import time
import requests

_last_send = None


def _safe_description(value, token, chat):
    text = str(value).replace(token, '[TOKEN]').replace(chat, '[CHAT]')
    text = re.sub(r'https?://\S+', '[URL]', text)
    return text[:400]


def send_message(text):
    global _last_send
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise RuntimeError('Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID или используйте --dry-run')
    for attempt in range(3):
        if _last_send is not None:
            time.sleep(max(0, 1.1 - (time.monotonic() - _last_send)))
        try:
            response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                data=dict(chat_id=chat, text=text, parse_mode='HTML', disable_web_page_preview='true'), timeout=30)
        except requests.RequestException as exc:
            # A timeout can occur after acceptance: do not blindly resend a message.
            raise RuntimeError(f'Telegram: сетевая ошибка {type(exc).__name__}; подтверждение доставки не получено') from None
        finally:
            _last_send = time.monotonic()
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f'Telegram HTTP {response.status_code}: ответ не в формате JSON') from None
        if not isinstance(payload, dict):
            raise RuntimeError(f'Telegram HTTP {response.status_code}: некорректный ответ')
        if response.ok and payload.get('ok') is True:
            return
        code = payload.get('error_code', response.status_code)
        description = _safe_description(payload.get('description', 'отправка не подтверждена'), token, chat)
        if code == 429 and attempt < 2:
            delay = payload.get('parameters', {}).get('retry_after')
            if type(delay) is int and 0 <= delay <= 30:
                logging.warning('Telegram: лимит частоты; повтор через %d с', delay + 1)
                time.sleep(delay + 1)
                continue
        raise RuntimeError(f'Telegram HTTP {response.status_code}, код {code}: {description}')

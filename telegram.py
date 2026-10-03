"""Telegram delivery; callers record state only after confirmed success."""
import os
import requests


def send_message(text):
    token, chat = os.environ.get('TELEGRAM_BOT_TOKEN'), os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        raise RuntimeError('Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID или используйте --dry-run')
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
            data=dict(chat_id=chat, text=text, parse_mode='HTML', disable_web_page_preview='true'), timeout=30)
        response.raise_for_status()
        if response.json().get('ok') is not True:
            raise RuntimeError('Telegram не подтвердил отправку')
    except (requests.RequestException, ValueError):
        # HTTP exceptions contain the request URL, which includes the secret token.
        raise RuntimeError('Ошибка отправки Telegram; проверьте токен, chat_id и доступность API') from None

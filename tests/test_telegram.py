import os
import unittest
from unittest.mock import Mock, patch
import requests
import telegram


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, TELEGRAM_BOT_TOKEN='secret-token', TELEGRAM_CHAT_ID='123456')
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sleep = patch('telegram.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        telegram._last_send = None

    def response(self, status, payload):
        return Mock(status_code=status, ok=status < 400, json=Mock(return_value=payload))

    def test_error_includes_description_without_secrets(self):
        r = self.response(400, dict(ok=False, error_code=400,
            description="Bad Request: can't parse entities secret-token 123456 https://api.telegram.org/botsecret-token/sendMessage"))
        with patch('telegram.requests.post', return_value=r), self.assertRaises(RuntimeError) as error:
            telegram.send_message('<broken>')
        self.assertIn("can't parse entities", str(error.exception))
        self.assertIn('400', str(error.exception))
        self.assertNotIn('secret-token', str(error.exception))
        self.assertNotIn('123456', str(error.exception))

    def test_rate_limit_retries_after_server_delay(self):
        responses = [self.response(429, dict(ok=False, error_code=429, parameters=dict(retry_after=2))),
                     self.response(200, dict(ok=True))]
        with patch('telegram.requests.post', side_effect=responses) as post, patch('telegram.time.sleep') as sleep:
            telegram.send_message('test')
        self.assertEqual(post.call_count, 2)
        sleep.assert_any_call(3)

    def test_timeout_not_retried_and_no_url_leak(self):
        with patch('telegram.requests.post', side_effect=requests.Timeout('secret-token')) as post:
            with self.assertRaisesRegex(RuntimeError, 'сетевая ошибка Timeout') as error:
                telegram.send_message('test')
        self.assertEqual(post.call_count, 1)
        self.assertNotIn('secret-token', str(error.exception))

    def test_non_json_error_reports_status(self):
        r = self.response(502, {})
        r.json.side_effect = ValueError('secret-token')
        with patch('telegram.requests.post', return_value=r):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 502'):
                telegram.send_message('test')

    def test_rate_limit_retry_is_bounded(self):
        r = self.response(429, dict(ok=False, error_code=429, parameters=dict(retry_after=1)))
        with patch('telegram.requests.post', return_value=r) as post:
            with self.assertRaisesRegex(RuntimeError, '429'):
                telegram.send_message('test')
        self.assertEqual(post.call_count, 3)

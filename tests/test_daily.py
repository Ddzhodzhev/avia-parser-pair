import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
import daily
import monitor


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.c = config.load()
        self.now = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)

    def report(self, destination, price, hub=None, return_hub=None):
        route = dict(destination=destination)
        if hub:
            route.update(hub=hub, return_hub=return_hub or hub)
        cities = ['MOW', hub, destination, return_hub or hub, 'MOW'] if hub else ['MOW', destination, 'MOW']
        legs = tuple(dict(origin=a, destination=b, origin_airport=a, destination_airport=b,
                          transfers=0 if hub else 1, dep=self.now, arr=self.now,
                          link=f'https://example.org/{a}-{b}') for a, b in zip(cities, cities[1:]))
        trip = dict(key=str(price), route=route, legs=legs, total=price, booking=not hub)
        return SimpleNamespace(route=route, cheapest=trip, error=None)

    def test_minimum_across_runs_routes_and_json_reload(self):
        state = {}
        daily.record(state, [self.report('CXR', 60000), self.report('HKT', 59000)], self.c, self.now)
        daily.record(state, [self.report('CXR', 55000, 'OVB', 'HAN'), self.report('HKT', 70000)], self.c, self.now)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'daily.json'
            monitor.save_json(path, state)
            state = monitor.load_json(path, {})
        send, save = Mock(), Mock()
        # 21:05 UTC is already October 4 in Moscow: summarize October 3.
        next_day = self.now.replace(hour=21, minute=5)
        daily.send_yesterday(state, self.c, next_day, send, save)
        text = '\n'.join(call.args[0] for call in send.call_args_list)
        self.assertIn('03.10.2026', text)
        self.assertIn('55000 ₽', text)
        self.assertIn('59000 ₽', text)
        self.assertNotIn('70000 ₽', text)
        self.assertIn('туда через Новосибирск, обратно через Ханой', text)
        self.assertEqual(text.count('href='), 5)
        count = send.call_count
        daily.send_yesterday(state, self.c, next_day, send, save)
        self.assertEqual(send.call_count, count)

    def test_moscow_day_boundary_and_no_carryover(self):
        state = {}
        daily.record(state, [self.report('CXR', 100)], self.c, self.now.replace(hour=20, minute=59))
        daily.record(state, [self.report('CXR', 200)], self.c, self.now.replace(hour=21))
        self.assertEqual(state['days']['2026-10-03']['best']['CXR']['total'], 100)
        self.assertEqual(state['days']['2026-10-04']['best']['CXR']['total'], 200)
        self.assertNotIn('HKT', state['days']['2026-10-04']['best'])

    def test_no_data_partial_and_dry_run(self):
        for partial in (False, True):
            state = {}
            if partial:
                report = self.report('CXR', 100)
                report.error = 'HTTP 500'
                daily.record(state, [report], self.c, self.now)
            send, save = Mock(), Mock()
            daily.send_yesterday(state, self.c, self.now.replace(hour=21), send, save, dry_run=True)
            text = '\n'.join(call.args[0] for call in send.call_args_list)
            self.assertIn('Часть поисков' if partial else 'нет сохранённых проверок', text)
            self.assertIn('Нячанг — подходящих вариантов не найдено', text)
            self.assertNotIn('100 ₽', text)
            save.assert_not_called()
            self.assertFalse(state['days']['2026-10-03'].get('sent'))

    def test_partial_send_resumes_at_unsent_chunk(self):
        state = {'days': {'2026-10-03': dict(messages=['first', 'second'], runs=1, best={})}}
        persisted = []
        send = Mock(side_effect=[None, RuntimeError('network')])
        with self.assertRaises(RuntimeError):
            daily.send_yesterday(state, self.c, self.now.replace(hour=21), send,
                                 lambda s: persisted.append(copy.deepcopy(s)))
        retry = Mock()
        daily.send_yesterday(persisted[-1], self.c, self.now.replace(hour=21), retry, Mock())
        retry.assert_called_once_with('second')

    def test_daily_cli_does_not_request_api_or_write_in_dry_run(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict('os.environ', {'DATA_DIR': folder, 'TRAVELPAYOUTS_TOKEN': ''}), \
                patch('config.load_dotenv'), patch('sys.argv', ['monitor.py', '--daily-summary', '--dry-run']), \
                patch('aviasales.Client') as client, patch('telegram.send_message') as send, \
                patch('builtins.print') as output:
            monitor.main()
            client.assert_not_called()
            send.assert_not_called()
            self.assertTrue(output.called)
            self.assertFalse((Path(folder) / 'daily.json').exists())

    def test_search_persists_minimum_before_telegram_failure(self):
        c = copy.deepcopy(self.c)
        c['routes'] = [next(r for r in c['routes'] if r['id'] == 'cxr-through')]
        trip = self.report('CXR', 60000).cheapest
        trip['route'] = c['routes'][0]
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict('os.environ', {'DATA_DIR': folder, 'TRAVELPAYOUTS_TOKEN': 'test',
                                          'TELEGRAM_BOT_TOKEN': 'test', 'TELEGRAM_CHAT_ID': '1'}), \
                patch('config.load', return_value=c), patch('config.load_dotenv'), \
                patch('sys.argv', ['monitor.py']), patch('aviasales.Client') as client, \
                patch('routes.booked_trips', return_value=iter([trip])), \
                patch('telegram.send_message', side_effect=RuntimeError('network')):
            client.return_value.fetch.return_value = []
            with self.assertRaises(RuntimeError):
                monitor.main()
            state = monitor.load_json(Path(folder) / 'daily.json', {})
            best = next(iter(state['days'].values()))['best']['CXR']
            self.assertEqual(best['total'], 60000)

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock
import aviasales
import config
import monitor
import routes


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.c = config.load()
        self.route = next(r for r in self.c['routes'] if r['id'] == 'bkk-dad')
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def leg(self, a, b, when, duration=120, price=5000, **extra):
        item = dict(origin=a, destination=b, origin_airport=a, destination_airport=b,
                    departure_at=when, duration_to=duration, price=price, transfers=0,
                    airline='XX', flight_number='1', link='/search/test')
        item.update(extra)
        return aviasales.normalize([item], a, b, self.c, True, self.now)[0]

    def offers(self):
        return {
            ('MOW', 'BKK'): [self.leg('MOW', 'BKK', '2027-04-30T20:00:00+03:00', 540)],
            ('BKK', 'DAD'): [self.leg('BKK', 'DAD', '2027-05-01T16:00:00+07:00')],
            ('DAD', 'BKK'): [self.leg('DAD', 'BKK', '2027-05-11T10:00:00+07:00')],
            ('BKK', 'MOW'): [self.leg('BKK', 'MOW', '2027-05-11T18:00:00+07:00', 540)],
        }

    def test_full_trip_and_may_return(self):
        trips = list(routes.build_trips(self.route, self.offers(), self.c))
        self.assertEqual(len(trips), 1)
        self.assertEqual((trips[0]['total'], trips[0]['stay'], len(trips[0]['legs'])), (20000, 10, 4))
        self.assertIn('2027-05', config.search_months(self.c))

    def test_missing_return(self):
        offers = self.offers()
        offers['BKK', 'MOW'] = []
        self.assertEqual(list(routes.build_trips(self.route, offers, self.c)), [])

    def test_connections_both_directions(self):
        for edge in [('BKK', 'DAD'), ('BKK', 'MOW')]:
            offers = self.offers()
            offers[edge][0]['dep'] = offers[edge][0]['dep'].replace(hour=13)
            self.assertEqual(list(routes.build_trips(self.route, offers, self.c)), [])

    def test_airport_change_and_unknown(self):
        for airport in ('DMK', ''):
            offers = self.offers()
            offers['BKK', 'DAD'][0]['origin_airport'] = airport
            self.assertEqual(list(routes.build_trips(self.route, offers, self.c)), [])

    def test_max_connection_and_stay(self):
        offers = self.offers()
        offers['BKK', 'DAD'][0]['dep'] = datetime.fromisoformat('2027-05-04T16:00:00+07:00')
        self.assertEqual(list(routes.build_trips(self.route, offers, self.c)), [])
        self.c['min_trip_days'] = 11
        self.assertEqual(list(routes.build_trips(self.route, self.offers(), self.c)), [])

    def test_total_threshold_inclusive(self):
        self.route['max_total_price'] = 20000
        self.assertEqual(len(list(routes.build_trips(self.route, self.offers(), self.c))), 1)
        self.route['max_total_price'] = 19999
        self.assertEqual(list(routes.build_trips(self.route, self.offers(), self.c)), [])

    def test_departure_window(self):
        offers = self.offers()
        offers['MOW', 'BKK'][0]['dep'] = datetime.fromisoformat('2027-05-01T00:00:00+03:00')
        self.assertEqual(list(routes.build_trips(self.route, offers, self.c)), [])

    def test_two_ticket_direct(self):
        r = self.c['routes'][0]
        offers = {('MOW', 'CXR'): [self.leg('MOW', 'CXR', '2027-03-01T20:00:00+03:00', 600)],
                  ('CXR', 'MOW'): [self.leg('CXR', 'MOW', '2027-03-12T12:00:00+07:00', 600)]}
        self.assertEqual(next(routes.build_trips(r, offers, self.c))['stay'], 10)

    def test_same_day_alternatives_preserved(self):
        offers = self.offers()
        bad = copy.deepcopy(offers['BKK', 'DAD'][0])
        bad['dep'] = bad['dep'].replace(hour=10)
        bad['price'] = 1000
        offers['BKK', 'DAD'].append(bad)
        self.assertEqual(len(list(routes.build_trips(self.route, offers, self.c))), 1)

    def test_invalid_api_records(self):
        good = dict(origin='MOW', destination='CXR', departure_at='2027-03-01T20:00:00+03:00',
                    duration=600, price=15000, transfers=0)
        for update in [dict(duration=0), dict(price=float('nan')), dict(transfers=1),
                       dict(departure_at='2027-03-01T20:00:00'), dict(return_at='2027-03-15'),
                       dict(expires_at='2026-01-01T00:00:00Z')]:
            self.assertEqual(aviasales.normalize([good | update], 'MOW', 'CXR', self.c, True, self.now), [])
        self.assertEqual(aviasales.normalize([good], 'MOW', 'CXR', self.c, True, self.now)[0]['arr'].hour, 10)

    def test_notification_state(self):
        trips = list(routes.build_trips(self.route, self.offers(), self.c))
        with tempfile.TemporaryDirectory() as directory, patch('telegram.send_message') as send:
            state = Path(directory) / 'alerts.json'
            alerts = {}
            monitor.notify(trips, alerts, self.c, state)
            monitor.notify(trips, alerts, self.c, state)
            self.assertEqual(send.call_count, 1)
            trips[0]['total'] -= 100
            monitor.notify(trips, alerts, self.c, state)
            self.assertEqual(send.call_count, 2)
            trips[0]['total'] -= 100
            send.side_effect = RuntimeError('failed')
            before = dict(alerts)
            with self.assertRaises(RuntimeError):
                monitor.notify(trips, alerts, self.c, state)
            self.assertEqual(alerts, before)

    def test_dry_run_and_limit(self):
        trip = next(routes.build_trips(self.route, self.offers(), self.c))
        second = dict(trip, key='another', total=21000)
        self.c['max_alerts'] = 1
        with tempfile.TemporaryDirectory() as directory, patch('telegram.send_message') as send, patch('builtins.print'):
            state = Path(directory) / 'alerts.json'
            alerts = {}
            monitor.notify([trip, second], alerts, self.c, state, True)
            self.assertFalse(state.exists())
            send.assert_not_called()
            monitor.notify([trip, second], alerts, self.c, state)
            self.assertNotIn('another', alerts)

    def test_pagination_and_cache(self):
        client = aviasales.Client('test', self.c, ['2027-03'])
        response1, response2 = Mock(), Mock()
        response1.json.return_value = dict(success=True, data=[{}] * 1000)
        response2.json.return_value = dict(success=True, data=[])
        client.session.get = Mock(side_effect=[response1, response2])
        self.assertEqual(client.fetch('MOW', 'CXR', True), [])
        client.fetch('MOW', 'CXR', True)
        self.assertEqual(client.session.get.call_count, 2)
        self.assertEqual(client.session.get.call_args.kwargs['params']['page'], 2)

    def test_config_validation(self):
        text = (config.ROOT / 'config.yaml').read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.yaml'
            path.write_text(text.replace('min_trip_days: 10', 'min_trip_days: 20'))
            with self.assertRaisesRegex(ValueError, 'min_trip_days'):
                config.load(path)

    def test_roundtrip_fare_not_doubled(self):
        item = dict(origin='MOW', destination='CXR', departure_at='2027-04-30T20:00:00+03:00',
                    return_at='2027-05-11T12:00:00+07:00', price=29000,
                    duration_to=600, duration_back=660, duration=1260,
                    transfers=0, return_transfers=0)
        bookings = aviasales.normalize_roundtrips([item], 'MOW', 'CXR', self.c, True, self.now)
        trip = next(routes.booked_trips(self.c['routes'][0], bookings, self.c))
        self.assertEqual(trip['total'], 29000)
        self.assertEqual(trip['stay'], 10)
        self.assertEqual(trip['legs'][1]['arr'].hour, 19)
        self.assertIn('Единый тариф RT', monitor.format_trip(trip, self.c))
        self.assertEqual(aviasales.normalize_roundtrips([item | dict(duration_back=0)],
                         'MOW', 'CXR', self.c, True, self.now), [])

    def test_nha_city_cxr_airport_both_directions(self):
        base = dict(departure_at='2027-03-01T20:00:00+03:00', duration_to=600,
                    price=15000, transfers=0)
        out = base | dict(origin='MOW', origin_airport='SVO', destination='NHA', destination_airport='CXR')
        back = base | dict(origin='NHA', origin_airport='CXR', destination='MOW', destination_airport='SVO')
        for item, origin, destination in [(out, 'MOW', 'CXR'), (back, 'CXR', 'MOW')]:
            offers = aviasales.normalize([item], origin, destination, self.c, True, self.now)
            self.assertEqual(len(offers), 1)
            self.assertEqual((offers[0]['origin'], offers[0]['destination']), (origin, destination))
        for airport in ('DAD', ''):
            self.assertEqual(aviasales.normalize([out | dict(destination_airport=airport)],
                             'MOW', 'CXR', self.c, True, self.now), [])

    def test_nha_roundtrip_and_aggregated_reasons(self):
        item = dict(origin='MOW', destination='NHA', destination_airport='CXR',
                    departure_at='2027-04-30T20:00:00+03:00', return_at='2027-05-11T12:00:00+07:00',
                    price=29000, duration_to=600, duration_back=660, transfers=0, return_transfers=0)
        bookings = aviasales.normalize_roundtrips([item], 'MOW', 'CXR', self.c, True, self.now)
        self.assertEqual(next(routes.booked_trips(self.c['routes'][0], bookings, self.c))['total'], 29000)
        with self.assertLogs(level='INFO') as logs:
            aviasales.normalize_roundtrips([item | dict(destination_airport='DAD')] * 3,
                                          'MOW', 'CXR', self.c, True, self.now)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("'route_mismatch': 3", logs.output[0])

    def test_main_dry_run_with_mock_api(self):
        import os
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'TRAVELPAYOUTS_TOKEN': 'test', 'DATA_DIR': directory}), \
                patch('sys.argv', ['monitor.py', '--dry-run']), \
                patch('aviasales.Client') as client, patch('builtins.print'), \
                patch('telegram.send_message') as send:
            offers = self.offers()
            client.return_value.fetch.side_effect = lambda a, b, direct: offers.get((a, b), [])
            client.return_value.roundtrips.return_value = []
            monitor.main()
            send.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()

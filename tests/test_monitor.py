import copy
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock
import aviasales
import config
import monitor
import routes
import reporting


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.c = config.load()
        # Legacy scenarios deliberately cover open-ended April departures / May returns.
        self.c.pop('return_end', None)
        self.c['departure_end'] = date(2027, 4, 30)
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

    def test_return_deadline_uses_arrival_in_moscow(self):
        offers = self.offers()
        self.c['return_end'] = date(2027, 5, 11)
        self.assertEqual(len(list(routes.build_trips(self.route, offers, self.c))), 1)
        offers['BKK', 'MOW'][0]['arr'] = datetime.fromisoformat('2027-05-12T00:00:00+03:00')
        self.assertEqual(list(routes.build_trips(self.route, offers, self.c)), [])
        self.assertEqual(list(routes.booked_trips(self.c['routes'][0],
            [(offers['MOW', 'BKK'][0], offers['BKK', 'MOW'][0])], self.c)), [])

    def test_current_search_settings(self):
        c = config.load()
        self.assertEqual(c['return_end'], date(2027, 4, 28))
        self.assertEqual(config.search_months(c), ['2027-03', '2027-04'])
        self.assertTrue(any(r['destination'] == 'CXR' and not r.get('hub')
                            and not r['direct_only'] for r in c['routes']))

    def test_histogram_boundaries(self):
        h = reporting.Histogram()
        for price in (1, 5000, 5000.01, 30000, 35000, 40000, 45000, 50000, 50001):
            h.add(price)
        self.assertEqual(h.counts, [2, 1, 0, 0, 0, 1, 1, 1, 1, 1, 1])
        self.assertEqual(h.minimum, 1)

    def test_cxr_transfer_limit_both_sides_and_hubs(self):
        r = next(r for r in self.c['routes'] if r['id'] == 'cxr-through')
        out = self.leg('MOW', 'CXR', '2027-03-01T20:00:00+03:00', 600)
        back = self.leg('CXR', 'MOW', '2027-03-12T12:00:00+07:00', 600)
        out['transfers'] = back['transfers'] = 1
        offers = {('MOW', 'CXR'): [out], ('CXR', 'MOW'): [back]}
        self.assertEqual(len(list(routes.build_trips(r, offers, self.c))), 1)
        self.assertEqual(len(list(routes.booked_trips(r, [(out, back)], self.c))), 1)
        for leg in (out, back):
            leg['transfers'] = 2
            self.assertEqual(list(routes.build_trips(r, offers, self.c)), [])
            self.assertEqual(list(routes.booked_trips(r, [(out, back)], self.c)), [])
            leg['transfers'] = 1
        self.assertFalse(routes.transfers_allowed((out, back), r, self.c))
        out['transfers'] = back['transfers'] = 0
        self.assertTrue(routes.transfers_allowed((out, back), r, self.c))

    def test_compact_report_cheapest_links_and_zero_rows(self):
        report = reporting.RouteReport(self.route, routes.directional_paths(self.route, self.offers(), self.c))
        trip = next(routes.build_trips(self.route, self.offers(), self.c))
        report.add_trip(dict(trip, total=60000))
        report.add_trip(dict(trip, key='cheaper', total=40000))
        output = report.render(self.c)
        self.assertIn('Лучший RT: <b>40000 ₽</b>', output)
        self.assertEqual(output.count('>Открыть</a>'), 4)
        self.assertIn('30.04 20:00', output)
        self.assertNotIn('≤5', output)
        empty = reporting.RouteReport(self.route, ([], [])).render(self.c)
        self.assertIn('нет вариантов', empty)
        self.assertNotIn('<pre>', empty)
        rt = reporting.RouteReport(self.route, ([], []))
        rt.add_trip(dict(trip, booking=True, legs=(trip['legs'][0], trip['legs'][-1])))
        self.assertEqual(rt.render(self.c).count('>Открыть</a>'), 1)

    def test_extra_hubs_enabled_for_both_destinations(self):
        for hub in ('DXB', 'DEL', 'KUL', 'SIN', 'FRU'):
            for destination in ('CXR', 'DAD'):
                self.assertTrue(any(r.get('hub') == hub and r['destination'] == destination
                                    and r.get('enabled', True) for r in self.c['routes']))

    def test_report_includes_expensive_compatible_trips(self):
        offers = self.offers()
        for legs in offers.values():
            legs[0]['price'] = 20000
        self.assertEqual(list(routes.build_trips(self.route, offers, self.c)), [])
        directions = routes.directional_paths(self.route, offers, self.c)
        report = reporting.RouteReport(self.route, directions)
        trips = list(routes.build_trips(self.route, offers, self.c, False, directions))
        for trip in trips * 2:
            report.add_trip(trip)
        self.assertEqual(report.outbound.minimum, 40000)
        self.assertEqual(report.inbound.minimum, 40000)
        self.assertEqual(report.roundtrip.minimum, 80000)
        self.assertEqual(sum(report.roundtrip.counts), 1)
        chunks = reporting.messages([report] * 30, self.c, self.now)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(s) <= 3800 for s in chunks))

    def test_summary_sent_every_run_without_deals(self):
        import os
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'TRAVELPAYOUTS_TOKEN': 'test', 'TELEGRAM_BOT_TOKEN': 'test',
                'TELEGRAM_CHAT_ID': '1', 'DATA_DIR': directory}), \
                patch('sys.argv', ['monitor.py']), patch('aviasales.Client') as client, \
                patch('telegram.send_message') as send:
            client.return_value.fetch.return_value = []
            client.return_value.roundtrips.return_value = []
            monitor.main()
            calls = send.call_count
            self.assertGreater(calls, 0)
            self.assertIn('Проверка цен', send.call_args_list[0].args[0])
            monitor.main()
            self.assertEqual(send.call_count, calls * 2)
            self.assertFalse((Path(directory) / 'alerts.json').exists())


if __name__ == '__main__':
    unittest.main()

"""Fetch, join, threshold, deduplicate and notify. See --help for offline validation."""
import argparse
import json
import logging
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path
import aviasales
import config
import routes
import telegram
import requests
import reporting


def load_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)  # Corrupt state must not silently cause duplicate alerts.


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def format_trip(trip, c):
    r = trip['route']
    name = escape(c['cities'][r['destination']]['name'])
    via = ' через ' + escape(c['cities'][r['hub']]['name']) if r.get('hub') else ''
    ticket_type = 'Единый тариф RT' if trip.get('booking') else 'Отдельные билеты'
    lines = [f"✈️ Москва ↔ {name}{via}", f"<b>{trip['total']:g} ₽ туда-обратно</b> · {trip['stay']} дней во Вьетнаме",
             f"Порог: {r['max_total_price']:g} ₽. {ticket_type}; время местное."]
    for o in trip['legs']:
        lines.append(f"\n{escape(o['origin_airport'] or o['origin'])} → {escape(o['destination_airport'] or o['destination'])}: "
                     f"{o['dep']:%d.%m.%Y %H:%M} → {o['arr']:%d.%m.%Y %H:%M}\n"
                     + ('' if trip.get('booking') else f"{o['price']:g} ₽ · ")
                     + f"{escape(o['airline'] + o['flight_number'])} · пересадок: {o['transfers']}")
        if o['link']:
            lines.append(f'<a href="{escape(o["link"], quote=True)}">Проверить билет</a>')
    lines.append('\nЦены из кеша: проверьте наличие, багаж и итоговую стоимость по ссылкам.')
    text = '\n'.join(lines)
    if len(text) > 4000:
        # Long tracking URLs can exceed Telegram limits. Flight dates/details remain usable.
        text = '\n'.join(line for line in lines if not line.startswith('<a href='))
    return text


def notify(trips, alerts, c, state_path, dry_run=False):
    sent = 0
    for trip in sorted(trips, key=lambda t: t['total']):
        old = alerts.get(trip['key'])
        if old is not None and trip['total'] >= old:
            continue
        message = format_trip(trip, c)
        if dry_run:
            print(message)
        else:
            telegram.send_message(message)
            alerts[trip['key']] = trip['total']
            save_json(state_path, alerts)
        sent += 1
        if sent >= c['max_alerts']:
            break
    return sent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=config.ROOT / 'config.yaml')
    parser.add_argument('--check-config', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Запросить API, печатать результаты без отправки и записи состояния')
    args = parser.parse_args()
    config.load_dotenv()
    c = config.load(args.config)
    months = config.search_months(c)
    if args.check_config:
        print(f"Конфиг корректен. Вылеты: {c['departure_start']} — {c['departure_end']}; месяцы API: {', '.join(months)}")
        return
    token = os.environ.get('TRAVELPAYOUTS_TOKEN', '').strip()
    if not token:
        raise ValueError('Задайте TRAVELPAYOUTS_TOKEN в .env или GitHub Secrets')
    if not args.dry_run and not all(os.environ.get(k, '').strip() for k in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID')):
        raise ValueError('Нужны секреты Telegram или --dry-run')
    data_dir = Path(os.environ.get('DATA_DIR', str(config.ROOT / 'data')))
    state_path = data_dir / 'alerts.json'
    alerts = load_json(state_path, {})
    if not isinstance(alerts, dict) or any(type(v) not in (int, float) for v in alerts.values()):
        raise ValueError('Некорректный формат alerts.json')
    client = aviasales.Client(token, c, months)
    trips, counts, reports = {}, {}, []
    failures = {}
    for route in c['routes']:
        if not route.get('enabled', True):
            continue
        try:
            offers = {(a, b): client.fetch(a, b, route['direct_only']) for a, b in routes.edges(route, c['origin'])}
            bookings = client.roundtrips(c['origin'], route['destination'], route['direct_only']) if not route.get('hub') else []
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            reason = f'HTTP {status}' if status else type(exc).__name__
            failures[route['id']] = reason
            logging.error('%s: поиск не завершён (%s)', route['id'], reason)
            report = reporting.RouteReport(route, ([], []))
            report.error = reason
            reports.append(report)
            continue
        directions = routes.directional_paths(route, offers, c)
        report = reporting.RouteReport(route, directions)
        reports.append(report)
        count = 0
        for trip in routes.build_trips(route, offers, c, apply_price_limit=False, directions=directions):
            report.add_trip(trip)
            if trip['total'] <= route['max_total_price']:
                trips[trip['key']] = trip
                count += 1
        if not route.get('hub'):
            for trip in routes.booked_trips(route, bookings, c, apply_price_limit=False):
                report.add_trip(trip)
                if trip['total'] <= route['max_total_price']:
                    trips[trip['key']] = trip
                    count += 1
        counts[route['id']] = count
        logging.info('%s: предложений по плечам %s; выгодных поездок %d', route['id'],
                     [len(v) for v in offers.values()], count)
        logging.info('%s: совместимых поездок %d; минимум RT %s', route['id'],
                     sum(report.roundtrip.counts), report.roundtrip.minimum)
    for index, message in enumerate(reporting.messages(reports, c, datetime.now(timezone.utc), include_summary=False), 1):
        if args.dry_run:
            print(message)
        else:
            logging.info('Telegram: отчёт, часть %d; текст %d, HTML %d символов',
                         index, reporting.text_length(message), len(message))
            telegram.send_message(message)
    sent = notify(trips.values(), alerts, c, state_path, args.dry_run)
    for index, message in enumerate(reporting.summary_messages(reports, c), 1):
        if args.dry_run:
            print(message)
        else:
            logging.info('Telegram: топ-5, часть %d; текст %d, HTML %d символов',
                         index, reporting.text_length(message), len(message))
            telegram.send_message(message)
    if not args.dry_run:
        history_path = data_dir / 'history.json'
        history = load_json(history_path, [])
        if not isinstance(history, list):
            raise ValueError('Некорректный формат history.json')
        history.append(dict(at=datetime.now(timezone.utc).isoformat(), routes=counts, errors=failures, notified=sent,
                            cheapest=min((t['total'] for t in trips.values()), default=None)))
        save_json(history_path, history[-1000:])
    logging.info('Выгодных полных поездок: %d; уведомлений: %d', len(trips), sent)
    if failures:
        raise RuntimeError(f'Отчёт неполный: ошибок маршрутов {len(failures)}; доступные результаты отправлены')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        logging.error('%s', exc)
        raise SystemExit(1) from None

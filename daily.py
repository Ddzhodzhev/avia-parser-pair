"""Persist daily observed RT minima and send yesterday's digest in Moscow time."""
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

import reporting
import routes

MOSCOW = ZoneInfo('Europe/Moscow')
DESTINATIONS = ('CXR', 'HKT')


def offer_block(trip, c):
    legs, route = trip['legs'], trip['route']
    changes = reporting.trip_group(trip)[2]
    title = (c['cities'][c['origin']]['name'] + ' ↔ ' + c['cities'][route['destination']]['name']
             + routes.via_label(route, c)
             + (' · ❗без пересадок' if changes == 0 else f' · ≤{changes} пересадки'))
    links = []
    for i, leg in enumerate(legs):
        if trip.get('booking') and i:
            continue
        label = ('Билет RT' if trip.get('booking') else
                 f"{leg['origin_airport'] or leg['origin']}→{leg['destination_airport'] or leg['destination']}")
        links.append(f'<a href="{escape(leg["link"], quote=True)}">{escape(label)}</a>'
                     if leg['link'] else escape(label) + ' (нет ссылки)')
    return (f'<b>{escape(title)}</b>\n{legs[0]["dep"]:%d.%m}–{legs[-1]["arr"]:%d.%m}'
            f' · <b>{trip["total"]:g} ₽</b> · ' + ' / '.join(links))


def record(state, reports, c, now):
    """Keep the lowest observation, even if it disappears or rises later."""
    local = now.astimezone(MOSCOW)
    days = state.setdefault('days', {})
    day = days.setdefault(local.date().isoformat(), dict(runs=0, partial=False, best={}))
    day['runs'] += 1
    day['partial'] = day['partial'] or any(r.error for r in reports)
    for report in reports:
        trip = report.cheapest
        destination = report.route['destination']
        if report.error or trip is None or destination not in DESTINATIONS:
            continue
        old = day['best'].get(destination)
        if old is None or trip['total'] < old['total']:
            day['best'][destination] = dict(total=trip['total'], key=trip['key'],
                                            block=offer_block(trip, c), observed=local.strftime('%H:%M'))
    cutoff = (local.date() - timedelta(days=30)).isoformat()
    state['days'] = {key: value for key, value in days.items() if key >= cutoff}


def send_yesterday(state, c, now, send, save, dry_run=False):
    date = now.astimezone(MOSCOW).date() - timedelta(days=1)
    day = state.setdefault('days', {}).setdefault(date.isoformat(), dict(runs=0, partial=False, best={}))
    if day.get('sent'):
        return
    # Freeze the payload so retries resume the same set of chunks.
    if 'messages' not in day:
        header = f'<b>🌙 Итоги дня · {date:%d.%m.%Y} МСК</b>\nМинимумы за день · туда-обратно'
        sections = []
        for destination in DESTINATIONS:
            best = day['best'].get(destination)
            if best:
                sections.append([best['block'] + f'\nЗамечено в {best["observed"]} МСК'])
            else:
                sections.append([escape(c['cities'][destination]['name']) + ' — подходящих вариантов не найдено'])
        if not day['runs']:
            header += '\n⚠️ За день нет сохранённых проверок.'
        elif day['partial']:
            header += '\n⚠️ Часть поисков завершилась с ошибкой.'
        sections.append(['Цены из кеша, найденные за день; наличие и цену проверьте по ссылкам.'])
        day['messages'] = reporting.pack_sections(header, sections, f'<b>🌙 Итоги дня · {date:%d.%m.%Y} · продолжение</b>')
    for index in range(day.get('sent_parts', 0), len(day['messages'])):
        send(day['messages'][index])
        if not dry_run:
            day['sent_parts'] = index + 1
            save(state)
    if not dry_run:
        day['sent'] = True
        save(state)

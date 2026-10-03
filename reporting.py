"""Price distributions of observed itineraries, not seats or live availability."""
from bisect import bisect_left
from html import escape
from html.parser import HTMLParser
from aviasales import identity

BOUNDS = list(range(5000, 50001, 5000))
LABELS = ['≤5'] + [f'{n}–{n+5}' for n in range(5, 50, 5)] + ['>50']


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.length = 0

    def handle_data(self, data):
        # UTF-16 units conservatively cover Telegram's entity offsets and emoji.
        self.length += len(data.encode('utf-16-le')) // 2


def text_length(html):
    parser = _VisibleText()
    parser.feed(html)
    return parser.length


# Conservative payload budget as well as the visible-text limit: hidden URLs
# contribute to Telegram's entity-size limit (ENTITIES_TOO_LONG).
HTML_BYTE_BUDGET = 8000


def fits_message(text):
    return text_length(text) <= 3800 and len(text.encode('utf-8')) <= HTML_BYTE_BUDGET


def pack_sections(header, sections, continuation):
    chunks, current = [], header
    def append(block):
        nonlocal current
        if not fits_message(current + '\n\n' + block):
            chunks.append(current)
            current = continuation
        if not fits_message(current + '\n\n' + block):
            raise ValueError('Один блок отчёта превышает безопасный размер Telegram')
        current += '\n\n' + block

    for section in sections:
        combined = '\n'.join(section)
        if fits_message(continuation + '\n\n' + combined):
            append(combined)
            continue
        for block in section:
            if fits_message(continuation + '\n\n' + block):
                append(block)
                continue
            # Split oversized expandable quotes without breaking HTML or URLs.
            opening, closing = '<blockquote expandable>', '</blockquote>'
            quoted = block.startswith(opening) and block.endswith(closing)
            content = block[len(opening):-len(closing)] if quoted else block
            for line in content.split('\n'):
                append(opening + line + closing if quoted else line)
    chunks.append(current)
    return chunks


def fold_details(title, summary, blocks):
    details = '\n'.join(blocks).replace('<pre>', '').replace('</pre>', '')
    return [title + ' · ' + summary, '<blockquote expandable>' + details + '</blockquote>']


class Histogram:
    def __init__(self):
        self.counts = [0] * (len(BOUNDS) + 1)
        self.minimum = None

    def add(self, price):
        self.counts[bisect_left(BOUNDS, price)] += 1
        self.minimum = price if self.minimum is None else min(self.minimum, price)


class TopOffers:
    """Keep only the cheapest distinct itineraries, with stable tie ordering."""
    def __init__(self, limit=5):
        self.limit = limit
        self.items = {}

    def add(self, key, item):
        if key not in self.items or item['total'] < self.items[key]['total']:
            self.items[key] = item
        if len(self.items) > self.limit:
            worst = max(self.items, key=lambda k: (self.items[k]['total'], k))
            del self.items[worst]

    def ordered(self):
        return [v for k, v in sorted(self.items.items(), key=lambda pair: (pair[1]['total'], pair[0]))]


def trip_group(item):
    legs, route = item['legs'], item['route']
    half = len(legs) // 2
    changes = max(sum(o['transfers'] for o in legs[:half]) + half - 1,
                  sum(o['transfers'] for o in legs[half:]) + half - 1)
    return (route['destination'], route.get('hub') or '', changes)


class RouteReport:
    def __init__(self, route, directions):
        self.route = route
        self.outbound, self.inbound, self.roundtrip = Histogram(), Histogram(), Histogram()
        self.tops = [TopOffers(), TopOffers(), TopOffers(10)]
        for index, (histogram, paths) in enumerate(zip((self.outbound, self.inbound), directions)):
            for legs in paths:
                histogram.add(sum(leg['price'] for leg in legs))
                key = '\n'.join(identity(leg) for leg in legs)
                self.tops[index].add(key, dict(legs=legs, total=sum(leg['price'] for leg in legs), route=route))
        self.group_tops = {}
        self.seen = set()
        self.cheapest = None
        self.error = None

    def add_trip(self, trip):
        self.group_tops.setdefault(trip_group(trip), TopOffers(5)).add(trip['key'], trip)
        self.tops[2].add(trip['key'], trip)
        if trip['key'] not in self.seen:
            self.roundtrip.add(trip['total'])
            self.seen.add(trip['key'])
            if self.cheapest is None or trip['total'] < self.cheapest['total']:
                self.cheapest = trip

    def blocks(self, c):
        r = self.route
        name = c['cities'][r['destination']]['name']
        if r.get('hub'):
            name += ' через ' + c['cities'][r['hub']]['name']
        elif r['direct_only']:
            name += ' · прямые'
        else:
            limit = c.get('max_transfers_by_destination', {}).get(r['destination'])
            name += f' · ≤{limit} пересадки' if limit is not None else ' · с пересадками'
        title = f'<b>{escape(name)}</b>'
        if self.error:
            return [title + ' — ошибка API: ' + escape(self.error)]
        histograms = (self.outbound, self.inbound, self.roundtrip)
        if not any(sum(h.counts) for h in histograms):
            return [title + ' — нет вариантов']
        rows = ['тыс.₽   туда обратно   RT']
        for i, label in enumerate(LABELS):
            if any(h.counts[i] for h in histograms):
                rows.append(f'{label:>6} {self.outbound.counts[i]:>6} {self.inbound.counts[i]:>7} {self.roundtrip.counts[i]:>4}')
        minimum = ' / '.join('—' if h.minimum is None else f'{h.minimum:,.0f}'.replace(',', ' ') for h in histograms)
        blocks = [title + '\n<pre>' + escape('\n'.join(rows)) + '</pre>\n'
                  + f'Мин. →/←/RT: {minimum} ₽ · порог {r["max_total_price"]:g} ₽']
        trip = self.cheapest
        if trip is None:
            blocks.append('Совместимого RT нет')
            return fold_details(title, 'RT нет', blocks[0].split('\n', 1)[1:] + blocks[1:])
        blocks.append(f"Лучший RT: <b>{trip['total']:g} ₽</b> · {trip['stay']} дней"
                      + (' · единый тариф' if trip.get('booking') else ' · отдельные билеты'))
        for index, leg in enumerate(trip['legs']):
            label = (f"{leg['origin_airport'] or leg['origin']}→{leg['destination_airport'] or leg['destination']} "
                     f"{leg['dep']:%d.%m %H:%M}–{leg['arr']:%d.%m %H:%M}")
            if not trip.get('booking'):
                label += f" · {leg['price']:g} ₽"
            if leg['transfers']:
                label += f" · пересадок {leg['transfers']}"
            line = escape(label)
            if leg['link'] and (not trip.get('booking') or index == 0):
                line += f' <a href="{escape(leg["link"], quote=True)}">Открыть</a>'
            elif not leg['link']:
                line += ' · ссылки нет в API'
            blocks.append(line)
        summary = f"<b>{trip['total']:g} ₽ RT</b> · {trip['legs'][0]['dep']:%d.%m}–{trip['legs'][-1]['arr']:%d.%m}"
        return fold_details(title, summary, blocks[0].split('\n', 1)[1:] + blocks[1:])

    def render(self, c):
        return '\n'.join(self.blocks(c))


def messages(reports, c, now, include_summary=True):
    end = c.get('return_end')
    header = (f'<b>Проверка цен · {now:%d.%m %H:%M} UTC</b>\n'
              f'Москва ↔ Вьетнам · {c["departure_start"]:%d.%m.%Y}–{c["departure_end"]:%d.%m.%Y}'
              + (f' · дома до {end:%d.%m}' if end else '')
              + f' · {c["min_trip_days"]}–{c["max_trip_days"]} дней.\n'
              'Цены из кеша; время местное. RT = туда-обратно.')
    if any(report.error for report in reports):
        header += '\n⚠️ Отчёт неполный: часть маршрутов не проверена.'
    sections, empty = [], []
    for report in reports:
        if not report.error and not any(sum(h.counts) for h in (report.outbound, report.inbound, report.roundtrip)):
            empty.append(report.blocks(c)[0].replace(' — нет вариантов', ''))
        else:
            sections.append(report.blocks(c))
    if empty:
        sections.append([f'Нет вариантов: {len(empty)} маршрутов',
                         '<blockquote expandable>' + '\n'.join(empty) + '</blockquote>'])
    chunks = pack_sections(header, sections, '<b>Цены · продолжение</b>')
    if include_summary:
        chunks.extend(summary_messages(reports, c))
    return chunks


def summary_messages(reports, c):
    groups = {}
    for report in reports:
        # Group by final destination, not by the country of a connection hub.
        group = c['cities'][report.route['destination']].get('summary_group', 'Другие направления')
        groups.setdefault(group, []).append(report)
    if not groups:
        return ['<b>Топ-10 RT</b>\nНет включённых маршрутов.']
    return [message for group, members in groups.items()
            for message in _group_summary(members, c, group)]


def _group_summary(reports, c, group):
    groups = {}
    for report in reports:
        if not report.error:
            for group_key, source in report.group_tops.items():
                target = groups.setdefault(group_key, TopOffers(5))
                for key, item in source.items.items():
                    target.add(key, item)
    ordered = sorted(groups.items(), key=lambda pair: (pair[1].ordered()[0]['total'], pair[0]))[:10]
    header = (f'<b>🏆 {escape(group)} · топ-10 маршрутов RT</b>\n'
              f'{c["departure_start"]:%d.%m.%Y}–{c["departure_end"]:%d.%m.%Y}'
              + (f' · дома до {c["return_end"]:%d.%m}' if c.get('return_end') else '')
              + f' · {c["min_trip_days"]}–{c["max_trip_days"]} дней\n'
              'Цена за всю поездку · максимум 1 пересадка в сторону · кеш API')
    errors = [r for r in reports if r.error]
    if errors:
        header += '\n⚠️ Отчёт неполный: ' + '; '.join(
            escape(r.route['id'] + ' — ошибка API: ' + r.error) for r in errors)
    sections = []
    for (destination, hub, changes), top in ordered:
        name = c['cities'][c['origin']]['name'] + ' ↔ ' + c['cities'][destination]['name']
        if hub:
            name += ' через ' + c['cities'][hub]['name']
        name += ' · ❗без пересадок' if changes == 0 else f' · ≤{changes} пересадки'
        title = '<b>' + escape(name) + '</b>'
        rows = []
        for item in top.ordered():
            legs = item['legs']
            dates = f'{legs[0]["dep"]:%d.%m}–{legs[-1]["arr"]:%d.%m}'
            links = []
            for n, leg in enumerate(legs):
                if item.get('booking') and n > 0:
                    continue
                label = 'RT' if item.get('booking') else ('туда' if n == 0 else 'обратно') if len(legs) == 2 else f'{leg["origin_airport"] or leg["origin"]}→{leg["destination_airport"] or leg["destination"]}'
                links.append(f'<a href="{escape(leg["link"], quote=True)}">{escape(label)}</a>' if leg['link'] else escape(label) + ' (нет ссылки)')
            rows.append(f'{dates} · <b>{item["total"]:g} ₽</b> · ' + '/'.join(links))
        # Keep up to five dates under one heading; split with the heading repeated
        # when hidden links exhaust Telegram's payload budget.
        batch = []
        for row in rows:
            candidate = title + '\n' + ' │ '.join(batch + [row])
            if batch and not fits_message('<b>Продолжение</b>\n\n' + candidate):
                sections.append([title, ' │ '.join(batch)])
                batch = []
            batch.append(row)
        if batch:
            sections.append([title, ' │ '.join(batch)])
    if not sections:
        sections.append(['Нет вариантов полной поездки в полученных данных.'])
    return pack_sections(header, sections, f'<b>🏆 {escape(group)} · топ-10 RT · продолжение</b>')

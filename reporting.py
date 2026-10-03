"""Price distributions of observed itineraries, not seats or live availability."""
from bisect import bisect_left
from html import escape
from html.parser import HTMLParser

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


class RouteReport:
    def __init__(self, route, directions):
        self.route = route
        self.outbound, self.inbound, self.roundtrip = Histogram(), Histogram(), Histogram()
        for histogram, paths in zip((self.outbound, self.inbound), directions):
            for legs in paths:
                histogram.add(sum(leg['price'] for leg in legs))
        self.seen = set()
        self.cheapest = None
        self.error = None

    def add_trip(self, trip):
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


def messages(reports, c, now):
    end = c.get('return_end')
    header = (f'<b>Проверка цен · {now:%d.%m %H:%M} UTC</b>\n'
              f'Москва ↔ Вьетнам · {c["departure_start"]:%d.%m.%Y}–{c["departure_end"]:%d.%m.%Y}'
              + (f' · дома до {end:%d.%m}' if end else '')
              + f' · {c["min_trip_days"]}–{c["max_trip_days"]} дней.\n'
              'Цены из кеша; время местное. RT = туда-обратно.')
    if any(report.error for report in reports):
        header += '\n⚠️ Отчёт неполный: часть маршрутов не проверена.'
    chunks, current = [], header
    empty = []
    for report in reports:
        if not report.error and not any(sum(h.counts) for h in (report.outbound, report.inbound, report.roundtrip)):
            empty.append(report.blocks(c)[0].replace(' — нет вариантов', ''))
            continue
        blocks = report.blocks(c)
        # Prefer keeping a route together; split only between complete HTML blocks.
        text = '\n'.join(blocks)
        groups = [text] if text_length(text) <= 3700 else blocks
        for block in groups:
            if text_length(current) + text_length(block) + 2 > 3800:
                chunks.append(current)
                current = '<b>Цены · продолжение</b>'
            current += '\n\n' + block
    if empty:
        block = f'Нет вариантов: {len(empty)} маршрутов\n<blockquote expandable>' + '\n'.join(empty) + '</blockquote>'
        if text_length(current) + text_length(block) + 2 > 3800:
            chunks.append(current)
            current = '<b>Цены · продолжение</b>'
        current += '\n\n' + block
    chunks.append(current)
    return chunks

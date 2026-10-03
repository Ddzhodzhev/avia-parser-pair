"""Price distributions of observed itineraries, not seats or live availability."""
from bisect import bisect_left
from html import escape

BOUNDS = list(range(5000, 50001, 5000))
LABELS = ['≤5'] + [f'{n}–{n+5}' for n in range(5, 50, 5)] + ['>50']


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

    def add_trip(self, trip):
        if trip['key'] not in self.seen:
            self.roundtrip.add(trip['total'])
            self.seen.add(trip['key'])

    def render(self, c):
        r = self.route
        name = c['cities'][r['destination']]['name']
        if r.get('hub'):
            name += ' через ' + c['cities'][r['hub']]['name']
        else:
            name += ' · прямые' if r['direct_only'] else ' · включая пересадки'
        histograms = (self.outbound, self.inbound, self.roundtrip)
        rows = ['тыс.₽     туда обратно    RT']
        for i, label in enumerate(LABELS):
            rows.append(f'{label:>6} {self.outbound.counts[i]:>8} {self.inbound.counts[i]:>7} {self.roundtrip.counts[i]:>5}')
        rows.append(f'Всего  {sum(self.outbound.counts):>8} {sum(self.inbound.counts):>7} {sum(self.roundtrip.counts):>5}')
        minimum = ' / '.join('—' if h.minimum is None else f'{h.minimum:,.0f}'.replace(',', ' ') for h in histograms)
        return (f'<b>Москва ↔ {escape(name)}</b>\n<pre>{escape(chr(10).join(rows))}</pre>\n'
                f'Минимум туда / обратно / RT: {minimum} ₽\nПорог RT: {r["max_total_price"]:g} ₽')


def messages(reports, c, now):
    end = c.get('return_end')
    header = (f'<b>Проверка цен · {now:%d.%m.%Y %H:%M} UTC</b>\n'
              f'Вылет: {c["departure_start"]:%d.%m.%Y}–{c["departure_end"]:%d.%m.%Y}. '
              + (f'Прилёт в Москву до {end:%d.%m.%Y} включительно. ' if end else '')
              + f'Во Вьетнаме {c["min_trip_days"]}–{c["max_trip_days"]} дней.\n'
              'Числа — варианты из кеша, не места. Туда/обратно — целые маршруты в одну сторону, '
              'без обязательной пары; RT — совместимые полные поездки, включая готовые тарифы. '
              'RT-тарифы не делятся пополам.\n'
              'Границы цен: нижняя не включена, верхняя включена. '
              'Строки маршрутов могут пересекаться — их количества не складывать.')
    chunks, current = [], header
    for report in reports:
        block = report.render(c)
        if len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = '<b>Распределение цен · продолжение</b>'
        current += '\n\n' + block
    chunks.append(current)
    return chunks

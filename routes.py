"""Combine complete journeys from two or four independent one-way tickets."""
import hashlib
from bisect import bisect_left, bisect_right
from datetime import timedelta
from aviasales import identity


def connected(first, second, c, direction):
    hours = (second['dep'] - first['arr']).total_seconds() / 3600
    minimum = c[f'min_connection_hours_{direction}']
    a, b = first['destination_airport'], second['origin_airport']
    if not a or not b or a != b:
        minimum = max(minimum, c['airport_change_min_hours'])
    return minimum <= hours <= c['max_connection_hours']


def paths(first, second, c, direction):
    ordered = sorted(second, key=lambda o: o['dep'])
    departures = [o['dep'] for o in ordered]
    for a in first:
        lo = bisect_left(departures, a['arr'])
        hi = bisect_right(departures, a['arr'] + timedelta(hours=c['max_connection_hours']))
        for b in ordered[lo:hi]:
            if connected(a, b, c, direction):
                yield (a, b)


def build_trips(route, offers, c):
    origin, dest, hub = c['origin'], route['destination'], route.get('hub')
    if hub:
        outbound = paths(offers[origin, hub], offers[hub, dest], c, 'outbound')
        inbound = list(paths(offers[dest, hub], offers[hub, origin], c, 'return'))
    else:
        outbound = ((o,) for o in offers[origin, dest])
        inbound = [(o,) for o in offers[dest, origin]]
    by_day = {}
    for legs in inbound:
        by_day.setdefault(legs[0]['dep'].date(), []).append(legs)
    for out in outbound:
        if not c['departure_start'] <= out[0]['dep'].date() <= c['departure_end']:
            continue
        out_price = sum(o['price'] for o in out)
        if out_price >= route['max_total_price']:
            continue
        arrival = out[-1]['arr']
        for stay in range(c['min_trip_days'], c['max_trip_days'] + 1):
            for back in by_day.get(arrival.date() + timedelta(days=stay), []):
                legs = out + back
                total = out_price + sum(o['price'] for o in back)
                if total <= route['max_total_price'] and back[0]['dep'] > arrival:
                    key = hashlib.sha256('\n'.join(identity(o) for o in legs).encode()).hexdigest()
                    yield dict(key=key, legs=legs, stay=stay, total=total, route=route)


def edges(route, origin):
    dest, hub = route['destination'], route.get('hub')
    return [(origin, hub), (hub, dest), (dest, hub), (hub, origin)] if hub else [(origin, dest), (dest, origin)]


def booked_trips(route, bookings, c):
    for out, back in bookings:
        stay = (back['dep'].date() - out['arr'].date()).days
        if (c['departure_start'] <= out['dep'].date() <= c['departure_end']
                and c['min_trip_days'] <= stay <= c['max_trip_days']
                and back['dep'] > out['arr'] and out['price'] <= route['max_total_price']):
            key = hashlib.sha256(('RT\n' + identity(out) + '\n' + identity(back)).encode()).hexdigest()
            yield dict(key=key, legs=(out, back), total=out['price'], stay=stay, route=route, booking=True)

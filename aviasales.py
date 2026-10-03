"""Travelpayouts cached offers. Never fabricate missing flight times."""
import logging
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = 'https://api.travelpayouts.com/aviasales/v3/prices_for_dates'


def timestamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('API timestamp lacks timezone')
    return value


def endpoint_matches(item, field, requested):
    # API returns city codes in origin/destination even for airport requests:
    # a request for CXR can return destination=NHA, destination_airport=CXR.
    return requested in (item.get(field), item.get(field + '_airport'))


def log_rejections(origin, destination, rejected):
    if rejected:
        logging.info('%s → %s: пропущено %d; причины: %s', origin, destination,
                     sum(rejected.values()), dict(rejected))


def normalize(items, origin, destination, c, direct_only, now=None, rejections=None):
    now = now or datetime.now(timezone.utc)
    offers = {}
    rejected = rejections if rejections is not None else Counter()
    for item in items:
        try:
            if item.get('return_at'):
                raise ValueError('round_trip_in_one_way_results')
            if not endpoint_matches(item, 'origin', origin) or not endpoint_matches(item, 'destination', destination):
                raise ValueError('route_mismatch')
            price = float(item['price'])
            minutes = float(item.get('duration_to') or item['duration'])
            transfers = item['transfers']
            if not math.isfinite(price) or price <= 0 or not 0 < minutes <= c['max_leg_hours'] * 60:
                raise ValueError('invalid price or duration')
            if type(transfers) is not int or transfers < 0 or (direct_only and transfers != 0):
                raise ValueError('invalid transfers')
            if item.get('expires_at') and timestamp(item['expires_at']) <= now:
                raise ValueError('expired')
            dep = timestamp(item['departure_at']).astimezone(ZoneInfo(c['cities'][origin]['timezone']))
            if dep <= now:
                raise ValueError('past departure')
            arr = (dep.astimezone(timezone.utc) + timedelta(minutes=minutes)).astimezone(
                ZoneInfo(c['cities'][destination]['timezone']))
            link = urljoin('https://www.aviasales.ru', item.get('link') or '')
            if urlparse(link).scheme != 'https' or urlparse(link).hostname not in ('www.aviasales.ru', 'www.aviasales.com', 'aviasales.ru', 'aviasales.com'):
                link = ''
            offer = dict(origin=origin, destination=destination, price=price, dep=dep, arr=arr,
                         origin_airport=item.get('origin_airport') or '',
                         destination_airport=item.get('destination_airport') or '',
                         airline=str(item.get('airline', '')), flight_number=str(item.get('flight_number', '')),
                         transfers=transfers, link=link)
            key = identity(offer)
            if key not in offers or price < offers[key]['price']:
                offers[key] = offer
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            # Fixed reason names only: do not dump raw responses or tracking links.
            known = {'round_trip_in_one_way_results', 'route_mismatch', 'invalid price or duration',
                     'invalid transfers', 'expired', 'past departure', 'API timestamp lacks timezone'}
            reason = str(exc) if str(exc) in known else 'missing_or_invalid_fields'
            rejected[reason] += 1
    if rejections is None:
        log_rejections(origin, destination, rejected)
    return sorted(offers.values(), key=lambda o: o['dep'])


def identity(o):
    return '|'.join(str(o[k]) for k in ('origin', 'destination', 'origin_airport',
        'destination_airport', 'dep', 'arr', 'airline', 'flight_number', 'transfers'))


class Client:
    def __init__(self, token, c, months):
        self.c, self.months = c, months
        self.session = requests.Session()
        self.session.headers['X-Access-Token'] = token
        self.session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
        self.cache = {}

    def raw(self, origin, destination, direct_only, one_way=True):
        key = origin, destination, direct_only, one_way
        if key in self.cache:
            return self.cache[key]
        items = []
        for month in self.months:
            for page in range(1, 101):
                response = self.session.get(API_URL, params=dict(origin=origin, destination=destination,
                    departure_at=month, currency=self.c['currency'], market=self.c['market'],
                    one_way=str(one_way).lower(), direct=str(direct_only).lower(), unique='false',
                    sorting='price', limit=1000, page=page), timeout=30)
                response.raise_for_status()
                payload = response.json()
                if payload.get('success') is not True or not isinstance(payload.get('data'), list):
                    raise RuntimeError(f'Некорректный ответ API: {origin} → {destination}, {month}')
                rows = payload['data']
                items.extend(rows)
                if len(rows) < 1000:
                    break
            else:
                raise RuntimeError('Превышен лимит страниц API; поиск неполон')
        self.cache[key] = items
        return self.cache[key]

    def fetch(self, origin, destination, direct_only):
        return normalize(self.raw(origin, destination, direct_only), origin, destination, self.c, direct_only)

    def roundtrips(self, origin, destination, direct_only):
        return normalize_roundtrips(self.raw(origin, destination, direct_only, False),
                                    origin, destination, self.c, direct_only)


def normalize_roundtrips(items, origin, destination, c, direct_only, now=None):
    """Keep the RT fare whole. Reverse airports/carrier are not supplied by this API."""
    result = []
    rejected = Counter()
    for item in items:
        if not item.get('return_at') or not item.get('duration_to') or not item.get('duration_back'):
            rejected['missing_round_trip_times'] += 1
            continue
        outbound = dict(item, return_at=None, duration=item['duration_to'])
        inbound = dict(item, origin=destination, destination=origin, origin_airport='', destination_airport='',
                       departure_at=item['return_at'], return_at=None, duration=item['duration_back'],
                       duration_to=item['duration_back'], transfers=item.get('return_transfers'),
                       airline='', flight_number='')
        out = normalize([outbound], origin, destination, c, direct_only, now, rejected)
        back = normalize([inbound], destination, origin, c, direct_only, now, rejected)
        if out and back:
            result.append((out[0], back[0]))
    log_rejections(origin, destination, rejected)
    return result

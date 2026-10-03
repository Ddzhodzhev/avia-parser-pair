"""Validated, editable search settings; credentials stay in the environment."""
import math
import os
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml

ROOT = Path(__file__).resolve().parent

def load_dotenv():
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


def load(path=ROOT / 'config.yaml'):
    with open(path, encoding='utf-8') as stream:
        c = yaml.safe_load(stream)
    if not isinstance(c, dict):
        raise ValueError('Конфиг должен быть YAML-объектом')
    for key in ('departure_start', 'departure_end'):
        c[key] = date.fromisoformat(str(c[key]))
    if c['departure_start'] > c['departure_end']:
        raise ValueError('departure_start должен быть не позже departure_end')
    if c.get('return_end'):
        c['return_end'] = date.fromisoformat(str(c['return_end']))
        if c['return_end'] < c['departure_start']:
            raise ValueError('return_end раньше departure_start')
    for key in ('min_trip_days', 'max_trip_days', 'max_alerts', 'max_leg_hours',
                'min_connection_hours_outbound', 'min_connection_hours_return',
                'airport_change_min_hours', 'max_connection_hours'):
        if type(c[key]) is not int or c[key] <= 0:
            raise ValueError(f'{key}: нужно положительное целое число')
    if c['min_trip_days'] > c['max_trip_days']:
        raise ValueError('min_trip_days > max_trip_days')
    if c.get('return_end') and c['departure_start'] + timedelta(days=c['min_trip_days']) > c['return_end']:
        raise ValueError('До return_end не помещается минимальная поездка')
    if max(c[k] for k in ('min_connection_hours_outbound', 'min_connection_hours_return',
                          'airport_change_min_hours')) > c['max_connection_hours']:
        raise ValueError('Минимальная пересадка больше максимальной')
    if c['currency'] != 'rub':
        raise ValueError('Пороги этого конфига заданы в рублях: currency должен быть rub')
    for city, details in c['cities'].items():
        if len(city) != 3 or not city.isupper():
            raise ValueError(f'Неверный IATA-код: {city}')
        ZoneInfo(details['timezone'])
    ids = set()
    for r in c['routes']:
        if r['id'] in ids:
            raise ValueError('Повторяющийся id маршрута')
        ids.add(r['id'])
        if type(r.get('enabled', True)) is not bool or type(r['direct_only']) is not bool:
            raise ValueError('enabled и direct_only должны быть true/false')
        cities = [c['origin'], r['destination']] + ([r['hub']] if r.get('hub') else [])
        if len(set(cities)) != len(cities) or any(x not in c['cities'] for x in cities):
            raise ValueError(f"Неверные города маршрута {r['id']}")
        if type(r['max_total_price']) not in (int, float) or not math.isfinite(r['max_total_price']) or r['max_total_price'] <= 0:
            raise ValueError('max_total_price должен быть положительным числом')
    return c


def search_months(c):
    # Include travel to Vietnam, stay, and the return connection, including month rollover.
    end = c['departure_end'] + timedelta(days=c['max_trip_days'] + math.ceil(
        (3 * c['max_leg_hours'] + 2 * c['max_connection_hours']) / 24) + 2)
    if c.get('return_end'):
        end = min(end, c['return_end'])
    cursor = c['departure_start'].replace(day=1)
    months = []
    while cursor <= end:
        months.append(cursor.strftime('%Y-%m'))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months

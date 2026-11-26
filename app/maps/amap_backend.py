"""Amap HTTP adapter shared by the backend and local validation tools."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from route_rules import AmapError, number, meaningful, metro_options, public_transport_options, walking_options, select_route

class AmapClient:
    def __init__(self, key, timeout=15, retries=2, transport=None, sleep=time.sleep):
        if not key:
            raise AmapError('PROVIDER_NOT_CONFIGURED')
        self._key, self.timeout, self.retries = key, timeout, retries
        self._transport, self._sleep = transport or self._http, sleep

    def _http(self, endpoint, params):
        query = urllib.parse.urlencode(dict(params, key=self._key, output='JSON'))
        with urllib.request.urlopen('https://restapi.amap.com/v3/' + endpoint + '?' + query,
                                    timeout=self.timeout) as response:
            return json.load(response)

    def request(self, endpoint, **params):
        for attempt in range(self.retries + 1):
            try:
                data = self._transport(endpoint, params)
                if data.get('status') == '1':
                    return data
                code = str(data.get('infocode', 'PROVIDER_ERROR'))
                # Short-period frequency and transient server errors only.
                retryable = code in {'10004', '10014', '10015', '10016', '10019', '10020', '10021', '10029'}
                error = AmapError('AMAP_' + code, retryable)
            except urllib.error.HTTPError as exc:
                error = AmapError('HTTP_' + str(exc.code), exc.code in {429, 500, 502, 503, 504})
            except (urllib.error.URLError, TimeoutError, OSError):
                error = AmapError('NETWORK_ERROR', True)
            except (ValueError, TypeError, AttributeError):
                error = AmapError('INVALID_RESPONSE', True)
            if not error.retryable or attempt == self.retries:
                raise error from None
            self._sleep(60 if error.code == 'AMAP_10004' else (1, 3)[min(attempt, 1)])

    def resolve_place(self, name, city='深圳'):
        data = self.request('place/text', keywords=name, city=city, citylimit='true', offset=10, page=1)
        items = data.get('pois') or []
        if not items:
            data = self.request('geocode/geo', address=name, city=city)
            items = data.get('geocodes') or []
        # Preserve ambiguous candidates; never silently pick the first.
        return {'candidates': items, 'quality': 'success' if len(items) == 1 else 'unconfirmed'}

    def station_catalog(self, city='深圳', max_pages=20):
        result = self._station_partition(city, max_pages)
        if result['coverage']['complete']:
            return result
        # City-wide POI queries may truncate well before count: split by official districts.
        geography = self.request('config/district', keywords=city, subdistrict=1, extensions='base')
        roots = geography.get('districts') or []
        children = roots[0].get('districts', []) if roots else []
        if not children:
            return result
        merged = {item['id']: item for item in result['stations']}
        partitions = []
        for district in children:
            code = district.get('adcode')
            if not code or district.get('level') != 'district':
                continue
            part = self._station_partition(code, max_pages)
            partitions.append({'adcode': code, 'name': district.get('name'), **part['coverage']})
            merged.update({item['id']: item for item in part['stations']})
        expected = result['coverage']['reported_count']
        complete = bool(partitions) and all(part['complete'] for part in partitions)
        complete = complete and (expected is not None and len(merged) >= expected)
        return {'stations': list(merged.values()), 'quality': 'success' if complete else 'partial',
                'coverage': {'complete': complete, 'reported_count': expected, 'unique_count': len(merged),
                             'method': 'city_then_administrative_districts', 'partitions': partitions,
                             'city_query_coverage': result['coverage']}}

    def _station_partition(self, city, max_pages):
        if max_pages < 1:
            raise ValueError('max_pages must be positive')
        stations, seen, positions, total = [], set(), 0, None
        complete = False
        for page in range(1, max_pages + 1):
            data = self.request('place/text', keywords='地铁站', types='150500', city=city,
                                citylimit='true', offset=25, page=page)
            items = data.get('pois') or []
            advertised = number(data.get('count'))
            # A later empty page may reset count to zero. It cannot erase prior coverage gaps.
            if advertised is not None:
                total = max(total or 0, advertised)
            new_ids = 0
            for item in items:
                identifier = item.get('id')
                if identifier and identifier not in seen:
                    seen.add(identifier)
                    new_ids += 1
                    stations.append(item)
            positions += len(items)
            # Empty/duplicate pages before the advertised total imply incomplete coverage.
            if total is not None and len(seen) >= total:
                complete = True
                break
            if not items or not new_ids:
                break
        return {'stations': stations, 'quality': 'success' if complete else 'partial',
                'coverage': {'complete': complete, 'reported_count': total,
                             'unique_count': len(seen), 'positions_read': positions, 'pages_read': page}}

    def walking(self, origin, destination):
        return self.request('direction/walking', origin=origin, destination=destination)

    @staticmethod
    def validate_context(context):
        from datetime import date
        if not context or context.get('departure_time') != '08:30' or context.get('timezone') != 'Asia/Shanghai' or context.get('direction') != 'to_target':
            raise AmapError('UNSUPPORTED_PLANNING_CONTEXT')
        try:
            if date.fromisoformat(context['reference_date']).weekday() >= 5 and context.get('calendar_policy') != 'actual_date':
                raise ValueError()
        except (KeyError, ValueError):
            raise AmapError('UNSUPPORTED_PLANNING_CONTEXT') from None

    def transit(self, origin, destination, city='深圳', *, planning_context, purpose='property'):
        self.validate_context(planning_context)
        if purpose not in ('property', 'station'):
            raise ValueError('Invalid route purpose')
        combined = []
        for strategy in (0, 1, 2, 3):
            data = self.request('direction/transit/integrated', origin=origin, destination=destination,
                                city=city, cityd=city, strategy=strategy, extensions='all',
                                date=planning_context['reference_date'], time=planning_context['departure_time'])
            combined.extend((data.get('route') or {}).get('transits') or [])
        return {'status': '1', 'route': {'transits': combined}, 'queried_strategies': [0, 1, 2, 3],
                'planning_context': planning_context, 'query_policy_version': 'v3-strategies-0123-v1.37'}

    def evaluate(self, origin, destination, commute_limit_sec, walk_limit_sec, *, planning_context, city='深圳'):
        from datetime import datetime, timezone
        self.validate_context(planning_context)
        walking = self.walking(origin, destination)
        transit = self.transit(origin, destination, city, planning_context=planning_context)
        result = select_route(walking, transit, commute_limit_sec, walk_limit_sec)
        result.update({key: planning_context[key] for key in ('reference_date', 'departure_time', 'timezone', 'direction')})
        result.update(queried_at=datetime.now(timezone.utc).isoformat(), origin=origin, destination=destination,
                      query_policy_version='v3-strategies-0123-v1.37')
        return result

    def station_plan(self, catalog, destination, commute_limit_sec, *, planning_context, city='深圳'):
        self.validate_context(planning_context)
        if type(commute_limit_sec) is not int or commute_limit_sec <= 0:
            raise ValueError('Station budget must be positive')
        eligible, errors = [], []
        for station in catalog['stations']:
            identifier, origin = station.get('id'), station.get('location')
            if not origin:
                errors.append({'poi_id': identifier, 'code': 'MISSING_LOCATION'})
                continue
            try:
                options = walking_options(self.walking(origin, destination))
                options += metro_options(self.transit(origin, destination, city, planning_context=planning_context, purpose='station'))
                qualifying = [item for item in options if item['duration_sec'] <= commute_limit_sec]
                if qualifying:
                    chosen = min(qualifying, key=lambda item: item['duration_sec'])
                    eligible.append({'poi_id': identifier, 'name': station.get('name'),
                                     'duration_sec': chosen['duration_sec'], 'route': chosen})
                elif not options:
                    errors.append({'poi_id': identifier, 'code': 'NO_VERIFIABLE_ROUTE'})
            except AmapError as exc:
                errors.append({'poi_id': identifier, 'code': exc.code})
        eligible.sort(key=lambda item: (item['duration_sec'], item['poi_id']))
        return {'stations': eligible, 'station_budget_sec': commute_limit_sec, 'errors': errors,
                'planning_context': planning_context,
                'quality': 'success' if catalog['coverage']['complete'] and not errors else 'partial'}



"""V1.37 route normalization: one complete route, two independent time limits."""
import hashlib
import json


class AmapError(RuntimeError):
    def __init__(self, code, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def number(value):
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def meaningful(value):
    if isinstance(value, dict):
        return any(meaningful(item) for item in value.values())
    if isinstance(value, list):
        return any(meaningful(item) for item in value)
    return value is not None and value != '' and value is not False


BUS_TYPES = {'普通公交线路', '公交线路', '公交专线', '旅游专线', '夜班车', '机场巴士', '机场大巴', 'BRT线路', '市郊公交线路'}
METRO_TYPES = {'地铁线路'}


def _id(route):
    signature = {key: route[key] for key in ('mode', 'duration_sec', 'total_walk_sec', 'segments')}
    return 'route_' + hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _check_response(response):
    if response.get('status') != '1':
        raise AmapError('INVALID_ROUTE_RESPONSE')


def walking_options(response):
    _check_response(response)
    options = []
    for route in (response.get('route') or {}).get('paths') or []:
        duration, distance = number(route.get('duration')), number(route.get('distance'))
        if duration is None:
            continue
        option = {'mode': 'walk', 'duration_sec': duration, 'total_walk_sec': duration,
                  'walking_distance_m': distance, 'transfer_count': 0, 'quality': 'success',
                  'segments': [{'mode': 'walk', 'duration_sec': duration, 'distance_m': distance,
                                'steps': route.get('steps') or []}]}
        option['route_id'] = _id(option)
        options.append(option)
    return options


def public_transport_options(response, *, station_only=False):
    _check_response(response)
    options = []
    for route in (response.get('route') or {}).get('transits') or []:
        duration = number(route.get('duration'))
        raw_segments = route.get('segments')
        if duration is None or not isinstance(raw_segments, list) or not raw_segments:
            continue
        segments, types, walk_sum, rides, valid = [], set(), 0, 0, True
        for segment in raw_segments:
            if meaningful(segment.get('taxi')) or meaningful(segment.get('railway')):
                valid = False
                break
            if 'walking' not in segment:
                valid = False
                break
            walking = segment['walking']
            if walking == []:
                walking_duration = 0  # Explicit empty transport segment, not a missing field.
            elif isinstance(walking, dict):
                walking_duration = number(walking.get('duration'))
            else:
                walking_duration = None
            if walking_duration is None:
                valid = False
                break
            walk_sum += walking_duration
            if isinstance(walking, dict):
                segments.append({'mode': 'walk', 'duration_sec': walking_duration,
                                 'distance_m': number(walking.get('distance')),
                                 'origin': walking.get('origin'), 'destination': walking.get('destination'),
                                 'steps': walking.get('steps') or []})
            lines = (segment.get('bus') or {}).get('buslines') or []
            if lines:
                # Alternatives in a leg are not separate transfers. Keep the first provider option.
                line = lines[0]
                kind = line.get('type')
                if kind in METRO_TYPES:
                    mode = 'metro'
                elif not station_only and kind in BUS_TYPES:
                    mode = 'bus'
                else:
                    valid = False
                    break
                types.add(mode)
                rides += 1
                segments.append({'mode': mode, 'line_name': line.get('name'), 'line_id': line.get('id'),
                                 'departure_stop': line.get('departure_stop'), 'arrival_stop': line.get('arrival_stop'),
                                 'duration_sec': number(line.get('duration')), 'via_num': number(line.get('via_num')),
                                 'via_stops': line.get('via_stops') or []})
        if valid and rides and walk_sum <= duration:
            option = {'mode': 'bus_metro' if len(types) > 1 else next(iter(types)),
                      'duration_sec': duration, 'total_walk_sec': walk_sum,
                      'walking_distance_m': number(route.get('walking_distance')),
                      'transfer_count': rides - 1, 'segments': segments, 'quality': 'success'}
            option['route_id'] = _id(option)
            options.append(option)
    return options


def metro_options(response):
    """Station discovery only; property routes must use public_transport_options."""
    return public_transport_options(response, station_only=True)


def select_route(walking, transit, commute_limit_sec, walk_limit_sec, waived=()):
    if type(commute_limit_sec) is not int or commute_limit_sec <= 0 or type(walk_limit_sec) is not int or walk_limit_sec < 0:
        raise ValueError('Invalid commute limits')
    options, seen = [], set()
    for source in walking_options(walking) + public_transport_options(transit):
        if source['route_id'] in seen:
            continue
        seen.add(source['route_id'])
        option = dict(source)
        option.update(commute_pass=option['duration_sec'] <= commute_limit_sec,
                      walk_pass=option['total_walk_sec'] <= walk_limit_sec)
        option['joint_pass'] = option['commute_pass'] and option['walk_pass']
        options.append(option)
    eligible = [item for item in options if item['joint_pass']]
    default = min(eligible, key=lambda item: item['duration_sec']) if eligible else None
    adopted = [item for item in options if (item['commute_pass'] or 'commute' in waived) and
               (item['walk_pass'] or 'walk' in waived)]
    adopted = min(adopted, key=lambda item: item['duration_sec']) if adopted else None
    return {'schema_version': 1, 'route_policy_version': 'v1.37', 'routes': options,
            'selected_route_id': default['route_id'] if default else None,
            'adopted_route_id': adopted['route_id'] if adopted else None,
            'quality': 'success' if options else 'unconfirmed',
            'commute_limit_sec': commute_limit_sec, 'walk_limit_sec': walk_limit_sec}

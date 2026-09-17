"""Independent per-binding activity evidence engine. No calving labels are inputs."""
from __future__ import annotations
from dataclasses import asdict
import copy
import hashlib
import json
import math
from .protocol import Context, PacketError, decode_packet, timestamp
from .features import Config, packet_minutes, compute_features

VERSION = '1.0.0'
STATE_VERSION = 1
PHASES = ('prepartum', 'calving', 'postpartum')


def _episode():
    return {'active': False, 'last_valid_ms': None, 'last_event_ms': None, 'last_event_id': None}


class ActivityModule:
    """One instance per cow/device/pregnancy binding; calls must be serialized.

    process_packet accepts complete V2 recordings with explicit server receipt
    and evaluation times. snapshot never fabricates new activity observations
    or new evidence events. Rebinding requires a fresh instance.
    """

    def __init__(self, context: Context, config: Config = None):
        if not isinstance(context, Context):
            raise TypeError('context must be Context')
        if config is not None and not isinstance(config, Config):
            raise TypeError('config must be Config')
        self.context = context
        self.config = config or Config()
        self._minutes = []
        self._first_minute_end_ms = None
        self._last_update = None
        self._features = None
        self._seen = {}
        self._phase = 'prepartum'
        self._last_evaluated_ms = context.binding_start_ms
        self._episodes = {'elevated': _episode(), 'strong': _episode()}

    def _check_now(self, now_ms):
        now = timestamp(now_ms, 'evaluated_at_ms')
        if now < self._last_evaluated_ms:
            raise PacketError('NONMONOTONIC_EVALUATION', 'evaluation time cannot go backwards')
        return now

    def _quality(self, now_ms):
        if self._phase != 'prepartum':
            return 'PHASE_INACTIVE'
        if self._last_update is None:
            return 'NO_DATA'
        if now_ms-self._last_update['data_through_ms'] > self.config.maximum_observation_age_minutes*60000:
            return 'STALE_DATA'
        f = self._features
        if f['coverage_1h'] < self.config.minimum_hour_coverage:
            return 'LOW_COVERAGE'
        if f['baseline_g'] is None:
            return 'INSUFFICIENT_HISTORY'
        if f['baseline_g'] < self.config.baseline_floor_g:
            return 'BASELINE_TOO_LOW'
        if f['activity_ratio'] is None:
            return 'INSUFFICIENT_HISTORY'
        return 'VALID' if f['reference_days'] else 'PROVISIONAL'

    def _render(self, now_ms, status, *, elevated_event=False, strong_event=False, input_uid=None):
        quality = self._quality(now_ms)
        usable = quality in ('VALID', 'PROVISIONAL')
        ratio = self._features['activity_ratio'] if usable else None
        level = ('STRONG' if ratio >= self.config.strong_evidence_ratio else
                 'ELEVATED' if ratio >= self.config.evidence_ratio else 'LOW') if usable else 'UNAVAILABLE'
        last = copy.deepcopy(self._last_update)
        return {'module': 'cowmata_activity_aux', 'version': VERSION,
                'cow_id': self.context.cow_id, 'device_id': self.context.device_id,
                'binding_id': self.context.binding_id, 'phase': self._phase,
                'evaluated_at_ms': now_ms, 'input_status': status, 'input_uid': input_uid,
                'quality_status': quality, 'usable_for_fusion': usable,
                'activity_ratio': ratio, 'activity_excess_ratio': ratio-1 if ratio is not None else None,
                'activity_evidence_level': level,
                'new_activity_evidence': bool(elevated_event),
                'new_strong_activity_evidence': bool(strong_event),
                'activity_event_id': self._episodes['elevated']['last_event_id'] if elevated_event else None,
                'strong_activity_event_id': self._episodes['strong']['last_event_id'] if strong_event else None,
                'data_through_ms': last['data_through_ms'] if last else None,
                'observation_age_seconds': (now_ms-last['data_through_ms'])/1000 if last else None,
                'maximum_observation_age_minutes': self.config.maximum_observation_age_minutes,
                'features': copy.deepcopy(self._features), 'last_update': last,
                'episodes': copy.deepcopy(self._episodes),
                'thresholds': {'elevated': self.config.evidence_ratio, 'strong': self.config.strong_evidence_ratio},
                'calving_probability': None, 'eta_hours': None}

    def _update_episode(self, name, threshold, now_ms, uid, valid):
        ep = self._episodes[name]
        last = ep['last_valid_ms']
        if last is None or now_ms-last > self.config.episode_reset_minutes*60000:
            ep['active'] = False
        if not valid:
            return False
        ep['last_valid_ms'] = now_ms
        flag = self._features['activity_ratio'] >= threshold
        due = ep['last_event_ms'] is None or now_ms-ep['last_event_ms'] >= self.config.episode_cooldown_minutes*60000
        new = flag and not ep['active'] and due
        if new:
            ep['last_event_ms'] = now_ms
            token = f'{self.context.binding_id}|{uid}|{name}|{now_ms}'
            ep['last_event_id'] = hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]
        ep['active'] = bool(flag)
        return bool(new)

    def process_packet(self, sensor_json, *, received_at_ms, evaluated_at_ms=None,
                       receive_time_source='server_received'):
        received = timestamp(received_at_ms, 'received_at_ms')
        now = self._check_now(received if evaluated_at_ms is None else evaluated_at_ms)
        if now < received:
            raise PacketError('CLOCK_ORDER', 'evaluation cannot precede receipt')
        if receive_time_source not in ('server_received', 'json_update_proxy'):
            raise ValueError('receive_time_source must be server_received or json_update_proxy')
        packet = decode_packet(sensor_json, self.context, max_duration_ms=self.config.max_packet_duration_ms)
        if packet.end_ms > received:
            raise PacketError('FUTURE_SAMPLES', 'packet contains samples later than receipt time')
        if packet.uid in self._seen:
            if self._seen[packet.uid] != packet.fingerprint:
                raise PacketError('UID_CONFLICT', 'same uid carries different IMU data or sampling origin')
            self._last_evaluated_ms = now
            return self._render(now, 'DUPLICATE_IGNORED', input_uid=packet.uid)
        if self._last_update and packet.start_ms <= self._last_update['data_through_ms']:
            self._last_evaluated_ms = now
            return self._render(now, 'LATE_OR_OVERLAP_IGNORED', input_uid=packet.uid)
        if self._last_update and received < self._last_update['received_at_ms']:
            raise PacketError('NONMONOTONIC_RECEIPT', 'new packet receipt precedes the last accepted packet')
        rows, diagnostics = packet_minutes(packet, self.config)
        merged = {r[0]: list(r) for r in self._minutes}
        for row in rows:
            if row[0] in merged:
                for col in range(1, 4):
                    merged[row[0]][col] += row[col]
            else:
                merged[row[0]] = row
        feature_end = rows[-1][0]
        cutoff = feature_end-self.config.history_retention_hours*3600000
        minutes = [merged[t] for t in sorted(merged) if t > cutoff]
        first = self._first_minute_end_ms if self._first_minute_end_ms is not None else rows[0][0]
        features = compute_features(minutes, feature_end_ms=feature_end, first_minute_end_ms=first, config=self.config)
        # Commit only after decoding, validation, aggregation and feature computation succeeded.
        self._minutes = minutes
        self._first_minute_end_ms = first
        self._features = features
        self._last_update = {'uid': packet.uid, 'received_at_ms': received,
            'receive_time_source': receive_time_source, 'sampling_start_ms': packet.start_ms,
            'data_through_ms': packet.end_ms, 'feature_window_end_ms': feature_end, **diagnostics}
        self._seen[packet.uid] = packet.fingerprint
        while len(self._seen) > self.config.uid_retention_count:
            del self._seen[next(iter(self._seen))]
        valid = self._quality(now) in ('VALID', 'PROVISIONAL')
        elevated = self._update_episode('elevated', self.config.evidence_ratio, now, packet.uid, valid)
        strong = self._update_episode('strong', self.config.strong_evidence_ratio, now, packet.uid, valid)
        self._last_evaluated_ms = now
        return self._render(now, 'ACCEPTED', elevated_event=elevated, strong_event=strong, input_uid=packet.uid)

    def snapshot(self, now_ms):
        now = self._check_now(now_ms)
        self._last_evaluated_ms = now
        return self._render(now, 'SNAPSHOT')

    def set_phase(self, phase, *, at_ms):
        now = self._check_now(at_ms)
        if phase not in PHASES or PHASES.index(phase) < PHASES.index(self._phase):
            raise ValueError('phase must move forward through prepartum, calving, postpartum; a new pregnancy requires a new binding')
        self._phase = phase
        if phase != 'prepartum':
            for ep in self._episodes.values():
                ep['active'] = False
        self._last_evaluated_ms = now
        return self._render(now, 'PHASE_CHANGED')

    def to_state_json(self):
        state = {'state_version': STATE_VERSION, 'module_version': VERSION,
                 'context': asdict(self.context), 'config': asdict(self.config), 'phase': self._phase,
                 'last_evaluated_ms': self._last_evaluated_ms, 'first_minute_end_ms': self._first_minute_end_ms,
                 'minutes': self._minutes, 'last_update': self._last_update,
                 'seen_packets': list(self._seen.items()), 'episodes': self._episodes}
        return json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(',', ':'))

    @classmethod
    def from_state_json(cls, state_text):
        try:
            state = json.loads(state_text)
            if state['state_version'] != STATE_VERSION or state['module_version'] != VERSION:
                raise ValueError('unsupported state version; replay prior packets with the installed version')
            obj = cls(Context(**state['context']), Config(**state['config']))
            if state['phase'] not in PHASES:
                raise ValueError('invalid phase')
            obj._phase = state['phase']
            obj._last_evaluated_ms = timestamp(state['last_evaluated_ms'])
            if obj._last_evaluated_ms < obj.context.binding_start_ms:
                raise ValueError('state evaluation precedes binding')
            rows = state['minutes']
            if not isinstance(rows, list) or len(rows) > obj.config.history_retention_hours*60+1:
                raise ValueError('invalid history size')
            prior = -1
            for row in rows:
                if len(row) != 4 or timestamp(row[0]) % 60000 or row[0] <= prior:
                    raise ValueError('invalid or unordered minute timestamps')
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v < 0 for v in row[1:]):
                    raise ValueError('invalid minute accumulators')
                if row[2] > row[3] or row[3] > 3100 or (row[2] == 0 and row[1] != 0):
                    raise ValueError('inconsistent minute counts')
                prior = row[0]
            obj._minutes = rows
            obj._last_update = state['last_update']
            obj._first_minute_end_ms = state['first_minute_end_ms']
            if bool(rows) != (obj._last_update is not None):
                raise ValueError('history and last_update must agree')
            if rows:
                first = timestamp(obj._first_minute_end_ms)
                last = obj._last_update
                if first % 60000 or first > rows[0][0] or rows[-1][0] != last['feature_window_end_ms']:
                    raise ValueError('inconsistent feature endpoints')
                if not (obj.context.binding_start_ms <= timestamp(last['sampling_start_ms']) <=
                        timestamp(last['data_through_ms']) <= timestamp(last['received_at_ms']) <= obj._last_evaluated_ms):
                    raise ValueError('inconsistent state time order')
                if not last['data_through_ms'] < rows[-1][0] <= last['data_through_ms']+60000:
                    raise ValueError('invalid final minute')
                obj._features = compute_features(rows, feature_end_ms=rows[-1][0], first_minute_end_ms=first, config=obj.config)
            elif obj._first_minute_end_ms is not None:
                raise ValueError('empty history cannot have a sampling origin')
            seen = state['seen_packets']
            if len(seen) > obj.config.uid_retention_count or len(dict(seen)) != len(seen):
                raise ValueError('invalid uid ledger')
            if any(not isinstance(uid,str) or not isinstance(digest,str) or len(digest) != 64 for uid,digest in seen):
                raise ValueError('invalid uid fingerprint')
            obj._seen = dict(seen)
            eps = state['episodes']
            if set(eps) != {'elevated', 'strong'}:
                raise ValueError('invalid episode state')
            for ep in eps.values():
                if set(ep) != set(_episode()) or not isinstance(ep['active'], bool):
                    raise ValueError('invalid episode fields')
                for field in ['last_valid_ms','last_event_ms']:
                    if ep[field] is not None and not obj.context.binding_start_ms <= timestamp(ep[field]) <= obj._last_evaluated_ms:
                        raise ValueError('invalid episode time')
                if (ep['last_event_ms'] is None) != (ep['last_event_id'] is None):
                    raise ValueError('inconsistent event identifier')
            obj._episodes = eps
            return obj
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ValueError(f'invalid activity state: {exc}') from exc


def as_fusion_features(result, *, now_ms=None):
    """Compact continuous features; unavailable activity must not veto other modalities.

    Supplying current time also invalidates a cached result once its actual
    observation becomes stale. No heuristic probability or unvalidated weight.
    """
    now = timestamp(result['evaluated_at_ms'] if now_ms is None else now_ms, 'now_ms')
    if now < result['evaluated_at_ms']:
        raise ValueError('now_ms cannot precede the result evaluation time')
    data_end = result['data_through_ms']
    age = (now-data_end)/1000 if data_end is not None else None
    usable = bool(result['usable_for_fusion'] and age is not None and
                  age <= result['maximum_observation_age_minutes']*60 and result['phase'] == 'prepartum')
    f = result['features'] or {}
    return {'activity_available': usable,
        'activity_ratio': result['activity_ratio'] if usable else None,
        'activity_excess_ratio': result['activity_excess_ratio'] if usable else None,
        'activity_level': result['activity_evidence_level'] if usable else 'UNAVAILABLE',
        'activity_coverage_1h': f.get('coverage_1h', 0.),
        'activity_reference_quality': f.get('reference_quality', 'INSUFFICIENT_HISTORY'),
        'activity_reference_days': f.get('reference_days', 0),
        'activity_reference_valid_hours': f.get('reference_valid_hours', 0.),
        'activity_observation_age_seconds': age}

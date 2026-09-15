"""Shared, label-free event features; one-second summaries never bridge gaps."""
from __future__ import annotations

import numpy as np

FEATURE_VERSION = 'event-shape-1'


def _unit(a):
    return a/np.maximum(np.linalg.norm(a, axis=-1, keepdims=True), 1e-12)


def _angle(a, b):
    return np.degrees(np.arccos(np.clip(np.sum(_unit(a)*_unit(b), axis=-1), -1, 1)))


def _mean(x, lo, hi):
    n = len(x)
    a, b = np.clip(np.arange(n)+lo, 0, n), np.clip(np.arange(n)+hi, 0, n)
    finite = np.isfinite(x)
    row_valid = finite if x.ndim == 1 else finite.all(axis=1)
    lower = np.maximum.accumulate(np.where(~row_valid, np.arange(n)+1, 0))
    upper = np.minimum.accumulate(np.where(~row_valid, np.arange(n), n)[::-1])[::-1]
    a = np.maximum(a, lower)
    b = np.maximum(a, np.minimum(b, upper))
    sums = np.concatenate([np.zeros_like(x[:1]), np.cumsum(np.where(finite, x, 0), axis=0)])
    counts = np.concatenate([np.zeros_like(x[:1]), np.cumsum(finite, axis=0)])
    count = counts[b]-counts[a]
    return np.divide(sums[b]-sums[a], count, out=np.full_like(x, np.nan), where=count > 0)


def second_features(times_ms, acceleration_g, gyro_dps, *, valid_samples=None):
    t = np.asarray(times_ms, dtype=float)
    acc, gyro = np.asarray(acceleration_g, dtype=float), np.asarray(gyro_dps, dtype=float)
    if len(t) < 2 or acc.shape != (len(t), 3) or gyro.shape != acc.shape:
        raise ValueError('Expected timestamped three-axis acceleration and angular velocity')
    if not np.isfinite(t).all() or t[0] < 0 or np.any(np.diff(t) <= 0) or t[-1] > 7200000:
        raise ValueError('Nonmonotonic or overlong sensor record')
    sec = np.floor(t/1000).astype(int)
    n = int(sec[-1])+1
    sample_valid = np.isfinite(acc).all(axis=1) & np.isfinite(gyro).all(axis=1)
    if valid_samples is not None:
        sample_valid &= np.asarray(valid_samples, dtype=bool)
    count = np.bincount(sec[sample_valid], minlength=n).astype(float)
    dt = np.diff(t)
    expected_hz = 1000/float(np.median(dt[dt <= 100])) if np.any(dt <= 100) else 50.
    coverage = np.clip(count/expected_hz, 0, 1)
    valid = coverage >= .8
    for left, right in zip(t[:-1][dt > 100], t[1:][dt > 100]):
        valid[int(left//1000):int(right//1000)+1] = False

    def bins(values):
        x = np.asarray(values)
        if x.ndim == 1:
            x = x[:, None]
        mean, deviation = [], []
        for column in x.T:
            values = column[sample_valid]
            mu = np.bincount(sec[sample_valid], weights=values, minlength=n)/np.maximum(count, 1)
            sq = np.bincount(sec[sample_valid], weights=values*values, minlength=n)/np.maximum(count, 1)
            mean.append(mu)
            deviation.append(np.sqrt(np.maximum(0., sq-mu*mu)))
        return np.asarray(mean).T, np.asarray(deviation).T

    a, sd = bins(acc)
    g, gsd = bins(gyro)
    norm = np.linalg.norm(a, axis=1)
    dynamic = np.linalg.norm(sd, axis=1)/np.maximum(norm, .05)
    energy = np.sqrt(np.sum(g*g+gsd*gsd, axis=1))
    orientation = _unit(a)
    for x in (a, g, orientation, norm, dynamic, energy):
        x[~valid] = np.nan
    columns, reference = {}, {}
    for name, lo, hi in [('pre', -8, -2), ('core', -2, 3), ('post', 3, 9), ('long', -20, 21)]:
        direction = _mean(orientation, lo, hi)
        reference[name] = _unit(direction)
        columns[name+'_orientation_noise'] = np.maximum(0., 1-np.linalg.norm(direction, axis=1))
        for key, values in [('gyro', energy), ('dynamic', dynamic), ('acc_norm', norm)]:
            avg = _mean(values, lo, hi)
            columns[name+'_'+key] = avg
            columns[name+'_'+key+'_sd'] = np.sqrt(np.maximum(0., _mean(values*values, lo, hi)-avg*avg))
        columns[name+'_gravity_parallel_gyro'] = np.sum(_mean(g, lo, hi)*reference[name], axis=1)
    columns['orientation_change_deg'] = _angle(reference['pre'], reference['post'])
    columns['signed_z_change'] = reference['post'][:, 2]-reference['pre'][:, 2]
    columns['orientation_retention_deg'] = _angle(reference['post'], _mean(orientation, 9, 15))
    for key in ('gyro', 'dynamic'):
        eps = .01 if key == 'dynamic' else 1.
        columns[key+'_burst_logratio'] = np.log((columns['core_'+key]+eps)/(columns['pre_'+key]+eps))
        columns[key+'_settle_logratio'] = np.log((columns['post_'+key]+eps)/(columns['core_'+key]+eps))
        columns[key+'_cv_long'] = columns['long_'+key+'_sd']/(columns['long_'+key]+eps)
    turns = np.r_[np.nan, _angle(orientation[1:], orientation[:-1])]
    columns['direction_path_deg'] = _mean(turns, -2, 4)*6
    columns['direction_efficiency'] = columns['orientation_change_deg']/(columns['direction_path_deg']+1)
    # Envelope correlation represents repeated bouts without assuming a fixed frequency.
    for lag in (1, 2, 4):
        shifted = np.full_like(energy, np.nan)
        if lag < len(energy):
            shifted[lag:] = energy[:-lag]
        numerator = _mean(energy*shifted, -10, 11)-_mean(energy, -10, 11)*_mean(shifted, -10, 11)
        denominator = np.sqrt(np.maximum(0., _mean(energy*energy, -10, 11)-_mean(energy, -10, 11)**2)
                              *np.maximum(0., _mean(shifted*shifted, -10, 11)-_mean(shifted, -10, 11)**2))
        columns[f'gyro_repeat_{lag}s'] = numerator/np.maximum(denominator, 1.)
    valid_context = np.zeros(n, dtype=bool)
    edge = np.diff(np.r_[False, valid, False].astype(int))
    segments = []
    for lo, hi in zip(np.flatnonzero(edge == 1), np.flatnonzero(edge == -1)):
        segments.append([int(lo*1000), int(hi*1000)])
        if hi-lo >= 18:
            valid_context[lo+8:hi-9] = True
    # Even extended features are unusable when any part crosses a missing bin.
    for key, values in columns.items():
        if key.startswith('long_') or key.endswith('_long'):
            continue
        values[~valid_context] = np.nan
    x = np.column_stack(list(columns.values())).astype(np.float32)
    x[~valid] = np.nan
    return dict(X=x, names=list(columns), valid=valid, valid_context=valid_context,
        seconds=np.arange(n, dtype=float)+.5, coverage=coverage, segments=segments,
        gyro=energy, dynamic=dynamic, orientation=orientation,
        observed_seconds=float(coverage[valid].sum()), feature_version=FEATURE_VERSION)

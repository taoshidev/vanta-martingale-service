"""Martingale detection core: pure, dependency-free, fully parameterized.

The detection rule is documented in the README ("How detection works"). This module only
implements it and hardcodes no thresholds -- every knob is an argument.

Input, per pair: a list of ``Step(t_ms, exposure, pnl)``. ``exposure`` is net position size in
a QUANTITY unit (only its magnitude is used); only the SIGN of ``pnl`` matters (< 0 =
underwater). The core is scale-invariant -- it compares exposure ratios against a floor that
is a fraction of ``max_size`` -- so any consistent unit works. Building ``Step``s from a data
source is the caller's job (see ``vanta_adapter``).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    """One point in a pair's timeline, normalized for the detection core.

    ``t_ms``      -- placement time; steps are sorted by it (stable on ties).
    ``exposure``  -- net position size after this order (QUANTITY unit; magnitude is used).
    ``pnl``       -- cumulative P&L when the order is placed; only its SIGN is used (< 0 = underwater).
    ``order_id``  -- optional caller reference, passed through for reporting; not used by the math.
    """
    t_ms: int
    exposure: float
    pnl: float
    order_id: object = None


def _first_escalating_chain(exposures, escalation_factor, target_len, eligible=None):
    """Indices (in time order) of the FIRST escalating chain to reach ``target_len`` -- each
    element ``>= escalation_factor x`` a previous chain element -- or ``None``. O(n^2) dynamic
    program with predecessor tracking (streaks are short). ``eligible`` (optional bool list)
    restricts which steps may serve as chain levels."""
    chain = [1] * len(exposures)
    prev = [-1] * len(exposures)
    for i in range(len(exposures)):
        if eligible is not None and not eligible[i]:
            continue
        for j in range(i):
            if eligible is not None and not eligible[j]:
                continue
            if exposures[i] >= escalation_factor * exposures[j] and chain[j] + 1 > chain[i]:
                chain[i] = chain[j] + 1
                prev[i] = j
        if chain[i] >= target_len:
            out, k = [], i
            while k != -1:
                out.append(k)
                k = prev[k]
            return out[::-1]
    return None


def _escalating_chain_ends(exposures, escalation_factor, target_len, eligible=None):
    """Indices i where the longest escalating chain ending at i reaches ``target_len`` -- every
    such order completes/extends a length->=target_len escalating chain (a "trigger"). Same O(n^2)
    DP as ``_first_escalating_chain`` but collects EVERY qualifying end, not just the first."""
    chain = [1] * len(exposures)
    ends = []
    for i in range(len(exposures)):
        if eligible is not None and not eligible[i]:
            continue
        for j in range(i):
            if eligible is not None and not eligible[j]:
                continue
            if exposures[i] >= escalation_factor * exposures[j] and chain[j] + 1 > chain[i]:
                chain[i] = chain[j] + 1
        if chain[i] >= target_len:
            ends.append(i)
    return ends


def _floored(streak, floor, floor_mode):
    """``(exposures, eligible)`` for one streak under a floor mode:

    ``"clip"``    -- below-floor exposures are raised TO the floor (a close at 0 becomes a low level).
    ``"exclude"`` -- below-floor exposures keep their value but may not be chain levels (they still
                     count for pnl). Keeps dust and closes from padding the ladder.
    """
    raws = [abs(s.exposure) for s in streak]
    if floor_mode == "clip":
        return [max(floor, r) for r in raws], None
    if floor_mode == "exclude":
        return raws, [r >= floor for r in raws]
    raise ValueError(f"unknown floor_mode {floor_mode!r} (use 'clip' or 'exclude')")


def _underwater_streaks(steps):
    """Split chronologically-ordered ``steps`` into maximal runs with ``pnl < 0``. A step at
    breakeven-or-better (``pnl >= 0``) ends the current streak."""
    streaks, current = [], []
    for s in steps:
        if s.pnl < 0:
            current.append(s)
        elif current:
            streaks.append(current)
            current = []
    if current:
        streaks.append(current)
    return streaks


def pair_martingale_chain(steps, max_size, escalation_factor, floor_fraction, chain_length,
                          floor_mode="clip"):
    """The ``Step``s of the FIRST escalating underwater chain that flags this pair (in time
    order), or ``None`` if the pair is not a martingale. The last element is the order that
    completed the chain (the trigger). Args as in ``pair_is_martingale``.
    """
    if max_size <= 0.0:
        return None
    floor = floor_fraction * max_size
    ordered = sorted(steps, key=lambda s: s.t_ms)      # walk chronologically (stable on ties)
    for streak in _underwater_streaks(ordered):
        if len(streak) < chain_length:
            continue
        exposures, eligible = _floored(streak, floor, floor_mode)
        idx = _first_escalating_chain(exposures, escalation_factor, chain_length, eligible)
        if idx is not None:
            return [streak[k] for k in idx]
    return None


def pair_is_martingale(steps, max_size, escalation_factor, floor_fraction, chain_length,
                       floor_mode="clip"):
    """Verdict for ONE pair: True iff some underwater streak contains a ``chain_length`` chain
    where each level is ``>= escalation_factor x`` a previous one (after flooring).

    ``steps`` are the pair's ``Step``s; ``max_size`` is in the same unit as ``exposure`` and
    the floor is ``floor_fraction`` x it; ``floor_mode`` is ``"clip"`` or ``"exclude"`` (see
    ``_floored``). These args are shared by the other ``pair_*`` functions below.
    """
    return pair_martingale_chain(steps, max_size, escalation_factor,
                                 floor_fraction, chain_length, floor_mode) is not None


def pair_martingale_triggers(steps, max_size, escalation_factor, floor_fraction, chain_length,
                             floor_mode="clip"):
    """Every triggering ``Step`` for one pair, in time order: an order at which the longest
    escalating chain within its underwater streak (after flooring) reaches ``chain_length``. A
    clean doubling run of k levels yields ``k - chain_length + 1`` triggers; empty if not a
    martingale. Args as in ``pair_is_martingale``."""
    if max_size <= 0.0:
        return []
    floor = floor_fraction * max_size
    ordered = sorted(steps, key=lambda s: s.t_ms)      # walk chronologically (stable on ties)
    triggers = []
    for streak in _underwater_streaks(ordered):
        if len(streak) < chain_length:
            continue
        exposures, eligible = _floored(streak, floor, floor_mode)
        triggers.extend(streak[i] for i in
                        _escalating_chain_ends(exposures, escalation_factor, chain_length, eligible))
    return triggers


def trader_is_martingale(steps_by_pair, max_size_by_pair, escalation_factor,
                         floor_fraction, chain_length, floor_mode="clip"):
    """Verdict for a trader: True iff ANY pair is a martingale strategy.

    ``steps_by_pair``    -- ``{pair_id: [Step, ...]}``.
    ``max_size_by_pair`` -- ``{pair_id: max_size}`` in the same unit as that pair's exposures.
    Remaining args are forwarded to ``pair_is_martingale``.
    """
    return any(
        pair_is_martingale(steps, max_size_by_pair.get(pair, 0.0),
                           escalation_factor, floor_fraction, chain_length, floor_mode)
        for pair, steps in steps_by_pair.items()
    )


# ------------------------------------------------------------------ service additions
# Helpers for the live service to reconstruct the ladder ONE specific order completed (the
# audit trail stored with each trigger). Same DP and eligibility rules as the core above.


def _chain_ending_at(exposures, escalation_factor, target_len, eligible, end):
    """Indices (in time order) of a longest escalating chain ending exactly at ``end``, or
    ``None`` if none there reaches ``target_len``. Same DP as ``_escalating_chain_ends`` (an
    index qualifies here exactly when that function lists it), read at ``end``."""
    if eligible is not None and not eligible[end]:
        return None
    chain = [1] * (end + 1)
    prev = [-1] * (end + 1)
    for i in range(end + 1):
        if eligible is not None and not eligible[i]:
            continue
        for j in range(i):
            if eligible is not None and not eligible[j]:
                continue
            if exposures[i] >= escalation_factor * exposures[j] and chain[j] + 1 > chain[i]:
                chain[i] = chain[j] + 1
                prev[i] = j
    if chain[end] < target_len:
        return None
    out, k = [], end
    while k != -1:
        out.append(k)
        k = prev[k]
    return out[::-1]


def pair_trigger_chain(steps, max_size, escalation_factor, floor_fraction, chain_length,
                       floor_mode="clip", *, order_id):
    """The escalating underwater ladder COMPLETED by the step with this ``order_id`` (time
    order, last element = that step), or ``None`` if it completes no ``chain_length`` ladder.
    A step qualifies here exactly when ``pair_martingale_triggers`` lists it. Args as in
    ``pair_is_martingale``; ``order_id`` must be set and unique within ``steps``."""
    if max_size <= 0.0:
        return None
    floor = floor_fraction * max_size
    ordered = sorted(steps, key=lambda s: s.t_ms)      # walk chronologically (stable on ties)
    for streak in _underwater_streaks(ordered):
        end = next((i for i, s in enumerate(streak) if s.order_id == order_id), None)
        if end is None:
            continue
        if len(streak) < chain_length:
            return None                                # its streak is too short for any chain
        exposures, eligible = _floored(streak, floor, floor_mode)
        idx = _chain_ending_at(exposures, escalation_factor, chain_length, eligible, end)
        return [streak[k] for k in idx] if idx else None
    return None                                        # not underwater at that order -> no chain

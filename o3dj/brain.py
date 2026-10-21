"""The DJ's judgement: atmosphere in, targets and track picks out.

Pure functions (no I/O) so they are easy to test and tune.
"""

import math
import random
from dataclasses import asdict, dataclass, field


@dataclass
class Inputs:
    genres: list = field(default_factory=lambda: ["chill"])
    moods: bool = True                # add genres for the time of day and weather (config `moods`)
    picks: dict = field(default_factory=dict)  # genre -> mood slot it was switched on in, with moods on (see targets)
    weather: str = "clear"            # clear | cloudy | rain
    occupancy: int = 50               # 0..100 %
    occupancy_enabled: bool = False
    hour_override: float | None = None  # None = follow the clock
    auto: bool = True                 # auto volume from atmosphere
    manual_volume: int = 30           # used when auto is off
    manual_since: float | None = None  # when the volume was set by hand (controller or Sonos app); it expires
    volume_trim: int = 0              # added to auto volume
    energy_trim: float = 0.0          # added to target energy

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        known = {k: v for k, v in (d or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def interp_curve(curve, hour):
    """Linear interpolation over [[hour, energy, volume, label], ...]."""
    hour = hour % 24
    for (h0, e0, v0, label), (h1, e1, v1, _) in zip(curve, curve[1:]):
        if h0 <= hour <= h1:
            t = 0 if h1 == h0 else (hour - h0) / (h1 - h0)
            return e0 + (e1 - e0) * t, v0 + (v1 - v0) * t, label
    return curve[-1][1], curve[-1][2], curve[-1][3]


def mood_daypart(cfg, hour):
    """Which `moods` daypart an hour falls in (mood_hours: [[start_hour, name], ...], wrapping at midnight)."""
    hours = cfg.get("mood_hours") or []
    if not hours:
        return None
    name = hours[-1][1]
    for start, label in hours:
        if hour % 24 >= start:
            name = label
    return name


def mood_until(cfg, hour):
    """The hour the current `moods` slot ends (when staff genre picks made in it expire)."""
    starts = sorted(h for h, _ in cfg.get("mood_hours") or [])
    if not starts:
        return None
    later = [h for h in starts if h > hour % 24]
    return later[0] if later else starts[0]


def mood_progress(cfg, hour):
    """How far through its mood daypart an hour is, 0..1 (0 = the part just started)."""
    hours = sorted(cfg.get("mood_hours") or [], key=lambda x: x[0])
    if not hours:
        return 0.5
    h = hour % 24
    starts = [x[0] for x in hours]
    i = max((j for j, st in enumerate(starts) if h >= st), default=len(starts) - 1)
    start, end = starts[i], starts[(i + 1) % len(starts)]
    length = (end - start) % 24 or 24
    return ((h - start) % 24) / length


def new_music_share(cfg, mood, counts=None):
    """Share of picks for the slot's new genres: start_share with an empty library, rising linearly to full_share
    as the slot's genres fill up to full_at_tracks songs each (counts unknown -> start_share)."""
    nm = cfg.get("new_music") or {}
    start, full = nm.get("start_share", 1.0), nm.get("full_share", 1.0)
    if counts is None or not mood:
        return start
    need = nm.get("full_at_tracks", 30)
    new = {g: w for g, w in mood.items() if cfg.get("genres", {}).get(g, {}).get("fallback")}  # e.g. not chill
    if not new:
        return full
    fill = sum(w * min(1.0, counts.get(g, 0) / need) for g, w in new.items()) / sum(new.values())
    return start + (full - start) * fill


def volume_floor(cfg, occupancy):
    """The minimum auto volume for how full the room is, or None below the first point."""
    pts = sorted(cfg.get("occupancy", {}).get("volume_floor", []))
    if not pts or occupancy < pts[0][0]:
        return None
    for (o0, v0), (o1, v1) in zip(pts, pts[1:]):
        if occupancy <= o1:
            return v0 + (v1 - v0) * (occupancy - o0) / (o1 - o0)
    return pts[-1][1]


def occupancy_band(cfg, occupancy):
    """The occupancy band the room is in: {lo, hi, energy, bpm, instrumental, label}, or None."""
    for lo, hi, energy, bpm, instrumental, *label in cfg.get("occupancy", {}).get("bands", []):
        if lo <= occupancy < hi:
            return {"lo": lo, "hi": hi, "energy": energy, "bpm": bpm, "instrumental": instrumental,
                    "label": label[0] if label else ""}
    return None


def targets(inputs: Inputs, cfg, hour_now: float, all_genres, calibration=None, counts=None):
    """counts: {genre: number of tracks}; when given, genres with too few tracks hand weight to their fallback."""
    hour = inputs.hour_override if inputs.hour_override is not None else hour_now
    energy, volume, daypart = interp_curve(cfg["daypart_curve"], hour)
    reasons = [f"{daypart} {int(hour):02d}:{int(hour % 1 * 60):02d} -> energy {energy:.2f}, vol {volume:.0f}"]

    # A calibrated venue (see calibrate.py) replaces the default weather/occupancy numbers with its own fit.
    model = (calibration or {}).get("model")

    selected = [g for g in inputs.genres if g in all_genres] or list(all_genres)
    weights = {g: 1.0 for g in selected}
    sel_unit = 1.0  # weight of one selected genre; the new-music share shrinks it
    added = {}  # weight the moods matrix brought in (staff-selected weight is never handed to a fallback)

    # Genre complements time and weather: the moods matrix brings in each slot's (new) genres alongside the
    # selection, at a share that grows from new_music.start_share to full_share as the slot's library fills.
    moods = cfg.get("moods") or {}
    part = mood_daypart(cfg, hour)
    if inputs.moods and moods.get("enabled", True) and part:
        slot = moods.get(part, {})
        mood = {g: w for g, w in slot.get(inputs.weather, slot.get("clear", {})).items() if g in all_genres and w > 0}
        if mood:
            share = new_music_share(cfg, mood, counts)
            sel_total, mood_total = sum(weights.values()) or 1, sum(mood.values())
            weights = {g: v / sel_total * (1 - share) for g, v in weights.items()}
            sel_unit = (1 - share) / sel_total
            for g, v in mood.items():
                added[g] = v / mood_total * share
                weights[g] = weights.get(g, 0.0) + added[g]
            reasons.append(f"{part.replace('_', ' ')}, {inputs.weather} -> " + "/".join(all_genres.get(g, g) for g in mood)
                           + f" ({share:.0%} new music)")
            # Genres staff switched on during this slot get a guaranteed share (moods.pick_share) until the slot ends,
            # even when the mood styles would otherwise take every pick.
            picked = [g for g in selected if g in (inputs.picks or {})]
            if picked:
                ps = moods.get("pick_share", 0.3)
                weights = {g: v * (1 - ps) for g, v in weights.items()}
                added = {g: v * (1 - ps) for g, v in added.items()}
                sel_unit *= (1 - ps)
                for g in picked:
                    weights[g] = weights.get(g, 0.0) + ps / len(picked)
                reasons.append("staff picks " + "/".join(all_genres.get(g, g) for g in picked) + f" ({ps:.0%})")

    if counts is not None:
        _fallback(weights, added, cfg, counts, all_genres, reasons)

    w = cfg["weather"].get(inputs.weather, cfg["weather"]["clear"])
    if not model:
        energy += w["energy"]
        volume += w["volume"]
    for g, boost in w.get("boost", {}).items():
        if g not in all_genres:
            continue
        if g in weights:
            weights[g] *= boost
        elif boost >= 2:  # strong weather mood pulls the genre in even if unselected
            weights[g] = cfg.get("unselected_boost_base", 0.35) * boost * sel_unit
    if inputs.weather != "clear":
        parts = [f"{inputs.weather}"]
        if w["energy"] and not model:
            parts.append(f"energy {w['energy']:+.2f}")
        if w["volume"] and not model:
            parts.append(f"vol {w['volume']:+d}")
        if w.get("boost"):
            parts.append("+" + "/".join(all_genres.get(g, g) for g in w["boost"] if g in weights))
        reasons.append(" ".join(parts))

    if model:
        from .calibrate import effects
        # Occupancy moves energy through the bands below, so only weather feeds the calibrated energy.
        de = effects(model, inputs.weather, 50, False)[0]
        dv = effects(model, inputs.weather, inputs.occupancy, inputs.occupancy_enabled)[1]
        energy += de
        volume += dv
        occ_txt = f", {inputs.occupancy}% full" if inputs.occupancy_enabled else ""
        reasons.append(f"calibrated ({inputs.weather}{occ_txt}) -> energy {de:+.2f}, vol {dv:+.0f}")
    elif inputs.occupancy_enabled:
        # Volume counters the crowd: it rises as the room fills, to sit above the chatter.
        occ = cfg["occupancy"]
        f = _clip((inputs.occupancy - occ["threshold"]) / (100 - occ["threshold"]), 0, 1)
        if f > 0:
            volume += occ["max_volume"] * f
            reasons.append(f"busy {inputs.occupancy}% -> vol {occ['max_volume'] * f:+.0f}")

    # Time-of-day volume rules for a quiet room, e.g. afternoons under 60% full play 5 steps quieter.
    if inputs.occupancy_enabled:
        for rule in cfg.get("occupancy", {}).get("volume_rules", []):
            if mood_daypart(cfg, hour) == rule.get("part") and inputs.occupancy < rule.get("below", 0):
                volume += rule["volume"]
                reasons.append(f"{rule['part'].replace('_', ' ')} under {rule['below']}% full -> vol {rule['volume']:+d}")

    # A busy room needs a minimum level to sit above the chatter, whatever the time of day: occupancy.volume_floor
    # [[percent_full, volume], ...], interpolated between points and flat after the last (none below the first).
    floor = volume_floor(cfg, inputs.occupancy) if inputs.occupancy_enabled else None
    if floor is not None and volume < floor:
        reasons.append(f"{inputs.occupancy}% full -> at least vol {floor:.0f}")
        volume = floor

    # Energy and tempo meet the room's need: lift an empty cafe, calm a packed one.
    bpm_shift, instrumental = 0, False
    band = occupancy_band(cfg, inputs.occupancy) if inputs.occupancy_enabled else None
    if band:
        energy += band["energy"]
        bpm_shift, instrumental = band["bpm"], band["instrumental"]
        if band["energy"] or band["bpm"] or instrumental:
            parts = [f"energy {band['energy']:+.2f}"] if band["energy"] else []
            if band["bpm"]:
                parts.append(f"{band['bpm']:+d} BPM")
            if instrumental:
                parts.append("instrumental")
            label = f" ({band['label']})" if band["label"] else ""
            reasons.append(f"{band['lo']}-{min(band['hi'], 100)}% full{label} -> " + ", ".join(parts))

    if inputs.energy_trim:
        energy += inputs.energy_trim
        reasons.append(f"energy trim {inputs.energy_trim:+.2f}")

    volume_base = volume
    if inputs.auto:
        volume += inputs.volume_trim
        if inputs.volume_trim:
            reasons.append(f"vol trim {inputs.volume_trim:+d}")
    else:
        volume = inputs.manual_volume
        reasons.append("manual volume")

    # A genre's target tempo is the middle of its range, or with bpm_ramp it climbs from low to high across the daypart.
    bpm = {}
    for g in weights:
        gc = cfg.get("genres", {}).get(g, {})
        if gc.get("bpm"):
            lo, hi = gc["bpm"]
            pos = mood_progress(cfg, hour) if gc.get("bpm_ramp") else 0.5
            bpm[g] = round(lo + (hi - lo) * pos + bpm_shift, 1)

    total = sum(weights.values()) or 1
    return {
        "hour": round(hour, 2),
        "clock": inputs.hour_override is None,
        "daypart": daypart,
        "mood_daypart": part,
        "mood_until": mood_until(cfg, hour),
        "energy": round(_clip(energy, 0.05, 0.95), 3),
        "volume": int(round(_clip(volume, cfg["min_volume"], cfg["max_volume"]))),
        "volume_base": round(volume_base, 1),
        "weights": {g: round(v / total, 3) for g, v in weights.items()},
        "bpm_shift": bpm_shift,
        "bpm": bpm,
        "instrumental": instrumental,
        "vocals_penalty": cfg.get("vocals_penalty", 0.3),
        "source_weight": cfg.get("source_weight", {}),
        "reasons": reasons,
    }


def _fallback(weights, added, cfg, counts, all_genres, reasons):
    """A genre the moods matrix added (`added`: genre -> weight) with fewer than min_tracks hands a proportional
    share of that weight to its fallback genre, so a new (e.g. Suno) genre fades in as its library grows instead
    of playing the same few songs. A genre staff switched on themselves keeps its full weight."""
    need = cfg.get("min_tracks", 8)
    moved = []
    for g in list(added):
        fb = cfg.get("genres", {}).get(g, {}).get("fallback")
        n = counts.get(g, 0)
        if not fb or fb not in all_genres or n >= need:
            continue
        share = added[g] * (1 - n / need)
        weights[g] -= share
        weights[fb] = weights.get(fb, 0.0) + share
        if not weights[g]:
            del weights[g]
        moved.append(f"{all_genres.get(g, g)} ({n})")
    if moved:
        reasons.append("few tracks yet: " + ", ".join(moved) + " -> fallback genres")


ENERGY_WIDTH = 0.15      # how strictly tracks must match target energy
UNKNOWN_MATCH = 0.45     # match score for tracks not yet analysed
BPM_WIDTH = 8.0          # how strictly tracks must match a genre's target BPM
UNKNOWN_BPM = 0.6        # tempo score for tracks with no BPM
BAN_THRESHOLD = 3        # net downvotes that remove a track from rotation


def vote_factor(v):
    if not v:
        return 1.0
    net = v.get("up", 0) - v.get("down", 0)
    skip_penalty = 0.08 * v.get("skip", 0) / max(1, v.get("plays", 0))
    return _clip(1 + 0.3 * net - skip_penalty, 0.15, 2.5)


def banned(v):
    return bool(v) and v.get("down", 0) - v.get("up", 0) >= BAN_THRESHOLD


def match(track_energy, target):
    if track_energy is None:
        return UNKNOWN_MATCH
    return math.exp(-((track_energy - target) ** 2) / (2 * ENERGY_WIDTH ** 2))


def bpm_match(track_bpm, target):
    if target is None:
        return 1.0
    if not track_bpm:
        return UNKNOWN_BPM
    return math.exp(-((track_bpm - target) ** 2) / (2 * BPM_WIDTH ** 2))


def score(t, tgt, energy_of, bpm_of, votes):
    """How well a track suits the targets: energy x tempo x votes x vocals/source preferences."""
    s = match(energy_of(t.id), tgt["energy"]) * vote_factor(votes.get(t.id))
    if bpm_of:
        s *= bpm_match(bpm_of(t), tgt.get("bpm", {}).get(t.genre))
    if tgt.get("instrumental") and getattr(t, "vocals", None):
        s *= tgt.get("vocals_penalty", 0.3)
    return s * tgt.get("source_weight", {}).get(t.source, 1.0)


SMOOTH_ENERGY = 0.18   # how far the next song's energy may sit from the last one's before it's discouraged
SMOOTH_BPM = 14        # same for tempo


def smoothness(t, prev, energy_of, bpm_of):
    """How gently `t` follows `prev` (1 = same feel): a somber song shouldn't be followed by a very upbeat one."""
    if prev is None:
        return 1.0
    f = 1.0
    e0, e1 = energy_of(prev.id), energy_of(t.id)
    if e0 is not None and e1 is not None:
        f *= math.exp(-((e1 - e0) ** 2) / (2 * SMOOTH_ENERGY ** 2))
    b0, b1 = (bpm_of(prev), bpm_of(t)) if bpm_of else (None, None)
    if b0 and b1:
        f *= math.exp(-((b1 - b0) ** 2) / (2 * SMOOTH_BPM ** 2))
    return max(f, 0.02)


def pick(tracks_by_genre, tgt, energy_of, votes, recent, available, rng=random, bpm_of=None, stay_in=None, prev=None):
    """Choose a genre by weight, then a track in it by energy/tempo match x votes.

    tracks_by_genre: {genre: [Track]}; energy_of(id) -> float|None; bpm_of(track) -> float|None;
    votes: {id: {...}}; recent: set of ids to avoid; available(track) -> bool.
    stay_in: a genre to try first (so a genre plays at least min_genre_run songs in a row), if it's still wanted
    and has a song that hasn't played recently (a song is never repeated just to make a pair).
    prev: the song this one follows. Songs (and genres) close to it in energy and tempo are preferred, so the
    music flows instead of jumping from somber to upbeat.
    """
    genres = [g for g, w in tgt["weights"].items() if w > 0 and tracks_by_genre.get(g)]
    for relax in (False, True):
        cands_by = {}
        for g in genres:
            cs = [t for t in tracks_by_genre[g]
                  if not banned(votes.get(t.id)) and available(t) and (relax or t.id not in recent)]
            if cs:
                cands_by[g] = cs
        if not cands_by:
            continue
        smooth = {g: [smoothness(t, prev, energy_of, bpm_of) for t in cs] for g, cs in cands_by.items()}
        if stay_in in cands_by:
            g = stay_in
        else:
            gs = list(cands_by)
            # a genre with nothing close to the last song is still possible, just less likely
            ws = [tgt["weights"][x] * (0.25 + 0.75 * max(smooth[x])) for x in gs]
            g = rng.choices(gs, weights=ws)[0]
        cs = cands_by[g]
        scores = [score(t, tgt, energy_of, bpm_of, votes) * f for t, f in zip(cs, smooth[g])]
        return rng.choices(cs, weights=scores)[0]
    return None

"""The DJ's judgement: atmosphere in, targets and track picks out.

Pure functions (no I/O) so they are easy to test and tune.
"""

import math
import random
from dataclasses import asdict, dataclass, field


@dataclass
class Inputs:
    genres: list = field(default_factory=lambda: ["chill"])
    weather: str = "clear"            # clear | cloudy | rain
    occupancy: int = 50               # 0..100 %
    occupancy_enabled: bool = False
    hour_override: float | None = None  # None = follow the clock
    auto: bool = True                 # auto volume from atmosphere
    manual_volume: int = 30           # used when auto is off
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


def targets(inputs: Inputs, cfg, hour_now: float, all_genres, calibration=None):
    hour = inputs.hour_override if inputs.hour_override is not None else hour_now
    energy, volume, daypart = interp_curve(cfg["daypart_curve"], hour)
    reasons = [f"{daypart} {int(hour):02d}:{int(hour % 1 * 60):02d} -> energy {energy:.2f}, vol {volume:.0f}"]

    # Venue calibration: staff-saved offsets for this part of the day (see DJ.save_calibration)
    cal_v = (calibration or {}).get("volume", {}).get(daypart, 0)
    cal_e = (calibration or {}).get("energy", {}).get(daypart, 0)
    if cal_v or cal_e:
        volume += cal_v
        energy += cal_e
        reasons.append(f"calibrated {daypart}: vol {cal_v:+d}, energy {cal_e:+.2f}")

    selected = [g for g in inputs.genres if g in all_genres] or list(all_genres)
    weights = {g: 1.0 for g in selected}

    w = cfg["weather"].get(inputs.weather, cfg["weather"]["clear"])
    energy += w["energy"]
    volume += w["volume"]
    for g, boost in w.get("boost", {}).items():
        if g not in all_genres:
            continue
        if g in weights:
            weights[g] *= boost
        elif boost >= 2:  # strong weather mood pulls the genre in even if unselected
            weights[g] = cfg.get("unselected_boost_base", 0.35) * boost
    if inputs.weather != "clear":
        parts = [f"{inputs.weather}"]
        if w["energy"]:
            parts.append(f"energy {w['energy']:+.2f}")
        if w["volume"]:
            parts.append(f"vol {w['volume']:+d}")
        if w.get("boost"):
            parts.append("+" + "/".join(all_genres.get(g, g) for g in w["boost"] if g in weights))
        reasons.append(" ".join(parts))

    if inputs.occupancy_enabled:
        occ = cfg["occupancy"]
        f = _clip((inputs.occupancy - occ["threshold"]) / (100 - occ["threshold"]), 0, 1)
        if f > 0:
            energy += occ["max_energy"] * f
            volume += occ["max_volume"] * f
            reasons.append(f"busy {inputs.occupancy}% -> energy {occ['max_energy'] * f:+.2f}, vol {occ['max_volume'] * f:+.0f}")

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

    total = sum(weights.values()) or 1
    return {
        "hour": round(hour, 2),
        "clock": inputs.hour_override is None,
        "daypart": daypart,
        "energy": round(_clip(energy, 0.05, 0.95), 3),
        "volume": int(round(_clip(volume, cfg["min_volume"], cfg["max_volume"]))),
        "volume_base": round(volume_base, 1),
        "weights": {g: round(v / total, 3) for g, v in weights.items()},
        "reasons": reasons,
    }


ENERGY_WIDTH = 0.15      # how strictly tracks must match target energy
UNKNOWN_MATCH = 0.45     # match score for tracks not yet analysed
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


def pick(tracks_by_genre, tgt, energy_of, votes, recent, available, rng=random):
    """Choose a genre by weight, then a track in it by energy match x votes.

    tracks_by_genre: {genre: [Track]}; energy_of(id) -> float|None;
    votes: {id: {...}}; recent: set of ids to avoid; available(track) -> bool.
    """
    genres = [g for g, w in tgt["weights"].items() if w > 0 and tracks_by_genre.get(g)]
    for relax in (False, True):
        pool_g = list(genres)
        while pool_g:
            g = rng.choices(pool_g, weights=[tgt["weights"][x] for x in pool_g])[0]
            cands, scores = [], []
            for t in tracks_by_genre[g]:
                v = votes.get(t.id)
                if banned(v) or not available(t) or (not relax and t.id in recent):
                    continue
                cands.append(t)
                scores.append(match(energy_of(t.id), tgt["energy"]) * vote_factor(v))
            if cands:
                return rng.choices(cands, weights=scores)[0]
            pool_g.remove(g)
    return None

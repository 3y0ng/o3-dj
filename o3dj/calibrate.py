"""One-time venue calibration: listen to a handful of scenarios, adjust by ear, and fit how each
factor (overall level, busy room, empty room, rain, cloud) should move volume and energy.

Model (per target, volume and energy), on top of the time-of-day curve:

    target = curve(hour) + offset + busy * f_busy - empty * f_empty + rain * [rain] + cloudy * [cloudy]

    f_busy  = how far above half-full the room is (0 at 50%, 1 at 100%)
    f_empty = how far below half-full it is       (0 at 50%, 1 at 0%)

Occupancy's effect on energy comes from config `occupancy.bands` (brain.occupancy_band), so the
energy fit subtracts the band and brain.targets only uses the calibrated energy's weather terms.

The fit is a small ridge regression pulled toward the defaults below, so six answers can
shift things sensibly without producing anything extreme.
"""

import numpy as np

from .brain import interp_curve, occupancy_band

SCENARIOS = [
    {"id": "morning_quiet",   "name": "Quiet morning",          "hour": 9.5,  "weather": "clear",  "occupancy": 20},
    {"id": "lunch_rush",      "name": "Lunchtime rush",         "hour": 13.0, "weather": "clear",  "occupancy": 90},
    {"id": "rainy_afternoon", "name": "Rainy afternoon",        "hour": 15.5, "weather": "rain",   "occupancy": 55},
    {"id": "after_work",      "name": "Busy after work",        "hour": 18.5, "weather": "cloudy", "occupancy": 85},
    {"id": "late_evening",    "name": "Winding down",           "hour": 22.0, "weather": "clear",  "occupancy": 30},
    {"id": "rainy_night",     "name": "Rainy night, near empty", "hour": 1.5, "weather": "rain",   "occupancy": 8},
]

PARAMS = ("offset", "busy", "empty", "rain", "cloudy")
# How strongly each parameter is pulled toward its default, in "samples' worth". The overall offset is the
# thing people most want to change, so it's nearly free; the finer factors need the answers to agree.
RIDGE = {"offset": 0.02, "busy": 0.3, "empty": 0.3, "rain": 0.3, "cloudy": 0.3}


def defaults(cfg):
    """The model that reproduces the uncalibrated behaviour."""
    w, occ = cfg["weather"], cfg["occupancy"]
    return {
        "volume": {"offset": 0.0, "busy": float(occ["max_volume"]), "empty": 0.0,
                   "rain": float(w["rain"]["volume"]), "cloudy": float(w["cloudy"]["volume"])},
        # occupancy moves energy through the bands in brain.occupancy_band, not through this model
        "energy": {"offset": 0.0, "busy": 0.0, "empty": 0.0,
                   "rain": float(w["rain"]["energy"]), "cloudy": float(w["cloudy"]["energy"])},
    }


def features(sample):
    occ = sample["occupancy"]
    return np.array([1.0, max(0.0, (occ - 50) / 50), -max(0.0, (50 - occ) / 50),
                     1.0 if sample["weather"] == "rain" else 0.0,
                     1.0 if sample["weather"] == "cloudy" else 0.0])


def fit(samples, cfg):
    """samples: [{hour, weather, occupancy, volume, energy}] (what sounded right). Returns a model."""
    prior = defaults(cfg)
    model = {}
    X = np.array([features(s) for s in samples])
    for target in ("volume", "energy"):
        idx = 1 if target == "volume" else 0  # interp_curve -> (energy, volume, label)
        base = np.array([interp_curve(cfg["daypart_curve"], s["hour"])[idx] for s in samples])
        if target == "energy":  # the answers include the occupancy band's energy; fit only the rest
            base += np.array([(occupancy_band(cfg, s["occupancy"]) or {}).get("energy", 0.0) for s in samples])
        y = np.array([s[target] for s in samples]) - base
        p0 = np.array([prior[target][k] for k in PARAMS])
        lam = np.diag([RIDGE[k] for k in PARAMS])
        p = np.linalg.solve(X.T @ X + lam, X.T @ y + lam @ p0)
        digits = 1 if target == "volume" else 3
        model[target] = {k: round(float(v), digits) for k, v in zip(PARAMS, p)}
    return model


def effects(model, weather, occupancy, occupancy_known):
    """(energy_delta, volume_delta) from the calibrated model, excluding the time-of-day curve."""
    s = {"weather": weather, "occupancy": occupancy if occupancy_known else 50}
    f = features(s)
    dv = float(f @ np.array([model["volume"][k] for k in PARAMS]))
    de = float(f @ np.array([model["energy"][k] for k in PARAMS]))
    return de, dv


def describe(model, cfg):
    """Plain-language summary of what calibration changed, for the controller."""
    d = defaults(cfg)
    v, e = model["volume"], model["energy"]
    lines = []

    def loud(x):
        return "louder" if x > 0 else "quieter"

    def pace(x):
        return "more upbeat" if x > 0 else "calmer"

    if abs(v["offset"]) >= 1 or abs(e["offset"]) >= 0.02:
        parts = []
        if abs(v["offset"]) >= 1:
            parts.append(f"{abs(v['offset']):.0f} {loud(v['offset'])}")
        if abs(e["offset"]) >= 0.02:
            parts.append(pace(e["offset"]))
        lines.append("overall: " + ", ".join(parts))
    lines.append(f"packed room: {v['busy']:+.0f} volume (was {d['volume']['busy']:+.0f})")
    lines.append(f"empty room: {-v['empty']:+.0f} volume (was {-d['volume']['empty']:+.0f})")
    lines.append(f"rain: {v['rain']:+.0f} volume, {e['rain']:+.2f} energy (was {d['volume']['rain']:+.0f}, {d['energy']['rain']:+.2f})")
    lines.append(f"cloudy: {v['cloudy']:+.0f} volume, {e['cloudy']:+.2f} energy")
    return lines

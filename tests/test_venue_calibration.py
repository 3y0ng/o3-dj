"""Venue auto-detection and per-venue, per-daypart calibration."""

import copy

import pytest

from o3dj import venue
from tests.test_dj_mock import CFG, make_dj, started

NEWTOWN_HH = CFG["venues"]["sydney_01"]["sonos_households"][0]


def test_venue_from_sonos_household():
    assert venue.detect(CFG, household=NEWTOWN_HH, ip_lookup=lambda: None) == ("sydney_01", "sonos")


def test_explicit_venue_wins_and_is_validated():
    cfg = copy.deepcopy(CFG); cfg["live"]["venue"] = "melbourne_02"
    assert venue.detect(cfg, household=NEWTOWN_HH) == ("melbourne_02", "config")
    cfg["live"]["venue"] = "nowhere"
    assert venue.detect(cfg, household=NEWTOWN_HH)[0] is None


def test_public_ip_fallback_and_unknown():
    assert venue.detect(CFG, household="Sonos_other", ip_lookup=lambda: "180.150.9.35") == ("sydney_01", "public ip")
    code, how = venue.detect(CFG, household="Sonos_other", ip_lookup=lambda: "1.2.3.4")
    assert code is None and "Sonos_other" in how


def test_apply_sets_venue_max_volume():
    cfg = venue.apply(copy.deepcopy(CFG), "sydney_01", "sonos")
    assert cfg["live"]["venue"] == "sydney_01" and cfg["max_volume"] == 75


# -- calibration walk-through --------------------------------------------------------------

from o3dj import brain, calibrate

GENRES = {k: g["label"] for k, g in CFG["genres"].items()}


def _answers(model):
    """What a listener following `model` would settle on for each scenario."""
    out = []
    for sc in calibrate.SCENARIOS:
        e, v, _ = brain.interp_curve(CFG["daypart_curve"], sc["hour"])
        de, dv = calibrate.effects(model, sc["weather"], sc["occupancy"], True)
        band = brain.occupancy_band(CFG, sc["occupancy"]) or {}  # the DJ's answers include the band
        out.append({**sc, "volume": v + dv, "energy": e + de + band.get("energy", 0.0)})
    return out


def test_default_model_matches_uncalibrated_behaviour():
    for occ, w in ((90, "rain"), (70, "clear"), (20, "cloudy"), (50, "clear")):
        i = brain.Inputs(weather=w, occupancy=occ, occupancy_enabled=True)
        a = brain.targets(i, CFG, 15, GENRES)
        b = brain.targets(i, CFG, 15, GENRES, {"model": calibrate.defaults(CFG)})
        assert (a["volume"], a["energy"]) == (b["volume"], b["energy"])


def test_fit_learns_louder_venue_and_busy_boost():
    truth = calibrate.defaults(CFG)
    truth["volume"].update(offset=15, busy=12, empty=4, rain=0)
    fitted = calibrate.fit(_answers(truth), CFG)
    v = fitted["volume"]
    assert 11 < v["offset"] < 18 and v["busy"] > 8 and v["empty"] > 1.5 and abs(v["rain"]) < 3
    # and predictions land close to what the listener chose
    for s in _answers(truth):
        _, dv = calibrate.effects(fitted, s["weather"], s["occupancy"], True)
        assert abs((brain.interp_curve(CFG["daypart_curve"], s["hour"])[1] + dv) - s["volume"]) < 2.5


def test_fit_with_no_changes_keeps_defaults():
    d = calibrate.defaults(CFG)
    fitted = calibrate.fit(_answers(d), CFG)
    for t in ("volume", "energy"):
        for k in calibrate.PARAMS:
            assert abs(fitted[t][k] - d[t][k]) < (0.6 if t == "volume" else 0.01)


def test_describe_is_plain_words():
    d = calibrate.defaults(CFG); d["volume"]["offset"] = 12; d["energy"]["offset"] = -0.1
    text = " ".join(calibrate.describe(d, CFG))
    assert "12 louder" in text and "calmer" in text and "packed room" in text


def _walk(dj, louder=0):
    dj.start_wizard()
    for _ in calibrate.SCENARIOS:
        if louder:
            dj.set_inputs({"volume_trim": louder})
        dj.wizard_action("next")


def test_walkthrough_plays_each_scenario_and_applies(tmp_path):
    dj = make_dj(tmp_path)
    dj.cfg = dict(dj.cfg, max_volume=100)  # the afternoon schedule sits at the default cap; "louder" needs headroom
    started(dj)
    dj.start_wizard()
    sc = calibrate.SCENARIOS[0]
    i = dj.effective_inputs()
    assert (i.hour_override, i.weather, i.occupancy) == (sc["hour"], sc["weather"], sc["occupancy"])
    before = dj.targets["volume"]
    dj.set_inputs({"volume_trim": 10})                 # blue knob: louder, applied straight away
    assert dj.targets["volume"] == before + 10
    assert set(dj.player.volumes().values()) == {before + 10}
    assert dj.inputs.volume_trim == 0                   # doesn't touch the normal knob trims
    for _ in calibrate.SCENARIOS:
        dj.wizard_action("next")
        if dj.wizard["proposal"]:
            break
        dj.set_inputs({"volume_trim": 10})
    assert dj.wizard["proposal"] and dj.snapshot()["calibration"]["wizard"]["summary"]
    dj.wizard_action("apply")
    assert dj.wizard is None and dj.running                 # back to normal DJ-ing
    assert dj.calibration["model"]["volume"]["offset"] > 5  # it learned "louder"
    assert any("calibrated" in r for r in dj.targets["reasons"])


def test_walkthrough_stop_changes_nothing(tmp_path):
    dj = make_dj(tmp_path)
    started(dj)
    dj.start_wizard(); dj.set_inputs({"volume_trim": 20}); dj.wizard_action("stop")
    assert dj.wizard is None and dj.calibration == {} and dj.running


def test_green_knob_picks_a_new_song(tmp_path, monkeypatch):
    dj = make_dj(tmp_path)
    started(dj)
    dj.start_wizard()
    first = dj.now_id
    dj.set_inputs({"energy_trim": 0.3})
    import time as _t
    real = _t.time
    monkeypatch.setattr(__import__("o3dj.dj", fromlist=["time"]).time, "time", lambda: real() + 5)
    dj.tick(1)
    assert dj.now_id != first


def test_calibration_is_per_venue(tmp_path):
    dj = make_dj(tmp_path)
    dj.live.cfg["venue"] = "sydney_01"
    _walk(dj, louder=10); dj.wizard_action("apply")
    assert dj.calibration.get("model")
    dj.live.cfg["venue"] = "melbourne_02"
    assert dj.calibration == {}
    dj.live.cfg["venue"] = "sydney_01"
    dj.reset_calibration()
    assert dj.calibration == {}

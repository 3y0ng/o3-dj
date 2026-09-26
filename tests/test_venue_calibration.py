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


def test_saving_calibration_keeps_the_sound_and_makes_it_the_default(tmp_path):
    dj = make_dj(tmp_path)
    started(dj)
    before = dj.targets["volume"]
    dj.set_inputs({"volume_trim": 12, "energy_trim": -0.1})
    trimmed = dj.targets["volume"], dj.targets["energy"]
    dj.save_calibration()
    assert (dj.targets["volume"], dj.targets["energy"]) == trimmed   # nothing audible changes
    assert dj.inputs.volume_trim == 0 and dj.inputs.energy_trim == 0
    dp = dj.targets["daypart"]
    assert dj.calibration["volume"][dp] == 12 and dj.calibration["energy"][dp] == -0.1
    assert dj.snapshot()["calibration"]["unsaved"] is False
    # a new DJ (restart) at the same venue starts at the calibrated level
    dj2 = make_dj(tmp_path)
    assert dj2.targets["volume"] == trimmed[0] and dj2.targets["volume"] != before
    dj2.reset_calibration()
    assert dj2.targets["volume"] == before


def test_calibration_is_per_daypart(tmp_path):
    dj = make_dj(tmp_path)
    dj.set_inputs({"hour_override": 21})          # simulate evening at any time of day
    evening = dj.targets["daypart"]
    dj.set_inputs({"volume_trim": 20}); dj.save_calibration()
    dj.set_inputs({"hour_override": 14})
    assert dj.targets["daypart"] != evening and dj.calibration["volume"].get(dj.targets["daypart"], 0) == 0
    dj.set_inputs({"hour_override": 21})
    assert any("calibrated" in r for r in dj.targets["reasons"])


def test_calibration_is_per_venue(tmp_path):
    dj = make_dj(tmp_path)
    dj.live.cfg["venue"] = "sydney_01"
    dj.set_inputs({"volume_trim": 10}); dj.save_calibration()
    dj.live.cfg["venue"] = "melbourne_02"
    assert dj.calibration == {}


def test_calibrating_needs_auto_volume(tmp_path):
    dj = make_dj(tmp_path)
    dj.set_inputs({"auto": False})
    with pytest.raises(ValueError):
        dj.save_calibration()

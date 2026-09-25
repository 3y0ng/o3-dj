import json
import random
from collections import Counter

from o3dj import brain
from o3dj.config import ROOT
from o3dj.library import Track

CFG = json.loads((ROOT / "config.json").read_text())
GENRES = {k: g["label"] for k, g in CFG["genres"].items()}


def tgt(hour, **kw):
    return brain.targets(brain.Inputs(**kw), CFG, hour, GENRES)


def test_evening_is_slower_and_quieter_than_afternoon():
    aft, eve = tgt(14), tgt(21.5)
    assert eve["energy"] < aft["energy"]
    assert eve["volume"] < aft["volume"]


def test_hour_override_beats_clock():
    assert tgt(14, hour_override=22)["hour"] == 22
    assert tgt(14, hour_override=22)["energy"] == tgt(22)["energy"]


def test_rain_boosts_jazz_when_selected():
    dry = tgt(14, genres=["chill", "jazzy_cafe"])
    wet = tgt(14, genres=["chill", "jazzy_cafe"], weather="rain")
    assert wet["weights"]["jazzy_cafe"] > dry["weights"]["jazzy_cafe"]
    assert wet["energy"] < dry["energy"]


def test_rain_pulls_in_jazz_even_when_unselected():
    wet = tgt(14, genres=["chill"], weather="rain")
    assert 0 < wet["weights"]["jazzy_cafe"] < wet["weights"]["chill"]
    assert "jazzy_cafe" not in tgt(14, genres=["chill"], weather="cloudy")["weights"]


def test_occupancy_only_counts_when_enabled_and_busy():
    base = tgt(14, occupancy=95)
    busy = tgt(14, occupancy=95, occupancy_enabled=True)
    quiet = tgt(14, occupancy=20, occupancy_enabled=True)
    assert busy["energy"] > base["energy"] and busy["volume"] > base["volume"]
    assert quiet["energy"] == base["energy"]


def test_volume_is_clamped_and_manual_mode_wins():
    assert tgt(14, volume_trim=30)["volume"] == CFG["max_volume"]
    assert tgt(14, auto=False, manual_volume=17)["volume"] == 17


def _tracks():
    by = {}
    for g in GENRES:
        by[g] = [Track(id=f"{g}:{i}", title=f"{g} {i}", genre=g, source="test", url="http://x") for i in range(40)]
    return by


def test_pick_follows_genre_weights_and_energy():
    by = _tracks()
    energy = {t.id: i / 39 for g in by for i, t in enumerate(by[g])}
    t = tgt(14, genres=["chill", "jazzy_cafe"], weather="rain")
    rng = random.Random(1)
    picks = [brain.pick(by, t, energy.get, {}, set(), lambda _: True, rng) for _ in range(600)]
    genres = Counter(p.genre for p in picks)
    assert genres["jazzy_cafe"] > genres["chill"] and genres["asian"] == 0
    mean_e = sum(energy[p.id] for p in picks) / len(picks)
    assert abs(mean_e - t["energy"]) < 0.12


def test_pick_respects_bans_recent_and_availability():
    by = {"chill": _tracks()["chill"][:3]}
    t = {"weights": {"chill": 1.0}, "energy": 0.5}
    votes = {"chill:0": {"down": 3}}
    recent = {"chill:1"}
    for _ in range(50):
        assert brain.pick(by, t, lambda _: None, votes, recent, lambda _: True).id == "chill:2"
    assert brain.pick(by, t, lambda _: None, votes, set(), lambda tr: tr.id == "chill:0") is None
    # when everything is recent, it relaxes rather than going silent
    assert brain.pick(by, t, lambda _: None, {}, {"chill:0", "chill:1", "chill:2"}, lambda _: True)

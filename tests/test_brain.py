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


def test_volume_schedule_steps():
    """Newtown's schedule (Sep 2026): base volume by time of day, before busy/empty/rain nudges."""
    expect = {23.5: 35, 1: 35, 3: 20, 6: 20, 8: 35, 9.99: 35, 11: 50, 12.5: 60, 18.9: 60, 19.5: 50, 20.5: 38, 22.9: 38}
    for hour, vol in expect.items():
        assert tgt(hour)["volume"] == vol, hour
    assert tgt(9.9)["energy"] < tgt(10.1)["energy"] + 0.01  # energy stays smooth across the volume steps


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


def test_occupancy_only_counts_when_enabled():
    base = tgt(14, occupancy=95)
    assert (base["bpm_shift"], base["instrumental"]) == (0, False)
    assert tgt(14, occupancy=55, occupancy_enabled=True)["energy"] == base["energy"]


def test_occupancy_bands_counter_the_room():
    base = tgt(14)
    cases = {5: (0.15, 8, False), 25: (0.05, 0, False), 55: (0.0, 0, False),
             80: (-0.10, -5, False), 95: (-0.20, -10, True), 100: (-0.20, -10, True)}
    for occ, (de, bpm, instr) in cases.items():
        t = tgt(14, occupancy=occ, occupancy_enabled=True)
        assert abs(t["energy"] - (base["energy"] + de)) < 1e-6, occ
        assert (t["bpm_shift"], t["instrumental"]) == (bpm, instr), occ


def test_volume_rises_as_the_room_fills():  # 8am: a quiet hour, so there's room under max_volume to get louder
    vols = [tgt(8, occupancy=o, occupancy_enabled=True)["volume"] for o in (10, 50, 80, 100)]
    assert vols == sorted(vols) and vols[-1] > vols[0]


def test_moods_follow_time_and_weather():
    def top(hour, weather):  # the slot's leading new genre
        w = {g: v for g, v in tgt(hour, weather=weather)["weights"].items() if CFG["genres"].get(g, {}).get("fallback")}
        return max(w, key=w.get)
    assert top(9, "clear") == "bossa_house"
    clear_afternoon = {g: v for g, v in tgt(15)["weights"].items() if CFG["genres"].get(g, {}).get("fallback")}
    total = sum(clear_afternoon.values())
    assert {g: round(v / total, 2) for g, v in clear_afternoon.items()} == \
        {"lofi_indie": 0.5, "electronic_lofi": 0.3, "whimsy_fantasy": 0.2}
    assert top(15, "cloudy") == "whimsy_fantasy"
    assert top(19, "rain") == "dark_academia"
    assert top(23, "clear") == "ambient_dream" and top(3, "rain") == "ambient_dream"
    t = tgt(9)
    assert t["weights"]["chill"] > 0 and t["mood_daypart"] == "morning"


def test_new_music_share_grows_with_the_library():
    def new_share(counts):
        w = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts=counts)["weights"]
        return sum(v for g, v in w.items() if CFG["genres"].get(g, {}).get("fallback"))
    base = {"chill": 400, "jazzy_cafe": 300, "asian": 200}
    full = CFG["new_music"]["full_at_tracks"]
    start = new_share({**base, "bossa_house": 8, "jazzhop": 8})  # just past min_tracks, so no fallback
    half = new_share({**base, "bossa_house": full // 2, "jazzhop": full // 2})
    done = new_share({**base, "bossa_house": full, "jazzhop": full})
    s0 = CFG["new_music"]["start_share"]
    assert abs(tgt(9)["weights"]["chill"] - ((1 - s0) + s0 * 0.2)) < 0.01  # selection + chill's 20% of the mix
    assert start < half < done and abs(done - CFG["new_music"]["full_share"] * 0.8) < 0.01
    full_lib = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts={**base, "bossa_house": full, "jazzhop": full})
    assert abs(full_lib["weights"]["chill"] - 0.2) < 0.01  # morning keeps 20% chill even when Suno is full
    empty = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts=base)["weights"]
    assert not any(CFG["genres"].get(g, {}).get("fallback") for g in empty)  # Chillify covers an empty slot


def test_a_genre_staff_pick_plays_even_with_few_songs():
    counts = {"chill": 400, "jazzy_cafe": 300, "asian": 200, "dark_academia": 1}
    t = brain.targets(brain.Inputs(genres=["dark_academia"], moods=False), CFG, 15, GENRES, counts=counts)
    assert t["weights"] == {"dark_academia": 1.0}
    # with auto on, the slot's own short genres still lean on Chillify, but the pick keeps its weight
    t = brain.targets(brain.Inputs(genres=["dark_academia"]), CFG, 15, GENRES, counts=counts)
    assert t["weights"]["dark_academia"] >= 1 - CFG["new_music"]["start_share"] - 0.01


def test_moods_off_keeps_staff_selection():
    assert set(tgt(9, genres=["asian"], moods=False)["weights"]) == {"asian"}


def test_short_genre_hands_weight_to_fallback():
    full = {g: 50 for g in GENRES}
    t = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts=full)
    assert "jazzy_cafe" not in t["weights"] and not any("few tracks" in r for r in t["reasons"])
    empty = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts={**full, "bossa_house": 0})
    assert "bossa_house" not in empty["weights"] and empty["weights"]["jazzy_cafe"] > 0
    half = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts={**full, "bossa_house": 4})
    assert 0 < half["weights"]["bossa_house"] < t["weights"]["bossa_house"]


def test_bpm_target_is_genre_middle_plus_shift():
    lo, hi = CFG["genres"]["jazzhop"]["bpm"]
    assert tgt(9, weather="rain")["bpm"]["jazzhop"] == (lo + hi) / 2
    assert tgt(9, weather="rain", occupancy=95, occupancy_enabled=True)["bpm"]["jazzhop"] == (lo + hi) / 2 - 10


def test_bossa_tempo_climbs_through_the_morning():
    lo, hi = CFG["genres"]["bossa_house"]["bpm"]
    early, mid, late = (tgt(h)["bpm"]["bossa_house"] for h in (6, 9, 11.9))
    assert early == lo and early < mid < late and abs(late - hi) < 1
    assert brain.mood_progress(CFG, 23) == 0.125 and brain.mood_progress(CFG, 3) == 0.625


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


def _suno(n, bpm=None, vocals=None, genre="bossa_house"):
    return [Track(id=f"s:{i}", title=str(i), genre=genre, source="suno", path=f"{genre}/{i}.mp3",
                  bpm=(bpm(i) if callable(bpm) else bpm), vocals=(vocals(i) if callable(vocals) else vocals))
            for i in range(n)]


def test_pick_prefers_tracks_near_target_bpm():
    tracks = _suno(40, bpm=lambda i: 90 + i)  # 90..129
    t = {"weights": {"bossa_house": 1.0}, "energy": 0.5, "bpm": {"bossa_house": 100}}
    rng = random.Random(2)
    picks = [brain.pick({"bossa_house": tracks}, t, lambda _: None, {}, set(), lambda _: True, rng,
                        bpm_of=lambda tr: tr.bpm) for _ in range(400)]
    mean = sum(p.bpm for p in picks) / len(picks)
    assert abs(mean - 100) < 5


def test_vocals_penalised_only_when_instrumental():
    tracks = _suno(20, vocals=lambda i: i % 2 == 0)
    by = {"bossa_house": tracks}

    def share(tgt):
        rng = random.Random(3)
        ps = [brain.pick(by, tgt, lambda _: None, {}, set(), lambda _: True, rng) for _ in range(600)]
        return sum(p.vocals for p in ps) / len(ps)
    base = {"weights": {"bossa_house": 1.0}, "energy": 0.5}
    assert 0.4 < share(base) < 0.6
    assert share({**base, "instrumental": True, "vocals_penalty": 0.3}) < 0.33
    assert tgt(14, occupancy=80, occupancy_enabled=True)["instrumental"] is False


def test_suno_tracks_preferred_within_a_genre():
    other = [Track(id=f"c:{i}", title=str(i), genre="chill", source="chillify", url="http://x") for i in range(10)]
    by = {"chill": other + _suno(10, genre="chill")}
    t = {"weights": {"chill": 1.0}, "energy": 0.5, "source_weight": {"suno": 3.0}}
    rng = random.Random(4)
    ps = [brain.pick(by, t, lambda _: None, {}, set(), lambda _: True, rng) for _ in range(600)]
    assert sum(p.source == "suno" for p in ps) / len(ps) > 0.65

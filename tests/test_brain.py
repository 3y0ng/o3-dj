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
    expect = {23.5: 35, 1: 35, 3: 20, 6: 20, 8: 35, 9.99: 35, 11: 50, 12.5: 60, 18.9: 60, 19.5: 50, 20.5: 50, 21.5: 38, 22.9: 38}
    for hour, vol in expect.items():
        assert tgt(hour)["volume"] == vol, hour
    assert tgt(9.9)["energy"] < tgt(10.1)["energy"] + 0.01  # energy stays smooth across the volume steps


def test_hour_override_beats_clock():
    assert tgt(14, hour_override=22)["hour"] == 22
    assert tgt(14, hour_override=22)["energy"] == tgt(22)["energy"]


def test_rain_boosts_chill_when_selected():
    dry = tgt(14, genres=["chill", "asian"], moods=False)
    wet = tgt(14, genres=["chill", "asian"], weather="rain", moods=False)
    assert wet["weights"]["chill"] > dry["weights"]["chill"]
    assert wet["energy"] < dry["energy"]


def test_rainy_evening_leans_on_chill():  # Jazzy Cafe used to fill rainy early evenings
    assert tgt(18, weather="rain")["weights"]["chill"] > tgt(18)["weights"]["chill"]


def test_jazzy_cafe_is_gone():
    assert "jazzy_cafe" not in CFG["genres"]
    for h in range(24):
        for w in ("clear", "cloudy", "rain"):
            assert "jazzy_cafe" not in tgt(h, weather=w)["weights"]


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


def test_quiet_afternoon_plays_5_quieter():
    def vol(hour, occ, known=True):
        return tgt(hour, occupancy=occ, occupancy_enabled=known)["volume"]
    assert vol(14, 40) == vol(14, 40, known=False) - 5  # afternoon under 60% full
    assert vol(14, 59) == vol(14, 59, known=False) - 5 + 1  # still gets the small busy bump above 50%
    assert vol(14, 70) >= vol(14, 70, known=False)  # 60%+ full: no cut
    assert vol(9, 40) == vol(9, 40, known=False)  # morning: no rule
    assert any("under 60% full" in r for r in tgt(14, occupancy=40, occupancy_enabled=True)["reasons"])


def test_moods_follow_time_and_weather():
    def top(hour, weather):  # the slot's leading new genre
        w = {g: v for g, v in tgt(hour, weather=weather)["weights"].items() if CFG["genres"].get(g, {}).get("fallback")}
        return max(w, key=w.get)
    assert top(9, "clear") == "bossa_house"
    clear_afternoon = tgt(15)["weights"]  # Ghibli Lofi leads; Jazzy Cafe's share went to Ghibli Lofi and Chill
    assert clear_afternoon["lofi_indie"] > clear_afternoon["chill"] > 0 and clear_afternoon["whimsy_fantasy"] > 0
    assert "electronic_lofi" not in clear_afternoon
    assert top(15, "cloudy") == "lofi_indie"  # Ghibli Lofi just leads Whimsy Fantasy now
    assert top(19, "rain") == "jazzhop" and top(21, "rain") == "dark_academia"  # Dark Academia waits for 8pm
    assert top(18, "clear") == "lofi_indie" and top(21, "clear") == "indie_lofi_fantasy"  # Evening Lofi waits until 8pm
    assert top(23, "clear") == "ambient_dream" and top(3, "rain") == "ambient_dream"
    t = tgt(9)
    assert t["weights"]["chill"] > 0 and t["mood_daypart"] == "morning"


def test_new_music_share_grows_with_the_library():
    def new_share(counts):
        w = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts=counts)["weights"]
        return sum(v for g, v in w.items() if CFG["genres"].get(g, {}).get("fallback"))
    base = {"chill": 400, "asian": 200}
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
    counts = {"chill": 400, "asian": 200, "dark_academia": 1}
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
    assert not any("few tracks" in r for r in t["reasons"])
    empty = brain.targets(brain.Inputs(), CFG, 9, GENRES, counts={**full, "bossa_house": 0})
    assert "bossa_house" not in empty["weights"] and empty["weights"]["chill"] > t["weights"]["chill"]
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
    t = tgt(14, genres=["chill", "asian"], weather="rain", moods=False)
    rng = random.Random(1)
    picks = [brain.pick(by, t, energy.get, {}, set(), lambda _: True, rng) for _ in range(600)]
    genres = Counter(p.genre for p in picks)
    assert genres["chill"] > genres["asian"] > 0 and genres["jazzhop"] == 0
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


def test_busy_room_has_a_volume_floor():
    cfg = {**CFG, "max_volume": 75}
    def vol(hour, occ):
        return brain.targets(brain.Inputs(occupancy=occ, occupancy_enabled=True), cfg, hour, GENRES)["volume"]
    pts = dict(CFG["occupancy"]["volume_floor"])
    assert vol(21.5, 40) == 38                       # under the first point: the schedule
    assert vol(21.5, 70) >= pts[70] and vol(21.5, 100) >= pts[100]
    steps = [vol(21.5, o) for o in range(50, 101, 5)]
    assert steps == sorted(steps) and max(b - a for a, b in zip(steps, steps[1:])) <= 8  # climbs, no cliff
    assert vol(14, 75) >= vol(14, 40)                # a louder schedule still wins
    assert brain.targets(brain.Inputs(occupancy=95), cfg, 21.5, GENRES)["volume"] == 38  # needs live occupancy


def test_no_dark_academia_or_electronic_lofi_before_8pm():
    for hour in (7, 10, 14, 16, 18, 19.9):
        for weather in ("clear", "cloudy", "rain"):
            w = tgt(hour, weather=weather)["weights"]
            assert not w.get("dark_academia") and not w.get("electronic_lofi"), (hour, weather)


def test_whimsy_fantasy_has_a_middle_share():
    # staff downvoted it most: it plays every afternoon but no longer leads any of them
    for weather in ("clear", "cloudy", "rain"):
        w = tgt(15, weather=weather)["weights"]
        assert 0 < w["whimsy_fantasy"] < max(w.values()), weather


def test_staff_genre_pick_gets_its_share_with_moods_on():
    full = {g: 40 for g in GENRES}  # every style past full_at_tracks: the moods would take every pick
    plain = brain.targets(brain.Inputs(genres=["chill", "asian"]), CFG, 15, GENRES, counts=full)["weights"]
    assert plain.get("asian", 0) < 0.01  # without a pick, a lit key does nothing (the old behaviour)
    t = brain.targets(brain.Inputs(genres=["chill", "asian"], picks={"asian": "afternoon"}), CFG, 15, GENRES, counts=full)
    assert abs(t["weights"]["asian"] - CFG["moods"]["pick_share"]) < 0.01
    assert t["mood_until"] == 17 and any("staff picks" in r for r in t["reasons"])


def test_pick_flows_from_the_last_song():
    """A somber song shouldn't be followed by a very upbeat one: genres and songs near the last one's feel win."""
    by = {"dark": [Track(id=f"d:{i}", title=str(i), genre="dark", source="test", url="x") for i in range(20)],
          "upbeat": [Track(id=f"u:{i}", title=str(i), genre="upbeat", source="test", url="x") for i in range(20)]}
    energy = {**{f"d:{i}": 0.1 + i * 0.01 for i in range(20)}, **{f"u:{i}": 0.75 + i * 0.01 for i in range(20)}}
    t = {"weights": {"dark": 1.0, "upbeat": 1.0}, "energy": 0.5}
    prev = by["dark"][0]
    rng = random.Random(3)
    picks = [brain.pick(by, t, energy.get, {}, set(), lambda _: True, rng, prev=prev) for _ in range(400)]
    assert Counter(p.genre for p in picks)["dark"] > 300
    free = [brain.pick(by, t, energy.get, {}, set(), lambda _: True, rng) for _ in range(400)]
    assert 120 < Counter(p.genre for p in free)["dark"] < 280  # without a previous song, weights alone decide
    # a big tempo jump is discouraged too
    tracks = _suno(40, bpm=lambda i: 70 + i * 2)  # 70..148
    t2 = {"weights": {"bossa_house": 1.0}, "energy": 0.5}
    picks = [brain.pick({"bossa_house": tracks}, t2, lambda _: None, {}, set(), lambda _: True, rng,
                        bpm_of=lambda tr: tr.bpm, prev=tracks[0]) for _ in range(300)]
    assert sum(p.bpm for p in picks) / len(picks) < 95


def test_smoothness_never_rules_a_song_out():
    a = Track(id="a", title="a", genre="g", source="test", url="x")
    b = Track(id="b", title="b", genre="g", source="test", url="x")
    assert brain.smoothness(b, a, {"a": 0.0, "b": 1.0}.get, None) >= 0.02
    assert brain.smoothness(b, None, {"a": 0.0, "b": 1.0}.get, None) == 1.0

"""End-to-end DJ behaviour against the silent MockPlayer (no network, no speakers)."""

import json
import time

import pytest

from o3dj import dj as dj_mod
from o3dj.config import ROOT
from o3dj.library import Track
from o3dj.player import MockPlayer

CFG = json.loads((ROOT / "config.json").read_text())


class FakeLibrary:
    class chillify:
        online = True

    def __init__(self):
        self.tracks = {f"{g}:{i}": Track(id=f"{g}:{i}", title=f"{g} {i}", genre=g, source="chillify",
                                         url=f"https://example.test/{g}/{i}.mp3")
                       for g in CFG["genres"] for i in range(20)}

    def genres(self):
        return {k: g["label"] for k, g in CFG["genres"].items()}

    def get(self, i):
        return self.tracks.get(i)

    def in_genre(self, g):
        return [t for t in self.tracks.values() if t.genre == g]

    def all(self):
        return list(self.tracks.values())

    def counts(self):
        return {g: len(self.in_genre(g)) for g in CFG["genres"]}


class FakeCache:
    names = set()
    def __init__(self): self.cached = set()
    def has(self, t): return t.id in self.cached
    def local_path(self, t): return None
    def name_for(self, t): return t.id.replace(":", "_") + ".mp3"
    def touch(self, t): pass
    def usage_mb(self): return 0
    def gain(self, t): return None


class FakeMeta:
    def __init__(self): self.durations = {}
    def energy(self, i): return None
    def get(self, i): return None
    def duration(self, i): return self.durations.get(i)
    def count(self): return 0


def make_dj(tmp_path, player=None):
    d = dj_mod.DJ(CFG, player or MockPlayer(track_seconds=1000), FakeLibrary(), FakeCache(), FakeMeta(),
                  state_file=tmp_path / "state.json")
    d.fade_skips = False  # fades run in a thread with sleeps; tested separately
    d.cfg = {**CFG, "skip_crossfade_seconds": 0,  # plain skips; the crossfade skip is tested separately
             # no per-genre volume offsets, so volume checks don't depend on which genre the clock picks
             "genres": {g: {k: v for k, v in c.items() if k != "volume_offset"} for g, c in CFG["genres"].items()}}
    return d


@pytest.fixture
def dj(tmp_path):
    return make_dj(tmp_path)


def started(dj, duration=200):
    """Start the DJ with known track lengths and let one tick observe it."""
    for t in dj.library.all():
        dj.meta.durations[t.id] = duration
    dj.start()
    dj.tick(0)
    return dj


def at_elapsed(dj, secs):
    dj.player.started_at = time.monotonic() - secs


def settle(dj, monkeypatch):
    """Make any pending re-pick due."""
    real = time.time
    monkeypatch.setattr(dj_mod.time, "time", lambda: real() + 60)


# -- basics ------------------------------------------------------------------------

def test_start_queues_ahead_and_counts_play_once_playing(dj):
    dj.start()
    assert dj.votes == {}  # nothing counted until the speaker says PLAYING
    dj.tick(0)
    assert dj.status["state"] == "PLAYING"
    assert len(dj.upcoming) == CFG["queue_ahead"]
    assert dj.votes[dj.now_id]["plays"] == 1
    dj.tick(1)
    assert dj.votes[dj.now_id]["plays"] == 1


def test_skip_advances_tops_up_and_has_cooldown(dj):
    started(dj)
    first, nxt = dj.now_id, dj.upcoming[0]
    dj.skip(); dj.tick(1)
    assert dj.now_id == nxt and dj.votes[first]["skip"] == 1
    assert len(dj.upcoming) == CFG["queue_ahead"]
    dj.skip(); dj.tick(2)  # double tap within cooldown is ignored
    assert dj.now_id == nxt


def test_skip_with_crossfade_jumps_to_the_last_seconds(dj):
    dj.player.duration_of = lambda uri: 200  # the mock speaker knows the real length too
    started(dj)
    dj.cfg["skip_crossfade_seconds"] = 8
    at_elapsed(dj, 30)
    first, nxt = dj.now_id, dj.upcoming[0]
    dj.skip(); dj.tick(1)
    assert dj.now_id == first and 191 <= dj.player.status()["elapsed"] <= 193  # outro plays, speaker crossfades
    at_elapsed(dj, 201)  # the song ends naturally
    dj.tick(2)
    assert dj.now_id == nxt and dj.votes[first]["skip"] == 1


def test_skip_without_known_length_falls_back_to_next(dj):
    started(dj, duration=None)
    dj.cfg["skip_crossfade_seconds"] = 8
    nxt = dj.upcoming[0]
    dj.skip(); dj.tick(1)
    assert dj.now_id == nxt


def test_each_genre_plays_at_least_twice_in_a_row(dj):
    dj.set_inputs({"genres": ["chill", "jazzy_cafe", "asian"], "moods": False})
    started(dj)
    seq = []
    for i in range(20):  # fewer than each genre's 20 songs: a genre with no fresh song left ends its run early
        seq.append(dj.library.get(dj.now_id).genre)
        dj.skip(); dj.last_skip = 0; dj.tick(i + 1)
    runs, n = [], 1
    for a, b in zip(seq, seq[1:]):
        if a == b:
            n += 1
        else:
            runs.append(n); n = 1
    assert len(runs) >= 2 and min(runs) >= 2  # every completed run is 2+ songs, and genres do change


def test_quiet_genres_play_a_little_quieter(dj):
    dj.cfg = {**dj.cfg, "genres": {**CFG["genres"], "chill": {**CFG["genres"]["chill"], "volume_offset": -3}}}
    dj.set_inputs({"genres": ["chill"], "moods": False})
    started(dj)
    dj._apply_volume(max_step=100)
    assert dj.library.get(dj.now_id).genre == "chill"
    assert set(dj.player.volumes().values()) == {dj.targets["volume"] - 3}
    dj.set_inputs({"auto": False, "manual_volume": 30})
    dj._apply_volume(max_step=100)
    assert set(dj.player.volumes().values()) == {30}  # a hand-set volume is left alone


def test_downvote_skips_current(dj):
    started(dj)
    cur = dj.now_id
    dj.vote("down"); dj.tick(1)
    assert dj.now_id != cur and dj.votes[cur]["down"] == 1


def test_mock_and_live_state_are_separate(tmp_path):
    a = make_dj(tmp_path)
    started(a)
    b = dj_mod.DJ(CFG, MockPlayer(), FakeLibrary(), FakeCache(), FakeMeta(), state_file=tmp_path / "other.json")
    assert b.votes == {} and len(b.history) == 0


# -- re-picking safely ---------------------------------------------------------------

def test_genre_change_repicks_after_the_next_track(dj, monkeypatch):
    started(dj)
    at_elapsed(dj, 30)
    nxt = dj.upcoming[0]
    dj.set_inputs({"genres": ["asian"], "moods": False})
    settle(dj, monkeypatch)
    dj.tick(1)
    assert dj.upcoming[0] == nxt  # pre-loaded by Sonos: kept
    assert all(dj.library.get(i).genre == "asian" for i in dj.upcoming[1:])
    assert dj.player.queue_uris()[dj.status["index"]] == dj.id_uri[dj.now_id]


def test_mid_song_repick_never_stops_playback(dj, monkeypatch):
    """Seen live 23:11: a re-pick ~40 s before the end removed the pre-loaded next item
    and the Sonos stopped. Any point in the song must be safe."""
    for elapsed in (5, 60, 150, 190):
        started(dj)
        at_elapsed(dj, elapsed)
        nxt = dj.upcoming[0]
        dj.set_inputs({"weather": "rain" if dj.inputs.weather != "rain" else "clear"})
        settle(dj, monkeypatch)
        dj.tick(1)
        monkeypatch.undo()
        assert dj.status["state"] == "PLAYING" and dj.upcoming[0] == nxt, elapsed
        assert dj.player.queue_uris()[dj.status["index"] + 1] == dj.id_uri[nxt]


def test_unknown_length_keeps_next_track(dj, monkeypatch):
    started(dj, duration=None)
    nxt = dj.upcoming[0]
    dj.set_inputs({"genres": ["asian"], "moods": False})
    settle(dj, monkeypatch)
    dj.tick(1)
    assert dj.upcoming[0] == nxt
    assert all(dj.library.get(i).genre == "asian" for i in dj.upcoming[1:])


def test_drop_never_removes_playing_or_next_track(dj):
    started(dj)
    dj.player.drop_from(0)
    assert dj.player.status()["state"] == "PLAYING"
    assert dj.player.queue_uris() == [dj.id_uri[dj.now_id], dj.id_uri[dj.upcoming[0]]]


# -- recovery ---------------------------------------------------------------------

def test_queue_reset_jumps_to_next_unplayed_without_fake_play(dj):
    started(dj)
    first = dj.now_id
    dj.skip(); dj.tick(1)
    second, third = dj.now_id, dj.upcoming[0]
    plays_before = dict((k, v.get("plays")) for k, v in dj.votes.items())
    # Sonos drops back to item 1 and stops (what we saw live)
    dj.player.index, dj.player.state = 0, "STOPPED"
    dj.tick(2); dj.tick(3)
    dj.tick(4)
    assert dj.now_id not in (first, second)
    assert dj.now_id == third
    assert dj.votes[first].get("plays") == plays_before[first]  # no fake replay counted
    assert first not in dj.failures


def test_old_track_playing_jumps_forward(dj):
    started(dj)
    first = dj.now_id
    dj.skip(); dj.tick(1)
    upcoming = list(dj.upcoming)
    dj.player.play_index(0)  # queue reset straight into PLAYING item 1
    dj.tick(2); dj.tick(3)
    assert dj.now_id == upcoming[0] and dj.votes[first]["plays"] == 1


def test_failed_stream_is_blamed_and_skipped(dj):
    started(dj)
    bad, nxt = dj.now_id, dj.upcoming[0]
    dj.now_max_elapsed = 0
    dj.player.stop()
    dj.tick(1); dj.tick(2); dj.tick(3)
    assert dj.failures[bad][0] == 1 and dj.now_id == nxt


def test_stop_from_sonos_app_is_respected(dj):
    started(dj)
    at_elapsed(dj, 60); dj.tick(1)
    cur = dj.now_id
    dj.player.stop()
    dj.tick(2); dj.tick(3); dj.tick(4)
    assert dj.paused and dj.now_id == cur and dj.player.status()["state"] == "STOPPED"
    assert cur not in dj.failures


def test_pause_from_sonos_app_is_mirrored(dj):
    started(dj)
    dj.player.pause(); dj.tick(1)
    assert dj.paused
    dj.player.play(); dj.tick(2)
    assert not dj.paused


def test_downvote_upcoming_swaps_just_that_track(dj):
    started(dj)
    at_elapsed(dj, 30)
    keep, bad = dj.upcoming[0], dj.upcoming[1]
    dj.vote("down", bad)
    assert keep in dj.upcoming and bad not in dj.upcoming
    assert len(dj.upcoming) == CFG["queue_ahead"]
    assert dj.id_uri[bad] not in dj.player.queue_uris()


def test_downvote_next_track_too_late_skips_it_on_arrival(dj):
    started(dj)
    at_elapsed(dj, 190)
    nxt = dj.upcoming[0]
    dj.vote("down", nxt)
    assert nxt in dj.upcoming  # too late to edit safely
    dj.player.next(); dj.tick(1); dj.tick(2)
    assert dj.now_id != nxt


def test_offline_source_only_plays_cached(dj):
    dj.library.chillify.online = False
    try:
        with pytest.raises(RuntimeError):
            dj.start()
        dj.cache.cached = {"chill:3", "chill:4", "chill:5"}
        dj.start()
        assert dj.now_id in dj.cache.cached and set(dj.upcoming) <= dj.cache.cached
    finally:
        dj.library.chillify.online = True


# -- time of day ------------------------------------------------------------------

def test_daypart_change_resets_trims_and_repicks(dj, monkeypatch):
    started(dj)
    at_elapsed(dj, 30)
    dj.set_inputs({"energy_trim": -0.4, "volume_trim": -7})
    dj.dirty_at = None
    dj.last_daypart = ("some earlier daypart", dj.targets["mood_daypart"])
    calls = []
    monkeypatch.setattr(dj, "refresh_upcoming", lambda: calls.append(1) or True)
    dj.tick(1)
    assert dj.inputs.energy_trim == 0 and dj.inputs.volume_trim == 0
    assert calls and "trims reset" in dj.events[0]["msg"]


def test_mood_change_repicks_but_keeps_trims(dj, monkeypatch):
    started(dj)
    at_elapsed(dj, 30)
    dj.set_inputs({"energy_trim": -0.3})
    dj.dirty_at = None
    dj.last_daypart = (dj.targets["daypart"], "some earlier mood")
    calls = []
    monkeypatch.setattr(dj, "refresh_upcoming", lambda: calls.append(1) or True)
    dj.tick(1)
    assert calls and dj.inputs.energy_trim == -0.3
    assert dj.events[0]["msg"].endswith("music")


def test_simulated_time_does_not_reset_trims(dj):
    started(dj)
    dj.set_inputs({"energy_trim": -0.3, "hour_override": 21})
    dj.last_daypart = ("afternoon", "afternoon")
    dj.tick(1)
    assert dj.inputs.energy_trim == -0.3


# -- restart ------------------------------------------------------------------------

def test_restart_adopts_existing_queue(tmp_path):
    player = MockPlayer(track_seconds=1000)
    a = make_dj(tmp_path, player)
    started(a)
    now, upcoming = a.now_id, list(a.upcoming)
    b = make_dj(tmp_path, player)  # same state file, same speaker
    assert b.adopt()
    assert b.running and b.now_id == now and b.upcoming == upcoming
    b.tick(0)
    assert b.running and b.foreign_ticks == 0


# -- volume -------------------------------------------------------------------------

def test_manual_volume_applies_immediately(dj):
    dj.start()
    dj.set_inputs({"auto": False, "manual_volume": 40})
    assert set(dj.player.volumes().values()) == {40}
    dj.set_inputs({"auto": True})
    assert set(dj.player.volumes().values()) == {dj.targets["volume"]}


def test_auto_volume_ramps_gently(dj):
    started(dj)
    ip = dj.player.anchor_ip
    dj.player.set_volume(ip, dj.targets["volume"] + 10)
    dj.tick(1)
    assert dj.player.volumes()[ip] == dj.targets["volume"] + 8


def test_restart_does_not_adopt_when_something_else_is_playing(tmp_path):
    player = MockPlayer(track_seconds=1000)
    a = make_dj(tmp_path, player)
    started(a)
    player.queue[player.index] = dict(player.queue[player.index], uri="x-sonos-vli:spotify")  # Spotify took over
    b = make_dj(tmp_path, player)
    assert not b.adopt() and not b.running


def test_skip_mid_transition_does_not_cascade(dj, monkeypatch):
    """Seen live: after a skip, Sonos briefly reports the new position with the old URI.
    The DJ must not drop the starting track and then 'jump back' past it."""
    started(dj)
    first = dj.now_id
    nxt, after = dj.upcoming[0], dj.upcoming[1]
    dj.skip()
    real_status = dj.player.status
    stale = dict(real_status(), uri=dj.id_uri[first])  # new index, old URI
    monkeypatch.setattr(dj.player, "status", lambda: dict(stale))
    dj.tick(1)
    monkeypatch.setattr(dj.player, "status", real_status)
    dj.tick(2); dj.tick(3)
    assert dj.now_id == nxt and dj.upcoming[0] == after
    assert not any("jumped back" in e["msg"] for e in dj.events)
    uris = dj.player.queue_uris()
    assert len(set(uris)) == len(uris), "a track was queued twice"


def test_every_streamable_track_is_queued_with_a_lan_url(dj):
    started(dj)
    assert all("/media/cache/" in u for u in dj.player.queue_uris())
    name = dj.player.queue_uris()[0].rsplit("/", 1)[-1]
    assert dj.track_for_cache_name(name).id == dj.now_id


def test_fade_skip_dips_volume_and_restores_it(dj):
    started(dj)
    dj._apply_volume(max_step=100)  # settle the auto-volume ramp so only the fade moves it
    dj.fade_skips = True
    before = dj.player.group_volume()
    seen = []
    real_set = dj.player.set_group_volume
    dj.player.set_group_volume = lambda v: (seen.append(v), real_set(v))
    first = dj.now_id
    dj.skip()
    for _ in range(40):
        if not dj.fading:
            break
        time.sleep(0.05)
    dj.tick(1)
    assert dj.now_id != first
    assert min(seen) < before * 0.3 and seen[-1] == before
    assert dj.player.group_volume() == before


# -- rooms: main room, switching off, per-room volume -------------------------------

def ip_of(dj, name):
    return next(s["ip"] for s in dj.player.speakers() if s["name"] == name)


def test_make_main_keeps_music_playing_and_survives_restart(tmp_path):
    player = MockPlayer(track_seconds=1000)
    a = started(make_dj(tmp_path, player))
    entrance = ip_of(a, "Mock Cafe Entrance")
    now, queue = a.now_id, player.queue_uris()
    a.speaker_action("main", entrance)  # not in the group yet: joins, then leads
    main = [s for s in a.speakers if s["coordinator"]]
    assert [s["ip"] for s in main] == [entrance]
    assert {s["name"] for s in a.speakers if s["in_group"]} == {"Mock Cafe Entrance", "Mock Office"}
    a.tick(1)
    assert a.running and a.status["state"] == "PLAYING" and a.now_id == now and player.queue_uris() == queue

    player.anchor_ip = "10.0.0.11"  # a restart starts from config's main room...
    b = make_dj(tmp_path, player)
    assert player.anchor_ip == entrance  # ...and goes back to the one chosen on the controller


def test_switching_off_main_room_hands_over_and_music_carries_on(dj):
    started(dj)
    office, storage = ip_of(dj, "Mock Office"), ip_of(dj, "Mock 2nd Floor Storage Room")
    dj.speaker_action("join", storage)
    now = dj.now_id
    dj.speaker_action("leave", office)
    rooms = {s["name"]: s for s in dj.speakers}
    assert rooms["Mock 2nd Floor Storage Room"]["coordinator"] and not rooms["Mock Office"]["in_group"]
    dj.tick(1)
    assert dj.status["state"] == "PLAYING" and dj.now_id == now and not dj.paused


def test_switching_off_main_room_prefers_the_configured_home_room(tmp_path):
    d = started(make_dj(tmp_path))
    for n in ("Mock 2nd Floor Painting Corner", "Mock 2nd Floor Storage Room"):
        d.speaker_action("join", ip_of(d, n))
    d.cfg = dict(d.cfg, coordinator_ip=ip_of(d, "Mock 2nd Floor Storage Room"))
    d.speaker_action("leave", ip_of(d, "Mock Office"))
    assert d.player.anchor_ip == ip_of(d, "Mock 2nd Floor Storage Room")  # not 2nd Floor, though it sorts first


def test_switching_off_the_only_room_pauses(dj):
    started(dj)
    dj.speaker_action("leave", dj.player.anchor_ip)
    assert dj.paused and dj.player.status()["state"] == "PAUSED_PLAYBACK"
    assert [s["in_group"] for s in dj.speakers if s["coordinator"]] == [True]


def test_room_offset_is_relative_to_main_volume_snapped_and_saved(tmp_path):
    player = MockPlayer(track_seconds=1000)
    d = started(make_dj(tmp_path, player))
    storage = ip_of(d, "Mock 2nd Floor Storage Room")
    d.speaker_action("join", storage)
    d.speaker_action("offset", storage, -6)  # snaps to the nearest knob detent
    assert d.room_offset("Mock 2nd Floor Storage Room") == -5
    target = d.targets["volume"]
    assert player.volumes()[storage] == max(d.cfg["min_volume"], target - 5)
    assert player.volumes()[player.anchor_ip] == target
    d.speaker_action("offset", storage, 99)
    assert d.room_offset("Mock 2nd Floor Storage Room") == 10
    assert d.snapshot()["room_offsets"][storage] == 10
    assert d.snapshot()["limits"]["room_offset_steps"] == [-10, -5, 0, 5, 10]
    assert make_dj(tmp_path, player).room_offset("Mock 2nd Floor Storage Room") == 10


def test_room_offset_from_controller_overrides_config(tmp_path):
    d = make_dj(tmp_path)
    d.cfg = dict(d.cfg, room_volume_offsets={"Mock Office": 4})
    assert d.room_offset("Mock Office") == 4
    d.speaker_action("offset", d.player.anchor_ip, -5)
    assert d.room_offset("Mock Office") == -5


def test_mute_keeps_room_in_group_and_leaves_volume_alone(dj):
    started(dj)
    ip = dj.player.anchor_ip
    dj.speaker_action("mute", ip, True)
    room = next(s for s in dj.speakers if s["ip"] == ip)
    assert room["muted"] and room["in_group"] and room["coordinator"]
    dj.tick(1)
    assert dj.status["state"] == "PLAYING" and dj.player.volumes()[ip] == dj.targets["volume"]  # mute isn't volume 0
    dj.speaker_action("mute", ip, False)
    assert not next(s for s in dj.speakers if s["ip"] == ip)["muted"]


def test_unknown_speaker_is_rejected(dj):
    with pytest.raises(ValueError):
        dj.speaker_action("main", "10.9.9.9")


def test_snapshot_gives_listening_devices_a_precise_speaker_position(dj):
    """The listen switch places this device's playback from position + age; a stale or whole-second
    reading made it jump back and replay (heard as an echo)."""
    dj.play()
    time.sleep(0.3)
    dj.refresh_status()
    time.sleep(0.2)
    st = dj.snapshot()["status"]
    assert isinstance(st["position"], float) and 0.2 < st["position"] < 1.0
    assert 0.15 < st["age"] < 1.0 and "read_at" not in st
    assert "read_at" in dj.status  # the snapshot works on a copy


def test_mock_tracks_last_their_real_length_when_known():
    lengths = {"a": 0.3, "b": None}  # b unknown: falls back to track_seconds
    p = MockPlayer(track_seconds=0.6, duration_of=lambda uri: lengths.get(uri))
    p.start([{"uri": "a"}, {"uri": "b"}, {"uri": "c"}])
    time.sleep(0.4)
    assert p.status()["uri"] == "b"
    time.sleep(0.4)
    assert p.status()["uri"] == "b"  # 0.6 s fallback, not 0.3
    time.sleep(0.35)
    assert p.status()["uri"] == "c"


# -- quiet hours: music off 5-7am ----------------------------------------------------

def at_hour(dj, monkeypatch, hour):
    monkeypatch.setattr(dj, "hour_now", lambda: hour)


def test_quiet_hours_pause_then_resume(dj, monkeypatch):
    at_hour(dj, monkeypatch, 4.9)
    started(dj)
    at_hour(dj, monkeypatch, 5.0); dj.tick(1)
    assert dj.paused and dj.player.status()["state"] == "PAUSED_PLAYBACK"
    at_hour(dj, monkeypatch, 6.5); dj.tick(2)
    assert dj.paused
    at_hour(dj, monkeypatch, 7.0); dj.tick(3)
    assert not dj.paused and dj.player.status()["state"] == "PLAYING"


def test_staff_can_play_during_quiet_hours(dj, monkeypatch):
    at_hour(dj, monkeypatch, 4.9)
    started(dj)
    at_hour(dj, monkeypatch, 5.1); dj.tick(1)
    dj.play(); dj.tick(2); dj.tick(3)
    assert not dj.paused and dj.player.status()["state"] == "PLAYING"  # not paused again


def test_quiet_hours_leave_a_paused_dj_alone(dj, monkeypatch):
    at_hour(dj, monkeypatch, 4.9)
    started(dj)
    dj.pause(); dj.tick(1)
    at_hour(dj, monkeypatch, 5.5); dj.tick(2)
    at_hour(dj, monkeypatch, 7.5); dj.tick(3)
    assert dj.paused  # quiet hours didn't pause it, so they don't resume it


def test_quiet_hours_resume_after_a_restart(tmp_path, monkeypatch):
    player = MockPlayer(track_seconds=1000)
    a = make_dj(tmp_path, player)
    at_hour(a, monkeypatch, 4.9)
    started(a)
    at_hour(a, monkeypatch, 5.2); a.tick(1)
    b = make_dj(tmp_path, player)  # restarted during quiet hours
    b.adopt()
    at_hour(b, monkeypatch, 7.1); b.tick(0); b.tick(1)
    assert b.running and player.status()["state"] == "PLAYING"


def test_quiet_hours_can_wrap_midnight(dj, monkeypatch):
    dj.cfg = dict(dj.cfg, quiet_hours=[[23, 1]])
    for h, q in ((22.9, False), (23.5, True), (0.5, True), (1.0, False)):
        at_hour(dj, monkeypatch, h)
        assert dj.quiet_now() is q, h


def test_auto_volume_back_on_returns_to_the_schedule(dj):
    started(dj)
    dj.set_inputs({"volume_trim": 6})             # an old trim from before
    dj.set_inputs({"auto": False, "manual_volume": 45})  # volume set by hand: auto off
    assert dj.targets["volume"] == 45
    dj.set_inputs({"auto": True})                 # auto back on: the schedule's level, no trim
    assert dj.inputs.volume_trim == 0
    assert dj.targets["volume"] == round(dj.targets["volume_base"])


# -- tone (EQ) ------------------------------------------------------------------------

def test_eq_applies_to_rooms_in_the_group_with_room_trims(dj):
    started(dj)
    office, mezz, entrance = ip_of(dj, "Mock Office"), ip_of(dj, "Mock Mezzanine"), ip_of(dj, "Mock Cafe Entrance")
    dj.speaker_action("join", mezz)
    dj.set_eq(bass=3, treble=-1)
    dj.set_eq(bass=-5, room="Mock Mezzanine")
    assert dj.player.eq(office) == {"bass": 3, "treble": -1, "loudness": True}
    assert dj.player.eq(mezz) == {"bass": -2, "treble": -1, "loudness": True}
    assert dj.player.eq(entrance)["bass"] == 0  # not in the DJ's group: left alone
    dj.speaker_action("join", entrance)          # joins: gets the venue's tone straight away
    assert dj.player.eq(entrance)["bass"] == 3


def test_eq_is_clamped_saved_and_put_back_if_changed_elsewhere(tmp_path):
    player = MockPlayer(track_seconds=1000)
    d = started(make_dj(tmp_path, player))
    d.set_eq(bass=8, treble=4)
    d.set_eq(bass=9, room="Mock Office")         # 8 + 9 is past the Sonos range
    assert d.eq_for("Mock Office")["bass"] == 10
    player.set_eq(player.anchor_ip, bass=0)      # someone changes it in the Sonos app
    d.tick(30)                                   # the once-a-minute check puts it back
    assert player.eq(player.anchor_ip)["bass"] == 10
    b = make_dj(tmp_path, player)
    assert b.eq["bass"] == 8 and b.eq["rooms"]["Mock Office"] == {"bass": 9, "treble": 0}
    d.set_eq(room="Mock Office", reset=True)     # a room back to flat drops its trim
    assert "Mock Office" not in d.eq["rooms"]
    with pytest.raises(ValueError):
        d.set_eq(bass=1, room="Nowhere")


def test_eq_first_run_keeps_the_speakers_current_tone(tmp_path):
    player = MockPlayer(track_seconds=1000)
    mezz = next(ip for ip, r in player.rooms.items() if r["name"] == "Mock Mezzanine")
    player.join(mezz)
    player.set_eq(player.anchor_ip, bass=2, treble=1)  # set in the Sonos app before the DJ had an EQ
    player.set_eq(mezz, bass=-1, treble=1)
    d = started(make_dj(tmp_path, player))
    assert d.eq["bass"] == 2 and d.eq["treble"] == 1 and "adopt" not in d.eq
    assert d.eq["rooms"] == {"Mock Mezzanine": {"bass": -3, "treble": 0}}
    assert player.eq(mezz)["bass"] == -1 and player.eq(player.anchor_ip)["bass"] == 2  # nothing changed


def test_one_vote_per_song_per_play(dj):
    started(dj)
    now, nxt = dj.now_id, dj.upcoming[0]
    for _ in range(3):  # repeated taps (or several people) during one play count once
        dj.vote("up")
    assert dj.votes[now]["up"] == 1
    dj.vote("down")                    # changing your mind mid-play doesn't count either
    assert dj.votes[now].get("down", 0) == 0
    dj.vote("up", nxt); dj.vote("up", nxt)  # "up next" too
    assert dj.votes[nxt]["up"] == 1
    dj.skip(); dj.tick(1)              # next play of the song after: it can be voted on again later
    assert dj.now_id == nxt
    dj.vote("up")                      # nxt's vote was for this same play
    assert dj.votes[nxt]["up"] == 1
    dj.vote("up", now)                 # the first song has played out: a new play takes a new vote
    assert dj.votes[now]["up"] == 2


def test_song_ending_early_outside_the_dj_is_logged(dj, monkeypatch):
    started(dj, duration=200)
    first = dj.now_id
    real = time.time
    monkeypatch.setattr(dj_mod.time, "time", lambda: real() + 60)  # well past the DJ's own start
    at_elapsed(dj, 79); dj.tick(1)
    dj.player.next()                    # someone pressed next in the Sonos app
    dj.tick(2)
    assert any("ended early at 1:19 of 3:20" in e["msg"] and dj.title(first) in e["msg"] for e in dj.events)


def test_the_djs_own_skip_is_not_logged_as_early(dj):
    started(dj, duration=200)
    at_elapsed(dj, 79); dj.tick(1)
    dj.skip(); dj.tick(2); dj.tick(3)
    assert not any("ended early" in e["msg"] for e in dj.events)


def test_feedback_is_logged_with_context(dj):
    started(dj)
    e = dj.add_feedback(["too quiet", "not a tag"], " can't hear it by the window ", "sam")
    assert e["tags"] == ["too quiet"] and e["note"] == "can't hear it by the window" and e["who"] == "sam"
    assert e["song_id"] == dj.now_id and e["volume_target"] == dj.targets["volume"] and e["playing"]
    dj.add_feedback(["bad song"])
    rows = dj.feedback()
    assert [r["tags"] for r in rows] == [["bad song"], ["too quiet"]]  # newest first, and kept on disk
    assert dj.feedback_file.name == "feedback.jsonl" and len(dj.feedback_file.read_text().splitlines()) == 2
    with pytest.raises(ValueError):
        dj.add_feedback([], "  ")

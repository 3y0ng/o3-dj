"""End-to-end DJ behaviour against the silent MockPlayer (no network, no speakers)."""

import json

import pytest

from o3dj import brain, dj as dj_mod
from o3dj.config import ROOT
from o3dj.library import Track
from o3dj.player import MockPlayer
from o3dj.store import JsonFile

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


class FakeCache:
    names = set()
    def __init__(self): self.cached = set()
    def has(self, t): return t.id in self.cached
    def local_path(self, t): return None
    def touch(self, t): pass
    def usage_mb(self): return 0


class FakeMeta:
    def energy(self, i): return None
    def get(self, i): return None
    def count(self): return 0


@pytest.fixture
def dj(tmp_path, monkeypatch):
    real = JsonFile
    monkeypatch.setattr(dj_mod, "JsonFile", lambda path, default: real(tmp_path / path.name, default))
    lib = FakeLibrary()
    d = dj_mod.DJ(CFG, MockPlayer(track_seconds=1000), lib, FakeCache(), FakeMeta())
    return d


def test_start_queues_ahead_and_plays(dj):
    dj.start()
    dj.tick(0)
    assert dj.status["state"] == "PLAYING"
    assert len(dj.upcoming) == CFG["queue_ahead"]
    assert dj.library.get(dj.now_id).genre in dj.inputs.genres


def test_skip_advances_and_tops_up(dj):
    dj.start(); dj.tick(0)
    first, nxt = dj.now_id, dj.upcoming[0]
    dj.skip(); dj.tick(1)
    assert dj.now_id == nxt and dj.now_id != first
    assert len(dj.upcoming) == CFG["queue_ahead"]
    assert dj.votes[first]["skip"] == 1


def test_downvote_skips_current(dj):
    dj.start(); dj.tick(0)
    cur = dj.now_id
    dj.vote("down"); dj.tick(1)
    assert dj.now_id != cur and dj.votes[cur]["down"] == 1


def test_genre_change_repicks_upcoming(dj, monkeypatch):
    dj.start(); dj.tick(0)
    dj.set_inputs({"genres": ["asian"]})
    monkeypatch.setattr(dj_mod.time, "time", lambda: 10**10)
    dj.tick(1)
    assert all(dj.library.get(i).genre == "asian" for i in dj.upcoming)


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


def test_manual_volume_applies_immediately(dj):
    dj.start()
    dj.set_inputs({"auto": False, "manual_volume": 40})
    assert set(dj.player.volumes().values()) == {40}
    dj.set_inputs({"auto": True})
    assert set(dj.player.volumes().values()) == {dj.targets["volume"]}


def test_auto_volume_ramps_gently(dj):
    dj.start(); dj.tick(0)
    ip = dj.player.anchor_ip
    dj.player.set_volume(ip, dj.targets["volume"] + 10)
    dj.tick(1)
    assert dj.player.volumes()[ip] == dj.targets["volume"] + 8

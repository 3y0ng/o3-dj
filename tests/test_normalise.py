"""Loudness normalisation of cached files (needs ffmpeg; no network)."""

import shutil
import subprocess

import pytest

from o3dj import cache as cache_mod
from o3dj.analysis import measure_lufs
from o3dj.library import Track

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")

CFG = {"cache": {"max_mb": 100, "warm_per_genre": 1},
       "normalise": {"enabled": True, "target_lufs": -16, "max_boost_db": 8, "max_cut_db": 15, "skip_within_db": 1.0}}


def tone(path, db, seconds=6):
    """A pink-noise 'song' at a given level (dBFS gain)."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"anoisesrc=c=pink:d={seconds}:a=0.5",
                    "-af", f"volume={db}dB", "-ac", "2", "-b:a", "192k", str(path)], check=True)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(cache_mod, "DATA", tmp_path)
    return cache_mod.Cache(CFG)


def put(cache, tid, db):
    t = Track(id=tid, title=tid, genre="chill", source="chillify", url=f"https://example.com/{tid}.mp3")
    tone(cache.dir / cache.name_for(t), db)
    cache.names.add(cache.name_for(t))
    return t


def test_loud_and_quiet_tracks_end_up_at_the_same_loudness(cache):
    loud, quiet = put(cache, "loud", 10), put(cache, "quiet", -2)  # about -11 and -23 LUFS
    assert measure_lufs(cache.local_path(loud)) - measure_lufs(cache.local_path(quiet)) > 10

    g_loud, g_quiet = cache.normalise(loud), cache.normalise(quiet)
    assert g_loud < 0 < g_quiet
    for t in (loud, quiet):
        assert abs(measure_lufs(cache.local_path(t)) - (-16)) < 1.0
        assert not cache.needs_normalising(t)
    assert cache.gain(loud) == g_loud
    assert not list(cache.dir.glob("*.part"))


def test_boost_is_capped_and_a_redownload_is_levelled_again(cache):
    t = put(cache, "whisper", -15)  # about -36 LUFS
    gain = cache.normalise(t)
    assert gain == CFG["normalise"]["max_boost_db"]
    cache.norm.data.pop(cache.name_for(t))  # what download() does for a fresh copy
    assert cache.needs_normalising(t)


def test_playing_and_next_tracks_are_not_rewritten(cache):
    a, b, c = put(cache, "a", 10), put(cache, "b", 10), put(cache, "c", 10)

    class DJ:
        library = meta = None
        cfg = {"cache": CFG["cache"]}
        def queued_ids(self): return ["a", "b", "c"]

    dj = DJ()
    dj.cache = cache
    pf = cache_mod.Prefetcher(dj)
    assert not pf._normalise(a) and not pf._normalise(b)  # Sonos may be reading these
    assert pf._normalise(c) and cache.gain(c) is not None
    assert cache.needs_normalising(a) and cache.needs_normalising(b)


def test_changing_the_target_relevels_cached_files(cache):
    t = put(cache, "retune", 10)  # about -11 LUFS
    first = cache.normalise(t)
    assert abs(measure_lufs(cache.local_path(t)) - (-16)) < 1.0 and not cache.needs_normalising(t)

    cache.norm_cfg = dict(cache.norm_cfg, target_lufs=-20)
    assert cache.needs_normalising(t)
    total = cache.normalise(t)
    assert abs(measure_lufs(cache.local_path(t)) - (-20)) < 1.0
    assert total < first < 0  # gain is the total applied to the recording, not just the latest change
    entry = cache.norm.data[cache.name_for(t)]
    assert entry["target"] == -20 and abs(entry["lufs"] - (-11)) < 1.5
    assert not cache.needs_normalising(t)


def test_files_levelled_before_targets_were_recorded_are_rechecked(cache):
    t = put(cache, "legacy", 10)
    cache.normalise(t)
    del cache.norm.data[cache.name_for(t)]["target"]  # entry written by the first version
    assert cache.needs_normalising(t)
    gain = cache.normalise(t)  # already at -16: nothing rewritten, prior gain kept
    assert gain == cache.norm.data[cache.name_for(t)]["gain"] < 0

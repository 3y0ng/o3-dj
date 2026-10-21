"""Evening out loudness mistakes inside a song (o3dj/smooth.py)."""

import shutil
import subprocess

import numpy as np
import pytest

from o3dj import smooth
from o3dj.library import Track

SEC = 10  # readings per second


def curve(*parts):
    """[(seconds, LUFS)] -> a short-term loudness curve."""
    return np.concatenate([np.full(int(s * SEC), float(v)) for s, v in parts])


def test_level_song_is_left_alone():
    s = curve((180, -16))
    gain, info = smooth.plan(s + np.random.default_rng(0).normal(0, 1, len(s)), -16)
    assert gain is None and not info["changed"]


def test_quiet_intro_and_ending_are_never_lifted():
    s = curve((20, -30), (140, -16), (20, -30))  # intended quiet start and end
    gain, info = smooth.plan(s, -16)
    assert gain is None and info["max_lift"] == 0


def test_short_break_is_left_alone():
    s = curve((60, -16), (4, -32), (60, -16))  # a 4 s musical break
    gain, _ = smooth.plan(s, -16)
    assert gain is None


def test_long_mid_song_dropout_is_lifted_gently_keeping_its_shape():
    s = curve((60, -16), (15, -28), (60, -16))
    gain, info = smooth.plan(s, -16)
    assert info["changed"] and info["max_lift"] == 6.0          # -28 -> -22: still clearly quieter than -16
    mid = gain[int(64 * SEC):int(71 * SEC)]
    assert np.allclose(mid, mid[0], atol=0.2)                   # one amount across the stretch, not compression
    assert np.abs(np.diff(gain)).max() < 6.0 / (2 * SEC) + 0.05  # eased over ~2 s, no step
    assert abs(gain[: 30 * SEC]).max() < 0.5                    # the rest of the song barely moves


def test_loud_mistake_is_turned_down_before_it_arrives():
    s = curve((60, -17), (20, -8), (60, -17))  # 9 LU over the body for 20 s
    gain, info = smooth.plan(s, -15)
    assert info["changed"] and 4 <= info["max_cut"] <= 6        # down to body + 4 LU
    onset = 60 * SEC
    assert gain[onset] < gain[onset - 3 * SEC] - 1              # already easing down as it arrives
    assert info["max_lift"] == 0


def test_chorus_a_little_louder_is_kept():
    s = curve((40, -17), (20, -14), (40, -17), (20, -14), (40, -17))
    gain, _ = smooth.plan(s, -16)
    assert gain is None


def test_near_silence_is_never_lifted():
    s = curve((60, -16), (15, -60), (60, -16))
    gain, info = smooth.plan(s, -16)
    assert info["max_lift"] == 0


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_smoother_rewrites_once_keeps_original_and_restores(tmp_path, monkeypatch):
    lib, data = tmp_path / "library", tmp_path / "data"
    (lib / "g").mkdir(parents=True)
    monkeypatch.setattr(smooth, "LIBRARY", lib)
    monkeypatch.setattr(smooth, "DATA", data)
    f = lib / "g" / "song.mp3"
    # 40 s loud, 15 s nearly gone, 40 s loud: a mid-song drop-out
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=c=pink:d=95:a=0.3",
                    "-af", "volume='if(between(t,40,55),0.15,1)':eval=frame", "-ac", "2", "-b:a", "192k", str(f)],
                   check=True)
    before = f.read_bytes()
    t = Track(id="x", title="x", genre="g", source="suno", path="g/song.mp3")
    sm = smooth.Smoother({})
    assert sm.needs(t)
    assert sm.smooth(t, safe=lambda: False) is None and f.read_bytes() == before  # became busy: untouched
    info = sm.smooth(t)
    assert info["changed"] and info["max_lift"] > 3 and f.read_bytes() != before
    s, _ = smooth.loudness_curve(f)
    assert s[470] - s[300] > -12                                  # the drop-out is shallower now
    assert not sm.needs(t)
    assert sm.restore() == 1 and f.read_bytes() == before and sm.needs(t)

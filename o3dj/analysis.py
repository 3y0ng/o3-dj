"""Tempo / energy estimation with ffmpeg + numpy.

Chillify ships no tempo metadata, so we listen to 60 s of each track:
  bpm     - autocorrelation of a spectral-flux onset envelope
  rms     - loudness
  onset   - how busy the rhythm is
  bright  - share of high-frequency content

These combine into a raw score, and a track's *energy* (0..1) is its
percentile rank among every analysed track, so the scale is always relative
to what is actually in the library.
"""

import bisect
import subprocess
import threading

import numpy as np

from .config import DATA
from .store import JsonFile

SR = 11025


def analyse(src, offset=30, seconds=60):
    """src is a local path or an http(s) URL (ffmpeg range-reads only what it needs)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", str(offset), "-t", str(seconds)]
    if str(src).startswith("http"):
        cmd += ["-rw_timeout", "20000000"]
    cmd += ["-i", str(src), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, timeout=90).stdout
    x = np.frombuffer(raw, np.float32)
    if len(x) < SR * 5 and offset:
        return analyse(src, offset=0, seconds=seconds)  # short track
    if len(x) < SR * 5:
        raise ValueError("track too short to analyse")

    hop, win = 256, 1024
    n = 1 + (len(x) - win) // hop
    frames = np.lib.stride_tricks.as_strided(x, (n, win), (x.strides[0] * hop, x.strides[0]))
    spec = np.abs(np.fft.rfft(frames * np.hanning(win), axis=1))
    flux = np.maximum(0, np.diff(np.log1p(spec * 10), axis=0)).sum(1)
    flux = np.maximum(flux - np.convolve(flux, np.ones(16) / 16, "same"), 0)

    fps = SR / hop
    ac = np.correlate(flux, flux, "full")[len(flux) - 1:]
    bpm_of = 60 * fps / np.maximum(np.arange(len(ac)), 1)
    weight = np.exp(-0.5 * (np.log2(bpm_of / 90) / 0.6) ** 2)
    weight[(bpm_of < 55) | (bpm_of > 170)] = 0
    lag = int(np.argmax(ac * weight))

    return {
        "bpm": round(60 * fps / lag, 1),
        "rms": round(float(np.sqrt(np.mean(x ** 2))), 4),
        "onset": round(float(np.mean(flux > flux.mean() + flux.std())), 4),
        "bright": round(float(spec[:, spec.shape[1] // 4:].sum() / spec.sum()), 4),
    }


def raw_score(f):
    clip = lambda v: min(max(v, 0.0), 1.0)
    return (0.5 * clip((f["bpm"] - 60) / 70)
            + 0.2 * clip(f["rms"] / 0.3)
            + 0.2 * clip(f["onset"] / 0.15)
            + 0.1 * clip(f["bright"] / 0.3))


class Meta:
    """Per-track analysis results, persisted to data/track_meta.json."""

    def __init__(self):
        self.file = JsonFile(DATA / "track_meta.json", {})
        self.lock = threading.Lock()
        self._sorted = None

    def has(self, track_id):
        return track_id in self.file.data

    def get(self, track_id):
        return self.file.data.get(track_id)

    def put(self, track_id, features):
        with self.lock:
            self.file.data[track_id] = features
            self._sorted = None
        self.file.save()

    def mark_failed(self, track_id):
        self.put(track_id, {"failed": True})

    def count(self):
        return sum(1 for f in self.file.data.values() if "bpm" in f)

    def energy(self, track_id):
        """0..1 percentile energy, or None if the track hasn't been analysed."""
        f = self.file.data.get(track_id)
        if not f or "bpm" not in f:
            return None
        with self.lock:
            if self._sorted is None:
                self._sorted = sorted(raw_score(v) for v in self.file.data.values() if "bpm" in v)
            s = self._sorted
        if len(s) < 2:
            return 0.5
        return bisect.bisect_left(s, raw_score(f)) / (len(s) - 1)

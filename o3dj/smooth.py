"""Even out loudness mistakes inside a song, without touching its intended shape.

Whole-song levelling makes every song start at the same level, but a few generated songs have a part that is far
louder than the rest, or drop almost out for a long stretch mid-song and then come slamming back. This rides the
song's level the way an engineer rides a fader, against the song's own main level (its "body"):

  - parts more than `tolerance_lu` louder than the body are turned down to it (a chorus a little louder is left alone);
  - a long stretch (>= `dip_min_s`) far below the body (> `dip_lu`) in the middle of the song is lifted by a constant
    amount (its own shape kept), so the return isn't a jump; short breaks are musical and left alone;
  - the first and last `protect_s` seconds are never lifted: a song that starts or ends quiet on purpose still does;
  - every change starts a moment early and eases in and out over seconds (no pumping), and the song is brought
    back to the loudness it had (capped, through a -1 dBFS limiter) so it still sits level with the others.

Songs that need less than `min_change_db` are left byte-for-byte alone. The original of every rewritten file is kept
in data/originals/ (`python -m o3dj.smooth --restore` puts them all back; `--dry-run` lists what would change).
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from .analysis import NORM_FORMATS
from .config import DATA, LIBRARY
from .store import JsonFile

log = logging.getLogger(__name__)

VERSION = 1
STEP = 0.1  # seconds between ebur128 short-term readings

DEFAULTS = {
    "enabled": True,
    "tolerance_lu": 4.0,   # parts up to this much louder than the body are left alone (choruses, drops)
    "max_cut_db": 10.0,    # never turn anything down by more than this
    "dip_lu": 8.0,         # a mid-song stretch this far below the body...
    "dip_min_s": 8.0,      # ...for at least this long is lifted...
    "dip_target_lu": 6.0,  # ...to this far below the body (still clearly a quiet part)
    "max_lift_db": 6.0,
    "protect_s": 25.0,     # never lift the first/last this many seconds (intended quiet intros and endings)
    "silence_lufs": -45.0, # never lift near-silence (it would only raise noise)
    "min_change_db": 1.5,  # smaller fixes aren't worth a re-encode
    "max_makeup_db": 3.0,  # how far the whole song may be moved back to its old loudness
    "lead_s": 1.0,         # changes are in place this long before the part they fix
    "ease_s": 2.0,         # length of each ease in/out
    "bitrate": "192k",
}


def loudness_curve(path):
    """(short-term loudness every 0.1 s in LUFS, integrated LUFS)."""
    err = subprocess.run(["ffmpeg", "-v", "verbose", "-nostats", "-nostdin", "-i", str(path),
                          "-af", "ebur128=framelog=verbose", "-f", "null", "-"],
                         capture_output=True, text=True, timeout=300).stderr
    s = np.array([float(v) for v in re.findall(r"\bt:\s*[\d.]+.*?\bS:\s*(-?[\d.]+|-inf)", err)
                  if v != "-inf"] or [], float)
    found = re.findall(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", err, re.M)
    if len(s) < 50 or not found:
        raise ValueError("no loudness reading")
    return s, float(found[-1])


def _runs(mask):
    """[(start, end)] of consecutive True values."""
    edges = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(int), [0]])))
    return list(zip(edges[::2], edges[1::2]))


def plan(s, integrated, cfg=None):
    """The gain (dB) to apply every 0.1 s, and a summary; (None, summary) when the song is fine. Pure (testable)."""
    c = {**DEFAULTS, **(cfg or {})}
    s = np.maximum(np.asarray(s, float), -70.0)
    n = len(s)
    gate = s > integrated - 10
    body = float(np.median(s[gate])) if gate.any() else integrated
    # cuts: anything above the ceiling comes down to it
    cut = np.clip(body + c["tolerance_lu"] - s, -c["max_cut_db"], 0.0)
    # lifts: long stretches far below the body, away from the start and end, by one amount per stretch
    lift = np.zeros(n)
    protect, min_len = int(c["protect_s"] / STEP), int(c["dip_min_s"] / STEP)
    low = np.zeros(n, bool)
    low[protect:max(protect, n - protect)] = True
    low &= s < body - c["dip_lu"]
    for a, b in _runs(low):
        level = float(np.median(s[a:b]))
        if b - a >= min_len and level > c["silence_lufs"]:
            lift[a:b] = np.clip(body - c["dip_target_lu"] - level, 0.0, c["max_lift_db"])
    # in place a moment early and held a moment after, then eased (a moving average never deepens a change)
    lead, ease = int(c["lead_s"] / STEP), max(1, int(c["ease_s"] / STEP))
    win = lambda x, f: np.array([f(x[max(0, i - lead):i + lead + 1]) for i in range(n)])
    smooth = lambda x: np.convolve(np.concatenate([np.zeros(ease), x, np.zeros(ease)]), np.ones(ease) / ease,
                                   "same")[ease:-ease]
    cut = np.minimum(smooth(win(cut, np.min)), 0.0)
    lift = np.maximum(smooth(win(lift, np.max)), 0.0)
    lift[:protect] = 0.0  # (easing can't spill into the protected start/end)
    lift[max(protect, n - protect):] = 0.0
    g = cut + lift
    info = {"body": round(body, 1), "max_cut": round(float(-cut.min()), 1), "max_lift": round(float(lift.max()), 1),
            "seconds_cut": round(float((cut < -1).sum() * STEP), 1),
            "seconds_lifted": round(float((lift > 1).sum() * STEP), 1)}
    if max(info["max_cut"], info["max_lift"]) < c["min_change_db"]:
        return None, {**info, "changed": False}
    # back to the song's old loudness (energy mean over the gated frames), capped
    before = 10 * np.log10(np.mean(10 ** (s[gate] / 10)))
    after = 10 * np.log10(np.mean(10 ** ((s[gate] + g[gate]) / 10)))
    makeup = float(np.clip(before - after, -c["max_makeup_db"], c["max_makeup_db"]))
    return g + makeup, {**info, "makeup": round(makeup, 1), "changed": True}


def _rate_channels(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "stream=sample_rate,channels", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=30).stdout.strip().split(",")
    return int(out[0]), int(out[1])


def render(src, dst, gain_db, bitrate="192k"):
    """Write src to dst with a time-varying gain (one value per 0.1 s), through a -1 dBFS limiter."""
    fmt, codec = NORM_FORMATS[Path(src).suffix.lower()]
    sr, ch = _rate_channels(src)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(src), "-map", "0:a:0",
                          "-f", "f32le", "-ac", str(ch), "-ar", str(sr), "-"],
                         capture_output=True, check=True, timeout=300).stdout
    x = np.frombuffer(raw, np.float32).reshape(-1, ch)
    t = np.arange(len(x)) / sr
    curve = np.interp(t, (np.arange(len(gain_db)) + 0.5) * STEP, gain_db)
    y = (x * (10 ** (curve / 20))[:, None]).astype(np.float32)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "f32le", "-ar", str(sr), "-ac", str(ch), "-i", "-",
           "-i", str(src), "-map", "0:a", "-map_metadata", "1",
           "-af", "alimiter=limit=0.891:level=false:latency=true", "-c:a", codec]
    if codec in ("libmp3lame", "aac"):
        cmd += ["-b:a", bitrate]
    if fmt == "mp3":
        cmd += ["-id3v2_version", "3"]
    subprocess.run(cmd + ["-f", fmt, str(dst)], input=y.tobytes(), capture_output=True, check=True, timeout=300)


class Smoother:
    """Smooths library files once each; data/smoothed.json records what was done."""

    def __init__(self, cfg):
        self.cfg = {**DEFAULTS, **cfg.get("smooth", {})}
        self.done = JsonFile(DATA / "smoothed.json", {})  # library-relative path -> summary
        self.originals = DATA / "originals"

    def needs(self, track):
        if not (self.cfg["enabled"] and track.path and Path(track.path).suffix.lower() in NORM_FORMATS):
            return False
        entry = self.done.data.get(track.path)
        return not entry or entry.get("version") != VERSION

    def smooth(self, track, safe=lambda: True):
        """Smooth one library file. Returns the summary, or None if `safe()` said no just before swapping the file
        in (it became the playing or next song while we worked; try again later)."""
        path = LIBRARY / track.path
        s, integrated = loudness_curve(path)
        gain, info = plan(s, integrated, self.cfg)
        if gain is not None:
            backup = self.originals / track.path
            if not backup.exists():  # keep the first original only
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
            tmp = path.with_name(path.name + ".smooth.part")
            try:
                render(path, tmp, gain, self.cfg["bitrate"])
                if not safe():
                    return None
                os.replace(tmp, path)  # atomic: a reader mid-file keeps the old one
            finally:
                tmp.unlink(missing_ok=True)
        with self.done.lock:
            self.done.data[track.path] = {**info, "version": VERSION, "at": time.strftime("%Y-%m-%d %H:%M")}
        self.done.save()
        return info

    def mark_failed(self, track, err):
        with self.done.lock:
            self.done.data[track.path] = {"failed": str(err)[:120], "version": VERSION}
        self.done.save()

    def restore(self):
        """Put every original back (and forget what was done)."""
        n = 0
        for b in self.originals.rglob("*"):
            if b.is_file():
                rel = b.relative_to(self.originals)
                os.replace(b, LIBRARY / rel)
                self.done.data.pop(str(rel), None)
                n += 1
        self.done.save()
        return n


def main(argv):
    """python -m o3dj.smooth [--dry-run | --restore | --preview FILE OUT]"""
    import json
    from concurrent.futures import ThreadPoolExecutor
    from .config import load
    cfg = load()
    if argv[:1] == ["--restore"]:
        print(f"restored {Smoother(cfg).restore()} files (restart the DJ so it re-checks them)")
        return
    if argv[:1] == ["--preview"]:
        s, i = loudness_curve(argv[1])
        gain, info = plan(s, i, {**DEFAULTS, **cfg.get("smooth", {})})
        print(json.dumps(info))
        if gain is not None:
            render(argv[1], argv[2], gain)
        return
    def one(p):
        s, i = loudness_curve(p)
        return plan(s, i, {**DEFAULTS, **cfg.get("smooth", {})})[1], p.relative_to(LIBRARY)
    with ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(one, [p for p in sorted(LIBRARY.rglob("*")) if p.suffix.lower() in NORM_FORMATS]))
    changed = [r for r in rows if r[0]["changed"]]
    for info, rel in sorted(changed, key=lambda r: -max(r[0]["max_cut"], r[0]["max_lift"])):
        print(f"cut {info['max_cut']:4.1f} dB/{info['seconds_cut']:5.1f} s  lift {info['max_lift']:4.1f} dB/"
              f"{info['seconds_lifted']:5.1f} s  makeup {info['makeup']:+.1f}  {rel}")
    print(f"{len(changed)} of {len(rows)} songs would be smoothed")


if __name__ == "__main__":
    main(sys.argv[1:])

"""Local audio cache + background prefetch/analysis worker.

The cache is the resilience layer:
  - tracks queued next are downloaded ahead of time, and when cached the
    speakers stream them from this machine over the LAN, so an internet
    blip mid-song doesn't cut the music;
  - a "warm set" of tracks per genre is kept on disk so that if chillify.me
    (or the internet) is down, the DJ keeps playing from cache.

Cached files are also loudness-normalised (EBU R128) once, so no track is much louder
or quieter than the rest; data/normalised.json records each file's measured loudness and gain.
"""

import hashlib
import logging
import os
import random
import shutil
import time
import urllib.parse
from pathlib import Path

import requests

from . import brain
from .analysis import NORM_FORMATS, analyse, apply_gain, measure_lufs, probe_duration
from .config import CACHE_DIR, DATA, LIBRARY
from .store import JsonFile

log = logging.getLogger(__name__)


class Cache:
    def __init__(self, cfg):
        self.cfg = cfg["cache"]
        self.dir = CACHE_DIR
        self.names = {n for n in os.listdir(self.dir) if not n.endswith(".part")}
        self.norm_cfg = cfg.get("normalise", {})
        self.norm = JsonFile(DATA / "normalised.json", {})  # cache file name -> {lufs, gain}

    def name_for(self, track):
        ext = Path(urllib.parse.urlparse(track.url or "").path).suffix.lower() or ".mp3"
        return hashlib.sha1(track.id.encode()).hexdigest()[:16] + ext

    def local_path(self, track):
        """Path of a playable local copy, or None."""
        if track.path:
            p = LIBRARY / track.path
            return p if p.exists() else None
        name = self.name_for(track)
        return self.dir / name if name in self.names else None

    def has(self, track):
        return self.local_path(track) is not None

    def download(self, track):
        name = self.name_for(track)
        tmp = self.dir / (name + ".part")
        with requests.get(track.url, stream=True, timeout=(10, 60),
                          headers={"User-Agent": "o3-dj/0.1"}) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        os.replace(tmp, self.dir / name)
        self.names.add(name)
        self.norm.data.pop(name, None)  # a fresh copy hasn't been levelled yet

    # -- loudness normalisation ---------------------------------------------------
    def can_normalise(self, track):
        return (self.norm_cfg.get("enabled", True) and not track.path
                and Path(self.name_for(track)).suffix in NORM_FORMATS)

    def target(self):
        return self.norm_cfg.get("target_lufs", -16)

    def needs_normalising(self, track):
        """Not levelled yet, or levelled for a different target_lufs (so changing the target takes effect)."""
        if not (self.can_normalise(track) and self.has(track)):
            return False
        entry = self.norm.data.get(self.name_for(track))
        return entry is None or entry.get("target") != self.target()

    def gain(self, track):
        """Total dB applied to the cached file (0 if it was already close), or None if not normalised."""
        entry = self.norm.data.get(self.name_for(track))
        return entry["gain"] if entry else None

    def normalise(self, track):
        """Measure the cached file and rewrite it at the target loudness. Returns the total gain in dB.
        A file levelled before (for another target) already carries a gain; the caps apply to the total."""
        c = self.norm_cfg
        name = self.name_for(track)
        path = self.dir / name
        prior = (self.norm.data.get(name) or {}).get("gain") or 0.0
        lufs = measure_lufs(path)  # as the file is now, i.e. including any prior gain
        total = prior
        if lufs > -50:  # near-silent reading: leave it alone rather than boost noise
            total = max(-c.get("max_cut_db", 15), min(c.get("max_boost_db", 8), prior + self.target() - lufs))
        change = round(total - prior, 1)
        if abs(change) >= c.get("skip_within_db", 1.0):
            tmp = self.dir / (name + ".norm.part")
            try:
                apply_gain(path, tmp, change, c.get("bitrate", "192k"))
                os.replace(tmp, path)  # atomic: a reader mid-file keeps the old one
            finally:
                tmp.unlink(missing_ok=True)
        else:
            change = 0.0
        gain = round(prior + change, 1)
        with self.norm.lock:  # lufs: the track as recorded, before any levelling
            self.norm.data[name] = {"lufs": round(lufs - prior, 1), "gain": gain, "target": self.target()}
        self.norm.save()
        return gain

    def touch(self, track):
        p = self.local_path(track)
        if p and not track.path:
            os.utime(p)

    def usage_mb(self):
        # names is a set the prefetch thread adds to while the web thread reads it; iterate a snapshot
        total = 0
        for n in list(self.names):
            try:
                total += (self.dir / n).stat().st_size
            except FileNotFoundError:
                pass
        return total / 1e6

    def evict(self, protect):
        limit = self.cfg["max_mb"]
        if self.usage_mb() <= limit:
            return
        files = sorted((self.dir / n for n in list(self.names) if n not in protect and (self.dir / n).exists()),
                       key=lambda p: p.stat().st_mtime)
        for p in files:
            if self.usage_mb() <= limit * 0.9:
                break
            p.unlink(missing_ok=True)
            self.names.discard(p.name)
            self.norm.data.pop(p.name, None)
        self.norm.save()


class Prefetcher:
    """One background thread: download upcoming, keep warm set, analyse tempo."""

    def __init__(self, dj):
        self.dj = dj
        self.lib, self.cache, self.meta = dj.library, dj.cache, dj.meta
        self.cfg = dj.cfg["cache"]
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        self.status = "idle"
        self.no_duration = set()

    def _analyse(self, track, src):
        self.status = f"analysing {track.title}"
        try:
            f = analyse(src)
            gain = self.cache.gain(track) if src == self.cache.local_path(track) else None
            if gain:  # energy should reflect the track as recorded, not after normalising
                f["rms"] = round(f["rms"] * 10 ** (-gain / 20), 4)
            self.meta.put(track.id, f)
        except Exception as e:
            log.info("analysis failed for %s: %s", track.id, e)
            self.meta.mark_failed(track.id)

    def _download(self, track, why):
        self.status = f"caching {track.title}"
        try:
            self.cache.download(track)
            log.info("cached (%s) %s", why, track.title)
            self._normalise(track)
            if self.has_ffmpeg and not self.meta.duration(track.id):
                try:
                    self.meta.set_duration(track.id, probe_duration(self.cache.local_path(track)))
                except Exception:
                    pass
            return True
        except Exception as e:
            log.info("download failed for %s: %s", track.id, e)
            self.dj.note_failure(track.id)
            return False

    def _normalise(self, track):
        """Normalise a cached file, unless the speakers may be reading it right now
        (the playing track, or the next one, which Sonos pre-loads)."""
        if not (self.has_ffmpeg and self.cache.needs_normalising(track)):
            return False
        if track.id in self.dj.queued_ids()[:2]:
            return False
        self.status = f"levelling {track.title}"
        try:
            gain = self.cache.normalise(track)
            log.info("normalised %s (%+.1f dB)", track.title, gain)
        except Exception as e:
            log.info("normalise failed for %s: %s", track.id, e)
            with self.cache.norm.lock:  # don't retry forever; it still plays as it is
                prior = (self.cache.norm.data.get(self.cache.name_for(track)) or {}).get("gain") or 0.0
                self.cache.norm.data[self.cache.name_for(track)] = {"lufs": None, "gain": prior, "failed": True,
                                                                     "target": self.cache.target()}
        return True

    def step(self):
        """Do one unit of work. Returns True if something was done."""
        online = self.dj.online
        # 0. know how long queued tracks are (the DJ avoids editing the queue near a track's end)
        if self.has_ffmpeg:
            for tid in self.dj.queued_ids():
                t = self.lib.get(tid)
                if t and not self.meta.duration(t.id) and tid not in self.no_duration:
                    src = self.cache.local_path(t) or (t.url if online else None)
                    if src:
                        self.status = f"measuring {t.title}"
                        try:
                            self.meta.set_duration(t.id, probe_duration(src))
                        except Exception as e:
                            log.info("duration probe failed for %s: %s", t.id, e)
                        if not self.meta.duration(t.id):
                            self.no_duration.add(tid)
                        return True

        # 1. upcoming tracks first
        for tid in self.dj.queued_ids():
            t = self.lib.get(tid)
            if t and t.url and not self.cache.has(t) and online:
                return self._download(t, "upcoming")

        # 1b. level upcoming tracks that were cached before they could be normalised
        for tid in self.dj.queued_ids():
            t = self.lib.get(tid)
            if t and self._normalise(t):
                return True

        # 2. level, then analyse, anything already on disk
        if self.has_ffmpeg:
            for t in self.lib.all():
                if self._normalise(t):
                    return True
            for t in self.lib.all():
                if not self.meta.has(t.id) and self.cache.has(t):
                    self._analyse(t, self.cache.local_path(t))
                    return True

        genres = list(self.lib.genres())
        random.shuffle(genres)
        selected = set(self.dj.inputs.genres)
        genres.sort(key=lambda g: g not in selected)

        # 3. warm set per genre, favouring good-vote tracks
        if online:
            votes = self.dj.votes
            for g in genres:
                tracks = [t for t in self.lib.in_genre(g) if t.url]
                if sum(self.cache.has(t) for t in tracks) >= self.cfg["warm_per_genre"]:
                    continue
                cands = [t for t in tracks if not self.cache.has(t) and not brain.banned(votes.get(t.id))
                         and not self.dj.failed(t.id)]
                if cands:
                    t = random.choices(cands, [brain.vote_factor(votes.get(c.id)) for c in cands])[0]
                    return self._download(t, f"warm {g}")

        # 4. analyse streaming tracks remotely (ffmpeg reads ~60 s via range requests)
        if online and self.has_ffmpeg and self.cfg.get("analyse_remote", True):
            for g in genres:
                todo = [t for t in self.lib.in_genre(g) if t.url and not self.meta.has(t.id)]
                if todo:
                    t = random.choice(todo)
                    self._analyse(t, t.url)
                    return True
        return False

    def run(self):
        while True:
            try:
                did = self.step()
                if not did:
                    self.status = "idle"
                self.cache.evict(protect={self.cache.name_for(t) for t in
                                          filter(None, map(self.lib.get, self.dj.queued_ids()))})
                time.sleep(1 if did else 5)
            except Exception:
                log.exception("prefetcher error")
                time.sleep(10)

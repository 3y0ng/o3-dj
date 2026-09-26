"""Local audio cache + background prefetch/analysis worker.

The cache is the resilience layer:
  - tracks queued next are downloaded ahead of time, and when cached the
    speakers stream them from this machine over the LAN, so an internet
    blip mid-song doesn't cut the music;
  - a "warm set" of tracks per genre is kept on disk so that if chillify.me
    (or the internet) is down, the DJ keeps playing from cache.
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
from .analysis import analyse, probe_duration
from .config import CACHE_DIR, LIBRARY

log = logging.getLogger(__name__)


class Cache:
    def __init__(self, cfg):
        self.cfg = cfg["cache"]
        self.dir = CACHE_DIR
        self.names = set(os.listdir(self.dir))

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
            self.meta.put(track.id, analyse(src))
        except Exception as e:
            log.info("analysis failed for %s: %s", track.id, e)
            self.meta.mark_failed(track.id)

    def _download(self, track, why):
        self.status = f"caching {track.title}"
        try:
            self.cache.download(track)
            log.info("cached (%s) %s", why, track.title)
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

        # 2. analyse anything already on disk
        if self.has_ffmpeg:
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

"""The DJ: keeps a short rolling queue on the speakers, re-picks upcoming
tracks when the atmosphere changes, ramps volume, and falls back to the
local cache when the music source is unreachable."""

import logging
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import brain
from .config import DATA
from .store import JsonFile

log = logging.getLogger(__name__)

MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "application/ogg"}
DISCRETE_INPUTS = {"genres", "weather", "occupancy_enabled", "hour_override"}
VOLUME_INPUTS = {"auto", "manual_volume", "volume_trim"}


def lan_ip_towards(host):
    """The address of this machine as seen from `host` (a speaker)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 1400))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class DJ:
    def __init__(self, cfg, player, library, cache, meta):
        self.cfg, self.player, self.library, self.cache, self.meta = cfg, player, library, cache, meta
        self.store = JsonFile(DATA / "state.json", {"inputs": {}, "votes": {}, "history": []})
        saved = self.store.data.get("inputs") or {"genres": cfg["default_genres"]}
        self.inputs = brain.Inputs.from_dict(saved)
        self.votes = self.store.data.setdefault("votes", {})
        self.history = deque(self.store.data.get("history", []), maxlen=200)

        self.lock = threading.RLock()
        self.running = False     # DJ is in charge of the speakers
        self.paused = False
        self.now_id = None
        self.upcoming = []       # track ids queued after the current one
        self.uri_map = {}        # uri -> track id
        self.failures = {}       # track id -> (count, last time)
        self.dirty_at = None     # atmosphere changed; re-pick upcoming soon
        self.stalled_ticks = 0
        self.foreign_ticks = 0
        self.status = {}
        self.speakers = []
        self.volumes = {}
        self.targets = self.compute()
        self.events = deque(maxlen=14)
        self.prefetcher = None

        host = player.anchor_ip if player.live else "127.0.0.1"
        self.base_url = f"http://{lan_ip_towards(host)}:{cfg['port']}"
        self.event("DJ ready" + ("" if player.live else " (mock speakers)"))

    # -- helpers ---------------------------------------------------------------
    def event(self, msg):
        self.events.appendleft({"t": datetime.now().strftime("%H:%M"), "msg": msg})
        log.info(msg)

    @property
    def online(self):
        return self.library.chillify.online is not False

    def queued_ids(self):
        return [i for i in [self.now_id, *self.upcoming] if i]

    def note_failure(self, tid):
        n, _ = self.failures.get(tid, (0, 0))
        self.failures[tid] = (n + 1, time.time())

    def failed(self, tid):
        n, when = self.failures.get(tid, (0, 0))
        return n >= 2 and time.time() - when < 3600

    def save(self):
        self.store.data["inputs"] = self.inputs.to_dict()
        self.store.data["history"] = list(self.history)
        self.store.save()

    def compute(self):
        now = datetime.now()
        return brain.targets(self.inputs, self.cfg, now.hour + now.minute / 60, self.library.genres())

    def available(self, t):
        if self.failed(t.id):
            return False
        if self.cache.has(t):
            return True
        if t.source == "chillify":
            return self.online
        return bool(t.url)

    def item_for(self, t):
        local = self.cache.local_path(t)
        use_local = local and (self.cfg["cache"]["prefer_local"] or not t.url or not self.online)
        if use_local:
            prefix = "library/" + t.path if t.path else "cache/" + local.name
            from urllib.parse import quote
            uri = f"{self.base_url}/media/{quote(prefix)}"
            ext = local.suffix.lower()
        else:
            uri, ext = t.url, Path(t.url).suffix.lower()
        self.uri_map[uri] = t.id
        label = self.library.genres().get(t.genre, t.genre)
        return {"uri": uri, "title": t.title, "artist": t.artist or "O3 DJ",
                "album": f"O3 DJ - {label}", "mime": MIME.get(ext, "audio/mpeg")}

    def pick(self, n=1):
        tgt = self.targets
        by_genre = {g: self.library.in_genre(g) for g in tgt["weights"]}
        recent = {h["id"] for h in list(self.history)[-60:]} | set(self.queued_ids())
        out = []
        for _ in range(n):
            t = brain.pick(by_genre, tgt, self.meta.energy, self.votes, recent, self.available)
            if not t:
                break
            recent.add(t.id)
            out.append(t)
        if len(out) < n:
            self.event("no playable tracks for this mood" if not out else "running low on tracks")
        return out

    # -- transport ---------------------------------------------------------------
    def start(self):
        with self.lock:
            self.targets = self.compute()
            picks = self.pick(self.cfg["queue_ahead"] + 1)
            if not picks:
                raise RuntimeError("Nothing playable - check the source or cache")
            self.uri_map.clear()
            self.player.start([self.item_for(t) for t in picks])
            self.running, self.paused = True, False
            self.now_id, self.upcoming = picks[0].id, [t.id for t in picks[1:]]
            self._record_play(picks[0].id)
            self.event(f"DJ started: {picks[0].title}")

    def play(self):
        with self.lock:
            if not self.running:
                return self.start()
            self.player.play()
            self.paused = False

    def pause(self):
        with self.lock:
            self.player.pause()
            self.paused = True

    def skip(self):
        with self.lock:
            if not self.running:
                return
            if self.now_id:
                self._vote(self.now_id, "skip")
                t = self.library.get(self.now_id)
                self.event(f"skipped {t.title if t else ''}")
            if not self.upcoming:
                self._top_up(1)
            self.player.next()
            self.paused = False

    def vote(self, direction, tid=None):
        with self.lock:
            tid = tid or self.now_id
            if not tid:
                return
            self._vote(tid, direction)
            t = self.library.get(tid)
            name = t.title if t else tid
            if direction == "down":
                if brain.banned(self.votes.get(tid)):
                    self.event(f"banned {name}")
                else:
                    self.event(f"downvoted {name}")
                if tid == self.now_id and self.running:
                    if not self.upcoming:
                        self._top_up(1)
                    self.player.next()
                elif tid in self.upcoming:
                    self.dirty_at = 0
            else:
                self.event(f"upvoted {name}")

    def _vote(self, tid, key):
        v = self.votes.setdefault(tid, {})
        v[key] = v.get(key, 0) + 1
        self.save()

    def _record_play(self, tid):
        self._vote(tid, "plays")
        self.history.append({"id": tid, "at": int(time.time())})
        t = self.library.get(tid)
        if t:
            self.cache.touch(t)
        self.save()

    def _top_up(self, n):
        picks = self.pick(n)
        if picks:
            self.player.append([self.item_for(t) for t in picks])
            self.upcoming += [t.id for t in picks]

    def refresh_upcoming(self):
        """Replace queued-but-not-started tracks with fresh picks for the current mood."""
        with self.lock:
            if not self.running or not self.status:
                return
            self.player.drop_upcoming(self.status["index"])
            self.upcoming = []
            self._top_up(self.cfg["queue_ahead"])

    # -- inputs --------------------------------------------------------------------
    def set_inputs(self, patch):
        with self.lock:
            changed = set()
            for k, v in patch.items():
                if k not in brain.Inputs.__dataclass_fields__:
                    continue
                if k == "genres":
                    v = [g for g in v if g in self.library.genres()]
                elif k == "weather" and v not in self.cfg["weather"]:
                    continue
                elif k in ("occupancy", "manual_volume"):
                    v = int(max(0, min(100, v)))
                elif k == "volume_trim":
                    v = int(max(-30, min(30, v)))
                elif k == "energy_trim":
                    v = round(max(-0.4, min(0.4, float(v))), 2)
                elif k == "hour_override" and v is not None:
                    v = float(v) % 24
                if getattr(self.inputs, k) != v:
                    setattr(self.inputs, k, v)
                    changed.add(k)
            if not changed:
                return
            self.save()
            self.targets = self.compute()
            if changed & DISCRETE_INPUTS or "energy_trim" in changed or "occupancy" in changed:
                self.dirty_at = 0 if changed & DISCRETE_INPUTS else time.time()
            if changed & VOLUME_INPUTS and self.running:
                self._apply_volume(max_step=100)

    # -- speakers ---------------------------------------------------------------
    def speaker_action(self, action, ip=None):
        with self.lock:
            p = self.player
            if action == "party":
                p.party()
                self.event("party mode: all rooms joined")
            elif action == "join":
                p.join(ip)
            elif action == "leave":
                p.leave(ip)
            elif action == "anchor":
                p.set_anchor(ip)
                self.running = False
                self.event("switched main room - press play to start")
            elif action == "discover":
                p.discover()
            self.speakers = p.speakers()
            if self.running:
                self._apply_volume(max_step=100)

    def _apply_volume(self, max_step=2):
        target = self.targets["volume"]
        names = {s["ip"]: s["name"] for s in self.speakers}
        offsets = self.cfg.get("room_volume_offsets", {})
        vols = self.player.volumes()
        for ip, cur in vols.items():
            want = target + offsets.get(names.get(ip, ""), 0)
            want = max(self.cfg["min_volume"], min(self.cfg["max_volume"], want))
            if cur != want:
                step = max(-max_step, min(max_step, want - cur))
                self.player.set_volume(ip, cur + step)
                vols[ip] = cur + step
        self.volumes = vols

    def refresh_status(self):
        with self.lock:
            try:
                self.status = self.player.status()
            except Exception as e:
                self.player.error = str(e)[:160]
                return
            if self.running:
                self._sync_now(self.uri_map.get(self.status["uri"]))

    def _sync_now(self, cur):
        """Note a track change reported by the speaker."""
        if cur and cur != self.now_id:
            if cur in self.upcoming:
                self.upcoming = self.upcoming[self.upcoming.index(cur) + 1:]
            self.now_id = cur
            self._record_play(cur)

    # -- main loop ------------------------------------------------------------------
    def tick(self, n):
        with self.lock:
            if n % 5 == 0 or not self.speakers:
                self.speakers = self.player.speakers()
            st = self.player.status()
            self.status = st
            self.targets = self.compute()
            if not self.running:
                if n % 5 == 0:
                    self.volumes = self.player.volumes()
                return

            cur = self.uri_map.get(st["uri"])
            if st["uri"] and cur is None and st["state"] == "PLAYING":
                self.foreign_ticks += 1
                if self.foreign_ticks >= 3:
                    self.running = False
                    self.event("speakers taken over by another app - DJ stood down")
                    return
            else:
                self.foreign_ticks = 0

            self._sync_now(cur)

            remaining = st["queue_size"] - st["index"] - 1
            if len(self.upcoming) > remaining:
                self.upcoming = self.upcoming[len(self.upcoming) - max(remaining, 0):]
            if remaining < self.cfg["queue_ahead"]:
                self._top_up(self.cfg["queue_ahead"] - remaining)

            if self.dirty_at is not None and time.time() - self.dirty_at > 2.5:
                self.dirty_at = None
                self.refresh_upcoming()

            # A stream that won't play (bad URL, source down) leaves the speaker STOPPED.
            if st["state"] == "STOPPED" and not self.paused:
                self.stalled_ticks += 1
                if self.stalled_ticks >= 2:
                    self.stalled_ticks = 0
                    if self.now_id:
                        self.note_failure(self.now_id)
                        t = self.library.get(self.now_id)
                        self.event(f"couldn't play {t.title if t else self.now_id} - moving on")
                    if st["queue_size"] - st["index"] - 1 <= 0:
                        self._top_up(1)
                    self.player.play_index(max(st["index"] + 1, 0))
            else:
                self.stalled_ticks = 0

            if st["state"] == "PLAYING":
                self._apply_volume()

    def run(self, interval=2.0):
        n = 0
        while True:
            try:
                self.tick(n)
                self.player.error = None
            except Exception as e:
                msg = str(e)
                if "Timeout" in msg or "Connection" in msg or "Max retries" in msg:
                    msg = f"can't reach speakers at {self.player.anchor_ip} - is this computer on the Sonos Wi-Fi?"
                self.player.error = msg[:160]
                if n % 30 == 0:
                    log.warning("tick failed: %s", e)
            n += 1
            time.sleep(interval)

    def health_loop(self):
        was = self.online
        n = 0
        while True:
            time.sleep(60)
            n += 1
            try:
                if n % 10 == 0:
                    self.library.refresh()
                now = self.library.chillify.ping()
                if now != was:
                    self.event("music source back online" if now else "music source OFFLINE - playing from cache")
                    self.dirty_at = 0
                    was = now
            except Exception:
                log.exception("health check failed")

    # -- snapshot for the UI -------------------------------------------------------
    def track_info(self, tid):
        t = self.library.get(tid) if tid else None
        if not t:
            return None
        f = self.meta.get(tid) or {}
        e = self.meta.energy(tid)
        v = self.votes.get(tid, {})
        return {**t.to_dict(), "genre_label": self.library.genres().get(t.genre, t.genre),
                "energy": None if e is None else round(e, 2), "bpm": f.get("bpm"),
                "up": v.get("up", 0), "down": v.get("down", 0), "plays": v.get("plays", 0),
                "cached": self.cache.has(t)}

    def snapshot(self):
        with self.lock:
            lib = self.library.all()
            genres = self.library.genres()
            per_genre = {g: {"label": label, "tracks": 0, "cached": 0} for g, label in genres.items()}
            for t in lib:
                pg = per_genre[t.genre]
                pg["tracks"] += 1
                pg["cached"] += self.cache.has(t)
            return {
                "live": self.player.live,
                "running": self.running,
                "paused": self.paused,
                "status": self.status,
                "now": self.track_info(self.now_id),
                "upcoming": [self.track_info(i) for i in self.upcoming],
                "inputs": self.inputs.to_dict(),
                "targets": self.targets,
                "genres": per_genre,
                "speakers": self.speakers,
                "volumes": self.volumes,
                "health": {
                    "source_online": self.online,
                    "speaker_error": self.player.error,
                    "base_url": self.base_url,
                    "prefetch": self.prefetcher.status if self.prefetcher else "off",
                },
                "cache": {"files": len(self.cache.names), "mb": round(self.cache.usage_mb()),
                          "analysed": self.meta.count(), "tracks": len(lib)},
                "events": list(self.events),
                "limits": {"min_volume": self.cfg["min_volume"], "max_volume": self.cfg["max_volume"]},
            }

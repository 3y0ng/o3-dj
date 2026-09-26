"""The DJ: keeps a short rolling queue on the speakers, re-picks upcoming
tracks when the atmosphere changes, ramps volume, and falls back to the
local cache when the music source is unreachable.

Queue safety rules (learned the hard way on a real Sonos):
  - never remove the next queue item: Sonos pre-loads it (as soon as the
    current file is buffered, i.e. within seconds for files served from this
    laptop) and removing it stops playback and resets the queue to item 1.
    Mood changes therefore re-pick from the track after next;
  - if the queue does reset, jump to the first *unplayed* DJ track, never
    replay from the top;
  - only count a play once the speaker reports it PLAYING.
"""

import logging
import socket
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from . import brain, calibrate
from .config import DATA
from .live import Live
from .store import JsonFile

log = logging.getLogger(__name__)

MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "application/ogg"}
DISCRETE_INPUTS = {"genres", "weather", "occupancy_enabled", "hour_override"}
LIVE_INPUTS = {"weather", "occupancy", "occupancy_enabled", "hour_override"}  # come from feeds in live mode
CONTINUOUS_INPUTS = {"energy_trim", "occupancy"}
VOLUME_INPUTS = {"auto", "manual_volume", "volume_trim"}

KNOB_SETTLE_SECS = 4      # wait this long after the last knob turn before re-picking
SKIP_COOLDOWN_SECS = 3    # ignore double-taps on skip / not this


def lan_ip_towards(host):
    """The address of this machine as seen from `host` (a speaker)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 1400))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class DJ:
    def __init__(self, cfg, player, library, cache, meta, state_file=DATA / "state.json"):
        self.cfg, self.player, self.library, self.cache, self.meta = cfg, player, library, cache, meta
        self.store = JsonFile(state_file, {"inputs": {}, "votes": {}, "history": []})
        # per-venue calibration lives beside the state file (data/calibration.json, or mock_calibration.json)
        cal_name = state_file.name.replace("state", "calibration") if "state" in state_file.name else "calibration.json"
        self.calibration_store = JsonFile(state_file.with_name(cal_name), {})
        saved = self.store.data.get("inputs") or {"genres": cfg["default_genres"]}
        self.inputs = brain.Inputs.from_dict(saved)
        self.votes = self.store.data.setdefault("votes", {})
        self.history = deque(self.store.data.get("history", []), maxlen=200)
        self.was_running = bool(self.store.data.get("running"))
        self.mode = self.store.data.get("mode", "demo")  # demo: manual atmosphere; live: feeds
        self.live = Live(cfg)
        self._last_effective = None

        self.lock = threading.RLock()
        self.wizard = None       # calibration walk-through in progress (see start_wizard)
        self.running = False     # DJ is in charge of the speakers
        self.paused = False
        self.now_id = None
        self.now_counted = False  # play recorded for now_id
        self.now_max_elapsed = 0  # furthest point now_id was seen playing
        self.upcoming = []       # track ids queued after the current one, in order
        self.uri_map = {}        # uri -> track id
        self.id_uri = {}         # track id -> uri it was queued with
        self.skip_on_start = set()  # downvoted too late to swap out
        self.failures = {}       # track id -> (count, last time)
        self.dirty_at = None     # atmosphere changed; re-pick upcoming soon
        self.stopped_ticks = 0
        self.foreign_ticks = 0
        self.last_skip = 0.0
        self.last_daypart = None
        self.status = {}
        self.speakers = []
        self.volumes = {}
        self.targets = self.compute()
        self.events = deque(maxlen=14)
        self.prefetcher = None
        self._cache_names = {}
        self.fading = False
        self.fade_skips = cfg.get("skip_fade", True)

        self.base_url = self._base_url()
        self.event("DJ ready" + ("" if player.live else " (mock speakers)"))

    # -- helpers ---------------------------------------------------------------
    def event(self, msg):
        self.events.appendleft({"t": datetime.now().strftime("%H:%M"), "msg": msg})
        log.info(msg)

    def _base_url(self):
        host = self.player.anchor_ip if self.player.live else "127.0.0.1"
        return f"http://{lan_ip_towards(host)}:{self.cfg['port']}"

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

    def title(self, tid):
        t = self.library.get(tid) if tid else None
        return t.title if t else (tid or "?")

    def save(self):
        self.store.data["inputs"] = self.inputs.to_dict()
        self.store.data["history"] = list(self.history)
        self.store.data["running"] = self.running
        self.store.data["mode"] = self.mode
        self.store.save()

    def hour_now(self):
        tz = self.live.cfg.get("timezone")
        now = datetime.now(ZoneInfo(tz)) if tz else datetime.now()
        return now.hour + now.minute / 60

    def effective_inputs(self):
        """What the brain sees: manual inputs in demo mode; in live mode the atmosphere comes
        from feeds (or a staff override) and time follows the venue clock."""
        if self.wizard and not self.wizard.get("proposal"):
            sc = calibrate.SCENARIOS[self.wizard["i"]]
            i = brain.Inputs.from_dict(self.inputs.to_dict())
            i.hour_override, i.weather = sc["hour"], sc["weather"]
            i.occupancy, i.occupancy_enabled = sc["occupancy"], True
            i.auto, i.volume_trim, i.energy_trim = True, self.wizard["vol"], self.wizard["nrg"]
            return i
        if self.mode != "live":
            return self.inputs
        e = self.live.effective()
        i = brain.Inputs.from_dict(self.inputs.to_dict())
        i.hour_override = None
        i.weather = e["weather"]
        i.occupancy_enabled = e["occupancy"] is not None
        if e["occupancy"] is not None:
            i.occupancy = e["occupancy"]
        return i

    @property
    def venue_key(self):
        return self.live.cfg.get("venue") or "_default"

    @property
    def calibration(self):
        return self.calibration_store.data.get(self.venue_key, {})

    def compute(self):
        return brain.targets(self.effective_inputs(), self.cfg, self.hour_now(), self.library.genres(), self.calibration)

    def available(self, t):
        if self.failed(t.id):
            return False
        if self.cache.has(t):
            return True
        if t.source == "chillify":
            return self.online
        return bool(t.url)

    def item_for(self, t):
        # Speakers always stream from this laptop: cached files start instantly and survive
        # internet drops; not-yet-cached tracks are passed through from the source by the server
        # (and the prefetcher downloads them within seconds of being queued).
        if t.path:
            uri, ext = f"{self.base_url}/media/{quote('library/' + t.path)}", Path(t.path).suffix.lower()
        elif self.cfg["cache"]["prefer_local"]:
            name = self.cache.name_for(t)
            uri, ext = f"{self.base_url}/media/cache/{quote(name)}", Path(name).suffix.lower()
        else:
            uri, ext = t.url, Path(t.url).suffix.lower()
        self.uri_map[uri] = t.id
        self.id_uri[t.id] = uri
        label = self.library.genres().get(t.genre, t.genre)
        return {"uri": uri, "title": t.title, "artist": t.artist or "O3 DJ",
                "album": f"O3 DJ - {label}", "mime": MIME.get(ext, "audio/mpeg")}

    def track_for_cache_name(self, name):
        if name not in self._cache_names:
            self._cache_names = {self.cache.name_for(t): t for t in self.library.all() if t.url}
        return self._cache_names.get(name)

    def pick(self, n=1, exclude=()):
        tgt = self.targets
        by_genre = {g: self.library.in_genre(g) for g in tgt["weights"]}
        recent = {h["id"] for h in list(self.history)[-60:]} | set(self.queued_ids()) | set(exclude)
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

    def seconds_left(self, st=None):
        """Seconds until the current track ends, or None if its length is unknown."""
        st = st or self.status
        dur = self.meta.duration(self.now_id) if self.now_id else None
        dur = dur or st.get("duration") or None
        return None if not dur else dur - st.get("elapsed", 0)

    # -- calibration walk-through ----------------------------------------------------------
    def start_wizard(self):
        """Play a song for each scenario; staff adjust by ear; then fit this venue's model."""
        with self.lock:
            self.wizard = {"i": 0, "vol": 0, "nrg": 0.0, "samples": [], "repick_at": None,
                           "was_running": self.running, "proposal": None}
            self.event("calibration started: adjust each scenario until it sounds right")
            self._wizard_play()

    def _wizard_play(self):
        """Start a song matching the current scenario (replacing the queue), past its intro, at the target volume."""
        self.targets = self.compute()
        picks = self.pick(2)
        if not picks:
            raise RuntimeError("nothing playable for this scenario")
        self.uri_map.clear()
        self.id_uri.clear()
        self.player.start([self.item_for(t) for t in picks])
        self.running, self.paused = True, False
        self._set_now(picks[0].id)
        self.upcoming = [t.id for t in picks[1:]]
        self.wizard["repick_at"] = None
        self._apply_volume(max_step=100)
        try:
            dur = self.meta.duration(picks[0].id) or 0
            if dur > 90:
                if self.player.live:
                    time.sleep(0.6)  # let the speaker start before seeking
                self.player.seek(45)
        except Exception:
            log.info("seek not supported here")

    def wizard_action(self, action):
        with self.lock:
            w = self.wizard
            if not w:
                raise ValueError("calibration isn't running")
            if action == "another":
                self._wizard_play()
            elif action == "next":
                sc = calibrate.SCENARIOS[w["i"]]
                w["samples"].append({**sc, "volume": self.targets["volume"], "energy": self.targets["energy"]})
                w["i"] += 1
                w["vol"], w["nrg"] = 0, 0.0
                if w["i"] < len(calibrate.SCENARIOS):
                    self._wizard_play()
                else:
                    w["i"] -= 1
                    w["proposal"] = calibrate.fit(w["samples"], self.cfg)
                    self.player.pause()
                    self.event("calibration ready: apply or discard")
            elif action == "back":
                if w["proposal"]:
                    w["proposal"] = None
                elif w["i"] > 0:
                    w["i"] -= 1
                w["samples"] = w["samples"][:w["i"]]
                w["vol"], w["nrg"] = 0, 0.0
                self._wizard_play()
            elif action == "apply":
                if not w["proposal"]:
                    raise ValueError("finish the scenarios first")
                self.calibration_store.data[self.venue_key] = {
                    "model": w["proposal"], "samples": w["samples"],
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
                self.calibration_store.save()
                self.event(f"calibration saved for {self.live.cfg.get('venue_name') or 'this venue'}")
                self._end_wizard()
            elif action in ("stop", "discard"):
                self.event("calibration discarded" if w["proposal"] else "calibration stopped")
                self._end_wizard()
            else:
                raise ValueError(f"unknown action {action!r}")

    def _end_wizard(self):
        was_running = self.wizard["was_running"]
        self.wizard = None
        self.targets = self.compute()
        if was_running:
            self.start()      # fresh queue for the real atmosphere
        else:
            self.player.pause()
            self.running = False
            self.save()

    def reset_calibration(self):
        with self.lock:
            self.calibration_store.data.pop(self.venue_key, None)
            self.calibration_store.save()
            self.targets = self.compute()
            self.dirty_at = time.time()
            self.event("calibration reset to defaults")

    # -- transport ---------------------------------------------------------------
    def start(self):
        with self.lock:
            self.targets = self.compute()
            picks = self.pick(self.cfg["queue_ahead"] + 1)
            if not picks:
                raise RuntimeError("Nothing playable - check the source or cache")
            self.uri_map.clear()
            self.id_uri.clear()
            self.player.start([self.item_for(t) for t in picks])
            self.running, self.paused = True, False
            self._set_now(picks[0].id)
            self.upcoming = [t.id for t in picks[1:]]
            self.save()
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

    def _cooldown(self):
        if time.time() - self.last_skip < SKIP_COOLDOWN_SECS:
            return True
        self.last_skip = time.time()
        return False

    def skip(self):
        with self.lock:
            if not self.running or self._cooldown():
                return
            if self.now_id:
                self._vote(self.now_id, "skip")
                self.event(f"skipped {self.title(self.now_id)}")
            if not self.upcoming:
                self._top_up(1)
            self._next()
            self.paused = False

    def _next(self):
        """Skip, with a quick fade out/in so it isn't a hard cut into silence."""
        if not self.fade_skips or self.fading:
            return self.player.next()
        self.fading = True
        threading.Thread(target=self._fade_skip, daemon=True, name="fade-skip").start()

    def _fade_skip(self):
        try:
            start = self.player.group_volume()
            for f in (0.7, 0.45, 0.25, 0.1):
                self.player.set_group_volume(round(start * f))
                time.sleep(0.1)
            self.player.next()
            for _ in range(25):  # wait (up to 5 s) for the next track to actually start
                time.sleep(0.2)
                if self.player.status()["state"] == "PLAYING":
                    break
            for f in (0.3, 0.5, 0.7, 0.85, 1.0):
                self.player.set_group_volume(round(start * f))
                time.sleep(0.15)
        except Exception:
            log.exception("fade skip failed")
            try:
                self.player.next()
            except Exception:
                pass
        finally:
            self.fading = False

    def vote(self, direction, tid=None):
        with self.lock:
            tid = tid or self.now_id
            if not tid:
                return
            if direction == "down" and tid == self.now_id and self.running and self._cooldown():
                return
            self._vote(tid, direction)
            name = self.title(tid)
            if direction != "down":
                self.event(f"upvoted {name}")
                return
            self.event(f"{'banned' if brain.banned(self.votes.get(tid)) else 'downvoted'} {name}")
            if tid == self.now_id and self.running:
                if not self.upcoming:
                    self._top_up(1)
                self._next()
            elif tid in self.upcoming:
                self._swap_out(tid)

    def _swap_out(self, tid):
        """Replace one upcoming track, or skip it when it starts if it's too late to edit."""
        uri = self.id_uri.get(tid)
        uris = self.player.queue_uris()
        idx = len(uris) - 1 - uris[::-1].index(uri) if uri in uris else None
        st = self.player.status()
        if idx is None or idx <= st["index"] + 1:  # the next item is pre-loaded: never remove it
            self.skip_on_start.add(tid)
            return
        if self.player.remove_index(idx):
            self.upcoming.remove(tid)
            self._top_up(1, exclude={tid})  # don't hand the downvoted track straight back
        else:
            self.skip_on_start.add(tid)

    def _vote(self, tid, key):
        v = self.votes.setdefault(tid, {})
        v[key] = v.get(key, 0) + 1
        self.save()

    def _set_now(self, tid):
        self.now_id, self.now_counted, self.now_max_elapsed = tid, False, 0

    def _count_play(self):
        if self.now_counted or not self.now_id:
            return
        self.now_counted = True
        self._vote(self.now_id, "plays")
        self.history.append({"id": self.now_id, "at": int(time.time())})
        t = self.library.get(self.now_id)
        if t:
            self.cache.touch(t)
        self.save()

    def _top_up(self, n, exclude=()):
        picks = self.pick(n, exclude) if n > 0 else []
        if picks:
            self.player.append([self.item_for(t) for t in picks])
            self.upcoming += [t.id for t in picks]
        return picks

    def refresh_upcoming(self):
        """Replace queued-but-not-started tracks with picks for the current mood.

        Returns False if it's not safe right now (retry next tick)."""
        with self.lock:
            if not self.running:
                return True
            st = self.player.status()
            self.status = st
            if st["state"] == "TRANSITIONING":
                return False
            keep = min(1, len(self.upcoming))  # the next track is pre-loaded by Sonos: leave it
            self.player.drop_from(st["index"] + 1 + keep)
            self.upcoming = self.upcoming[:keep]
            self._top_up(self.cfg["queue_ahead"] - keep)
            return True

    def _jump_to_next_unplayed(self):
        """After a queue reset / failure, carry on with the first DJ track not yet played."""
        uris = self.player.queue_uris()
        for tid in list(self.upcoming):
            uri = self.id_uri.get(tid)
            if uri in uris:
                self.player.play_index(len(uris) - 1 - uris[::-1].index(uri))
                return True
        if self._top_up(1):
            self.player.play_index(len(self.player.queue_uris()) - 1)
            return True
        self.running = False
        self.save()
        self.event("nothing left to play - DJ stopped")
        return False

    def adopt(self):
        """After a restart, take back control of a queue this DJ built earlier."""
        with self.lock:
            if not self.was_running:
                return False
            try:
                uris, st = self.player.queue_uris(), self.player.status()
            except Exception:
                return False
            lookup = {}
            for t in self.library.all():
                if t.url:
                    lookup[t.url] = t.id
                    lookup["/media/cache/" + quote(self.cache.name_for(t))] = t.id
                if t.path:
                    lookup["/media/library/" + quote(t.path)] = t.id
            key = lambda u: u if u in lookup else urlparse(u).path
            ids = [lookup.get(key(u)) for u in uris]
            i = st["index"]
            # only if the speaker is really playing our queue (not e.g. Spotify on the same group)
            if not (0 <= i < len(ids)) or not ids[i] or uris[i] != st["uri"]:
                self.running = False
                self.save()
                return False
            for u, tid in zip(uris, ids):
                if tid:
                    self.uri_map[u], self.id_uri[tid] = tid, u
            self.running = True
            self.paused = st["state"] != "PLAYING"
            self.now_id, self.now_counted, self.now_max_elapsed = ids[i], True, st["elapsed"]
            self.upcoming = [x for x in ids[i + 1:] if x]
            self.status = st
            self.event(f"picked up where it left off: {self.title(ids[i])}")
            return True

    # -- inputs --------------------------------------------------------------------
    def set_inputs(self, patch):
        with self.lock:
            if self.wizard and not self.wizard.get("proposal"):
                if "volume_trim" in patch:
                    self.wizard["vol"] = int(max(-50, min(50, patch["volume_trim"])))
                if "energy_trim" in patch:
                    self.wizard["nrg"] = round(max(-0.5, min(0.5, float(patch["energy_trim"]))), 2)
                    self.wizard["repick_at"] = time.time() + 1.5  # faster/slower: new song once you stop turning
                self.targets = self.compute()
                self._apply_volume(max_step=100)
                return
            changed = set()
            if self.mode == "live":
                patch = dict(patch)
                routed = {k: patch.pop(k) for k in list(patch) if k in LIVE_INPUTS}
                if routed.get("weather") in self.cfg["weather"]:
                    self.live.override("weather", routed["weather"])
                    changed.add("weather")
                if "occupancy" in routed:
                    self.live.override("occupancy", int(max(0, min(100, routed["occupancy"]))))
                    changed.add("occupancy")
                if routed.get("occupancy_enabled") is False:  # occ key off: hand occupancy back to the feed
                    self.live.clear_override("occupancy")
                    changed.add("occupancy")
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
                    v = int(max(-50, min(50, v)))
                elif k == "energy_trim":
                    v = round(max(-0.5, min(0.5, float(v))), 2)
                elif k == "hour_override" and v is not None:
                    v = float(v) % 24
                if getattr(self.inputs, k) != v:
                    setattr(self.inputs, k, v)
                    changed.add(k)
            if not changed:
                return
            self._last_effective = self._effective_key()
            self.save()
            self.targets = self.compute()
            if changed & DISCRETE_INPUTS:
                self.dirty_at = 0
            elif changed & CONTINUOUS_INPUTS:
                self.dirty_at = time.time()
            if changed & VOLUME_INPUTS and self.running:
                self._apply_volume(max_step=100)

    def set_mode(self, mode):
        if mode not in ("demo", "live") or mode == self.mode:
            return
        if mode == "live":
            self.live.poll(force=True)  # network: outside the DJ lock
        with self.lock:
            self.mode = mode
            self.save()
            self.targets = self.compute()
            self._last_effective = self._effective_key()
            self.dirty_at = 0
            e = self.live.effective()
            self.event("LIVE mode: " + f"weather {e['weather_src']}, occupancy {e['occupancy_src']}" if mode == "live"
                       else "DEMO mode: manual atmosphere")
            if mode == "live":
                if not self.live.cfg.get("venue"):
                    self.event("live setup: set live.venue in config.local.json")
                elif self.live.occupancy.missing:
                    self.event("occupancy setup needs: " + ", ".join(self.live.occupancy.missing))
                elif self.live.occupancy.error:
                    self.event("occupancy: " + self.live.occupancy.error)

    def clear_override(self, key):
        with self.lock:
            self.live.clear_override(key)
            self.targets = self.compute()
            self.dirty_at = 0

    def _effective_key(self):
        i = self.effective_inputs()
        return (i.weather, i.occupancy_enabled, i.occupancy // 10 if i.occupancy_enabled else None)

    def _follow_live(self):
        """In live mode, re-pick upcoming tracks when the live atmosphere changes."""
        if self.mode != "live":
            return
        key = self._effective_key()
        if self._last_effective is not None and key != self._last_effective:
            before = self._last_effective
            if key[0] != before[0]:
                self.event(f"live: weather now {key[0]}")
            elif key[1] != before[1]:
                self.event("live: occupancy " + ("available" if key[1] else "unavailable - ignoring it"))
            else:
                self.event(f"live: occupancy now {self.effective_inputs().occupancy}%")
            self.dirty_at = 0 if key[:2] != before[:2] else time.time()
        self._last_effective = key

    def live_loop(self):
        while True:
            try:
                if self.mode == "live":
                    self.live.poll()
            except Exception:
                log.exception("live feeds failed")
            time.sleep(5)

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
                self.save()
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

    # -- following the speaker --------------------------------------------------------
    def refresh_status(self):
        """Called after a button press so the UI updates straight away."""
        with self.lock:
            try:
                self.status = self.player.status()
            except Exception as e:
                self.player.error = str(e)[:160]
                return
            if self.running and self.status["state"] == "PLAYING":
                self._on_playing(self.status, self.uri_map.get(self.status["uri"]))

    def _on_playing(self, st, cur):
        """Track the speaker while it plays. Returns True if it had to intervene."""
        if self.paused:
            self.paused = False
        if cur is None or cur == self.now_id:
            if cur:
                self.now_max_elapsed = max(self.now_max_elapsed, st["elapsed"])
                self._count_play()
            return False
        already_played = cur in {h["id"] for h in list(self.history)[-60:]}
        if cur in self.upcoming or not already_played:
            # normal advance (or a DJ track we lost track of during a transition: still new music)
            if cur in self.upcoming:
                self.upcoming = self.upcoming[self.upcoming.index(cur) + 1:]
            self._set_now(cur)
            self._count_play()
            if cur in self.skip_on_start:
                self.skip_on_start.discard(cur)
                self.event(f"skipping downvoted {self.title(cur)}")
                if not self.upcoming:
                    self._top_up(1)
                self.player.next()
                return True
            return False
        # A DJ track that already played: the queue reset to the top, or someone tapped an old track.
        self.event(f"queue jumped back to {self.title(cur)} - carrying on with new music")
        self._jump_to_next_unplayed()
        return True

    def _on_stopped(self, st, cur):
        left = self.seconds_left(st) if cur == self.now_id else None
        mid_song = cur == self.now_id and self.now_max_elapsed >= 10 and (left is None or left > 15)
        if mid_song and self.online:
            # Someone stopped it (Sonos app, grouping change). Respect that.
            self.paused = True
            self.event("stopped from the Sonos app - press play to resume")
            return
        if cur == self.now_id and self.now_max_elapsed < 3:
            self.note_failure(self.now_id)
            self.event(f"couldn't play {self.title(self.now_id)} - moving on")
        elif cur != self.now_id:
            self.event("speaker queue reset - carrying on")
        self._jump_to_next_unplayed()

    # -- main loop ------------------------------------------------------------------
    def tick(self, n):
        with self.lock:
            if n % 5 == 0 or not self.speakers:
                self.speakers = self.player.speakers()
            st = self.player.status()
            self.status = st
            self.targets = self.compute()
            if self.wizard:
                w = self.wizard
                if not w.get("proposal") and ((w["repick_at"] and time.time() >= w["repick_at"]) or st["state"] == "STOPPED"):
                    self._wizard_play()
                return
            self._check_daypart()
            self._follow_live()
            if not self.running:
                if n % 5 == 0:
                    self.volumes = self.player.volumes()
                return

            cur = self.uri_map.get(st["uri"])
            state = st["state"]
            if st["uri"] and cur is None and state == "PLAYING":
                self.foreign_ticks += 1
                if self.foreign_ticks >= 3:
                    self.running = False
                    self.save()
                    self.event("speakers taken over by another app - DJ stood down")
                return
            self.foreign_ticks = 0

            if state == "PLAYING":
                self.stopped_ticks = 0
                if self._on_playing(st, cur):
                    return
            elif state == "PAUSED_PLAYBACK":
                self.stopped_ticks = 0
                if not self.paused:
                    self.paused = True
                    self.event("paused from the Sonos app")
            elif state == "STOPPED" and not self.paused:
                self.stopped_ticks += 1
                if self.stopped_ticks >= 2:
                    self.stopped_ticks = 0
                    self._on_stopped(st, cur)
                    return

            # "up next" advances only when the speaker reports a different track (_on_playing).
            # Never trim it by queue position: mid-transition (e.g. right after a skip) Sonos
            # reports the new position with the old track's URI, and trimming then dropped the
            # starting track, which then got picked and queued a second time.
            remaining = st["queue_size"] - st["index"] - 1
            if remaining < self.cfg["queue_ahead"]:
                self._top_up(self.cfg["queue_ahead"] - remaining)

            if self.dirty_at is not None and time.time() - self.dirty_at > KNOB_SETTLE_SECS:
                if self.refresh_upcoming():
                    self.dirty_at = None

            if state == "PLAYING" and not self.fading:
                self._apply_volume()

    def _check_daypart(self):
        """When the real clock moves into a new part of the day: re-pick, and let knob trims expire."""
        if self.mode != "live" and self.inputs.hour_override is not None:
            return
        dp = self.targets["daypart"]
        if self.last_daypart and dp != self.last_daypart:
            msg = f"now {dp}"
            if self.cfg.get("reset_trims_on_daypart", True) and (self.inputs.energy_trim or self.inputs.volume_trim):
                self.inputs.energy_trim, self.inputs.volume_trim = 0.0, 0
                self.save()
                self.targets = self.compute()
                msg += " - knob trims reset"
            self.event(msg)
            self.dirty_at = 0
        self.last_daypart = dp

    def run(self, interval=2.0):
        try:
            self.adopt()
        except Exception:
            log.exception("couldn't adopt existing queue")
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
                base = self._base_url()
                if base != self.base_url and not base.startswith("http://127."):
                    self.event(f"this computer's address changed to {base} - re-queuing")
                    self.base_url = base
                    self.dirty_at = 0
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
                "duration": f.get("duration"),
                "up": v.get("up", 0), "down": v.get("down", 0), "plays": v.get("plays", 0),
                "cached": self.cache.has(t)}

    def _calibration_snapshot(self):
        saved = self.calibration
        out = {"venue": self.live.cfg.get("venue_name") or self.live.cfg.get("venue"),
               "venue_detected_by": self.live.cfg.get("venue_detected_by"),
               "saved_at": saved.get("at"), "summary": calibrate.describe(saved["model"], self.cfg) if saved.get("model") else None,
               "wizard": None}
        w = self.wizard
        if w:
            sc = calibrate.SCENARIOS[w["i"]]
            out["wizard"] = {"step": w["i"] + 1, "of": len(calibrate.SCENARIOS), "scenario": sc,
                             "proposal": bool(w["proposal"]),
                             "summary": calibrate.describe(w["proposal"], self.cfg) if w["proposal"] else None}
        return out

    def snapshot(self):
        with self.lock:
            lib = self.library.all()
            genres = self.library.genres()
            per_genre = {g: {"label": label, "tracks": 0, "cached": 0} for g, label in genres.items()}
            for t in lib:
                pg = per_genre[t.genre]
                pg["tracks"] += 1
                pg["cached"] += self.cache.has(t)
            status = dict(self.status)
            # something the DJ didn't queue is playing (Spotify, radio, another app)
            status["other_source"] = bool(status.get("uri")) and status.get("state") == "PLAYING" \
                and (not self.running or status["uri"] not in self.uri_map)
            if self.now_id and not status.get("duration"):
                status["duration"] = int(self.meta.duration(self.now_id) or 0)
            return {
                "live": self.player.live,
                "running": self.running,
                "paused": self.paused,
                "status": status,
                "now": self.track_info(self.now_id),
                "upcoming": [self.track_info(i) for i in self.upcoming],
                "inputs": self.effective_inputs().to_dict(),
                "mode": self.mode,
                # feed details + where each effective value came from (the values themselves are in "inputs")
                "feeds": {**self.live.status(),
                         **{k: v for k, v in self.live.effective().items() if k.endswith("_src")},
                         "override_minutes": self.cfg.get("live", {}).get("override_minutes", 60)},
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
                "calibration": self._calibration_snapshot(),
                "limits": {"min_volume": self.cfg["min_volume"], "max_volume": self.cfg["max_volume"]},
            }

"""Speaker control. SonosPlayer drives real speakers through SoCo;
MockPlayer simulates them so the UI can be exercised silently.

Both expose the same small interface used by the DJ.
Items passed to start/append are dicts: {uri, title, artist, album, mime}.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)


def _secs(hms):
    try:
        h, m, s = (int(x) for x in hms.split(":"))
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return 0


class SonosPlayer:
    live = True

    def __init__(self, coordinator_ip, crossfade=True):
        import soco  # imported here so --mock works without SoCo installed
        self.soco = soco
        self.anchor_ip = coordinator_ip
        self.crossfade = crossfade
        self.zones = {}          # ip -> SoCo
        self.lock = threading.RLock()
        self.error = None

    # -- discovery & grouping ------------------------------------------------
    def discover(self):
        found = self.soco.discover(timeout=8) or set()
        if not found:
            # Multicast discovery sometimes finds nothing on this Wi-Fi (it did on 10 Oct 2026): scan the network directly.
            try:
                found = self.soco.discovery.scan_network() or set()
                log.warning("speaker discovery found nothing; a network scan found %d", len(found))
            except Exception as e:
                log.warning("speaker discovery found nothing and the network scan failed: %s", e)
        with self.lock:
            for z in found:
                self.zones[z.ip_address] = z
            if self.anchor_ip and self.anchor_ip not in self.zones:
                if self.zones:
                    # The configured main room is offline (unplugged, off the Wi-Fi): lead from a room that's here,
                    # preferring the coordinator of the biggest group, rather than failing to start.
                    try:
                        best = max(self.zones.values(), key=lambda z: len(z.group.members)).group.coordinator
                    except Exception:
                        best = sorted(self.zones.values(), key=lambda z: z.player_name)[0]
                    log.warning("main room %s not found; using %s (%s)", self.anchor_ip, best.player_name, best.ip_address)
                    self.anchor_ip = best.ip_address
                else:  # discovery found nothing (e.g. multicast blocked): try the configured address directly
                    self.zones[self.anchor_ip] = self.soco.SoCo(self.anchor_ip)
            if not self.anchor_ip and self.zones:
                self.anchor_ip = sorted(self.zones.values(), key=lambda z: z.player_name)[0].ip_address
        return len(self.zones)

    def household(self):
        with self.lock:
            z = self.zones.get(self.anchor_ip) or self.soco.SoCo(self.anchor_ip)
            return z.household_id

    @property
    def ctrl(self):
        """The group coordinator for the anchor room; all transport goes here."""
        return self.zones.get(self.anchor_ip, self.soco.SoCo(self.anchor_ip)).group.coordinator

    def speakers(self):
        with self.lock:
            ctrl = self.ctrl
            members = {m.ip_address for m in ctrl.group.members}
            out = []
            for ip, z in sorted(self.zones.items(), key=lambda kv: kv[1].player_name):
                try:
                    muted = bool(z.mute)
                except Exception:
                    muted = False
                out.append({"ip": ip, "name": z.player_name, "in_group": ip in members,
                            "coordinator": ip == ctrl.ip_address, "anchor": ip == self.anchor_ip, "muted": muted})
            return out

    def set_mute(self, ip, on):
        """Per-room mute: the room stays in the group, so unmuting is instant."""
        with self.lock:
            self.zones[ip].mute = bool(on)

    def eq(self, ip):
        """The room's own tone settings (Sonos: bass/treble -10..10, loudness on/off)."""
        with self.lock:
            z = self.zones[ip]
            return {"bass": int(z.bass), "treble": int(z.treble), "loudness": bool(z.loudness)}

    def set_eq(self, ip, bass=None, treble=None, loudness=None):
        with self.lock:
            z = self.zones[ip]
            if bass is not None:
                z.bass = int(bass)
            if treble is not None:
                z.treble = int(treble)
            if loudness is not None:
                z.loudness = bool(loudness)

    def set_anchor(self, ip):
        with self.lock:
            self.anchor_ip = ip

    def _wait(self, ip, cond, secs=8):
        """Poll group state until cond() holds (Sonos applies grouping changes asynchronously)."""
        end = time.monotonic() + secs
        while True:
            self.zones[ip].zone_group_state.clear_cache()
            try:
                if cond():
                    return True
            except Exception:
                pass
            if time.monotonic() > end:
                return False
            time.sleep(0.4)

    def make_main(self, ip, keep_old=True):
        """Hand group leadership (the queue and what's playing) to `ip`, a room in the group; playback carries on.
        keep_old=False takes the old leader out of the group in the same step."""
        with self.lock:
            ctrl = self.ctrl
            if ip == ctrl.ip_address:
                self.anchor_ip = ip
                return
            new = self.zones[ip]
            if not self._wait(ip, lambda: ip in {m.ip_address for m in ctrl.group.members}):
                raise RuntimeError(f"{new.player_name} didn't join the group")
            ctrl.avTransport.DelegateGroupCoordinationTo([
                ("InstanceID", 0), ("NewCoordinator", new.uid), ("RejoinGroup", "1" if keep_old else "0")])
            if not self._wait(ip, lambda: new.group.coordinator.ip_address == ip):
                raise RuntimeError(f"{new.player_name} didn't take over as main room")
            self.anchor_ip = ip
            if not keep_old:
                self._silence_alone(ctrl)

    def _silence_alone(self, zone):
        """Make sure a room that left the group isn't still playing on its own (never touches the group)."""
        try:
            if zone.ip_address in {m.ip_address for m in self.ctrl.group.members}:
                zone.unjoin()
            self._wait(zone.ip_address, lambda: len(zone.group.members) == 1)
            if len(zone.group.members) == 1 and \
                    zone.get_current_transport_info()["current_transport_state"] == "PLAYING":
                zone.stop()
        except Exception as e:
            log.warning("couldn't silence %s: %s", zone.player_name, e)

    def party(self):
        with self.lock:
            self.ctrl.partymode()

    def join(self, ip):
        with self.lock:
            ctrl = self.ctrl
            if ip != ctrl.ip_address:
                self.zones[ip].join(ctrl)

    def leave(self, ip):
        with self.lock:
            ctrl = self.ctrl
            if ip != ctrl.ip_address:
                self.zones[ip].unjoin()

    # -- queue & transport ---------------------------------------------------
    def _didl(self, item):
        from soco.data_structures import DidlMusicTrack, DidlResource
        res = [DidlResource(uri=item["uri"], protocol_info=f"http-get:*:{item['mime']}:*")]
        return DidlMusicTrack(title=item["title"], parent_id="", item_id=item["uri"],
                              resources=res, creator=item.get("artist", ""), album=item.get("album", ""))

    def start(self, items):
        with self.lock:
            c = self.ctrl
            c.clear_queue()
            c.add_multiple_to_queue([self._didl(i) for i in items])
            c.play_from_queue(0)  # switches the speaker to its queue (e.g. away from Spotify)
            # Play mode / crossfade can only be set once the queue is the source,
            # and they're nice-to-haves: never let them stop the music.
            for attr, value in (("play_mode", "NORMAL"), ("cross_fade", self.crossfade)):
                try:
                    setattr(c, attr, value)
                except Exception as e:
                    log.warning("couldn't set %s: %s", attr, e)

    def group_size(self):
        with self.lock:
            return len(self.ctrl.group.members)

    def append(self, items):
        if items:
            with self.lock:
                self.ctrl.add_multiple_to_queue([self._didl(i) for i in items])

    def _position(self, c):
        return int(c.get_current_track_info().get("playlist_position") or 0) - 1

    # Never remove the playing item OR the one right after it: Sonos pre-loads the next item
    # as soon as the current file is buffered (seconds, for files served from this laptop),
    # and removing it stops playback and resets the queue to item 1.
    PROTECTED_AHEAD = 1

    def drop_from(self, index):
        """Remove queue entries from `index` onwards, re-reading the live position first
        so the playing track and the pre-loaded next track are never removed."""
        with self.lock:
            c = self.ctrl
            start = max(index, self._position(c) + 1 + self.PROTECTED_AHEAD)
            for i in range(c.queue_size - 1, start - 1, -1):
                c.remove_from_queue(i)

    def remove_index(self, index):
        with self.lock:
            c = self.ctrl
            if index > self._position(c) + self.PROTECTED_AHEAD:
                c.remove_from_queue(index)
                return True
            return False

    def queue_uris(self):
        with self.lock:
            c, out = self.ctrl, []
            while True:
                batch = c.get_queue(start=len(out), max_items=100)
                out += [(it.resources[0].uri if it.resources else "") for it in batch]
                if len(batch) < 100:
                    return out

    def play_index(self, index):
        with self.lock:
            self.ctrl.play_from_queue(index)

    def seek(self, seconds):
        with self.lock:
            s = int(seconds)
            self.ctrl.seek(f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}")

    def play(self):
        with self.lock:
            self.ctrl.play()

    def pause(self):
        with self.lock:
            self.ctrl.pause()

    def next(self):
        with self.lock:
            self.ctrl.next()

    def status(self):
        with self.lock:
            c = self.ctrl
            info = c.get_current_track_info()
            state = c.get_current_transport_info()["current_transport_state"]
            return {
                "state": state,  # PLAYING | PAUSED_PLAYBACK | STOPPED | TRANSITIONING
                "index": int(info.get("playlist_position") or 0) - 1,
                "uri": info.get("uri", ""),
                "elapsed": _secs(info.get("position")),
                # Sonos reports whole seconds; the true position is somewhere in the next second
                "position": _secs(info.get("position")) + 0.5, "read_at": time.time(),
                "duration": _secs(info.get("duration")),
                "queue_size": c.queue_size,
            }

    def volumes(self):
        with self.lock:
            return {m.ip_address: m.volume for m in self.ctrl.group.members}

    def group_volume(self):
        with self.lock:
            return self.ctrl.group.volume

    def set_group_volume(self, vol):
        """One call for the whole group; Sonos keeps the rooms' relative levels."""
        with self.lock:
            self.ctrl.group.volume = max(0, min(100, int(vol)))

    def set_volume(self, ip, vol):
        with self.lock:
            self.zones[ip].volume = vol


class MockPlayer:
    """Pretend speakers. Each track 'plays' for its real length when `duration_of(uri)` knows it
    (set by run.py, so listening on a device hears whole songs), otherwise for track_seconds."""
    live = False

    def __init__(self, track_seconds=45, duration_of=None):
        self.track_seconds = track_seconds
        self.duration_of = duration_of
        names = ["Mock Cafe Entrance", "Mock Office", "Mock Mezzanine", "Mock 2nd Floor Painting Corner", "Mock 2nd Floor Storage Room"]
        self.rooms = {f"10.0.0.{i + 10}": {"name": n, "volume": 25, "mute": False} for i, n in enumerate(names)}
        self.anchor_ip = "10.0.0.11"
        self.group = {self.anchor_ip}
        self.queue, self.index, self.state = [], -1, "STOPPED"
        self.started_at, self.paused_elapsed = 0.0, 0.0
        self.error = None
        self.lock = threading.RLock()

    def discover(self):
        return len(self.rooms)

    def household(self):
        return "Sonos_MOCK"

    def speakers(self):
        return [{"ip": ip, "name": r["name"], "in_group": ip in self.group,
                 "coordinator": ip == self.anchor_ip, "anchor": ip == self.anchor_ip, "muted": r["mute"]}
                for ip, r in sorted(self.rooms.items(), key=lambda kv: kv[1]["name"])]

    def set_mute(self, ip, on):
        self.rooms[ip]["mute"] = bool(on)

    def eq(self, ip):
        r = self.rooms[ip]
        return {"bass": r.get("bass", 0), "treble": r.get("treble", 0), "loudness": r.get("loudness", True)}

    def set_eq(self, ip, bass=None, treble=None, loudness=None):
        r = self.rooms[ip]
        for k, v in (("bass", bass), ("treble", treble), ("loudness", loudness)):
            if v is not None:
                r[k] = v

    def set_anchor(self, ip):
        self.anchor_ip = ip
        self.group.add(ip)

    def make_main(self, ip, keep_old=True):
        """Like Sonos's group delegation: the queue and playback move to `ip`, which must be in the group."""
        if ip not in self.group:
            raise RuntimeError(f"{self.rooms[ip]['name']} didn't join the group")
        old, self.anchor_ip = self.anchor_ip, ip
        if not keep_old:
            self.group.discard(old)

    def party(self):
        self.group = set(self.rooms)

    def join(self, ip):
        self.group.add(ip)

    def leave(self, ip):
        if ip != self.anchor_ip:
            self.group.discard(ip)

    def _elapsed(self):
        if self.state == "PLAYING":
            return time.monotonic() - self.started_at
        return self.paused_elapsed

    def _length(self):
        uri = self.queue[self.index].get("uri") if 0 <= self.index < len(self.queue) else None
        try:
            known = self.duration_of(uri) if self.duration_of and uri else None
        except Exception:
            known = None
        return known or self.track_seconds

    def _advance(self):
        while self.state == "PLAYING" and self._elapsed() >= (length := self._length()):
            if self.index + 1 < len(self.queue):
                self.index += 1
                self.started_at += length
            else:
                self.state, self.paused_elapsed = "STOPPED", 0

    def start(self, items):
        with self.lock:
            self.queue = list(items)
            self.play_index(0)

    def append(self, items):
        with self.lock:
            self.queue.extend(items)

    def group_size(self):
        return len(self.group)

    PROTECTED_AHEAD = SonosPlayer.PROTECTED_AHEAD

    def drop_from(self, index):
        with self.lock:
            del self.queue[max(index, self.index + 1 + self.PROTECTED_AHEAD):]

    def remove_index(self, index):
        """Like Sonos: removing the playing or pre-loaded next item stops and resets to item 1."""
        with self.lock:
            del self.queue[index]
            if index in (self.index, self.index + 1):
                self.index, self.state, self.paused_elapsed = 0, "STOPPED", 0
            elif index < self.index:
                self.index -= 1
            return index > self.index + 1

    def queue_uris(self):
        return [q["uri"] for q in self.queue]

    def stop(self):
        with self.lock:
            self.state, self.paused_elapsed = "STOPPED", 0

    def play_index(self, index):
        with self.lock:
            self.index, self.state, self.started_at = index, "PLAYING", time.monotonic()

    def seek(self, seconds):
        with self.lock:
            if self.state == "PLAYING":
                self.started_at = time.monotonic() - seconds

    def play(self):
        with self.lock:
            if self.state != "PLAYING" and self.queue:
                self.started_at = time.monotonic() - self.paused_elapsed
                self.state = "PLAYING"

    def pause(self):
        with self.lock:
            if self.state == "PLAYING":
                self.paused_elapsed = self._elapsed()
                self.state = "PAUSED_PLAYBACK"

    def next(self):
        with self.lock:
            if self.index + 1 < len(self.queue):
                self.play_index(self.index + 1)

    def status(self):
        with self.lock:
            self._advance()
            cur = self.queue[self.index] if 0 <= self.index < len(self.queue) else {}
            # duration 0, like a real Sonos reports for these http streams
            return {"state": self.state, "index": self.index, "uri": cur.get("uri", ""),
                    "elapsed": int(self._elapsed()), "position": round(self._elapsed(), 3),
                    "read_at": time.time(), "duration": 0,
                    "queue_size": len(self.queue)}

    def volumes(self):
        return {ip: self.rooms[ip]["volume"] for ip in self.group}

    def group_volume(self):
        v = self.volumes()
        return round(sum(v.values()) / len(v)) if v else 0

    def set_group_volume(self, vol):
        cur = self.group_volume()
        for ip in self.group:
            self.rooms[ip]["volume"] = max(0, min(100, self.rooms[ip]["volume"] + int(vol) - cur))

    def set_volume(self, ip, vol):
        self.rooms[ip]["volume"] = vol

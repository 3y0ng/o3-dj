"""Track catalogue, merged from pluggable sources.

Sources:
  ChillifySource  - chillify.me catalogue (snapshot kept in data/ so it survives outages)
  FolderSource    - audio files dropped into library/<genre>/
  UrlListSource   - library/custom_tracks.json, a list of direct stream URLs

To add another service, write a class with `name` and `load() -> list[Track]`
and append it in Library.__init__.
"""

import hashlib
import json
import logging
import threading
import urllib.parse
from dataclasses import asdict, dataclass

import requests

from .config import DATA, LIBRARY

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg"}
UA = {"User-Agent": "o3-dj/0.1"}


@dataclass
class Track:
    id: str
    title: str
    genre: str            # our genre key, e.g. "jazzy_cafe"
    source: str           # "chillify" | "folder" | "suno" | "url"
    url: str | None = None    # remote stream URL
    path: str | None = None   # path relative to library/ for local files
    artist: str = ""
    subgenre: str = ""
    bpm: float | None = None      # known tempo (a Suno sidecar), better than the ffmpeg estimate
    vocals: bool | None = None    # None = unknown
    tags: list | None = None

    def to_dict(self):
        return asdict(self)


class ChillifySource:
    name = "chillify"

    def __init__(self, cfg):
        self.cfg = cfg["chillify"]
        self.snapshot = DATA / "chillify_catalog.json"
        self.map = {}
        for key, g in cfg["genres"].items():
            for sub in g.get("sources", {}).get("chillify", []):
                self.map[sub] = key
        self.online = None

    def _fetch(self):
        r = requests.get(self.cfg["catalog_url"], headers=UA, timeout=20)
        r.raise_for_status()
        raw = r.json()["tracks"]
        self.snapshot.write_text(json.dumps(raw))
        return raw

    def load(self):
        if not self.cfg.get("enabled", True):
            return []
        try:
            raw = self._fetch()
            self.online = True
        except Exception as e:
            self.online = False
            log.warning("Chillify catalogue unavailable (%s); using snapshot", e)
            try:
                raw = json.loads(self.snapshot.read_text())
            except FileNotFoundError:
                return []
        tracks = []
        for t in raw:
            key = self.map.get(t.get("genre"))
            if not key:
                continue
            name = t["trackName"]
            tracks.append(Track(
                id=f"chillify:{name}",
                title=name.split("/", 1)[-1].rsplit(".", 1)[0],
                genre=key,
                source=self.name,
                url=self.cfg["track_base"] + urllib.parse.quote(name, safe="/()"),
                artist="Chillify",
                subgenre=t["genre"],
            ))
        return tracks

    def ping(self):
        """True if chillify.me is reachable right now."""
        if not self.cfg.get("enabled", True):
            return False
        try:
            r = requests.head(self.cfg["catalog_url"], headers=UA, timeout=6)
            self.online = r.status_code < 500
        except requests.RequestException:
            self.online = False
        return self.online


def read_sidecar(audio):
    """Metadata next to an audio file (<stem>.json, written by tools/suno_generate.py), or {}."""
    try:
        d = json.loads(audio.with_suffix(".json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


class FolderSource:
    name = "folder"

    def __init__(self, root=None):
        self.root = root or LIBRARY

    def load(self):
        tracks = []
        for genre_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            for f in sorted(genre_dir.rglob("*")):
                if f.suffix.lower() not in AUDIO_EXTS or f.name.startswith("."):
                    continue
                rel = f.relative_to(self.root).as_posix()
                meta = read_sidecar(f)
                if meta.get("commercial") is False:  # e.g. made on a free Suno plan: not licensed for the cafe
                    continue
                suno = meta.get("source") == "suno"
                vocals = meta.get("vocals")
                tracks.append(Track(
                    id=f"folder:{rel}", title=meta.get("title") or f.stem, genre=genre_dir.name,
                    source="suno" if suno else self.name, path=rel,
                    artist="Suno" if suno else meta.get("artist", "Local"),
                    bpm=meta.get("bpm"), vocals=None if vocals is None else bool(vocals),
                    tags=meta.get("tags"),
                ))
        return tracks


class UrlListSource:
    name = "url"
    file = LIBRARY / "custom_tracks.json"

    def _read(self):
        try:
            return json.loads(self.file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def load(self):
        return [
            Track(
                id="url:" + hashlib.sha1(t["url"].encode()).hexdigest()[:12],
                title=t.get("title") or t["url"].rsplit("/", 1)[-1],
                genre=t["genre"], source=self.name, url=t["url"],
                artist=t.get("artist", ""),
            )
            for t in self._read() if t.get("url") and t.get("genre")
        ]

    def add(self, url, title, genre, artist=""):
        items = self._read()
        if not any(t["url"] == url for t in items):
            items.append({"url": url, "title": title, "genre": genre, "artist": artist})
            self.file.write_text(json.dumps(items, indent=2, ensure_ascii=False))


class Library:
    def __init__(self, cfg):
        self.cfg = cfg
        self.chillify = ChillifySource(cfg)
        self.urls = UrlListSource()
        self.sources = [self.chillify, FolderSource(), self.urls]
        self.tracks: dict[str, Track] = {}
        self.lock = threading.RLock()

    def refresh(self):
        merged = {}
        for src in self.sources:
            try:
                for t in src.load():
                    merged[t.id] = t
            except Exception:
                log.exception("source %s failed", src.name)
        with self.lock:
            self.tracks = merged
        log.info("library: %d tracks", len(merged))

    def genres(self):
        """Configured genres, plus any extra folder names found in library/."""
        out = {k: g["label"] for k, g in self.cfg["genres"].items()}
        with self.lock:
            for t in self.tracks.values():
                out.setdefault(t.genre, t.genre.replace("_", " ").title())
        return out

    def get(self, track_id):
        with self.lock:
            return self.tracks.get(track_id)

    def in_genre(self, genre):
        with self.lock:
            return [t for t in self.tracks.values() if t.genre == genre]

    def all(self):
        with self.lock:
            return list(self.tracks.values())

    def counts(self):
        """{genre: number of tracks}."""
        out = {}
        with self.lock:
            for t in self.tracks.values():
                out[t.genre] = out.get(t.genre, 0) + 1
        return out

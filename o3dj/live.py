"""Live data feeds for LIVE mode (see docs/live-mode-plan.md).

  WeatherFeed    - Open-Meteo current conditions for the venue, mapped to clear / cloudy / rain
  OccupancyFeed  - read-only Supabase: count rows in an existing table, or call an existing function

Feeds never raise into the DJ: each keeps its last good value, when it was fetched, and the last error.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)
UA = {"User-Agent": "o3-dj/0.1"}

# WMO weather codes (Open-Meteo): https://open-meteo.com/en/docs
RAIN_CODES = set(range(51, 68)) | set(range(71, 78)) | set(range(80, 87)) | set(range(95, 100))  # drizzle, rain, snow, showers, storms
CLOUD_CODES = {2, 3, 45, 48}  # partly cloudy, overcast, fog


def weather_from(code, precipitation=0.0, cloud_cover=0):
    if code in RAIN_CODES or (precipitation or 0) >= 0.2:
        return "rain"
    if code in CLOUD_CODES or (cloud_cover or 0) >= 70:
        return "cloudy"
    return "clear"


def count_from_content_range(header):
    """PostgREST 'Content-Range: 0-24/57' or '*/57' -> 57."""
    try:
        total = header.rsplit("/", 1)[1]
        return None if total == "*" else int(total)
    except (AttributeError, IndexError, ValueError):
        return None


class Feed:
    name = "feed"

    def __init__(self, poll_seconds):
        self.poll_seconds = poll_seconds
        self.value = None       # last good value
        self.detail = {}        # extra info for the screen (temp, count...)
        self.at = 0.0           # when value was fetched
        self.error = None
        self.next_poll = 0.0

    @property
    def configured(self):
        return True

    def fetch(self):
        raise NotImplementedError

    def poll(self, force=False):
        if not self.configured or (not force and time.time() < self.next_poll):
            return False
        self.next_poll = time.time() + self.poll_seconds
        try:
            value, detail = self.fetch()
            changed = value != self.value
            self.value, self.detail, self.at, self.error = value, detail, time.time(), None
            return changed
        except Exception as e:
            self.error = str(e)[:140]
            log.info("%s feed failed: %s", self.name, e)
            return False

    def age(self):
        return None if not self.at else time.time() - self.at

    def status(self):
        return {"configured": self.configured, "value": self.value, "detail": self.detail,
                "age_s": None if not self.at else int(self.age()), "error": self.error}


class WeatherFeed(Feed):
    name = "weather"

    def __init__(self, cfg):
        super().__init__(cfg.get("poll_seconds", 600))
        self.lat, self.lon = cfg.get("lat"), cfg.get("lon")

    @property
    def configured(self):
        return self.lat is not None and self.lon is not None

    def fetch(self):
        r = requests.get("https://api.open-meteo.com/v1/forecast", headers=UA, timeout=10, params={
            "latitude": self.lat, "longitude": self.lon,
            "current": "weather_code,precipitation,cloud_cover,temperature_2m"})
        r.raise_for_status()
        cur = r.json()["current"]
        value = weather_from(cur.get("weather_code"), cur.get("precipitation"), cur.get("cloud_cover"))
        return value, {"temp_c": cur.get("temperature_2m"), "code": cur.get("weather_code")}


class OccupancyFeed(Feed):
    """Read-only. mode 'count': HEAD a table with filters and read the exact count.
    mode 'rpc': call an existing function that returns a number (or {result_key: n})."""
    name = "occupancy"

    def __init__(self, cfg, capacity):
        super().__init__(cfg.get("poll_seconds", 60))
        self.cfg, self.capacity = cfg, capacity
        self.url = (cfg.get("supabase_url") or "").rstrip("/")

    @property
    def key(self):
        return os.environ.get(self.cfg.get("key_env", "O3_SUPABASE_KEY")) or self.cfg.get("key")

    @property
    def configured(self):
        mode = self.cfg.get("mode")
        ready = (mode == "count" and self.cfg.get("table")) or (mode == "rpc" and self.cfg.get("rpc"))
        return bool(self.url and self.key and self.capacity and ready)

    def _headers(self):
        return {**UA, "apikey": self.key, "Authorization": f"Bearer {self.key}"}

    def count_params(self, now=None):
        c = self.cfg
        now = now or datetime.now(timezone.utc)
        params = {"select": c.get("select", "*")}
        if c.get("site_column") and c.get("site_value") is not None:
            params[c["site_column"]] = f"eq.{c['site_value']}"
        if c.get("start_column"):
            since = now - timedelta(hours=c.get("max_visit_hours", 8))
            params[c["start_column"]] = f"gte.{since.isoformat()}"
        if c.get("end_column"):
            params[c["end_column"]] = "is.null"
        for k, v in (c.get("extra_filters") or {}).items():
            params[k] = v
        return params

    def fetch(self):
        c = self.cfg
        if c["mode"] == "count":
            r = requests.head(f"{self.url}/rest/v1/{c['table']}", params=self.count_params(), timeout=10,
                              headers={**self._headers(), "Prefer": "count=exact", "Range": "0-0"})
            if r.status_code >= 400:
                raise RuntimeError(f"Supabase {r.status_code}")
            count = count_from_content_range(r.headers.get("Content-Range"))
            if count is None:
                raise RuntimeError("no count in response")
        else:
            r = requests.post(f"{self.url}/rest/v1/rpc/{c['rpc']}", json=c.get("rpc_args") or {},
                              headers=self._headers(), timeout=10)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                data = data[0] if data else 0
            count = data.get(c.get("result_key", "count")) if isinstance(data, dict) else data
            count = int(count)
        pct = max(0, min(100, round(100 * count / self.capacity)))
        return pct, {"count": count, "capacity": self.capacity}


class Live:
    """Holds the feeds plus staff overrides, and works out the effective live inputs."""

    def __init__(self, cfg):
        self.cfg = cfg.get("live", {})
        self.weather = WeatherFeed(self.cfg.get("weather", {}))
        self.occupancy = OccupancyFeed(self.cfg.get("occupancy", {}), self.cfg.get("capacity"))
        self.overrides = {}  # key -> (value, expires_at)

    @property
    def stale_after(self):
        return self.cfg.get("stale_after_minutes", 15) * 60

    def fresh(self, feed):
        age = feed.age()
        return feed.value is not None and age is not None and age <= self.stale_after

    def override(self, key, value):
        self.overrides[key] = (value, time.time() + self.cfg.get("override_minutes", 60) * 60)

    def clear_override(self, key):
        self.overrides.pop(key, None)

    def _override(self, key):
        v = self.overrides.get(key)
        if v and time.time() < v[1]:
            return v[0]
        self.overrides.pop(key, None)
        return None

    def effective(self):
        """{'weather': str, 'occupancy': int|None, ...sources} for the brain and the screen."""
        out = {}
        w = self._override("weather")
        if w is not None:
            out["weather"], out["weather_src"] = w, "override"
        elif self.fresh(self.weather):
            out["weather"], out["weather_src"] = self.weather.value, "live"
        else:
            out["weather"], out["weather_src"] = "clear", "stale" if self.weather.configured else "off"
        o = self._override("occupancy")
        if o is not None:
            out["occupancy"], out["occupancy_src"] = o, "override"
        elif self.fresh(self.occupancy):
            out["occupancy"], out["occupancy_src"] = self.occupancy.value, "live"
        else:
            out["occupancy"], out["occupancy_src"] = None, "stale" if self.occupancy.configured else "off"
        return out

    def poll(self, force=False):
        changed = False
        for feed in (self.weather, self.occupancy):
            changed |= feed.poll(force)
        return changed

    def status(self):
        now = time.time()
        return {
            "weather": self.weather.status(),
            "occupancy": self.occupancy.status(),
            "overrides": {k: int((exp - now) / 60) + 1 for k, (v, exp) in self.overrides.items() if exp > now},
            "timezone": self.cfg.get("timezone"),
        }

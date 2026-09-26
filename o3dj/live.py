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
        self.last_attempt = 0.0

    @property
    def configured(self):
        return True

    def fetch(self):
        raise NotImplementedError

    MIN_GAP = 60  # even a forced poll (e.g. flipping to live) waits this long since the last request

    def poll(self, force=False):
        now = time.time()
        if not self.configured or now < self.next_poll and (not force or now - self.last_attempt < self.MIN_GAP):
            return False
        self.last_attempt = now
        self.next_poll = now + self.poll_seconds  # failures also wait for the next scheduled poll: no retry loops
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
                "age_s": None if not self.at else int(self.age()), "error": self.error,
                "missing": getattr(self, "missing", [])}


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


class SupabaseSession:
    """Signs in as an O3 app user (email + password grant) and keeps the access token fresh.
    This is the same sign-in the O3 app does; it doesn't change anything in the database."""

    def __init__(self, url, anon_key, email, password):
        self.url, self.anon_key, self.email, self.password = url, anon_key, email, password
        self.access_token, self.refresh_token, self.expires_at = None, None, 0.0

    def _grant(self, grant_type, body):
        r = requests.post(f"{self.url}/auth/v1/token", params={"grant_type": grant_type}, json=body, timeout=10,
                          headers={**UA, "apikey": self.anon_key})
        if r.status_code >= 400:
            raise RuntimeError(f"sign-in failed ({r.status_code})")
        data = r.json()
        self.access_token, self.refresh_token = data["access_token"], data.get("refresh_token")
        self.expires_at = time.time() + int(data.get("expires_in", 3600))

    def token(self, force=False):
        if force or not self.access_token or time.time() > self.expires_at - 120:
            if self.refresh_token and not force:
                try:
                    self._grant("refresh_token", {"refresh_token": self.refresh_token})
                    return self.access_token
                except RuntimeError:
                    pass
            self._grant("password", {"email": self.email, "password": self.password})
        return self.access_token


class OccupancyFeed(Feed):
    """Read-only occupancy from Supabase.

    mode 'o3'    : O3's existing get_location_occupancy_counts() for `location_code`, called as a signed-in
                   app user. Capacity is the venue's `capacity` in config.json (O3's red threshold, copied from
                   physical_location), so each poll is a single request. No database changes.
    mode 'count' : HEAD a table with filters and read the exact count.
    mode 'rpc'   : call another existing function that returns a number (or {result_key: n}).
    """
    name = "occupancy"

    def __init__(self, cfg, capacity):
        super().__init__(cfg.get("poll_seconds", 300))  # every 5 min: ~13 Supabase requests an hour in total
        self.cfg, self.capacity = cfg, capacity
        self.url = (cfg.get("supabase_url") or "").rstrip("/")
        self.session = None

    def _secret(self, name, env_default):
        return os.environ.get(self.cfg.get(f"{name}_env", env_default)) or self.cfg.get(name)

    @property
    def key(self):
        return self._secret("key", "O3_SUPABASE_KEY")

    @property
    def missing(self):
        """What still needs setting up (shown on the controller)."""
        c, need = self.cfg, []
        if not self.url:
            need.append("supabase_url")
        if c.get("mode") == "o3":
            if not c.get("location_code"):
                need.append("location_code")
            if not self.capacity:
                need.append("capacity")
            if not self.key:
                need.append("O3_SUPABASE_KEY (the app's public anon key)")
            if not self._secret("email", "O3_DJ_EMAIL") or not self._secret("password", "O3_DJ_PASSWORD"):
                need.append("O3_DJ_EMAIL / O3_DJ_PASSWORD (an O3 app account)")
        else:
            if not self.key:
                need.append("O3_SUPABASE_KEY")
            if not self.capacity:
                need.append("capacity")
            if not ((c.get("mode") == "count" and c.get("table")) or (c.get("mode") == "rpc" and c.get("rpc"))):
                need.append("table or rpc")
        return need

    @property
    def configured(self):
        return not self.missing

    def _bearer(self, force=False):
        if self.cfg.get("mode") == "o3" or self._secret("email", "O3_DJ_EMAIL"):
            if self.session is None:
                self.session = SupabaseSession(self.url, self.key, self._secret("email", "O3_DJ_EMAIL"),
                                               self._secret("password", "O3_DJ_PASSWORD"))
            return self.session.token(force)
        return self.key

    def _headers(self, force=False):
        return {**UA, "apikey": self.key, "Authorization": f"Bearer {self._bearer(force)}"}

    def _get(self, path, **kw):
        r = requests.get(f"{self.url}{path}", headers=self._headers(), timeout=10, **kw)
        if r.status_code == 401:
            r = requests.get(f"{self.url}{path}", headers=self._headers(force=True), timeout=10, **kw)
        r.raise_for_status()
        return r.json()

    def _rpc(self, fn, args):
        # Only existing read-only functions are called this way (see docs/live-mode-plan.md).
        r = requests.post(f"{self.url}/rest/v1/rpc/{fn}", json=args, headers=self._headers(), timeout=10)
        if r.status_code == 401:
            r = requests.post(f"{self.url}/rest/v1/rpc/{fn}", json=args, headers=self._headers(force=True), timeout=10)
        r.raise_for_status()
        return r.json()


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
        if c["mode"] == "o3":
            code = c["location_code"]
            rows = self._rpc("get_location_occupancy_counts", {"p_location_codes": [code]})
            count = next((int(r["occupancy"]) for r in rows or [] if r.get("location_code") == code), 0)
            pct = max(0, min(100, round(100 * count / self.capacity)))
            return pct, {"count": count, "capacity": self.capacity}
        if c["mode"] == "count":
            r = requests.head(f"{self.url}/rest/v1/{c['table']}", params=self.count_params(), timeout=10,
                              headers={**self._headers(), "Prefer": "count=exact", "Range": "0-0"})
            if r.status_code >= 400:
                raise RuntimeError(f"Supabase {r.status_code}")
            count = count_from_content_range(r.headers.get("Content-Range"))
            if count is None:
                raise RuntimeError("no count in response")
        else:
            data = self._rpc(c["rpc"], c.get("rpc_args") or {})
            if isinstance(data, list):
                data = data[0] if data else 0
            count = data.get(c.get("result_key", "count")) if isinstance(data, dict) else data
            count = int(count)
        pct = max(0, min(100, round(100 * count / self.capacity)))
        return pct, {"count": count, "capacity": self.capacity}


def resolve_live_config(cfg):
    """live.venue (e.g. "sydney_01") fills in timezone, weather location and the Supabase venue code
    from cfg["venues"]; anything set explicitly in live.* wins."""
    live = {**cfg.get("live", {})}
    live["weather"] = {**live.get("weather", {})}
    live["occupancy"] = {**live.get("occupancy", {})}
    venue = (cfg.get("venues") or {}).get(live.get("venue") or "")
    if venue:
        live["timezone"] = live.get("timezone") or venue.get("timezone")
        if live["weather"].get("lat") is None:
            live["weather"]["lat"], live["weather"]["lon"] = venue.get("lat"), venue.get("lon")
        live["occupancy"]["location_code"] = live["occupancy"].get("location_code") or live["venue"]
        live["capacity"] = live.get("capacity") or venue.get("capacity")
        live["venue_name"] = venue.get("name")
    return live


class Live:
    """Holds the feeds plus staff overrides, and works out the effective live inputs."""

    def __init__(self, cfg):
        self.cfg = resolve_live_config(cfg)
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
            "venue": self.cfg.get("venue"),
            "venue_name": self.cfg.get("venue_name"),
        }

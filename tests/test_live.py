"""Live mode: feeds, overrides, staleness, and how the DJ follows them. No network."""

import time
from datetime import datetime, timezone

from o3dj import live as live_mod
from o3dj.live import Live, OccupancyFeed, count_from_content_range, weather_from
from tests.test_dj_mock import CFG, make_dj, settle, started, at_elapsed


def test_weather_codes_map_to_moods():
    assert weather_from(0) == "clear"
    assert weather_from(3) == "cloudy" and weather_from(45) == "cloudy"
    assert weather_from(61) == "rain" and weather_from(95) == "rain" and weather_from(73) == "rain"
    assert weather_from(1, precipitation=0.5) == "rain"
    assert weather_from(1, cloud_cover=85) == "cloudy"


def test_postgrest_count_parsing():
    assert count_from_content_range("0-0/57") == 57
    assert count_from_content_range("*/0") == 0
    assert count_from_content_range("0-24/*") is None
    assert count_from_content_range(None) is None


def test_occupancy_count_query_is_read_only_and_filtered(monkeypatch):
    cfg = {"supabase_url": "https://x.supabase.co", "key": "k", "mode": "count", "table": "visits",
           "site_column": "site_id", "site_value": 3, "start_column": "checked_in_at",
           "end_column": "checked_out_at", "max_visit_hours": 6, "extra_filters": {"status": "eq.active"}}
    feed = OccupancyFeed(cfg, capacity=80)
    calls = []

    class Resp:
        status_code = 206
        headers = {"Content-Range": "0-0/58"}

    def fake_head(url, **kw):
        calls.append((url, kw))
        return Resp()

    def forbidden(*a, **k):
        raise AssertionError("occupancy must only read")

    monkeypatch.setattr(live_mod.requests, "head", fake_head)
    for verb in ("post", "patch", "put", "delete"):
        monkeypatch.setattr(live_mod.requests, verb, forbidden)
    assert feed.poll(force=True)
    assert feed.value == 72 and feed.detail == {"count": 58, "capacity": 80}  # 58/80
    url, kw = calls[0]
    assert url == "https://x.supabase.co/rest/v1/visits"
    p = kw["params"]
    assert p["site_id"] == "eq.3" and p["checked_out_at"] == "is.null" and p["status"] == "eq.active"
    assert p["checked_in_at"].startswith("gte.")
    assert kw["headers"]["Prefer"] == "count=exact"


def test_occupancy_not_configured_without_key(monkeypatch):
    monkeypatch.delenv("O3_SUPABASE_KEY", raising=False)
    feed = OccupancyFeed({"supabase_url": "https://x", "mode": "count", "table": "visits"}, capacity=80)
    assert not feed.configured and not feed.poll(force=True)


def _live(**cfg):
    return Live({"live": {"stale_after_minutes": 15, "override_minutes": 60, **cfg}})


def test_layering_override_beats_live_beats_neutral():
    lv = _live()
    lv.weather.lat = lv.weather.lon = 0  # configured
    assert lv.effective()["weather"] == "clear" and lv.effective()["weather_src"] == "stale"
    lv.weather.value, lv.weather.at = "rain", time.time()
    assert lv.effective()["weather"] == "rain" and lv.effective()["weather_src"] == "live"
    lv.override("weather", "cloudy")
    assert lv.effective()["weather"] == "cloudy" and lv.effective()["weather_src"] == "override"
    lv.overrides["weather"] = ("cloudy", time.time() - 1)  # expired
    assert lv.effective()["weather"] == "rain"


def test_stale_feed_falls_back_to_neutral():
    lv = _live()
    lv.occupancy.value, lv.occupancy.at = 90, time.time() - 16 * 60
    e = lv.effective()
    assert e["occupancy"] is None and e["weather"] == "clear"


# -- the DJ in live mode ----------------------------------------------------------

def test_demo_mode_ignores_feeds(tmp_path):
    dj = make_dj(tmp_path)
    dj.live.weather.value, dj.live.weather.at = "rain", time.time()
    assert dj.effective_inputs().weather == dj.inputs.weather


def test_live_mode_uses_feeds_and_real_clock(tmp_path):
    dj = make_dj(tmp_path)
    dj.set_inputs({"hour_override": 3, "weather": "clear"})
    dj.live.weather.value, dj.live.weather.at = "rain", time.time()
    dj.live.occupancy.value, dj.live.occupancy.at = 95, time.time()
    dj.set_mode("live")
    i = dj.effective_inputs()
    assert i.weather == "rain" and i.occupancy_enabled and i.occupancy == 95 and i.hour_override is None
    assert dj.targets["clock"]
    dj.set_mode("demo")
    assert dj.effective_inputs().hour_override == 3  # demo settings untouched


def test_staff_override_in_live_mode(tmp_path):
    dj = make_dj(tmp_path)
    dj.live.weather.value, dj.live.weather.at = "clear", time.time()
    dj.set_mode("live")
    dj.set_inputs({"weather": "rain"})
    assert dj.effective_inputs().weather == "rain"
    assert dj.inputs.weather == "clear"  # the demo setting is untouched; only an override was added
    assert "weather" in dj.live.status()["overrides"]
    dj.clear_override("weather")
    assert dj.effective_inputs().weather == "clear"


def test_live_weather_change_repicks_without_touching_next(tmp_path, monkeypatch):
    dj = make_dj(tmp_path)
    started(dj)
    at_elapsed(dj, 30)
    dj.live.weather.value, dj.live.weather.at = "clear", time.time()
    dj.set_mode("live")
    dj.tick(1)
    nxt = dj.upcoming[0]
    dj.live.weather.value = "rain"  # feed update
    settle(dj, monkeypatch)
    dj.tick(2)
    assert dj.upcoming[0] == nxt and dj.status["state"] == "PLAYING"
    assert any("weather now rain" in e["msg"] for e in dj.events)


def test_venue_timezone(tmp_path):
    dj = make_dj(tmp_path)
    dj.live.cfg["timezone"] = "Pacific/Auckland"
    nz = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Pacific/Auckland"))
    assert abs(dj.hour_now() - (nz.hour + nz.minute / 60)) < 0.05


# -- O3 mode: existing get_location_occupancy_counts() as a signed-in app user ------------

class FakeSupabase:
    """Records every request; only allows sign-in, the occupancy function and reading venues."""
    ALLOWED = {("POST", "/auth/v1/token"), ("POST", "/rest/v1/rpc/get_location_occupancy_counts")}

    def __init__(self, occupancy=58, red=160, expire_first_token=False):
        self.calls, self.occupancy, self.red = [], occupancy, red
        self.tokens_issued, self.expire_first_token = 0, expire_first_token

    def _resp(self, status, data):
        class R:
            status_code = status
            def json(self): return data
            def raise_for_status(self):
                if status >= 400: raise RuntimeError(status)
        return R()

    def handle(self, method, url, **kw):
        path = url.split(".co", 1)[1]
        assert (method, path) in self.ALLOWED, f"unexpected {method} {path}"
        self.calls.append((method, path, kw))
        if path == "/auth/v1/token":
            self.tokens_issued += 1
            return self._resp(200, {"access_token": f"t{self.tokens_issued}", "refresh_token": "r", "expires_in": 3600})
        auth = kw["headers"]["Authorization"]
        if self.expire_first_token and auth == "Bearer t1":
            return self._resp(401, {})
        if path == "/rest/v1/physical_location":
            return self._resp(200, [{"location_name": "Newtown", "green_capacity_threshold": 120,
                                     "red_capacity_threshold": self.red}])
        return self._resp(200, [{"location_code": "sydney_01", "occupancy": self.occupancy}])


O3_CFG = {"mode": "o3", "supabase_url": "https://proj.supabase.co", "location_code": "sydney_01",
          "key": "anon", "email": "dj@o3.test", "password": "pw"}


def _o3_feed(monkeypatch, fake, capacity=160, cfg=O3_CFG):
    monkeypatch.setattr(live_mod.requests, "post", lambda url, **kw: fake.handle("POST", url, **kw))
    monkeypatch.setattr(live_mod.requests, "get", lambda url, **kw: fake.handle("GET", url, **kw))
    for verb in ("patch", "put", "delete"):
        monkeypatch.setattr(live_mod.requests, verb, lambda *a, **k: (_ for _ in ()).throw(AssertionError("write")))
    return OccupancyFeed(cfg, capacity)


def test_o3_mode_signs_in_and_uses_existing_function(monkeypatch):
    fake = FakeSupabase(occupancy=58, red=160)
    feed = _o3_feed(monkeypatch, fake)
    assert feed.configured and feed.poll(force=True), feed.error
    assert feed.value == 36 and feed.detail == {"count": 58, "capacity": 160}  # 58/160
    assert len(fake.calls) == 2  # sign-in + one function call
    rpc = next(c for c in fake.calls if "rpc" in c[1])
    assert rpc[2]["json"] == {"p_location_codes": ["sydney_01"]}
    assert rpc[2]["headers"]["Authorization"] == "Bearer t1" and rpc[2]["headers"]["apikey"] == "anon"
    signin = fake.calls[0]
    assert signin[2]["params"] == {"grant_type": "password"} and signin[2]["json"]["email"] == "dj@o3.test"


def test_o3_mode_uses_configured_capacity(monkeypatch):
    fake = FakeSupabase(occupancy=40)
    feed = _o3_feed(monkeypatch, fake, capacity=80)
    feed.poll(force=True)
    assert feed.value == 50
    assert not _o3_feed(monkeypatch, fake, capacity=None).configured  # no capacity -> not configured


def test_o3_mode_recovers_from_expired_session(monkeypatch):
    fake = FakeSupabase(expire_first_token=True)
    feed = _o3_feed(monkeypatch, fake)
    assert feed.poll(force=True), feed.error
    assert fake.tokens_issued == 2 and feed.value is not None


def test_o3_mode_lists_what_is_missing(monkeypatch):
    for k in ("O3_SUPABASE_KEY", "O3_DJ_EMAIL", "O3_DJ_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    feed = OccupancyFeed({"mode": "o3", "supabase_url": "https://proj.supabase.co"}, 160)
    missing = " ".join(feed.missing)
    assert not feed.configured
    assert "location_code" in missing and "O3_SUPABASE_KEY" in missing and "O3_DJ_EMAIL" in missing
    monkeypatch.setenv("O3_SUPABASE_KEY", "anon"); monkeypatch.setenv("O3_DJ_EMAIL", "a"); monkeypatch.setenv("O3_DJ_PASSWORD", "b")
    feed.cfg["location_code"] = "sydney_01"
    assert feed.configured


def test_o3_sign_in_failure_is_reported_not_raised(monkeypatch):
    class Bad(FakeSupabase):
        def handle(self, method, url, **kw):
            if "/auth/" in url:
                return self._resp(400, {})
            return super().handle(method, url, **kw)
    feed = _o3_feed(monkeypatch, Bad())
    assert not feed.poll(force=True)
    assert feed.value is None and "sign-in failed" in feed.error


def test_venue_preset_fills_timezone_weather_and_code():
    from o3dj.live import resolve_live_config
    cfg = {**CFG, "live": {**CFG["live"], "venue": "melbourne_02"}}
    live = resolve_live_config(cfg)
    assert live["timezone"] == "Australia/Melbourne" and live["venue_name"] == "Brunswick"
    assert live["weather"]["lat"] == CFG["venues"]["melbourne_02"]["lat"]
    assert live["occupancy"]["location_code"] == "melbourne_02" and live["capacity"] == 120
    # explicit settings win
    cfg["live"] = {**cfg["live"], "timezone": "UTC", "occupancy": {**CFG["live"]["occupancy"], "location_code": "x"}}
    live = resolve_live_config(cfg)
    assert live["timezone"] == "UTC" and live["occupancy"]["location_code"] == "x"
    assert "venue" not in CFG["live"] or CFG["live"]["venue"] is None  # config.json itself untouched


def test_supabase_request_budget(monkeypatch):
    """Occupancy polls every 5 min; flipping modes can't hammer Supabase; failures don't retry in a loop."""
    fake = FakeSupabase()
    feed = _o3_feed(monkeypatch, fake)
    assert feed.poll_seconds == 300
    feed.poll(force=True)
    n = len(fake.calls)                      # sign-in + capacity + rpc
    for _ in range(20):                      # someone flicking the switch
        feed.poll(force=True); feed.poll()
    assert len(fake.calls) == n
    clock = [time.time()]
    monkeypatch.setattr(live_mod.time, "time", lambda: clock[0])
    clock[0] += 301
    feed.poll()
    assert len(fake.calls) == n + 1          # one rpc per poll, session still valid
    clock[0] += 60
    feed.poll(); assert len(fake.calls) == n + 1


def test_failed_poll_waits_for_next_slot(monkeypatch):
    calls = []
    def boom(*a, **k):
        calls.append(1); raise RuntimeError("down")
    monkeypatch.setattr(live_mod.requests, "post", boom)
    feed = OccupancyFeed(O3_CFG, 160)
    feed.poll(force=True)
    for _ in range(10):
        feed.poll(); feed.poll(force=True)
    assert len(calls) == 1 and feed.error

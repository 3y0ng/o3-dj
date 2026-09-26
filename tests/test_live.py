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
    dj.cfg = {**CFG, "live": {"timezone": "Pacific/Auckland"}}
    nz = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Pacific/Auckland"))
    assert abs(dj.hour_now() - (nz.hour + nz.minute / 60)) < 0.05

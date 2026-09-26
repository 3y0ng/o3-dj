"""Which O3 venue is this DJ in?

In order:
  1. `live.venue` set explicitly (config.local.json), for overrides and testing;
  2. the Sonos household ID of the speakers we're controlling. Each venue has its own Sonos system,
     the ID never changes, and it's the one thing guaranteed to be at the venue;
  3. this network's public IP (can change if the ISP reassigns it, so it's only a fallback).

The Wi-Fi name isn't used: macOS hides it from apps without location permission, and router
addresses like 192.168.1.1 are the same everywhere.
"""

import logging

import requests

log = logging.getLogger(__name__)


def public_ip():
    try:
        return requests.get("https://api.ipify.org", timeout=5).text.strip()
    except requests.RequestException:
        return None


def detect(cfg, household=None, ip_lookup=public_ip):
    """Returns (venue_code or None, how)."""
    venues = cfg.get("venues") or {}
    explicit = (cfg.get("live") or {}).get("venue")
    if explicit:
        return (explicit, "config") if explicit in venues else (None, f"unknown venue {explicit!r} in config")
    if household:
        for code, v in venues.items():
            if household in (v.get("sonos_households") or []):
                return code, "sonos"
    if any(v.get("public_ips") for v in venues.values()):
        ip = ip_lookup()
        for code, v in venues.items():
            if ip and ip in (v.get("public_ips") or []):
                return code, "public ip"
    return None, f"unknown Sonos system {household}" if household else "no speakers to identify"


def apply(cfg, code, how):
    """Put the detected venue into the (in-memory) config, plus its per-venue settings."""
    cfg.setdefault("live", {})
    cfg["live"]["venue"] = code
    cfg["live"]["venue_detected_by"] = how
    v = (cfg.get("venues") or {}).get(code) or {}
    if v.get("max_volume"):
        cfg["max_volume"] = v["max_volume"]
    return cfg

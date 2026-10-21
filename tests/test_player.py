"""SonosPlayer start-up, with a stand-in for the soco library (no network)."""

import sys
import types

from o3dj.player import SonosPlayer


class FakeGroup:
    def __init__(self, members):
        self.members = members
        self.coordinator = members[0]


class FakeZone:
    def __init__(self, name, ip):
        self.player_name, self.ip_address = name, ip
        self.group = None


def fake_soco(found, direct):
    mod = types.ModuleType("soco")
    mod.discover = lambda timeout=8: set(found)
    mod.SoCo = lambda ip: direct.append(ip) or FakeZone("direct", ip)
    return mod


def test_offline_main_room_falls_back_to_the_biggest_group(monkeypatch):
    painting, storage, mezz = FakeZone("Painting", "1.205"), FakeZone("Storage", "1.68"), FakeZone("Mezz", "0.93")
    big = FakeGroup([painting, storage])
    painting.group = storage.group = big
    mezz.group = FakeGroup([mezz])
    direct = []
    monkeypatch.setitem(sys.modules, "soco", fake_soco([painting, storage, mezz], direct))
    p = SonosPlayer("1.136")  # Office: configured, but not on the network
    assert p.discover() == 3
    assert p.anchor_ip == "1.205" and "1.136" not in p.zones and not direct


def test_nothing_discovered_still_tries_the_configured_room(monkeypatch):
    direct = []
    monkeypatch.setitem(sys.modules, "soco", fake_soco([], direct))
    p = SonosPlayer("1.136")
    p.discover()
    assert p.anchor_ip == "1.136" and direct == ["1.136"]


def test_nothing_discovered_falls_back_to_a_network_scan(monkeypatch):
    painting = FakeZone("Painting", "1.205")
    painting.group = FakeGroup([painting])
    direct = []
    mod = fake_soco([], direct)
    mod.discovery = types.SimpleNamespace(scan_network=lambda **kw: {painting})
    monkeypatch.setitem(sys.modules, "soco", mod)
    p = SonosPlayer("1.136")  # Office: offline, and multicast discovery found nothing
    assert p.discover() == 1
    assert p.anchor_ip == "1.205" and not direct


class DeadZone(FakeZone):
    @property
    def player_name(self):
        raise TimeoutError("Read timed out")

    @player_name.setter
    def player_name(self, v):
        pass


def test_a_room_that_dropped_off_doesnt_break_the_room_list(monkeypatch):
    painting = FakeZone("Painting", "1.205")
    painting.group = FakeGroup([painting])
    monkeypatch.setitem(sys.modules, "soco", fake_soco([painting], []))
    p = SonosPlayer("1.205")
    p.discover()
    p.zones["1.94"] = DeadZone("x", "1.94")   # Cafe Entrance, gone quiet since it was seen
    assert [r["name"] for r in p.speakers()] == ["Painting"]

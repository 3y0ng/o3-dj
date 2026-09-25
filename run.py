"""Start the O3 DJ controller.

    python3 run.py            # real Sonos speakers (coordinator from config.json)
    python3 run.py --mock     # simulated speakers, nothing makes a sound
"""

import argparse
import logging
import threading

from o3dj import config
from o3dj.analysis import Meta
from o3dj.cache import Cache, Prefetcher
from o3dj.dj import DJ
from o3dj.library import Library
from o3dj.player import MockPlayer, SonosPlayer
from o3dj.server import create_app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="simulate speakers")
    ap.add_argument("--speaker", help="IP of the main Sonos room (overrides config)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-prefetch", action="store_true", help="don't cache or analyse tracks")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("soco").setLevel(logging.WARNING)

    cfg = config.load()
    if args.port:
        cfg["port"] = args.port
    if args.mock:
        player = MockPlayer(cfg["mock_track_seconds"])
    else:
        player = SonosPlayer(args.speaker or cfg.get("coordinator_ip"), cfg.get("crossfade", True))
        print("Looking for Sonos speakers...")
        print(f"  found {player.discover()} room(s)")

    library = Library(cfg)
    library.refresh()
    state_file = config.DATA / ("mock_state.json" if args.mock else "state.json")
    dj = DJ(cfg, player, library, Cache(cfg), Meta(), state_file=state_file)

    threading.Thread(target=dj.run, daemon=True, name="dj").start()
    threading.Thread(target=dj.health_loop, daemon=True, name="health").start()
    if not args.no_prefetch:
        dj.prefetcher = Prefetcher(dj)
        threading.Thread(target=dj.prefetcher.run, daemon=True, name="prefetch").start()

    print(f"\n  O3 DJ  ->  http://localhost:{cfg['port']}   (on this Wi-Fi: {dj.base_url})\n")
    create_app(dj).run(host="0.0.0.0", port=cfg["port"], threaded=True)


if __name__ == "__main__":
    main()

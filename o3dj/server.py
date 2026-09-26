"""HTTP API + static UI + media serving (the speakers stream cached files from here)."""

import logging
import re

import requests
from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import NotFound

from .config import CACHE_DIR, LIBRARY, WEB

log = logging.getLogger(__name__)


def create_app(dj):
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False  # keep genre order from config.json

    def ok():
        return jsonify(dj.snapshot())

    @app.errorhandler(NotFound)
    def not_found(e):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(Exception)
    def on_error(e):
        log.exception("request failed")
        if request.path.startswith("/api/"):
            dj.event("error: " + str(e).split(" from ")[0][:90])
        code = getattr(e, "code", 500)
        return jsonify({"error": str(e)}), code if isinstance(code, int) else 500

    @app.get("/")
    def index():
        return send_from_directory(WEB, "index.html")

    @app.get("/ui/<path:name>")
    def ui(name):
        return send_from_directory(WEB, name)

    @app.get("/api/state")
    def state():
        return ok()

    @app.post("/api/control")
    def control():
        body = request.get_json(force=True)
        action = body.get("action")
        if action == "play":
            dj.play()
        elif action == "pause":
            dj.pause()
        elif action == "skip":
            dj.skip()
        elif action in ("up", "down"):
            dj.vote(action, body.get("id"))
        elif action == "restart":
            dj.start()
        else:
            return jsonify({"error": f"unknown action {action!r}"}), 400
        dj.refresh_status()
        return ok()

    @app.post("/api/inputs")
    def inputs():
        dj.set_inputs(request.get_json(force=True))
        return ok()

    @app.post("/api/mode")
    def mode():
        body = request.get_json(force=True)
        if body.get("mode") not in ("demo", "live"):
            return jsonify({"error": "mode must be demo or live"}), 400
        dj.set_mode(body["mode"])
        return ok()

    @app.post("/api/override/clear")
    def clear_override():
        dj.clear_override(request.get_json(force=True).get("key"))
        return ok()

    @app.post("/api/calibration")
    def calibration():
        action = request.get_json(force=True).get("action")
        try:
            if action == "save":
                dj.save_calibration()
            elif action == "reset":
                dj.reset_calibration()
            else:
                return jsonify({"error": "action must be save or reset"}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return ok()

    @app.post("/api/speakers")
    def speakers():
        body = request.get_json(force=True)
        dj.speaker_action(body.get("action"), body.get("ip"))
        return ok()

    @app.post("/api/tracks")
    def add_track():
        body = request.get_json(force=True)
        url, genre = (body.get("url") or "").strip(), body.get("genre")
        if not re.match(r"^https?://", url):
            return jsonify({"error": "URL must start with http:// or https://"}), 400
        if genre not in dj.library.genres():
            return jsonify({"error": "unknown genre"}), 400
        title = (body.get("title") or "").strip() or url.rsplit("/", 1)[-1]
        dj.library.urls.add(url, title, genre, body.get("artist", ""))
        dj.library.refresh()
        dj.event(f"added {title}")
        return ok()

    @app.get("/media/cache/<name>")
    def media_cache(name):
        """Speakers always get this laptop's URL. Serve the cached file if we have it;
        otherwise pass the stream through from the source (the prefetcher caches it meanwhile)."""
        if (CACHE_DIR / name).is_file():
            return send_from_directory(CACHE_DIR, name, conditional=True)
        track = dj.track_for_cache_name(name)
        if not track or not track.url:
            abort(404)
        headers = {"User-Agent": "o3-dj/0.1"}
        if request.headers.get("Range"):
            headers["Range"] = request.headers["Range"]
        try:
            upstream = requests.get(track.url, headers=headers, stream=True, timeout=(5, 30))
        except requests.RequestException:
            abort(502)
        passthrough = {k: v for k, v in upstream.headers.items()
                       if k.lower() in ("content-type", "content-length", "content-range", "accept-ranges")}
        body = upstream.iter_content(1 << 15) if request.method != "HEAD" else []
        return Response(body, status=upstream.status_code, headers=passthrough, direct_passthrough=True)

    @app.get("/media/library/<path:rel>")
    def media_library(rel):
        return send_from_directory(LIBRARY, rel, conditional=True)

    return app

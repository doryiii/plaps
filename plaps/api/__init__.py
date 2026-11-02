"""Flask app factory and the response contract's home.

Every JSON response uses one error shape, ``{"error": {"code", "message"}}``,
for all failures including 404 and 500. The photo-card schema is defined once
here — the JSON API is the client contract, and uniform errors plus one schema
definition are part of it.
"""

from __future__ import annotations

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException


def photo_card(photo) -> dict:
    """The one photo-card schema; the three media fields are prebuilt URLs and
    width/height are the display dimensions the justified-rows layout needs
    before images load."""
    content_hash = photo["content_hash"]
    return {
        "photo_id": photo["photo_id"],
        "content_hash": content_hash,
        "width": photo["width"],
        "height": photo["height"],
        "date_taken": photo["date_taken"],
        "date_source": photo["date_source"],
        "thumb_sq": f"/media/thumb/{content_hash}.sq.webp",
        "thumb_as": f"/media/thumb/{content_hash}.as.webp",
        "large": f"/media/large/{content_hash}.webp",
        "original": f"/original/{photo['photo_id']}",
    }


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def create_app(db, settings, cache, scanner, worker, adapter, index) -> Flask:
    """Build the app: blueprints, error handlers, and the shared collaborators."""
    from plaps.api import folders, media, photos, search, status  # imported here: they need this module

    app = Flask(__name__)
    app.config["PLAPS_DB"] = db
    app.config["PLAPS_SETTINGS"] = settings
    app.config["PLAPS_CACHE"] = cache
    app.config["PLAPS_SCANNER"] = scanner
    app.config["PLAPS_WORKER"] = worker
    app.config["PLAPS_ADAPTER"] = adapter
    app.config["PLAPS_INDEX"] = index
    app.config["PLAPS_FOLDERS"] = folders.FolderTree(db)

    for blueprint in (photos.bp, folders.bp, media.bp, search.bp, status.bp):
        app.register_blueprint(blueprint)

    @app.errorhandler(HTTPException)
    def handle_http(exc: HTTPException):
        code = {
            400: "bad_request",
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.code, "error")
        return jsonify(error_body(code, exc.description)), exc.code

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        from flask import current_app

        current_app.logger.exception("unhandled error")
        return jsonify(error_body("internal_error", "an internal error occurred")), 500

    return app

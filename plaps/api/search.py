"""Search endpoint: filtered, similarity-ranked results with the three result
states — endpoint down, nothing embedded in this filter, nothing reached the
similarity floor — and filter-scoped coverage counts. These are part of the
response contract, not a UI afterthought.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from plaps import paths
from plaps.api import error_body, photo_card
from plaps.api.photos import DEFAULT_LIMIT, MAX_LIMIT, date_bounds
from plaps.embed.adapter import AdapterError, EndpointUnreachable, ModelMismatch

bp = Blueprint("search", __name__)


@bp.get("/api/search")
def search_view():
    app = current_app
    index = app.config["PLAPS_INDEX"]
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify(error_body("bad_request", "q is required")), 400
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except ValueError:
        return jsonify(error_body("bad_request", "limit must be an integer")), 400
    if not 1 <= limit <= MAX_LIMIT:
        return jsonify(error_body("bad_request", f"limit must be 1..{MAX_LIMIT}")), 400

    folder = None
    if request.args.get("folder"):
        folder = paths.decode_folder_id(request.args["folder"])
        if folder is None:
            return jsonify(error_body("bad_request", "malformed folder id")), 400
    low, high = date_bounds()
    if low == "invalid" or high == "invalid":
        return jsonify(error_body("bad_request", "after/before must be ISO dates")), 400

    try:
        result = index.query(query, folder=folder, after=low, before=high, limit=limit)
    except (EndpointUnreachable, ModelMismatch) as exc:
        # never an empty result set: a dead (or wrong-model) endpoint is a
        # distinct, visible state
        body = {"error": {"code": "endpoint_down", "message": str(exc)}}
        body["state"] = "endpoint_down"
        return jsonify(body), 503
    except AdapterError as exc:  # defensive: unknown adapter failures are not 200s
        return jsonify(error_body("endpoint_down", str(exc))), 503

    db = app.config["PLAPS_DB"]
    cards = []
    for hit in result["photos"]:
        row = db.reader().execute("SELECT * FROM photos WHERE photo_id=?", (hit["photo_id"],)).fetchone()
        if row is not None:  # tombstoned between rank and fetch: drop it
            cards.append({**photo_card(row), "score": hit["score"]})
    return jsonify(
        {
            "photos": cards,
            "coverage": result["coverage"],
            "state": result["state"],
        }
    )

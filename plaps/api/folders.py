"""Folder tree endpoints: a derived tree that cannot drift from the index.

The tree is grouped from live-photo path prefixes at query time — no separately
maintained folders table to reconcile — and memoized in-process, keyed on the
scanner's last-pass stamp so a scan commit invalidates it. Each node carries its
recursive newest date_taken and recursive photo count; branches with no photos
anywhere beneath them cannot appear, because every node exists only because a
photo lives under it. Subtree scoping is the index-friendly range
``path >= folder || '/' AND path < folder || '0'``.
"""

from __future__ import annotations

import threading

from flask import Blueprint, current_app, jsonify

from plaps import paths
from plaps.api import error_body
from plaps.api.photos import fetch_window, parse_window_args

bp = Blueprint("folders", __name__)


def _build_tree(db) -> dict:
    """Group live photos into a nested tree with recursive aggregates.

    One row at a time walks each photo's ancestor chain, so both the build and
    the serialization stay linear in photos — no per-node child lookups.
    """
    root = {"name": "", "newest": "", "count": 0, "children": {}}
    for row in db.reader().execute("SELECT path, date_taken FROM photos WHERE deleted_at IS NULL"):
        node = root
        node["count"] += 1
        if row["date_taken"] > node["newest"]:
            node["newest"] = row["date_taken"]
        for part in row["path"].split("/")[:-1]:
            node = node["children"].setdefault(part, {"name": part, "newest": "", "count": 0, "children": {}})
            node["count"] += 1
            if row["date_taken"] > node["newest"]:
                node["newest"] = row["date_taken"]
    return root


def _serialize(node: dict, key: str) -> dict:
    children = [
        _serialize(child, f"{key}/{name}" if key else name)
        for name, child in sorted(
            node["children"].items(),
            key=lambda item: (item[1]["newest"], item[0]),
            reverse=True,
        )
    ]
    return {
        "id": paths.encode_folder_id(key),
        "name": node["name"] or "library",
        "newest_date_taken": node["newest"] or None,
        "photo_count": node["count"],
        "children": children,
    }


class FolderTree:
    """The memoized tree: the serialized JSON is built once and served until the
    scanner's change stamp moves. The stamp bumps only on passes that actually
    changed something, so the idle poll loop never invalidates it."""

    def __init__(self, db):
        self.db = db
        self._lock = threading.Lock()
        self._stamp = None
        self._payload: bytes | None = None

    def get(self) -> bytes:
        stamp = self.db.get_meta("scan_change")
        with self._lock:
            if self._payload is None or stamp != self._stamp:
                self._payload = jsonify(_serialize(_build_tree(self.db), "")).get_data()
                self._stamp = stamp
            return self._payload

    def invalidate(self) -> None:
        with self._lock:
            self._payload = None
            self._stamp = None


@bp.get("/api/folders")
def folder_tree():
    payload = current_app.config["PLAPS_FOLDERS"].get()
    return current_app.response_class(payload, mimetype="application/json")


@bp.get("/api/folders/<folder_id>/photos")
def folder_photos(folder_id: str):
    app = current_app
    db = app.config["PLAPS_DB"]
    rel = paths.decode_folder_id(folder_id)
    if rel is None:
        return jsonify(error_body("bad_request", "malformed folder id")), 400
    # a folder exists only if some live photo sits beneath it
    low, high = paths.subtree_bounds(rel)
    known = (
        db.reader()
        .execute(
            "SELECT 1 FROM photos WHERE deleted_at IS NULL AND path >= ? AND path < ? LIMIT 1",
            (low, high),
        )
        .fetchone()
    )
    if known is None:
        return jsonify(error_body("not_found", "no such folder")), 404
    parsed = parse_window_args()
    if isinstance(parsed, tuple) and isinstance(parsed[0], dict):
        body, code = parsed
        return jsonify(body), code
    order, limit = parsed
    result = fetch_window(db, ["path >= ?", "path < ?"], [low, high], order, limit)
    if "error" in result:
        code, message = result["error"]
        return jsonify(error_body(code, message)), 400
    return jsonify(result)

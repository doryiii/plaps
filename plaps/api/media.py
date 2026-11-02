"""Image and original serving.

Clients never supply paths — every route resolves from the database (by content
hash or by photo id), which makes path traversal structurally impossible. Derived
files carry long-lived immutable cache headers (keys are immutable by
construction), and originals stream with full Range/206 support.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, current_app, send_file
from werkzeug.exceptions import NotFound

from plaps import paths as plaps_paths

bp = Blueprint("media", __name__)

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


def _live_record_by_hash(content_hash: str):
    """A hash serves only when some live record carries it — never generate for
    unknown hashes."""
    db = current_app.config["PLAPS_DB"]
    return (
        db.reader()
        .execute(
            "SELECT path FROM photos WHERE content_hash=? AND deleted_at IS NULL LIMIT 1",
            (content_hash,),
        )
        .fetchone()
    )


@bp.get("/media/thumb/<content_hash>.<variant>.webp")
def thumb(content_hash: str, variant: str):
    if variant not in ("sq", "as"):
        raise NotFound("unknown thumbnail variant")
    return _derived(content_hash, "thumb", variant)


@bp.get("/media/large/<content_hash>.webp")
def large(content_hash: str):
    return _derived(content_hash, "large", None)


def _derived(content_hash: str, kind: str, variant: str):
    if not plaps_paths.valid_hash(content_hash):
        raise NotFound("unknown content hash")
    record = _live_record_by_hash(content_hash)
    if record is None:
        raise NotFound("no live photo with this content hash")
    settings = current_app.config["PLAPS_SETTINGS"]
    cache = current_app.config["PLAPS_CACHE"]
    source = settings.library_root / record["path"]
    targets = cache.ensure(content_hash, kind, source)  # generated on miss, in this thread
    target = targets[0] if kind == "thumb" else targets[-1]
    if variant and kind == "thumb":
        from plaps.images import cache_path

        target = cache_path(cache.cache_dir, variant, content_hash)
    response = send_file(Path(target), mimetype="image/webp", conditional=True)
    response.headers["Cache-Control"] = IMMUTABLE_CACHE
    return response


@bp.get("/original/<int:photo_id>")
def original(photo_id: int):
    db = current_app.config["PLAPS_DB"]
    settings = current_app.config["PLAPS_SETTINGS"]
    row = db.reader().execute("SELECT path, deleted_at FROM photos WHERE photo_id=?", (photo_id,)).fetchone()
    if row is None or row["deleted_at"] is not None:
        raise NotFound("no such photo")
    source = settings.library_root / row["path"]
    try:
        return send_file(Path(source), mimetype="image/jpeg", conditional=True)
    except FileNotFoundError:
        raise NotFound("original file is missing") from None

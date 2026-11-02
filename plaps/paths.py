"""Pure path functions: the path jail, folder ids, and hash validation.

The scanner, media routes, and folder endpoints all reuse these rules, so they
live in one module with no I/O of its own (``tests/test_paths.py`` targets it).
"""

from __future__ import annotations

import base64
import re
from pathlib import Path, PurePosixPath

# The library root's folder id: an empty path segment cannot appear in the
# /api/folders/<id>/photos route, so the root gets a reserved literal.
ROOT_ID = "root"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def valid_hash(value: str) -> bool:
    """True when value is a blake2b-32 content hash: exactly 64 hex chars."""
    return bool(_HASH_RE.match(value))


def relative_to_library(root: Path, path: Path) -> str:
    """Library-relative POSIX path (no leading slash). Raises if outside root."""
    resolved = path.resolve()
    root = root.resolve()
    if resolved == root:
        return ""
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path {path} is outside the library root {root}") from exc


def inside_jail(root: Path, path: Path) -> bool:
    """True when path stays under root (defense in depth; symlinks are skipped)."""
    try:
        relative_to_library(root, path)
        return True
    except ValueError:
        return False


def encode_folder_id(rel_path: str) -> str:
    """base64url of the library-relative directory path, no trailing slash."""
    if not rel_path:
        return ROOT_ID
    return base64.urlsafe_b64encode(rel_path.encode()).decode().rstrip("=")


def decode_folder_id(folder_id: str) -> str | None:
    """Inverse of encode_folder_id; None when the id is malformed.

    Decoding validates strictly: base64 would otherwise silently drop invalid
    characters, turning garbage like '!!!' into the root."""
    if folder_id == ROOT_ID:
        return ""
    if not folder_id:
        return None
    try:
        padding = "=" * (-len(folder_id) % 4)
        decoded = base64.b64decode(folder_id + padding, altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not decoded or PurePosixPath(decoded).is_absolute() or decoded != decoded.strip("/"):
        return None
    return decoded


def subtree_bounds(rel_path: str) -> tuple[str, str]:
    """SQL range exactly covering rel_path's subtree.

    ``'0'`` immediately follows ``'/'' in ASCII, so [rel/'/' , rel/'0') is the
    subtree: case-sensitive, separator-safe, and index-friendly over UNIQUE(path).
    The root is special-cased: it matches everything.
    """
    if not rel_path:
        return ("", "\U0010ffff")
    return (rel_path + "/", rel_path + "0")

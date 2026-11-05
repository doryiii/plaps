"""Configuration loading and validation.

One TOML file (stdlib ``tomllib``), read from ``--config`` or ``PLAPS_CONFIG``.
Required keys refuse to start the service; optional keys carry pinned defaults.
No silent defaults exist for the settings that point at irreplaceable files
(``library_root``) or that define what the vectors mean (``embedding.model_id``).
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_DATABASE_PATH = Path("/var/lib/plaps/plaps.db")
DEFAULT_CACHE_DIR = Path("/var/cache/plaps")

# Constants that are deliberately not config keys.
EMBED_BATCH_SIZE = 8
EMBED_RETRY_BASE = 30.0  # seconds; per-file failures only
EMBED_RETRY_CAP = 3600.0
EMBED_RETRY_MAX_ATTEMPTS = 5
EMBED_CIRCUIT_BASE = 5.0  # endpoint-level circuit backoff
EMBED_CIRCUIT_CAP = 300.0
PROBE_TIMEOUT = 2.0
PROBE_TTL = 60.0
STALL_THRESHOLD = 900.0  # seconds without checkpoint progress
THUMB_SIZE = 512
SCREENFIT_SIZE = 2048
WEBP_QUALITY = 80
INDEX_CAPACITY = 200_000
BACKUP_KEEP = 8


class ConfigError(Exception):
    """Raised when the configuration file is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class EmbeddingSettings:
    base_url: str
    model_id: str
    min_score: float
    min_matches: int


@dataclass(frozen=True)
class ScannerSettings:
    poll_interval: int
    full_sweep_interval: int
    rehash_interval: int
    guard_threshold: float  # percent of the previous pass's live count


@dataclass(frozen=True)
class ServerSettings:
    host: str
    port: int


@dataclass(frozen=True)
class Settings:
    library_root: Path
    database_path: Path
    cache_dir: Path
    backup_dir: Path
    timezone: ZoneInfo
    embedding: EmbeddingSettings
    scanner: ScannerSettings
    purge_after_days: int
    server: ServerSettings


def config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the config file path, or explain what the service expects."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("PLAPS_CONFIG")
    if env:
        return Path(env)
    raise ConfigError("no configuration file given — pass --config PATH or set PLAPS_CONFIG")


def _require(data: dict, key: str) -> object:
    section, _, name = key.rpartition(".")
    source = data
    if section:
        source = data.get(section)
        if not isinstance(source, dict):
            raise ConfigError(f"missing required key [{section}] (needed for {key})")
    if name not in source:
        raise ConfigError(f"missing required key {key}")
    return source[name]


def _optional(data: dict, key: str, default):
    section, _, name = key.rpartition(".")
    source = data.get(section) if section else data
    if not isinstance(source, dict) or name not in source:
        return default
    return source[name]


def _validate_url(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError("embedding.base_url must be a string")
    scheme = value.split("://", 1)[0].lower() if "://" in value else ""
    if scheme not in ("http", "https"):
        raise ConfigError("embedding.base_url scheme must be http or https")
    return value


def _validate_listen(value: object) -> tuple[str, int]:
    """`server.listen` = "host:port". The default stays loopback-only: exposing
    the app beyond localhost is lighttpd's job (TLS + digest auth)."""
    if not isinstance(value, str) or value.count(":") != 1:
        raise ConfigError('server.listen must be "host:port" (e.g. "127.0.0.1:8096")')
    host, _, raw_port = value.rpartition(":")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f'server.listen port must be an integer, got "{raw_port}"') from exc
    if not 1 <= port <= 65_535:
        raise ConfigError(f"server.listen port must be 1..65535, got {port}")
    return host, port


def _validate_timezone(value: object) -> ZoneInfo:
    if not isinstance(value, str):
        raise ConfigError("timezone must be an IANA name (string)")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone is not a valid IANA name: {value!r}") from exc


def _validate_int(value: object, key: str, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


def load_config(path: str | Path | None = None) -> Settings:
    """Load, default, and validate the configuration. Raises ConfigError."""
    cfg_path = config_path(path)
    try:
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {cfg_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration file is not valid TOML: {exc}") from exc

    library_root = Path(_require(data, "library_root"))
    if not library_root.is_dir():
        raise ConfigError(f"library_root is not an existing directory: {library_root}")

    database_path = Path(_optional(data, "database_path", DEFAULT_DATABASE_PATH))
    cache_dir = Path(_optional(data, "cache_dir", DEFAULT_CACHE_DIR))
    backup_dir = Path(_optional(data, "backup_dir", database_path.parent / "backups"))

    min_score = _optional(data, "embedding.min_score", 0.70)
    if not isinstance(min_score, int | float) or not 0 <= min_score <= 1:
        raise ConfigError("embedding.min_score must be a number between 0 and 1")
    min_matches = _validate_int(_optional(data, "embedding.min_matches", 10), "embedding.min_matches", 0)

    poll_interval = _validate_int(_optional(data, "scanner.poll_interval", 30), "scanner.poll_interval", 1)
    full_sweep = _validate_int(
        _optional(data, "scanner.full_sweep_interval", 86_400),
        "scanner.full_sweep_interval",
        1,
    )
    rehash = _validate_int(
        _optional(data, "scanner.rehash_interval", 30 * 86_400),
        "scanner.rehash_interval",
        1,
    )
    guard = _optional(data, "scanner.guard_threshold", 10)
    if not isinstance(guard, int | float) or not 0 <= guard <= 100:
        raise ConfigError("scanner.guard_threshold must be a number between 0 and 100")
    purge_days = _validate_int(
        _optional(data, "tombstone.purge_after_days", 30), "tombstone.purge_after_days", 1
    )
    host, port = _validate_listen(_optional(data, "server.listen", "127.0.0.1:8096"))

    return Settings(
        library_root=library_root.resolve(),
        database_path=database_path,
        cache_dir=cache_dir,
        backup_dir=backup_dir,
        timezone=_validate_timezone(_require(data, "timezone")),
        embedding=EmbeddingSettings(
            base_url=_validate_url(_require(data, "embedding.base_url")),
            model_id=str(_require(data, "embedding.model_id")),
            min_score=float(min_score),
            min_matches=int(min_matches),
        ),
        scanner=ScannerSettings(
            poll_interval=poll_interval,
            full_sweep_interval=full_sweep,
            rehash_interval=rehash,
            guard_threshold=float(guard),
        ),
        purge_after_days=purge_days,
        server=ServerSettings(host=host, port=port),
    )


def main() -> int:
    """Print the configuration error and exit — the cold-start refusal."""
    try:
        load_config()
    except ConfigError as exc:
        print(f"plaps: {exc}", file=sys.stderr)
        return 2
    print("configuration OK")
    return 0

"""Unit — cold-start refusal for each required key; range validation; default
application. This file guards the settings that point at irreplaceable files."""

import pytest
from conftest import make_config

from plaps import config


def test_defaults_applied(tmp_path, library):
    library.root.mkdir(parents=True, exist_ok=True)
    settings = make_config(tmp_path, library.root)
    assert settings.database_path == tmp_path / "db" / "plaps.db"
    assert settings.cache_dir == tmp_path / "cache"
    assert settings.backup_dir == tmp_path / "backups"
    assert settings.embedding.min_score == 0.70
    assert settings.embedding.min_matches == 10
    assert settings.scanner.poll_interval == 30
    assert settings.scanner.full_sweep_interval == 86_400
    assert settings.scanner.rehash_interval == 30 * 86_400
    assert settings.scanner.guard_threshold == 10
    assert settings.purge_after_days == 30
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8096
    assert settings.timezone.key == "Europe/Berlin"


@pytest.mark.parametrize(
    "listen",
    ["127.0.0.1", "127.0.0.1:8096:extra", "localhost:http", "1.2.3.4:0", "1.2.3.4:70000"],
)
def test_listen_validation(tmp_path, library, listen):
    library.root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(config.ConfigError):
        make_config(tmp_path, library.root, **{"server.listen": listen})


def test_listen_override(tmp_path, library):
    library.root.mkdir(parents=True, exist_ok=True)
    settings = make_config(tmp_path, library.root, **{"server.listen": "127.0.0.1:9000"})
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 9000


def test_backup_dir_defaults_beside_database(tmp_path, library):
    library.root.mkdir(parents=True, exist_ok=True)
    settings = make_config(tmp_path, library.root, backup_dir=None, database_path="/var/lib/plaps/x.db")
    assert settings.backup_dir.as_posix() == "/var/lib/plaps/backups"


@pytest.mark.parametrize(
    "missing",
    [
        "library_root",
        "embedding.base_url",
        "embedding.model_id",
        "timezone",
    ],
)
def test_cold_start_refusal_per_required_key(tmp_path, library, missing):
    library.root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(config.ConfigError, match=missing):
        make_config(tmp_path, library.root, **{missing: None})


def test_missing_library_root_directory(tmp_path):
    with pytest.raises(config.ConfigError, match="library_root"):
        make_config(tmp_path, tmp_path / "nowhere")


def test_no_config_file_at_all(monkeypatch):
    monkeypatch.delenv("PLAPS_CONFIG", raising=False)
    with pytest.raises(config.ConfigError, match="PLAPS_CONFIG"):
        config.load_config(None)


@pytest.mark.parametrize("url", ["ftp://x", "127.0.0.1:8095", ""])
def test_base_url_scheme_validation(tmp_path, library, url):
    library.root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(config.ConfigError, match="base_url"):
        make_config(tmp_path, library.root, **{"embedding.base_url": url})


def test_invalid_timezone(tmp_path, library):
    library.root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(config.ConfigError, match="timezone"):
        make_config(tmp_path, library.root, timezone="Nowhere/None")


@pytest.mark.parametrize(
    "key,bad",
    [
        ("scanner.poll_interval", 0),
        ("scanner.full_sweep_interval", 0),
        ("scanner.rehash_interval", -5),
        ("scanner.guard_threshold", 101),
        ("tombstone.purge_after_days", 0),
        ("embedding.min_score", 1.5),
        ("embedding.min_score", -0.1),
        ("embedding.min_matches", -1),
    ],
)
def test_range_validation(tmp_path, library, key, bad):
    library.root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(config.ConfigError):
        make_config(tmp_path, library.root, **{key: bad})


def test_guard_threshold_zero_disables_guard(tmp_path, library):
    library.root.mkdir(parents=True, exist_ok=True)
    settings = make_config(tmp_path, library.root, **{"scanner.guard_threshold": 0})
    assert settings.scanner.guard_threshold == 0

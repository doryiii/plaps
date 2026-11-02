"""Shared fixtures: a temp-library factory writing real JPEGs (pixel data
generated with pyvips, since piexif can only insert EXIF into existing JPEG
bytes), an in-process app fixture, and mtime manipulation."""

from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
import piexif
import pytest
import pyvips

from plaps import config as plaps_config
from plaps.api import create_app
from plaps.app import startup_model_check
from plaps.db import Database
from plaps.images import ImageCache
from plaps.scanner import Scanner
from plaps.search import VectorIndex
from plaps.worker import Worker

pyvips.cache_set_max(0)


def jpeg_bytes(width=64, height=48, exif_date: str | None = None, orientation: int | None = None) -> bytes:
    """A real JPEG with valid pixel data and optional controlled EXIF."""
    image = pyvips.Image.black(width, height).cast("uchar")
    image = image.draw_rect([255], 0, 0, width // 2, height)  # not uniformly black
    data = image.jpegsave_buffer()
    if exif_date is not None or orientation is not None:
        exif = {"0th": {}, "Exif": {}}
        if exif_date is not None:
            exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = exif_date.encode()
        if orientation is not None:
            exif["0th"][piexif.ImageIFD.Orientation] = orientation
        out = io.BytesIO()
        piexif.insert(piexif.dump(exif), data, out)
        data = out.getvalue()
    return data


class Library:
    """A temp library tree with helpers the scanner tests drive directly."""

    def __init__(self, root: Path):
        self.root = root

    def add(
        self,
        rel_path: str,
        *,
        content: bytes | None = None,
        exif_date: str | None = None,
        orientation: int | None = None,
        width=64,
        height=48,
    ) -> Path:
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = jpeg_bytes(width, height, exif_date, orientation)
        target.write_bytes(content)
        return target

    def delete(self, rel_path: str) -> None:
        (self.root / rel_path).unlink(missing_ok=True)

    def mtime(self, rel_path: str) -> int:
        return (self.root / rel_path).stat().st_mtime_ns


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def make_config(tmp_path: Path, library: Path, **overrides) -> plaps_config.Settings:
    """Settings pointing at temp library/DB/cache; overrides for ranges."""
    values = {
        "library_root": str(library),
        "database_path": str(tmp_path / "db" / "plaps.db"),
        "cache_dir": str(tmp_path / "cache"),
        "backup_dir": str(tmp_path / "backups"),
        "timezone": "Europe/Berlin",
        "embedding.base_url": "http://127.0.0.1:8095",
        "embedding.model_id": "test-model",
    }
    values.update(overrides)
    top: dict[str, object] = {}
    sections: dict[str, dict] = {}
    for key, value in values.items():
        if value is None:
            continue  # a None override removes the key (cold-start refusal tests)
        section, _, name = key.rpartition(".")
        if section:
            sections.setdefault(section, {})[name] = value
        else:
            top[name] = value
    lines = [f"{key} = {_toml_value(value)}" for key, value in top.items()]
    for section, items in sections.items():
        lines.append(f"\n[{section}]")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in items.items())
    cfg = tmp_path / "plaps.toml"
    cfg.write_text("\n".join(lines) + "\n")
    return plaps_config.load_config(cfg)


class FakeAdapter:
    """The fake embedding adapter: injectable vectors for deterministic
    ranking, error injection, and a configurable probe result."""

    def __init__(self, model_id: str = "test-model"):
        self.model_id = model_id
        self.image_vectors: dict[bytes, object] = {}
        self.text_vectors: dict[str, object] = {}
        self.default_image_vector = [1.0] + [0.0] * 7
        self.default_text_vector = [1.0] + [0.0] * 7
        self.probe_result = {
            "reachable": True,
            "served_model": model_id,
            "matches": True,
        }
        self.fail_images_with = None  # exception instance raised per call
        self.fail_text_with = None
        self.calls = {"images": 0, "text": 0, "probe": 0}

    @staticmethod
    def _unit(vector) -> np.ndarray:
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        return array / norm if norm > 0 else array

    def register_image(self, data: bytes, vector) -> None:
        self.image_vectors[data] = vector

    def register_text(self, text: str, vector) -> None:
        self.text_vectors[text] = vector

    def probe(self, force: bool = False) -> dict:
        self.calls["probe"] += 1
        return dict(self.probe_result)

    def embed_images(self, images: list[bytes]) -> list:
        self.calls["images"] += 1
        if self.fail_images_with is not None:
            raise self.fail_images_with
        return [self._unit(self.image_vectors.get(image, self.default_image_vector)) for image in images]

    def embed_text(self, text: str):
        self.calls["text"] += 1
        if self.fail_text_with is not None:
            raise self.fail_text_with
        return self._unit(self.text_vectors.get(text, self.default_text_vector))


class PlapsHarness:
    """Database + index + scanner + worker + cache + in-process test client."""

    def __init__(self, settings: plaps_config.Settings):
        self.settings = settings
        self.db = Database(settings.database_path)
        self.cache = ImageCache(self.db, settings.cache_dir)
        self.cache.init_counter()
        self.adapter = FakeAdapter(settings.embedding.model_id)
        self.index = VectorIndex(self.db, settings, self.adapter)
        self.scanner = Scanner(settings, self.db, cache=self.cache, index=self.index)
        self.worker = Worker(
            self.db,
            self.cache,
            settings.library_root,
            adapter=self.adapter,
            index=self.index,
        )
        startup_model_check(self.db, settings)
        self.app = create_app(
            self.db,
            settings,
            self.cache,
            self.scanner,
            self.worker,
            self.adapter,
            self.index,
        )
        self.client = self.app.test_client()

    def scan(self, mode: str | None = None) -> dict:
        self.scanner.run_pass(mode)
        import json

        return json.loads(self.db.get_meta("scan_stats", "{}"))

    def drain(self) -> None:
        self.worker.drain()

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path / "library")


@pytest.fixture
def make_harness(tmp_path, library):
    """Harness factory: make_harness(**config_overrides). The mass-delete guard
    is off by default so identity tests can delete freely; the guard tests opt
    in with scanner.guard_threshold."""
    library.root.mkdir(parents=True, exist_ok=True)
    created = []

    def _make(**overrides):
        defaults = {"scanner.guard_threshold": 0}
        defaults.update(overrides)
        harness = PlapsHarness(make_config(tmp_path, library.root, **defaults))
        created.append(harness)
        return harness

    yield _make
    for instance in created:
        instance.close()


@pytest.fixture
def harness(make_harness):
    return make_harness()


def set_mtime_ns(path: Path, mtime_ns: int) -> None:
    """Set both mtime and atime, preserving sub-second precision."""
    atime = path.stat().st_atime_ns
    os.utime(path, ns=(atime, mtime_ns))


__all__ = [
    "FakeAdapter",
    "Library",
    "PlapsHarness",
    "jpeg_bytes",
    "make_config",
    "set_mtime_ns",
]

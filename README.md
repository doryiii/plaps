# plaps

A single-user, self-hosted photo gallery that indexes an existing on-disk
library in place. No uploads, no NodeJS, no Docker: your photos stay where
they are, plaps builds a SQLite index beside them, and one Python process
serves the UI, the API, and the derived media.

The full design lives in `docs/designs/plaps.md`.

## What it does

- **Folder view** — the library's directory tree, browsed as-is, with a
  timeline of every subtree.
- **Timeline** — photos ordered by the date they were taken (EXIF first,
  file mtime as fallback), paged with a stable keyset cursor.
- **Search** — natural-language queries embedded by a llama.cpp endpoint and
  ranked against per-photo embeddings (a resident in-memory matrix; no
  vector-DB service).
- **Derived media** — 512px square/aspect thumbnails and a 2048px webfit
  (WebP), generated lazily by pyvips into `/var/cache/plaps` and hard-linked
  into place. Originals are streamed straight off disk.
- **In-place indexing** — the scanner walks the library, hashes new or
  changed files, and reconciles moves, renames, deletions, and duplicates by
  content hash. Nothing is ever written inside the library.

## Requirements

- Linux, Python 3.11+
- libvips (the system package; pyvips binds to it)
- lighttpd (only for the public TLS/auth front door; optional in dev)
- A llama.cpp server (or any OpenAI-ish `/v1/embeddings` endpoint) for search

## Install

Everything lives under `/opt/plaps`; state lives in `/var/lib/plaps`
(database + backups) and `/var/cache/plaps` (derived media). Both are
regenerable from the library except the database, which the backup timer
covers.

```sh
# OS packages (Arch names; adapt to your distro)
sudo pacman -S libvips lighttpd

# The app itself, with its own stdlib venv
sudo mkdir -p /opt/plaps && sudo chown -R "$USER:wheel" /opt/plaps
git clone <this repo> /opt/plaps
python -m venv /opt/plaps/venv
/opt/plaps/venv/bin/pip install -e "/opt/plaps[dev]"

# State directories owned by the service user
sudo useradd -r -d /var/lib/plaps -M plaps 2>/dev/null || true
sudo mkdir -p /var/lib/plaps /var/cache/plaps
sudo chown -R plaps:plaps /var/lib/plaps /var/cache/plaps
```

To rebuild the environment from scratch later, it is one line:

```sh
rm -rf /opt/plaps/venv && python -m venv /opt/plaps/venv && /opt/plaps/venv/bin/pip install -e "/opt/plaps[dev]"
```

## Configuration

`/etc/plaps/plaps.toml` (path overridable with `--config` or
`PLAPS_CONFIG`). Two keys are required; everything else has a pinned default.

```toml
library_root = "/mnt/photos"
timezone     = "Europe/Berlin"

[embedding]
base_url = "http://ai-box:8095"   # llama.cpp server, /v1/embeddings
model_id = "gemma-embedding"      # must match what the endpoint serves

# Optional keys and their defaults:
# database_path = "/var/lib/plaps/plaps.db"
# cache_dir     = "/var/cache/plaps"
# backup_dir    = "<database dir>/backups"
# min_score     = 0.70   # similarity floor for "ok" search results
# min_matches   = 10     # top up to this many results even below the floor

# [scanner]
# poll_interval        = 30        # seconds; stat-pruned pass
# full_sweep_interval  = 86400     # walk everything, hash on stat change
# rehash_interval      = 2592000   # re-hash every file regardless of stat
# guard_threshold      = 10        # % of live photos; > that vanishing halts
                                  # tombstoning until resumed (0 disables)

# [server]
# listen = "127.0.0.1:8096"        # host:port; keep it loopback — TLS and
                                  # auth are lighttpd's job

# [tombstone]
# purge_after_days     = 30        # deleted photos purge after this
```

The service refuses to start when a required key is missing or a value is
out of range, and says so in plain text.

## Run

### systemd

```sh
sudo cp systemd/plaps.service systemd/plaps-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now plaps
```

The unit runs as the `plaps` user with `ProtectSystem=strict`, write access
only to `/var/lib/plaps` and `/var/cache/plaps`, and the library mounted
read-only. First start creates the database and begins the initial scan; the
UI is usable immediately, photos appear as the scan progresses.

### The front door (lighttpd)

plaps itself listens on `127.0.0.1:8096` only. `lighttpd/plaps-lighttpd.conf`
is a drop-in vhost that serves the static UI directly from disk and proxies
`/api`, `/media`, and `/original` to the app, with digest auth and an
optional TLS block:

```sh
sudo mkdir -p /srv/http
sudo ln -sfn /opt/plaps/static /srv/http/plaps
sudo cp lighttpd/plaps-lighttpd.conf /etc/lighttpd/vhosts.d/
# edit the vhost: server name, TLS cert paths, digest user list
sudo systemctl reload lighttpd
```

For development, point the vhost's docroot at your checkout's `static/`
instead of the symlink, or skip lighttpd entirely — Flask serves the static
files itself.

## Backups and restore

The timer takes one `VACUUM INTO` snapshot per week and keeps the last 8:

```sh
sudo cp systemd/plaps-backup.service systemd/plaps-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now plaps-backup.timer
# snapshots land in <backup_dir>/plaps-YYYYMMDD-HHMMSS.db (default
# /var/lib/plaps/backups/). The timer is Persistent=true: downtime does not
# skip the week.
```

Restoring is stop–copy–start:

```sh
sudo systemctl stop plaps
sudo cp /var/lib/plaps/backups/plaps-YYYYMMDD-HHMMSS.db /var/lib/plaps/plaps.db
sudo systemctl start plaps
```

The scanner reconciles on the next pass: photos deleted after the snapshot
tombstone again, photos added since re-add. Derived media in
`/var/cache/plaps` is keyed by content hash and regenerates on demand, so a
stale cache is never a problem — `rm -rf /var/cache/plaps/*` is always safe.

## Tests

```sh
/opt/plaps/venv/bin/python -m pytest
/opt/plaps/venv/bin/ruff check .
```

The suite is hermetic (an on-disk `Library` fixture and a fake embedding
adapter) except `tests/test_adapter_contract.py`, which is skipped unless a
real endpoint is exported:

```sh
PLAPS_ADAPTER_URL=http://ai-box:8095 PLAPS_ADAPTER_MODEL=gemma-embedding \
  /opt/plaps/venv/bin/python -m pytest tests/test_adapter_contract.py
```

`tests/test_stress_concurrency.py` exercises concurrent browsing, scanning,
and downloads; `tests/tools/make_synthetic_db.py` builds a synthetic
150K-photo database for performance smoke-testing (see its docstring).

"""Entrypoint: ``python -m plaps`` — serve (default) or backup.

Keeps the systemd unit and manual runs identical, and gives the backup timer
something to invoke. The config path comes from --config or PLAPS_CONFIG; a
cold start with no file prints what the service expects and exits.
"""

from __future__ import annotations

import argparse
import sys

from plaps import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plaps", description="on-disk photo gallery")
    parser.add_argument("--config", help="path to the TOML configuration file")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "backup"],
        help="serve (default) or backup (one VACUUM INTO snapshot)",
    )
    args = parser.parse_args(argv)

    try:
        settings = config.load_config(args.config)
    except config.ConfigError as exc:
        print(f"plaps: {exc}", file=sys.stderr)
        return 2

    if args.command == "backup":
        from plaps.db import Database

        db = Database(settings.database_path)
        target = db.backup(settings.backup_dir)
        print(f"backup written: {target}")
        db.close()
        return 0

    from plaps.app import serve

    serve(settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())

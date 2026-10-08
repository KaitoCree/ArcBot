"""Entry point.

  python -m arcbot            run the bot
  python -m arcbot --check    validate config/copy/db/tools and print a summary (no Discord connection)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from .config import ConfigError, load_config
from .copytext import load_copy


def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("ARCBOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.ERROR)  # hide "voice will NOT be supported" (unused)


def check() -> int:
    from . import __version__
    from .db import connect, default_db_path, schema_version

    ok = True
    print(f"arcbot {__version__} check")
    try:
        cfg = load_config()
        copy = load_copy()
    except ConfigError as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(f"[ok]   config: guild '{cfg.guild_name}', ranks {', '.join(r.role for r in cfg.ranks)}")
    print(f"[ok]   copy: {len(copy.all_keys())} strings")
    print(f"[ok]   job tiers: {', '.join(t.label for t in cfg.job_tiers.values())}")
    print(f"[ok]   flag list: {sum(len(v.get('terms', [])) for v in cfg.flag_list.values() if isinstance(v, dict))} terms")

    try:
        import discord.ui as ui
        import discord

        ui.Label, ui.FileUpload  # noqa: B018
        print(f"[ok]   discord.py {discord.__version__} (Label + FileUpload available)")
    except (ImportError, AttributeError) as exc:
        ok = False
        print(f"[FAIL] discord.py lacks modal Label/FileUpload: {exc}")

    from .ocr.reader import tesseract_cmd

    tess = tesseract_cmd()
    if tess:
        try:
            import pytesseract

            pytesseract.pytesseract.tesseract_cmd = tess
            print(f"[ok]   tesseract {pytesseract.get_tesseract_version()} at {tess}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[FAIL] tesseract at {tess} does not run: {exc}")
    else:
        print("[warn] tesseract not found: screenshot reading falls back to manual entry")

    try:
        conn = connect()
        print(f"[ok]   database {default_db_path()} schema v{schema_version(conn)}")
        conn.close()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] database: {exc}")

    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("GUILD_ID")
    print(f"[{'ok' if token else 'warn'}]{'   ' if token else ' '}DISCORD_TOKEN {'set' if token else 'missing'}")
    print(f"[{'ok' if guild else 'warn'}]{'   ' if guild else ' '}GUILD_ID {'set' if guild else 'missing'}")
    print(f"[info] dry run: {'ON' if os.environ.get('ARCBOT_DRY_RUN') == '1' else 'off'}")
    print("[info] timers:", "enabled" if cfg.timers.get("enabled") else "disabled",
          "| keepalive:", "enabled" if cfg.keepalive.get("enabled") else "disabled")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="arcbot")
    parser.add_argument("--check", action="store_true", help="validate setup and exit")
    args = parser.parse_args(argv)
    setup_logging()
    if args.check:
        return check()
    from .bot import run

    return run()


if __name__ == "__main__":
    sys.exit(main())

"""Nightly SQLite backups: online backup API -> gzip -> keep N. Optional push to a PRIVATE git repo.

Run standalone too:  python -m arcbot.services.backup
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from ..config import ROOT, Config

log = logging.getLogger("arcbot.backup")


def backup_dir(cfg: Config) -> Path:
    p = Path(cfg.backups.get("local_dir", "data/backups"))
    return p if p.is_absolute() else ROOT / p


def run_backup(conn: sqlite3.Connection, cfg: Config, *, now: datetime | None = None) -> Path:
    """Consistent copy of the live DB (safe while the bot is writing), gzipped, then rotate."""
    out_dir = backup_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"arcbot-{stamp}.db.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "snap.db"
        dest = sqlite3.connect(tmp_db)
        try:
            conn.backup(dest)
        finally:
            dest.close()
        check = sqlite3.connect(tmp_db)
        try:
            ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if ok != "ok":
            raise RuntimeError(f"backup integrity check failed: {ok}")
        with open(tmp_db, "rb") as src, gzip.open(target, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
    rotate(out_dir, int(cfg.backups.get("keep_daily", 14)))
    log.info("backup written: %s (%d KB)", target.name, target.stat().st_size // 1024)
    if cfg.backups.get("git_push"):
        try:
            git_push(out_dir, target)
        except Exception as exc:  # noqa: BLE001
            log.error("backup git push failed: %s", exc)
    return target


def rotate(out_dir: Path, keep: int) -> list[Path]:
    files = sorted(out_dir.glob("arcbot-*.db.gz"))
    removed = files[:-keep] if keep > 0 else []
    for f in removed:
        f.unlink(missing_ok=True)
    return removed


def git_push(out_dir: Path, newest: Path) -> None:
    """Commit the newest backup as latest.db.gz in a git checkout at <backups>/repo and push.

    One-time setup (see docs/DEPLOY.md): clone your PRIVATE backup repo into <backups>/repo with a
    read-write deploy key. Only the latest file is kept in the tree; history holds older ones.
    """
    repo = out_dir / "repo"
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git checkout; clone the private backup repo there first")
    shutil.copy2(newest, repo / "latest.db.gz")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True, timeout=120)

    git("add", "latest.db.gz")
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True,
                            env=env, timeout=60)
    if status.stdout.strip():
        git("commit", "-m", f"backup {newest.name}")
        git("push", "--quiet")
        log.info("backup pushed to private repo")


def restore(backup_file: Path, db_path: Path) -> None:
    """Decompress a backup over db_path. Stop the bot first."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    with gzip.open(backup_file, "rb") as src, open(db_path, "wb") as dst:
        shutil.copyfileobj(src, dst)


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    from ..config import load_config
    from ..db import connect, default_db_path

    load_dotenv()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="arcbot backup / restore")
    ap.add_argument("--restore", type=Path, help="restore this .db.gz over the live database (stop the bot first)")
    args = ap.parse_args()
    if args.restore:
        restore(args.restore, default_db_path())
        print(f"restored {args.restore} -> {default_db_path()}")
    else:
        c = connect()
        print(run_backup(c, load_config()))

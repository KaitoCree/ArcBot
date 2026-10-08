from datetime import datetime

from arcbot.db import connect
from arcbot.engine import RankEngine
from arcbot.services import backup


def test_backup_rotate_and_restore(cfg, tmp_path, monkeypatch):
    monkeypatch.setitem(cfg.backups, "local_dir", str(tmp_path / "b"))
    monkeypatch.setitem(cfg.backups, "git_push", False)
    live = connect(tmp_path / "live.db")
    RankEngine(live, cfg).place(42, "pathfinder")
    paths = [backup.run_backup(live, cfg, now=datetime(2026, 10, d)) for d in range(1, 17)]
    kept = sorted((tmp_path / "b").glob("arcbot-*.db.gz"))
    assert len(kept) == 14 and kept[-1] == paths[-1] and paths[0] not in kept
    backup.restore(paths[-1], tmp_path / "restored.db")
    r = connect(tmp_path / "restored.db")
    assert r.execute("SELECT points, rank_key FROM users WHERE discord_id = 42").fetchone()[:] == (50, "pathfinder")

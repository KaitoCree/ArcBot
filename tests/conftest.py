import json
from pathlib import Path

import pytest

from arcbot.config import load_config
from arcbot.copytext import load_copy
from arcbot.db import connect
from arcbot.engine import RankEngine

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config" / "arcbot.yaml")


@pytest.fixture(scope="session")
def copy():
    return load_copy(ROOT / "config" / "copy.yaml")


@pytest.fixture(scope="session")
def expected():
    return json.loads((FIXTURES / "expected_stats.json").read_text(encoding="utf-8"))


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def engine(conn, cfg):
    return RankEngine(conn, cfg)

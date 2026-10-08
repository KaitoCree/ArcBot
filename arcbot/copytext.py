"""Player-facing strings from config/copy.yaml.

Named copytext (not copy) so it never shadows the stdlib `copy` module.
Every player-facing message goes through `Copy.t()`, which refuses point-like placeholders.
"""
from __future__ import annotations

import os
import re
import string
from pathlib import Path
from typing import Any

import yaml

from .config import ROOT, ConfigError

_POINTISH = re.compile(r"point|score|pts", re.IGNORECASE)


class Copy:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw

    def has(self, key: str) -> bool:
        try:
            self._lookup(key)
            return True
        except KeyError:
            return False

    def _lookup(self, key: str) -> str:
        node: Any = self.raw
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"copy.yaml has no key '{key}'")
            node = node[part]
        if not isinstance(node, str):
            raise KeyError(f"copy.yaml key '{key}' is not a string")
        return node

    def t(self, key: str, **kwargs: Any) -> str:
        for name in kwargs:
            if _POINTISH.search(name):
                raise ValueError(f"refusing to put '{name}' into player-facing text (points are hidden)")
        template = self._lookup(key)
        fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
        missing = fields - kwargs.keys()
        if missing:
            raise KeyError(f"copy '{key}' needs {sorted(missing)}")
        return template.format(**kwargs)

    def all_keys(self) -> list[str]:
        out: list[str] = []

        def walk(node: Any, prefix: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{prefix}.{k}" if prefix else k)
            elif isinstance(node, str):
                out.append(prefix)

        walk(self.raw, "")
        return out

    def placeholders(self, key: str) -> set[str]:
        return {f for _, f, _, _ in string.Formatter().parse(self._lookup(key)) if f}


def load_copy(path: str | os.PathLike | None = None) -> Copy:
    p = Path(path or os.environ.get("ARCBOT_COPY") or ROOT / "config" / "copy.yaml")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot load {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must be a mapping")
    return Copy(raw)

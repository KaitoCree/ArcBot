"""Channel-name matching that ignores decoration: '✅vouch', '💬・general-chat' and 'vouch' are the same channel."""
from __future__ import annotations

import re

_EDGE = re.compile(r"^[\W_]+|[\W_]+$")


def norm_channel(name: str) -> str:
    return _EDGE.sub("", (name or "").lstrip("#")).casefold()

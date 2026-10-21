"""Blocks the Roblox website through the Windows hosts file while Roblox Shield is not protecting."""

from __future__ import annotations

import os
from pathlib import Path

from winapi import flush_dns_cache

HOSTS_PATH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
BEGIN = "# >>> Roblox Guard: blocked until Roblox Shield protects (managed automatically)"
END = "# <<< Roblox Guard"
# hosts has no wildcards: the entry pages are enough, nothing on the site works without them.
BLOCKED_HOSTS = (
    "roblox.com", "www.roblox.com", "web.roblox.com", "create.roblox.com",
    "apis.roblox.com", "auth.roblox.com",
)


def _read() -> str:
    # latin-1 round-trips any byte, so foreign comments in hosts survive a rewrite untouched.
    return HOSTS_PATH.read_bytes().decode("latin-1")


def _strip_block(text: str) -> str:
    lines, skipping = [], False
    for line in text.splitlines():
        if line.strip() == BEGIN:
            skipping = True
        elif skipping and line.strip() == END:
            skipping = False
        elif not skipping:
            lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\r\n".join(lines) + "\r\n"


def is_blocked() -> bool:
    try:
        return BEGIN in _read()
    except OSError:
        return False


def set_blocked(block: bool) -> bool:
    """Add or remove our section. Returns True if the file changed. Raises OSError if hosts is not writable."""
    current = _read()
    wanted = _strip_block(current)
    if block:
        wanted += "\r\n" + "\r\n".join([BEGIN, *(f"0.0.0.0 {host}" for host in BLOCKED_HOSTS), END]) + "\r\n"
    if wanted == current:
        return False
    HOSTS_PATH.write_bytes(wanted.encode("latin-1"))
    flush_dns_cache()
    return True

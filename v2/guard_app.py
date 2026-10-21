"""
Roblox Guard — tray-only watcher that starts with Windows and covers the moments Roblox Shield is not running.

  * Roblox / Roblox Studio started while Shield is not protecting -> killed at once, Shield is opened and
    asked to connect.
  * The Roblox website is blocked through the hosts file whenever Shield is not protecting, so a browser
    never reaches it with the Vietnamese IP. When the browser shows the blocked page, Shield is started too.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape

import pystray
from PIL import Image

import hostsblock
import i18n
from tunnel import ROBLOX_PROCESSES
from winapi import (
    GUARD_APP_MUTEX, SHIELD_CONNECT_EVENT, SHIELD_PROTECTED_MUTEX, SHIELD_TUNNEL_MUTEX, NamedMutex,
    find_processes, is_admin, mutex_exists, relaunch_as_admin, signal_event, terminate_process,
    visible_window_titles,
)

APP_TITLE = "Roblox Guard"
TASK_NAME = "Roblox Guard"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent

ROBLOX_NAMES = frozenset(name.lower() for name in ROBLOX_PROCESSES)
# Chromium's error page for a blocked host is titled with the bare host name: "www.roblox.com - Brave".
# Exactly one " - <browser>" suffix, so "roblox.com - Google Search - Brave" does not match.
BLOCKED_PAGE_TITLE = re.compile(r"^(?:[\w-]+\.)?roblox\.com(?: [-—] [^-—]+)?$", re.IGNORECASE)
POLL_S = 0.5
REQUEST_COOLDOWN_S = 20
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

tr = i18n.Strings({
    "watching": {"vi": "đang canh gác", "en": "watching"},
    "error": {"vi": "lỗi {error}", "en": "error {error}"},
    "open_shield": {"vi": "Mở Roblox Shield", "en": "Open Roblox Shield"},
    "pause": {"vi": "Tạm tắt canh gác (đến lần mở máy sau)", "en": "Pause guarding (until next restart)"},
    "uninstall": {"vi": "Gỡ khỏi khởi động cùng Windows", "en": "Remove from Windows startup"},
    "shield_failed": {"vi": "Không mở được Roblox Shield: {error}", "en": "Could not open Roblox Shield: {error}"},
    "closed_game": {
        "vi": "Đã đóng Roblox vì Roblox Shield chưa bật bảo vệ. Đang bật bảo vệ — xong thì bấm Play lại.",
        "en": "Closed Roblox because Roblox Shield is not protecting. Turning protection on — then press Play again.",
    },
    "blocked_page": {
        "vi": "Trang Roblox bị chặn vì Roblox Shield chưa bật bảo vệ. Đang bật bảo vệ — xong thì tải lại trang (F5).",
        "en": "The Roblox website is blocked because Roblox Shield is not protecting. Turning protection on — "
              "then reload the page (F5).",
    },
    "hosts_failed": {"vi": "Không sửa được file hosts để chặn web Roblox: {error}",
                     "en": "Could not edit the hosts file to block the Roblox website: {error}"},
})


def shield_command(*args: str) -> list[str]:
    if FROZEN:
        return [str(BASE_DIR / "RobloxShield.exe"), *args]
    return [sys.executable, str(BASE_DIR / "app.py"), *args]


def register_autostart() -> None:
    """Logon task with highest privileges: starts elevated without a UAC prompt, no time or battery limits."""
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Blocks Roblox while Roblox Shield is not protecting.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author">
    <UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel>
  </Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{escape(sys.executable)}</Command><WorkingDirectory>{escape(str(BASE_DIR))}</WorkingDirectory>
  </Exec></Actions>
</Task>
"""
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", path, "/F"],
                       capture_output=True, creationflags=_NO_WINDOW, check=False)
    finally:
        os.unlink(path)


def unregister_autostart() -> None:
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                   capture_output=True, creationflags=_NO_WINDOW, check=False)


class Guard:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.seen_game_pids: set[int] = set()
        self.seen_blocked_tabs: set[tuple[int, str]] = set()
        self.last_request = 0.0
        self.hosts_error_shown = False
        icon_path = BASE_DIR / "app_icon.ico"
        image = Image.open(icon_path) if icon_path.is_file() else Image.new("RGB", (64, 64), "#1a7f37")
        self.tray = pystray.Icon("RobloxGuard", image, f"{APP_TITLE}: {tr('watching')}", pystray.Menu(
            pystray.MenuItem(tr("open_shield"), lambda *_: self.open_shield(connect=False), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(tr("pause"), lambda *_: self.quit(uninstall=False)),
            pystray.MenuItem(tr("uninstall"), lambda *_: self.quit(uninstall=True)),
        ))

    # ── Shield control ────────────────────────────────────────────────────────
    def open_shield(self, connect: bool) -> None:
        # A running Shield takes the request through its event; otherwise start one (it inherits our
        # elevation, so there is no UAC prompt).
        if connect and signal_event(SHIELD_CONNECT_EVENT):
            return
        try:
            subprocess.Popen(shield_command(*(["--connect"] if connect else [])), cwd=BASE_DIR, close_fds=True)
        except OSError as exc:
            self.tray.notify(tr("shield_failed", error=exc), APP_TITLE)

    def request_protection(self, message: str) -> None:
        if time.monotonic() - self.last_request < REQUEST_COOLDOWN_S:
            return
        self.last_request = time.monotonic()
        self.tray.notify(message, APP_TITLE)
        if not mutex_exists(SHIELD_TUNNEL_MUTEX):   # Shield is not already connecting
            self.open_shield(connect=True)

    # ── Watch loop ────────────────────────────────────────────────────────────
    def run(self, icon: pystray.Icon) -> None:
        icon.visible = True
        while not self.stop.is_set():
            try:
                self.check()
            except Exception as exc:   # never let one bad poll kill the guard
                icon.title = f"{APP_TITLE}: {tr('error', error=exc)}"
            self.stop.wait(POLL_S)
        # Cleanup here, after the last check(), so no poll can re-add the block behind it.
        try:
            hostsblock.set_blocked(False)
        except OSError:
            pass
        icon.stop()

    def check(self) -> None:
        protected = mutex_exists(SHIELD_PROTECTED_MUTEX)
        self.sync_hosts(block=not protected)
        if protected:
            self.seen_game_pids.clear()
            self.seen_blocked_tabs.clear()
            return

        pids = set(find_processes(ROBLOX_NAMES))
        if pids:
            for pid in pids:
                terminate_process(pid)
            if pids - self.seen_game_pids:
                self.request_protection(tr("closed_game"))
        self.seen_game_pids = pids

        tabs = {(hwnd, title) for hwnd, title in visible_window_titles().items() if BLOCKED_PAGE_TITLE.match(title)}
        if tabs - self.seen_blocked_tabs:
            self.request_protection(tr("blocked_page"))
        self.seen_blocked_tabs = tabs

    def sync_hosts(self, block: bool) -> None:
        try:
            hostsblock.set_blocked(block)
            self.hosts_error_shown = False
        except OSError as exc:
            if not self.hosts_error_shown:
                self.hosts_error_shown = True
                self.tray.notify(tr("hosts_failed", error=exc), APP_TITLE)

    def quit(self, uninstall: bool) -> None:
        if uninstall:
            unregister_autostart()
        self.stop.set()   # run() unblocks the website and closes the tray


def apply_language() -> None:
    """Same language as Roblox Shield: its settings.json sits next to us."""
    try:
        choice = json.loads((BASE_DIR / "settings.json").read_text(encoding="utf-8")).get("language")
    except (OSError, ValueError):
        choice = None
    i18n.set_language(choice)


def main() -> None:
    apply_language()
    if not is_admin():
        relaunch_as_admin()
        return
    instance = NamedMutex(GUARD_APP_MUTEX)
    if not instance.acquire():
        return
    if FROZEN:
        register_autostart()
    guard = Guard()
    guard.tray.run(setup=guard.run)


if __name__ == "__main__":
    main()

"""
Steam Unblock — gets past the ISP block on Steam without a VPN.

Only connections to Steam domains pass through sing-box (fake-IP range routed into a TUN); game traffic
never touches it, so in-game ping is unchanged. sing-box is bound to a kill-on-close Job Object: closing or
crashing this app removes the bypass and the TUN with it.
"""

from __future__ import annotations

import enum
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from tunnel import SingBox, build_steam_unblock_config, host_has_ipv6, probe_steam
from winapi import KillOnCloseJob, enable_dpi_awareness, find_processes, flush_dns_cache, is_admin, relaunch_as_admin

APP_TITLE = "Steam Unblock"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CORE_DIR = BASE_DIR / "core"

ROBLOX_SHIELD_NAMES = frozenset({"robloxshield.exe"})
UI_TICK_MS = 250
WATCHDOG_EVERY_TICKS = 4            # 1 s
CONNECT_TIMEOUT_S = 20
LOG_MAX_LINES = 300

COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_IDLE = "#1a7f37", "#9a6700", "#cf222e", "#57606a"


class State(enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    ON = "on"
    STOPPING = "stopping"


def find_steam_exe() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            path = Path(winreg.QueryValueEx(key, "SteamExe")[0])
        return path if path.is_file() else None
    except OSError:
        return None


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.job = KillOnCloseJob()
        self.singbox = SingBox(CORE_DIR, name="steam-runtime", log_name="steam-unblock.log")
        self.events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self.state = State.IDLE
        self.cancel_start = threading.Event()
        self.tick_count = 0

        self._build_ui()
        self._render_state("Đang tắt", COLOR_IDLE)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(UI_TICK_MS, self._tick)
        self.start()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.minsize(460, 340)
        icon = BASE_DIR / "app_icon.ico"
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))

        pad = {"padx": 12, "pady": 6}
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        self.lbl_state = tk.Label(main, font=("Segoe UI", 15, "bold"), anchor="w")
        self.lbl_state.pack(fill="x", **pad)
        ttk.Label(main, foreground=COLOR_IDLE, wraplength=420, justify="left",
                  text="Chỉ kết nối tới Steam được xử lý. Game và mọi app khác đi mạng bình thường, "
                       "không qua VPN nên không tăng ping.").pack(fill="x", padx=12)

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", **pad)
        self.btn_start = ttk.Button(buttons, text="Bật", command=self.start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_steam = ttk.Button(buttons, text="Mở Steam", command=self.launch_steam)
        self.btn_steam.pack(side="left", fill="x", expand=True, padx=8)
        self.btn_stop = ttk.Button(buttons, text="Tắt", command=self.stop)
        self.btn_stop.pack(side="left", fill="x", expand=True)

        log_frame = ttk.LabelFrame(main, text="Nhật ký")
        log_frame.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(log_frame, height=8, state="disabled", wrap="word", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True)

    def _render_state(self, text: str, color: str) -> None:
        self.lbl_state.configure(text=text, fg=color)
        self.btn_start.state(["!disabled"] if self.state is State.IDLE else ["disabled"])
        self.btn_steam.state(["!disabled"] if self.state is State.ON else ["disabled"])
        self.btn_stop.state(["!disabled"] if self.state in (State.STARTING, State.ON) else ["disabled"])

    def log(self, message: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        overflow = int(self.txt_log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if overflow > 0:
            self.txt_log.delete("1.0", f"{overflow + 1}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    # ── Actions ───────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self.state is not State.IDLE:
            return
        # Both apps create a TUN with strict routing; Roblox Shield already unblocks Steam on its own.
        if find_processes(ROBLOX_SHIELD_NAMES):
            self.log("Roblox Shield đang chạy và đã tự mở khoá Steam, không cần bật app này.")
            self._render_state("Đang tắt (Roblox Shield đang lo Steam)", COLOR_IDLE)
            return
        self.state = State.STARTING
        self.cancel_start.clear()
        self._render_state("Đang bật…", COLOR_WARN)
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self) -> None:
        try:
            config = build_steam_unblock_config(self.singbox.log_path, ipv6=host_has_ipv6())
            pid = self.singbox.start(config)
            if not self.job.assign(pid):
                self.events.put(("log", "Cảnh báo: không gắn được sing-box vào Job Object."))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self.singbox.stop()
            self.events.put(("start_failed", f"Không khởi động được sing-box: {exc}"))
            return

        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last_issue = "chưa có phản hồi"
        while time.monotonic() < deadline and not self.cancel_start.is_set():
            if not self.singbox.is_alive():
                last_issue = f"sing-box đã thoát.\n{self.singbox.log_tail()}"
                break
            flush_dns_cache()
            try:
                self.events.put(("started", probe_steam()))
                return
            except OSError as exc:
                last_issue = str(exc)
            self.cancel_start.wait(1.0)

        self.singbox.stop()
        flush_dns_cache()
        reason = "Đã hủy." if self.cancel_start.is_set() else f"Không vào được Steam: {last_issue}"
        self.events.put(("start_failed", reason))

    def stop(self) -> None:
        if self.state is State.STARTING:
            self.cancel_start.set()
        elif self.state is State.ON:
            self._shutdown("Đã tắt.", COLOR_IDLE)

    def _shutdown(self, reason: str, color: str) -> None:
        self.state = State.STOPPING
        self._render_state("Đang tắt…", COLOR_WARN)

        def worker() -> None:
            self.singbox.stop()
            flush_dns_cache()  # fake IPs must not outlive the TUN
            self.events.put(("stopped", reason, color))

        threading.Thread(target=worker, daemon=True).start()

    def launch_steam(self) -> None:
        exe = find_steam_exe()
        if exe:
            # explorer.exe starts Steam with the normal (non-admin) user token.
            subprocess.Popen(["explorer.exe", str(exe)], close_fds=True)
        else:
            os.startfile("steam://open/main")
        self.log("Đang mở Steam… Nếu Steam đã mở sẵn mà vẫn báo lỗi mạng, hãy thoát hẳn Steam rồi mở lại.")

    # ── Event loop / watchdog ─────────────────────────────────────────────────
    def _tick(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

        self.tick_count += 1
        if self.tick_count % WATCHDOG_EVERY_TICKS == 0 and self.state is State.ON and not self.singbox.is_alive():
            tail = self.singbox.log_tail()
            self.log(f"sing-box dừng đột ngột.\n{tail}" if tail else "sing-box dừng đột ngột.")
            self._shutdown("Bị tắt do lỗi — bấm Bật để thử lại.", COLOR_ERR)
        self.root.after(UI_TICK_MS, self._tick)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self.log(event[1])
        elif kind == "started":
            self.state = State.ON
            self._render_state("Đã mở khoá Steam ✓", COLOR_OK)
            self.log(f"Steam phản hồi (HTTP {event[1]}). Cứ để app chạy trong lúc dùng Steam.")
        elif kind == "start_failed":
            self.state = State.IDLE
            self._render_state("Chưa bật", COLOR_ERR)
            self.log(event[1])
        elif kind == "stopped":
            _, reason, color = event
            self.state = State.IDLE
            self._render_state(reason, color)
            self.log(reason)

    def _on_close(self) -> None:
        self.cancel_start.set()
        # Hide first: stopping sing-box can take a few seconds and the window must not look frozen.
        self.root.withdraw()
        self.root.update_idletasks()
        self.singbox.stop()
        flush_dns_cache()
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    if not is_admin():
        if not relaunch_as_admin():
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, "Cần quyền Administrator để tạo card mạng ảo (TUN).")
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

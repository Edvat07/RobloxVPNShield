"""
Steam Unblock — gets past the ISP block on Steam without a VPN.

Only connections to Steam domains pass through sing-box (fake-IP range routed into a TUN); game traffic
never touches it, so in-game ping is unchanged. sing-box is bound to a kill-on-close Job Object: quitting or
crashing this app removes the bypass and the TUN with it. Closing or minimizing the window only hides it to
the system tray; "Thoát" in the tray menu quits. Optionally starts with Windows (logon task), either hidden in
the tray or with the window shown.
"""

from __future__ import annotations

import enum
import json
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

import pystray
from PIL import Image

import autostart
from tunnel import SingBox, build_steam_unblock_config, host_has_ipv6, probe_steam
from winapi import (
    SHIELD_TUNNEL_MUTEX, STEAM_APP_MUTEX, STEAM_SHOW_EVENT, STEAM_TUNNEL_MUTEX, KillOnCloseJob, NamedEvent,
    NamedMutex, enable_dpi_awareness, find_processes, flush_dns_cache, is_admin, mutex_exists, relaunch_as_admin,
    signal_event,
)

APP_TITLE = "Steam Unblock"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CORE_DIR = BASE_DIR / "core"
SETTINGS_PATH = BASE_DIR / "steam-settings.json"
AUTOSTART_TASK = "SteamUnblock"
STARTUP_TRAY, STARTUP_WINDOW = "tray", "window"

SINGBOX_NAMES = frozenset({"sing-box.exe"})
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
    PAUSED = "paused"       # Roblox Shield's tunnel is up and unblocks Steam itself; resume when it goes down


def find_steam_exe() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            path = Path(winreg.QueryValueEx(key, "SteamExe")[0])
        return path if path.is_file() else None
    except OSError:
        return None


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.show_event = NamedEvent(STEAM_SHOW_EVENT)
        self.settings = load_settings()
        self.job = KillOnCloseJob()
        self.singbox = SingBox(CORE_DIR, name="steam-runtime", log_name="steam-unblock.log")
        self.events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self.state = State.IDLE
        self.cancel_start = threading.Event()
        self.tunnel_flag = NamedMutex(STEAM_TUNNEL_MUTEX)
        self.yielding = False       # stopping because Roblox Shield wants the network, not because the user asked
        self.tick_count = 0
        self.tray_hint_shown = False

        self._build_ui()
        self.tray = self._create_tray()
        self._render_state("Đang tắt", COLOR_IDLE)
        self._sync_autostart()
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        root.bind("<Unmap>", self._on_unmap)
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
                       "không qua VPN nên không tăng ping.\n"
                       "Đóng/thu nhỏ cửa sổ thì app vẫn chạy ngầm ở khay hệ thống; "
                       "chuột phải biểu tượng → Thoát để tắt hẳn.").pack(fill="x", padx=12)

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", **pad)
        self.btn_start = ttk.Button(buttons, text="Bật", command=self.start)
        self.btn_start.pack(side="left", fill="x", expand=True)
        self.btn_steam = ttk.Button(buttons, text="Mở Steam", command=self.launch_steam)
        self.btn_steam.pack(side="left", fill="x", expand=True, padx=8)
        self.btn_stop = ttk.Button(buttons, text="Tắt", command=self.stop)
        self.btn_stop.pack(side="left", fill="x", expand=True)

        startup = ttk.LabelFrame(main, text="Khởi động")
        startup.pack(fill="x", **pad)
        self.var_autostart = tk.BooleanVar(value=False)
        ttk.Checkbutton(startup, text="Khởi động cùng Windows", variable=self.var_autostart,
                        command=self._on_autostart_toggled).pack(anchor="w", padx=8, pady=(4, 0))
        modes = ttk.Frame(startup)
        modes.pack(fill="x", padx=28, pady=(0, 4))
        self.var_startup_mode = tk.StringVar(value=self.settings.get("startup_mode", STARTUP_TRAY))
        self.radio_modes = [
            ttk.Radiobutton(modes, text="Chạy ngầm (chỉ hiện ở khay hệ thống)", value=STARTUP_TRAY,
                            variable=self.var_startup_mode, command=self._on_startup_mode_changed),
            ttk.Radiobutton(modes, text="Hiện cửa sổ", value=STARTUP_WINDOW,
                            variable=self.var_startup_mode, command=self._on_startup_mode_changed),
        ]
        for radio in self.radio_modes:
            radio.pack(side="left", padx=(0, 16))

        log_frame = ttk.LabelFrame(main, text="Nhật ký")
        log_frame.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(log_frame, height=8, state="disabled", wrap="word", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True)

    def _create_tray(self) -> pystray.Icon:
        """Tray callbacks run on pystray's thread, so they only post events for the Tk loop."""
        icon_path = BASE_DIR / "app_icon.ico"
        image = Image.open(icon_path) if icon_path.is_file() else Image.new("RGB", (64, 64), COLOR_OK)
        post = lambda action: lambda *_: self.events.put(("tray", action))
        menu = pystray.Menu(
            pystray.MenuItem("Mở cửa sổ", post("show"), default=True),
            pystray.MenuItem("Mở Steam", post("steam")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Thoát", post("quit")),
        )
        tray = pystray.Icon("SteamUnblock", image, APP_TITLE, menu)
        tray.run_detached()
        return tray

    # ── Start with Windows ────────────────────────────────────────────────────
    def _sync_autostart(self) -> None:
        """The scheduled task is the source of truth; refresh it so a moved app folder keeps working."""
        enabled = autostart.is_enabled(AUTOSTART_TASK)
        if enabled:
            try:
                autostart.enable(AUTOSTART_TASK, APP_TITLE)
            except OSError as exc:
                self.log(f"Không cập nhật được tác vụ khởi động: {exc}")
        self.var_autostart.set(enabled)
        self._render_startup_modes()

    def _render_startup_modes(self) -> None:
        for radio in self.radio_modes:
            radio.state(["!disabled"] if self.var_autostart.get() else ["disabled"])

    def _on_autostart_toggled(self) -> None:
        want = self.var_autostart.get()
        try:
            if want:
                autostart.enable(AUTOSTART_TASK, APP_TITLE)
            else:
                autostart.disable(AUTOSTART_TASK)
        except OSError as exc:
            self.var_autostart.set(not want)
            self.log(f"Không {'bật' if want else 'tắt'} được khởi động cùng Windows: {exc}")
        else:
            self.log("Đã bật khởi động cùng Windows." if want else "Đã tắt khởi động cùng Windows.")
        self._render_startup_modes()

    def _on_startup_mode_changed(self) -> None:
        self.settings["startup_mode"] = self.var_startup_mode.get()
        save_settings(self.settings)
        hidden = self.settings["startup_mode"] == STARTUP_TRAY
        self.log("Khi khởi động cùng Windows: " + ("chạy ngầm ở khay hệ thống." if hidden else "hiện cửa sổ."))

    def starts_hidden(self) -> bool:
        return self.settings.get("startup_mode", STARTUP_TRAY) == STARTUP_TRAY

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            self.tray.notify("Vẫn đang chạy ngầm. Bấm vào biểu tượng ở khay hệ thống để mở lại, "
                             "chuột phải → Thoát để tắt hẳn.", APP_TITLE)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_unmap(self, event: tk.Event) -> None:
        # Minimize button: go to the tray instead of the taskbar.
        if event.widget is self.root and self.root.state() == "iconic":
            self.hide_to_tray()

    def _render_state(self, text: str, color: str) -> None:
        self.tray.title = f"{APP_TITLE}: {text}"
        self.lbl_state.configure(text=text, fg=color)
        self.btn_start.state(["!disabled"] if self.state is State.IDLE else ["disabled"])
        self.btn_steam.state(["!disabled"] if self.state is State.ON else ["disabled"])
        self.btn_stop.state(["!disabled"] if self.state in (State.STARTING, State.ON, State.PAUSED) else ["disabled"])

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
        # Flag first, then look: Roblox Shield does the same in reverse, so at least one side always sees
        # the other and the two TUNs are never up together.
        self.tunnel_flag.acquire()
        if self._other_tunnel_up():
            self.tunnel_flag.release()
            self._pause()
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
        self.yielding = False
        if self.state is State.STARTING:
            self.cancel_start.set()
        elif self.state is State.ON:
            self._shutdown("Đã tắt.", COLOR_IDLE)
        elif self.state is State.PAUSED:
            self.state = State.IDLE
            self._render_state("Đã tắt.", COLOR_IDLE)
            self.log("Đã tắt. Sẽ không tự bật lại khi Roblox Shield tắt.")

    def _other_tunnel_up(self) -> bool:
        """
        Roblox Shield's flag, or any sing-box that is not ours. The flag alone is not enough: a sing-box that
        outlives its owner (crash before it was bound to a job) keeps its strict-route TUN up, and starting
        ours next to it cuts the whole machine off the network.
        """
        if mutex_exists(SHIELD_TUNNEL_MUTEX):
            return True
        return bool(set(find_processes(SINGBOX_NAMES)) - {self.singbox.pid})

    def _pause(self) -> None:
        if self.state is State.PAUSED:
            return
        self.yielding = False
        self.state = State.PAUSED
        self._render_state("Tạm nghỉ: đang có tunnel khác (Roblox Shield)", COLOR_IDLE)
        self.log("Roblox Shield (hoặc một sing-box khác) đang chạy và tự lo Steam, app này tạm nghỉ để tránh "
                 "xung đột. Khi nó tắt, app sẽ tự bật lại.")

    def _yield_to_shield(self) -> None:
        """Roblox Shield is connecting: take our TUN down; it waits for our flag to disappear."""
        self.yielding = True
        if self.state is State.STARTING:
            self.cancel_start.set()
        elif self.state is State.ON:
            self._shutdown("", COLOR_IDLE)

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
        if self.show_event.is_set():
            self.show_window()
        if self.tick_count % WATCHDOG_EVERY_TICKS == 0:
            self._watchdog()
        self.root.after(UI_TICK_MS, self._tick)

    def _watchdog(self) -> None:
        other_up = self._other_tunnel_up()
        if other_up and self.state in (State.STARTING, State.ON) and not self.yielding:
            self._yield_to_shield()
        elif not other_up and self.state is State.PAUSED:
            self.state = State.IDLE
            self.log("Roblox Shield đã tắt, bật lại mở khoá Steam.")
            self.start()
        elif self.state is State.ON and not self.singbox.is_alive():
            tail = self.singbox.log_tail()
            self.log(f"sing-box dừng đột ngột.\n{tail}" if tail else "sing-box dừng đột ngột.")
            self._shutdown("Bị tắt do lỗi — bấm Bật để thử lại.", COLOR_ERR)
            self.tray.notify("Mở khoá Steam bị tắt do lỗi. Mở cửa sổ để bật lại.", APP_TITLE)

    def _on_tunnel_down(self, reason: str, color: str) -> None:
        """sing-box is stopped and DNS flushed: drop our flag so Roblox Shield may start its TUN."""
        self.tunnel_flag.release()
        if self.yielding:
            self._pause()
            return
        self.state = State.IDLE
        self._render_state(reason, color)
        self.log(reason)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self.log(event[1])
        elif kind == "tray":
            {"show": self.show_window, "steam": self.launch_steam, "quit": self._quit}[event[1]]()
        elif kind == "started":
            self.state = State.ON
            self._render_state("Đã mở khoá Steam ✓", COLOR_OK)
            self.log(f"Steam phản hồi (HTTP {event[1]}). Cứ để app chạy trong lúc dùng Steam.")
        elif kind == "start_failed":
            if not self.yielding:
                self.log(event[1])
            self._on_tunnel_down("Chưa bật", COLOR_ERR)
        elif kind == "stopped":
            self._on_tunnel_down(event[1], event[2])

    def _quit(self) -> None:
        self.cancel_start.set()
        # Hide first: stopping sing-box can take a few seconds and the window must not look frozen.
        self.root.withdraw()
        self.tray.stop()
        self.root.update_idletasks()
        self.singbox.stop()
        flush_dns_cache()
        self.tunnel_flag.release()
        self.root.destroy()


def main() -> None:
    enable_dpi_awareness()
    if not is_admin():
        if not relaunch_as_admin():
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, "Cần quyền Administrator để tạo card mạng ảo (TUN).")
        return
    instance = NamedMutex(STEAM_APP_MUTEX)
    if not instance.acquire():
        signal_event(STEAM_SHOW_EVENT)
        return
    root = tk.Tk()
    app = App(root)
    if autostart.AUTOSTART_ARG in sys.argv and app.starts_hidden():
        root.withdraw()
    root.mainloop()


if __name__ == "__main__":
    main()

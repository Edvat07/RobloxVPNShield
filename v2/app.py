"""
Roblox Shield v2 — routes ONLY Roblox through a WireGuard (Proton VPN) tunnel, everything else stays on the local ISP.

Leak guarantees:
  * Roblox traffic has no fallback route: tunnel down => Roblox offline, never Vietnam IP.
  * While protection is off/connecting, any Roblox process is terminated immediately.
  * sing-box and Roblox are bound to a kill-on-close Job Object: if this app exits or crashes, both die with it.
  * Exit country is verified at connect and re-checked every minute.
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
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tunnel import (
    HOME_COUNTRY, ROBLOX_PROCESSES, ProfileError, SingBox, build_singbox_config, host_has_ipv6, parse_wireguard_conf,
    probe_exit_location,
)
from winapi import KillOnCloseJob, enable_dpi_awareness, find_processes, is_admin, relaunch_as_admin, terminate_process

APP_TITLE = "Roblox Shield"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CORE_DIR = BASE_DIR / "core"
SETTINGS_PATH = BASE_DIR / "settings.json"
PROFILE_COPY = BASE_DIR / "wireguard.conf"
PROFILE_SEARCH_DIRS = (BASE_DIR, Path.home() / "Downloads")

ROBLOX_NAMES = frozenset(name.lower() for name in ROBLOX_PROCESSES)
UI_TICK_MS = 250
WATCHDOG_EVERY_TICKS = 4            # 1 s
CONNECT_TIMEOUT_S = 25
GEO_RECHECK_S = 60
GEO_RETRY_S = 5
GEO_FAIL_LIMIT = 3
LOG_MAX_LINES = 500

COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_IDLE = "#1a7f37", "#9a6700", "#cf222e", "#57606a"


class State(enum.Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    PROTECTED = "protected"
    STOPPING = "stopping"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


def discover_profile() -> Path | None:
    """Newest valid WireGuard .conf next to the app or in Downloads."""
    candidates = [p for d in PROFILE_SEARCH_DIRS if d.is_dir() for p in d.glob("*.conf")]
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            parse_wireguard_conf(path)
            return path
        except (OSError, ProfileError):
            continue
    return None


def find_roblox_player() -> Path | None:
    versions = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions"
    candidates = list(versions.glob("*/RobloxPlayerBeta.exe")) if versions.is_dir() else []
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.job = KillOnCloseJob()
        self.singbox = SingBox(CORE_DIR)
        self.events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self.settings = load_settings()

        self.state = State.IDLE
        self.cancel_connect = threading.Event()
        self.launch_after_connect = False
        self.jobbed_pids: set[int] = set()
        self.unkillable_pids: set[int] = set()
        self.geo_inflight = False
        self.geo_failures = 0
        self.next_geo_at = 0.0
        self.tick_count = 0

        self._build_ui()
        self._render_state("Chưa bật bảo vệ", COLOR_IDLE)
        self._ensure_profile()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(UI_TICK_MS, self._tick)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.minsize(520, 420)
        icon = BASE_DIR / "app_icon.ico"
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))

        pad = {"padx": 12, "pady": 6}
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        profile_row = ttk.Frame(main)
        profile_row.pack(fill="x", **pad)
        ttk.Label(profile_row, text="Cấu hình WireGuard:").pack(side="left")
        self.var_profile = tk.StringVar(value=self._profile_label())
        ttk.Label(profile_row, textvariable=self.var_profile, foreground=COLOR_IDLE).pack(side="left", padx=8)
        self.btn_profile = ttk.Button(profile_row, text="Chọn file .conf…", command=self._choose_profile)
        self.btn_profile.pack(side="right")

        self.lbl_state = tk.Label(main, font=("Segoe UI", 15, "bold"), anchor="w")
        self.lbl_state.pack(fill="x", **pad)
        self.var_exit = tk.StringVar(value="IP ra của Roblox: —")
        ttk.Label(main, textvariable=self.var_exit).pack(fill="x", padx=12)

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", **pad)
        self.btn_connect = ttk.Button(buttons, text="Bật bảo vệ & vào game", command=lambda: self.connect(True))
        self.btn_connect.pack(side="left", fill="x", expand=True)
        self.btn_launch = ttk.Button(buttons, text="Mở Roblox", command=self.launch_roblox)
        self.btn_launch.pack(side="left", fill="x", expand=True, padx=8)
        self.btn_stop = ttk.Button(buttons, text="Tắt bảo vệ", command=self.stop)
        self.btn_stop.pack(side="left", fill="x", expand=True)

        log_frame = ttk.LabelFrame(main, text="Nhật ký")
        log_frame.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(log_frame, height=10, state="disabled", wrap="word", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True)

    def _profile_label(self) -> str:
        if not self.settings.get("wireguard_conf"):
            return "(chưa chọn)"
        return self.settings.get("wireguard_source") or Path(self.settings["wireguard_conf"]).name

    def _import_profile(self, source: Path):
        """Validate and keep a private copy next to the app, so cleaning Downloads never breaks it."""
        profile = parse_wireguard_conf(source)
        if source.resolve() != PROFILE_COPY.resolve():
            PROFILE_COPY.write_text(source.read_text(encoding="utf-8-sig"), encoding="utf-8")
            self.settings["wireguard_source"] = source.name
        self.settings["wireguard_conf"] = str(PROFILE_COPY)
        save_settings(self.settings)
        self.var_profile.set(self._profile_label())
        return profile

    def _ensure_profile(self) -> None:
        current = Path(self.settings.get("wireguard_conf") or PROFILE_COPY)
        source = current if current.is_file() else discover_profile()
        if not source:
            self.log("Chưa có cấu hình WireGuard. Tải file .conf từ account.protonvpn.com (Downloads → "
                     "WireGuard configuration) vào thư mục Downloads rồi mở lại app, hoặc bấm 'Chọn file .conf…'.")
            return
        try:
            profile = self._import_profile(source)
        except (OSError, ProfileError) as exc:
            self.log(f"Cấu hình {source.name} không dùng được: {exc}")
            return
        if source != current:
            self.log(f"Đã tự động dùng cấu hình {source.name} → máy chủ {profile.endpoint_host}.")

    def _render_state(self, text: str, color: str) -> None:
        self.lbl_state.configure(text=text, fg=color)
        idle, protected = self.state is State.IDLE, self.state is State.PROTECTED
        self.btn_connect.state(["!disabled"] if idle else ["disabled"])
        self.btn_profile.state(["!disabled"] if idle else ["disabled"])
        self.btn_launch.state(["!disabled"] if protected else ["disabled"])
        self.btn_stop.state(["!disabled"] if self.state in (State.CONNECTING, State.PROTECTED) else ["disabled"])

    def log(self, message: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        overflow = int(self.txt_log.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if overflow > 0:
            self.txt_log.delete("1.0", f"{overflow + 1}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _choose_profile(self) -> None:
        path = filedialog.askopenfilename(
            title="Chọn file WireGuard (.conf) tải từ Proton VPN",
            filetypes=[("WireGuard config", "*.conf"), ("Tất cả", "*.*")],
        )
        if not path:
            return
        try:
            profile = self._import_profile(Path(path))
        except (OSError, ProfileError) as exc:
            messagebox.showerror(APP_TITLE, f"File cấu hình không hợp lệ:\n{exc}")
            return
        self.log(f"Đã chọn cấu hình: {Path(path).name} → máy chủ {profile.endpoint_host}:{profile.endpoint_port}")

    # ── Actions ───────────────────────────────────────────────────────────────
    def connect(self, launch_game: bool) -> None:
        if self.state is not State.IDLE:
            return
        path = self.settings.get("wireguard_conf", "")
        if not path:
            messagebox.showwarning(APP_TITLE, "Hãy chọn file WireGuard (.conf) trước.")
            return
        try:
            profile = parse_wireguard_conf(path)
        except (OSError, ProfileError) as exc:
            messagebox.showerror(APP_TITLE, f"Không đọc được cấu hình:\n{exc}")
            return

        self.state = State.CONNECTING
        self.launch_after_connect = launch_game
        self.cancel_connect.clear()
        self._render_state("Đang kết nối VPN…", COLOR_WARN)
        self.log(f"Khởi động đường hầm tới {profile.endpoint_host}…")
        threading.Thread(target=self._connect_worker, args=(profile,), daemon=True).start()

    def _connect_worker(self, profile) -> None:
        # Another full-tunnel VPN (e.g. the Proton VPN app) fights our TUN for routes and its kill switch
        # can block sing-box. If traffic already leaves outside Vietnam before we start, one is active.
        try:
            ip, country = probe_exit_location()
            if country not in (HOME_COUNTRY, "?"):
                self.events.put(("connect_failed",
                                 f"Một VPN khác đang bật (IP hiện tại {ip}, {country}). "
                                 "Hãy bấm Disconnect trong Proton VPN rồi thử lại."))
                return
        except OSError:
            pass  # no internet yet: the tunnel check below reports the real problem

        try:
            pid = self.singbox.start(build_singbox_config(profile, self.singbox.log_path, ipv6=host_has_ipv6()))
            if not self.job.assign(pid):
                self.events.put(("log", "Cảnh báo: không gắn được sing-box vào Job Object."))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self.singbox.stop()
            self.events.put(("connect_failed", f"Không khởi động được sing-box: {exc}"))
            return

        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last_issue = "chưa có phản hồi"
        while time.monotonic() < deadline and not self.cancel_connect.is_set():
            if not self.singbox.is_alive():
                last_issue = f"sing-box đã thoát.\n{self.singbox.log_tail()}"
                break
            try:
                ip, country = probe_exit_location()
                if country not in (HOME_COUNTRY, "?"):
                    self.events.put(("connected", ip, country))
                    return
                last_issue = f"IP ra vẫn là {country} ({ip})"
            except OSError as exc:
                last_issue = str(exc)
            self.cancel_connect.wait(1.0)

        self.singbox.stop()
        reason = "Đã hủy." if self.cancel_connect.is_set() else f"Kết nối thất bại: {last_issue}"
        self.events.put(("connect_failed", reason))

    def stop(self) -> None:
        if self.state is State.CONNECTING:
            self.cancel_connect.set()
        elif self.state is State.PROTECTED:
            self._shutdown("Đã tắt bảo vệ.", COLOR_IDLE)

    def _shutdown(self, reason: str, color: str) -> None:
        """Kill Roblox first (no leak window), then stop the tunnel off the UI thread."""
        self.state = State.STOPPING
        killed = self._kill_roblox()
        if killed:
            self.log(f"Đã đóng {killed} tiến trình Roblox.")
        self._render_state("Đang tắt…", COLOR_WARN)

        def worker() -> None:
            self.singbox.stop()
            self.events.put(("stopped", reason, color))

        threading.Thread(target=worker, daemon=True).start()

    def launch_roblox(self) -> None:
        if self.state is not State.PROTECTED:
            return
        exe = find_roblox_player()
        if not exe:
            self.log("Không tìm thấy RobloxPlayerBeta.exe. Hãy bấm Play trên roblox.com (trình duyệt vẫn được định tuyến qua VPN).")
            return
        # explorer.exe starts Roblox with the normal (non-admin) user token.
        subprocess.Popen(["explorer.exe", str(exe)], close_fds=True)
        self.log("Đang mở Roblox…")

    def _kill_roblox(self) -> int:
        return sum(terminate_process(pid) for pid in find_processes(ROBLOX_NAMES))

    # ── Event loop / watchdog ─────────────────────────────────────────────────
    def _tick(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass

        self.tick_count += 1
        if self.tick_count % WATCHDOG_EVERY_TICKS == 0:
            self._watchdog()
        self.root.after(UI_TICK_MS, self._tick)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self.log(event[1])
        elif kind == "connected":
            _, ip, country = event
            self.state = State.PROTECTED
            self.geo_failures = 0
            self.next_geo_at = time.monotonic() + GEO_RECHECK_S
            self.var_exit.set(f"IP ra của Roblox: {ip} ({country})")
            self._render_state(f"Đang bảo vệ — Roblox đi qua {country}", COLOR_OK)
            self.log(f"Đường hầm hoạt động. IP ra: {ip} ({country}). Mạng các app khác giữ nguyên.")
            if self.launch_after_connect:
                self.launch_roblox()
        elif kind == "connect_failed":
            self.state = State.IDLE
            self.var_exit.set("IP ra của Roblox: —")
            self._render_state("Chưa bật bảo vệ", COLOR_ERR)
            self.log(event[1])
        elif kind == "stopped":
            _, reason, color = event
            self.state = State.IDLE
            self.jobbed_pids.clear()
            self.var_exit.set("IP ra của Roblox: —")
            self._render_state(reason, color)
            self.log(reason)
        elif kind == "geo":
            self._on_geo_result(event[1], event[2])
        elif kind == "geo_failed":
            self._on_geo_failure(event[1])

    def _watchdog(self) -> None:
        pids = find_processes(ROBLOX_NAMES)

        if self.state is not State.PROTECTED:
            if pids:
                survivors = [pid for pid in pids if not terminate_process(pid)]
                if len(survivors) < len(pids):
                    self.log("Đã chặn Roblox vì bảo vệ chưa bật (tránh bị nhận IP Việt Nam).")
                new_survivors = set(survivors) - self.unkillable_pids
                if new_survivors:
                    self.log(f"CẢNH BÁO: không đóng được Roblox (PID {sorted(new_survivors)}). Hãy tự tắt game.")
                self.unkillable_pids = set(survivors)
            return

        if not self.singbox.is_alive():
            tail = self.singbox.log_tail()
            self.log(f"sing-box dừng đột ngột.\n{tail}" if tail else "sing-box dừng đột ngột.")
            self._shutdown("Mất đường hầm — đã đóng Roblox.", COLOR_ERR)
            return

        current = set(pids)
        for pid in current - self.jobbed_pids:
            if not self.job.assign(pid):
                self.log(f"Cảnh báo: không gắn được Roblox (PID {pid}) vào Job Object.")
        self.jobbed_pids = current

        if not self.geo_inflight and time.monotonic() >= self.next_geo_at:
            self.geo_inflight = True
            threading.Thread(target=self._geo_worker, daemon=True).start()

    def _geo_worker(self) -> None:
        try:
            ip, country = probe_exit_location()
            self.events.put(("geo", ip, country))
        except OSError as exc:
            self.events.put(("geo_failed", str(exc)))

    def _on_geo_result(self, ip: str, country: str) -> None:
        self.geo_inflight = False
        if self.state is not State.PROTECTED:
            return
        if country == HOME_COUNTRY:
            self.log(f"Phát hiện IP ra là {country} ({ip})!")
            self._shutdown("IP ra bị về Việt Nam — đã đóng Roblox.", COLOR_ERR)
            return
        self.geo_failures = 0
        self.next_geo_at = time.monotonic() + GEO_RECHECK_S
        self.var_exit.set(f"IP ra của Roblox: {ip} ({country})")

    def _on_geo_failure(self, error: str) -> None:
        self.geo_inflight = False
        if self.state is not State.PROTECTED:
            return
        self.geo_failures += 1
        self.log(f"Kiểm tra IP thất bại ({self.geo_failures}/{GEO_FAIL_LIMIT}): {error}")
        if self.geo_failures >= GEO_FAIL_LIMIT:
            self._shutdown("VPN không phản hồi — đã đóng Roblox.", COLOR_ERR)
        else:
            self.next_geo_at = time.monotonic() + GEO_RETRY_S

    def _on_close(self) -> None:
        if find_processes(ROBLOX_NAMES) and not messagebox.askyesno(APP_TITLE, "Đóng app sẽ tắt luôn Roblox. Tiếp tục?"):
            return
        self.cancel_connect.set()
        self._kill_roblox()
        # Hide first: stopping sing-box can take a few seconds and the window must not look frozen.
        self.root.withdraw()
        self.root.update_idletasks()
        self.singbox.stop()
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

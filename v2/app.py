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
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tunnel import (
    HOME_COUNTRY, ROBLOX_PROCESSES, ProfileError, SingBox, WireGuardProfile, build_singbox_config, host_has_ipv6,
    parse_wireguard_conf, probe_exit_location,
)
import hostsblock
import i18n
from winapi import (
    SHIELD_APP_MUTEX, SHIELD_CONNECT_EVENT, SHIELD_PROTECTED_MUTEX, SHIELD_SHOW_EVENT, SHIELD_TUNNEL_MUTEX,
    STEAM_TUNNEL_MUTEX, KillOnCloseJob, NamedEvent, NamedMutex, enable_dpi_awareness, find_processes, icmp_ping_ms,
    is_admin, mutex_exists, relaunch_as_admin, signal_event, terminate_process,
)

APP_TITLE = "Roblox Shield"
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
CORE_DIR = BASE_DIR / "core"
SETTINGS_PATH = BASE_DIR / "settings.json"
PROFILES_DIR = BASE_DIR / "profiles"
LEGACY_PROFILE = BASE_DIR / "wireguard.conf"       # single-profile copy kept by older builds
DOWNLOADS_DIR = Path.home() / "Downloads"
# Proton WireGuard certificates expire one year after the .conf is generated (= downloaded).
PROFILE_LIFETIME_DAYS = 365
PROFILE_WARN_DAYS = 30

ROBLOX_NAMES = frozenset(name.lower() for name in ROBLOX_PROCESSES)
UI_TICK_MS = 250
WATCHDOG_EVERY_TICKS = 4            # 1 s
CONNECT_TIMEOUT_S = 25
STEAM_YIELD_TIMEOUT_S = 15
GEO_RECHECK_S = 60
GEO_RETRY_S = 5
GEO_FAIL_LIMIT = 3
LOG_MAX_LINES = 500

COLOR_OK, COLOR_WARN, COLOR_ERR, COLOR_IDLE = "#1a7f37", "#9a6700", "#cf222e", "#57606a"

tr = i18n.Strings({
    "profile_label": {"vi": "Cấu hình WireGuard:", "en": "WireGuard config:"},
    "add_conf": {"vi": "Thêm file .conf…", "en": "Add .conf file…"},
    "exit_ip": {"vi": "IP ra của Roblox: {ip}", "en": "Roblox exit IP: {ip}"},
    "btn_connect": {"vi": "Bật bảo vệ & vào game", "en": "Protect & play"},
    "btn_roblox": {"vi": "Mở Roblox", "en": "Open Roblox"},
    "btn_studio": {"vi": "Mở Studio", "en": "Open Studio"},
    "btn_stop": {"vi": "Tắt bảo vệ", "en": "Stop protection"},
    "language": {"vi": "Ngôn ngữ:", "en": "Language:"},
    "log": {"vi": "Nhật ký", "en": "Log"},
    "server_added": {"vi": "Đã thêm máy chủ {name}.", "en": "Added server {name}."},
    "server_exists": {"vi": "Máy chủ này đã có sẵn ({name}).", "en": "This server is already added ({name})."},
    "profile_expired": {"vi": "Cấu hình {name} đã hết hạn và bị gỡ. Hãy tải file mới từ account.protonvpn.com.",
                        "en": "Config {name} expired and was removed. Download a new one from account.protonvpn.com."},
    "profile_expiring": {
        "vi": "Cấu hình {name} còn {days} ngày nữa hết hạn — nên tải file mới từ account.protonvpn.com rồi bỏ vào "
              "Downloads.",
        "en": "Config {name} expires in {days} days — download a new one from account.protonvpn.com and put it in "
              "Downloads.",
    },
    "servers_auto": {"vi": "{count} máy chủ — tự chọn nhanh nhất", "en": "{count} servers — fastest picked automatically"},
    "no_profile": {"vi": "(chưa có)", "en": "(none yet)"},
    "need_profile": {
        "vi": "Chưa có cấu hình WireGuard. Tải file .conf từ account.protonvpn.com (Downloads → WireGuard "
              "configuration) vào thư mục Downloads, hoặc bấm 'Thêm file .conf…'. Có thể thêm nhiều máy chủ, app sẽ "
              "tự dùng máy chủ ping thấp nhất.",
        "en": "No WireGuard config yet. Download a .conf file from account.protonvpn.com (Downloads → WireGuard "
              "configuration) into your Downloads folder, or press 'Add .conf file…'. You can add several servers; "
              "the app uses the one with the lowest ping.",
    },
    "choose_title": {"vi": "Chọn file WireGuard (.conf) tải từ Proton VPN",
                     "en": "Choose a WireGuard (.conf) file downloaded from Proton VPN"},
    "all_files": {"vi": "Tất cả", "en": "All files"},
    "invalid_conf": {"vi": "File cấu hình không hợp lệ:\n{error}", "en": "Invalid config file:\n{error}"},
    "add_conf_first": {"vi": "Hãy thêm file WireGuard (.conf) tải từ Proton VPN trước.",
                       "en": "Add a WireGuard (.conf) file downloaded from Proton VPN first."},
    "state_off": {"vi": "Chưa bật bảo vệ", "en": "Not protected"},
    "state_connecting": {"vi": "Đang kết nối VPN…", "en": "Connecting VPN…"},
    "state_stopping": {"vi": "Đang tắt…", "en": "Stopping…"},
    "state_stopped": {"vi": "Đã tắt bảo vệ.", "en": "Protection stopped."},
    "state_protected": {"vi": "Đang bảo vệ — Roblox đi qua {country}", "en": "Protected — Roblox goes through {country}"},
    "state_tunnel_lost": {"vi": "Mất đường hầm — đã đóng Roblox.", "en": "Tunnel lost — Roblox was closed."},
    "state_ip_home": {"vi": "IP ra bị về Việt Nam — đã đóng Roblox.", "en": "Exit IP fell back to Vietnam — Roblox was closed."},
    "state_vpn_silent": {"vi": "VPN không phản hồi — đã đóng Roblox.", "en": "VPN not responding — Roblox was closed."},
    "pinging": {"vi": "Đang đo ping tới {count} máy chủ…", "en": "Measuring ping to {count} servers…"},
    "no_reply": {"vi": "không phản hồi", "en": "no reply"},
    "none_pingable": {"vi": "Không ping được máy chủ nào, thử dùng {name}.", "en": "No server answered ping, trying {name}."},
    "starting_tunnel": {"vi": "Khởi động đường hầm tới {name} ({host})…", "en": "Starting the tunnel to {name} ({host})…"},
    "cancelled": {"vi": "Đã hủy.", "en": "Cancelled."},
    "other_vpn": {
        "vi": "Một VPN khác đang bật (IP hiện tại {ip}, {country}). Hãy bấm Disconnect trong Proton VPN rồi thử lại.",
        "en": "Another VPN is on (current IP {ip}, {country}). Press Disconnect in Proton VPN and try again.",
    },
    "waiting_steam": {"vi": "Đang đợi Steam Unblock tạm nghỉ…", "en": "Waiting for Steam Unblock to pause…"},
    "steam_wont_stop": {"vi": "Steam Unblock không chịu tắt. Hãy thoát Steam Unblock rồi thử lại.",
                        "en": "Steam Unblock did not stop. Quit Steam Unblock and try again."},
    "warn_job": {"vi": "Cảnh báo: không gắn được sing-box vào Job Object.",
                 "en": "Warning: could not bind sing-box to the Job Object."},
    "singbox_start_failed": {"vi": "Không khởi động được sing-box: {error}", "en": "Could not start sing-box: {error}"},
    "no_response": {"vi": "chưa có phản hồi", "en": "no response yet"},
    "singbox_exited": {"vi": "sing-box đã thoát.", "en": "sing-box exited."},
    "still_home": {"vi": "IP ra vẫn là {country} ({ip})", "en": "Exit IP is still {country} ({ip})"},
    "connect_failed": {"vi": "Kết nối thất bại: {error}", "en": "Connection failed: {error}"},
    "killed_roblox": {"vi": "Đã đóng {count} tiến trình Roblox.", "en": "Closed {count} Roblox processes."},
    "hint_play": {"vi": "Hãy bấm Play trên roblox.com (trình duyệt vẫn được định tuyến qua VPN).",
                  "en": "Press Play on roblox.com (the browser also goes through the VPN)."},
    "hint_studio": {"vi": "Hãy cài Roblox Studio từ create.roblox.com.", "en": "Install Roblox Studio from create.roblox.com."},
    "exe_not_found": {"vi": "Không tìm thấy {exe}. {hint}", "en": "{exe} not found. {hint}"},
    "opening": {"vi": "Đang mở {label}…", "en": "Opening {label}…"},
    "auto_connect": {"vi": "Roblox được mở khi chưa bật bảo vệ — đang tự bật. Xong thì bấm Play / tải lại trang.",
                     "en": "Roblox was opened without protection — turning it on. Then press Play / reload the page."},
    "using": {"vi": "Đang dùng {name}", "en": "Using {name}"},
    "hosts_warn": {"vi": "Cảnh báo: không gỡ được chặn web Roblox trong file hosts: {error}",
                   "en": "Warning: could not remove the Roblox website block from the hosts file: {error}"},
    "tunnel_up": {"vi": "Đường hầm hoạt động. IP ra: {ip} ({country}). Mạng các app khác giữ nguyên.",
                  "en": "Tunnel is up. Exit IP: {ip} ({country}). Other apps keep their normal connection."},
    "blocked_roblox": {"vi": "Đã chặn Roblox vì bảo vệ chưa bật (tránh bị nhận IP Việt Nam).",
                       "en": "Blocked Roblox because protection is off (so it never sees the Vietnamese IP)."},
    "cannot_kill": {"vi": "CẢNH BÁO: không đóng được Roblox (PID {pids}). Hãy tự tắt game.",
                    "en": "WARNING: could not close Roblox (PID {pids}). Close the game yourself."},
    "singbox_died": {"vi": "sing-box dừng đột ngột.", "en": "sing-box stopped unexpectedly."},
    "warn_job_roblox": {"vi": "Cảnh báo: không gắn được Roblox (PID {pid}) vào Job Object.",
                        "en": "Warning: could not bind Roblox (PID {pid}) to the Job Object."},
    "ip_home": {"vi": "Phát hiện IP ra là {country} ({ip})!", "en": "Exit IP detected as {country} ({ip})!"},
    "geo_failed": {"vi": "Kiểm tra IP thất bại ({count}/{limit}): {error}", "en": "IP check failed ({count}/{limit}): {error}"},
    "close_kills_roblox": {"vi": "Đóng app sẽ tắt luôn Roblox. Tiếp tục?", "en": "Closing the app also closes Roblox. Continue?"},
    "restart_question": {"vi": "Mở lại app ngay để đổi ngôn ngữ?\n(Nếu đang bảo vệ, Roblox sẽ bị đóng.)",
                         "en": "Restart the app now to change the language?\n(If protection is on, Roblox will be closed.)"},
    "language_later": {"vi": "Ngôn ngữ mới sẽ dùng từ lần mở app sau.",
                       "en": "The new language applies the next time the app starts."},
    "need_admin": {"vi": "Cần quyền Administrator để tạo card mạng ảo (TUN).",
                   "en": "Administrator rights are needed to create the virtual network adapter (TUN)."},
})


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


Profiles = list[tuple[Path, WireGuardProfile]]


def _server_key(profile: WireGuardProfile) -> tuple:
    # The private key is part of it so a renewed .conf for the same server counts as new.
    return profile.private_key, profile.peer_public_key, profile.endpoint_host, profile.endpoint_port


def _import_dirs() -> list[Path]:
    """App folder, Downloads and its direct subfolders (e.g. Downloads\\VPN)."""
    subdirs = sorted(d for d in DOWNLOADS_DIR.iterdir() if d.is_dir()) if DOWNLOADS_DIR.is_dir() else []
    return [BASE_DIR, DOWNLOADS_DIR, *subdirs]


def profile_days_left(path: Path) -> int:
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return int(PROFILE_LIFETIME_DAYS - age_days)


def _valid_profiles(directory: Path) -> Profiles:
    result: Profiles = []
    for path in sorted(directory.glob("*.conf")) if directory.is_dir() else ():
        try:
            result.append((path, parse_wireguard_conf(path)))
        except (OSError, ProfileError):
            continue
    return result


def import_profile(source: Path, name: str | None = None) -> tuple[Path, bool]:
    """Copy a .conf into the app's store unless that server is already there. Returns (stored path, added)."""
    profile = parse_wireguard_conf(source)
    for path, stored in _valid_profiles(PROFILES_DIR):
        if _server_key(stored) == _server_key(profile):
            return path, False
    PROFILES_DIR.mkdir(exist_ok=True)
    target = PROFILES_DIR / (name or source.name)
    n = 2
    while target.exists():
        target = PROFILES_DIR / f"{Path(name or source.name).stem}-{n}.conf"
        n += 1
    target.write_text(source.read_text(encoding="utf-8-sig"), encoding="utf-8")
    # Keep the download time: it is what the expiry countdown is based on.
    stat = source.stat()
    os.utime(target, (stat.st_atime, stat.st_mtime))
    return target, True


def endpoint_ping_ms(profile: WireGuardProfile) -> int | None:
    try:
        ip = socket.getaddrinfo(profile.endpoint_host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None
    return icmp_ping_ms(ip)


def find_roblox_exe(exe_name: str) -> Path | None:
    """Newest install of a Roblox executable (Player and Studio live in separate version folders)."""
    versions = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions"
    candidates = list(versions.glob(f"*/{exe_name}")) if versions.is_dir() else []
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


class App:
    def __init__(self, root: tk.Tk, settings: dict, connect_now: bool = False) -> None:
        self.root = root
        self.job = KillOnCloseJob()
        self.singbox = SingBox(CORE_DIR)
        self.events: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self.settings = settings
        self.restart_requested = False

        self.state = State.IDLE
        self.cancel_connect = threading.Event()
        self.tunnel_flag = NamedMutex(SHIELD_TUNNEL_MUTEX)
        # Held only while the tunnel is verified up: Roblox Guard lets Roblox (game and website) through then.
        self.protected_flag = NamedMutex(SHIELD_PROTECTED_MUTEX)
        self.connect_request = NamedEvent(SHIELD_CONNECT_EVENT)
        self.show_request = NamedEvent(SHIELD_SHOW_EVENT)
        self.launch_after_connect = False
        self.jobbed_pids: set[int] = set()
        self.unkillable_pids: set[int] = set()
        self.geo_inflight = False
        self.geo_failures = 0
        self.next_geo_at = 0.0
        self.tick_count = 0

        self._build_ui()
        self._render_state(tr("state_off"), COLOR_IDLE)
        self._ensure_profile()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(UI_TICK_MS, self._tick)
        if connect_now:
            root.after(UI_TICK_MS, self._on_connect_request)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.minsize(600, 420)
        icon = BASE_DIR / "app_icon.ico"
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))

        pad = {"padx": 12, "pady": 6}
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        profile_row = ttk.Frame(main)
        profile_row.pack(fill="x", **pad)
        ttk.Label(profile_row, text=tr("profile_label")).pack(side="left")
        self.var_profile = tk.StringVar(value="—")
        ttk.Label(profile_row, textvariable=self.var_profile, foreground=COLOR_IDLE).pack(side="left", padx=8)
        self.btn_profile = ttk.Button(profile_row, text=tr("add_conf"), command=self._choose_profile)
        self.btn_profile.pack(side="right")

        self.lbl_state = tk.Label(main, font=("Segoe UI", 15, "bold"), anchor="w")
        self.lbl_state.pack(fill="x", **pad)
        self.var_exit = tk.StringVar(value=tr("exit_ip", ip="—"))
        ttk.Label(main, textvariable=self.var_exit).pack(fill="x", padx=12)

        buttons = ttk.Frame(main)
        buttons.pack(fill="x", **pad)
        self.btn_connect = ttk.Button(buttons, text=tr("btn_connect"), command=lambda: self.connect(True))
        self.btn_connect.pack(side="left", fill="x", expand=True)
        self.btn_launch = ttk.Button(buttons, text=tr("btn_roblox"), command=self.launch_roblox)
        self.btn_launch.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.btn_studio = ttk.Button(buttons, text=tr("btn_studio"), command=self.launch_studio)
        self.btn_studio.pack(side="left", fill="x", expand=True, padx=8)
        self.btn_stop = ttk.Button(buttons, text=tr("btn_stop"), command=self.stop)
        self.btn_stop.pack(side="left", fill="x", expand=True)

        language = ttk.Frame(main)
        language.pack(fill="x", padx=12)
        ttk.Label(language, text=tr("language")).pack(side="left")
        self.var_language = tk.StringVar(value=i18n.LANGUAGES[self.settings.get("language", "auto")])
        combo = ttk.Combobox(language, textvariable=self.var_language, values=list(i18n.LANGUAGES.values()),
                             state="readonly", width=18)
        combo.pack(side="left", padx=8)
        combo.bind("<<ComboboxSelected>>", self._on_language_changed)

        log_frame = ttk.LabelFrame(main, text=tr("log"))
        log_frame.pack(fill="both", expand=True, **pad)
        self.txt_log = tk.Text(log_frame, height=10, state="disabled", wrap="word", font=("Consolas", 9), relief="flat")
        scroll = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.txt_log.pack(side="left", fill="both", expand=True)

    def _sync_profiles(self) -> Profiles:
        """Pull new .conf files from the app folder and Downloads into the store; return the whole store."""
        if LEGACY_PROFILE.is_file():
            try:
                import_profile(LEGACY_PROFILE, self.settings.get("wireguard_source"))
                LEGACY_PROFILE.unlink()
                for key in ("wireguard_conf", "wireguard_source"):
                    self.settings.pop(key, None)
                save_settings(self.settings)
            except (OSError, ProfileError):
                pass
        for directory in _import_dirs():
            for path, _ in _valid_profiles(directory):
                try:
                    if profile_days_left(path) <= 0:
                        continue
                    stored, added = import_profile(path)
                except (OSError, ProfileError):
                    continue
                if added:
                    self.log(tr("server_added", name=stored.stem))

        profiles: Profiles = []
        for path, profile in _valid_profiles(PROFILES_DIR):
            days_left = profile_days_left(path)
            if days_left <= 0:
                path.unlink(missing_ok=True)
                self.log(tr("profile_expired", name=path.stem))
                continue
            if days_left <= PROFILE_WARN_DAYS:
                self.log(tr("profile_expiring", name=path.stem, days=days_left))
            profiles.append((path, profile))
        if len(profiles) > 1:
            self.var_profile.set(tr("servers_auto", count=len(profiles)))
        else:
            self.var_profile.set(profiles[0][0].stem if profiles else tr("no_profile"))
        return profiles

    def _ensure_profile(self) -> None:
        if not self._sync_profiles():
            self.log(tr("need_profile"))

    def _render_state(self, text: str, color: str) -> None:
        self.lbl_state.configure(text=text, fg=color)
        idle, protected = self.state is State.IDLE, self.state is State.PROTECTED
        self.btn_connect.state(["!disabled"] if idle else ["disabled"])
        self.btn_profile.state(["!disabled"] if idle else ["disabled"])
        self.btn_launch.state(["!disabled"] if protected else ["disabled"])
        self.btn_studio.state(["!disabled"] if protected else ["disabled"])
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
            title=tr("choose_title"),
            filetypes=[("WireGuard config", "*.conf"), (tr("all_files"), "*.*")],
        )
        if not path:
            return
        try:
            stored, added = import_profile(Path(path))
        except (OSError, ProfileError) as exc:
            messagebox.showerror(APP_TITLE, tr("invalid_conf", error=exc))
            return
        self.log(tr("server_added" if added else "server_exists", name=stored.stem))
        self._sync_profiles()

    # ── Actions ───────────────────────────────────────────────────────────────
    def connect(self, launch_game: bool) -> None:
        if self.state is not State.IDLE:
            return
        profiles = self._sync_profiles()
        if not profiles:
            messagebox.showwarning(APP_TITLE, tr("add_conf_first"))
            return

        self.state = State.CONNECTING
        self.launch_after_connect = launch_game
        self.cancel_connect.clear()
        # Tells Steam Unblock to take its TUN down; held until our tunnel is stopped (or this process dies).
        self.tunnel_flag.acquire()
        self._render_state(tr("state_connecting"), COLOR_WARN)
        self.log(tr("pinging", count=len(profiles)))
        threading.Thread(target=self._connect_worker, args=(profiles,), daemon=True).start()

    def _pick_fastest(self, profiles: Profiles) -> WireGuardProfile:
        """Lowest ICMP round-trip to the VPN server; Roblox then also matches us with game servers near it."""
        with ThreadPoolExecutor(max_workers=8) as pool:
            pings = list(pool.map(lambda item: endpoint_ping_ms(item[1]), profiles))
        for (path, _), ms in zip(profiles, pings):
            self.events.put(("log", f"  {path.stem}: {f'{ms} ms' if ms is not None else tr('no_reply')}"))

        reachable = [(ms, i) for i, ms in enumerate(pings) if ms is not None]
        if reachable:
            ms, best = min(reachable)
            path, profile = profiles[best]
            self.events.put(("profile_chosen", f"{path.stem} ({ms} ms)"))
        else:
            path, profile = profiles[0]
            self.events.put(("profile_chosen", path.stem))
            self.events.put(("log", tr("none_pingable", name=path.stem)))
        self.events.put(("log", tr("starting_tunnel", name=path.stem, host=profile.endpoint_host)))
        return profile

    def _connect_worker(self, profiles: Profiles) -> None:
        profile = self._pick_fastest(profiles)
        if self.cancel_connect.is_set():
            self.events.put(("connect_failed", tr("cancelled")))
            return

        # Another full-tunnel VPN (e.g. the Proton VPN app) fights our TUN for routes and its kill switch
        # can block sing-box. If traffic already leaves outside Vietnam before we start, one is active.
        try:
            ip, country = probe_exit_location()
            if country not in (HOME_COUNTRY, "?"):
                self.events.put(("connect_failed", tr("other_vpn", ip=ip, country=country)))
                return
        except OSError:
            pass  # no internet yet: the tunnel check below reports the real problem

        # Two strict-route TUNs block each other's DNS. Our tunnel unblocks Steam too, so Steam Unblock
        # steps aside and resumes on its own once we stop.
        if mutex_exists(STEAM_TUNNEL_MUTEX):
            self.events.put(("log", tr("waiting_steam")))
            deadline = time.monotonic() + STEAM_YIELD_TIMEOUT_S
            while mutex_exists(STEAM_TUNNEL_MUTEX):
                if self.cancel_connect.is_set():
                    self.events.put(("connect_failed", tr("cancelled")))
                    return
                if time.monotonic() >= deadline:
                    self.events.put(("connect_failed", tr("steam_wont_stop")))
                    return
                self.cancel_connect.wait(0.25)

        try:
            pid = self.singbox.start(build_singbox_config(profile, self.singbox.log_path, ipv6=host_has_ipv6()))
            if not self.job.assign(pid):
                self.events.put(("log", tr("warn_job")))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self.singbox.stop()
            self.events.put(("connect_failed", tr("singbox_start_failed", error=exc)))
            return

        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last_issue = tr("no_response")
        while time.monotonic() < deadline and not self.cancel_connect.is_set():
            if not self.singbox.is_alive():
                last_issue = f"{tr('singbox_exited')}\n{self.singbox.log_tail()}"
                break
            try:
                ip, country = probe_exit_location()
                if country not in (HOME_COUNTRY, "?"):
                    self.events.put(("connected", ip, country))
                    return
                last_issue = tr("still_home", country=country, ip=ip)
            except OSError as exc:
                last_issue = str(exc)
            self.cancel_connect.wait(1.0)

        self.singbox.stop()
        reason = tr("cancelled") if self.cancel_connect.is_set() else tr("connect_failed", error=last_issue)
        self.events.put(("connect_failed", reason))

    def stop(self) -> None:
        if self.state is State.CONNECTING:
            self.cancel_connect.set()
        elif self.state is State.PROTECTED:
            self._shutdown(tr("state_stopped"), COLOR_IDLE)

    def _shutdown(self, reason: str, color: str) -> None:
        """Kill Roblox first (no leak window), then stop the tunnel off the UI thread."""
        self.state = State.STOPPING
        self.protected_flag.release()   # Roblox Guard re-blocks the website right away
        killed = self._kill_roblox()
        if killed:
            self.log(tr("killed_roblox", count=killed))
        self._render_state(tr("state_stopping"), COLOR_WARN)

        def worker() -> None:
            self.singbox.stop()
            self.events.put(("stopped", reason, color))

        threading.Thread(target=worker, daemon=True).start()

    def launch_roblox(self) -> None:
        self._launch("RobloxPlayerBeta.exe", "Roblox", tr("hint_play"))

    def launch_studio(self) -> None:
        self._launch("RobloxStudioBeta.exe", "Roblox Studio", tr("hint_studio"))

    def _launch(self, exe_name: str, label: str, hint: str) -> None:
        if self.state is not State.PROTECTED:
            return
        exe = find_roblox_exe(exe_name)
        if not exe:
            self.log(tr("exe_not_found", exe=exe_name, hint=hint))
            return
        # explorer.exe starts it with the normal (non-admin) user token.
        subprocess.Popen(["explorer.exe", str(exe)], close_fds=True)
        self.log(tr("opening", label=label))

    def _kill_roblox(self) -> int:
        return sum(terminate_process(pid) for pid in find_processes(ROBLOX_NAMES))

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def _on_connect_request(self) -> None:
        """From Roblox Guard (Roblox was opened without protection) or a second launch with --connect."""
        self._show_window()
        if self.state is State.IDLE:
            self.log(tr("auto_connect"))
            self.connect(False)

    # ── Event loop / watchdog ─────────────────────────────────────────────────
    def _tick(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        if self.show_request.is_set():
            self._show_window()
        if self.connect_request.is_set():
            self._on_connect_request()

        self.tick_count += 1
        if self.tick_count % WATCHDOG_EVERY_TICKS == 0:
            self._watchdog()
        self.root.after(UI_TICK_MS, self._tick)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            self.log(event[1])
        elif kind == "profile_chosen":
            self.var_profile.set(tr("using", name=event[1]))
        elif kind == "connected":
            _, ip, country = event
            self.state = State.PROTECTED
            self.protected_flag.acquire()
            try:
                hostsblock.set_blocked(False)   # Roblox Guard does it too; this covers Guard not running
            except OSError as exc:
                self.log(tr("hosts_warn", error=exc))
            self.geo_failures = 0
            self.next_geo_at = time.monotonic() + GEO_RECHECK_S
            self.var_exit.set(tr("exit_ip", ip=f"{ip} ({country})"))
            self._render_state(tr("state_protected", country=country), COLOR_OK)
            self.log(tr("tunnel_up", ip=ip, country=country))
            if self.launch_after_connect:
                self.launch_roblox()
        elif kind == "connect_failed":
            self.tunnel_flag.release()
            self.state = State.IDLE
            self.var_exit.set(tr("exit_ip", ip="—"))
            self._render_state(tr("state_off"), COLOR_ERR)
            self.log(event[1])
        elif kind == "stopped":
            _, reason, color = event
            self.tunnel_flag.release()
            self.state = State.IDLE
            self.jobbed_pids.clear()
            self.var_exit.set(tr("exit_ip", ip="—"))
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
                    self.log(tr("blocked_roblox"))
                new_survivors = set(survivors) - self.unkillable_pids
                if new_survivors:
                    self.log(tr("cannot_kill", pids=sorted(new_survivors)))
                self.unkillable_pids = set(survivors)
            return

        if not self.singbox.is_alive():
            tail = self.singbox.log_tail()
            self.log(f"{tr('singbox_died')}\n{tail}" if tail else tr("singbox_died"))
            self._shutdown(tr("state_tunnel_lost"), COLOR_ERR)
            return

        current = set(pids)
        for pid in current - self.jobbed_pids:
            if not self.job.assign(pid):
                self.log(tr("warn_job_roblox", pid=pid))
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
            self.log(tr("ip_home", country=country, ip=ip))
            self._shutdown(tr("state_ip_home"), COLOR_ERR)
            return
        self.geo_failures = 0
        self.next_geo_at = time.monotonic() + GEO_RECHECK_S
        self.var_exit.set(tr("exit_ip", ip=f"{ip} ({country})"))

    def _on_geo_failure(self, error: str) -> None:
        self.geo_inflight = False
        if self.state is not State.PROTECTED:
            return
        self.geo_failures += 1
        self.log(tr("geo_failed", count=self.geo_failures, limit=GEO_FAIL_LIMIT, error=error))
        if self.geo_failures >= GEO_FAIL_LIMIT:
            self._shutdown(tr("state_vpn_silent"), COLOR_ERR)
        else:
            self.next_geo_at = time.monotonic() + GEO_RETRY_S

    def _on_language_changed(self, _event=None) -> None:
        choice = next(code for code, name in i18n.LANGUAGES.items() if name == self.var_language.get())
        if choice == self.settings.get("language", "auto"):
            return
        self.settings["language"] = choice
        save_settings(self.settings)
        # The question is asked in the language the app will switch to, so the user can read it.
        previous = i18n.current()
        i18n.set_language(choice)
        if messagebox.askyesno(APP_TITLE, tr("restart_question")):
            self.restart_requested = True
            self._close()
        else:
            i18n.set_language(previous)   # this window stays in its language until the next start
            self.log(tr("language_later"))

    def _on_close(self) -> None:
        if find_processes(ROBLOX_NAMES) and not messagebox.askyesno(APP_TITLE, tr("close_kills_roblox")):
            return
        self._close()

    def _close(self) -> None:
        self.cancel_connect.set()
        self._kill_roblox()
        # Hide first: stopping sing-box can take a few seconds and the window must not look frozen.
        self.root.withdraw()
        self.root.update_idletasks()
        self.singbox.stop()
        self.root.destroy()


def restart_command() -> list[str]:
    # The new copy inherits our elevation, so there is no UAC prompt.
    if FROZEN:
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]


def main() -> None:
    enable_dpi_awareness()
    settings = load_settings()
    i18n.set_language(settings.get("language"))
    if not is_admin():
        if not relaunch_as_admin():
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, tr("need_admin"))
        return
    connect_now = "--connect" in sys.argv
    instance = NamedMutex(SHIELD_APP_MUTEX)
    if not instance.acquire():
        # Already open: hand the request to that window instead of starting a second tunnel owner.
        signal_event(SHIELD_CONNECT_EVENT if connect_now else SHIELD_SHOW_EVENT)
        return
    root = tk.Tk()
    app = App(root, settings, connect_now=connect_now)
    root.mainloop()
    if app.restart_requested:
        instance.release()   # or the new copy would see us and just exit
        subprocess.Popen(restart_command(), cwd=BASE_DIR, close_fds=True)


if __name__ == "__main__":
    main()

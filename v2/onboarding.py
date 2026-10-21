"""Getting-started window for a first run with no WireGuard config: three steps, and the app picks up the .conf
from Downloads by itself the moment the browser finishes saving it."""

from __future__ import annotations

import tkinter as tk
import webbrowser
from tkinter import ttk
from typing import Callable

import i18n

PROTON_SIGNUP_URL = "https://account.protonvpn.com/signup"
PROTON_DOWNLOADS_URL = "https://account.protonvpn.com/downloads"
PROTON_HELP_URL = "https://protonvpn.com/support/wireguard-configurations"

COLOR_OK, COLOR_WAIT, COLOR_MUTED = "#1a7f37", "#9a6700", "#57606a"

tr = i18n.Strings({
    "title": {"vi": "Bắt đầu với Roblox Shield", "en": "Get started with Roblox Shield"},
    "intro": {
        "vi": "Roblox Shield cần ít nhất 1 file cấu hình WireGuard miễn phí của Proton VPN. Chỉ cần làm một lần:",
        "en": "Roblox Shield needs at least one free WireGuard config file from Proton VPN. You only do this once:",
    },
    "step1": {"vi": "1. Tạo tài khoản Proton VPN miễn phí", "en": "1. Create a free Proton VPN account"},
    "step1_text": {"vi": "Gói Free là đủ, không cần thẻ. Đã có tài khoản thì bỏ qua bước này.",
                   "en": "The Free plan is enough, no card needed. Skip this if you already have an account."},
    "signup": {"vi": "Tạo tài khoản", "en": "Create account"},
    "step2": {"vi": "2. Tải file cấu hình", "en": "2. Download a config file"},
    "step2_text": {
        "vi": "Trên trang Downloads, tìm mục WireGuard configuration:\n"
              "  • Đặt tên bất kỳ, Platform chọn Windows\n"
              "  • Chọn một máy chủ (gói Free có Nhật, Hà Lan, Mỹ…)\n"
              "  • Bấm Create, rồi bấm Download",
        "en": "On the Downloads page, find WireGuard configuration:\n"
              "  • Enter any name, set Platform to Windows\n"
              "  • Pick a server (the Free plan has Japan, the Netherlands, the US…)\n"
              "  • Click Create, then Download",
    },
    "open_downloads": {"vi": "Mở trang tải cấu hình", "en": "Open the config page"},
    "proton_help": {"vi": "Hướng dẫn của Proton", "en": "Proton's guide"},
    "step3": {"vi": "3. App tự nhận file", "en": "3. The app picks the file up"},
    "waiting": {"vi": "⏳ Đang chờ file .conf trong thư mục Downloads…",
                "en": "⏳ Waiting for a .conf file in your Downloads folder…"},
    "found": {"vi": "✓ Đã nhận {count} máy chủ: {names}", "en": "✓ Got {count} server(s): {names}"},
    "choose": {"vi": "File lưu chỗ khác? Chọn file…", "en": "Saved somewhere else? Choose file…"},
    "tip": {"vi": "Mẹo: tải thêm vài nước khác nhau, app sẽ tự chọn máy chủ ping thấp nhất.",
            "en": "Tip: download a few different countries; the app picks the lowest-ping server."},
    "later": {"vi": "Để sau", "en": "Later"},
    "play": {"vi": "Bật bảo vệ & vào game", "en": "Protect & play"},
})


class SetupGuide(tk.Toplevel):
    """The owner polls Downloads and calls show_profiles(); the guide only displays and forwards clicks."""

    def __init__(self, owner: tk.Tk, on_choose_file: Callable[[], None], on_play: Callable[[], None]) -> None:
        super().__init__(owner)
        self.on_play = on_play
        self.title(tr("title"))
        self.transient(owner)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=tr("intro"), wraplength=460, justify="left").pack(fill="x", pady=(0, 10))

        self._step(body, tr("step1"), tr("step1_text"), [(tr("signup"), lambda: webbrowser.open(PROTON_SIGNUP_URL))])
        self._step(body, tr("step2"), tr("step2_text"), [
            (tr("open_downloads"), lambda: webbrowser.open(PROTON_DOWNLOADS_URL)),
            (tr("proton_help"), lambda: webbrowser.open(PROTON_HELP_URL)),
        ])
        step3 = self._step(body, tr("step3"), None, [(tr("choose"), on_choose_file)])
        self.lbl_status = tk.Label(step3, font=("Segoe UI", 10, "bold"), anchor="w", justify="left", wraplength=440)
        self.lbl_status.pack(fill="x", padx=8, pady=(0, 6), before=step3.winfo_children()[0])

        ttk.Label(body, text=tr("tip"), foreground=COLOR_MUTED, wraplength=460).pack(fill="x", pady=(8, 10))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text=tr("later"), command=self.destroy).pack(side="right")
        self.btn_play = ttk.Button(buttons, text=tr("play"), command=self._play)
        self.btn_play.pack(side="right", padx=8)
        self.show_profiles([])

    @staticmethod
    def _step(parent, title: str, text: str | None, actions) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title)
        frame.pack(fill="x", pady=4)
        if text:
            ttk.Label(frame, text=text, justify="left", wraplength=440).pack(fill="x", padx=8, pady=(4, 4))
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=8, pady=(0, 8))
        for label, command in actions:
            ttk.Button(row, text=label, command=command).pack(side="left", padx=(0, 8))
        return frame

    def show_profiles(self, names: list[str]) -> None:
        if names:
            shown = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
            self.lbl_status.configure(text=tr("found", count=len(names), names=shown), fg=COLOR_OK)
            self.btn_play.state(["!disabled"])
        else:
            self.lbl_status.configure(text=tr("waiting"), fg=COLOR_WAIT)
            self.btn_play.state(["disabled"])

    def _play(self) -> None:
        self.destroy()
        self.on_play()

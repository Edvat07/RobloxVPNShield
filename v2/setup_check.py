"""First-run checks for a freshly downloaded copy: opened straight from the zip, or the core folder is missing.

Both end in a plain-language dialog instead of a sing-box error the user cannot act on.
"""

from __future__ import annotations

import os
import re
import webbrowser
from pathlib import Path
from tkinter import messagebox

import i18n

REQUIRED_CORE_FILES = ("sing-box.exe", "wintun.dll")
# Folders archive tools extract a single double-clicked file into:
# Explorer "Temp1_<name>.zip", WinRAR "Rar$EXa…", 7-Zip "7zO…" / "7zS…".
_ARCHIVE_TEMP_PART = re.compile(r"(^temp\d+_.+\.zip$)|(\.zip$)|(^rar\$)|(^7z[os][0-9a-f]*$)", re.IGNORECASE)

tr = i18n.Strings({
    "in_zip": {
        "vi": "Bạn đang mở app ngay bên trong file zip, nên Windows chỉ lấy ra mỗi file exe và thiếu thư mục "
              "\"core\".\n\nCách sửa:\n1. Đóng cửa sổ này.\n2. Chuột phải file zip vừa tải → Extract All… (Giải "
              "nén tất cả) → chọn một thư mục cố định, ví dụ C:\\Apps.\n3. Mở {exe} trong thư mục vừa giải nén.",
        "en": "You opened the app straight from inside the zip file, so Windows only took out the exe and the "
              "\"core\" folder is missing.\n\nTo fix it:\n1. Close this window.\n2. Right-click the downloaded zip → "
              "Extract All… → choose a permanent folder, for example C:\\Apps.\n3. Open {exe} in the extracted "
              "folder.",
    },
    "missing_core": {
        "vi": "Thiếu file cần thiết trong thư mục \"core\" cạnh app:\n{files}\n\nHãy tải lại bản đầy đủ (file zip) "
              "và giải nén cả thư mục, giữ nguyên thư mục \"core\" cạnh {exe}.\n\nMở trang tải ngay?",
        "en": "Required files are missing from the \"core\" folder next to the app:\n{files}\n\nDownload the full "
              "release (the zip) again and extract the whole folder, keeping \"core\" next to {exe}.\n\nOpen the "
              "download page now?",
    },
})


def running_from_archive(base_dir: Path) -> bool:
    """True if the exe sits in a temp folder an archive tool made for a single double-clicked file."""
    temp = Path(os.environ.get("TEMP", "")).resolve()
    try:
        relative = base_dir.resolve().relative_to(temp)
    except ValueError:
        return False
    return any(_ARCHIVE_TEMP_PART.search(part) for part in relative.parts)


def missing_core_files(core_dir: Path) -> list[str]:
    return [name for name in REQUIRED_CORE_FILES if not (core_dir / name).is_file()]


def ensure_ready(base_dir: Path, core_dir: Path, app_title: str, exe_name: str, download_url: str) -> bool:
    """Show what to do and return False when the app cannot run from where it is."""
    missing = missing_core_files(core_dir)
    if not missing:
        return True
    if running_from_archive(base_dir):
        messagebox.showwarning(app_title, tr("in_zip", exe=exe_name))
    elif messagebox.askyesno(app_title, tr("missing_core", files="\n".join(f"  • core\\{f}" for f in missing),
                                            exe=exe_name), icon="warning"):
        webbrowser.open(download_url)
    return False

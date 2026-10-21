"""Minimal Win32 helpers via ctypes: elevation, process enumeration/termination, kill-on-close job object."""

from __future__ import annotations

import ctypes
import socket
import subprocess
import sys
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_kernel32.SetInformationJobObject.restype = wintypes.BOOL
_kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
_kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
_shell32.ShellExecuteW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int)
_shell32.ShellExecuteW.restype = wintypes.HINSTANCE


_SYNCHRONIZE = 0x00100000
_kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.OpenMutexW.restype = wintypes.HANDLE

# Roblox Shield and Steam Unblock both create a strict-route TUN and must never run one at the same time.
# Each holds its mutex while its TUN may be up; Windows drops it when the process exits or crashes.
SHIELD_TUNNEL_MUTEX = r"Global\RobloxShield.Tunnel"
STEAM_TUNNEL_MUTEX = r"Global\SteamUnblock.Tunnel"
# Held by Roblox Shield only while the tunnel is verified up; Roblox Guard blocks Roblox whenever it is absent.
SHIELD_PROTECTED_MUTEX = r"Global\RobloxShield.Protected"
SHIELD_APP_MUTEX = r"Global\RobloxShield.App"
GUARD_APP_MUTEX = r"Global\RobloxGuard.App"
# Signals to the running Roblox Shield window (a second launch or Roblox Guard sets them).
SHIELD_CONNECT_EVENT = r"Global\RobloxShield.Connect"
SHIELD_SHOW_EVENT = r"Global\RobloxShield.Show"
# Single instance for Steam Unblock: a second launch (e.g. the shortcut while it sits in the tray) shows the window.
STEAM_APP_MUTEX = r"Global\SteamUnblock.App"
STEAM_SHOW_EVENT = r"Global\SteamUnblock.Show"

_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002


class NamedMutex:
    """A presence flag for other processes: held from acquire() until release() or process exit."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None

    def acquire(self) -> bool:
        """Returns False if another process already held it (useful as a single-instance check)."""
        if self._handle:
            return True
        ctypes.set_last_error(0)
        self._handle = _kernel32.CreateMutexW(None, False, self.name)
        return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            _kernel32.CloseHandle(handle)


def mutex_exists(name: str) -> bool:
    handle = _kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


_kernel32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.OpenEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.WaitForSingleObject.restype = wintypes.DWORD


class NamedEvent:
    """Auto-reset event another process can set with signal_event(); poll it with is_set()."""

    def __init__(self, name: str) -> None:
        self._handle = _kernel32.CreateEventW(None, False, False, name)

    def is_set(self) -> bool:
        return bool(self._handle) and _kernel32.WaitForSingleObject(self._handle, 0) == 0


def signal_event(name: str) -> bool:
    """Set a NamedEvent owned by another process. False if no process has created it."""
    handle = _kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        return bool(_kernel32.SetEvent(handle))
    finally:
        _kernel32.CloseHandle(handle)


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
_user32.IsWindowVisible.argtypes = (wintypes.HWND,)
_user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)


def visible_window_titles() -> dict[int, str]:
    """{hwnd: title} of every visible top-level window with a title."""
    titles: dict[int, str] = {}

    def collect(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            length = _user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                titles[hwnd] = buf.value
        return True

    _user32.EnumWindows(_WNDENUMPROC(collect), 0)
    return titles


def flush_dns_cache() -> None:
    """Drop cached (hijacked or fake) answers so apps re-resolve through the current route."""
    try:
        ctypes.WinDLL("dnsapi").DnsFlushResolverCache()
    except (OSError, AttributeError):
        pass


_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
_iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
_iphlpapi.IcmpCloseHandle.argtypes = (wintypes.HANDLE,)
_iphlpapi.IcmpSendEcho.argtypes = (
    wintypes.HANDLE, wintypes.ULONG, wintypes.LPVOID, wintypes.WORD, wintypes.LPVOID, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD,
)
_iphlpapi.IcmpSendEcho.restype = wintypes.DWORD


class _ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ("Address", wintypes.ULONG),
        ("Status", wintypes.ULONG),
        ("RoundTripTime", wintypes.ULONG),
        ("DataSize", wintypes.USHORT),
        ("Reserved", wintypes.USHORT),
        ("Data", wintypes.LPVOID),
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", wintypes.LPVOID),
    ]


def icmp_ping_ms(ipv4: str, count: int = 3, timeout_ms: int = 1000) -> int | None:
    """Best round-trip time in ms to an IPv4 address, or None if every echo was lost."""
    handle = _iphlpapi.IcmpCreateFile()
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    payload = b"RobloxShield"
    reply = ctypes.create_string_buffer(ctypes.sizeof(_ICMP_ECHO_REPLY) + len(payload) + 64)
    # IPAddr is the address in network byte order, i.e. the raw 4 bytes read as a little-endian ULONG.
    dest = int.from_bytes(socket.inet_aton(ipv4), "little")
    best: int | None = None
    try:
        for _ in range(count):
            if _iphlpapi.IcmpSendEcho(handle, dest, payload, len(payload), None, reply, len(reply), timeout_ms):
                echo = _ICMP_ECHO_REPLY.from_buffer(reply)
                if echo.Status == 0:
                    best = echo.RoundTripTime if best is None else min(best, echo.RoundTripTime)
    finally:
        _iphlpapi.IcmpCloseHandle(handle)
    return best


def enable_dpi_awareness() -> None:
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass


def is_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> bool:
    """Start an elevated copy of the current program. Returns True if the UAC prompt was accepted."""
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    rc = _shell32.ShellExecuteW(None, "runas", sys.executable, subprocess.list2cmdline(args), None, 1)
    return (rc or 0) > 32


def find_processes(names_lower: frozenset[str]) -> list[int]:
    """Return PIDs whose executable name (lowercase) is in names_lower. ~1 ms, no child process spawned."""
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        return []
    pids: list[int] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in names_lower:
                pids.append(entry.th32ProcessID)
            ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    return pids


def terminate_process(pid: int) -> bool:
    handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(handle, 1))
    finally:
        _kernel32.CloseHandle(handle)


class KillOnCloseJob:
    """
    Every process assigned here is killed by Windows when this program exits for ANY reason
    (normal close, crash, Task Manager). Guarantees Roblox never outlives the tunnel.
    """

    def __init__(self) -> None:
        self._handle = _kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            self._handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int) -> bool:
        proc = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(self._handle, proc))
        finally:
            _kernel32.CloseHandle(proc)

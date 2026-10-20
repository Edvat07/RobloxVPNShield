"""WireGuard profile parsing, sing-box config generation, process control and exit-IP verification."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import ssl
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROBLOX_PROCESSES: tuple[str, ...] = (
    "RobloxPlayerBeta.exe",
    "RobloxPlayerLauncher.exe",
    "RobloxPlayerInstaller.exe",
    "RobloxCrashHandler.exe",
    "RobloxStudioBeta.exe",
    "RobloxStudioLauncherBeta.exe",
)
ROBLOX_DOMAINS: tuple[str, ...] = ("roblox.com", "rbxcdn.com", "rbx.com", "robloxlabs.com")

# Vietnamese ISPs block Steam twice: DNS answers are hijacked (even queries to 8.8.8.8) and TLS connections
# are reset by DPI after reading the SNI. Resolving over DoH and splitting the ClientHello into several TLS
# records gets through both, without any tunnel. Game servers are never touched.
STEAM_DOMAINS: tuple[str, ...] = (
    "steampowered.com", "steamcommunity.com", "steamstatic.com", "steamserver.net", "steamcontent.com",
    "steam-chat.com", "steamgames.com", "steamusercontent.com", "valvesoftware.com", "akamaihd.net",
)
STEAM_DOH_SERVER = "1.1.1.1"
STEAM_PROBE_URL = "https://steamcommunity.com/"

GEO_PROBE_IP = "1.0.0.1"
GEO_PROBE_URL = f"https://{GEO_PROBE_IP}/cdn-cgi/trace"
HOME_COUNTRY = "VN"

TUN_INTERFACE_NAME = "RobloxShield"
WG_TAG = "wg"
WG_DIAL_TAG = "wg-dial"
DEFAULT_WG_DNS = "1.1.1.1"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WireGuardProfile:
    private_key: str
    addresses: tuple[str, ...]
    peer_public_key: str
    endpoint_host: str
    endpoint_port: int
    allowed_ips: tuple[str, ...]
    preshared_key: str = ""
    dns: tuple[str, ...] = field(default_factory=tuple)
    mtu: int = 0
    keepalive: int = 25


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_endpoint(value: str) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):
        host, _, port = value[1:].partition("]:")
    else:
        host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise ProfileError(f"Endpoint không hợp lệ: {value}")
    return host, int(port)


def parse_wireguard_conf(path: str | os.PathLike) -> WireGuardProfile:
    """Parse a standard wg-quick .conf file (single peer, as exported by Proton VPN)."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    peer_count = 0

    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip().lower()
            if name == "peer":
                peer_count += 1
                if peer_count > 1:
                    raise ProfileError("File cấu hình có nhiều hơn 1 [Peer], chỉ hỗ trợ 1 peer.")
            current = sections.setdefault(name, {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current.setdefault(key.strip().lower(), []).extend(_split_list(value))

    iface, peer = sections.get("interface"), sections.get("peer")
    if not iface or not peer:
        raise ProfileError("Thiếu mục [Interface] hoặc [Peer].")

    def first(sec: dict[str, list[str]], key: str, required: bool = True) -> str:
        values = sec.get(key)
        if not values:
            if required:
                raise ProfileError(f"Thiếu trường '{key}'.")
            return ""
        return values[0]

    host, port = _parse_endpoint(first(peer, "endpoint"))
    addresses = tuple(a if "/" in a else (f"{a}/128" if ":" in a else f"{a}/32") for a in iface.get("address", []))
    if not addresses:
        raise ProfileError("Thiếu trường 'Address'.")

    mtu = first(iface, "mtu", required=False)
    keepalive = first(peer, "persistentkeepalive", required=False)

    return WireGuardProfile(
        private_key=first(iface, "privatekey"),
        addresses=addresses,
        peer_public_key=first(peer, "publickey"),
        endpoint_host=host,
        endpoint_port=port,
        allowed_ips=tuple(peer.get("allowedips") or ("0.0.0.0/0", "::/0")),
        preshared_key=first(peer, "presharedkey", required=False),
        dns=tuple(d for d in iface.get("dns", []) if _is_ip(d)),
        mtu=int(mtu) if mtu.isdigit() else 0,
        keepalive=int(keepalive) if keepalive.isdigit() else 25,
    )


def host_has_ipv6() -> bool:
    """True if the OS has a global IPv6 route. UDP connect() only does a route lookup, nothing is sent."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
            sock.connect(("2001:4860:4860::8888", 53))
        return True
    except OSError:
        return False


def _v4_only(items) -> list[str]:
    return [item for item in items if ":" not in item]


def build_singbox_config(profile: WireGuardProfile, log_path: Path, ipv6: bool) -> dict:
    """
    Routing policy:
      * Roblox processes, Roblox domains (any app, e.g. browser) and the geo probe -> WireGuard only.
      * Steam domains -> direct, resolved over DoH, ClientHello split into TLS records (ISP block bypass).
      * Everything else -> direct (Vietnam ISP).
    There is no fallback outbound for Roblox traffic: if WireGuard is down, Roblox loses connectivity
    instead of leaking through the local network.
    Without host IPv6, every IPv6 address is dropped and DNS answers IPv4 only, so apps never try
    IPv6 connections that can only fail.
    """
    addresses = list(profile.addresses) if ipv6 else _v4_only(profile.addresses)
    allowed_ips = list(profile.allowed_ips) if ipv6 else _v4_only(profile.allowed_ips)
    if not addresses or not allowed_ips:
        raise ProfileError("Cấu hình không có địa chỉ IPv4, mà máy này không có IPv6.")

    peer: dict = {
        "address": profile.endpoint_host,
        "port": profile.endpoint_port,
        "public_key": profile.peer_public_key,
        "allowed_ips": allowed_ips,
        "persistent_keepalive_interval": profile.keepalive,
    }
    if profile.preshared_key:
        peer["pre_shared_key"] = profile.preshared_key

    # With the default dialer, WireGuard listens on udp4 AND udp6 bound to the physical NIC; on a host with
    # IPv6 disabled the udp6 bind fails and takes the whole bind down (no handshake is ever sent).
    # A detour makes it dial only the peer's address instead.
    endpoint: dict = {
        "type": "wireguard",
        "tag": WG_TAG,
        "address": addresses,
        "private_key": profile.private_key,
        "peers": [peer],
        "detour": WG_DIAL_TAG,
    }
    if profile.mtu:
        endpoint["mtu"] = profile.mtu

    vpn_dns = next((d for d in profile.dns if ipv6 or ":" not in d), DEFAULT_WG_DNS)
    tun_address = ["172.19.0.1/30", "fdfe:dcba:9876::1/126"] if ipv6 else ["172.19.0.1/30"]

    return {
        "log": {"level": "warn", "timestamp": True, "output": str(log_path)},
        "dns": {
            "servers": [
                {"type": "local", "tag": "dns-local"},
                {"type": "udp", "tag": "dns-vpn", "server": vpn_dns, "detour": WG_TAG},
                {"type": "https", "tag": "dns-doh", "server": STEAM_DOH_SERVER},
            ],
            "rules": [
                {"domain_suffix": list(ROBLOX_DOMAINS), "server": "dns-vpn"},
                {"domain_suffix": list(STEAM_DOMAINS), "server": "dns-doh"},
            ],
            "final": "dns-local",
            "strategy": "prefer_ipv4" if ipv6 else "ipv4_only",
        },
        "endpoints": [endpoint],
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": TUN_INTERFACE_NAME,
                "address": tun_address,
                "auto_route": True,
                "strict_route": True,
            }
        ],
        "outbounds": [
            {"type": "direct", "tag": "direct"},
            {"type": "direct", "tag": WG_DIAL_TAG, "domain_resolver": "dns-local"},
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"process_name": list(ROBLOX_PROCESSES), "outbound": WG_TAG},
                {"domain_suffix": list(ROBLOX_DOMAINS), "outbound": WG_TAG},
                {"ip_cidr": [f"{GEO_PROBE_IP}/32"], "outbound": WG_TAG},
                {"domain_suffix": list(STEAM_DOMAINS), "protocol": ["tls"], "outbound": "direct", "tls_record_fragment": True},
            ],
            "final": "direct",
            "auto_detect_interface": True,
            "default_domain_resolver": "dns-local",
        },
    }


STEAM_TUN_INTERFACE_NAME = "SteamUnblock"
FAKEIP_RANGE = "198.18.0.0/15"


def build_steam_unblock_config(log_path: Path, ipv6: bool) -> dict:
    """
    Steam-only bypass, no VPN. Steam domains get fake IPs, and only that fake range is routed into the TUN,
    so every other packet (games, voice, downloads from other apps) keeps the normal route with zero overhead.
    Inside the TUN, connections to Steam leave directly, resolved over DoH, with the ClientHello split into
    TLS records. All other DNS queries go to the system resolver as before.
    """
    steam = list(STEAM_DOMAINS)
    return {
        "log": {"level": "warn", "timestamp": True, "output": str(log_path)},
        "dns": {
            "servers": [
                {"type": "local", "tag": "dns-local"},
                {"type": "https", "tag": "dns-doh", "server": STEAM_DOH_SERVER},
                {"type": "fakeip", "tag": "dns-fake", "inet4_range": FAKEIP_RANGE},
            ],
            "rules": [
                {"domain_suffix": steam, "query_type": ["A"], "server": "dns-fake"},
                # AAAA would bypass the fake range: answer empty so apps use the IPv4 fake address.
                {"domain_suffix": steam, "action": "predefined", "rcode": "NOERROR"},
            ],
            "final": "dns-local",
            "strategy": "prefer_ipv4" if ipv6 else "ipv4_only",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": STEAM_TUN_INTERFACE_NAME,
                "address": ["172.19.1.1/30"],
                "auto_route": True,
                "strict_route": True,
                "route_address": [FAKEIP_RANGE],
            }
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                # Must match by domain: after sniffing, the fake IP destination is rewritten to the domain.
                # TLS only: record fragmentation corrupts plain HTTP.
                {"domain_suffix": steam, "protocol": ["tls"], "outbound": "direct", "tls_record_fragment": True},
            ],
            "final": "direct",
            "auto_detect_interface": True,
            "default_domain_resolver": "dns-doh",
        },
    }


class SingBox:
    def __init__(self, core_dir: Path, name: str = "runtime", log_name: str = "sing-box.log"):
        self.exe = core_dir / "sing-box.exe"
        self.config_path = core_dir / f"{name}.json"
        self.log_path = core_dir / log_name
        self._proc: subprocess.Popen | None = None
        # A crash of the previous run can leave the WireGuard private key on disk.
        for leftover in (self.config_path, self.config_path.with_suffix(".tmp")):
            leftover.unlink(missing_ok=True)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, config: dict) -> int:
        if self.is_alive():
            return self._proc.pid
        if not self.exe.is_file():
            raise FileNotFoundError(f"Không tìm thấy {self.exe}")

        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.config_path)
        self.log_path.write_text("", encoding="utf-8")

        check = subprocess.run(
            [str(self.exe), "check", "-c", str(self.config_path)],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW,
        )
        if check.returncode != 0:
            raise RuntimeError((check.stderr or check.stdout).strip() or "sing-box check thất bại")

        self._proc = subprocess.Popen(
            [str(self.exe), "run", "-c", str(self.config_path), "-D", str(self.exe.parent)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
        )
        return self._proc.pid

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.config_path.unlink(missing_ok=True)  # contains the WireGuard private key

    def log_tail(self, lines: int = 8) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""


_SSL_CTX = ssl.create_default_context()


def probe_exit_location(timeout: float = 4.0) -> tuple[str, str]:
    """Return (ip, country_code) as seen by Cloudflare through the tunnel route."""
    req = urllib.request.Request(GEO_PROBE_URL, headers={"User-Agent": "RobloxShield"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        body = resp.read(4096).decode("ascii", errors="replace")
    data = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
    return data.get("ip", "?"), data.get("loc", "?").upper()


def probe_steam(timeout: float = 8.0) -> int:
    """HTTP status of a Steam page through the system resolver/route; raises OSError when blocked."""
    req = urllib.request.Request(STEAM_PROBE_URL, headers={"User-Agent": "SteamUnblock"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return resp.status

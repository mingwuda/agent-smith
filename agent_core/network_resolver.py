"""Small DNS fallback for unstable model gateway resolution."""
from __future__ import annotations
import socket
import threading
from typing import Iterable


_LOCK = threading.Lock()
_ORIG_GETADDRINFO = socket.getaddrinfo
_PATCHED = False
_HOST_IPS: dict[str, list[str]] = {}
_HOST_INDEX: dict[str, int] = {}


def configure_host_resolution(host: str, ips: Iterable[str] | str = ""):
    """Install a process-wide fallback resolver for a hostname when explicit IPs are configured."""
    host = (host or "").strip().lower()
    if not host:
        return

    if isinstance(ips, str):
        explicit_ips = [item.strip() for item in ips.replace(";", ",").split(",") if item.strip()]
    else:
        explicit_ips = [str(item).strip() for item in ips if str(item).strip()]

    if not explicit_ips:
        return

    with _LOCK:
        _HOST_IPS[host] = list(dict.fromkeys(explicit_ips))
        _HOST_INDEX.setdefault(host, 0)
        _install_patch_locked()


def _install_patch_locked():
    global _PATCHED
    if _PATCHED:
        return

    def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        host_key = str(host or "").strip().lower()
        try:
            result = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
            _remember_host_ips(host_key, result)
            return result
        except socket.gaierror:
            fallback_ips = _HOST_IPS.get(host_key, [])
            if not fallback_ips:
                raise
            results = []
            last_error = None
            rotated_ips = _rotate_ips(host_key, fallback_ips)
            for ip in rotated_ips:
                try:
                    results.extend(_ORIG_GETADDRINFO(ip, port, family, type, proto, flags))
                except socket.gaierror as exc:
                    last_error = exc
            if results:
                return results
            if last_error:
                raise last_error
            raise

    socket.getaddrinfo = patched_getaddrinfo
    _PATCHED = True


def _remember_host_ips(host: str, addrinfo: list):
    if not host or _is_ip_address(host):
        return
    if host in _HOST_IPS:
        return
    ips = []
    for item in addrinfo:
        sockaddr = item[4]
        if sockaddr:
            ips.append(str(sockaddr[0]))
    if ips:
        _HOST_IPS[host] = list(dict.fromkeys(ips))
        _HOST_INDEX.setdefault(host, 0)


def _rotate_ips(host: str, ips: list[str]) -> list[str]:
    if not ips:
        return []
    with _LOCK:
        start = _HOST_INDEX.get(host, 0) % len(ips)
        _HOST_INDEX[host] = start + 1
    return ips[start:] + ips[:start]


def _is_ip_address(value: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, value)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, value)
        return True
    except OSError:
        return False

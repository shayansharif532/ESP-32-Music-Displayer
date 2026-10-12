"""LAN discovery: broadcast a small UDP beacon so the desk display finds this PC without any IP configuration.

Payload:  DESKMEDIA1 {"port": 8765, "name": "<pc name>"}   - the display uses the packet's source address as the PC's IP.
The beacon is sent from every real LAN interface (virtual / VPN adapters are skipped).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading

import psutil

log = logging.getLogger("discovery")

BEACON_PERIOD_S = 2.0
SKIP_INTERFACE = ("nord", "vpn", "tun", "tap", "wintun", "vethernet", "vmware", "virtualbox", "loopback",
                  "wsl", "hyper-v", "bluetooth", "docker", "openvpn", "wireguard", "tailscale", "zerotier")


def lan_broadcasts() -> list[tuple[str, str]]:
    """[(local ip, directed broadcast address)] for each usable IPv4 LAN interface."""
    out: list[tuple[str, str]] = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        lname = name.lower()
        if any(s in lname for s in SKIP_INTERFACE):
            continue
        st = stats.get(name)
        if st is None or not st.isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            ip = ipaddress.ip_address(a.address)
            if not ip.is_private or ip.is_link_local or ip.is_loopback:
                continue
            net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
            out.append((a.address, str(net.broadcast_address)))
    return out


class Beacon(threading.Thread):
    def __init__(self, http_port: int, discovery_port: int) -> None:
        super().__init__(name="beacon", daemon=True)
        self.payload = b"DESKMEDIA1 " + json.dumps({"port": http_port, "name": socket.gethostname()}).encode()
        self.port = discovery_port
        self._halt = threading.Event()
        self._last_logged: tuple = ()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self._halt.is_set():
            targets = []
            try:
                targets = lan_broadcasts()
            except Exception as exc:
                log.debug("interface scan failed: %s", exc)
            key = tuple(targets)
            if key != self._last_logged:
                self._last_logged = key
                log.info("beacon on: %s", ", ".join(f"{ip}->{bc}" for ip, bc in targets) or "(no LAN interface)")
            for local_ip, bcast in targets:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        s.bind((local_ip, 0))
                        s.sendto(self.payload, (bcast, self.port))
                except OSError as exc:
                    log.debug("beacon via %s failed: %s", local_ip, exc)
            self._halt.wait(BEACON_PERIOD_S)

"""LAN discovery: broadcast a small UDP beacon so the desk display finds this PC without any IP configuration.

Payload:  DESKMEDIA2 {"v":2,"port":8765,"name":"<pc name>","id":"<companion id, 16 hex>","paired":bool,"pairing":bool}
The display uses the packet's source address as the PC's IP. `paired` / `pairing` are read live from the Pairing object at every send
(a display is paired / the user has opened the pairing window); the beacon is only a locator - everything else is authenticated.
The beacon is sent from every real LAN interface (virtual / VPN adapters are skipped).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
from typing import Optional

import psutil

from .pairing import Pairing

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
    def __init__(self, http_port: int, discovery_port: int, pairing: Optional[Pairing] = None) -> None:
        super().__init__(name="beacon", daemon=True)
        self.http_port = http_port
        self.pairing = pairing
        self.name_text = (pairing.name if pairing is not None else socket.gethostname())[:63]
        self.port = discovery_port
        self._halt = threading.Event()
        self._last_logged: tuple = ()

    @property
    def payload(self) -> bytes:
        """The datagram, built fresh each time: `paired` / `pairing` change while the companion runs."""
        p = self.pairing
        doc = {"v": 2, "port": self.http_port, "name": self.name_text, "id": p.cid_hex if p is not None else "0" * 16,
               "paired": p.paired if p is not None else False, "pairing": p.window_open if p is not None else False}
        return b"DESKMEDIA2 " + json.dumps(doc, separators=(",", ":")).encode()

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
            payload = self.payload
            for local_ip, bcast in targets:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        s.bind((local_ip, 0))
                        s.sendto(payload, (bcast, self.port))
                except OSError as exc:
                    log.debug("beacon via %s failed: %s", local_ip, exc)
            self._halt.wait(BEACON_PERIOD_S)

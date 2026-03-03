#!/usr/bin/env python3
"""Wireless client tracker for Cisco Catalyst 9800 WLC and Cisco APs.

Copyright (c) 2026 Cisco and/or its affiliates.

This software is licensed to you under the terms of the Cisco Sample
Code License, Version 1.1 (the "License"). You may obtain a copy of the
License at

               https://developer.cisco.com/docs/licenses

All use of the material herein must be in accordance with the terms of
the License. All rights not expressly granted by the License are
reserved. Unless required by applicable law or agreed to separately in
writing, software distributed under the License is distributed on an "AS
IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
or implied.
"""

import argparse
import pathlib
import re
import signal
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

import yaml
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# --- Load credentials from config.yaml next to this script ---
_CONFIG_PATH = pathlib.Path(__file__).resolve().parent / "config.yaml"


def _load_config() -> Dict[str, Any]:
    if not _CONFIG_PATH.exists():
        sys.exit(f"Config file not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        sys.exit(f"Invalid config format in {_CONFIG_PATH}")
    return cfg


_CFG = _load_config()

WLC_HOST = _CFG["wlc"]["host"]
WLC_USERNAME = _CFG["wlc"]["username"]
WLC_PASSWORD = _CFG["wlc"]["password"]

AP_USERNAME = _CFG["ap"]["username"]
AP_PASSWORD = _CFG["ap"]["password"]
AP_ENABLE = _CFG["ap"]["enable"]

POLL_INTERVAL = 5
MAX_ROAM_HISTORY = 10


def normalize_mac(mac: str) -> str:
    """Strip all delimiters and return lowercase hex-only MAC for comparison."""
    return re.sub(r"[:\-.]", "", mac).lower()


def mac_to_cisco(mac: str) -> str:
    """Convert any MAC to Cisco dot notation: ``aaaa.bbbb.cccc``."""
    raw = normalize_mac(mac)
    return f"{raw[0:4]}.{raw[4:8]}.{raw[8:12]}"


def mac_to_colon(mac: str) -> str:
    """Convert any MAC to colon notation: ``aa:bb:cc:dd:ee:ff``."""
    raw = normalize_mac(mac)
    return ":".join(raw[i:i + 2] for i in range(0, 12, 2))


_VALID_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def is_valid_mac(mac: str) -> bool:
    """Return True if *mac* is 12 hex digits after stripping delimiters."""
    return bool(_VALID_MAC_RE.match(normalize_mac(mac)))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class WLCClientState:
    mac: str = ""
    ap_name: str = ""
    ap_ip: str = ""
    ssid: str = ""
    protocol: str = ""
    state: str = ""
    rssi: str = ""
    snr: str = ""
    timestamp: Optional[datetime] = None


@dataclass
class APClientState:
    mac: str = ""
    ap_name: str = ""
    rssi: str = ""
    channel: str = ""
    ssid: str = ""
    mcs_rate: str = ""
    timestamp: Optional[datetime] = None


@dataclass
class RoamEvent:
    from_ap: str
    to_ap: str
    last_rssi: str
    last_mcs_rate: str
    last_channel: str
    timestamp: datetime


# ---------------------------------------------------------------------------
# WLC Session – persistent SSH connection
# ---------------------------------------------------------------------------

class WLCSession:
    def __init__(self, host: str, username: str, password: str):
        self.host = host
        self.username = username
        self.password = password
        self.connection: Optional[ConnectHandler] = None
        self.hostname: str = ""
        self._lock = threading.Lock()

    def connect(self):
        self.connection = ConnectHandler(
            device_type="cisco_ios",
            host=self.host,
            username=self.username,
            password=self.password,
        )
        self._fetch_hostname()

    def _fetch_hostname(self):
        output = self._send("show run | include hostname")
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("hostname"):
                parts = stripped.split(None, 1)
                self.hostname = parts[1] if len(parts) > 1 else ""
                break

    def _send(self, command: str) -> str:
        with self._lock:
            if self.connection is None:
                raise RuntimeError("WLC session not connected")
            return self.connection.send_command(command)

    def _send_with_retry(self, command: str) -> str:
        """Send a command, reconnecting once on failure."""
        try:
            return self._send(command)
        except Exception:
            self.reconnect()
            return self._send(command)

    def reconnect(self):
        self.disconnect()
        self.connect()

    def disconnect(self):
        with self._lock:
            if self.connection:
                try:
                    self.connection.disconnect()
                except Exception:
                    pass
                self.connection = None

    # --- data queries -------------------------------------------------------

    def get_client_state(self, mac: str) -> Optional[WLCClientState]:
        """Parse ``show wireless client mac-address <mac> detail`` for full client info."""
        cisco_mac = mac_to_cisco(mac)
        output = self._send_with_retry(f"show wireless client mac-address {cisco_mac} detail")
        if "Client MAC Address" not in output:
            return None
        return self._parse_client_detail(output, mac)

    def get_ap_ip(self, ap_name: str) -> str:
        """Derive AP management IP from ``show ap summary``."""
        output = self._send_with_retry("show ap summary")

        for line in output.splitlines():
            if ap_name in line:
                match = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", line)
                if match:
                    return match.group(1)
        return ""

    @staticmethod
    def _parse_client_detail(output: str, mac: str) -> WLCClientState:
        """Parse ``show wireless client mac-address <mac> detail``."""
        state = WLCClientState(mac=mac, timestamp=datetime.now())

        field_map: dict[str, str] = {
            "AP Name": "ap_name",
            "Wireless LAN Network Name (SSID)": "ssid",
            "Protocol": "protocol",
            "Policy Manager State": "state",
            "Radio Signal Strength Indicator": "rssi",
            "Signal to Noise Ratio": "snr",
        }
        unit_strip: dict[str, str] = {
            "rssi": " dBm",
            "snr": " dB",
        }

        for line in output.splitlines():
            stripped = line.strip()
            if ":" not in stripped:
                continue
            key, _, raw_value = stripped.partition(":")
            field_name = key.strip()
            for label, attr in field_map.items():
                if field_name == label:
                    value = raw_value.strip()
                    suffix = unit_strip.get(attr)
                    if suffix:
                        value = value.replace(suffix, "")
                    setattr(state, attr, value)
                    break

        return state


# ---------------------------------------------------------------------------
# AP Session Pool – parallel, on-demand SSH
# ---------------------------------------------------------------------------

class APSessionPool:
    def __init__(self, username: str, password: str, enable: str = ""):
        self.username = username
        self.password = password
        self.enable = enable
        self._sessions: dict[str, ConnectHandler] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=4)

    def query_rssi(self, ap_name: str, ap_ip: str, mac: str) -> Future:
        """Submit a background task to fetch RSSI from an AP."""
        return self._executor.submit(self._fetch_rssi, ap_name, ap_ip, mac)

    def _get_or_create_session(self, ap_name: str, ap_ip: str) -> ConnectHandler:
        with self._lock:
            conn = self._sessions.get(ap_name)
            if conn and conn.is_alive():
                return conn

        conn = ConnectHandler(
            device_type="cisco_ios",
            host=ap_ip,
            username=self.username,
            password=self.password,
            secret=self.enable,
        )
        if self.enable:
            conn.enable()
        with self._lock:
            self._sessions[ap_name] = conn
        return conn

    def _fetch_rssi(self, ap_name: str, ap_ip: str, mac: str) -> APClientState:
        conn = self._get_or_create_session(ap_name, ap_ip)
        output = conn.send_command("show dot11 clients")
        return self._parse_dot11_clients(output, mac, ap_name)

    @staticmethod
    def _parse_dot11_clients(output: str, mac: str, ap_name: str) -> APClientState:
        """Parse ``show dot11 clients`` for a given MAC.

        Expected columns:
        MAC  AID  ?  Ch  SSID  RSSI  Rate  ...
        """
        state = APClientState(mac=mac, ap_name=ap_name, timestamp=datetime.now())
        target = normalize_mac(mac)
        for line in output.splitlines():
            if target in normalize_mac(line):
                tokens = line.split()
                if len(tokens) >= 4:
                    state.channel = tokens[3]
                if len(tokens) >= 5:
                    state.ssid = tokens[4]
                if len(tokens) >= 6:
                    state.rssi = tokens[5]
                if len(tokens) >= 7:
                    state.mcs_rate = tokens[6]
                break
        return state

    def close_session(self, ap_name: str):
        with self._lock:
            conn = self._sessions.pop(ap_name, None)
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass

    def shutdown(self):
        with self._lock:
            names = list(self._sessions.keys())
        for name in names:
            self.close_session(name)
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Live TUI Display
# ---------------------------------------------------------------------------

class LiveDisplay:
    PANEL_WIDTH = 80

    def __init__(self):
        self.console = Console()

    def build(
        self,
        wlc_hostname: str,
        wlc_state: Optional[WLCClientState],
        ap_state: Optional[APClientState],
        roam_history: list[RoamEvent],
        wlc_error: str = "",
        ap_error: str = "",
    ) -> Table:
        """Return a renderable composed of the three panels stacked vertically."""
        outer = Table.grid(expand=False)
        outer.add_column()
        outer.add_row(self._wlc_panel(wlc_hostname, wlc_state, wlc_error))
        outer.add_row(self._ap_panel(ap_state, ap_error))
        outer.add_row(self._history_panel(roam_history))
        outer.add_row(Text("Ctrl+C to quit", style="dim"))
        return outer

    # -- individual panels ---------------------------------------------------

    def _wlc_panel(
        self, hostname: str, state: Optional[WLCClientState], error: str
    ) -> Panel:
        tbl = Table.grid(padding=(0, 2))
        tbl.add_column(min_width=10)
        tbl.add_column(min_width=20)

        if error:
            tbl.add_row("[red]Error[/red]", f"[red]{error}[/red]")
        elif state is None:
            tbl.add_row("[yellow]Status[/yellow]", "[yellow]Client not associated[/yellow]")
        else:
            ts = state.timestamp.strftime("%H:%M:%S") if state.timestamp else ""
            tbl.add_row("WLC:", hostname)
            tbl.add_row("Client:", state.mac)
            tbl.add_row("AP Name:", f"{state.ap_name:<20s}  AP IP: {state.ap_ip}")
            tbl.add_row("SSID:", f"{state.ssid:<20s}  Protocol: {state.protocol}")
            rssi_display = f"{state.rssi} dBm" if state.rssi else "N/A"
            snr_display = f"{state.snr} dB" if state.snr else "N/A"
            tbl.add_row("RSSI:", f"{rssi_display:<20s}  SNR: {snr_display}")
            tbl.add_row("State:", f"{state.state:<20s}  Updated: {ts}")

        return Panel(tbl, title="WLC Client Stats", width=self.PANEL_WIDTH, border_style="yellow")

    def _ap_panel(self, state: Optional[APClientState], error: str) -> Panel:
        title = "AP Client Stats"
        tbl = Table.grid(padding=(0, 2))
        tbl.add_column()

        if error:
            tbl.add_row(f"[red]{error}[/red]")
        elif state is None:
            tbl.add_row("[dim]Waiting for data...[/dim]")
        else:
            title = f"AP Client Stats ([white]{state.ap_name}[/white])"
            rssi_display = f"[cyan]{state.rssi} dBm[/cyan]" if state.rssi else "N/A"
            mcs_display = state.mcs_rate if state.mcs_rate else "N/A"
            ch_display = state.channel if state.channel else "N/A"
            tbl.add_row(f"Live RSSI: {rssi_display}    Rate: {mcs_display}    Ch: {ch_display}")
            ts = state.timestamp.strftime("%H:%M:%S") if state.timestamp else ""
            tbl.add_row(f"Updated: {ts}")

        return Panel(tbl, title=title, width=self.PANEL_WIDTH, border_style="yellow")

    def _history_panel(self, history: list[RoamEvent]) -> Panel:
        if not history:
            tbl = Table.grid()
            tbl.add_column()
            tbl.add_row("[dim]No roaming events yet[/dim]")
        else:
            tbl = Table.grid(padding=(0, 1))
            tbl.add_column(min_width=10)
            tbl.add_column(min_width=30)
            tbl.add_column(min_width=10)
            tbl.add_column(min_width=12)
            tbl.add_column(min_width=5, justify="right")
            for event in reversed(history):
                ts = event.timestamp.strftime("%H:%M:%S")
                rssi_text = f"{event.last_rssi} dBm" if event.last_rssi else "N/A"
                mcs_text = event.last_mcs_rate if event.last_mcs_rate else "N/A"
                ch_text = f"Ch {event.last_channel}" if event.last_channel else ""
                tbl.add_row(ts, f"{event.from_ap} -> {event.to_ap}", rssi_text, mcs_text, ch_text)

        return Panel(tbl, title="Roaming History", width=self.PANEL_WIDTH, border_style="yellow")


# ---------------------------------------------------------------------------
# Client Tracker – main orchestrator
# ---------------------------------------------------------------------------

class ClientTracker:
    def __init__(self, mac: str):
        self.mac = mac.lower()
        self.wlc = WLCSession(WLC_HOST, WLC_USERNAME, WLC_PASSWORD)
        self.ap_pool = APSessionPool(AP_USERNAME, AP_PASSWORD, AP_ENABLE)
        self.display = LiveDisplay()

        self.wlc_state: Optional[WLCClientState] = None
        self.ap_state: Optional[APClientState] = None
        self.roam_history: deque[RoamEvent] = deque(maxlen=MAX_ROAM_HISTORY)

        self.wlc_error = ""
        self.ap_error = ""
        self._current_ap: str = ""
        self._stop = threading.Event()

    def run(self):
        print(f"Connecting to WLC at {WLC_HOST}...")
        try:
            self.wlc.connect()
        except (NetmikoAuthenticationException, NetmikoTimeoutException) as exc:
            print(f"Failed to connect to WLC: {exc}")
            sys.exit(1)

        self.display.console.clear()
        self.display.console.print(f"Connected to {self.wlc.hostname}. Tracking client [green]{self.mac}[/green]...\n")

        signal.signal(signal.SIGINT, self._handle_signal)

        with Live(self._render(), console=self.display.console, refresh_per_second=2, screen=False) as live:
            while not self._stop.is_set():
                self._poll_wlc()
                self._poll_ap()
                live.update(self._render())
                self._stop.wait(timeout=POLL_INTERVAL)

        self._cleanup()

    # -- internal ------------------------------------------------------------

    def _poll_wlc(self):
        try:
            state = self.wlc.get_client_state(self.mac)
            self.wlc_error = ""
        except Exception as exc:
            self.wlc_error = str(exc)
            return

        if state is None:
            self.wlc_state = None
            return

        if state.ap_name:
            try:
                ap_ip = self.wlc.get_ap_ip(state.ap_name)
                state.ap_ip = ap_ip
            except Exception:
                state.ap_ip = ""

        if state.ap_name and state.ap_name != self._current_ap:
            if self._current_ap:
                last_rssi = self.ap_state.rssi if self.ap_state else ""
                last_mcs = self.ap_state.mcs_rate if self.ap_state else ""
                last_ch = self.ap_state.channel if self.ap_state else ""
                self.roam_history.append(
                    RoamEvent(
                        from_ap=self._current_ap,
                        to_ap=state.ap_name,
                        last_rssi=last_rssi,
                        last_mcs_rate=last_mcs,
                        last_channel=last_ch,
                        timestamp=datetime.now(),
                    )
                )
                self.ap_pool.close_session(self._current_ap)
                self.ap_state = None
            self._current_ap = state.ap_name

        self.wlc_state = state

    def _poll_ap(self):
        if not self.wlc_state or not self.wlc_state.ap_name:
            return
        if not self.wlc_state.ap_ip:
            self.ap_error = f"No IP resolved for AP {self.wlc_state.ap_name}"
            return

        try:
            future = self.ap_pool.query_rssi(
                self.wlc_state.ap_name, self.wlc_state.ap_ip, self.mac
            )
            result = future.result(timeout=10)
            self.ap_state = result
            self.ap_error = ""
        except Exception as exc:
            self.ap_error = str(exc)

    def _render(self):
        return self.display.build(
            wlc_hostname=self.wlc.hostname,
            wlc_state=self.wlc_state,
            ap_state=self.ap_state,
            roam_history=list(self.roam_history),
            wlc_error=self.wlc_error,
            ap_error=self.ap_error,
        )

    def _handle_signal(self, _signum, _frame):
        self._stop.set()

    def _cleanup(self):
        print("\nShutting down...")
        self.ap_pool.shutdown()
        self.wlc.disconnect()
        print("Done.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Track a wireless client across a Catalyst 9800 WLC and its APs."
    )
    parser.add_argument("mac", help="MAC address of the wireless client (e.g. aa:bb:cc:dd:ee:ff)")
    args = parser.parse_args()

    mac = args.mac.strip().lower()
    if not is_valid_mac(mac):
        print(f"Invalid MAC address format: {args.mac}")
        sys.exit(1)

    tracker = ClientTracker(mac)
    tracker.run()


if __name__ == "__main__":
    main()

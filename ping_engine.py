"""
ping_engine.py
==============
Two ping engines, selectable at runtime:

  "standard"  - shells out to the OS's native `ping` command. No admin/root
                needed. Precision matches Windows' own ping (~1ms).

  "high"      - uses `icmplib` to send raw ICMP echo requests directly,
                giving latency measurements with ~0.01ms resolution.
                Requires Administrator privileges on Windows (root on
                Linux/macOS) because raw ICMP sockets are a privileged
                operation on every OS. If the privilege check fails, a
                clear ERROR result is returned explaining why - the app
                does not crash.
"""

import platform
import re
import subprocess
from datetime import datetime

OS_NAME = platform.system()

try:
    from icmplib import ping as _icmp_ping
    from icmplib.exceptions import ICMPLibError
    HAS_ICMPLIB = True
except ImportError:
    HAS_ICMPLIB = False


def no_window_kwargs():
    """
    Suppresses the console window Windows would otherwise flash for every
    child process (ping.exe, or any custom action command). No-op elsewhere.
    """
    kwargs = {}
    if OS_NAME == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


# ---------------------------------------------------------------------------
# Standard engine (OS ping command)
# ---------------------------------------------------------------------------
def _build_ping_command(ip, count, timeout_sec):
    timeout_sec = max(1, int(round(timeout_sec)))
    if OS_NAME == "Windows":
        timeout_ms = int(timeout_sec * 1000)
        return ["ping", "-n", str(count), "-w", str(timeout_ms), ip]
    elif OS_NAME == "Darwin":
        timeout_ms = int(timeout_sec * 1000)
        return ["ping", "-c", str(count), "-W", str(timeout_ms), ip]
    else:
        return ["ping", "-c", str(count), "-W", str(timeout_sec), ip]


def _parse_ping_output(output):
    loss_percent = None
    avg_rtt = None
    loss_match = re.search(r"(\d+)%\s*(packet)?\s*loss", output, re.IGNORECASE)
    if loss_match:
        loss_percent = float(loss_match.group(1))
    unix_match = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+(?:/[\d.]+)?\s*ms", output)
    if unix_match:
        avg_rtt = float(unix_match.group(1))
    else:
        win_match = re.search(r"Average\s*=\s*(\d+)\s*ms", output, re.IGNORECASE)
        if win_match:
            avg_rtt = float(win_match.group(1))
    return loss_percent, avg_rtt


def ping_device_standard(hostname, ip, count, timeout_sec):
    cmd = _build_ping_command(ip, count, timeout_sec)
    result = {
        "hostname": hostname, "ip": ip, "status": "UNKNOWN",
        "avg_rtt": None, "loss": None,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw": "",
    }
    try:
        proc_timeout = count * (timeout_sec + 1) + 5
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=proc_timeout, **no_window_kwargs()
        )
        output = proc.stdout + proc.stderr
        result["raw"] = output.strip()
        loss, avg_rtt = _parse_ping_output(output)
        result["loss"] = loss
        result["avg_rtt"] = avg_rtt
        if proc.returncode == 0 and (loss is None or loss < 100):
            result["status"] = "ONLINE"
        else:
            result["status"] = "OFFLINE"
    except subprocess.TimeoutExpired:
        result["status"] = "OFFLINE"
        result["loss"] = 100.0
        result["raw"] = "Ping process timed out."
    except Exception as e:
        result["status"] = "ERROR"
        result["raw"] = str(e)
    return result


# ---------------------------------------------------------------------------
# High-precision engine (raw ICMP via icmplib)
# ---------------------------------------------------------------------------
def ping_device_precise(hostname, ip, count, timeout_sec, interval=0.2):
    result = {
        "hostname": hostname, "ip": ip, "status": "UNKNOWN",
        "avg_rtt": None, "loss": None,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw": "",
    }
    if not HAS_ICMPLIB:
        result["status"] = "ERROR"
        result["raw"] = "High-precision mode needs the 'icmplib' package, which isn't installed."
        return result
    try:
        host = _icmp_ping(ip, count=count, interval=interval, timeout=timeout_sec, privileged=True)
        if host.packets_received:
            result["avg_rtt"] = round(host.avg_rtt, 3)
        result["loss"] = round(host.packet_loss * 100, 1)
        result["status"] = "ONLINE" if host.is_alive else "OFFLINE"
        result["raw"] = (
            f"Sent {host.packets_sent}, received {host.packets_received}, "
            f"min/avg/max = {host.min_rtt:.3f}/{host.avg_rtt:.3f}/{host.max_rtt:.3f} ms, "
            f"jitter {host.jitter:.3f} ms"
        )
    except ICMPLibError as e:
        result["status"] = "ERROR"
        name = type(e).__name__
        if "Permission" in name:
            result["raw"] = (
                "High-precision mode needs Administrator privileges on Windows "
                "(root on Linux/macOS). Right-click the app -> 'Run as administrator', "
                "or switch back to Standard precision."
            )
        else:
            result["raw"] = f"{name}: {e}"
    except Exception as e:
        result["status"] = "ERROR"
        result["raw"] = str(e)
    return result


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------
def ping_device(hostname, ip, count, timeout_sec, precision="standard", interval=0.2):
    if precision == "high":
        return ping_device_precise(hostname, ip, count, timeout_sec, interval)
    return ping_device_standard(hostname, ip, count, timeout_sec)

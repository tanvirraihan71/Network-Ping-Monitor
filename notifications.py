"""
notifications.py
=================
Flexible notifications: system tray icon (with overall health reflected in
its color), sound alerts, and e-mail alerts when a host's state changes.

The tray icon needs a native GUI backend (win32 on Windows, AppIndicator/GTK
on Linux, Cocoa on macOS via pyobjc). If that backend isn't available for
any reason, tray features are silently disabled rather than crashing the
app - notifications degrade gracefully to sound/email only.
"""

import platform
import threading

import reports

OS_NAME = platform.system()

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    # pystray's backend detection can fail with errors beyond ImportError
    # (e.g. missing GTK/AppIndicator on some Linux setups) - tray support
    # should degrade gracefully rather than crash the whole app either way.
    HAS_TRAY = False


# ---------------------------------------------------------------------------
# Sound alerts
# ---------------------------------------------------------------------------
def play_alert_sound(kind="down"):
    """
    kind: 'down' (more urgent) or 'up' (informational).
    Best-effort - never raises, since a broken sound device should never
    interrupt monitoring.
    """
    try:
        if OS_NAME == "Windows":
            import winsound
            if kind == "down":
                winsound.MessageBeep(winsound.MB_ICONHAND)
            else:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
        elif OS_NAME == "Darwin":
            import subprocess
            sound = "Basso" if kind == "down" else "Glass"
            subprocess.Popen(["afplay", f"/System/Library/Sounds/{sound}.aiff"])
        else:
            print("\a", end="", flush=True)  # terminal bell fallback on Linux
    except Exception:
        pass


# ---------------------------------------------------------------------------
# E-mail alerts
# ---------------------------------------------------------------------------
DEFAULT_ALERT_SUBJECT = "[Network Ping Monitor] {hostname} is {event}"
DEFAULT_ALERT_BODY = (
    "Host: {hostname}\n"
    "IP: {ip}\n"
    "Event: {event}\n"
    "Time: {time}\n"
)


def send_alert_email(smtp_cfg, hostname, ip, event, when, subject_tpl=None, body_tpl=None):
    subject = (subject_tpl or DEFAULT_ALERT_SUBJECT).format(hostname=hostname, ip=ip, event=event, time=when)
    body = (body_tpl or DEFAULT_ALERT_BODY).format(hostname=hostname, ip=ip, event=event, time=when)
    reports.send_email_report(smtp_cfg, subject, body)


def send_alert_email_async(smtp_cfg, hostname, ip, event, when, subject_tpl=None, body_tpl=None, on_error=None):
    """Fire-and-forget so a slow/broken mail server never blocks the scan."""
    def _worker():
        try:
            send_alert_email(smtp_cfg, hostname, ip, event, when, subject_tpl, body_tpl)
        except Exception as e:
            if on_error:
                on_error(str(e))
    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------
def _make_icon_image(color):
    """Draws a simple colored circle icon on the fly (no external image file
    needed), so the tray icon can reflect overall health at a glance."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, size - 4, size - 4], fill=color)
    return img


TRAY_COLORS = {
    "good": (34, 197, 94, 255),      # green - all online
    "warning": (245, 158, 11, 255),  # amber - some warning/bad
    "critical": (239, 68, 68, 255),  # red - some offline
    "idle": (100, 116, 139, 255),    # gray - no scan yet
}


class TrayManager:
    """
    Runs a system tray icon in a background thread. If the platform backend
    isn't available, all methods become safe no-ops.
    """

    def __init__(self, app_title, on_show, on_exit):
        self._icon = None
        self._thread = None
        self._available = HAS_TRAY
        self._app_title = app_title
        self._on_show = on_show
        self._on_exit = on_exit

    def start(self):
        if not self._available:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Show", lambda: self._on_show(), default=True),
                pystray.MenuItem("Exit", lambda: self._on_exit()),
            )
            self._icon = pystray.Icon(
                "network_ping_monitor", _make_icon_image(TRAY_COLORS["idle"]), self._app_title, menu
            )
            self._thread = threading.Thread(target=self._icon.run, daemon=True)
            self._thread.start()
        except Exception:
            self._available = False
            self._icon = None

    def set_status(self, status_key):
        """status_key: 'good' | 'warning' | 'critical' | 'idle'"""
        if not self._available or not self._icon:
            return
        try:
            self._icon.icon = _make_icon_image(TRAY_COLORS.get(status_key, TRAY_COLORS["idle"]))
        except Exception:
            pass

    def notify(self, title, message):
        if not self._available or not self._icon:
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

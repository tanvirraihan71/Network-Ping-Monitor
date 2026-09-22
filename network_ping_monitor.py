#!/usr/bin/env python3
"""
Network Ping Monitor (Modern UI v3)
====================================
A GUI tool for network admins to ICMP-ping a list of devices/servers and
quickly see which are online/offline.

Design goals for this version:
  - Light, soft color palette with clear, high-contrast text (not heavy
    solid-color blocks).
  - All controls (device list, ping parameters, actions, live counts)
    packed into a compact top strip that takes minimal vertical space.
  - The results table gets the large majority of the window.

Uses `ttkbootstrap` for the modern flat theme if installed. If it isn't,
the app still runs fine with a hand-styled light theme on plain tkinter/ttk.

    pip install ttkbootstrap      (recommended, optional)

No admin/root privileges needed — pings run via your OS's native `ping`
command, not raw sockets.
"""

import csv
import json
import os
import platform
import queue
import subprocess
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk as _ttk
from tkinter import filedialog, messagebox, simpledialog

import db
import quality
import ping_engine
import reports
import notifications

APP_TITLE = "Network Ping Monitor"
CONFIG_FILE = "ping_monitor_config.json"
DEVICE_FILE_DEFAULT = "devices.csv"
DB_FILE = "ping_monitor_history.db"
OS_NAME = platform.system()

# ---------------------------------------------------------------------------
# Modern UI backend: try ttkbootstrap, fall back to a styled plain-ttk shim
# ---------------------------------------------------------------------------
try:
    import ttkbootstrap as tb
    HAS_BOOTSTRAP = True
except ImportError:
    HAS_BOOTSTRAP = False

# Soft, light "pill" colors used for the live counters and result rows.
# Kept identical whether ttkbootstrap is available or not, so the look is
# consistent either way. (bg, fg) pairs - light background, dark readable text.
SOFT = {
    "total":   ("#eef2f7", "#334155"),
    "online":  ("#e6f7ec", "#15803d"),
    "offline": ("#fdecec", "#b91c1c"),
    "error":   ("#fff6e5", "#b45309"),
}

if not HAS_BOOTSTRAP:
    _PALETTE = {
        "dark":      ("#334155", "#ffffff"),
        "secondary": ("#e2e8f0", "#1e293b"),
        "success":   ("#e6f7ec", "#15803d"),
        "danger":    ("#fdecec", "#b91c1c"),
        "warning":   ("#fff6e5", "#b45309"),
        "info":      ("#e0f2fe", "#0369a1"),
        "primary":   ("#e0e7ff", "#3730a3"),
        "light":     ("#f8fafc", "#1e293b"),
    }

    def _bkey(bootstyle):
        if not bootstyle:
            return None
        return bootstyle.replace("inverse-", "").replace("-outline", "").split("-")[0]

    def _install_fallback_styles(style):
        style.theme_use("clam")
        style.configure(".", background="#f8fafc", foreground="#1e293b", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f8fafc")
        style.configure("TLabel", background="#f8fafc", foreground="#1e293b")
        style.configure("TLabelframe", background="#f8fafc", foreground="#1e293b", bordercolor="#cbd5e1")
        style.configure("TLabelframe.Label", background="#f8fafc", foreground="#475569", font=("Segoe UI", 9, "bold"))
        style.configure("TCheckbutton", background="#f8fafc", foreground="#1e293b")
        style.configure("TEntry", fieldbackground="#ffffff", foreground="#1e293b")
        style.configure("TSpinbox", fieldbackground="#ffffff", foreground="#1e293b", arrowsize=12)
        style.configure("TButton", background="#e2e8f0", foreground="#1e293b", borderwidth=0, padding=5)
        style.map("TButton", background=[("active", "#cbd5e1")])
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff",
                         foreground="#1e293b", rowheight=26, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background="#f1f5f9", foreground="#475569",
                         font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", "#dbeafe")])
        for key, (bg, fg) in _PALETTE.items():
            style.configure(f"{key}.TFrame", background=bg)
            style.configure(f"{key}.TLabel", background=bg, foreground=fg)
            style.configure(f"{key}.TButton", background=bg, foreground=fg, borderwidth=0, padding=5)
            style.map(f"{key}.TButton", background=[("active", bg)])

    def _wrap(base_cls, tag):
        class Wrapped(base_cls):
            def __init__(self, master=None, bootstyle=None, **kwargs):
                key = _bkey(bootstyle)
                if key and "style" not in kwargs:
                    kwargs["style"] = f"{key}.{tag}"
                super().__init__(master, **kwargs)
        return Wrapped

    def _strip(base_cls):
        class Wrapped(base_cls):
            def __init__(self, master=None, bootstyle=None, **kwargs):
                super().__init__(master, **kwargs)
        return Wrapped

    class _FallbackWindow(tk.Tk):
        def __init__(self, themename=None, *a, **kw):
            super().__init__(*a, **kw)

    tb = types.SimpleNamespace(
        Window=_FallbackWindow,
        Frame=_wrap(_ttk.Frame, "TFrame"),
        Label=_wrap(_ttk.Label, "TLabel"),
        Button=_wrap(_ttk.Button, "TButton"),
        Labelframe=_strip(_ttk.Labelframe),
        LabelFrame=_strip(_ttk.Labelframe),
        Checkbutton=_strip(_ttk.Checkbutton),
        Radiobutton=_strip(_ttk.Radiobutton),
        Spinbox=_strip(_ttk.Spinbox),
        Entry=_strip(_ttk.Entry),
        Progressbar=_strip(_ttk.Progressbar),
        Treeview=_strip(_ttk.Treeview),
        Scrollbar=_strip(_ttk.Scrollbar),
        Separator=_strip(_ttk.Separator),
        Combobox=_strip(_ttk.Combobox),
        Notebook=_strip(_ttk.Notebook),
        Style=_ttk.Style,
    )


# ---------------------------------------------------------------------------
# Core ping logic (unchanged)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# GUI Application
# ---------------------------------------------------------------------------
class PingMonitorApp:
    ROW_COLORS = quality.QUALITY_COLORS

    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1080x720")
        self.root.minsize(920, 560)

        self.devices = []
        self.result_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.scan_thread = None
        self.is_scanning = False
        self.continuous_job = None
        self.dark_mode = False
        self._last_results = {}
        self._prev_status = {}  # ip -> last known status, for outage detection

        # Global config for notifications, e-mail, FTP, and scheduled reports.
        self.smtp_cfg = {"host": "", "port": 587, "username": "", "password": "",
                          "use_tls": True, "from_addr": "", "to_addrs": []}
        self.ftp_cfg = {"host": "", "port": 21, "username": "", "password": "", "remote_dir": ""}
        self.notify_cfg = {"tray_enabled": True, "tray_on_down": True, "tray_on_up": False,
                            "sound_on_down": True, "sound_on_up": False,
                            "email_on_down": False, "email_on_up": False}
        self.report_schedule = {"enabled": False, "every_hours": 24, "format": "html",
                                 "deliver_save": True, "deliver_email": False, "deliver_ftp": False,
                                 "save_dir": ""}
        self._report_job = None

        db.init_db(DB_FILE)

        if not HAS_BOOTSTRAP:
            _install_fallback_styles(_ttk.Style())

        self._build_header()
        self._build_toolbar_row1()
        self._build_toolbar_row2()
        self._build_table()

        self._load_config()
        self._poll_queue()

        self.tray = notifications.TrayManager(APP_TITLE, on_show=self._restore_from_tray, on_exit=self._exit_app)
        if self.notify_cfg.get("tray_enabled", True):
            self.tray.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        if os.path.exists(DEVICE_FILE_DEFAULT) and not self.devices:
            self._load_devices_from_csv(DEVICE_FILE_DEFAULT, silent=True)

    def _restore_from_tray(self):
        self.root.after(0, self.root.deiconify)

    def _exit_app(self):
        self.root.after(0, self._quit)

    def _quit(self):
        self._save_config()
        self.stop_event.set()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def _on_window_close(self):
        if self.notify_cfg.get("tray_enabled", True) and notifications.HAS_TRAY:
            self.root.withdraw()  # minimize to tray instead of quitting
        else:
            self._quit()

    # -------------------------- UI construction --------------------------
    def _pill(self, parent, key, prefix):
        """Small, soft-colored inline stat pill, e.g. '🖥 Total  8'."""
        bg, fg = SOFT[key]
        if HAS_BOOTSTRAP:
            card = tk.Frame(parent, bg=bg, padx=10, pady=4)
        else:
            card = tk.Frame(parent, bg=bg, padx=10, pady=4)
        card.pack(side="left", padx=4)
        lbl = tk.Label(card, text=f"{prefix} 0", bg=bg, fg=fg, font=("Segoe UI", 10, "bold"))
        lbl.pack()
        return lbl

    def _build_header(self):
        header = tb.Frame(self.root, bootstyle="light", padding=(16, 8))
        header.pack(fill="x")
        tb.Label(header, text="🌐 Network Ping Monitor", font=("Segoe UI", 14, "bold")).pack(side="left")
        tb.Label(header, text="   ICMP health checks for your network devices and servers",
                  font=("Segoe UI", 9), bootstyle="secondary").pack(side="left")
        if HAS_BOOTSTRAP:
            self.theme_btn = tb.Button(header, text="🌙 Dark mode", bootstyle="secondary",
                                        command=self.on_toggle_theme, padding=(8, 2))
            self.theme_btn.pack(side="right")
        tb.Button(header, text="🔔 Notifications", bootstyle="secondary",
                  command=self.on_open_notifications, padding=(8, 2)).pack(side="right", padx=6)
        tb.Button(header, text="📄 Reports", bootstyle="secondary",
                  command=self.on_open_reports, padding=(8, 2)).pack(side="right")

    def _build_toolbar_row1(self):
        """Row 1: device-list management buttons (left) + live stat pills (right)."""
        row = tb.Frame(self.root, padding=(16, 6, 16, 0))
        row.pack(fill="x")

        btns = tb.Frame(row)
        btns.pack(side="left")
        specs = [
            ("📂 Load", self.on_load_csv, "secondary"),
            ("💾 Save", self.on_save_csv, "secondary"),
            ("➕ Add", self.on_add_device, "secondary"),
            ("✏ Edit", self.on_edit_device, "secondary"),
            ("🗑 Remove", self.on_remove_device, "secondary"),
            ("🧹 Clear", self.on_clear_devices, "secondary"),
            ("⚙ Host Settings", self.on_host_settings, "secondary"),
            ("📊 History", self.on_view_history, "secondary"),
            ("⤢ Fit Columns", self.on_autofit_columns, "secondary"),
        ]
        for text, cmd, style in specs:
            tb.Button(btns, text=text, command=cmd, bootstyle=f"{style}",
                      padding=(8, 3)).pack(side="left", padx=2)

        pills = tb.Frame(row)
        pills.pack(side="right")
        self.stat_total_lbl = self._pill(pills, "total", "🖥 Total")
        self.stat_online_lbl = self._pill(pills, "online", "✔ Online")
        self.stat_offline_lbl = self._pill(pills, "offline", "✖ Offline")
        self.stat_error_lbl = self._pill(pills, "error", "⚠ Errors")

    def _build_toolbar_row2(self):
        """Row 2: compact inline ping parameters (left) + actions (right)."""
        row = tb.Frame(self.root, padding=(16, 6, 16, 6))
        row.pack(fill="x")

        params = tb.Frame(row)
        params.pack(side="left")

        def inline_field(label, var, kwargs, suffix=""):
            f = tb.Frame(params)
            f.pack(side="left", padx=(0, 14))
            tb.Label(f, text=label, font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
            tb.Spinbox(f, textvariable=var, width=5, **kwargs).pack(side="left")
            if suffix:
                tb.Label(f, text=suffix, font=("Segoe UI", 9)).pack(side="left", padx=(3, 0))

        self.count_var = tk.IntVar(value=4)
        inline_field("Count", self.count_var, dict(from_=1, to=100))

        self.timeout_var = tk.DoubleVar(value=1.0)
        inline_field("Timeout", self.timeout_var, dict(from_=1, to=30, increment=0.5), "sec")

        self.workers_var = tk.IntVar(value=10)
        inline_field("Concurrent", self.workers_var, dict(from_=1, to=100))

        self.interval_var = tk.DoubleVar(value=0.0)
        inline_field("Delay", self.interval_var, dict(from_=0, to=60, increment=0.5), "sec")

        prec_f = tb.Frame(params)
        prec_f.pack(side="left", padx=(0, 14))
        tb.Label(prec_f, text="Precision", font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        self.precision_var = tk.StringVar(value="standard")
        prec_combo = tb.Combobox(prec_f, textvariable=self.precision_var, width=9, state="readonly",
                                  values=["standard", "high"])
        prec_combo.pack(side="left")
        info_btn = tb.Label(prec_f, text="ⓘ", font=("Segoe UI", 9, "bold"), bootstyle="secondary",
                             cursor="hand2")
        info_btn.pack(side="left", padx=(3, 0))
        info_btn.bind("<Button-1>", lambda e: messagebox.showinfo(
            "Ping Precision",
            "Standard: uses the OS ping command, ~1ms resolution, no admin rights needed.\n\n"
            "High: uses raw ICMP sockets for ~0.01ms resolution. Requires running this app "
            "as Administrator (Windows) or root (Linux/macOS). If privileges are missing, "
            "affected devices will show an ERROR explaining this instead of a result."))

        auto_f = tb.Frame(params)
        auto_f.pack(side="left")
        self.continuous_var = tk.BooleanVar(value=False)
        tb.Label(auto_f, text="Auto re-scan every", font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        tb.Checkbutton(auto_f, variable=self.continuous_var, bootstyle="round-toggle").pack(side="left")
        self.repeat_minutes_var = tk.IntVar(value=5)
        tb.Spinbox(auto_f, textvariable=self.repeat_minutes_var, width=4, from_=1, to=1440).pack(side="left", padx=4)
        tb.Label(auto_f, text="min", font=("Segoe UI", 9)).pack(side="left")

        actions = tb.Frame(row)
        actions.pack(side="right")
        self.start_btn = tb.Button(actions, text="▶ Start Scan", bootstyle="success",
                                    command=self.on_start_scan, padding=(10, 4))
        self.start_btn.pack(side="left", padx=3)
        self.stop_btn = tb.Button(actions, text="■ Stop", bootstyle="danger",
                                   command=self.on_stop_scan, state="disabled", padding=(8, 4))
        self.stop_btn.pack(side="left", padx=3)
        tb.Button(actions, text="⬇ Export CSV", bootstyle="info",
                  command=self.on_export_results, padding=(8, 4)).pack(side="left", padx=3)

        status_row = tb.Frame(self.root, padding=(16, 0, 16, 4))
        status_row.pack(fill="x")
        self.device_count_var = tk.StringVar(value="0 devices loaded")
        tb.Label(status_row, textvariable=self.device_count_var, font=("Segoe UI", 8),
                  bootstyle="secondary").pack(side="left")
        self.status_var = tk.StringVar(value="Ready.")
        tb.Label(status_row, textvariable=self.status_var, font=("Segoe UI", 8),
                  bootstyle="secondary").pack(side="right")

        pb_kwargs = dict(bootstyle="success-striped") if HAS_BOOTSTRAP else {}
        self.progress = tb.Progressbar(self.root, mode="determinate", **pb_kwargs)
        self.progress.pack(fill="x", padx=16, pady=(0, 6))

    def _build_table(self):
        outer = tb.Frame(self.root, padding=(16, 0, 16, 12))
        outer.pack(fill="both", expand=True)

        columns = ("hostname", "ip", "status", "quality", "rtt", "loss", "checked")
        tree_kwargs = dict(bootstyle="light") if HAS_BOOTSTRAP else {}
        self.tree = tb.Treeview(outer, columns=columns, show="headings", selectmode="extended",
                                 height=20, **tree_kwargs)
        headings = {"hostname": "Hostname", "ip": "IP Address", "status": "Status", "quality": "Quality",
                    "rtt": "Avg RTT (ms)", "loss": "Packet Loss (%)", "checked": "Last Checked"}
        widths = {"hostname": 220, "ip": 140, "status": 90, "quality": 100, "rtt": 110, "loss": 130, "checked": 170}
        self._col_headings = headings
        self._sort_state = {}
        for col in columns:
            self.tree.heading(col, text=headings[col], command=lambda c=col: self.on_sort_column(c))
            stretch = (col == "hostname")
            minwidth = 120 if col == "hostname" else 60
            self.tree.column(col, width=widths[col], minwidth=minwidth, stretch=stretch,
                              anchor="center" if col != "hostname" else "w")

        vsb = tb.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        for tag, (bg, fg) in self.ROW_COLORS.items():
            if bg:
                self.tree.tag_configure(tag.lower(), background=bg, foreground=fg, font=("Segoe UI", 10, "bold"))
        self.tree.bind("<Double-1>", lambda e: self.on_view_detail())

    def on_sort_column(self, col):
        reverse = self._sort_state.get(col, False)
        items = list(self.tree.get_children(""))

        def keyfunc(iid):
            val = self.tree.set(iid, col)
            try:
                return (0, float(val))
            except (ValueError, TypeError):
                return (1, str(val).lower())

        items.sort(key=keyfunc, reverse=reverse)
        for index, iid in enumerate(items):
            self.tree.move(iid, "", index)
        self._sort_state[col] = not reverse

        for c in self.tree["columns"]:
            base = self._col_headings[c]
            arrow = ""
            if c == col:
                arrow = " ▼" if reverse else " ▲"
            self.tree.heading(c, text=base + arrow)

    def on_autofit_columns(self):
        """Resizes every column to fit its widest current cell content
        (including the header), so long hostnames (or any other value)
        are never cropped."""
        font = tkfont.Font(font=("Segoe UI", 10, "bold"))
        for col in self.tree["columns"]:
            widest = font.measure(self._col_headings[col])
            for iid in self.tree.get_children(""):
                val = self.tree.set(iid, col)
                widest = max(widest, font.measure(str(val)))
            cap = 560 if col == "hostname" else 260
            new_width = min(max(widest + 28, 70), cap)
            self.tree.column(col, width=new_width)

    def on_toggle_theme(self):
        if not HAS_BOOTSTRAP:
            return
        self.dark_mode = not self.dark_mode
        style = tb.Style()
        style.theme_use("darkly" if self.dark_mode else "flatly")
        self.theme_btn.config(text="☀ Light mode" if self.dark_mode else "🌙 Dark mode")

    # -------------------------- Notifications & Reports --------------------------
    def on_open_notifications(self):
        win = tk.Toplevel(self.root)
        win.title("Notification Settings")
        win.geometry("460x560")
        win.resizable(False, True)

        tray_note = "" if notifications.HAS_TRAY else "  (tray backend not available on this system)"
        tray_var = tk.BooleanVar(value=self.notify_cfg.get("tray_enabled", True))
        tb.Checkbutton(win, text=f"Enable system tray icon{tray_note}", variable=tray_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=(16, 4))
        tray_down_var = tk.BooleanVar(value=self.notify_cfg.get("tray_on_down", True))
        tb.Checkbutton(win, text="Show tray popup when a host goes DOWN", variable=tray_down_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=32, pady=2)
        tray_up_var = tk.BooleanVar(value=self.notify_cfg.get("tray_on_up", False))
        tb.Checkbutton(win, text="Show tray popup when a host comes back UP", variable=tray_up_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=32, pady=(2, 4))

        sound_down_var = tk.BooleanVar(value=self.notify_cfg.get("sound_on_down", True))
        tb.Checkbutton(win, text="Play sound when a host goes DOWN", variable=sound_down_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=(14, 4))
        sound_up_var = tk.BooleanVar(value=self.notify_cfg.get("sound_on_up", False))
        tb.Checkbutton(win, text="Play sound when a host comes back UP", variable=sound_up_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=4)

        email_down_var = tk.BooleanVar(value=self.notify_cfg.get("email_on_down", False))
        tb.Checkbutton(win, text="Send e-mail when a host goes DOWN", variable=email_down_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=(14, 4))
        email_up_var = tk.BooleanVar(value=self.notify_cfg.get("email_on_up", False))
        tb.Checkbutton(win, text="Send e-mail when a host comes back UP", variable=email_up_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=4)

        tb.Button(win, text="✉ Configure E-mail (SMTP) Settings...", bootstyle="info",
                  command=self.on_open_smtp_settings).pack(anchor="w", padx=16, pady=(10, 4))

        tb.Label(win, text="E-mail subject template (optional override)", font=("Segoe UI", 8),
                  bootstyle="secondary").pack(anchor="w", padx=16, pady=(10, 0))
        subj_var = tk.StringVar(value=self.notify_cfg.get("email_subject_tpl") or "")
        tb.Entry(win, textvariable=subj_var).pack(fill="x", padx=16)

        def save():
            self.notify_cfg.update({
                "tray_enabled": tray_var.get(),
                "tray_on_down": tray_down_var.get(),
                "tray_on_up": tray_up_var.get(),
                "sound_on_down": sound_down_var.get(),
                "sound_on_up": sound_up_var.get(),
                "email_on_down": email_down_var.get(),
                "email_on_up": email_up_var.get(),
                "email_subject_tpl": subj_var.get().strip() or None,
            })
            if self.notify_cfg["tray_enabled"] and not self.tray._available:
                self.tray.start()
            self._save_config()
            win.destroy()

        btn_row = tb.Frame(win)
        btn_row.pack(fill="x", padx=16, pady=16, side="bottom")
        tb.Button(btn_row, text="Save", bootstyle="success", command=save).pack(side="right", padx=3)
        tb.Button(btn_row, text="Cancel", bootstyle="secondary", command=win.destroy).pack(side="right")

    def on_open_smtp_settings(self):
        win = tk.Toplevel(self.root)
        win.title("E-mail (SMTP) Settings")
        win.geometry("420x560")
        win.resizable(False, True)

        def field(label, value, show=None):
            tb.Label(win, text=label, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(10, 2))
            var = tk.StringVar(value="" if value is None else str(value))
            tb.Entry(win, textvariable=var, show=show).pack(fill="x", padx=16)
            return var

        host_var = field("SMTP server (e.g. smtp.gmail.com)", self.smtp_cfg.get("host"))
        port_var = field("Port (587 for TLS, 465 for SSL, 25 plain)", self.smtp_cfg.get("port", 587))
        user_var = field("Username", self.smtp_cfg.get("username"))
        pass_var = field("Password", self.smtp_cfg.get("password"), show="*")
        from_var = field("From address", self.smtp_cfg.get("from_addr"))
        to_var = field("To address(es), comma-separated", ", ".join(self.smtp_cfg.get("to_addrs", [])))

        tls_var = tk.BooleanVar(value=self.smtp_cfg.get("use_tls", True))
        tb.Checkbutton(win, text="Use STARTTLS", variable=tls_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=(10, 4))

        def build_cfg():
            return {
                "host": host_var.get().strip(), "port": int(port_var.get() or 587),
                "username": user_var.get().strip(), "password": pass_var.get(),
                "use_tls": tls_var.get(), "from_addr": from_var.get().strip(),
                "to_addrs": [a.strip() for a in to_var.get().split(",") if a.strip()],
            }

        def send_test():
            cfg = build_cfg()
            try:
                reports.send_email_report(cfg, "Network Ping Monitor - Test Email",
                                           "This is a test message from Network Ping Monitor.")
                messagebox.showinfo(APP_TITLE, "Test e-mail sent successfully.")
            except Exception as e:
                messagebox.showerror(APP_TITLE, f"Failed to send test e-mail:\n{e}")

        def save():
            self.smtp_cfg = build_cfg()
            self._save_config()
            win.destroy()

        btn_row = tb.Frame(win)
        btn_row.pack(fill="x", padx=16, pady=16, side="bottom")
        tb.Button(btn_row, text="Save", bootstyle="success", command=save).pack(side="right", padx=3)
        tb.Button(btn_row, text="Cancel", bootstyle="secondary", command=win.destroy).pack(side="right")
        tb.Button(btn_row, text="Send Test E-mail", bootstyle="info", command=send_test).pack(side="left")

    def on_open_ftp_settings(self):
        win = tk.Toplevel(self.root)
        win.title("FTP Settings")
        win.geometry("380x360")
        win.resizable(False, False)

        def field(label, value):
            tb.Label(win, text=label, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(10, 2))
            var = tk.StringVar(value="" if value is None else str(value))
            tb.Entry(win, textvariable=var).pack(fill="x", padx=16)
            return var

        host_var = field("FTP host", self.ftp_cfg.get("host"))
        port_var = field("Port", self.ftp_cfg.get("port", 21))
        user_var = field("Username", self.ftp_cfg.get("username"))
        pass_var = field("Password", self.ftp_cfg.get("password"))
        dir_var = field("Remote directory (optional)", self.ftp_cfg.get("remote_dir"))

        def build_cfg():
            return {"host": host_var.get().strip(), "port": int(port_var.get() or 21),
                    "username": user_var.get().strip(), "password": pass_var.get(),
                    "remote_dir": dir_var.get().strip()}

        def save():
            self.ftp_cfg = build_cfg()
            self._save_config()
            win.destroy()

        btn_row = tb.Frame(win)
        btn_row.pack(fill="x", padx=16, pady=16, side="bottom")
        tb.Button(btn_row, text="Save", bootstyle="success", command=save).pack(side="right", padx=3)
        tb.Button(btn_row, text="Cancel", bootstyle="secondary", command=win.destroy).pack(side="right")

    def on_open_reports(self):
        win = tk.Toplevel(self.root)
        win.title("Reports")
        win.geometry("440x520")
        win.resizable(False, False)

        tb.Label(win, text="Format", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(16, 2))
        fmt_var = tk.StringVar(value=self.report_schedule.get("format", "html"))
        fmt_row = tb.Frame(win)
        fmt_row.pack(anchor="w", padx=16)
        tb.Radiobutton(fmt_row, text="HTML", variable=fmt_var, value="html").pack(side="left", padx=(0, 16))
        tb.Radiobutton(fmt_row, text="PDF", variable=fmt_var, value="pdf").pack(side="left")

        tb.Label(win, text="Delivery", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        save_var = tk.BooleanVar(value=self.report_schedule.get("deliver_save", True))
        tb.Checkbutton(win, text="Save to a folder", variable=save_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=2)

        dir_row = tb.Frame(win)
        dir_row.pack(fill="x", padx=16)
        dir_var = tk.StringVar(value=self.report_schedule.get("save_dir") or os.getcwd())
        tb.Entry(dir_row, textvariable=dir_var).pack(side="left", fill="x", expand=True)

        def browse_dir():
            d = filedialog.askdirectory(title="Choose folder for saved reports")
            if d:
                dir_var.set(d)

        tb.Button(dir_row, text="Browse...", bootstyle="secondary", command=browse_dir).pack(side="left", padx=6)

        email_var = tk.BooleanVar(value=self.report_schedule.get("deliver_email", False))
        tb.Checkbutton(win, text="E-mail the report", variable=email_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=(10, 2))
        tb.Button(win, text="✉ Configure E-mail (SMTP) Settings...", bootstyle="info",
                  command=self.on_open_smtp_settings).pack(anchor="w", padx=16, pady=(0, 4))

        ftp_var = tk.BooleanVar(value=self.report_schedule.get("deliver_ftp", False))
        tb.Checkbutton(win, text="Upload the report via FTP", variable=ftp_var,
                        bootstyle="round-toggle").pack(anchor="w", padx=16, pady=(10, 2))
        tb.Button(win, text="📁 Configure FTP Settings...", bootstyle="info",
                  command=self.on_open_ftp_settings).pack(anchor="w", padx=16, pady=(0, 4))

        tb.Label(win, text="Schedule", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(14, 2))
        sched_row = tb.Frame(win)
        sched_row.pack(anchor="w", padx=16)
        sched_var = tk.BooleanVar(value=self.report_schedule.get("enabled", False))
        tb.Checkbutton(sched_row, text="Generate automatically every", variable=sched_var,
                        bootstyle="round-toggle").pack(side="left")
        hours_var = tk.IntVar(value=self.report_schedule.get("every_hours", 24))
        tb.Spinbox(sched_row, textvariable=hours_var, width=5, from_=1, to=720).pack(side="left", padx=6)
        tb.Label(sched_row, text="hours").pack(side="left")

        def generate_now():
            fmt = fmt_var.get()
            ok, msg = self._generate_and_deliver_report(
                fmt=fmt, save_dir=dir_var.get() or os.getcwd(), deliver_save=save_var.get(),
                deliver_email=email_var.get(), deliver_ftp=ftp_var.get(), manual=True)
            if ok:
                messagebox.showinfo(APP_TITLE, msg)
            else:
                messagebox.showerror(APP_TITLE, msg)

        def save_and_close():
            self.report_schedule.update({
                "enabled": sched_var.get(), "every_hours": max(1, hours_var.get()),
                "format": fmt_var.get(), "deliver_save": save_var.get(),
                "deliver_email": email_var.get(), "deliver_ftp": ftp_var.get(),
                "save_dir": dir_var.get() or os.getcwd(),
            })
            self._save_config()
            self._restart_report_schedule()
            win.destroy()

        btn_row = tb.Frame(win)
        btn_row.pack(fill="x", padx=16, pady=16, side="bottom")
        tb.Button(btn_row, text="Save", bootstyle="success", command=save_and_close).pack(side="right", padx=3)
        tb.Button(btn_row, text="Cancel", bootstyle="secondary", command=win.destroy).pack(side="right")
        tb.Button(btn_row, text="Generate Report Now", bootstyle="primary", command=generate_now).pack(side="left")

    def _generate_and_deliver_report(self, fmt, save_dir, deliver_save, deliver_email, deliver_ftp, manual=False):
        if not self._last_results:
            return False, "No scan results yet. Run a scan before generating a report."

        ext = "pdf" if fmt == "pdf" else "html"
        filename = f"ping_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}"
        target_dir = save_dir or os.getcwd()
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception:
            pass
        path = os.path.join(target_dir, filename)

        try:
            if fmt == "pdf":
                reports.save_pdf_report(self.devices, self._last_results, path)
            else:
                reports.save_html_report(self.devices, self._last_results, path)
        except Exception as e:
            return False, f"Failed to generate report:\n{e}"

        errors = []
        if deliver_email:
            try:
                reports.send_email_report(
                    self.smtp_cfg,
                    f"Network Ping Monitor Report - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    "The latest network ping monitor report is attached.",
                    attachment_paths=[path],
                )
            except Exception as e:
                errors.append(f"E-mail delivery failed: {e}")
        if deliver_ftp:
            try:
                reports.upload_ftp_report(self.ftp_cfg, path)
            except Exception as e:
                errors.append(f"FTP upload failed: {e}")

        if not deliver_save:
            try:
                os.remove(path)
            except Exception:
                pass

        if errors:
            return False, "Report generated, but: " + "; ".join(errors)
        where = f"saved to {path}" if deliver_save else "generated and delivered"
        return True, f"Report {where}."

    def _restart_report_schedule(self):
        if self._report_job:
            self.root.after_cancel(self._report_job)
            self._report_job = None
        if self.report_schedule.get("enabled"):
            self._schedule_next_report()

    def _schedule_next_report(self):
        hours = max(1, self.report_schedule.get("every_hours", 24))
        self._report_job = self.root.after(hours * 3600 * 1000, self._run_scheduled_report)

    def _run_scheduled_report(self):
        rs = self.report_schedule
        self._generate_and_deliver_report(
            fmt=rs.get("format", "html"), save_dir=rs.get("save_dir") or os.getcwd(),
            deliver_save=rs.get("deliver_save", True), deliver_email=rs.get("deliver_email", False),
            deliver_ftp=rs.get("deliver_ftp", False), manual=False,
        )
        if self.report_schedule.get("enabled"):
            self._schedule_next_report()

    # -------------------------- Device management --------------------------
    def _refresh_device_count(self):
        self.device_count_var.set(f"{len(self.devices)} devices loaded")
        self.stat_total_lbl.config(text=f"🖥 Total  {len(self.devices)}")

    def _refresh_tree_from_devices(self):
        self.tree.delete(*self.tree.get_children())
        for dev in self.devices:
            self.tree.insert("", "end", iid=dev["ip"],
                              values=(dev["hostname"], dev["ip"], "PENDING", "-", "-", "-", "-"),
                              tags=("pending",))
        self._autofit_hostname_only()

    def _autofit_hostname_only(self):
        """Lightweight auto-fit just for the hostname column, run automatically
        whenever the device list changes (Fit Columns does all columns on demand)."""
        if not self.devices:
            return
        font = tkfont.Font(font=("Segoe UI", 10, "bold"))
        widest = font.measure(self._col_headings["hostname"])
        for dev in self.devices:
            widest = max(widest, font.measure(dev["hostname"]))
        self.tree.column("hostname", width=min(max(widest + 28, 160), 560))

    def on_load_csv(self):
        path = filedialog.askopenfilename(title="Select device list CSV",
                                           filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self._load_devices_from_csv(path)

    def _load_devices_from_csv(self, path, silent=False):
        try:
            loaded = []
            with open(path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fieldmap = {k.lower().strip(): k for k in (reader.fieldnames or [])}
                host_key = next((fieldmap[k] for k in fieldmap if "host" in k or "name" in k), None)
                ip_key = next((fieldmap[k] for k in fieldmap if "ip" in k or "address" in k), None)
                if not ip_key:
                    raise ValueError("Could not find an IP column. Expected a header like 'Hostname,IP'.")
                for row in reader:
                    ip = (row.get(ip_key) or "").strip()
                    hostname = (row.get(host_key) or "").strip() if host_key else ""
                    if ip:
                        loaded.append({"hostname": hostname or ip, "ip": ip})
            if not loaded:
                raise ValueError("No valid rows found in file.")
            self.devices = loaded
            self._refresh_device_count()
            self._refresh_tree_from_devices()
            self._save_config()
            if not silent:
                messagebox.showinfo(APP_TITLE, f"Loaded {len(loaded)} devices from:\n{path}")
        except Exception as e:
            if not silent:
                messagebox.showerror(APP_TITLE, f"Failed to load CSV:\n{e}")

    def on_save_csv(self):
        if not self.devices:
            messagebox.showwarning(APP_TITLE, "No devices to save yet.")
            return
        path = filedialog.asksaveasfilename(title="Save device list as CSV", defaultextension=".csv",
                                             filetypes=[("CSV files", "*.csv")])
        if path:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["Hostname", "IP"])
                writer.writeheader()
                for dev in self.devices:
                    writer.writerow({"Hostname": dev["hostname"], "IP": dev["ip"]})
            messagebox.showinfo(APP_TITLE, f"Saved {len(self.devices)} devices to:\n{path}")

    def on_add_device(self):
        hostname = simpledialog.askstring(APP_TITLE, "Hostname / Device name:")
        if hostname is None:
            return
        ip = simpledialog.askstring(APP_TITLE, "IP address:")
        if not ip:
            return
        self.devices.append({"hostname": hostname.strip() or ip.strip(), "ip": ip.strip()})
        self._refresh_device_count()
        self._refresh_tree_from_devices()
        self._save_config()

    def on_edit_device(self):
        sel = self.tree.selection()
        if len(sel) != 1:
            messagebox.showwarning(APP_TITLE, "Select exactly one device to edit.")
            return
        ip_old = sel[0]
        dev = next((d for d in self.devices if d["ip"] == ip_old), None)
        if not dev:
            return
        hostname = simpledialog.askstring(APP_TITLE, "Hostname / Device name:", initialvalue=dev["hostname"])
        if hostname is None:
            return
        ip_new = simpledialog.askstring(APP_TITLE, "IP address:", initialvalue=dev["ip"])
        if not ip_new:
            return
        dev["hostname"] = hostname.strip() or ip_new.strip()
        dev["ip"] = ip_new.strip()
        self._refresh_tree_from_devices()
        self._save_config()

    def on_remove_device(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning(APP_TITLE, "Select one or more devices to remove.")
            return
        self.devices = [d for d in self.devices if d["ip"] not in sel]
        self._refresh_device_count()
        self._refresh_tree_from_devices()
        self._save_config()

    def on_clear_devices(self):
        if self.devices and messagebox.askyesno(APP_TITLE, "Remove all devices from the list?"):
            self.devices = []
            self._refresh_device_count()
            self._refresh_tree_from_devices()
            self._save_config()

    def on_view_detail(self):
        sel = self.tree.selection()
        if not sel:
            return
        ip = sel[0]
        result = self._last_results.get(ip)
        if result:
            messagebox.showinfo(f"{result['hostname']} ({ip})", result.get("raw", "No details available."))

    def _selected_device(self):
        sel = self.tree.selection()
        if len(sel) != 1:
            messagebox.showwarning(APP_TITLE, "Select exactly one device first.")
            return None
        ip = sel[0]
        dev = next((d for d in self.devices if d["ip"] == ip), None)
        return dev

    def on_host_settings(self):
        dev = self._selected_device()
        if not dev:
            return
        ip = dev["ip"]
        current = db.get_host_settings(ip)
        thresholds = current.get("thresholds", {})

        win = tk.Toplevel(self.root)
        win.title(f"Host Settings — {dev['hostname']} ({ip})")
        win.geometry("440x640")
        win.resizable(False, True)

        pad = dict(padx=14, pady=(10, 2))

        def labeled_entry(parent, label, value):
            tb.Label(parent, text=label, font=("Segoe UI", 9)).pack(anchor="w", **pad)
            var = tk.StringVar(value="" if value is None else str(value))
            tb.Entry(parent, textvariable=var).pack(fill="x", padx=14)
            return var

        tb.Label(win, text="Leave a field blank to use the global setting for that value.",
                  font=("Segoe UI", 8), bootstyle="secondary").pack(anchor="w", padx=14, pady=(10, 0))

        count_var = labeled_entry(win, "Ping count override", current.get("count"))
        timeout_var = labeled_entry(win, "Timeout override (sec)", current.get("timeout"))

        tb.Label(win, text="Custom actions (optional) — {ip}, {hostname}, {event} are replaced automatically",
                  font=("Segoe UI", 8), bootstyle="secondary").pack(anchor="w", padx=14, pady=(12, 0))
        down_cmd_var = labeled_entry(win, "Command to run when host goes DOWN", current.get("on_down_cmd"))
        up_cmd_var = labeled_entry(win, "Command to run when host goes UP", current.get("on_up_cmd"))

        tb.Label(win, text="Custom quality thresholds (optional)",
                  font=("Segoe UI", 8), bootstyle="secondary").pack(anchor="w", padx=14, pady=(12, 0))
        warn_rtt_var = labeled_entry(win, "Warning RTT (ms)", thresholds.get("warning_rtt_ms"))
        bad_rtt_var = labeled_entry(win, "Bad RTT (ms)", thresholds.get("bad_rtt_ms"))
        warn_loss_var = labeled_entry(win, "Warning packet loss (%)", thresholds.get("warning_loss_pct"))
        bad_loss_var = labeled_entry(win, "Bad packet loss (%)", thresholds.get("bad_loss_pct"))

        def _to_float(s):
            s = s.strip()
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        def save():
            settings = {
                "count": int(_to_float(count_var.get())) if _to_float(count_var.get()) else None,
                "timeout": _to_float(timeout_var.get()),
                "on_down_cmd": down_cmd_var.get().strip() or None,
                "on_up_cmd": up_cmd_var.get().strip() or None,
                "thresholds": {
                    "warning_rtt_ms": _to_float(warn_rtt_var.get()),
                    "bad_rtt_ms": _to_float(bad_rtt_var.get()),
                    "warning_loss_pct": _to_float(warn_loss_var.get()),
                    "bad_loss_pct": _to_float(bad_loss_var.get()),
                },
            }
            db.save_host_settings(ip, settings)
            win.destroy()

        btn_row = tb.Frame(win)
        btn_row.pack(fill="x", padx=14, pady=14, side="bottom")
        tb.Button(btn_row, text="Save", bootstyle="success", command=save).pack(side="right", padx=3)
        tb.Button(btn_row, text="Cancel", bootstyle="secondary", command=win.destroy).pack(side="right")

    def on_view_history(self):
        dev = self._selected_device()
        if not dev:
            return
        ip, hostname = dev["ip"], dev["hostname"]

        win = tk.Toplevel(self.root)
        win.title(f"History — {hostname} ({ip})")
        win.geometry("720x480")

        notebook = tb.Notebook(win)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # --- Ping history tab ---
        hist_frame = tb.Frame(notebook)
        notebook.add(hist_frame, text="Ping History")
        hist_cols = ("time", "status", "rtt", "loss", "quality")
        hist_tree = tb.Treeview(hist_frame, columns=hist_cols, show="headings")
        for c, label, w in [("time", "Time", 160), ("status", "Status", 90),
                             ("rtt", "RTT (ms)", 90), ("loss", "Loss (%)", 90), ("quality", "Quality", 100)]:
            hist_tree.heading(c, text=label)
            hist_tree.column(c, width=w, anchor="center" if c != "time" else "w")
        hist_tree.pack(fill="both", expand=True, side="left")
        hist_vsb = tb.Scrollbar(hist_frame, orient="vertical", command=hist_tree.yview)
        hist_tree.configure(yscrollcommand=hist_vsb.set)
        hist_vsb.pack(side="right", fill="y")

        for ts, status, rtt, loss, q in db.get_history(ip, limit=500):
            rtt_txt = "-" if rtt is None else f"{rtt:.3f}" if rtt < 1 else f"{rtt:.1f}"
            loss_txt = "-" if loss is None else f"{loss:.0f}"
            hist_tree.insert("", "end", values=(ts, status, rtt_txt, loss_txt, q or "-"))

        # --- Outages tab ---
        out_frame = tb.Frame(notebook)
        notebook.add(out_frame, text="Outages")
        out_cols = ("down_at", "up_at", "duration")
        out_tree = tb.Treeview(out_frame, columns=out_cols, show="headings")
        for c, label, w in [("down_at", "Down At", 180), ("up_at", "Up At", 180), ("duration", "Duration", 120)]:
            out_tree.heading(c, text=label)
            out_tree.column(c, width=w, anchor="center")
        out_tree.pack(fill="both", expand=True, side="left")
        out_vsb = tb.Scrollbar(out_frame, orient="vertical", command=out_tree.yview)
        out_tree.configure(yscrollcommand=out_vsb.set)
        out_vsb.pack(side="right", fill="y")

        for down_at, up_at, duration in db.get_outages(ip):
            if duration is not None:
                mins, secs = divmod(int(duration), 60)
                dur_txt = f"{mins}m {secs}s" if mins else f"{secs}s"
            else:
                dur_txt = "ongoing" if not up_at else "-"
            out_tree.insert("", "end", values=(down_at, up_at or "(still down)", dur_txt))

    # -------------------------- Scanning --------------------------
    def on_start_scan(self):
        if self.is_scanning:
            return
        if not self.devices:
            messagebox.showwarning(APP_TITLE, "Add or load devices first.")
            return

        self.is_scanning = True
        self.stop_event.clear()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._last_results = {}
        self._update_stat_cards()

        for dev in self.devices:
            self.tree.item(dev["ip"], values=(dev["hostname"], dev["ip"], "PENDING", "-", "-", "-", "-"), tags=("pending",))

        count = self.count_var.get()
        timeout_sec = self.timeout_var.get()
        max_workers = max(1, self.workers_var.get())
        interval = max(0.0, self.interval_var.get())
        precision = self.precision_var.get()

        self.progress.config(maximum=len(self.devices), value=0)
        self.status_var.set(f"Scanning {len(self.devices)} devices...")

        self.scan_thread = threading.Thread(
            target=self._run_scan, args=(list(self.devices), count, timeout_sec, max_workers, interval, precision),
            daemon=True)
        self.scan_thread.start()

    def _run_scan(self, devices, count, timeout_sec, max_workers, interval, precision):
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for dev in devices:
                if self.stop_event.is_set():
                    break
                # Per-host overrides (count/timeout) fall back to the global values.
                settings = db.get_host_settings(dev["ip"])
                dev_count = settings.get("count") or count
                dev_timeout = settings.get("timeout") or timeout_sec
                fut = executor.submit(
                    ping_engine.ping_device, dev["hostname"], dev["ip"], dev_count, dev_timeout, precision)
                futures[fut] = dev
                if interval > 0:
                    time.sleep(interval)
            for fut in as_completed(futures):
                if self.stop_event.is_set():
                    break
                try:
                    result = fut.result()
                except Exception as e:
                    dev = futures[fut]
                    result = {"hostname": dev["hostname"], "ip": dev["ip"], "status": "ERROR",
                              "avg_rtt": None, "loss": None,
                              "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "raw": str(e)}
                self.result_queue.put(result)
        self.result_queue.put({"__done__": True})

    def _poll_queue(self):
        try:
            while True:
                item = self.result_queue.get_nowait()
                if item.get("__done__"):
                    self._on_scan_finished()
                    continue
                self._apply_result(item)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_queue)

    def _apply_result(self, result):
        self._last_results[result["ip"]] = result
        ip = result["ip"]
        hostname = result["hostname"]
        status = result["status"]
        avg_rtt = result["avg_rtt"]
        loss = result["loss"]

        host_settings = db.get_host_settings(ip)
        thresholds = host_settings.get("thresholds")
        q = quality.assess_quality(status, avg_rtt, loss, thresholds)

        db.record_ping(ip, hostname, status, avg_rtt, loss, q, ts=result["checked_at"])
        self._handle_outage_transition(ip, hostname, status, host_settings)

        rtt_txt = "-" if avg_rtt is None else (f"{avg_rtt:.3f}" if avg_rtt < 1 else f"{avg_rtt:.1f}")
        loss_txt = f"{loss:.0f}" if loss is not None else "-"
        if self.tree.exists(ip):
            self.tree.item(ip, values=(hostname, ip, status, q, rtt_txt, loss_txt, result["checked_at"]),
                            tags=(q.lower(),))
        self.progress.step(1)
        self._update_stat_cards()

    def _handle_outage_transition(self, ip, hostname, status, host_settings):
        """Tracks up/down transitions for the outage log, fires any
        configured custom action command, and triggers sound/e-mail alerts
        per the global notification settings."""
        prev = self._prev_status.get(ip)
        is_up = status == "ONLINE"
        was_up = prev == "ONLINE"

        if prev is not None and is_up != was_up:
            now = datetime.now().isoformat(timespec="seconds")
            if not is_up:
                db.open_outage(ip, hostname, ts=now)
                self._run_custom_action(host_settings.get("on_down_cmd"), ip, hostname, "DOWN")
                self._fire_alerts(ip, hostname, "DOWN", now)
            else:
                db.close_outage(ip, ts=now)
                self._run_custom_action(host_settings.get("on_up_cmd"), ip, hostname, "UP")
                self._fire_alerts(ip, hostname, "UP", now)
        elif prev is None and not is_up:
            # First time we've ever seen this host and it's already down.
            db.open_outage(ip, hostname)

        self._prev_status[ip] = status

    def _fire_alerts(self, ip, hostname, event, when):
        if event == "DOWN" and self.notify_cfg.get("sound_on_down"):
            notifications.play_alert_sound("down")
        if event == "UP" and self.notify_cfg.get("sound_on_up"):
            notifications.play_alert_sound("up")

        want_tray = (event == "DOWN" and self.notify_cfg.get("tray_on_down", True)) or \
                    (event == "UP" and self.notify_cfg.get("tray_on_up", False))
        if want_tray and self.tray:
            self.tray.notify(f"{hostname} is {event}", f"{ip} changed state to {event} at {when}")

        want_email = (event == "DOWN" and self.notify_cfg.get("email_on_down")) or \
                     (event == "UP" and self.notify_cfg.get("email_on_up"))
        if want_email and self.smtp_cfg.get("host"):
            notifications.send_alert_email_async(
                self.smtp_cfg, hostname, ip, event, when,
                subject_tpl=self.notify_cfg.get("email_subject_tpl"),
                body_tpl=self.notify_cfg.get("email_body_tpl"),
            )

    def _run_custom_action(self, cmd, ip, hostname, event):
        if not cmd:
            return
        try:
            full_cmd = cmd.replace("{ip}", ip).replace("{hostname}", hostname).replace("{event}", event)
            subprocess.Popen(full_cmd, shell=True, **ping_engine.no_window_kwargs())
        except Exception:
            pass  # a broken custom action should never crash a scan

    def _update_stat_cards(self):
        online = sum(1 for r in self._last_results.values() if r["status"] == "ONLINE")
        offline = sum(1 for r in self._last_results.values() if r["status"] == "OFFLINE")
        errors = sum(1 for r in self._last_results.values() if r["status"] == "ERROR")
        self.stat_online_lbl.config(text=f"✔ Online  {online}")
        self.stat_offline_lbl.config(text=f"✖ Offline  {offline}")
        self.stat_error_lbl.config(text=f"⚠ Errors  {errors}")

        if self.tray:
            if offline > 0:
                self.tray.set_status("critical")
            elif errors > 0:
                self.tray.set_status("warning")
            elif online > 0:
                self.tray.set_status("good")
            else:
                self.tray.set_status("idle")

    def _on_scan_finished(self):
        self.is_scanning = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        online = sum(1 for r in self._last_results.values() if r["status"] == "ONLINE")
        offline = sum(1 for r in self._last_results.values() if r["status"] == "OFFLINE")
        errors = sum(1 for r in self._last_results.values() if r["status"] == "ERROR")
        self.status_var.set(
            f"Scan complete — Online: {online}  Offline: {offline}  Errors: {errors}  "
            f"(finished {datetime.now().strftime('%H:%M:%S')})")

        if self.continuous_var.get() and not self.stop_event.is_set():
            minutes = max(1, self.repeat_minutes_var.get())
            self.status_var.set(self.status_var.get() + f" | Next scan in {minutes} min")
            self.continuous_job = self.root.after(minutes * 60 * 1000, self.on_start_scan)

    def on_stop_scan(self):
        self.stop_event.set()
        if self.continuous_job:
            self.root.after_cancel(self.continuous_job)
            self.continuous_job = None
        self.status_var.set("Stopping... (finishing in-flight pings)")

    # -------------------------- Export / config --------------------------
    def on_export_results(self):
        if not self._last_results:
            messagebox.showwarning(APP_TITLE, "No results yet. Run a scan first.")
            return
        default_name = f"ping_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(title="Export results", initialfile=default_name,
                                             defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Hostname", "IP", "Avg RTT (ms)", "Last Checked", "Status", "Packet Loss (%)", "Quality"])
            for dev in self.devices:
                r = self._last_results.get(dev["ip"])
                if r:
                    host_settings = db.get_host_settings(dev["ip"])
                    q = quality.assess_quality(r["status"], r["avg_rtt"], r["loss"], host_settings.get("thresholds"))
                    writer.writerow([r["hostname"], r["ip"],
                                      r["avg_rtt"] if r["avg_rtt"] is not None else "",
                                      r["checked_at"], r["status"],
                                      r["loss"] if r["loss"] is not None else "", q])
        messagebox.showinfo(APP_TITLE, f"Results exported to:\n{path}")

    def _save_config(self):
        config = {
            "devices": self.devices,
            "count": self.count_var.get() if hasattr(self, "count_var") else 4,
            "timeout": self.timeout_var.get() if hasattr(self, "timeout_var") else 1.0,
            "workers": self.workers_var.get() if hasattr(self, "workers_var") else 10,
            "interval": self.interval_var.get() if hasattr(self, "interval_var") else 0.0,
            "precision": self.precision_var.get() if hasattr(self, "precision_var") else "standard",
            "smtp": self.smtp_cfg,
            "ftp": self.ftp_cfg,
            "notify": self.notify_cfg,
            "report_schedule": self.report_schedule,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
        except Exception:
            pass

    def _load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                self.devices = config.get("devices", [])
                self.count_var.set(config.get("count", 4))
                self.timeout_var.set(config.get("timeout", 1.0))
                self.workers_var.set(config.get("workers", 10))
                self.precision_var.set(config.get("precision", "standard"))
                self.interval_var.set(config.get("interval", 0.0))
                self.smtp_cfg.update(config.get("smtp", {}))
                self.ftp_cfg.update(config.get("ftp", {}))
                self.notify_cfg.update(config.get("notify", {}))
                self.report_schedule.update(config.get("report_schedule", {}))
                self._refresh_device_count()
                self._refresh_tree_from_devices()
            except Exception:
                pass
        if self.report_schedule.get("enabled"):
            self._restart_report_schedule()


def main():
    if HAS_BOOTSTRAP:
        root = tb.Window(themename="flatly")
    else:
        root = tb.Window()
    app = PingMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

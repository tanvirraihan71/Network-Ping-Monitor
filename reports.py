"""
reports.py
==========
Built-in reporting: generates HTML and PDF reports with per-host detailed
statistics (current status, quality, ping history summary, outage log),
and can deliver them by e-mail or upload them to an FTP server.

Report generation and delivery are decoupled from the GUI so they can be
triggered on demand or on a schedule identically.
"""

import ftplib
import os
import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import db
import quality

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    )
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------
def _gather_report_data(devices, last_results):
    """Builds a per-host summary combining live results with DB history/outages."""
    rows = []
    for dev in devices:
        ip, hostname = dev["ip"], dev["hostname"]
        r = last_results.get(ip, {})
        status = r.get("status", "UNKNOWN")
        rtt = r.get("avg_rtt")
        loss = r.get("loss")
        settings = db.get_host_settings(ip)
        q = quality.assess_quality(status, rtt, loss, settings.get("thresholds"))
        outages = db.get_outages(ip, limit=20)
        history = db.get_history(ip, limit=200)

        rtts = [h[2] for h in history if h[2] is not None]
        avg_hist_rtt = round(sum(rtts) / len(rtts), 2) if rtts else None
        up_count = sum(1 for h in history if h[1] == "ONLINE")
        total_count = len(history)
        uptime_pct = round(100 * up_count / total_count, 1) if total_count else None

        rows.append({
            "hostname": hostname, "ip": ip, "status": status, "quality": q,
            "rtt": rtt, "loss": loss, "checked_at": r.get("checked_at", "-"),
            "outage_count": len(outages), "uptime_pct": uptime_pct,
            "avg_hist_rtt": avg_hist_rtt, "history_samples": total_count,
            "outages": outages[:10],
        })
    return rows


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#f8fafc; color:#1e293b; margin:0; padding:24px; }}
  h1 {{ font-size: 20px; margin-bottom:2px; }}
  .subtitle {{ color:#64748b; font-size:13px; margin-bottom:20px; }}
  table {{ border-collapse: collapse; width:100%; background:#fff; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  th, td {{ padding:8px 10px; text-align:left; font-size:13px; border-bottom:1px solid #e2e8f0; }}
  th {{ background:#f1f5f9; color:#475569; font-weight:600; }}
  .GOOD {{ background:#e6f7ec; color:#15803d; font-weight:600; }}
  .WARNING {{ background:#fff6e5; color:#b45309; font-weight:600; }}
  .BAD {{ background:#ffe9e0; color:#c2410c; font-weight:600; }}
  .CRITICAL {{ background:#fdecec; color:#b91c1c; font-weight:600; }}
  .summary {{ display:flex; gap:12px; margin-bottom:18px; }}
  .pill {{ padding:10px 16px; border-radius:6px; font-weight:700; font-size:15px; }}
  .footer {{ margin-top:20px; color:#94a3b8; font-size:11px; }}
</style></head><body>
<h1>Network Ping Monitor — {title}</h1>
<div class="subtitle">Generated {generated_at}</div>
<div class="summary">{summary_pills}</div>
<table>
<tr><th>Hostname</th><th>IP</th><th>Status</th><th>Quality</th><th>Avg RTT (ms)</th>
<th>Loss (%)</th><th>Uptime %</th><th>Outages</th><th>Last Checked</th></tr>
{rows}
</table>
<div class="footer">Network Ping Monitor — built-in report</div>
</body></html>"""


def generate_html_report(devices, last_results, title="Ping Report"):
    rows_data = _gather_report_data(devices, last_results)
    online = sum(1 for r in rows_data if r["status"] == "ONLINE")
    offline = sum(1 for r in rows_data if r["status"] == "OFFLINE")
    errors = sum(1 for r in rows_data if r["status"] not in ("ONLINE", "OFFLINE"))

    pills = (
        f'<div class="pill" style="background:#eef2f7;color:#334155;">Total {len(rows_data)}</div>'
        f'<div class="pill" style="background:#e6f7ec;color:#15803d;">Online {online}</div>'
        f'<div class="pill" style="background:#fdecec;color:#b91c1c;">Offline {offline}</div>'
        f'<div class="pill" style="background:#fff6e5;color:#b45309;">Errors {errors}</div>'
    )

    row_html = []
    for r in rows_data:
        rtt_txt = "-" if r["rtt"] is None else f"{r['rtt']:.2f}"
        loss_txt = "-" if r["loss"] is None else f"{r['loss']:.0f}"
        uptime_txt = "-" if r["uptime_pct"] is None else f"{r['uptime_pct']:.1f}"
        row_html.append(
            f'<tr><td>{r["hostname"]}</td><td>{r["ip"]}</td>'
            f'<td class="{r["quality"]}">{r["status"]}</td>'
            f'<td class="{r["quality"]}">{r["quality"]}</td>'
            f'<td>{rtt_txt}</td><td>{loss_txt}</td><td>{uptime_txt}</td>'
            f'<td>{r["outage_count"]}</td><td>{r["checked_at"]}</td></tr>'
        )

    html = _HTML_TEMPLATE.format(
        title=title,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        summary_pills=pills,
        rows="\n".join(row_html),
    )
    return html


def save_html_report(devices, last_results, path, title="Ping Report"):
    html = generate_html_report(devices, last_results, title)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ---------------------------------------------------------------------------
# PDF report
# ---------------------------------------------------------------------------
def _quality_pdf_colors():
    return {
        "GOOD": colors.HexColor("#e6f7ec"),
        "WARNING": colors.HexColor("#fff6e5"),
        "BAD": colors.HexColor("#ffe9e0"),
        "CRITICAL": colors.HexColor("#fdecec"),
    }


def save_pdf_report(devices, last_results, path, title="Ping Report"):
    if not HAS_REPORTLAB:
        raise RuntimeError("PDF generation requires the 'reportlab' package, which isn't installed.")

    rows_data = _gather_report_data(devices, last_results)
    page_size = landscape(letter)
    left_margin = right_margin = 36
    doc = SimpleDocTemplate(path, pagesize=page_size, topMargin=36, bottomMargin=36,
                             leftMargin=left_margin, rightMargin=right_margin)
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8.5, leading=11, wordWrap="CJK")
    story = []

    story.append(Paragraph(f"Network Ping Monitor — {title}", styles["Title"]))
    story.append(Paragraph(f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Normal"]))
    story.append(Spacer(1, 14))

    online = sum(1 for r in rows_data if r["status"] == "ONLINE")
    offline = sum(1 for r in rows_data if r["status"] == "OFFLINE")
    errors = sum(1 for r in rows_data if r["status"] not in ("ONLINE", "OFFLINE"))
    story.append(Paragraph(
        f"Total: {len(rows_data)} &nbsp;&nbsp; Online: {online} &nbsp;&nbsp; "
        f"Offline: {offline} &nbsp;&nbsp; Errors: {errors}", styles["Heading3"]))
    story.append(Spacer(1, 10))

    header = ["Hostname", "IP", "Status", "Quality", "Avg RTT (ms)", "Loss (%)", "Uptime %", "Outages"]
    table_data = [header]
    row_colors = []
    qcolors = _quality_pdf_colors()
    for r in rows_data:
        rtt_txt = "-" if r["rtt"] is None else f"{r['rtt']:.2f}"
        loss_txt = "-" if r["loss"] is None else f"{r['loss']:.0f}"
        uptime_txt = "-" if r["uptime_pct"] is None else f"{r['uptime_pct']:.1f}"
        # Hostname and IP are wrapped in Paragraphs so long values wrap onto a
        # second line instead of forcing the column wider than the page.
        table_data.append([
            Paragraph(r["hostname"], cell_style), Paragraph(r["ip"], cell_style),
            r["status"], r["quality"], rtt_txt, loss_txt, uptime_txt, str(r["outage_count"]),
        ])
        row_colors.append(qcolors.get(r["quality"]))

    # Column widths sum exactly to the printable page width so nothing runs
    # off the edge - hostname/IP get the most room since they vary most.
    usable_width = page_size[0] - left_margin - right_margin
    col_fractions = [0.24, 0.16, 0.10, 0.11, 0.12, 0.09, 0.09, 0.09]
    col_widths = [usable_width * f for f in col_fractions]

    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475569")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTSIZE", (0, 1), (-1, -1), 8.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    for i, c in enumerate(row_colors, start=1):
        if c:
            style_cmds.append(("BACKGROUND", (0, i), (-1, i), c))
    tbl.setStyle(TableStyle(style_cmds))
    story.append(tbl)

    doc.build(story)
    return path


# ---------------------------------------------------------------------------
# Delivery: e-mail and FTP
# ---------------------------------------------------------------------------
def send_email_report(smtp_cfg, subject, body, attachment_paths=None):
    """
    smtp_cfg: dict with host, port, username, password, use_tls, from_addr, to_addrs (list)
    """
    msg = MIMEMultipart()
    msg["From"] = smtp_cfg["from_addr"]
    msg["To"] = ", ".join(smtp_cfg["to_addrs"])
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    for path in (attachment_paths or []):
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(path))
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
        msg.attach(part)

    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg["port"]), timeout=20) as server:
        if smtp_cfg.get("use_tls", True):
            server.starttls()
        if smtp_cfg.get("username"):
            server.login(smtp_cfg["username"], smtp_cfg["password"])
        server.sendmail(smtp_cfg["from_addr"], smtp_cfg["to_addrs"], msg.as_string())


def upload_ftp_report(ftp_cfg, local_path):
    """
    ftp_cfg: dict with host, port, username, password, remote_dir
    """
    filename = os.path.basename(local_path)
    with ftplib.FTP() as ftp:
        ftp.connect(ftp_cfg["host"], int(ftp_cfg.get("port", 21)), timeout=20)
        ftp.login(ftp_cfg.get("username", ""), ftp_cfg.get("password", ""))
        remote_dir = ftp_cfg.get("remote_dir") or ""
        if remote_dir:
            try:
                ftp.cwd(remote_dir)
            except ftplib.error_perm:
                ftp.mkd(remote_dir)
                ftp.cwd(remote_dir)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {filename}", f)

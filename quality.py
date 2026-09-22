"""
quality.py
==========
Connection-quality scoring: turns a raw ping result (status / RTT / loss)
into one of GOOD / WARNING / BAD / CRITICAL using customizable thresholds.
This is the same kind of real-time quality assessment used by commercial
ping-monitoring tools, just with simple, transparent rules you can tune.
"""

DEFAULT_THRESHOLDS = {
    "warning_rtt_ms": 100.0,   # at/above this RTT -> at least WARNING
    "bad_rtt_ms": 300.0,       # at/above this RTT -> at least BAD
    "warning_loss_pct": 1.0,   # at/above this loss% -> at least WARNING
    "bad_loss_pct": 20.0,      # at/above this loss% -> at least BAD
}

# Soft, light (bg, fg) colors consistent with the rest of the UI's palette.
QUALITY_COLORS = {
    "GOOD":     ("#e6f7ec", "#15803d"),
    "WARNING":  ("#fff6e5", "#b45309"),
    "BAD":      ("#ffe9e0", "#c2410c"),
    "CRITICAL": ("#fdecec", "#b91c1c"),
    "PENDING":  ("", ""),
}


def assess_quality(status, rtt_ms, loss_pct, thresholds=None):
    """
    status: 'ONLINE' / 'OFFLINE' / 'ERROR' / 'PENDING'
    rtt_ms: average round-trip time in ms (float) or None
    loss_pct: packet loss percentage (float) or None
    thresholds: optional dict overriding any of DEFAULT_THRESHOLDS' keys
    Returns: 'GOOD' | 'WARNING' | 'BAD' | 'CRITICAL' | 'PENDING'
    """
    if status == "PENDING":
        return "PENDING"
    if status != "ONLINE":
        return "CRITICAL"

    t = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        t.update({k: v for k, v in thresholds.items() if v is not None})

    loss_pct = loss_pct or 0.0
    rtt_ms = rtt_ms if rtt_ms is not None else 0.0

    if loss_pct >= t["bad_loss_pct"] or rtt_ms >= t["bad_rtt_ms"]:
        return "BAD"
    if loss_pct >= t["warning_loss_pct"] or rtt_ms >= t["warning_rtt_ms"]:
        return "WARNING"
    return "GOOD"

"""Runs scans (manual or scheduled) and turns their results into events."""
import os
import threading
import time
from datetime import datetime

from . import core, events, stack

SCAN_INTERVAL_MINUTES = max(0, int(os.getenv("MG_SCAN_INTERVAL_MINUTES", "360")))
SCAN_WINDOW = os.getenv("MG_SCAN_WINDOW", "").strip()
STARTUP_DELAY = max(0, int(os.getenv("MG_STARTUP_SCAN_DELAY", "120")))
TICK_SECONDS = 30


def parse_window(text):
    """'1-7' means scheduled scans may start from 01:00 up to 06:59 local time."""
    if not text:
        return None
    start, _, end = text.partition("-")
    start, end = int(start), int(end)
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        raise ValueError("MG_SCAN_WINDOW must look like 1-7 (start hour-end hour)")
    return start, end


WINDOW = parse_window(SCAN_WINDOW)


def in_window(hour, window=WINDOW):
    if window is None:
        return True
    start, end = window
    return start <= hour < end if start < end else hour >= start or hour < end


def report(summary):
    if summary["fatal"]:
        events.record("critical", "scan_failed", f"Scan failed: {summary['fatal']}")
        return
    if summary["missing_held"]:
        events.record(
            "critical", "mass_missing",
            f"{summary['missing_held']:,} files ({summary['missing_share']}% of the library) were not found. "
            "Check that the media share is mounted. They will be marked missing if the next scan "
            "also cannot find them.",
        )
    if summary["missing_marked"]:
        names = ", ".join(summary["missing_sample"])
        more = summary["missing_marked"] - len(summary["missing_sample"])
        events.record(
            "warning", "files_missing",
            f"{summary['missing_marked']:,} files went missing: {names}" + (f" and {more:,} more" if more > 0 else ""),
        )
    if summary["new_issues"]:
        names = ", ".join(summary["issue_sample"])
        events.record("warning", "new_issues", f"{summary['new_issues']:,} new file issues: {names}")
    if summary["errors"]:
        events.record(
            "info", "read_errors",
            f"{summary['errors']:,} files or folders could not be read: {summary['error_sample']}",
        )


def run_scan(trigger="manual", reserved=False):
    summary = core.scan_once(reserved=reserved, trigger=trigger)
    if summary:
        report(summary)
    return summary


def scan_due(now, started_at):
    if SCAN_INTERVAL_MINUTES == 0 or core.STATE["running"]:
        return False
    if not in_window(datetime.fromtimestamp(now).hour):
        return False
    last = core.STATE["last_finished"]
    if last is None:
        return now - started_at >= STARTUP_DELAY
    return now - last >= SCAN_INTERVAL_MINUTES * 60


def scheduler(stop):
    started_at = time.time()
    while not stop.wait(TICK_SECONDS):
        if scan_due(time.time(), started_at) and core.reserve_scan():
            run_scan("scheduled", reserved=True)


def schedule_info():
    return {
        "interval_minutes": SCAN_INTERVAL_MINUTES,
        "window": SCAN_WINDOW or None,
        "next_scan_after": (
            core.STATE["last_finished"] + SCAN_INTERVAL_MINUTES * 60
            if SCAN_INTERVAL_MINUTES and core.STATE["last_finished"] else None
        ),
    }


def start_background(stop):
    threads = [
        threading.Thread(target=scheduler, args=(stop,), name="mg-scheduler", daemon=True),
        threading.Thread(target=stack.monitor, args=(stop,), name="mg-stack", daemon=True),
    ]
    for thread in threads:
        thread.start()
    return threads

"""Runs scans (manual or scheduled) and turns their results into events."""
import os
import threading
import time
from datetime import datetime

from . import cleanup, core, crosscheck, events, stack

SCAN_INTERVAL_MINUTES = max(0, int(os.getenv("MG_SCAN_INTERVAL_MINUTES", "360")))
SCAN_WINDOW = os.getenv("MG_SCAN_WINDOW", "").strip()
STARTUP_DELAY = max(0, int(os.getenv("MG_STARTUP_SCAN_DELAY", "120")))
CROSSCHECK_MINUTES = max(0, int(os.getenv("MG_CROSSCHECK_MINUTES", "60")))
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
    for path, before, after in summary["shrunk"][:5]:
        events.record(
            "warning", "file_shrunk",
            f"{path} shrank from {size_text(before)} to {size_text(after)}. Check it still plays in full.",
        )
    unreadable = [f for f in summary["new_findings"] if f[0] == "unreadable"]
    if unreadable:
        events.record(
            "warning", "unreadable",
            f"{len(unreadable)} files or folders can't be read (permission denied), e.g. {unreadable[0][1]}",
        )
    leftovers = [f for f in summary["new_findings"] if f[0] == "shrinkarr_leftover"]
    if leftovers:
        events.record(
            "warning", "shrinkarr_leftover",
            f"{len(leftovers)} Shrinkarr leftover files found, e.g. {leftovers[0][1]}. "
            "A replacement may have been interrupted.",
        )
    clutter = [f for f in summary["new_findings"] if f[0] in cleanup.KINDS]
    if clutter:
        reclaim = sum(f[2] for f in clutter)
        events.record(
            "info", "cleanup_found",
            f"{len(clutter):,} new cleanup items ({size_text(reclaim)}). See Cleanup.",
        )
    if summary["errors"]:
        events.record(
            "info", "read_errors",
            f"{summary['errors']:,} files or folders could not be read: {summary['error_sample']}",
        )


def size_text(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"


def check_capacity():
    """Sample free space (at most hourly) and alert once when a volume runs low."""
    if not cleanup.sample_capacity():
        return
    for item in cleanup.capacity():
        key = f"disk_alert:{item['label']}"
        problem = cleanup.capacity_problem(item)
        with core.DB_LOCK, core.connect() as db:
            flagged = cleanup.get_flag(db, key)
            cleanup.set_flag(db, key, "1" if problem else "")
        if problem and not flagged:
            events.record("warning", "disk_low", problem)
        elif flagged and not problem:
            events.record("info", "disk_ok", f"{item['label']} has enough free space again")


def report_crosscheck(new):
    by_kind = {}
    for finding in new:
        by_kind.setdefault(finding[0], []).append(finding)
    for kind, level in (
        ("arr_missing_file", "warning"), ("server_missing_file", "warning"), ("arr_stuck_import", "warning"),
        ("arr_not_tracked", "info"), ("server_not_in_library", "info"),
    ):
        found = by_kind.get(kind)
        if found:
            first = found[0]
            example = first[1] if kind != "arr_stuck_import" else f"{first[1]} ({first[3]})"
            events.record(
                level, kind,
                f"{crosscheck.KINDS[kind]}: {len(found)} new, e.g. {example}"[:900],
            )
    for state in crosscheck.STATE["services"].values():
        key = f"crosscheck_error:{state['name']}"
        with core.DB_LOCK, core.connect() as db:
            previous = cleanup.get_flag(db, key)
            cleanup.set_flag(db, key, state["error"] or "")
        if state["error"] and not previous:
            events.record("warning", "crosscheck_failed", f"Couldn't check {state['name']}: {state['error']}")


def run_crosscheck():
    report_crosscheck(crosscheck.run())


def run_scan(trigger="manual", reserved=False):
    summary = core.scan_once(reserved=reserved, trigger=trigger)
    if summary:
        report(summary)
        if summary["complete"]:
            run_crosscheck()
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


def crosscheck_due(now):
    last = crosscheck.STATE["last_run"]
    return bool(CROSSCHECK_MINUTES) and core.STATE["last_finished"] is not None and (
        last is None or now - last >= CROSSCHECK_MINUTES * 60
    )


def scheduler(stop):
    started_at = time.time()
    while not stop.wait(TICK_SECONDS):
        try:
            check_capacity()
            if not core.STATE["running"] and crosscheck_due(time.time()):
                run_crosscheck()
        except Exception as exc:  # a failed check must not stop scheduled scans
            core.STATE["last_error"] = f"Background check failed: {exc}"[:500]
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

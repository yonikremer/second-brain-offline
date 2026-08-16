import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from helpers import db_conn
from rich.console import Console
from rich.live import Live
from rich.table import Table


def get_summary(db_path: Path) -> dict:
    conn = db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) AS cnt FROM files GROUP BY status")
        counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT filepath, status, updated_at FROM files "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        last = cursor.fetchone()
    finally:
        conn.close()

    terminal = {"processed", "filtered", "error", "needs_review", "skipped"}
    total = sum(counts.values())
    done = sum(counts.get(s, 0) for s in terminal)
    pending = counts.get("pending", 0)
    return {
        "total": total,
        "done": done,
        "pending": pending,
        "processed": counts.get("processed", 0),
        "filtered": counts.get("filtered", 0),
        "error": counts.get("error", 0),
        "needs_review": counts.get("needs_review", 0),
        "skipped": counts.get("skipped", 0),
        "last_file": last["filepath"] if last else None,
        "last_status": last["status"] if last else None,
    }


def _format_remaining(remaining: int, rate_per_sec: float) -> str:
    if rate_per_sec <= 0 or remaining <= 0:
        return "?"
    seconds = int(remaining / rate_per_sec)
    td = timedelta(seconds=seconds)
    return str(td).split(".")[0]


def render_table(summary: dict, elapsed: timedelta, eta_str: str | None) -> Table:
    table = Table(title="Pipeline Watch")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Elapsed", str(elapsed).split(".")[0])
    table.add_row("Total files", str(summary["total"]))
    table.add_row("Pending", str(summary["pending"]))
    table.add_row("Processed", str(summary["processed"]))
    table.add_row("Filtered", str(summary["filtered"]))
    table.add_row("Errors", str(summary["error"]))
    table.add_row("Needs review", str(summary["needs_review"]))
    table.add_row("Skipped", str(summary["skipped"]))
    table.add_row("Done", f"{summary['done']} / {summary['total']}")
    if summary["last_file"]:
        table.add_row("Last updated", f"{Path(summary['last_file']).name} ({summary['last_status']})")
    if summary["done"] and summary["total"]:
        rate = summary["done"] / summary["total"]
        table.add_row("Progress", f"{rate:.1%}")
    if eta_str is not None:
        table.add_row("ETA", eta_str)
    return table


def watch(db_path: Path, interval: int = 5):
    console = Console()
    start = datetime.now()
    last_done = 0
    last_time = start
    with Live(console=console, refresh_per_second=1) as live:
        while True:
            summary = get_summary(db_path)
            now = datetime.now()
            elapsed = now - start
            rate = 0.0
            if summary["done"] > last_done and (now - last_time).total_seconds() > 0:
                secs = (now - last_time).total_seconds()
                rate = (summary["done"] - last_done) / secs
            last_done = summary["done"]
            last_time = now
            remaining = summary["total"] - summary["done"]
            eta_str = _format_remaining(remaining, rate) if remaining > 0 else None
            live.update(render_table(summary, elapsed, eta_str))
            if summary["pending"] == 0 and summary["total"] > 0:
                console.print("\n[green]All files reached a terminal state.[/green]")
                break
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Live monitor for the document pipeline.")
    parser.add_argument("--db", type=Path, default=Path("pipeline.db"))
    parser.add_argument("--interval", type=int, default=5, help="Polling interval in seconds")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    try:
        watch(args.db, args.interval)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")


if __name__ == "__main__":
    main()

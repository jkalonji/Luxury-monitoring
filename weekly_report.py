"""Luxury Radar - weekly e-mail: the last 7 days compared with the 7 days before.

Usage:
  python weekly_report.py             # build and send
  python weekly_report.py --dry-run   # build only (preview in output/weekly.html)
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

from entities import EntityIndex
from mailer import build_weekly, deliver
from signals import yesterday
from store import get_store


def run(args: argparse.Namespace) -> int:
    store, index = get_store(), EntityIndex.load(args.entities)
    end_day = args.end_day or yesterday()
    start = (datetime.fromisoformat(end_day) - timedelta(days=6)).strftime("%Y-%m-%d")
    prev_start = (datetime.fromisoformat(end_day) - timedelta(days=13)).strftime("%Y-%m-%d")

    rows = store.load_articles(21)
    week = [r for r in rows if start <= r["published"][:10] <= end_day]
    prev = [r for r in rows if prev_start <= r["published"][:10] < start]
    if not week:
        logging.warning("No article stored for the week - no e-mail")
        return 0

    subject, html_body, text_body = build_weekly(
        end_day=end_day, rows=week, prev_rows=prev, signal_rows=store.load_signals(40),
        stories=store.all_stories(14), index=index, dashboard_url=os.environ.get("DASHBOARD_URL", ""))
    if not deliver(subject, html_body, text_body, "weekly", dry_run=args.dry_run):
        print("::error::weekly e-mail could not be sent")
        return 1
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--entities", default="entities.json")
    p.add_argument("--end-day", help="last day of the week, YYYY-MM-DD (default: yesterday)")
    p.add_argument("--dry-run", action="store_true", help="do not send the e-mail")
    return p.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    sys.exit(run(parse_args()))

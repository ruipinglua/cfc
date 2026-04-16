"""
Wheel Strategy Agent — entry point.

Starts a scheduler that:
  • Calls monitor_positions() every 15 minutes, Mon–Fri 9:30 AM – 4:00 PM ET.
  • Calls generate_daily_summary() at 4:05 PM ET every trading day.

Usage:
    pip install -r requirements.txt
    cp credentials.json.example credentials.json   # fill in your keys
    python wheel_agent.py
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler

from config import load_credentials
from wheel_strategy import WheelStrategy

# ------------------------------------------------------------------
# Logging: write to stdout and a daily log file
# ------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOG_DIR / f"wheel_{datetime.now().strftime('%Y%m%d')}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

EASTERN = pytz.timezone("America/New_York")


# ------------------------------------------------------------------
# Scheduled job wrappers
# ------------------------------------------------------------------

def _is_market_hours() -> bool:
    now = datetime.now(EASTERN)
    if now.weekday() >= 5:          # Saturday or Sunday
        return False
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close


def monitor_job(strategy: WheelStrategy):
    if not _is_market_hours():
        logger.debug("Outside market hours — skipping monitor tick.")
        return
    try:
        strategy.monitor_positions()
    except Exception as exc:
        logger.error("Unhandled error in monitor_job: %s", exc, exc_info=True)


def daily_summary_job(strategy: WheelStrategy):
    try:
        strategy.generate_daily_summary()
    except Exception as exc:
        logger.error("Unhandled error in daily_summary_job: %s", exc, exc_info=True)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    logger.info("Starting Wheel Strategy Agent...")

    creds = load_credentials()
    strategy = WheelStrategy(
        api_key   = creds["api_key"],
        api_secret= creds["api_secret"],
        base_url  = creds.get("base_url", "https://paper-api.alpaca.markets"),
        ticker    = creds.get("ticker", "TSLA"),
    )

    scheduler = BlockingScheduler(timezone=EASTERN)

    # ── Monitor every 15 minutes during regular trading hours ──────────
    scheduler.add_job(
        monitor_job,
        trigger="cron",
        args=[strategy],
        day_of_week="mon-fri",
        hour="9-15",          # 9:xx – 15:xx; the guard in monitor_job handles 9:30
        minute="*/15",
        id="wheel_monitor",
        name="15-min position monitor",
        misfire_grace_time=60,
    )

    # ── Extra run right at market open (9:31 AM) ───────────────────────
    scheduler.add_job(
        monitor_job,
        trigger="cron",
        args=[strategy],
        day_of_week="mon-fri",
        hour=9,
        minute=31,
        id="market_open_check",
        name="Market-open check",
    )

    # ── Daily summary at 4:05 PM ET ────────────────────────────────────
    scheduler.add_job(
        daily_summary_job,
        trigger="cron",
        args=[strategy],
        day_of_week="mon-fri",
        hour=16,
        minute=5,
        id="daily_summary",
        name="Daily summary",
    )

    logger.info(
        "Scheduler running. Monitoring %s every 15 min (9:30–4:00 PM ET, Mon–Fri). "
        "Press Ctrl+C to stop.",
        creds.get("ticker", "TSLA"),
    )

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Agent stopped by user.")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()

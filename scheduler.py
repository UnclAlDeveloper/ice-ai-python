import logging
import os
import subprocess
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from environments import load_environment

load_environment()

logger = logging.getLogger(__name__)


# RUN SCRAPER SUBPROCESS
def run_scraper_subprocess(script_name: str) -> int:
    """
    Invoke a scraper script as a fresh Python subprocess so each run has a
    clean Playwright/Chromium lifecycle and any crash in the scraper is
    isolated from the scheduler process. Output is inherited from this
    process so it surfaces in container logs alongside the scheduler's own
    messages. Returns the subprocess exit code.
    """

    script_path = os.path.join(os.path.dirname(__file__), script_name)
    logger.info("Starting %s via %s", script_name, script_path)

    # run synchronously; APScheduler executes jobs in its own worker thread, so
    # the scheduler itself keeps ticking while the scrape is in flight
    result = subprocess.run(
        [sys.executable, script_path],
        cwd=os.path.dirname(script_path) or ".",
        check=False,
    )

    if result.returncode == 0:
        logger.info("%s finished successfully", script_name)
    else:
        logger.error(
            "%s exited with non-zero status %s", script_name, result.returncode
        )

    return result.returncode


# RUN DAILY SCRAPES
def run_daily_scrapes() -> None:
    """
    Run the Autotrader classics, Car & Classic, and PistonHeads scrapes
    back-to-back under a single daily trigger. Later scrapes always run even
    if an earlier one exits non-zero, so a failure in one source never
    silently suppresses the others. All run sequentially because they launch
    non-headless Chromium and would fight over the same display if run
    concurrently.
    """

    # run the autotrader classics scrape first; ignore its exit code for
    # sequencing purposes so a failure there does not skip later scrapers
    run_scraper_subprocess("autotrader_classics.py")

    # then run the car & classic scrape; its exit code is logged inside the
    # helper, and any uncaught exception here is logged by apscheduler
    run_scraper_subprocess("car_and_classic.py")

    run_scraper_subprocess("pistonheads.py")


# MAIN
def main() -> int:
    """
    Entry point for the standalone scheduler process. Configures logging,
    registers the daily scrape chain (fires once between 06:00 and 07:00
    Europe/London time via APScheduler's jitter), and blocks until the
    process receives SIGINT or SIGTERM.
    """

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # scheduling disabled — uncomment below to re-enable daily scrapes between
    # 06:00 and 07:00 Europe/London (independent of ice_ai_api.py's scheduler)
    #
    # scheduler = BlockingScheduler(timezone="Europe/London")
    #
    # # jitter=3540 spreads the start uniformly across the next 59 minutes after
    # # 06:00, so the chain lands somewhere inside the 06:00-07:00 window each day
    # trigger = CronTrigger(hour=6, minute=0, timezone=scheduler.timezone)
    # scheduler.add_job(
    #     run_daily_scrapes,
    #     trigger,
    #     id="daily-scrapes",
    #     coalesce=True,
    #     max_instances=1,
    #     misfire_grace_time=3600,
    #     jitter=3540,
    # )
    #
    # # compute next fire time directly from the trigger because jobs added before
    # # the scheduler starts are still "pending" and their next_run_time attribute
    # # is not populated until scheduler.start() runs
    # next_run = trigger.get_next_fire_time(None, datetime.now(scheduler.timezone))
    # logger.info("Daily scrape chain scheduled; next run at %s", next_run)
    #
    # # blocks the main thread; raises KeyboardInterrupt on SIGINT for clean exit
    # try:
    #     scheduler.start()
    # except (KeyboardInterrupt, SystemExit):
    #     logger.info("Scheduler shutting down")

    logger.info("Daily scrape scheduler disabled; no jobs registered")

    return 0


if __name__ == "__main__":
    sys.exit(main())

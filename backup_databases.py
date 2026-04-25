"""
Nightly PostgreSQL backup job for the three Ice AI databases.

Dumps anna-trainer, auto-ads, and ice-ai (with -dev suffixes in staging) using
pg_dump in custom-compressed format, uploads each dump to the backups/ folder
of the bucket named by POSTGRESQL_BACKUPS_BUCKET, then prunes old dumps:
Sunday backups are kept for 14 days, weekday backups for 7 days.
"""

import logging
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from AWSAccess import AWSAccess
from environments import load_environment


logger = logging.getLogger(__name__)

DATABASE_URL_ENV_VARS = (
    "ANNA_TRAINER_DATABASE_URL",
    "AUTO_ADS_DATABASE_URL",
    "ICE_AI_DATABASE_URL",
)

BACKUPS_PREFIX = "backups/"

BACKUP_KEY_PATTERN = re.compile(r"^backups/(?P<dbname>.+)_(?P<date>\d{4}-\d{2}-\d{2})\.dump$")

SUNDAY_RETENTION_DAYS = 14

WEEKDAY_RETENTION_DAYS = 7


# DATABASE NAME FROM URL
def database_name_from_url(database_url: str) -> str:
    """
    Extract the database name from a PostgreSQL connection URL.

    The database name is the URL path component with the leading slash and any
    query string stripped, so postgresql://user:pw@host:5432/anna-trainer-dev?sslmode=require
    yields "anna-trainer-dev".
    """

    parsed = urlparse(database_url)
    name = parsed.path.lstrip("/")
    if not name:
        raise ValueError(f"Could not derive database name from URL: {database_url}")
    return name


# DUMP DATABASE
def dump_database(database_url: str, output_path: Path) -> None:
    """
    Run pg_dump against the given database URL and write a compressed custom-format
    archive to output_path.

    Uses -Fc (custom format, zlib-compressed) with -Z 9 for maximum compression.
    The connection details (including the password and sslmode) travel inline in
    the URL so no PGPASSWORD environment juggling is required.
    """

    # build the pg_dump command; pg_dump accepts a connection URI as a positional argument
    cmd = [
        "pg_dump",
        "--format=custom",
        "--compress=9",
        "--no-owner",
        "--no-privileges",
        "--file", str(output_path),
        database_url,
    ]

    # run pg_dump and surface its stderr in our logs if it fails
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"pg_dump failed (exit {result.returncode}): {result.stderr.strip()}"
        )


# BACKUP DATABASE
def backup_database(aws_access: AWSAccess, database_url: str, today: date) -> str:
    """
    Dump a single database to a temporary file, upload it to S3 under
    backups/<dbname>_YYYY-MM-DD.dump, and return the S3 key written.

    The temporary file lives in /tmp and is removed immediately after the upload
    completes (or fails) so dumps never accumulate on the container's local disk.
    """

    dbname = database_name_from_url(database_url)
    backup_key = f"{BACKUPS_PREFIX}{dbname}_{today:%Y-%m-%d}.dump"

    # use a NamedTemporaryFile we manage by hand so pg_dump can write to it
    temp = tempfile.NamedTemporaryFile(suffix=".dump", delete=False)
    temp_path = Path(temp.name)
    temp.close()

    try:
        # dump the database to the temp file
        logger.info("Dumping database %s to %s", dbname, temp_path)
        dump_database(database_url, temp_path)

        # upload the dump to s3
        logger.info("Uploading %s to s3://%s/%s", dbname, aws_access.bucket_name, backup_key)
        aws_access.upload_to_s3(backup_key, temp_path)
    finally:
        # always remove the temp file, even on failure
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove temp file %s", temp_path, exc_info=True)

    return backup_key


# PRUNE OLD BACKUPS
def prune_old_backups(aws_access: AWSAccess, today: date) -> int:
    """
    Delete backups in the bucket whose dated filenames are older than the retention
    window for their weekday. Sunday backups are retained for 14 days, all other
    days for 7 days. Returns the number of objects deleted.

    Anything under backups/ that does not match the dbname_YYYY-MM-DD.dump naming
    convention (for example, an accidental folder placeholder) is left untouched.
    """

    deleted = 0

    # iterate every object under the backups/ prefix
    for obj in aws_access.get_bucket().objects.filter(Prefix=BACKUPS_PREFIX):
        match = BACKUP_KEY_PATTERN.match(obj.key)
        if not match:
            # skip placeholders or anything that does not look like a dated dump
            continue

        # parse the date out of the filename
        try:
            file_date = datetime.strptime(match.group("date"), "%Y-%m-%d").date()
        except ValueError:
            logger.warning("Skipping backup with unparseable date: %s", obj.key)
            continue

        # sunday backups have a longer retention than weekday backups
        keep_days = SUNDAY_RETENTION_DAYS if file_date.weekday() == 6 else WEEKDAY_RETENTION_DAYS
        cutoff = today - timedelta(days=keep_days)

        if file_date < cutoff:
            logger.info("Deleting expired backup %s (age %d days)", obj.key, (today - file_date).days)
            aws_access.get_s3_object(obj.key).delete()
            deleted += 1

    return deleted


# RUN BACKUPS
def run_backups() -> None:
    """
    Entry point used by the APScheduler job. Backs up all three configured databases
    in turn, then prunes old dumps. A failure on one database is logged but does not
    stop the others; if any database failed, the function re-raises a single error
    at the end so the scheduler logs the failure.
    """

    bucket_name = os.getenv("POSTGRESQL_BACKUPS_BUCKET")
    if not bucket_name:
        raise RuntimeError(
            "POSTGRESQL_BACKUPS_BUCKET is not set; refusing to run nightly backups"
        )

    aws_access = AWSAccess(bucket_name=bucket_name)
    today = date.today()
    failures: list[tuple[str, Exception]] = []

    # back up each configured database in turn
    for env_var in DATABASE_URL_ENV_VARS:
        database_url = os.getenv(env_var)
        if not database_url:
            failures.append((env_var, RuntimeError(f"{env_var} is not set")))
            continue

        try:
            backup_database(aws_access, database_url, today)
        except Exception as exc:
            logger.exception("Backup failed for %s", env_var)
            failures.append((env_var, exc))

    # prune old backups even if some dumps failed so retention does not drift
    try:
        removed = prune_old_backups(aws_access, today)
        logger.info("Pruned %d expired backups", removed)
    except Exception as exc:
        logger.exception("Pruning old backups failed")
        failures.append(("prune_old_backups", exc))

    if failures:
        summary = ", ".join(f"{name}: {exc}" for name, exc in failures)
        raise RuntimeError(f"Nightly backup completed with errors: {summary}")


# MAIN
def main() -> None:
    """
    Allow ad-hoc invocation via 'python backup_databases.py' inside the container,
    primarily for testing or recovering from a missed schedule.
    """

    # configure logging so manual runs print progress to stdout
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    load_environment()
    run_backups()


if __name__ == "__main__":
    main()

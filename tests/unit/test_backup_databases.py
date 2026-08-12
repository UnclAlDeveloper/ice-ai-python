from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from backup_databases import database_name_from_url, prune_old_backups


class TestDatabaseNameFromUrl:
    """Tests for PostgreSQL URL database name extraction."""

    def test_extracts_name_from_path(self):
        url = "postgresql://user:pw@host:5432/anna-trainer-dev?sslmode=require"
        assert database_name_from_url(url) == "anna-trainer-dev"

    def test_raises_when_path_empty(self):
        with pytest.raises(ValueError, match="Could not derive database name"):
            database_name_from_url("postgresql://user:pw@host:5432/")


class TestPruneOldBackups:
    """Tests for S3 backup retention pruning."""

    def _make_aws_access(self, keys: list[str]):
        aws_access = MagicMock()
        objects = []
        for key in keys:
            obj = MagicMock()
            obj.key = key
            objects.append(obj)

        bucket = MagicMock()
        bucket.objects.filter.return_value = objects
        aws_access.get_bucket.return_value = bucket
        aws_access.get_s3_object.side_effect = lambda key: MagicMock(key=key)
        return aws_access

    def test_prunes_weekday_backups_older_than_seven_days(self):
        today = date(2026, 8, 12)  # Wednesday
        old_key = f"backups/auto-ads_{(today - timedelta(days=8)):%Y-%m-%d}.dump"
        recent_key = f"backups/auto-ads_{(today - timedelta(days=3)):%Y-%m-%d}.dump"
        aws_access = self._make_aws_access([old_key, recent_key])

        deleted = prune_old_backups(aws_access, today)

        assert deleted == 1
        aws_access.get_s3_object.assert_called_once_with(old_key)

    def test_sunday_backups_kept_for_fourteen_days(self):
        today = date(2026, 8, 12)  # Wednesday
        sunday = today - timedelta(days=10)
        while sunday.weekday() != 6:
            sunday -= timedelta(days=1)

        sunday_key = f"backups/auto-ads_{sunday:%Y-%m-%d}.dump"
        aws_access = self._make_aws_access([sunday_key])

        deleted = prune_old_backups(aws_access, today)

        assert deleted == 0
        aws_access.get_s3_object.assert_not_called()

    def test_ignores_non_matching_keys(self):
        today = date(2026, 8, 12)
        aws_access = self._make_aws_access(["backups/", "backups/manual-export.zip"])

        deleted = prune_old_backups(aws_access, today)

        assert deleted == 0

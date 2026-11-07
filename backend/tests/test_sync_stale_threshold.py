"""Enable Banking connections are read at most four times a day."""

from datetime import timedelta

from app.tasks.sync_tasks import STALE_THRESHOLD, STALE_THRESHOLD_BY_PROVIDER


def test_enable_banking_is_synced_at_most_four_times_a_day() -> None:
    assert STALE_THRESHOLD_BY_PROVIDER["enable_banking"] >= timedelta(hours=6)
    assert timedelta(hours=24) / STALE_THRESHOLD_BY_PROVIDER["enable_banking"] <= 4


def test_other_providers_keep_the_default() -> None:
    assert "pluggy" not in STALE_THRESHOLD_BY_PROVIDER
    assert STALE_THRESHOLD == timedelta(hours=4)

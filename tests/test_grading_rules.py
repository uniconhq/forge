"""The rules a grading run keeps to, without a database: a failed start
waits longer each time.
"""

from datetime import timedelta

from forge.domain.grading import start_retry_wait


def test_a_failed_start_waits_longer_each_time_up_to_five_minutes() -> None:
    waits = [start_retry_wait(failures).total_seconds() for failures in range(1, 9)]
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300]
    assert start_retry_wait(10_000) == timedelta(minutes=5)

"""
Known bad-data windows, shared by the live service (main.py's /scoreboard)
and the offline scripts, so both leave out the same periods.

COLLECTOR_BACKLOGS - periods when the backend's RabbitMQ consumer fell
behind. vehicle_position_snapshots.recorded_at is set when the consumer
saves a row (VehiclePositionConsumer: Instant.now()), not when BKK
reported the position, so during a backlog every arrival is recorded late
- by up to ~19 min on 26 Sep (median lag vs BKK's last_update_time:
normally ~10 s). Found with scripts/collector_lag_scan.py, which scanned
the whole table (26 Aug - 26 Sep): this is the only real episode. Re-run
it and add new episodes here.
"""

COLLECTOR_BACKLOGS = [
    ("2026-09-26 16:26:00+02:00", "2026-09-26 16:58:00+02:00"),
]


def backlog_sql_filter(time_columns: list[str]) -> str:
    """SQL condition that is true when none of time_columns falls inside a
    backlog - for a WHERE clause (the windows are constants, not user input)."""
    parts = [f"{col} BETWEEN '{start}' AND '{end}'"
             for start, end in COLLECTOR_BACKLOGS for col in time_columns]
    return f"NOT ({' OR '.join(parts)})" if parts else "TRUE"

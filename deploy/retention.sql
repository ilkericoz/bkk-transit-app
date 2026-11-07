-- Run daily by the retention container. :days comes from RETENTION_DAYS.
-- The scoreboard's partial index was created by hand at home (Hibernate only
-- makes the two @Index ones), so make sure it exists here too.
CREATE INDEX IF NOT EXISTS idx_snapshot_scoreboard_join ON vehicle_position_snapshots
    (trip_id, stop_id, stop_sequence, service_date, recorded_at)
    WHERE status = 'STOPPED_AT' AND stop_distance_percent = 100;
DELETE FROM vehicle_position_snapshots WHERE recorded_at < now() - make_interval(days => :days);
DELETE FROM stop_predictions        WHERE reference_time < now() - interval '30 days';
DELETE FROM long_range_predictions  WHERE reference_time < now() - interval '30 days';
DELETE FROM prediction_log          WHERE predicted_at   < now() - interval '30 days';
DELETE FROM bkk_stop_samples        WHERE fetched_at     < now() - interval '30 days';
VACUUM (ANALYZE) vehicle_position_snapshots;

package hu.elte.bkktransit.service;

/**
 * The handful of fields our map actually needs from BKK's real-time feed —
 * not a 1:1 mirror of their response, which has a lot more we don't use.
 *
 * tripId/stopId/stopSequence/serviceDate/status/stopDistancePercent were
 * added for stage 5 (delay prediction) — the map itself doesn't use them,
 * but without tripId there's no way to join a sighting back to a specific
 * scheduled trip in static GTFS stop_times.txt to compute a delay label.
 * status/stopDistancePercent let us detect "vehicle was physically AT this
 * stop just now" (STOPPED_AT, 100%) as the ground-truth actual-arrival
 * event, rather than trusting BKK's own predicted times.
 *
 * deviated/stale added after checking BKK's raw response for fields we
 * were silently dropping: deviated (off-route) is a plausible real delay
 * predictor and always present; stale is rare but marks a sighting BKK
 * itself considers unreliable - worth being able to exclude those from
 * future label-building rather than treating every sighting as equally
 * trustworthy.
 */
public record VehiclePosition(
        String vehicleId,
        String routeId,
        String vehicleRouteType,
        double lat,
        double lon,
        Double bearing,
        String label,
        long lastUpdateTime,
        String tripId,
        String stopId,
        Integer stopSequence,
        String serviceDate,
        String status,
        Integer stopDistancePercent,
        Boolean deviated,
        Boolean stale
) {
}

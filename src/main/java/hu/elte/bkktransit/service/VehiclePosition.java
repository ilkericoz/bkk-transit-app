package hu.elte.bkktransit.service;

/**
 * The handful of fields our map actually needs from BKK's real-time feed —
 * not a 1:1 mirror of their response, which has a lot more we don't use.
 */
public record VehiclePosition(
        String vehicleId,
        String routeId,
        String vehicleRouteType,
        double lat,
        double lon,
        Double bearing,
        String label,
        long lastUpdateTime
) {
}

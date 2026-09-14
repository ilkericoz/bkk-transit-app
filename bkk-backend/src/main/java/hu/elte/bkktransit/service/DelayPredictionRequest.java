package hu.elte.bkktransit.service;

/**
 * What the frontend POSTs to /api/vehicles/delay-prediction — just the
 * handful of a VehiclePosition's own fields the delay model needs, echoed
 * straight back to us since app.js already has them in memory from its
 * last /api/vehicles poll. No lookup needed on our side to build this.
 */
public record DelayPredictionRequest(
        String tripId,
        String routeId,
        String stopId,
        String vehicleRouteType,
        int stopSequence,
        String serviceDate,
        Boolean deviated
) {
}

package hu.elte.bkktransit.service;

import java.util.List;

/**
 * What the frontend POSTs to /api/vehicles/current-delays - every tripId
 * currently on the map, in one batch, so the map's real-time coloring
 * feed is one call per poll instead of one per vehicle.
 */
public record CurrentDelaysRequest(List<String> tripIds, String serviceDate) {
}

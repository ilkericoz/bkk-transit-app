package hu.elte.bkktransit.service;

/**
 * One vehicle's most recent CONFIRMED delay today (real ground truth, not
 * a prediction) - see VehicleController's /current-delays, the map's
 * real-time coloring feed (added 2026-09-14).
 */
public record CurrentDelay(double delaySeconds, double minutesAgo) {
}

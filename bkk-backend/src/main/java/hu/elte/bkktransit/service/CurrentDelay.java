package hu.elte.bkktransit.service;

/**
 * One vehicle's most recent CONFIRMED delay today (real ground truth, not
 * a prediction) - see VehicleController's /current-delays, the map's
 * real-time coloring feed (added 2026-09-14). staleAfterMinutes (added
 * 2026-09-24): the map shows this delay only while minutesAgo is at most
 * this - computed by the delay service from the timetable, so a vehicle on
 * a long stretch between stops keeps its color longer.
 */
public record CurrentDelay(double delaySeconds, double minutesAgo, double staleAfterMinutes) {
}

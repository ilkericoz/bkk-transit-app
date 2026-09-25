package hu.elte.bkktransit.service;

/**
 * One vehicle's most recent CONFIRMED delay today (real ground truth, not
 * a prediction) - see VehicleController's /current-delays, the map's
 * real-time coloring feed (added 2026-09-14). staleAfterMinutes (added
 * 2026-09-24): the map shows this delay only while minutesAgo is at most
 * this - computed by the delay service from the timetable, so a vehicle on
 * a long stretch between stops keeps its color longer. predictionError*
 * (added 2026-09-25, null until graded): predicted minus actual delay of
 * this trip's latest graded every-stop prediction, for the map's accuracy mode.
 */
public record CurrentDelay(double delaySeconds, double minutesAgo, double staleAfterMinutes,
                           Double predictionErrorSeconds, Double predictionGradedMinutesAgo,
                           String stopId, String gradedStopId, Double gradedPredictedSeconds,
                           Double gradedActualSeconds) {
}

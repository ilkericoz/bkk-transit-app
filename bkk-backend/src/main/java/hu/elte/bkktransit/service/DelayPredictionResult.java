package hu.elte.bkktransit.service;

/**
 * What VehicleController hands back to the browser for a delay-prediction
 * request. available=false is the *expected* outcome for a real slice of
 * vehicles — ones not on the imported static GTFS schedule (MÁV-START/
 * Volánbusz vehicles BKK's live feed surfaces, or ones the sidecar's
 * schedule snapshot doesn't cover — see the 2026-08-28 route-name
 * investigation and the 2026-09-12 stale-feed finding) — so it's modeled
 * as a normal response, not an HTTP error the frontend has to branch on.
 */
public record DelayPredictionResult(boolean available, Double predictedDelaySeconds, LastConfirmedDelay lastConfirmedDelay) {

    /**
     * This trip's last CONFIRMED delay — genuine ground truth (an actual
     * observed arrival), not a prediction — shown next to the predicted
     * delay so the map can display both together for an easy sanity-check
     * comparison. Null minutesAgo only happens for a manually-supplied
     * value from the sidecar's /predict testing endpoint, not a live one.
     */
    public record LastConfirmedDelay(double delaySeconds, Double minutesAgo) {
    }

    public static DelayPredictionResult unavailable() {
        return new DelayPredictionResult(false, null, null);
    }

    public static DelayPredictionResult of(double predictedDelaySeconds, LastConfirmedDelay lastConfirmedDelay) {
        return new DelayPredictionResult(true, predictedDelaySeconds, lastConfirmedDelay);
    }
}

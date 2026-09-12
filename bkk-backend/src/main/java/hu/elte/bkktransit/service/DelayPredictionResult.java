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
public record DelayPredictionResult(boolean available, Double predictedDelaySeconds) {

    public static DelayPredictionResult unavailable() {
        return new DelayPredictionResult(false, null);
    }

    public static DelayPredictionResult of(double predictedDelaySeconds) {
        return new DelayPredictionResult(true, predictedDelaySeconds);
    }
}

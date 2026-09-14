package hu.elte.bkktransit.service;

import java.util.List;

/**
 * Live "how good are we actually doing" stats - reconciles predictions
 * we've already logged against what the vehicle actually did, computed by
 * the sidecar (see main.py's /scoreboard) at read time, not a background
 * job. Genuine ground truth, not another prediction - the point is to
 * give a real, continuously-updating accuracy number rather than only
 * "does one prediction look plausible."
 */
public record PredictionScoreboard(
        int reconciledCount,
        Double meanAbsoluteErrorSeconds,
        List<Entry> recent
) {

    public record Entry(
            String routeId,
            String vehicleRouteType,
            double predictedDelaySeconds,
            double actualDelaySeconds,
            double errorSeconds,
            String predictedAt
    ) {
    }
}

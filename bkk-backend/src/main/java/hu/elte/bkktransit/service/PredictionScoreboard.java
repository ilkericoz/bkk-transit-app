package hu.elte.bkktransit.service;

import java.util.List;

/**
 * Live "how good are we actually doing" stats for the model currently
 * deployed, computed by the sidecar (see main.py's /scoreboard) from its
 * every-stop job: every vehicle's next stop is predicted when its previous
 * stop is confirmed and graded when it arrives. Genuine ground truth, not
 * another prediction.
 *
 * persistenceMaeSeconds is the "no model" yardstick on the same
 * predictions - the error of just assuming the delay at the previous stop
 * stays the same.
 *
 * Until 2026-09-26 this was built from clicked/randomly sampled
 * predictions of every model since the start (now main.py's
 * /scoreboard/sampled), which showed ~52 s for a model that is ~27 s
 * measured this way - see the README changelog.
 */
public record PredictionScoreboard(
        String modelVersion,
        String since,
        int gradedCount,
        Double meanAbsoluteErrorSeconds,
        Double within60sShare,
        Double persistenceMaeSeconds,
        List<Group> byVehicleType
) {

    public record Group(
            String vehicleRouteType,
            int gradedCount,
            double meanAbsoluteErrorSeconds,
            double within60sShare,
            double persistenceMaeSeconds
    ) {
    }
}

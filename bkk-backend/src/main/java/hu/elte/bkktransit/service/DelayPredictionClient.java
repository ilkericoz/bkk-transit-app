package hu.elte.bkktransit.service;

import com.fasterxml.jackson.annotation.JsonProperty;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.client.JdkClientHttpRequestFactory;
import org.springframework.stereotype.Service;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.RestClient;

import java.net.http.HttpClient;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * Talks to the Python/FastAPI delay-prediction sidecar (stage 5) — the
 * piece that actually wires the two halves of the polyglot architecture
 * together. Up to now they were two services that each worked standalone;
 * nothing called /predict.
 *
 * Same "own RestClient.builder(), don't rely on an injected Builder bean"
 * pattern as FutarClient, for the same reason (Boot 4 split RestClient
 * auto-configuration into its own opt-in starter, not on this classpath).
 */
@Service
public class DelayPredictionClient {

    private final RestClient restClient;

    public DelayPredictionClient(@Value("${bkk.delay-service.base-url}") String baseUrl) {
        // Pinned to HTTP/1.1 - the JDK HttpClient RestClient uses by default
        // otherwise tries an HTTP/2 cleartext ("h2c") upgrade against this
        // plain http:// sidecar. FutarClient never hits this because BKK's
        // endpoint is HTTPS (ALPN negotiates the version cleanly there);
        // uvicorn only speaks HTTP/1.1 and doesn't support the upgrade
        // request, which corrupted every call until pinned (confirmed via
        // uvicorn's own "Unsupported upgrade request" log line).
        HttpClient httpClient = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .build();
        this.restClient = RestClient.builder()
                .baseUrl(baseUrl)
                .requestFactory(new JdkClientHttpRequestFactory(httpClient))
                .build();
    }

    /**
     * FastAPI's request/response fields are snake_case (Python/pydantic
     * convention) — these two records exist only to speak that shape over
     * the wire, so the rest of this Java codebase's camelCase convention
     * doesn't have to bend for one upstream call.
     */
    private record UpstreamRequest(
            @JsonProperty("trip_id") String tripId,
            @JsonProperty("route_id") String routeId,
            @JsonProperty("stop_id") String stopId,
            @JsonProperty("vehicle_route_type") String vehicleRouteType,
            @JsonProperty("stop_sequence") int stopSequence,
            @JsonProperty("service_date") String serviceDate,
            @JsonProperty("deviated") boolean deviated
    ) {
    }

    private record UpstreamResponse(
            @JsonProperty("predicted_delay_seconds") double predictedDelaySeconds,
            @JsonProperty("last_confirmed_delay") UpstreamDelayReading lastConfirmedDelay
    ) {
    }

    private record UpstreamDelayReading(
            @JsonProperty("delay_seconds") double delaySeconds,
            @JsonProperty("minutes_ago") Double minutesAgo
    ) {
    }

    /**
     * Predicts the delay for one specific vehicle's current stop visit,
     * using exactly the fields BKK's live feed already gives us for it
     * (see VehiclePosition) — the sidecar resolves the scheduled arrival
     * itself from its own in-memory GTFS schedule index, since this side
     * never imported stop_times.txt (387MB) into Postgres.
     *
     * Returns "unavailable" — not an error — for the expected 404 case
     * where the sidecar can't find this trip/stop on its static schedule.
     * An actual transport failure (sidecar down, unreachable) is left to
     * propagate as an exception so a 503-ish response reaches the browser
     * instead of being silently swallowed as "unavailable" — the two mean
     * different things and the frontend/logs should be able to tell them
     * apart.
     */
    public DelayPredictionResult predict(DelayPredictionRequest request) {
        UpstreamRequest upstreamRequest = new UpstreamRequest(
                request.tripId(), request.routeId(), request.stopId(),
                request.vehicleRouteType(), request.stopSequence(), request.serviceDate(),
                Boolean.TRUE.equals(request.deviated()));

        try {
            UpstreamResponse response = restClient.post()
                    .uri("/predict/from-vehicle")
                    .body(upstreamRequest)
                    .retrieve()
                    .body(UpstreamResponse.class);
            DelayPredictionResult.LastConfirmedDelay lastConfirmedDelay = response.lastConfirmedDelay() == null
                    ? null
                    : new DelayPredictionResult.LastConfirmedDelay(
                            response.lastConfirmedDelay().delaySeconds(), response.lastConfirmedDelay().minutesAgo());
            return DelayPredictionResult.of(response.predictedDelaySeconds(), lastConfirmedDelay);
        } catch (HttpClientErrorException.NotFound e) {
            return DelayPredictionResult.unavailable();
        }
    }

    private record UpstreamScoreboardResponse(
            @JsonProperty("model_version") String modelVersion,
            @JsonProperty("since") String since,
            @JsonProperty("graded_count") int gradedCount,
            @JsonProperty("mean_absolute_error_seconds") Double meanAbsoluteErrorSeconds,
            @JsonProperty("within_60s_share") Double within60sShare,
            @JsonProperty("persistence_mae_seconds") Double persistenceMaeSeconds,
            @JsonProperty("accurate_share") Double accurateShare,
            @JsonProperty("persistence_accurate_share") Double persistenceAccurateShare,
            @JsonProperty("by_vehicle_type") List<UpstreamScoreboardGroup> byVehicleType
    ) {
    }

    private record UpstreamScoreboardGroup(
            @JsonProperty("vehicle_route_type") String vehicleRouteType,
            @JsonProperty("graded_count") int gradedCount,
            @JsonProperty("mean_absolute_error_seconds") double meanAbsoluteErrorSeconds,
            @JsonProperty("within_60s_share") double within60sShare,
            @JsonProperty("persistence_mae_seconds") double persistenceMaeSeconds
    ) {
    }

    /**
     * Live "how good are we actually doing" stats - see PredictionScoreboard.
     * A thin proxy, same as everything else here: the sidecar computes the
     * numbers, this just reshapes its response into this codebase's
     * camelCase convention.
     */
    public PredictionScoreboard scoreboard() {
        UpstreamScoreboardResponse response = restClient.get()
                .uri("/scoreboard")
                .retrieve()
                .body(UpstreamScoreboardResponse.class);

        List<PredictionScoreboard.Group> groups = response.byVehicleType().stream()
                .map(g -> new PredictionScoreboard.Group(
                        g.vehicleRouteType(), g.gradedCount(), g.meanAbsoluteErrorSeconds(),
                        g.within60sShare(), g.persistenceMaeSeconds()))
                .toList();
        return new PredictionScoreboard(response.modelVersion(), response.since(), response.gradedCount(),
                response.meanAbsoluteErrorSeconds(), response.within60sShare(), response.persistenceMaeSeconds(),
                response.accurateShare(), response.persistenceAccurateShare(), groups);
    }

    private record UpstreamCurrentDelaysRequest(
            @JsonProperty("trip_ids") List<String> tripIds,
            @JsonProperty("service_date") String serviceDate
    ) {
    }

    private record UpstreamCurrentDelayReading(
            @JsonProperty("delay_seconds") double delaySeconds,
            @JsonProperty("minutes_ago") double minutesAgo,
            @JsonProperty("stale_after_minutes") double staleAfterMinutes,
            @JsonProperty("prediction_error_seconds") Double predictionErrorSeconds,
            @JsonProperty("prediction_graded_minutes_ago") Double predictionGradedMinutesAgo,
            @JsonProperty("stop_id") String stopId,
            @JsonProperty("graded_stop_id") String gradedStopId,
            @JsonProperty("graded_predicted_seconds") Double gradedPredictedSeconds,
            @JsonProperty("graded_actual_seconds") Double gradedActualSeconds,
            @JsonProperty("prediction_accurate") Boolean predictionAccurate
    ) {
    }

    private record UpstreamCurrentDelaysResponse(
            @JsonProperty("delays") Map<String, UpstreamCurrentDelayReading> delays
    ) {
    }

    /**
     * Each vehicle's most recent CONFIRMED delay today, for potentially
     * hundreds of vehicles in one call - the map's real-time coloring feed
     * (added 2026-09-14). A tripId absent from the returned map means "no
     * confirmed arrival for it yet today," not an error - the caller
     * should render that as "no data yet," not a failure.
     */
    public Map<String, CurrentDelay> currentDelays(List<String> tripIds, String serviceDate) {
        UpstreamCurrentDelaysResponse response = restClient.post()
                .uri("/vehicles/current-delays")
                .body(new UpstreamCurrentDelaysRequest(tripIds, serviceDate))
                .retrieve()
                .body(UpstreamCurrentDelaysResponse.class);

        return response.delays().entrySet().stream()
                .collect(Collectors.toMap(
                        Map.Entry::getKey,
                        e -> new CurrentDelay(e.getValue().delaySeconds(), e.getValue().minutesAgo(),
                                e.getValue().staleAfterMinutes(), e.getValue().predictionErrorSeconds(),
                                e.getValue().predictionGradedMinutesAgo(), e.getValue().stopId(),
                                e.getValue().gradedStopId(), e.getValue().gradedPredictedSeconds(),
                                e.getValue().gradedActualSeconds(), e.getValue().predictionAccurate())));
    }
}

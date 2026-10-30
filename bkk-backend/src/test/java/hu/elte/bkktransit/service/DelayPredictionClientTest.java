package hu.elte.bkktransit.service;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * Tests the client against a tiny stub HTTP server standing in for the
 * Python sidecar, so the real RestClient, JSON (de)serialization and
 * snake_case mapping all run - only the sidecar itself is fake.
 */
class DelayPredictionClientTest {

    private final ObjectMapper mapper = new ObjectMapper();
    private HttpServer server;
    private DelayPredictionClient client;

    // What the stub received on its last request, and what it answers with.
    private final AtomicReference<String> lastRequestBody = new AtomicReference<>();
    private final AtomicReference<String> lastRequestPath = new AtomicReference<>();
    private volatile int responseStatus = 200;
    private volatile String responseBody = "{}";

    @BeforeEach
    void startStubSidecar() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/", exchange -> {
            lastRequestPath.set(exchange.getRequestURI().getPath());
            lastRequestBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] body = responseBody.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(responseStatus, body.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(body);
            }
        });
        server.start();
        client = new DelayPredictionClient("http://127.0.0.1:" + server.getAddress().getPort());
    }

    @AfterEach
    void stopStubSidecar() {
        server.stop(0);
    }

    private DelayPredictionRequest sampleRequest(Boolean deviated) {
        return new DelayPredictionRequest("BKK_T1", "BKK_3060", "BKK_F00001", "TRAM", 7, "20261030", deviated);
    }

    @Test
    void predictSendsSnakeCaseRequestToTheSidecar() {
        responseBody = """
                {"predicted_delay_seconds": 42.0, "last_confirmed_delay": null}
                """;

        client.predict(sampleRequest(true));

        assertThat(lastRequestPath.get()).isEqualTo("/predict/from-vehicle");
        JsonNode sent = mapper.readTree(lastRequestBody.get());
        assertThat(sent.path("trip_id").asText()).isEqualTo("BKK_T1");
        assertThat(sent.path("route_id").asText()).isEqualTo("BKK_3060");
        assertThat(sent.path("stop_id").asText()).isEqualTo("BKK_F00001");
        assertThat(sent.path("vehicle_route_type").asText()).isEqualTo("TRAM");
        assertThat(sent.path("stop_sequence").asInt()).isEqualTo(7);
        assertThat(sent.path("service_date").asText()).isEqualTo("20261030");
        assertThat(sent.path("deviated").asBoolean()).isTrue();
    }

    @Test
    void predictTreatsMissingDeviatedFlagAsFalse() {
        responseBody = """
                {"predicted_delay_seconds": 0.0, "last_confirmed_delay": null}
                """;

        client.predict(sampleRequest(null));

        JsonNode sent = mapper.readTree(lastRequestBody.get());
        assertThat(sent.path("deviated").isBoolean()).isTrue();
        assertThat(sent.path("deviated").asBoolean()).isFalse();
    }

    @Test
    void predictMapsThePredictionAndLastConfirmedDelay() {
        responseBody = """
                {"predicted_delay_seconds": 95.5,
                 "last_confirmed_delay": {"delay_seconds": 80.0, "minutes_ago": 3.5}}
                """;

        DelayPredictionResult result = client.predict(sampleRequest(false));

        assertThat(result.available()).isTrue();
        assertThat(result.predictedDelaySeconds()).isEqualTo(95.5);
        assertThat(result.lastConfirmedDelay().delaySeconds()).isEqualTo(80.0);
        assertThat(result.lastConfirmedDelay().minutesAgo()).isEqualTo(3.5);
    }

    @Test
    void predictReturnsNullLastConfirmedDelayWhenTheSidecarHasNone() {
        responseBody = """
                {"predicted_delay_seconds": 12.0, "last_confirmed_delay": null}
                """;

        DelayPredictionResult result = client.predict(sampleRequest(false));

        assertThat(result.available()).isTrue();
        assertThat(result.lastConfirmedDelay()).isNull();
    }

    @Test
    void predictReturnsUnavailableWhenTheTripIsNotOnTheSchedule() {
        // 404 means "this trip/stop isn't in the static schedule" - an
        // expected answer, not a failure.
        responseStatus = 404;
        responseBody = """
                {"detail": "trip not found"}
                """;

        DelayPredictionResult result = client.predict(sampleRequest(false));

        assertThat(result.available()).isFalse();
        assertThat(result.predictedDelaySeconds()).isNull();
        assertThat(result.lastConfirmedDelay()).isNull();
    }

    @Test
    void predictLetsRealServerErrorsPropagate() {
        // A 500 must not be swallowed as "unavailable": the frontend and
        // logs need to tell "no schedule" apart from "sidecar is broken".
        responseStatus = 500;
        responseBody = "{}";

        assertThatThrownBy(() -> client.predict(sampleRequest(false)))
                .isInstanceOf(org.springframework.web.client.HttpServerErrorException.class);
    }

    @Test
    void predictFailsLoudlyWhenTheSidecarIsDown() {
        server.stop(0);

        assertThatThrownBy(() -> client.predict(sampleRequest(false)))
                .isInstanceOf(org.springframework.web.client.ResourceAccessException.class);
    }

    @Test
    void scoreboardMapsOverallAndPerVehicleTypeNumbers() {
        responseBody = """
                {"model_version": "linear-v3", "since": "2026-09-26",
                 "graded_count": 1200, "mean_absolute_error_seconds": 27.4,
                 "within_60s_share": 0.91, "persistence_mae_seconds": 33.0,
                 "accurate_share": 0.946, "persistence_accurate_share": 0.903,
                 "by_vehicle_type": [
                   {"vehicle_route_type": "BUS", "graded_count": 800,
                    "mean_absolute_error_seconds": 30.1, "within_60s_share": 0.89,
                    "persistence_mae_seconds": 36.2},
                   {"vehicle_route_type": "TRAM", "graded_count": 400,
                    "mean_absolute_error_seconds": 22.0, "within_60s_share": 0.95,
                    "persistence_mae_seconds": 26.8}
                 ]}
                """;

        PredictionScoreboard board = client.scoreboard();

        assertThat(lastRequestPath.get()).isEqualTo("/scoreboard");
        assertThat(board.modelVersion()).isEqualTo("linear-v3");
        assertThat(board.since()).isEqualTo("2026-09-26");
        assertThat(board.gradedCount()).isEqualTo(1200);
        assertThat(board.meanAbsoluteErrorSeconds()).isEqualTo(27.4);
        assertThat(board.within60sShare()).isEqualTo(0.91);
        assertThat(board.persistenceMaeSeconds()).isEqualTo(33.0);
        assertThat(board.accurateShare()).isEqualTo(0.946);
        assertThat(board.persistenceAccurateShare()).isEqualTo(0.903);
        assertThat(board.byVehicleType())
                .extracting(PredictionScoreboard.Group::vehicleRouteType)
                .containsExactly("BUS", "TRAM");
        assertThat(board.byVehicleType().get(1).meanAbsoluteErrorSeconds()).isEqualTo(22.0);
    }

    @Test
    void scoreboardKeepsNullMetricsWhenNothingHasBeenGradedYet() {
        responseBody = """
                {"model_version": "linear-v3", "since": "2026-10-30",
                 "graded_count": 0, "mean_absolute_error_seconds": null,
                 "within_60s_share": null, "persistence_mae_seconds": null,
                 "accurate_share": null, "persistence_accurate_share": null,
                 "by_vehicle_type": []}
                """;

        PredictionScoreboard board = client.scoreboard();

        assertThat(board.gradedCount()).isZero();
        assertThat(board.meanAbsoluteErrorSeconds()).isNull();
        assertThat(board.accurateShare()).isNull();
        assertThat(board.byVehicleType()).isEmpty();
    }

    @Test
    void currentDelaysSendsTripIdsAndMapsEachReading() {
        responseBody = """
                {"delays": {
                   "BKK_T1": {"delay_seconds": 120.0, "minutes_ago": 2.0, "stale_after_minutes": 15.0,
                              "prediction_error_seconds": 18.0, "prediction_graded_minutes_ago": 2.0,
                              "stop_id": "BKK_F00001", "graded_stop_id": "BKK_F00001",
                              "graded_predicted_seconds": 102.0, "graded_actual_seconds": 120.0,
                              "prediction_accurate": true},
                   "BKK_T2": {"delay_seconds": -30.0, "minutes_ago": 9.0, "stale_after_minutes": 15.0,
                              "prediction_error_seconds": null, "prediction_graded_minutes_ago": null,
                              "stop_id": "BKK_F00002", "graded_stop_id": null,
                              "graded_predicted_seconds": null, "graded_actual_seconds": null,
                              "prediction_accurate": null}
                 }}
                """;

        Map<String, CurrentDelay> delays = client.currentDelays(List.of("BKK_T1", "BKK_T2", "BKK_T3"), "20261030");

        assertThat(lastRequestPath.get()).isEqualTo("/vehicles/current-delays");
        JsonNode sent = mapper.readTree(lastRequestBody.get());
        assertThat(sent.path("service_date").asText()).isEqualTo("20261030");
        assertThat(sent.path("trip_ids")).extracting(JsonNode::asText)
                .containsExactly("BKK_T1", "BKK_T2", "BKK_T3");

        // BKK_T3 has no confirmed arrival yet today, so it is simply absent.
        assertThat(delays).containsOnlyKeys("BKK_T1", "BKK_T2");

        CurrentDelay late = delays.get("BKK_T1");
        assertThat(late.delaySeconds()).isEqualTo(120.0);
        assertThat(late.staleAfterMinutes()).isEqualTo(15.0);
        assertThat(late.predictionErrorSeconds()).isEqualTo(18.0);
        assertThat(late.predictionAccurate()).isTrue();

        CurrentDelay early = delays.get("BKK_T2");
        assertThat(early.delaySeconds()).isEqualTo(-30.0);
        assertThat(early.predictionErrorSeconds()).isNull();
        assertThat(early.predictionAccurate()).isNull();
    }
}

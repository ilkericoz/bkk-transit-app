package hu.elte.bkktransit.controller;

import hu.elte.bkktransit.service.CurrentDelay;
import hu.elte.bkktransit.service.DelayPredictionClient;
import hu.elte.bkktransit.service.DelayPredictionRequest;
import hu.elte.bkktransit.service.DelayPredictionResult;
import hu.elte.bkktransit.service.FutarClient;
import hu.elte.bkktransit.service.VehiclePosition;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.assertj.MockMvcTester;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

/**
 * Web-layer tests for /api/vehicles: only the controller and Spring MVC are
 * started (no database, RabbitMQ or real BKK/sidecar calls) - the two
 * clients are replaced with Mockito mocks.
 */
@WebMvcTest(VehicleController.class)
class VehicleControllerTest {

    @Autowired
    private MockMvcTester mvc;

    @MockitoBean
    private FutarClient futarClient;

    @MockitoBean
    private DelayPredictionClient delayPredictionClient;

    private static VehiclePosition tram6() {
        return new VehiclePosition("BKK_V123", "BKK_3060", "TRAM", 47.497, 19.054, 182.5, "6",
                1_790_000_000L, "BKK_T1", "BKK_F00001", 7, "20261030", "IN_TRANSIT_TO", 40, false, false);
    }

    @Test
    void nearbyVehiclesUsesA2kmRadiusByDefault() {
        when(futarClient.vehiclesNear(47.4979, 19.0402, 2000)).thenReturn(List.of(tram6()));

        assertThat(mvc.get().uri("/api/vehicles").param("lat", "47.4979").param("lon", "19.0402"))
                .hasStatusOk()
                .bodyJson()
                .extractingPath("$[0].vehicleId").isEqualTo("BKK_V123");

        verify(futarClient).vehiclesNear(47.4979, 19.0402, 2000);
    }

    @Test
    void nearbyVehiclesPassesAnExplicitRadiusThrough() {
        when(futarClient.vehiclesNear(47.5, 19.0, 500)).thenReturn(List.of());

        assertThat(mvc.get().uri("/api/vehicles")
                .param("lat", "47.5").param("lon", "19.0").param("radius", "500"))
                .hasStatusOk()
                .bodyJson()
                .isLenientlyEqualTo("[]");

        verify(futarClient).vehiclesNear(47.5, 19.0, 500);
    }

    @Test
    void nearbyVehiclesRejectsARequestWithoutCoordinates() {
        assertThat(mvc.get().uri("/api/vehicles")).hasStatus4xxClientError();
    }

    @Test
    void delayPredictionReturnsTheSidecarResultAsJson() {
        when(delayPredictionClient.predict(any(DelayPredictionRequest.class)))
                .thenReturn(DelayPredictionResult.of(95.5,
                        new DelayPredictionResult.LastConfirmedDelay(80.0, 3.5)));

        assertThat(mvc.post().uri("/api/vehicles/delay-prediction")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                        {"tripId": "BKK_T1", "routeId": "BKK_3060", "stopId": "BKK_F00001",
                         "vehicleRouteType": "TRAM", "stopSequence": 7,
                         "serviceDate": "20261030", "deviated": false}
                        """))
                .hasStatusOk()
                .bodyJson()
                .isLenientlyEqualTo("""
                        {"available": true, "predictedDelaySeconds": 95.5,
                         "lastConfirmedDelay": {"delaySeconds": 80.0, "minutesAgo": 3.5}}
                        """);

        verify(delayPredictionClient).predict(
                new DelayPredictionRequest("BKK_T1", "BKK_3060", "BKK_F00001", "TRAM", 7, "20261030", false));
    }

    @Test
    void delayPredictionReportsUnavailableTripsAsAvailableFalse() {
        when(delayPredictionClient.predict(any(DelayPredictionRequest.class)))
                .thenReturn(DelayPredictionResult.unavailable());

        assertThat(mvc.post().uri("/api/vehicles/delay-prediction")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                        {"tripId": "UNKNOWN", "routeId": "BKK_3060", "stopId": "BKK_F00001",
                         "vehicleRouteType": "TRAM", "stopSequence": 1, "serviceDate": "20261030"}
                        """))
                .hasStatusOk()
                .bodyJson()
                .extractingPath("$.available").isEqualTo(false);
    }

    @Test
    void currentDelaysForwardsTripIdsAndServiceDate() {
        when(delayPredictionClient.currentDelays(List.of("BKK_T1", "BKK_T2"), "20261030"))
                .thenReturn(Map.of("BKK_T1", new CurrentDelay(120.0, 2.0, 15.0, 18.0, 2.0,
                        "BKK_F00001", "BKK_F00001", 102.0, 120.0, true)));

        assertThat(mvc.post().uri("/api/vehicles/current-delays")
                .contentType(MediaType.APPLICATION_JSON)
                .content("""
                        {"tripIds": ["BKK_T1", "BKK_T2"], "serviceDate": "20261030"}
                        """))
                .hasStatusOk()
                .bodyJson()
                .extractingPath("$.BKK_T1.delaySeconds").isEqualTo(120.0);

        verify(delayPredictionClient).currentDelays(List.of("BKK_T1", "BKK_T2"), "20261030");
    }
}

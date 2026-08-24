package hu.elte.bkktransit.service;

import tools.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

import java.util.ArrayList;
import java.util.List;

/**
 * Talks to BKK's FUTAR real-time API (opendata.bkk.hu). Kept separate from
 * StopController/VehicleController on purpose: this class only knows how to
 * call BKK and shape the response, it has no idea it's serving a REST API
 * itself - that's the controller's job. Same layering idea as
 * GtfsImportService not knowing about HTTP at all.
 */
@Service
public class FutarClient {

    private final RestClient restClient;
    private final String apiKey;

    // Built directly via RestClient.builder() rather than injecting a
    // RestClient.Builder bean - Boot 4 split RestClient auto-configuration
    // into its own opt-in starter, which isn't on our classpath, and we only
    // ever need this one client instance anyway, so there's no real benefit
    // to going through Spring's bean wiring for it here.
    public FutarClient(@Value("${bkk.futar.api-key}") String apiKey) {
        this.restClient = RestClient.builder()
                .baseUrl("https://futar.bkk.hu/api/query/v1/ws/otp/api/where")
                .build();
        this.apiKey = apiKey;
    }

    /**
     * Every vehicle BKK is currently tracking within radiusMeters of (lat, lon).
     * BKK's response has a lot of fields we don't need (capacity, congestion,
     * icon styling, ...) - rather than write DTO classes to model the whole
     * thing just to throw most of it away, we read it as a generic JsonNode
     * tree and pull out only what VehiclePosition needs.
     */
    public List<VehiclePosition> vehiclesNear(double lat, double lon, int radiusMeters) {
        JsonNode root = restClient.get()
                .uri(uriBuilder -> uriBuilder
                        .path("/vehicles-for-location.json")
                        .queryParam("lat", lat)
                        .queryParam("lon", lon)
                        .queryParam("radius", radiusMeters)
                        .queryParam("key", apiKey)
                        .build())
                .retrieve()
                .body(JsonNode.class);

        List<VehiclePosition> vehicles = new ArrayList<>();
        for (JsonNode v : root.path("data").path("list")) {
            vehicles.add(new VehiclePosition(
                    v.path("vehicleId").asText(),
                    v.path("routeId").asText(null),
                    v.path("vehicleRouteType").asText(null),
                    v.path("location").path("lat").asDouble(),
                    v.path("location").path("lon").asDouble(),
                    v.hasNonNull("bearing") ? v.path("bearing").asDouble() : null,
                    v.path("label").asText(null),
                    v.path("lastUpdateTime").asLong()
            ));
        }
        return vehicles;
    }
}

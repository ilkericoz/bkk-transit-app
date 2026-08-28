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

    // How long a cached response is served before we call BKK again. Pegged
    // to the frontend's own poll interval (see app.js POLL_INTERVAL_MS): if
    // several browser tabs are open and polling every 10s, this collapses
    // them to roughly one upstream BKK call per 10s instead of one per tab -
    // otherwise every viewer's poll would hit BKK's live API directly and
    // the load would scale with viewer count, not with how often the
    // underlying data actually changes.
    private static final long CACHE_TTL_MILLIS = 10_000;

    private final RestClient restClient;
    private final String apiKey;

    // Single-slot cache: holds only the most recent (query, result) pair,
    // not a full keyed cache of every distinct (lat, lon, radius) ever
    // requested. Fine as long as the frontend always queries the same fixed
    // area (true today - see CENTER in app.js); if this ever needs to serve
    // many different viewports at once, this would need to become a bounded
    // keyed cache (e.g. Caffeine) instead, so entries for viewports nobody
    // is looking at anymore get evicted.
    private volatile CachedVehicles cache;

    private record CachedVehicles(String queryKey, long expiresAtMillis, List<VehiclePosition> vehicles) {
    }

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
     * Serves a cached result when one exists for this exact query and hasn't
     * expired yet, rather than calling BKK on every single request - see
     * CACHE_TTL_MILLIS.
     *
     * Synchronized so two requests racing right as the cache expires don't
     * both slip through and double up the upstream call - vehicle counts are
     * small enough (a few hundred, at most) that holding the lock for the
     * duration of one BKK call is not worth optimizing away here.
     */
    public synchronized List<VehiclePosition> vehiclesNear(double lat, double lon, int radiusMeters) {
        String queryKey = lat + "," + lon + "," + radiusMeters;
        CachedVehicles current = cache;
        if (current != null && current.queryKey().equals(queryKey)
                && System.currentTimeMillis() < current.expiresAtMillis()) {
            return current.vehicles();
        }

        List<VehiclePosition> fresh = fetchFromBkk(lat, lon, radiusMeters);
        cache = new CachedVehicles(queryKey, System.currentTimeMillis() + CACHE_TTL_MILLIS, fresh);
        return fresh;
    }

    /**
     * The actual BKK call - pulled out of vehiclesNear() so the caching logic
     * above and the "talk to BKK" logic below aren't tangled together.
     * BKK's response has a lot of fields we don't need (capacity, congestion,
     * icon styling, ...) - rather than write DTO classes to model the whole
     * thing just to throw most of it away, we read it as a generic JsonNode
     * tree and pull out only what VehiclePosition needs.
     */
    private List<VehiclePosition> fetchFromBkk(double lat, double lon, int radiusMeters) {
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
                    v.path("lastUpdateTime").asLong(),
                    v.path("tripId").asText(null),
                    v.path("stopId").asText(null),
                    v.hasNonNull("stopSequence") ? v.path("stopSequence").asInt() : null,
                    v.path("serviceDate").asText(null),
                    v.path("status").asText(null),
                    v.hasNonNull("stopDistancePercent") ? v.path("stopDistancePercent").asInt() : null
            ));
        }
        return vehicles;
    }
}

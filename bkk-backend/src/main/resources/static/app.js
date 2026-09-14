// Deák Ferenc tér, central Budapest - a fixed center for stage 3 rather than
// recomputing lat/lon/radius from the map's current pan/zoom. Simpler, and
// good enough to demo "live vehicles across the city" convincingly.
const CENTER = [47.4979, 19.0402];
// 25km, not the original 6km - matches the ingestion pipeline's radius
// (see the comment on bkk.ingestion.radius-meters in application.properties
// for how this number was picked). BKK's API itself caps out somewhere
// between 25km and 28km (LIMIT_EXCEEDED beyond that), found empirically.
const RADIUS_METERS = 25000;
// 10s, not 5s: checked BKK's actual data - most vehicles only report a new
// GPS position every 10-20+ seconds, so polling faster than that just
// re-fetches data that hasn't changed yet.
const POLL_INTERVAL_MS = 10000;

// BKK's real-time feed returns vehicleRouteType as a descriptive string, NOT
// the numeric GTFS route_type code stage 1's static feed uses (e.g. "TRAM",
// not "0") - found by actually calling the live endpoint rather than
// assuming the two feeds share an encoding. Map each to a distinct color so
// vehicle types are visually distinguishable on the map at a glance.
const ROUTE_TYPE_COLORS = {
    TRAM: "#e67e22",
    SUBWAY: "#2980b9",
    RAIL: "#8e44ad",
    SUBURBAN_RAILWAY: "#8e44ad",
    BUS: "#27ae60",
    TROLLEYBUS: "#16a085",
    COACH: "#27ae60",
    FERRY: "#2c3e50",
};
const DEFAULT_COLOR = "#7f8c8d";

function colorFor(routeType) {
    return ROUTE_TYPE_COLORS[routeType] ?? DEFAULT_COLOR;
}

// Keyed by the real-time routeId format ("BKK_" + static route_id, same
// prefix convention as tripId/stopId) so lookups from vehicle data need no
// extra string surgery at use-site. Loaded once on page load - routes.txt
// is ~390 static rows, not something that changes while the page is open.
let routesById = new Map();

function loadRoutes() {
    return fetch("/api/routes")
        .then((response) => response.json())
        .then((routes) => {
            routesById = new Map(routes.map((route) => [`BKK_${route.routeId}`, route]));
        })
        .catch((error) => console.error("Failed to fetch routes", error));
}

// Falls back to the raw routeId (e.g. "BKK_3020") if routes haven't loaded
// yet or this particular route isn't in the static feed for some reason -
// better to show something than nothing.
function routeLabel(vehicle) {
    const route = routesById.get(vehicle.routeId);
    return route?.routeShortName || vehicle.routeId || "n/a";
}

// Zoom 13 (city-district scale) made sense for the old 6km radius but
// would hide most of a 25km radius's worth of vehicles off-screen until
// the viewer manually zoomed out - 11 fits the wider metro area by default.
const map = L.map("map").setView(CENTER, 11);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

// Keyed by vehicleId so a repeat sighting of the same vehicle moves its
// existing marker instead of stacking a new one on top - the alternative
// (clear all markers, re-add every poll) would make the map flicker every
// 5 seconds.
const markersById = new Map();

// Latest known data per vehicle, kept alongside markersById so the
// 'popupopen' handler below (bound once, at marker creation) can always
// look up this vehicle's *current* trip/stop rather than whatever it was
// when the marker was first created.
const vehiclesById = new Map();

// Turns BKK's raw status + stopDistancePercent into one readable line - the
// same fields the stage-5 label pipeline joins against GTFS stop_times.txt
// to compute delay, surfaced here as a visible sanity check that the data
// we're now collecting looks right, not just something living in Postgres.
function statusLine(vehicle) {
    if (!vehicle.stopId) {
        return "n/a";
    }
    if (vehicle.status === "STOPPED_AT") {
        return `Stopped at ${vehicle.stopId}`;
    }
    if (vehicle.status === "IN_TRANSIT_TO") {
        return `En route to ${vehicle.stopId} (${vehicle.stopDistancePercent ?? "?"}%)`;
    }
    return `${vehicle.status ?? "n/a"} - ${vehicle.stopId}`;
}

// Delay predictions are fetched lazily (see maybeFetchPrediction below) -
// keyed here by vehicleId so a poll's setPopupContent() re-render (every
// 10s, see refreshVehicles) doesn't wipe out a prediction that's already
// loading or already came back, and so a still-open popup can be updated
// in place once the fetch resolves rather than waiting for the next poll.
const predictionsByVehicleId = new Map();

function predictionLine(vehicle) {
    if (!vehicle.tripId || !vehicle.stopId || vehicle.stopSequence == null) {
        return "";
    }
    const prediction = predictionsByVehicleId.get(vehicle.vehicleId);
    if (!prediction) {
        return "";
    }
    if (prediction.status === "loading") {
        return "<br>Predicted delay: …";
    }
    if (prediction.status === "error") {
        return "<br>Predicted delay: (prediction service unavailable)";
    }
    if (!prediction.available) {
        return "<br>Predicted delay: n/a (not on our imported schedule)";
    }
    return `<br>Predicted delay: ${Math.round(prediction.predictedDelaySeconds)}s` + lastConfirmedLine(prediction);
}

// Shows the vehicle's last CONFIRMED delay (genuine ground truth - an
// actual observed arrival at an earlier stop) right next to the
// prediction above, so it's easy to eyeball whether the prediction looks
// reasonable given what this vehicle was *actually* doing a moment ago -
// not a comparison the prediction itself can prove right or wrong (this
// stop hasn't happened yet), just a sanity-check reference point.
function lastConfirmedLine(prediction) {
    const confirmed = prediction.lastConfirmedDelay;
    if (!confirmed) {
        return "<br>Last confirmed delay: n/a (first observed stop on this trip)";
    }
    const recency = confirmed.minutesAgo != null ? ` (${confirmed.minutesAgo.toFixed(1)} min ago)` : "";
    return `<br>Last confirmed delay: ${Math.round(confirmed.delaySeconds)}s${recency}`;
}

function popupHtml(vehicle) {
    const label = vehicle.label || vehicle.vehicleId;
    const secondsAgo = Math.round(Date.now() / 1000 - vehicle.lastUpdateTime);
    return `
        <strong>${label}</strong><br>
        Route: ${routeLabel(vehicle)}<br>
        Trip: ${vehicle.tripId ?? "n/a"}<br>
        ${statusLine(vehicle)}<br>
        Updated ${secondsAgo}s ago${predictionLine(vehicle)}
    `;
}

// Called only when a vehicle's popup is actually opened (see 'popupopen'
// below), not for every vehicle on every 10s poll - with ~1700 vehicles in
// view, predicting all of them constantly would be ~1700 calls/10s to a
// sidecar nobody asked about. A short TTL avoids re-fetching every time the
// same popup is reopened moments apart, while still refreshing if it's been
// sitting open a while (the vehicle's actual stop/trip can move on).
const PREDICTION_TTL_MS = 15000;

function maybeFetchPrediction(vehicle, marker) {
    if (!vehicle.tripId || !vehicle.stopId || vehicle.stopSequence == null) {
        return; // statusLine already shows "n/a" for these - nothing to predict.
    }
    const existing = predictionsByVehicleId.get(vehicle.vehicleId);
    if (existing && existing.status !== "error" && Date.now() - existing.fetchedAt < PREDICTION_TTL_MS) {
        return;
    }

    predictionsByVehicleId.set(vehicle.vehicleId, { status: "loading", fetchedAt: Date.now() });
    marker.setPopupContent(popupHtml(vehicle));

    fetch("/api/vehicles/delay-prediction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            tripId: vehicle.tripId,
            routeId: vehicle.routeId,
            stopId: vehicle.stopId,
            vehicleRouteType: vehicle.vehicleRouteType,
            stopSequence: vehicle.stopSequence,
            serviceDate: vehicle.serviceDate,
            deviated: vehicle.deviated ?? false,
        }),
    })
        .then((response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then((result) => {
            predictionsByVehicleId.set(vehicle.vehicleId, { ...result, status: "ok", fetchedAt: Date.now() });
        })
        .catch((error) => {
            console.error("Failed to fetch delay prediction", error);
            predictionsByVehicleId.set(vehicle.vehicleId, { status: "error", fetchedAt: Date.now() });
        })
        .finally(() => {
            // Only touch the DOM if this vehicle's marker is still the one
            // on the map (updateMarkers may have swapped it) and its popup
            // is still open - otherwise this just updates the cache above
            // for whenever it's next opened.
            if (markersById.get(vehicle.vehicleId) === marker && marker.isPopupOpen()) {
                marker.setPopupContent(popupHtml(vehicle));
            }
        });
}

function updateMarkers(vehicles) {
    const seenIds = new Set();

    for (const vehicle of vehicles) {
        seenIds.add(vehicle.vehicleId);
        vehiclesById.set(vehicle.vehicleId, vehicle);
        const color = colorFor(vehicle.vehicleRouteType);
        const existing = markersById.get(vehicle.vehicleId);

        if (existing) {
            existing.setLatLng([vehicle.lat, vehicle.lon]);
            existing.setStyle({ color, fillColor: color });
            existing.setPopupContent(popupHtml(vehicle));
        } else {
            const marker = L.circleMarker([vehicle.lat, vehicle.lon], {
                radius: 6,
                color,
                fillColor: color,
                fillOpacity: 0.8,
                weight: 2,
            }).bindPopup(popupHtml(vehicle));
            // Only predict for a vehicle someone actually looked at, and
            // re-check on every open (not just the first) since the TTL in
            // maybeFetchPrediction may have expired by then.
            marker.on("popupopen", () => maybeFetchPrediction(vehiclesById.get(vehicle.vehicleId), marker));
            marker.addTo(map);
            markersById.set(vehicle.vehicleId, marker);
        }
    }

    // A vehicle BKK stopped reporting (out of range, gone offline, trip
    // ended) won't be in this poll's response - drop its marker rather than
    // leaving a stale dot behind forever.
    for (const [id, marker] of markersById) {
        if (!seenIds.has(id)) {
            map.removeLayer(marker);
            markersById.delete(id);
            vehiclesById.delete(id);
            predictionsByVehicleId.delete(id);
        }
    }
}

function refreshVehicles() {
    const url = `/api/vehicles?lat=${CENTER[0]}&lon=${CENTER[1]}&radius=${RADIUS_METERS}`;
    fetch(url)
        .then((response) => response.json())
        .then(updateMarkers)
        .catch((error) => console.error("Failed to fetch vehicles", error));
}

loadRoutes().then(refreshVehicles);
setInterval(refreshVehicles, POLL_INTERVAL_MS);

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

// Color scale for real-time delay severity (added 2026-09-14) - replaces
// vehicle-type as the map's primary, at-a-glance color signal, since
// delay is the actual point of this project and it was previously only
// visible one click at a time. Vehicle type is still in every popup, just
// not glanceable anymore - a deliberate trade-off. Discrete buckets
// rather than a continuous gradient: easier to read at a glance across
// hundreds of small dots, and easier to build a legible legend for.
const DELAY_COLOR_BUCKETS = [
    { max: -30, color: "#2980b9", label: "Early (30s+)" },
    { max: 60, color: "#27ae60", label: "On time" },
    { max: 180, color: "#f1c40f", label: "Minor delay (1-3 min)" },
    { max: 360, color: "#e67e22", label: "Moderate delay (3-6 min)" },
    { max: Infinity, color: "#e74c3c", label: "Severe delay (6+ min)" },
];
const NO_DELAY_DATA_COLOR = "#95a5a6";

function delayColor(delaySeconds) {
    if (delaySeconds == null) {
        return NO_DELAY_DATA_COLOR;
    }
    const bucket = DELAY_COLOR_BUCKETS.find((b) => delaySeconds < b.max);
    return bucket.color;
}

// Keyed by tripId, matching the /current-delays response's own keying -
// refreshed once per vehicle poll (see refreshDelayColors), not per
// popup click. A tripId missing from this map means "no confirmed
// arrival yet today", not an error.
let currentDelaysByTripId = new Map();

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

// Keyed by "BKK_" + stop_id. Tried stop_code first (matching tripId/routeId's
// own "BKK_" + code convention), but checked it against every currently live
// vehicle's actual stopId rather than assume the same convention holds here:
// stop_id resolved 321/338, stop_code only 314/338 - and every stop_code
// match was already covered by stop_id, i.e. stop_code is a strict subset
// here, not a better key. Loaded once on page load, same reasoning as
// routesById (~6k static rows, doesn't change while the page is open).
let stopsById = new Map();

function loadStops() {
    return fetch("/api/stops")
        .then((response) => response.json())
        .then((stops) => {
            stopsById = new Map(stops.map((stop) => [`BKK_${stop.stopId}`, stop]));
        })
        .catch((error) => console.error("Failed to fetch stops", error));
}

// Falls back to the raw stopId (e.g. "BKK_F01294") if stops haven't loaded
// yet, or this particular stop isn't in the static feed - most often a
// Volánbusz/MÁV-START vehicle BKK's live feed surfaces without owning its
// schedule (see README's "Known limitations"), same "show something rather
// than nothing" fallback as routeLabel.
function stopLabel(stopId) {
    return stopsById.get(stopId)?.stopName || stopId;
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
        return `Stopped at ${stopLabel(vehicle.stopId)}`;
    }
    if (vehicle.status === "IN_TRANSIT_TO") {
        return `En route to ${stopLabel(vehicle.stopId)} (${vehicle.stopDistancePercent ?? "?"}%)`;
    }
    return `${vehicle.status ?? "n/a"} - ${stopLabel(vehicle.stopId)}`;
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
        // Uses last poll's delay data until refreshDelayColors (called
        // right after this function, see refreshVehicles) gets this
        // poll's fresh numbers back - briefly a poll stale, close enough
        // given delay doesn't swing wildly in 10s.
        const color = delayColor(currentDelaysByTripId.get(vehicle.tripId)?.delaySeconds);
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

// One bulk call per poll for every tracked vehicle's real-time delay
// color, not one call per vehicle - see main.py's /vehicles/current-delays
// docstring for the cost reasoning (same one that made per-click
// prediction lazy in the first place).
function refreshDelayColors(vehicles) {
    const tripIds = vehicles.map((v) => v.tripId).filter(Boolean);
    const serviceDate = vehicles.find((v) => v.serviceDate)?.serviceDate;
    if (tripIds.length === 0 || !serviceDate) {
        return;
    }

    fetch("/api/vehicles/current-delays", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tripIds, serviceDate }),
    })
        .then((response) => response.json())
        .then((delays) => {
            currentDelaysByTripId = new Map(Object.entries(delays));
            for (const vehicle of vehicles) {
                const marker = markersById.get(vehicle.vehicleId);
                if (!marker) continue;
                const color = delayColor(currentDelaysByTripId.get(vehicle.tripId)?.delaySeconds);
                marker.setStyle({ color, fillColor: color });
            }
        })
        .catch((error) => console.error("Failed to fetch current delays", error));
}

function refreshVehicles() {
    const url = `/api/vehicles?lat=${CENTER[0]}&lon=${CENTER[1]}&radius=${RADIUS_METERS}`;
    fetch(url)
        .then((response) => response.json())
        .then((vehicles) => {
            updateMarkers(vehicles);
            refreshDelayColors(vehicles);
        })
        .catch((error) => console.error("Failed to fetch vehicles", error));
}

Promise.all([loadRoutes(), loadStops()]).then(refreshVehicles);
setInterval(refreshVehicles, POLL_INTERVAL_MS);

// Static legend for the delay color scale above - rendered once, not
// polled (the scale itself never changes).
function renderLegend() {
    const el = document.getElementById("legend");
    const swatch = (color) => `<span style="display:inline-block;width:10px;height:10px;background:${color};margin-right:6px;border-radius:2px;"></span>`;
    const rows = DELAY_COLOR_BUCKETS.map((b) => `${swatch(b.color)}${b.label}<br>`).join("");
    el.innerHTML = `<strong>Delay</strong><br>${rows}${swatch(NO_DELAY_DATA_COLOR)}No data yet`;
    el.classList.remove("hidden");
}

renderLegend();

// Live accuracy scoreboard - reconciles predictions already made against
// what actually happened (see PredictionScoreboard on the Java side).
// Polled far less often than vehicle positions: a prediction only
// reconciles once the vehicle actually reaches the stop, so refreshing
// this every 10s like the map would just re-fetch the same few numbers.
const SCOREBOARD_POLL_INTERVAL_MS = 30000;

function scoreboardHtml(scoreboard) {
    if (scoreboard.reconciledCount === 0) {
        return "<strong>Live model accuracy</strong><br>No reconciled predictions yet - click a vehicle to make one, then check back once it reaches its next stop.";
    }
    const mae = Math.round(scoreboard.meanAbsoluteErrorSeconds);
    const windowSize = Math.min(scoreboard.reconciledCount, 50);
    let html = `<strong>Live model accuracy</strong><br>Avg error (last ${windowSize}): ${mae}s<br>Reconciled so far: ${scoreboard.reconciledCount}`;
    if (scoreboard.recent.length > 0) {
        html += "<hr>";
        for (const entry of scoreboard.recent.slice(0, 5)) {
            const predicted = Math.round(entry.predictedDelaySeconds);
            const actual = Math.round(entry.actualDelaySeconds);
            html += `${routeLabelFor(entry)}: predicted ${predicted}s, actual ${actual}s<br>`;
        }
    }
    return html;
}

// Separate from routeLabel(vehicle) above since a scoreboard entry isn't
// shaped like a VehiclePosition (no "BKK_"-prefixed routeId lookup key
// mismatch to worry about here - routeId already comes through as-is).
function routeLabelFor(entry) {
    const route = routesById.get(entry.routeId);
    return route?.routeShortName || entry.routeId || "n/a";
}

function refreshScoreboard() {
    const el = document.getElementById("scoreboard");
    fetch("/api/vehicles/prediction-scoreboard")
        .then((response) => response.json())
        .then((scoreboard) => {
            el.innerHTML = scoreboardHtml(scoreboard);
            el.classList.remove("hidden");
        })
        .catch((error) => console.error("Failed to fetch prediction scoreboard", error));
}

refreshScoreboard();
setInterval(refreshScoreboard, SCOREBOARD_POLL_INTERVAL_MS);

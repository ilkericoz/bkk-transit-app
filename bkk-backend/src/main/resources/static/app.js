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

function popupHtml(vehicle) {
    const label = vehicle.label || vehicle.vehicleId;
    const secondsAgo = Math.round(Date.now() / 1000 - vehicle.lastUpdateTime);
    return `
        <strong>${label}</strong><br>
        Route: ${routeLabel(vehicle)}<br>
        Trip: ${vehicle.tripId ?? "n/a"}<br>
        ${statusLine(vehicle)}<br>
        Updated ${secondsAgo}s ago
    `;
}

function updateMarkers(vehicles) {
    const seenIds = new Set();

    for (const vehicle of vehicles) {
        seenIds.add(vehicle.vehicleId);
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

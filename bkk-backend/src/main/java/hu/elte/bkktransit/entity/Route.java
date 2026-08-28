package hu.elte.bkktransit.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * One row of BKK's GTFS routes.txt — a numbered/lettered line (tram 2,
 * bus 7E, ...), not to be confused with a Trip (one scheduled run of a
 * route) or a vehicle's real-time routeId (which is "BKK_" + this route_id,
 * same prefix convention already seen for stops and trips). route_id is
 * BKK's own stable identifier, so - like Stop - we use it directly as our
 * primary key instead of generating a surrogate one.
 */
@Entity
@Table(name = "routes")
public class Route {

    @Id
    private String routeId;

    // The short label riders actually recognize on the vehicle/stop sign,
    // e.g. "2" or "7E" - what we actually want to show instead of a raw
    // routeId in the UI.
    private String routeShortName;
    private String routeLongName;

    // GTFS's numeric route_type code (0 = tram, 3 = bus, ...) - the static
    // feed's convention, distinct from the descriptive strings ("TRAM",
    // "BUS") BKK's real-time feed uses for vehicleRouteType. Two different
    // encodings of the same concept, already hit once before in stage 3.
    private Integer routeType;

    private String routeDesc;
    private String routeColor;
    private String routeTextColor;

    protected Route() {
    }

    public Route(String routeId, String routeShortName, String routeLongName,
                 Integer routeType, String routeDesc, String routeColor, String routeTextColor) {
        this.routeId = routeId;
        this.routeShortName = routeShortName;
        this.routeLongName = routeLongName;
        this.routeType = routeType;
        this.routeDesc = routeDesc;
        this.routeColor = routeColor;
        this.routeTextColor = routeTextColor;
    }

    public String getRouteId() {
        return routeId;
    }

    public String getRouteShortName() {
        return routeShortName;
    }

    public String getRouteLongName() {
        return routeLongName;
    }

    public Integer getRouteType() {
        return routeType;
    }

    public String getRouteDesc() {
        return routeDesc;
    }

    public String getRouteColor() {
        return routeColor;
    }

    public String getRouteTextColor() {
        return routeTextColor;
    }
}

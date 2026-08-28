package hu.elte.bkktransit.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.Table;

import java.time.Instant;

/**
 * One historical sighting of one vehicle, persisted by the ingestion
 * pipeline's consumer (see service/VehiclePositionConsumer). Unlike Stop,
 * there's no natural primary key here - the same vehicleId recurs every
 * time the vehicle is polled, so we need a generated surrogate id. This is
 * the table stage 5 (delay-prediction) will eventually train on.
 */
@Entity
@Table(name = "vehicle_position_snapshots", indexes = {
        @Index(name = "idx_snapshot_vehicle_id", columnList = "vehicleId"),
        @Index(name = "idx_snapshot_recorded_at", columnList = "recordedAt"),
        @Index(name = "idx_snapshot_trip_id", columnList = "tripId")
})
public class VehiclePositionSnapshot {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private String vehicleId;
    private String routeId;
    private String vehicleRouteType;
    private double lat;
    private double lon;
    private Double bearing;
    private String label;

    // BKK's own timestamp for when the vehicle reported this position.
    private long lastUpdateTime;

    // Added for stage 5 (delay prediction) - see VehiclePosition's javadoc
    // for why these matter: tripId is the join key back to static GTFS
    // stop_times.txt, stopSequence disambiguates which visit to that trip's
    // stop list this is, serviceDate picks the right GTFS calendar day, and
    // status/stopDistancePercent together identify the "vehicle just
    // arrived at this stop" event (STOPPED_AT, 100%) that gives us the
    // actual side of the actual-vs-scheduled delay computation.
    private String tripId;
    private String stopId;
    private Integer stopSequence;
    private String serviceDate;
    private String status;
    private Integer stopDistancePercent;

    // When *our* consumer wrote this row - distinct from lastUpdateTime
    // above (that one's BKK's clock; this one's ours, and is what history
    // queries will actually filter/sort on).
    private Instant recordedAt;

    protected VehiclePositionSnapshot() {
    }

    public VehiclePositionSnapshot(String vehicleId, String routeId, String vehicleRouteType,
                                    double lat, double lon, Double bearing, String label,
                                    long lastUpdateTime, String tripId, String stopId,
                                    Integer stopSequence, String serviceDate, String status,
                                    Integer stopDistancePercent, Instant recordedAt) {
        this.vehicleId = vehicleId;
        this.routeId = routeId;
        this.vehicleRouteType = vehicleRouteType;
        this.lat = lat;
        this.lon = lon;
        this.bearing = bearing;
        this.label = label;
        this.lastUpdateTime = lastUpdateTime;
        this.tripId = tripId;
        this.stopId = stopId;
        this.stopSequence = stopSequence;
        this.serviceDate = serviceDate;
        this.status = status;
        this.stopDistancePercent = stopDistancePercent;
        this.recordedAt = recordedAt;
    }

    public Long getId() {
        return id;
    }

    public String getVehicleId() {
        return vehicleId;
    }

    public String getRouteId() {
        return routeId;
    }

    public String getVehicleRouteType() {
        return vehicleRouteType;
    }

    public double getLat() {
        return lat;
    }

    public double getLon() {
        return lon;
    }

    public Double getBearing() {
        return bearing;
    }

    public String getLabel() {
        return label;
    }

    public long getLastUpdateTime() {
        return lastUpdateTime;
    }

    public String getTripId() {
        return tripId;
    }

    public String getStopId() {
        return stopId;
    }

    public Integer getStopSequence() {
        return stopSequence;
    }

    public String getServiceDate() {
        return serviceDate;
    }

    public String getStatus() {
        return status;
    }

    public Integer getStopDistancePercent() {
        return stopDistancePercent;
    }

    public Instant getRecordedAt() {
        return recordedAt;
    }
}

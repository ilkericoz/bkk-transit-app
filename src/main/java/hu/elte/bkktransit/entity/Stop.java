package hu.elte.bkktransit.entity;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * One row of BKK's GTFS stops.txt — a physical stop, platform, or station.
 * stop_id is BKK's own stable identifier, so we use it directly as our
 * primary key instead of generating a surrogate one.
 */
@Entity
@Table(name = "stops")
public class Stop {

    @Id
    private String stopId;

    private String stopName;
    private Double stopLat;
    private Double stopLon;
    private String stopCode;
    private Integer locationType;
    private String parentStation;
    private Integer wheelchairBoarding;

    // JPA requires a no-arg constructor - it builds entities via reflection,
    // not by calling your constructors directly.
    protected Stop() {
    }

    public Stop(String stopId, String stopName, Double stopLat, Double stopLon,
                String stopCode, Integer locationType, String parentStation,
                Integer wheelchairBoarding) {
        this.stopId = stopId;
        this.stopName = stopName;
        this.stopLat = stopLat;
        this.stopLon = stopLon;
        this.stopCode = stopCode;
        this.locationType = locationType;
        this.parentStation = parentStation;
        this.wheelchairBoarding = wheelchairBoarding;
    }

    public String getStopId() {
        return stopId;
    }

    public String getStopName() {
        return stopName;
    }

    public Double getStopLat() {
        return stopLat;
    }

    public Double getStopLon() {
        return stopLon;
    }

    public String getStopCode() {
        return stopCode;
    }

    public Integer getLocationType() {
        return locationType;
    }

    public String getParentStation() {
        return parentStation;
    }

    public Integer getWheelchairBoarding() {
        return wheelchairBoarding;
    }
}

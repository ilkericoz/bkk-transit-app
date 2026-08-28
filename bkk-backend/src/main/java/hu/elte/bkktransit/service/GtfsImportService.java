package hu.elte.bkktransit.service;

import hu.elte.bkktransit.entity.Route;
import hu.elte.bkktransit.entity.Stop;
import hu.elte.bkktransit.repository.RouteRepository;
import hu.elte.bkktransit.repository.StopRepository;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVParser;
import org.apache.commons.csv.CSVRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * Parses BKK's GTFS stops.txt and loads it into the stops table.
 * GTFS is just CSV, but stop names can legally contain commas inside quotes
 * (e.g. "Örs vezér tere M+H, déli tárolótér"), so we use a real CSV parser
 * instead of String.split(",") — a naive split would silently corrupt those rows.
 */
@Service
public class GtfsImportService {

    private static final Logger log = LoggerFactory.getLogger(GtfsImportService.class);

    private final StopRepository stopRepository;
    private final RouteRepository routeRepository;

    public GtfsImportService(StopRepository stopRepository, RouteRepository routeRepository) {
        this.stopRepository = stopRepository;
        this.routeRepository = routeRepository;
    }

    public int importStops(Path stopsCsvFile) throws IOException {
        CSVFormat format = CSVFormat.DEFAULT.builder()
                .setHeader()            // read column names from the file's own header row
                .setSkipHeaderRecord(true)
                .get();

        List<Stop> stops = new ArrayList<>();

        try (Reader reader = Files.newBufferedReader(stopsCsvFile, StandardCharsets.UTF_8);
             CSVParser parser = CSVParser.parse(reader, format)) {

            for (CSVRecord record : parser) {
                stops.add(new Stop(
                        record.get("stop_id"),
                        record.get("stop_name"),
                        parseDouble(record.get("stop_lat")),
                        parseDouble(record.get("stop_lon")),
                        blankToNull(record.get("stop_code")),
                        parseInt(record.get("location_type")),
                        blankToNull(record.get("parent_station")),
                        parseInt(record.get("wheelchair_boarding"))
                ));
            }
        }

        stopRepository.saveAll(stops);
        log.info("Imported {} stops from {}", stops.size(), stopsCsvFile);
        return stops.size();
    }

    // routes.txt is tiny (a few hundred lines - one per numbered/lettered
    // line, not per scheduled trip) compared to stops.txt, so no real
    // performance difference from importStops() above - same CSV-parser
    // approach, kept as a separate method rather than a shared generic one
    // since the two entities' fields don't overlap enough to make that
    // worthwhile yet.
    public int importRoutes(Path routesCsvFile) throws IOException {
        CSVFormat format = CSVFormat.DEFAULT.builder()
                .setHeader()
                .setSkipHeaderRecord(true)
                .get();

        List<Route> routes = new ArrayList<>();

        try (Reader reader = Files.newBufferedReader(routesCsvFile, StandardCharsets.UTF_8);
             CSVParser parser = CSVParser.parse(reader, format)) {

            for (CSVRecord record : parser) {
                routes.add(new Route(
                        record.get("route_id"),
                        blankToNull(record.get("route_short_name")),
                        blankToNull(record.get("route_long_name")),
                        parseInt(record.get("route_type")),
                        blankToNull(record.get("route_desc")),
                        blankToNull(record.get("route_color")),
                        blankToNull(record.get("route_text_color"))
                ));
            }
        }

        routeRepository.saveAll(routes);
        log.info("Imported {} routes from {}", routes.size(), routesCsvFile);
        return routes.size();
    }

    private static String blankToNull(String value) {
        return (value == null || value.isBlank()) ? null : value;
    }

    private static Double parseDouble(String value) {
        return (value == null || value.isBlank()) ? null : Double.valueOf(value);
    }

    private static Integer parseInt(String value) {
        return (value == null || value.isBlank()) ? null : Integer.valueOf(value);
    }
}

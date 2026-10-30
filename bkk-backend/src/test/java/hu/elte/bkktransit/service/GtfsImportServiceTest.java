package hu.elte.bkktransit.service;

import hu.elte.bkktransit.entity.Route;
import hu.elte.bkktransit.entity.Stop;
import hu.elte.bkktransit.repository.RouteRepository;
import hu.elte.bkktransit.repository.StopRepository;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import org.mockito.ArgumentCaptor;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;

/**
 * Unit tests for the GTFS CSV import. The repositories are mocked, so these
 * run without a database: they only check that each CSV row turns into the
 * right entity before it is handed to saveAll().
 */
class GtfsImportServiceTest {

    @TempDir
    Path tempDir;

    private StopRepository stopRepository;
    private RouteRepository routeRepository;
    private GtfsImportService service;

    @BeforeEach
    void setUp() {
        stopRepository = mock(StopRepository.class);
        routeRepository = mock(RouteRepository.class);
        service = new GtfsImportService(stopRepository, routeRepository);
    }

    @SuppressWarnings("unchecked")
    private List<Stop> savedStops() {
        ArgumentCaptor<List<Stop>> captor = ArgumentCaptor.forClass(List.class);
        verify(stopRepository).saveAll(captor.capture());
        return captor.getValue();
    }

    @SuppressWarnings("unchecked")
    private List<Route> savedRoutes() {
        ArgumentCaptor<List<Route>> captor = ArgumentCaptor.forClass(List.class);
        verify(routeRepository).saveAll(captor.capture());
        return captor.getValue();
    }

    private Path writeCsv(String name, String content) throws IOException {
        Path file = tempDir.resolve(name);
        Files.writeString(file, content, StandardCharsets.UTF_8);
        return file;
    }

    @Test
    void importStopsReturnsRowCountAndSavesEveryRow() throws IOException {
        Path file = writeCsv("stops.txt", """
                stop_id,stop_name,stop_lat,stop_lon,stop_code,location_type,parent_station,wheelchair_boarding
                F00001,Deák Ferenc tér,47.497,19.054,F01,0,,1
                F00002,Astoria,47.494,19.060,,0,,2
                """);

        int count = service.importStops(file);

        assertThat(count).isEqualTo(2);
        assertThat(savedStops()).extracting(Stop::getStopId).containsExactly("F00001", "F00002");
    }

    @Test
    void importStopsKeepsCommasInsideQuotedNames() throws IOException {
        // The reason a real CSV parser is used instead of String.split(","):
        // BKK stop names can contain commas inside quotes.
        Path file = writeCsv("stops.txt", """
                stop_id,stop_name,stop_lat,stop_lon,stop_code,location_type,parent_station,wheelchair_boarding
                F01234,"Örs vezér tere M+H, déli tárolótér",47.502,19.137,,0,,
                """);

        service.importStops(file);

        Stop stop = savedStops().get(0);
        assertThat(stop.getStopName()).isEqualTo("Örs vezér tere M+H, déli tárolótér");
        assertThat(stop.getStopLat()).isEqualTo(47.502);
        assertThat(stop.getStopLon()).isEqualTo(19.137);
    }

    @Test
    void importStopsTurnsBlankOptionalFieldsIntoNull() throws IOException {
        Path file = writeCsv("stops.txt", """
                stop_id,stop_name,stop_lat,stop_lon,stop_code,location_type,parent_station,wheelchair_boarding
                F00002,Astoria,47.494,19.060,,,,
                """);

        service.importStops(file);

        Stop stop = savedStops().get(0);
        assertThat(stop.getStopCode()).isNull();
        assertThat(stop.getLocationType()).isNull();
        assertThat(stop.getParentStation()).isNull();
        assertThat(stop.getWheelchairBoarding()).isNull();
    }

    @Test
    void importStopsParsesNumericAndParentFields() throws IOException {
        Path file = writeCsv("stops.txt", """
                stop_id,stop_name,stop_lat,stop_lon,stop_code,location_type,parent_station,wheelchair_boarding
                F00003,Kálvin tér,47.489,19.061,F03,1,P00001,2
                """);

        service.importStops(file);

        Stop stop = savedStops().get(0);
        assertThat(stop.getStopCode()).isEqualTo("F03");
        assertThat(stop.getLocationType()).isEqualTo(1);
        assertThat(stop.getParentStation()).isEqualTo("P00001");
        assertThat(stop.getWheelchairBoarding()).isEqualTo(2);
    }

    @Test
    void importStopsWithOnlyAHeaderSavesNothing() throws IOException {
        Path file = writeCsv("stops.txt",
                "stop_id,stop_name,stop_lat,stop_lon,stop_code,location_type,parent_station,wheelchair_boarding\n");

        assertThat(service.importStops(file)).isZero();
        assertThat(savedStops()).isEmpty();
    }

    @Test
    void importRoutesMapsEveryColumnAndBlanksToNull() throws IOException {
        Path file = writeCsv("routes.txt", """
                route_id,route_short_name,route_long_name,route_type,route_desc,route_color,route_text_color
                3060,6,,0,"Széll Kálmán tér M / Móricz Zsigmond körtér M",FFD800,000000
                0050,M2,,1,,,
                """);

        int count = service.importRoutes(file);

        assertThat(count).isEqualTo(2);
        List<Route> routes = savedRoutes();

        Route tram6 = routes.get(0);
        assertThat(tram6.getRouteId()).isEqualTo("3060");
        assertThat(tram6.getRouteShortName()).isEqualTo("6");
        assertThat(tram6.getRouteLongName()).isNull();
        assertThat(tram6.getRouteType()).isZero();
        assertThat(tram6.getRouteDesc()).isEqualTo("Széll Kálmán tér M / Móricz Zsigmond körtér M");
        assertThat(tram6.getRouteColor()).isEqualTo("FFD800");
        assertThat(tram6.getRouteTextColor()).isEqualTo("000000");

        Route metro2 = routes.get(1);
        assertThat(metro2.getRouteId()).isEqualTo("0050");
        assertThat(metro2.getRouteType()).isEqualTo(1);
        assertThat(metro2.getRouteDesc()).isNull();
        assertThat(metro2.getRouteColor()).isNull();
    }
}

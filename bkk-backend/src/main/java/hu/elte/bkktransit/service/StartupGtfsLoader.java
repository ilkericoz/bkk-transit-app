package hu.elte.bkktransit.service;

import hu.elte.bkktransit.repository.StopRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.CommandLineRunner;
import org.springframework.stereotype.Component;

import java.nio.file.Path;

/**
 * Runs once when the app starts up. CommandLineRunner is a Spring Boot
 * interface: any bean implementing it has its run() method called right
 * after the application context is fully wired, before the app starts
 * accepting requests. Kept separate from GtfsImportService so the import
 * logic itself has no idea it's being triggered "at startup" — it's just a
 * plain method you could equally call from a test or a future admin endpoint.
 */
@Component
public class StartupGtfsLoader implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(StartupGtfsLoader.class);

    private final StopRepository stopRepository;
    private final GtfsImportService gtfsImportService;
    private final String stopsFilePath;

    public StartupGtfsLoader(StopRepository stopRepository,
                              GtfsImportService gtfsImportService,
                              @Value("${bkk.gtfs.stops-file}") String stopsFilePath) {
        this.stopRepository = stopRepository;
        this.gtfsImportService = gtfsImportService;
        this.stopsFilePath = stopsFilePath;
    }

    @Override
    public void run(String... args) throws Exception {
        if (stopRepository.count() > 0) {
            log.info("Stops table already populated ({} rows) - skipping GTFS import.",
                    stopRepository.count());
            return;
        }
        log.info("Stops table is empty - importing from {}", stopsFilePath);
        gtfsImportService.importStops(Path.of(stopsFilePath));
    }
}

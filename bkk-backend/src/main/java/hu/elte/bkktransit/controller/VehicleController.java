package hu.elte.bkktransit.controller;

import hu.elte.bkktransit.service.FutarClient;
import hu.elte.bkktransit.service.VehiclePosition;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/vehicles")
public class VehicleController {

    private final FutarClient futarClient;

    public VehicleController(FutarClient futarClient) {
        this.futarClient = futarClient;
    }

    // GET /api/vehicles?lat=&lon=&radius= (radius in metres, default 2000).
    // We proxy BKK's feed through our own backend rather than having the
    // frontend call BKK directly - keeps bkk.futar.api-key server-side only,
    // never shipped to a browser where anyone could read it out of network
    // requests or page source.
    @GetMapping
    public List<VehiclePosition> nearbyVehicles(
            @RequestParam double lat,
            @RequestParam double lon,
            @RequestParam(defaultValue = "2000") int radius) {
        return futarClient.vehiclesNear(lat, lon, radius);
    }
}

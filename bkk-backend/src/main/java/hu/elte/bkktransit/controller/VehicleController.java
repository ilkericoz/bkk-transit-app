package hu.elte.bkktransit.controller;

import hu.elte.bkktransit.service.DelayPredictionClient;
import hu.elte.bkktransit.service.DelayPredictionRequest;
import hu.elte.bkktransit.service.DelayPredictionResult;
import hu.elte.bkktransit.service.FutarClient;
import hu.elte.bkktransit.service.VehiclePosition;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/vehicles")
public class VehicleController {

    private final FutarClient futarClient;
    private final DelayPredictionClient delayPredictionClient;

    public VehicleController(FutarClient futarClient, DelayPredictionClient delayPredictionClient) {
        this.futarClient = futarClient;
        this.delayPredictionClient = delayPredictionClient;
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

    // POST /api/vehicles/delay-prediction - proxies the stage-5 Python
    // sidecar the same way nearbyVehicles() proxies BKK itself: the
    // frontend talks to one origin (this backend), not two. Called lazily,
    // per-vehicle, when someone actually opens that vehicle's map popup
    // (see app.js) - not for every vehicle on every 10s poll, which would
    // be ~1700 calls/10s to the sidecar for predictions nobody is looking at.
    @PostMapping("/delay-prediction")
    public DelayPredictionResult delayPrediction(@RequestBody DelayPredictionRequest request) {
        return delayPredictionClient.predict(request);
    }
}

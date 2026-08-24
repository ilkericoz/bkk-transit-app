package hu.elte.bkktransit.controller;

import hu.elte.bkktransit.entity.Stop;
import hu.elte.bkktransit.repository.StopRepository;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/stops")
public class StopController {

    private final StopRepository stopRepository;

    public StopController(StopRepository stopRepository) {
        this.stopRepository = stopRepository;
    }

    // GET /api/stops -> every stop. Fine for a ~6k-row table; we'll add
    // pagination if/when this ever needs to scale further.
    @GetMapping
    public List<Stop> getAllStops() {
        return stopRepository.findAll();
    }

    // GET /api/stops/{id} -> one stop, or a plain 404 if it doesn't exist.
    @GetMapping("/{stopId}")
    public ResponseEntity<Stop> getStop(@PathVariable String stopId) {
        return stopRepository.findById(stopId)
                .map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }
}

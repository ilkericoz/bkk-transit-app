package hu.elte.bkktransit.controller;

import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import hu.elte.bkktransit.entity.Stop;
import hu.elte.bkktransit.repository.StopRepository;

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

    // GET /api/stops/search?name=... -> partial, case-insensitive name match,
    // e.g. "nyugati" finds "Nyugati pu.". Uses the derived query method below.
    @GetMapping("/search")
    public List<Stop> searchStops(@RequestParam String name) {
        return stopRepository.findByStopNameContainingIgnoreCase(name);
    }

    // POST /api/stops -> create a new stop from a JSON body. @RequestBody tells
    // Spring to deserialize the request's JSON into a Stop (via Jackson) instead
    // of reading it from the URL/query string the way @PathVariable/@RequestParam
    // do. 201 Created (not 200 OK) is the REST convention for "made a new thing".
    @PostMapping
    public ResponseEntity<Stop> createStop(@RequestBody Stop stop) {
        Stop saved = stopRepository.save(stop);
        return ResponseEntity.status(HttpStatus.CREATED).body(saved);
    }
}

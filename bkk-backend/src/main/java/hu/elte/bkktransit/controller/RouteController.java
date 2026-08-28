package hu.elte.bkktransit.controller;

import java.util.List;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import hu.elte.bkktransit.entity.Route;
import hu.elte.bkktransit.repository.RouteRepository;

// Same shape as StopController - GET /api/routes and GET /api/routes/{id}.
// Only ~390 rows total, so no pagination/search needed the way stops (~6k
// rows) eventually got a /search endpoint.
@RestController
@RequestMapping("/api/routes")
public class RouteController {

    private final RouteRepository routeRepository;

    public RouteController(RouteRepository routeRepository) {
        this.routeRepository = routeRepository;
    }

    @GetMapping
    public List<Route> getAllRoutes() {
        return routeRepository.findAll();
    }

    @GetMapping("/{routeId}")
    public ResponseEntity<Route> getRoute(@PathVariable String routeId) {
        return routeRepository.findById(routeId)
                .map(ResponseEntity::ok)
                .orElse(ResponseEntity.notFound().build());
    }
}

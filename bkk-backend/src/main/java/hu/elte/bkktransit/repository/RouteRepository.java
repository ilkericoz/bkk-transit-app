package hu.elte.bkktransit.repository;

import hu.elte.bkktransit.entity.Route;
import org.springframework.data.jpa.repository.JpaRepository;

/**
 * Same shape as StopRepository - JpaRepository<Route, String> already gives
 * us save(), findById(), findAll(), count(), etc. for free.
 */
public interface RouteRepository extends JpaRepository<Route, String> {
}

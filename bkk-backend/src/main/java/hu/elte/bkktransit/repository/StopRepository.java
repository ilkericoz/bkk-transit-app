package hu.elte.bkktransit.repository;

import hu.elte.bkktransit.entity.Stop;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

/**
 * Extending JpaRepository<Stop, String> already gives us save(), findById(),
 * findAll(), count(), deleteAll(), etc. for free — Spring generates the
 * implementation at startup based on the entity (Stop) and its @Id type
 * (String). No SQL, no implementation class, needed for these basics.
 */
public interface StopRepository extends JpaRepository<Stop, String> {

    // No body here either — Spring Data parses this method NAME and derives
    // the query from it:
    //   findBy       -> SELECT ... WHERE
    //   StopName     -> the stopName field on Stop
    //   Containing   -> LIKE '%value%' (a substring match, not exact equals)
    //   IgnoreCase   -> case-insensitive comparison
    // Together: SELECT * FROM stops WHERE LOWER(stop_name) LIKE LOWER('%value%')
    List<Stop> findByStopNameContainingIgnoreCase(String stopName);
}

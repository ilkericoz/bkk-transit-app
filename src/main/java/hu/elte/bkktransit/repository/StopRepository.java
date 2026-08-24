package hu.elte.bkktransit.repository;

import hu.elte.bkktransit.entity.Stop;
import org.springframework.data.jpa.repository.JpaRepository;

/**
 * Extending JpaRepository<Stop, String> already gives us save(), findById(),
 * findAll(), count(), deleteAll(), etc. for free — Spring generates the
 * implementation at startup based on the entity (Stop) and its @Id type
 * (String). No SQL, no implementation class, needed for these basics.
 */
public interface StopRepository extends JpaRepository<Stop, String> {
}

package hu.elte.bkktransit.repository;

import hu.elte.bkktransit.entity.VehiclePositionSnapshot;
import org.springframework.data.jpa.repository.JpaRepository;

/**
 * Just the free CRUD methods for now (save() is the only one the consumer
 * actually calls). Query methods for pulling history back out - e.g. "all
 * snapshots for route X between two timestamps" - are a stage 5 concern,
 * added once there's something that actually needs to read this data back.
 */
public interface VehiclePositionSnapshotRepository extends JpaRepository<VehiclePositionSnapshot, Long> {
}

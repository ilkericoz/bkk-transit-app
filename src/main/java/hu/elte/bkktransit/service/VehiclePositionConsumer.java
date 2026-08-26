package hu.elte.bkktransit.service;

import hu.elte.bkktransit.entity.VehiclePositionSnapshot;
import hu.elte.bkktransit.repository.VehiclePositionSnapshotRepository;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;

import java.time.Instant;

/**
 * The consumer half of the ingestion pipeline. Spring runs this on its own
 * background listener thread, blocked waiting on the queue, invoking this
 * method as each message arrives - this class never calls BKK and has no
 * idea anything runs on a timer; it just reacts to whatever the queue hands
 * it. That's the decoupling a broker buys you: this class could be changed,
 * restarted, or scaled to run several instances in parallel, all without
 * VehicleIngestionProducer changing at all.
 */
@Component
public class VehiclePositionConsumer {

    private final VehiclePositionSnapshotRepository repository;

    public VehiclePositionConsumer(VehiclePositionSnapshotRepository repository) {
        this.repository = repository;
    }

    @RabbitListener(queues = "${bkk.rabbitmq.queue}")
    public void onVehiclePosition(VehiclePosition vehicle) {
        VehiclePositionSnapshot snapshot = new VehiclePositionSnapshot(
                vehicle.vehicleId(),
                vehicle.routeId(),
                vehicle.vehicleRouteType(),
                vehicle.lat(),
                vehicle.lon(),
                vehicle.bearing(),
                vehicle.label(),
                vehicle.lastUpdateTime(),
                Instant.now()
        );
        repository.save(snapshot);
    }
}

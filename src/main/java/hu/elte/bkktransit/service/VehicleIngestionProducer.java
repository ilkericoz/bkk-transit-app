package hu.elte.bkktransit.service;

import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.util.List;

/**
 * The producer half of the ingestion pipeline. Runs on a fixed timer,
 * independent of any browser request - unlike VehicleController, which only
 * calls FutarClient when someone's actually polling GET /api/vehicles, this
 * runs continuously so history keeps accumulating even with nobody looking
 * at the map.
 *
 * Deliberately thin: it doesn't touch Postgres and doesn't know who (if
 * anyone) is listening on the other end. It fetches from BKK and publishes
 * to the exchange - a separate consumer class decides what happens to each
 * message. That separation is the whole point of putting a broker between
 * them: this class could stop, restart, or change its polling logic without
 * the consumer ever noticing.
 */
@Component
public class VehicleIngestionProducer {

    private final FutarClient futarClient;
    private final RabbitTemplate rabbitTemplate;
    private final String exchangeName;
    private final String routingKey;
    private final double centerLat;
    private final double centerLon;
    private final int radiusMeters;

    public VehicleIngestionProducer(FutarClient futarClient,
                                     RabbitTemplate rabbitTemplate,
                                     @Value("${bkk.rabbitmq.exchange}") String exchangeName,
                                     @Value("${bkk.rabbitmq.routing-key}") String routingKey,
                                     @Value("${bkk.ingestion.center-lat}") double centerLat,
                                     @Value("${bkk.ingestion.center-lon}") double centerLon,
                                     @Value("${bkk.ingestion.radius-meters}") int radiusMeters) {
        this.futarClient = futarClient;
        this.rabbitTemplate = rabbitTemplate;
        this.exchangeName = exchangeName;
        this.routingKey = routingKey;
        this.centerLat = centerLat;
        this.centerLon = centerLon;
        this.radiusMeters = radiusMeters;
    }

    /**
     * fixedDelayString (not fixedRateString): waits this long after one run
     * FINISHES before starting the next, rather than firing every N ms
     * regardless of whether the previous run is still going. Matters here
     * because fetchFromBkk() is a network call that could occasionally be
     * slow - fixedRate could pile up overlapping runs if BKK ever lagged.
     */
    @Scheduled(fixedDelayString = "${bkk.ingestion.poll-interval-ms}")
    public void pollAndPublish() {
        List<VehiclePosition> vehicles = futarClient.vehiclesNear(centerLat, centerLon, radiusMeters);
        for (VehiclePosition vehicle : vehicles) {
            rabbitTemplate.convertAndSend(exchangeName, routingKey, vehicle);
        }
    }
}

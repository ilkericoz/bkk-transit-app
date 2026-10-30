package hu.elte.bkktransit.service;

import hu.elte.bkktransit.entity.VehiclePositionSnapshot;
import hu.elte.bkktransit.repository.VehiclePositionSnapshotRepository;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.time.Instant;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;

/**
 * The queue consumer is the only place a live sighting becomes a database
 * row, so every field has to survive the copy - a dropped field here (say
 * serviceDate or stopSequence) silently breaks delay labels downstream.
 */
class VehiclePositionConsumerTest {

    @Test
    void savesASnapshotWithEveryFieldCopied() {
        VehiclePositionSnapshotRepository repository = mock(VehiclePositionSnapshotRepository.class);
        VehiclePositionConsumer consumer = new VehiclePositionConsumer(repository);

        VehiclePosition vehicle = new VehiclePosition(
                "BKK_V123", "BKK_3060", "TRAM", 47.497, 19.054, 182.5, "6",
                1_790_000_000L, "BKK_T1", "BKK_F00001", 7, "20261030",
                "STOPPED_AT", 100, false, true);

        Instant before = Instant.now();
        consumer.onVehiclePosition(vehicle);
        Instant after = Instant.now();

        ArgumentCaptor<VehiclePositionSnapshot> captor = ArgumentCaptor.forClass(VehiclePositionSnapshot.class);
        verify(repository).save(captor.capture());
        VehiclePositionSnapshot saved = captor.getValue();

        assertThat(saved.getVehicleId()).isEqualTo("BKK_V123");
        assertThat(saved.getRouteId()).isEqualTo("BKK_3060");
        assertThat(saved.getVehicleRouteType()).isEqualTo("TRAM");
        assertThat(saved.getLat()).isEqualTo(47.497);
        assertThat(saved.getLon()).isEqualTo(19.054);
        assertThat(saved.getBearing()).isEqualTo(182.5);
        assertThat(saved.getLabel()).isEqualTo("6");
        assertThat(saved.getLastUpdateTime()).isEqualTo(1_790_000_000L);
        assertThat(saved.getTripId()).isEqualTo("BKK_T1");
        assertThat(saved.getStopId()).isEqualTo("BKK_F00001");
        assertThat(saved.getStopSequence()).isEqualTo(7);
        assertThat(saved.getServiceDate()).isEqualTo("20261030");
        assertThat(saved.getStatus()).isEqualTo("STOPPED_AT");
        assertThat(saved.getStopDistancePercent()).isEqualTo(100);
        assertThat(saved.getDeviated()).isFalse();
        assertThat(saved.getStale()).isTrue();
        assertThat(saved.getRecordedAt()).isBetween(before, after);
    }

    @Test
    void keepsOptionalFieldsNullWhenBkkLeavesThemOut() {
        VehiclePositionSnapshotRepository repository = mock(VehiclePositionSnapshotRepository.class);
        VehiclePositionConsumer consumer = new VehiclePositionConsumer(repository);

        VehiclePosition vehicle = new VehiclePosition(
                "BKK_V456", null, null, 47.5, 19.0, null, null,
                1_790_000_000L, null, null, null, null, null, null, null, null);

        consumer.onVehiclePosition(vehicle);

        ArgumentCaptor<VehiclePositionSnapshot> captor = ArgumentCaptor.forClass(VehiclePositionSnapshot.class);
        verify(repository).save(captor.capture());
        VehiclePositionSnapshot saved = captor.getValue();

        assertThat(saved.getBearing()).isNull();
        assertThat(saved.getStopSequence()).isNull();
        assertThat(saved.getDeviated()).isNull();
        assertThat(saved.getStale()).isNull();
    }
}

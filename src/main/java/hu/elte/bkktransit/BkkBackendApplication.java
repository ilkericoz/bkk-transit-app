package hu.elte.bkktransit;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

// EnableScheduling turns on @Scheduled method processing app-wide - without
// it, VehicleIngestionProducer's @Scheduled method would just sit there
// unused, since Spring doesn't scan for it unless this is explicitly on.
@SpringBootApplication
@EnableScheduling
public class BkkBackendApplication {

	public static void main(String[] args) {
		SpringApplication.run(BkkBackendApplication.class, args);
	}

}

package hu.elte.bkktransit.config;

import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.support.converter.JacksonJsonMessageConverter;
import org.springframework.amqp.support.converter.MessageConverter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Declares the AMQP topology for the vehicle-position ingestion pipeline:
 * one exchange, one queue, one binding between them. A producer (see the
 * scheduled job in service/) publishes to the exchange; it never talks to
 * the queue directly - the exchange is what decides, based on the routing
 * key, which bound queue(s) actually receive the message. With a single
 * queue this looks like unnecessary indirection, but it's the real AMQP
 * model, and it's what would let a second consumer subscribe to the same
 * data later (e.g. a "notify connected clients live" queue) without the
 * producer changing at all.
 */
@Configuration
public class RabbitConfig {

    @Value("${bkk.rabbitmq.exchange}")
    private String exchangeName;

    @Value("${bkk.rabbitmq.queue}")
    private String queueName;

    @Value("${bkk.rabbitmq.routing-key}")
    private String routingKey;

    @Bean
    public DirectExchange vehiclePositionExchange() {
        return new DirectExchange(exchangeName);
    }

    @Bean
    public Queue vehiclePositionQueue() {
        // durable=true (the Queue(String) default) so the queue itself
        // survives a RabbitMQ restart - messages sitting in it are a
        // separate durability question (persistent delivery), not needed
        // yet at this project's scale.
        return new Queue(queueName);
    }

    @Bean
    public Binding vehiclePositionBinding(Queue vehiclePositionQueue, DirectExchange vehiclePositionExchange) {
        return BindingBuilder.bind(vehiclePositionQueue).to(vehiclePositionExchange).with(routingKey);
    }

    /**
     * Swaps Spring AMQP's default message format (raw Java serialization -
     * unreadable, and unusable by anything that isn't Java) for JSON. Spring
     * Boot auto-detects this bean and wires it into both the RabbitTemplate
     * (producer side) and the @RabbitListener container factory (consumer
     * side) automatically, so nothing else needs to reference it directly.
     *
     * JacksonJsonMessageConverter, not the older Jackson2JsonMessageConverter
     * - this project runs on Jackson 3 (Boot 4.1 switched to it; see
     * FutarClient's tools.jackson.* import), and Jackson2JsonMessageConverter
     * still reaches for classic Jackson 2 internally, which isn't on our
     * classpath at all. Confirmed by an actual NoClassDefFoundError at
     * startup, not assumed - worth remembering for this project.
     */
    @Bean
    public MessageConverter jsonMessageConverter() {
        return new JacksonJsonMessageConverter();
    }
}

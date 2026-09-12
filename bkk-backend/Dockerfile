# Runtime-only image - takes an already-built jar rather than doing a
# multi-stage Maven build inside Docker. This project's dependencies are
# already resolved into the local ~/.m2 cache from regular dev work; a
# maven-image build stage would re-resolve everything from Maven Central
# inside the container with no cache, which is slower and depends on
# network access at build time for no real benefit here. Build the jar with
# the host's own Maven first:
#   mvn -o -DskipTests package
# then `docker compose build` (see ../docker-compose.yml).
FROM eclipse-temurin:25-jre

WORKDIR /app

COPY target/bkk-backend-*.jar app.jar

EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]

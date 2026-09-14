# Serves the already-trained model (main.py) - training itself (train_model.py,
# build_delay_dataset.py) stays a host-side dev workflow, not containerized.
# Those are one-off/occasional scripts that need direct Postgres access and a
# fair amount of RAM/CPU for a 6M+-row pandas job; the thing that actually
# needs to run continuously and survive a reboot is the serving process, so
# that's the only part stage 6 needs to containerize.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py delay_model.py gtfs_schedule.py weather.py ./

# models/ (delay_model.joblib) and data/ are NOT copied in - they're
# gitignored/regenerable and mounted as volumes by docker-compose.yml
# instead, same reasoning as the GTFS data mount: keeps the image small and
# means retraining on the host (scripts/train_model.py) takes effect on next
# container restart with no rebuild needed.

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

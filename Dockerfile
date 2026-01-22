FROM python:3.11-slim

WORKDIR /app

# Install only what we need: paho-mqtt
RUN pip install --no-cache-dir paho-mqtt

# Copy pyjacuzzi files directly (no git clone needed!)
COPY jacuzzi.py /app/
COPY balboa.py /app/

# Copy our pure bridge script
COPY mqtt_bridge.py /app/

# Run the bridge
CMD ["python", "-u", "/app/mqtt_bridge.py"]

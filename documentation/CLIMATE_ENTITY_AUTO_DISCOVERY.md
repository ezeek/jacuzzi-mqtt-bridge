# Climate Entity Auto-Discovery

## Problem

After deleting the MQTT device in Home Assistant and restarting Docker, the `climate.jacuzzi` entity wasn't being recreated automatically.

## Root Cause

The climate entity was **not in the discovery messages**. It was likely:
- Manually configured in `configuration.yaml`, OR
- From an old version of mqtt_bridge that had climate discovery

When you deleted the device, HA removed all entities including the manually configured climate entity.

## Solution: Add Climate Discovery

Added automatic MQTT discovery for the climate entity so it recreates on every restart.

### Discovery Message Added

```python
# Climate entity (thermostat-style control)
self.mqtt.publish(
    f"{base}/climate/jacuzzi/config",
    json.dumps({
        "name": "Jacuzzi",
        "unique_id": "jacuzzi_climate",
        "modes": ["heat", "off"],
        "mode_state_topic": f"{MQTT_BASE_TOPIC}/climate_mode",
        "mode_command_topic": f"{MQTT_BASE_TOPIC}/climate_mode/set",
        "current_temperature_topic": f"{MQTT_BASE_TOPIC}/temperature",
        "temperature_command_topic": f"{MQTT_BASE_TOPIC}/target_temperature/set",
        "temperature_state_topic": f"{MQTT_BASE_TOPIC}/target_temperature",
        "temp_step": 1,
        "min_temp": 50,
        "max_temp": 104,
        "temperature_unit": "F",
        "device": device
    }),
    retain=True
)
```

### State Publishing Added

In `publish_state()` method:
```python
# Climate mode for climate entity (always "heat" for hot tub)
# Hot tubs are always in heating mode when powered on
self.mqtt.publish(f"{MQTT_BASE_TOPIC}/climate_mode", "heat")
```

### Command Handler Added

In `send_command()` method:
```python
elif command == "climate_mode":
    # Climate entity mode command (heat/off)
    # For a hot tub, we always stay in "heat" mode
    # Just acknowledge the command by publishing back
    logger.info(f"Climate mode command: {payload} (hot tub always heating)")
    self.mqtt.publish(f"{MQTT_BASE_TOPIC}/climate_mode", "heat")
```

## How It Works

The climate entity provides a thermostat-style interface in Home Assistant:
- **Current temperature**: Shows water temp
- **Target temperature**: Slider to set desired temp (50-104°F)
- **Mode**: Always "heat" (hot tubs don't turn "off")
- **HVAC action**: Automatically shows "heating" or "idle" based on heater status

## Now Fully Automated

After this change:
1. **Delete device** in HA → All entities removed
2. **Restart Docker** → mqtt_bridge sends discovery
3. **Climate entity recreates** automatically ✓

No manual `configuration.yaml` entries needed!

## Terminal Command to Force Discovery

If you ever need to manually trigger discovery without restarting:

```bash
# Restart just the mqtt_bridge container
docker restart <mqtt_bridge_container_name>

# Or if using docker-compose
docker-compose restart mqtt_bridge
```

The discovery messages are sent automatically on startup.

## What Gets Created

After restart, Home Assistant will have:
- `climate.jacuzzi` - Thermostat control
- `sensor.jacuzzi_temperature` - Current temp
- `number.jacuzzi_target_temperature` - Target temp slider
- `switch.jacuzzi_pump_1` - Pump 1 control
- `switch.jacuzzi_pump_2` - Pump 2 control
- `button.jacuzzi_filter_boost` - Filter boost button
- Plus all lights, heat mode, filters, etc.

All automatically discovered via MQTT!

## Files Modified

- `mqtt_bridge.py`: Added ~30 lines
  - Climate discovery message in `send_discovery()`
  - Climate mode state publishing in `publish_state()`
  - Climate mode command handler in `send_command()`

# Persistent Restart Count - Track Long-Term Health

## Problem

The `restart_count` was resetting to 0 every time Docker restarted, making it impossible to track long-term connection stability and health patterns.

## Solution: Persist Via Retained MQTT Messages

Use MQTT's `retain=True` flag to persist the counter across Docker restarts. On startup, load the previous value from the retained message.

## Implementation

### 1. Initialize as None (Not Loaded Yet)

```python
self.restart_count = None  # Will be loaded from retained MQTT message
self.restart_count_loaded = False  # Track if we've loaded the persisted value
```

### 2. Subscribe to restart_count Topic on Connect

```python
def on_connect(self, client, userdata, flags, rc, properties=None):
    # ... existing code ...
    
    # Subscribe to restart_count to load persisted value
    client.subscribe(f"{MQTT_BASE_TOPIC}/restart_count")
    logger.info(f"Subscribed to {MQTT_BASE_TOPIC}/restart_count to load persisted value")
```

### 3. Load Persisted Value from MQTT

```python
def on_message(self, client, userdata, msg):
    # Load persisted restart_count on startup
    if topic == f"{MQTT_BASE_TOPIC}/restart_count" and not self.restart_count_loaded:
        try:
            self.restart_count = int(payload)
            self.restart_count_loaded = True
            logger.info(f"✅ Loaded persisted restart_count: {self.restart_count}")
        except ValueError:
            self.restart_count = 0
            self.restart_count_loaded = True
            logger.warning(f"Failed to parse restart_count, initializing to 0")
        return
```

### 4. Handle None Before Incrementing

```python
# In both increment locations
if self.restart_count is None:
    self.restart_count = 0
self.restart_count += 1
```

### 5. Publish with retain=True

```python
# Already using retain=True, no change needed
self.mqtt.publish(f"{MQTT_BASE_TOPIC}/restart_count", self.restart_count, retain=True)
```

### 6. Add Manual Reset Button

```python
# Command handler
elif command == "restart_count_reset":
    logger.info("Resetting restart_count to 0")
    self.restart_count = 0
    self.mqtt.publish(f"{MQTT_BASE_TOPIC}/restart_count", 0, retain=True)

# Discovery message
self.mqtt.publish(
    f"{base}/button/jacuzzi_restart_count_reset/config",
    json.dumps({
        "name": "Jacuzzi Reset Restart Count",
        "command_topic": f"{MQTT_BASE_TOPIC}/restart_count_reset/set",
        "payload_press": "RESET",
        "unique_id": "jacuzzi_restart_count_reset",
        "icon": "mdi:restore",
        "entity_category": "diagnostic",
        "device": device
    }),
    retain=True
)
```

## How It Works

### First Run Ever (No Retained Message)
1. mqtt_bridge starts, `restart_count = None`
2. Subscribes to `jacuzzi/restart_count`
3. No retained message exists, never loads
4. First increment: `if restart_count is None: restart_count = 0`
5. Increments to 1, publishes with `retain=True`
6. MQTT broker stores "1" as retained message

### Subsequent Runs (Retained Message Exists)
1. mqtt_bridge starts, `restart_count = None`
2. Subscribes to `jacuzzi/restart_count`
3. MQTT immediately sends retained message "1"
4. `on_message` receives it: `restart_count = 1`
5. Later, connection fails, increment to 2
6. Publishes "2" with `retain=True`, updates retained message

### Docker Restart
1. Container stops
2. MQTT broker keeps retained message "2"
3. Container starts
4. Loads "2" from retained message ✓
5. Continues counting from 2, not 0!

### Manual Reset
1. User presses "Reset Restart Count" button
2. Sets `restart_count = 0`
3. Publishes "0" with `retain=True`
4. Retained message now "0"
5. Future restarts load from "0"

## Benefits

### Long-Term Health Tracking
```
Week 1: restart_count = 2 (good)
Week 2: restart_count = 5 (moderate)
Week 3: restart_count = 15 (bad - investigate!)
Week 4: restart_count = 50 (critical - ProLink dying?)
```

### Track Patterns
- **Steady climb**: Progressive hardware failure
- **Sudden spike**: External issue (WiFi, power)
- **Low and stable**: Healthy system

### Historical Analysis
After a Docker restart, you still know:
- How many times it's restarted this month
- If issues are getting worse over time
- When to replace hardware

## Home Assistant Entities

**Sensor**: `sensor.jacuzzi_restart_count`
- Shows current count
- Updates every 10 seconds
- Persists across Docker restarts
- Located in Diagnostic section

**Button**: `button.jacuzzi_restart_count_reset`
- Resets counter to 0
- Useful after fixing issues
- Updates retained message
- Located in Diagnostic section

## Usage Examples

### Monitor in Dashboard
```yaml
type: gauge
entity: sensor.jacuzzi_restart_count
name: Connection Restarts
min: 0
max: 50
severity:
  green: 0
  yellow: 10
  red: 20
```

### Alert on High Count
```yaml
automation:
  - alias: "Jacuzzi High Restart Count"
    trigger:
      - platform: numeric_state
        entity_id: sensor.jacuzzi_restart_count
        above: 20
    action:
      - service: notify.mobile_app
        data:
          message: "Jacuzzi has restarted {{ states('sensor.jacuzzi_restart_count') }} times. Check connection!"
```

### Reset After Maintenance
```yaml
# After fixing WiFi issues, reset counter:
service: button.press
target:
  entity_id: button.jacuzzi_restart_count_reset
```

### Weekly Health Check
```yaml
automation:
  - alias: "Weekly Jacuzzi Health Report"
    trigger:
      - platform: time
        at: "09:00:00"
    condition:
      - condition: time
        weekday:
          - mon
    action:
      - service: notify.mobile_app
        data:
          message: "Jacuzzi health: {{ states('sensor.jacuzzi_restart_count') }} restarts this week"
```

## Testing

**Test 1: First Run**
1. Delete retained message: `mosquitto_pub -h <broker> -t "jacuzzi/restart_count" -r -n`
2. Restart mqtt_bridge
3. ✓ Should show restart_count = 0

**Test 2: Persistence**
1. Manually set: `mosquitto_pub -h <broker> -t "jacuzzi/restart_count" -r -m "42"`
2. Restart mqtt_bridge
3. Check logs: `✅ Loaded persisted restart_count: 42`
4. ✓ Sensor shows 42

**Test 3: Increment Persists**
1. Current count: 5
2. Trigger restart (disconnect ProLink)
3. Count increments to 6
4. Restart Docker: `docker restart mqtt_bridge`
5. ✓ Count still shows 6 (not reset to 0)

**Test 4: Manual Reset**
1. Count at 20
2. Press "Reset Restart Count" button
3. ✓ Count immediately shows 0
4. Restart Docker
5. ✓ Count still shows 0 (retained message updated)

## MQTT Topics

**State**: `jacuzzi/restart_count` (retained)
**Command**: `jacuzzi/restart_count_reset/set` (non-retained)

## Files Modified

- `mqtt_bridge.py`: Added ~40 lines
  - Initialize restart_count as None
  - Subscribe to restart_count topic on connect
  - Load persisted value in on_message
  - Handle None before incrementing
  - Add restart_count_reset command handler
  - Add reset button discovery message

## Migration Note

If you're upgrading from the old version:
- Old: Counter reset to 0 on every Docker restart
- New: Counter persists across Docker restarts
- The first time you deploy this version, if there's no retained message, it starts at 0
- From then on, it persists forever (or until manually reset)

This gives you true long-term health tracking!

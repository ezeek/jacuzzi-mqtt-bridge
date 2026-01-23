# Filter Boost Implementation Summary

## Discovery from Wireshark Analysis

### What Filter Boost Actually Does

Through Wireshark packet capture of the ProLink app, we discovered:

1. **"Filter Boost" is NOT a dedicated spa command**
2. **It's a toggle command**: `0x17 0x0d` 
3. **The spa controller handles timing**: Automatically turns off after 20 minutes
4. **Pressing boost again resets the timer**: Re-sends the same command

### Command Structure

```
Raw bytes: 7e 06 0a bf 17 0d a3 7e

Breakdown:
  0x7e = Frame start/end marker
  0x06 = Message length (6 bytes)
  0x0a = Destination address (spa controller)
  0xbf = Source address (client)
  0x17 = Command type (PUMP TOGGLE)
  0x0d = Parameter (filter boost mode)
  0xa3 = Checksum
  0x7e = Frame end marker
```

### How Pump Commands Work

All pump commands use **toggle**, not set:
- `0x17 0x04` = Toggle Pump 1 (same command for ON→OFF and OFF→ON)
- `0x17 0x05` = Toggle Pump 2
- `0x17 0x0d` = Toggle Filter Boost (turns on all filtration pumps)

### ProLink App Behavior

When "Filter Boost" is pressed:
1. App sends `0x17 0x0d` (3 times for reliability)
2. Spa turns on Pump 1, Pump 2, and Circ Pump
3. App UI shows all pumps including non-existent Pump 3
4. App sends cleanup commands `0x04` and `0x05` to correct UI state
5. Spa controller starts 20-minute timer
6. After 20 minutes, spa automatically turns pumps off

## Implementation in mqtt_bridge.py

### Command Handler (added to `send_command` method)

```python
elif command == "filter_boost":
    # Filter boost command from ProLink app analysis:
    # Sends 0x17 0x0d which toggles on filtration pumps
    # Spa controller handles 20-minute timer and auto-off
    logger.info("Sending filter boost command (0x17 0x0d)")
    raw_cmd = bytes.fromhex("7e060abf170da37e")
    self.spa.writer.write(raw_cmd)
    await self.spa.writer.drain()
```

### Home Assistant Discovery (added to `send_discovery` method)

```python
# Filter Boost (button)
self.mqtt.publish(
    f"{base}/button/jacuzzi_filter_boost/config",
    json.dumps({
        "name": "Jacuzzi Filter Boost",
        "command_topic": f"{MQTT_BASE_TOPIC}/filter_boost/set",
        "payload_press": "ON",
        "unique_id": "jacuzzi_filter_boost",
        "icon": "mdi:pump",
        "device": device
    }),
    retain=True
)
```

## Usage in Home Assistant

Once mqtt_bridge is restarted, a new button entity will appear:

**Entity**: `button.jacuzzi_filter_boost`

**To trigger via automation**:
```yaml
service: button.press
target:
  entity_id: button.jacuzzi_filter_boost
```

**To add to dashboard**:
```yaml
type: button
entity: button.jacuzzi_filter_boost
name: Filter Boost
icon: mdi:pump
```

## Behavior

- **Press once**: Activates filter boost (all filtration pumps run)
- **Spa controller**: Automatically turns off after 20 minutes
- **Press again**: Resets the 20-minute timer
- **Manual pump off**: Cancels filter boost
- **No state tracking needed**: It's a fire-and-forget command

## Testing

1. Deploy updated mqtt_bridge.py
2. Restart the container/service
3. Check Home Assistant for new button entity
4. Press the button
5. Verify pumps turn on
6. Wait 20 minutes
7. Verify pumps turn off automatically

## Files Modified

- `mqtt_bridge.py`: Added ~15 lines
  - Command handler in `send_command()` method
  - Discovery message in `send_discovery()` method

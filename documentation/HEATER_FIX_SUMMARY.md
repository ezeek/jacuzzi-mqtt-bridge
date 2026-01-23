# Heater Status Detection Fix - CORRECTED

## Problem
The previous heater detection logic was using pyjacuzzi's `spa.get_heatstate()` method, which was not correctly identifying heater state.

## Testing Results - CORRECTED
Live tap output analysis with temperature changes showed:
- When heater was **OFF**: Byte 16 = `0x16` (binary: `0b00010110` - bit 0 = 0)
- When heater was **ON**: Byte 16 = `0x17` (binary: `0b00010111` - bit 0 = 1)

From the test log:
```
[16:43:06.105] RX (heater OFF): 
7e25ffaf16082ab7011a420064fa62021601020000630205016b800000b200000000000000e27e
                                    ^^ byte 16 = 0x16 (bit 0 = 0, heater OFF)

[16:43:22.739] TX: Set temp to 102°F (above current 100°F)
7e060abf2066277e

[16:43:22.884] RX (heater ON):
7e25ffaf16082ab7011a420064fa66021701020000630205016b800000b200000000000000cc7e
                                    ^^ byte 16 = 0x17 (bit 0 = 1, heater ON)

[16:43:37.897] TX: Set temp to 97°F (below current 100°F)
7e060abf2061327e

[16:43:38.189] RX (heater OFF):
7e25ffaf16082bb7011a420064fa61021601020000630205016b800000b200000000000000437e
                                    ^^ byte 16 = 0x16 (bit 0 = 0, heater OFF)
```

## Solution
Check **bit 0** of byte 16 (index 16 in the data array):
- Bit 0 set (`0x01`) → Heater ON
- Bit 0 clear → Heater OFF

## Code Changes
Minimal changes to maintain clean git diff:

**Old Logic (REMOVED from publish_state):**
```python
# Heat state - show if heater is actively heating
heatstate = self.spa.get_heatstate(False)  # 0=Idle, 1=Heating, 2=Heat Waiting
heatstate_names = {0: "Idle", 1: "Heating", 2: "Heat Waiting"}
heatstate_text = heatstate_names.get(heatstate, f"Unknown ({heatstate})")
heater_on = "ON" if heatstate == 1 else "OFF"
logger.info(f"Publishing heat_state: {heatstate_text} (value={heatstate})")
logger.info(f"Publishing heater_on: {heater_on}")
self.mqtt.publish(f"{MQTT_BASE_TOPIC}/heat_state", heatstate_text)
self.mqtt.publish(f"{MQTT_BASE_TOPIC}/heater_on", heater_on)
```

**New Logic (ADDED to debug_process_message):**
```python
# HEATER STATUS - Check bit 0 of byte 16 (tap output verification)
# Bit 0 set (0x01) = Heater ON, Bit 0 clear = Heater OFF
if len(data) > 16:
    byte16 = data[16]
    heater_bit = byte16 & 0x01  # Extract bit 0
    
    raw_heater_on = "ON" if heater_bit == 1 else "OFF"
    raw_heat_state = "Heating" if heater_bit == 1 else "Idle"
    
    logger.info(f"       🔥 BYTE 16: 0x{byte16:02X} = 0b{byte16:08b}")
    logger.info(f"          Heater bit (bit 0): {heater_bit} -> heater_on={raw_heater_on}, heat_state={raw_heat_state}")
    
    # Publish immediately to MQTT
    try:
        if self.mqtt:
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/heater_on", raw_heater_on)
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/heat_state", raw_heat_state)
    except Exception as e:
        logger.debug(f"MQTT publish failed for heater status: {e}")
```

## Files Changed
- `mqtt_bridge.py` - Heater detection in debug_process_message

## Verification Method
The correct byte and bit were identified by:
1. Capturing live protocol traffic with tap server on port 8889
2. Manually changing temperature above current (heater should turn ON)
3. Comparing frames byte-by-byte to find what changed
4. Manually changing temperature below current (heater should turn OFF)
5. Verifying the byte changed back

Only three bytes changed between heater ON/OFF states:
- Byte 6: Counter (increments over time)
- Byte 14: Set temperature value
- **Byte 16: Bit 0 indicates heater ON (1) or OFF (0)** ✓
- Byte 37: Checksum

## Expected Behavior
The `jacuzzi/heater_on` MQTT topic will now accurately reflect:
- "ON" when the physical heater is energized (verified by tap output)
- "OFF" when the physical heater is not energized

Updates are published in real-time on every status frame (~3Hz).

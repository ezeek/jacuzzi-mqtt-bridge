# Climate Action Fix - Show Heating vs Idle

## Problem

The climate entity thermostat card always showed "Heat" even when the heater was idle and not actively heating.

## Root Cause

The climate entity was missing the `action_topic` configuration. Without this:
- **Mode** shows what the thermostat is set to ("heat" or "off")  
- **Action** shows what it's actually doing ("heating", "idle", "off")

Home Assistant climate entities default to showing the mode when action isn't provided.

## Solution

### 1. Added `action_topic` to Climate Discovery

```python
"action_topic": f"{MQTT_BASE_TOPIC}/climate_action",
```

This tells HA where to read the actual heating action.

### 2. Publish Climate Action Based on Heater State

Added to the byte 16 heater detection code:

```python
# Climate action for climate entity (heating/idle)
climate_action = "heating" if heater_bit == 1 else "idle"

# Publish immediately to MQTT
self.mqtt.publish(f"{MQTT_BASE_TOPIC}/climate_action", climate_action)
```

## How It Works

**When heater is ON (byte 16 bit 0 = 1):**
- `heater_on` = "ON"
- `heat_state` = "Heating"
- `climate_action` = "heating" ← Climate card shows "Heating" 🔥

**When heater is OFF (byte 16 bit 0 = 0):**
- `heater_on` = "OFF"
- `heat_state` = "Idle"
- `climate_action` = "idle" ← Climate card shows "Idle" ✓

## Climate Entity States

The climate card will now show:
- **Mode**: "Heat" (what it's set to do)
- **Action**: 
  - "Heating" when actively heating water
  - "Idle" when not heating (target temp reached)

## Testing

After deploying and restarting:

**Test 1: Below Target Temp**
1. Set target to 104°F
2. Current temp is 102°F
3. ✓ Card should show: "Heat" mode, "Heating" action

**Test 2: At Target Temp**
1. Water reaches 104°F
2. Heater turns off
3. ✓ Card should show: "Heat" mode, "Idle" action

**Test 3: Above Target Temp**
1. Lower target to 100°F while water is 102°F
2. Heater stays off
3. ✓ Card should show: "Heat" mode, "Idle" action

## Visual Result

**Before:**
```
🌡️ Climate Card
Mode: Heat
Action: Heat  ← Always showed this
Temp: 102°F → 104°F
```

**After:**
```
🌡️ Climate Card  
Mode: Heat
Action: Heating  ← When actually heating
Temp: 102°F → 104°F

OR

Mode: Heat
Action: Idle     ← When at temperature
Temp: 104°F → 104°F
```

Much more accurate representation of what the hot tub is actually doing!

## Files Modified

- `mqtt_bridge.py`: Added 4 lines
  - 1 line in climate discovery (action_topic)
  - 3 lines in heater detection (calculate and publish climate_action)

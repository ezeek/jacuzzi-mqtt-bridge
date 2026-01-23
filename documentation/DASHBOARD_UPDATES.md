# Dashboard Updates Summary

## What Was Added

### 1. Filter Boost Button (Jets Section)

**Location**: Under the Jets heading, added as third button next to Jets 1 and Jets 2

```yaml
- type: button
  entity: button.jacuzzi_filter_boost
  name: Filter Boost
  icon: mdi:pump
  tap_action:
    action: call-service
    service: button.press
    target:
      entity_id: button.jacuzzi_filter_boost
```

**What it does:**
- Press to activate filter boost
- Turns on all filtration pumps for 20 minutes
- Spa controller handles the timer automatically

**Visual:**
```
💨 Jets
┌─────────┬─────────┬──────────────┐
│ Jets 1  │ Jets 2  │Filter Boost  │
│  [OFF]  │  [OFF]  │   [PRESS]    │
└─────────┴─────────┴──────────────┘
```

### 2. Connection Health Section (New Section)

**Location**: New section added after the Charts section, before Spa Settings

**Contains:**

#### A. Restart Count Chart (ApexCharts)
```yaml
- type: custom:apexcharts-card
  header:
    show: true
    title: Connection Restarts Over Time
    show_states: true
  graph_span: 7d  # Shows last 7 days
  update_interval: 1m
  series:
    - entity: sensor.jacuzzi_restart_count
      name: Restart Count
      color: '#ff6b6b'  # Red line
```

**Features:**
- **7-day view**: Shows restart history for the past week
- **Step line**: Jumps up when restarts occur
- **Red color**: Makes restarts obvious
- **Updates every minute**: Real-time monitoring

**What you'll see:**
- Flat line = Stable connection ✓
- Climbing line = Connection problems 🔴
- Sudden spike = Temporary issue
- Steady climb = Hardware degradation

#### B. Health Status Entities
```yaml
- type: entities
  entities:
    - sensor.jacuzzi_restart_count
      name: Total Restarts
    - sensor.jacuzzi_last_seen
      name: Last Seen
    - button.jacuzzi_restart_count_reset
      name: Reset Counter
```

**Shows:**
1. **Total Restarts**: Current count (e.g., "42")
2. **Last Seen**: Seconds since last message (e.g., "5s")
3. **Reset Counter**: Button to reset to 0

**Visual:**
```
🔌 Connection Health

┌─────────────────────────────────────┐
│  Connection Restarts Over Time      │
│                                     │
│  50 ─                               │
│      │                       ╱──────│
│  40 ─│              ╱───────╱       │
│      │      ╱──────╱                │
│  30 ─│  ╱──╱                        │
│      │╱                             │
│   0 ─┴──────────────────────────────│
│     Mon  Tue  Wed  Thu  Fri  Sat    │
└─────────────────────────────────────┘

Total Restarts        42
Last Seen             5s
Reset Counter      [PRESS]
```

## Section Order (After Changes)

1. **Temperature** (thermostat)
2. **Power Usage** (current, daily energy, cost)
3. **Status** (pumps, heater, time/date)
4. **Jets** (Pump 1, Pump 2, **Filter Boost** ← NEW)
5. **Lighting** (mode, brightness)
6. **Charts** (temp, pumps over 1 hour)
7. **Connection Health** ← NEW SECTION
   - Restart chart (7 days)
   - Health metrics
   - Reset button
8. **Spa Settings** (heat mode, filters, time/date)

## How to Apply

1. Go to your Home Assistant dashboard
2. Click the three dots → Edit Dashboard
3. Find the "Hot Tub" view
4. Replace the entire view YAML with the updated version
5. Save

Or copy just the new sections:
- Find the "💨 Jets" section and replace it
- Add the new "🔌 Connection Health" section after Charts

## Usage Examples

### Monitor Connection Stability
- Check the chart daily
- Flat line = healthy
- Climbing = investigate WiFi or ProLink hardware

### After Maintenance
- Fixed WiFi issues? Press "Reset Counter"
- Replaced ProLink? Press "Reset Counter"
- Start tracking from 0 again

### Alert on Problems
The restart count sensor can be used in automations:

```yaml
automation:
  - alias: "High Restart Alert"
    trigger:
      - platform: numeric_state
        entity_id: sensor.jacuzzi_restart_count
        above: 20
    action:
      - service: notify.mobile_app
        data:
          message: "Jacuzzi connection unstable ({{ states('sensor.jacuzzi_restart_count') }} restarts this week)"
```

### Filter Boost Usage
- Before getting in: Press Filter Boost
- Extra filtration before guests arrive
- After heavy use or hot weather
- Pumps run for 20 minutes then auto-stop

## Entity Names to Update

If your entity names differ, update these in the YAML:

**Filter Boost:**
- `button.jacuzzi_filter_boost`

**Connection Health:**
- `sensor.jacuzzi_restart_count`
- `sensor.jacuzzi_last_seen`
- `button.jacuzzi_restart_count_reset`

The exact entity names depend on your mqtt_bridge configuration and how Home Assistant auto-discovered them.

## Chart Customization

**Change time span:**
```yaml
graph_span: 7d   # 7 days (default)
graph_span: 30d  # 30 days (monthly view)
graph_span: 1d   # 1 day (daily view)
```

**Change color:**
```yaml
color: '#ff6b6b'  # Red (default - alerts)
color: '#4dabf7'  # Blue (calm)
color: '#51cf66'  # Green (good)
```

**Adjust height:**
```yaml
chart:
  height: 200  # Default
  height: 300  # Taller
  height: 150  # Shorter
```

## Benefits

**Visual Health Monitoring:**
- Spot trends at a glance
- Historical tracking
- Easy to share with family

**Quick Actions:**
- One-tap filter boost
- One-tap counter reset
- No digging through settings

**Proactive Maintenance:**
- Know when to check WiFi
- Know when to replace hardware
- Track improvement after fixes

Enjoy your enhanced hot tub dashboard! 🎉

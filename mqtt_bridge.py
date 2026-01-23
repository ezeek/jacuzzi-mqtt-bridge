#!/usr/bin/env python3
"""
Pure pyjacuzzi MQTT Bridge
Uses pyjacuzzi EXACTLY like the terminal UI does - nothing more, nothing less.
"""

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime

import paho.mqtt.client as mqtt

# Import pyjacuzzi
sys.path.insert(0, '/app')
import jacuzzi

# Configuration
JACUZZI_IP = os.getenv('JACUZZI_IP', '192.168.1.100')
JACUZZI_PORT = int(os.getenv('JACUZZI_PORT', '4257'))
MQTT_BROKER = os.getenv('MQTT_BROKER', 'localhost')
MQTT_PORT = int(os.getenv('MQTT_PORT', '1883'))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', '')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', '')
MQTT_BASE_TOPIC = os.getenv('MQTT_BASE_TOPIC', 'jacuzzi')
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

# Auto-reconnect configuration
READFAIL_RESTART_ENABLE = os.getenv('READFAIL_RESTART_ENABLE', '1') == '1'
READFAIL_RESTART_COUNT = int(os.getenv('READFAIL_RESTART_COUNT', '2'))
READFAIL_WINDOW_S = int(os.getenv('READFAIL_WINDOW_S', '4'))

# Optional debug/tap ports (plain text + JSON)
ENABLE_TAP_8889 = os.getenv('ENABLE_TAP_8889', '0') == '1'
ENABLE_RXONLY_8890 = os.getenv('ENABLE_RXONLY_8890', '0') == '1'
ENABLE_JSON_8891 = os.getenv('ENABLE_JSON_8891', '0') == '1'
TAP_PORT_8889 = int(os.getenv('TAP_PORT_8889', '8889'))
TAP_PORT_8890 = int(os.getenv('TAP_PORT_8890', '8890'))
TAP_PORT_8891 = int(os.getenv('TAP_PORT_8891', '8891'))

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class TapServer:
    """
    Tiny TCP broadcast server:
      - Accept multiple clients
      - push(line) sends to all clients
    """
    def __init__(self, port: int, name: str):
        self.port = port
        self.name = name
        self._sock = None
        self._clients = set()
        self._lock = threading.Lock()
        self._thread = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"🔌 TapServer {self.name} listening on 0.0.0.0:{self.port}")

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        with self._lock:
            for c in list(self._clients):
                try:
                    c.close()
                except Exception:
                    pass
            self._clients.clear()

    def push(self, line: str):
        if not self._running:
            return
        data = (line.rstrip("\n") + "\n").encode("utf-8", errors="replace")
        dead = []
        with self._lock:
            for c in self._clients:
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                try:
                    c.close()
                except Exception:
                    pass
                self._clients.discard(c)

    def _run(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.listen(8)
        s.settimeout(1.0)
        self._sock = s
        while self._running:
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except Exception:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                self._clients.add(conn)
            try:
                conn.sendall(f"# connected to {self.name} on {self.port}\n".encode())
            except Exception:
                pass


class ReadFailHandler(logging.Handler):
    """Custom log handler to track 'Spa read failed' errors"""
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge
    
    def emit(self, record):
        if "Spa read failed" in record.getMessage():
            self.bridge.on_read_fail()


class PureJacuzziMQTTBridge:
    """
    Pure pyjacuzzi bridge - uses spa object EXACTLY like terminal UI:
    1. Create spa = jacuzzi.JacuzziSpaWifi(host)
    2. Run spa.check_connection_status() 
    3. Run spa.listen()
    4. Read spa attributes when they change
    """
    
    def __init__(self):
        self.spa = None
        self.mqtt = None
        self.running = False
        self.loop = None
        
        # Read fail tracking for auto-restart
        self.read_fail_times = []  # List of timestamps when read fails occurred
        self.restart_count = 0  # Track number of auto-restarts
        self.last_message_time = 0  # Track last time we received a message
        
        # Optional tap servers
        self.tap_all = TapServer(TAP_PORT_8889, "tap8889 RX+TX") if ENABLE_TAP_8889 else None
        self.tap_rx = TapServer(TAP_PORT_8890, "tap8890 RX-only") if ENABLE_RXONLY_8890 else None
        self.tap_json = TapServer(TAP_PORT_8891, "tap8891 JSON") if ENABLE_JSON_8891 else None
        
        # TX capture (best-effort without patching jacuzzi.py)
        self._tx_buf = bytearray()
    
    async def _set_2state_pump(self, pump_num: int, desired_state: int):
        """Set a 2-state pump (OFF=0, ON=1) reliably.

        Why this exists:
        - pyjacuzzi's change_pump() computes how many "button presses" to send
          based on pump_array[pump] (max state) and pump_status[pump] (current).
        - On some Jacuzzi/ProLink setups, a 2-state pump reports ON as "2" even
          though the valid commanded ON state is "1" and pump_array[pump]==1.
          That makes change_pump() compute 0 iterations for ON->OFF, so nothing
          gets sent.

        Fix:
        - Normalize the *current* status into the valid range before calling
          change_pump(). We do this by temporarily overriding pump_status[pump]
          just for the duration of the call.
        """

        # Guard rails.
        if desired_state not in (0, 1):
            raise ValueError(f"desired_state must be 0 or 1, got {desired_state}")

        # If pyjacuzzi hasn't populated arrays yet, fall back to direct call.
        if not hasattr(self.spa, "pump_array") or not hasattr(self.spa, "pump_status"):
            await self.spa.change_pump(pump_num, desired_state)
            return

        try:
            max_state = self.spa.pump_array[pump_num]
            cur_state = self.spa.pump_status[pump_num]
        except Exception:
            await self.spa.change_pump(pump_num, desired_state)
            return

        # Normalize weird reporting for 2-state pumps.
        # If it's a 2-state pump (max_state==1) and it reports ON as 2, treat that as 1.
        normalized_cur = cur_state
        if max_state == 1 and cur_state == 2:
            normalized_cur = 1
        # Also clamp any out-of-range values defensively.
        if normalized_cur > max_state:
            normalized_cur = max_state
        if normalized_cur < 0:
            normalized_cur = 0

        # If after normalization we're already at the desired state, do nothing.
        if normalized_cur == desired_state:
            logger.info(f"Pump {pump_num} already at state {desired_state} (normalized from {cur_state})")
            return

        # Temporarily override so change_pump() computes the correct number of presses.
        original = self.spa.pump_status[pump_num]
        self.spa.pump_status[pump_num] = normalized_cur
        try:
            await self.spa.change_pump(pump_num, desired_state)
        finally:
            # Restore whatever the spa last reported.
            self.spa.pump_status[pump_num] = original
    
    def on_read_fail(self):
        """Called when a 'Spa read failed' error occurs"""
        if not READFAIL_RESTART_ENABLE:
            logger.warning("Read fail detected but monitoring is DISABLED")
            return
        
        if not self.running:
            logger.debug("Read fail during shutdown - ignoring")
            return  # Don't count failures during shutdown
        
        now = time.time()
        self.read_fail_times.append(now)
        
        # Remove old failures outside the window
        cutoff = now - READFAIL_WINDOW_S
        self.read_fail_times = [t for t in self.read_fail_times if t > cutoff]
        
        fail_count = len(self.read_fail_times)
        
        # Always log the count so we can see it's working
        logger.warning(f"⚠️  Read fail #{fail_count}/{READFAIL_RESTART_COUNT} in last {READFAIL_WINDOW_S}s")
        
        # Check if we've hit the threshold
        if fail_count >= READFAIL_RESTART_COUNT:
            logger.error(f"🔴 THRESHOLD REACHED: {fail_count} read fails in {READFAIL_WINDOW_S}s - triggering restart")
            self.restart_count += 1
            self.running = False
        
    def setup_mqtt(self):
        """Setup MQTT connection"""
        logger.info(f"Connecting to MQTT: {MQTT_BROKER}:{MQTT_PORT}")
        
        try:
            self.mqtt = mqtt.Client(
                client_id="jacuzzi-bridge",
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )
        except:
            self.mqtt = mqtt.Client(client_id="jacuzzi-bridge")
        
        if MQTT_USERNAME and MQTT_PASSWORD:
            self.mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        
        self.mqtt.connect(MQTT_BROKER, MQTT_PORT, 60)
        self.mqtt.loop_start()
        
    def on_connect(self, client, userdata, flags, rc, properties=None):
        """MQTT connected"""
        if isinstance(rc, int):
            code = rc
        else:
            code = rc.value if hasattr(rc, 'value') else rc
            
        if code == 0:
            logger.info("MQTT connected")
            result = client.subscribe(f"{MQTT_BASE_TOPIC}/+/set")
            logger.info(f"Subscribed to {MQTT_BASE_TOPIC}/+/set, result: {result}")
            self.send_discovery()
        else:
            logger.error(f"MQTT connection failed: {code}")
    
    def on_message(self, client, userdata, msg):
        """Handle MQTT commands"""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            
            logger.info(f"MQTT RX: topic={topic}, payload={payload}")
            
            # Extract command from topic
            parts = topic.split('/')
            if len(parts) >= 3 and parts[-1] == 'set':
                command = parts[-2]
                logger.info(f"Extracted command: {command}")
                
                # Schedule command on main loop
                if self.loop:
                    asyncio.run_coroutine_threadsafe(
                        self.send_command(command, payload),
                        self.loop
                    )
                else:
                    logger.error("No event loop available!")
            else:
                logger.warning(f"Topic doesn't match expected pattern: {topic}")
        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
    

    async def send_command(self, command, payload):
        """Send command to spa - using pyjacuzzi's methods"""
        if not self.spa or not self.spa.connected:
            logger.warning(f"Spa not connected, ignoring {command}")
            return
        
        try:
            logger.info(f"Processing command: {command} = {payload}")
            
            # Use pyjacuzzi's send methods EXACTLY as terminal UI does
            if command == "target_temperature":
                temp = float(payload)
                logger.info(f"Sending temp change: {temp}")
                await self.spa.send_temp_change(temp)
                
            elif command == "pump_1":
                state_map = {"OFF": 0, "ON": 1}
                if payload.upper() in state_map:
                    state = state_map[payload.upper()]
                    logger.info(f"Sending pump 1 command: {state}")
                    await self._set_2state_pump(1, state)
                else:
                    logger.error(f"Unknown pump_1 payload: {payload}")
                    
            elif command == "pump_2":
                state_map = {"OFF": 0, "ON": 1}
                if payload.upper() in state_map:
                    state = state_map[payload.upper()]
                    logger.info(f"Sending pump 2 command: {state}")
                    await self._set_2state_pump(2, state)
                else:
                    logger.error(f"Unknown pump_2 payload: {payload}")
            
            elif command == "light_mode":
                mode_map = {"Off": 0, "Blue": 2, "Green": 3, "Orange": 5, 
                           "Red": 6, "Violet": 7, "Aqua": 9, "Blend": 0x80}
                if payload in mode_map:
                    mode = mode_map[payload]
                    if mode == 0:
                        await self.spa.change_brightness(0)
                    elif mode == 0x80:
                        await self.spa.change_brightness(0)
                        await asyncio.sleep(0.5)
                        await self.spa.change_brightness(100)
                    else:
                        await self.spa.change_light(mode)
            
            elif command == "light_brightness":
                await self.spa.change_brightness(int(payload))
            
            elif command == "set_time":
                # Format: HH:MM
                logger.info(f"Setting spa time to: {payload}")
                await self.spa.set_time(payload)
            
            elif command == "set_date":
                # Format: YYYY-MM-DD
                logger.info(f"Setting spa date to: {payload}")
                await self.spa.set_date(payload)
            
            elif command == "filter1_start_hour":
                # Filter 1 settings - need to send all 3 values together
                start_hour = int(payload)
                duration = self.spa.filter1DurationHours if hasattr(self.spa, 'filter1DurationHours') else 8
                freq = self.spa.filter1Freq if hasattr(self.spa, 'filter1Freq') else 2
                logger.info(f"Setting filter1 cycle: start={start_hour}, duration={duration}, freq={freq}")
                await self.spa.change_filter1_cycle(start_hour, duration, freq)
            
            elif command == "filter1_duration_hours":
                start_hour = self.spa.filter1StartHour if hasattr(self.spa, 'filter1StartHour') else 0
                duration = int(payload)
                freq = self.spa.filter1Freq if hasattr(self.spa, 'filter1Freq') else 2
                logger.info(f"Setting filter1 cycle: start={start_hour}, duration={duration}, freq={freq}")
                await self.spa.change_filter1_cycle(start_hour, duration, freq)
            
            elif command == "filter1_frequency":
                start_hour = self.spa.filter1StartHour if hasattr(self.spa, 'filter1StartHour') else 0
                duration = self.spa.filter1DurationHours if hasattr(self.spa, 'filter1DurationHours') else 8
                freq = int(payload)
                logger.info(f"Setting filter1 cycle: start={start_hour}, duration={duration}, freq={freq}")
                await self.spa.change_filter1_cycle(start_hour, duration, freq)
            
            elif command == "filter2_mode":
                # Filter 2: Off=0, Auto=1, Manual=2
                mode_map = {"Off": 0, "Auto": 1, "Manual": 2}
                if payload in mode_map:
                    mode = mode_map[payload]
                    logger.info(f"Setting filter2 mode: {payload} ({mode})")
                    await self.spa.change_filter2_cycle(mode)
                else:
                    logger.error(f"Unknown filter2_mode: {payload}")
            
            elif command == "heat_mode":
                # Home Assistant select sends text payloads ("Eco", "Auto", "Day")
                # Internal numeric mapping from spa.get_heatmode(False): 0=Auto, 1=Eco, 2=Day
                mode_map = {"Auto": 0, "Eco": 1, "Day": 2}
                if payload in mode_map:
                    mode = mode_map[payload]
                else:
                    mode = int(payload)

                if mode not in (0, 1, 2):
                    logger.error(f"Unknown heat_mode: {payload}")
                    return

                logger.info(f"Setting heat_mode: {payload} ({mode})")
                # Send exactly what the ProLink app sends:
                # 7e 06 0a bf 1a <mode> <crc> 7e
                await self.spa.send_message(self.spa.channel, 0xBF, 0x1A, mode)

            
                
        except Exception as e:
            logger.error(f"Error sending command: {e}", exc_info=True)
    
    def send_discovery(self):
        """Send Home Assistant discovery messages"""
        logger.info("Sending discovery messages")
        
        base = f"homeassistant"
        device = {
            "identifiers": ["jacuzzi_spa"],
            "name": "Jacuzzi Hot Tub",
            "manufacturer": "Jacuzzi"
        }
        
        # Temperature sensor
        self.mqtt.publish(
            f"{base}/sensor/jacuzzi_temp/config",
            json.dumps({
                "name": "Jacuzzi Temperature",
                "state_topic": f"{MQTT_BASE_TOPIC}/temperature",
                "unit_of_measurement": "°F",
                "device_class": "temperature",
                "unique_id": "jacuzzi_temp",
                "device": device
            }),
            retain=True
        )
        
        # Target temperature
        self.mqtt.publish(
            f"{base}/number/jacuzzi_target/config",
            json.dumps({
                "name": "Jacuzzi Target Temperature",
                "state_topic": f"{MQTT_BASE_TOPIC}/target_temperature",
                "command_topic": f"{MQTT_BASE_TOPIC}/target_temperature/set",
                "min": 50,
                "max": 104,
                "unit_of_measurement": "°F",
                "unique_id": "jacuzzi_target",
                "device": device
            }),
            retain=True
        )
        
        # Pump 1 (switch - both pumps are ON/OFF only)
        self.mqtt.publish(
            f"{base}/switch/jacuzzi_pump1/config",
            json.dumps({
                "name": "Jacuzzi Pump 1",
                "state_topic": f"{MQTT_BASE_TOPIC}/pump_1",
                "command_topic": f"{MQTT_BASE_TOPIC}/pump_1/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "unique_id": "jacuzzi_pump1",
                "device": device
            }),
            retain=True
        )
        
        # Pump 2 (switch)
        self.mqtt.publish(
            f"{base}/switch/jacuzzi_pump2/config",
            json.dumps({
                "name": "Jacuzzi Pump 2",
                "state_topic": f"{MQTT_BASE_TOPIC}/pump_2",
                "command_topic": f"{MQTT_BASE_TOPIC}/pump_2/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "unique_id": "jacuzzi_pump2",
                "device": device
            }),
            retain=True
        )
        
        # Light mode
        self.mqtt.publish(
            f"{base}/select/jacuzzi_light/config",
            json.dumps({
                "name": "Jacuzzi Light Mode",
                "state_topic": f"{MQTT_BASE_TOPIC}/light_mode",
                "command_topic": f"{MQTT_BASE_TOPIC}/light_mode/set",
                "options": ["Off", "Blue", "Green", "Orange", "Red", "Violet", "Aqua", "Blend"],
                "unique_id": "jacuzzi_light",
                "device": device
            }),
            retain=True
        )
        
        # Light brightness
        self.mqtt.publish(
            f"{base}/number/jacuzzi_brightness/config",
            json.dumps({
                "name": "Jacuzzi Light Brightness",
                "state_topic": f"{MQTT_BASE_TOPIC}/light_brightness",
                "command_topic": f"{MQTT_BASE_TOPIC}/light_brightness/set",
                "min": 0,
                "max": 100,
                "unique_id": "jacuzzi_brightness",
                "device": device
            }),
            retain=True
        )
        
        # Heat mode - Auto/Eco/Day (editable select)
        self.mqtt.publish(
            f"{base}/select/jacuzzi_heatmode/config",
            json.dumps({
                "name": "Jacuzzi Heat Mode",
                "state_topic": f"{MQTT_BASE_TOPIC}/heat_mode",
                "command_topic": f"{MQTT_BASE_TOPIC}/heat_mode/set",
                "options": ["Eco", "Auto", "Day"],
                "icon": "mdi:heat-wave",
                "unique_id": "jacuzzi_heatmode",
                "device": device
            }),
            retain=True
        )

# Heat state - Idle/Heating/Heat Waiting (read-only sensor)
        self.mqtt.publish(
            f"{base}/sensor/jacuzzi_heatstate/config",
            json.dumps({
                "name": "Jacuzzi Heat State",
                "state_topic": f"{MQTT_BASE_TOPIC}/heat_state",
                "icon": "mdi:radiator",
                "unique_id": "jacuzzi_heatstate",
                "device": device
            }),
            retain=True
        )
        
        # Heater On (binary sensor - read only)
        self.mqtt.publish(
            f"{base}/binary_sensor/jacuzzi_heater/config",
            json.dumps({
                "name": "Jacuzzi Heater On",
                "state_topic": f"{MQTT_BASE_TOPIC}/heater_on",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "heat",
                "unique_id": "jacuzzi_heater",
                "icon": "mdi:fire",
                "device": device
            }),
            retain=True
        )
        
        # Circulation Pump (binary sensor - read only)
        self.mqtt.publish(
            f"{base}/binary_sensor/jacuzzi_circ_pump/config",
            json.dumps({
                "name": "Jacuzzi Circulation Pump",
                "state_topic": f"{MQTT_BASE_TOPIC}/circulation_pump",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "running",
                "unique_id": "jacuzzi_circ_pump",
                "icon": "mdi:pump",
                "device": device
            }),
            retain=True
        )
        
        # UV Bulb (binary sensor - read only)
        self.mqtt.publish(
            f"{base}/binary_sensor/jacuzzi_uv/config",
            json.dumps({
                "name": "Jacuzzi UV Lamp",
                "state_topic": f"{MQTT_BASE_TOPIC}/uv_lamp",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": "running",
                "unique_id": "jacuzzi_uv",
                "icon": "mdi:lightbulb-cfl",
                "device": device
            }),
            retain=True
        )
        
        # Spa Time (sensor - read only)
        self.mqtt.publish(
            f"{base}/sensor/jacuzzi_spa_time/config",
            json.dumps({
                "name": "Jacuzzi Spa Time",
                "state_topic": f"{MQTT_BASE_TOPIC}/spa_time",
                "unique_id": "jacuzzi_spa_time",
                "icon": "mdi:clock-outline",
                "device": device
            }),
            retain=True
        )
        
        # Spa Date (sensor - read only)
        self.mqtt.publish(
            f"{base}/sensor/jacuzzi_spa_date/config",
            json.dumps({
                "name": "Jacuzzi Spa Date",
                "state_topic": f"{MQTT_BASE_TOPIC}/spa_date",
                "unique_id": "jacuzzi_spa_date",
                "icon": "mdi:calendar",
                "device": device
            }),
            retain=True
        )
        
        # Set Spa Time (text input)
        self.mqtt.publish(
            f"{base}/text/jacuzzi_set_time/config",
            json.dumps({
                "name": "Jacuzzi Set Spa Time (HH:MM)",
                "state_topic": f"{MQTT_BASE_TOPIC}/spa_time",
                "command_topic": f"{MQTT_BASE_TOPIC}/set_time/set",
                "unique_id": "jacuzzi_set_time",
                "icon": "mdi:clock-edit-outline",
                "pattern": "^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$",
                "device": device
            }),
            retain=True
        )
        
        # Set Spa Date (text input)
        self.mqtt.publish(
            f"{base}/text/jacuzzi_set_date/config",
            json.dumps({
                "name": "Jacuzzi Set Spa Date (YYYY-MM-DD)",
                "state_topic": f"{MQTT_BASE_TOPIC}/spa_date",
                "command_topic": f"{MQTT_BASE_TOPIC}/set_date/set",
                "unique_id": "jacuzzi_set_date",
                "icon": "mdi:calendar-edit",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                "device": device
            }),
            retain=True
        )
        
        # Filter Cycle 1 - Start Hour
        self.mqtt.publish(
            f"{base}/number/jacuzzi_filter1_start/config",
            json.dumps({
                "name": "Jacuzzi Filter Cycle 1 Start Hour",
                "state_topic": f"{MQTT_BASE_TOPIC}/filter1_start_hour",
                "command_topic": f"{MQTT_BASE_TOPIC}/filter1_start_hour/set",
                "min": 0,
                "max": 23,
                "step": 1,
                "mode": "box",
                "unique_id": "jacuzzi_filter1_start",
                "icon": "mdi:clock-start",
                "device": device
            }),
            retain=True
        )
        
        # Filter Cycle 1 - Duration
        self.mqtt.publish(
            f"{base}/number/jacuzzi_filter1_duration/config",
            json.dumps({
                "name": "Jacuzzi Filter Cycle 1 Duration (hrs)",
                "state_topic": f"{MQTT_BASE_TOPIC}/filter1_duration_hours",
                "command_topic": f"{MQTT_BASE_TOPIC}/filter1_duration_hours/set",
                "min": 0,
                "max": 24,
                "step": 1,
                "mode": "box",
                "unique_id": "jacuzzi_filter1_duration",
                "icon": "mdi:timer-outline",
                "device": device
            }),
            retain=True
        )
        
        # Filter Cycle 1 - Frequency
        self.mqtt.publish(
            f"{base}/number/jacuzzi_filter1_freq/config",
            json.dumps({
                "name": "Jacuzzi Filter Cycle 1 Frequency (per day)",
                "state_topic": f"{MQTT_BASE_TOPIC}/filter1_frequency",
                "command_topic": f"{MQTT_BASE_TOPIC}/filter1_frequency/set",
                "min": 1,
                "max": 8,
                "step": 1,
                "mode": "box",
                "unique_id": "jacuzzi_filter1_freq",
                "icon": "mdi:repeat",
                "device": device
            }),
            retain=True
        )
        
        # Filter Cycle 2 Mode
        self.mqtt.publish(
            f"{base}/select/jacuzzi_filter2_mode/config",
            json.dumps({
                "name": "Jacuzzi Cycle 2 Mode",
                "state_topic": f"{MQTT_BASE_TOPIC}/filter2_mode",
                "command_topic": f"{MQTT_BASE_TOPIC}/filter2_mode/set",
                "options": ["Off", "Auto", "Manual"],
                "unique_id": "jacuzzi_filter2_mode",
                "icon": "mdi:filter-variant",
                "device": device
            }),
            retain=True
        )
        
        logger.info("Discovery complete")
    
    def publish_state(self):
        """Publish current spa state - reading attributes like terminal UI does"""
        if not self.spa:
            logger.debug("No spa object")
            return
        
        try:
            # Debug: Check spa state
            logger.info(f"Spa state: connected={self.spa.connected}, "
                       f"config_loaded={self.spa.config_loaded}, "
                       f"lastupd={self.spa.lastupd}, "
                       f"curtemp={self.spa.curtemp}, "
                       f"settemp={self.spa.settemp if hasattr(self.spa, 'settemp') else 'N/A'}")
            
            logger.info(f"curtemp type: {type(self.spa.curtemp)}, value: {self.spa.curtemp}, > 0: {self.spa.curtemp > 0 if self.spa.curtemp else 'N/A'}")
            
            # Read attributes EXACTLY like terminal UI does
            
            # Current temp
            if hasattr(self.spa, 'curtemp') and self.spa.curtemp is not None and self.spa.curtemp > 0:
                logger.info(f"Publishing temperature: {self.spa.curtemp}")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/temperature", str(self.spa.curtemp))
            
            # Target temp - using get_settemp() like terminal UI
            settemp = self.spa.get_settemp()
            if settemp is not None:
                logger.info(f"Publishing target_temperature: {settemp}")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/target_temperature", str(settemp))
            
            # Pump 1 - using get_pump() like terminal UI
            # Pump reports state 2 when ON, but we normalize to ON/OFF
            pump1 = self.spa.get_pump(1, False)  # False = return number not text
            pump1_text = "ON" if pump1 > 0 else "OFF"  # State 1 or 2 = ON
            logger.info(f"Publishing pump_1: {pump1_text}")
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/pump_1", pump1_text)
            
            # Pump 2
            pump2 = self.spa.get_pump(2, False)
            pump2_text = "ON" if pump2 > 0 else "OFF"
            logger.info(f"Publishing pump_2: {pump2_text}")
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/pump_2", pump2_text)
            
            # Light mode - using get_lightMode() like terminal UI
            light_mode_val = self.spa.get_lightMode()
            mode_names = {0: "Off", 2: "Blue", 3: "Green", 5: "Orange",
                         6: "Red", 7: "Violet", 9: "Aqua", 0x80: "Blend"}
            light_mode = mode_names.get(light_mode_val, "Off")
            logger.info(f"Publishing light_mode: {light_mode}")
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/light_mode", light_mode)
            
            # Light brightness - using get_lightBrightness() like terminal UI
            brightness = self.spa.get_lightBrightness()
            logger.info(f"Publishing light_brightness: {brightness}")
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/light_brightness", str(brightness))
            
            # Heat mode - show text labels (Auto/Eco/Day)
            heatmode = self.spa.get_heatmode(False)  # 0=Auto, 1=Eco, 2=Day
            heatmode_names = {0: "Auto", 1: "Eco", 2: "Day"}
            heatmode_text = heatmode_names.get(heatmode, f"Unknown ({heatmode})")
            logger.info(f"Publishing heat_mode: {heatmode_text} (value={heatmode})")
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/heat_mode", heatmode_text)
            
            # Heat state / heater_on - published in real-time from debug_process_message
            # using bit 1 of byte 14 (electrically verified correct method)
            # No longer publishing here to avoid overwriting with pyjacuzzi's potentially incorrect values
            
            # Circulation pump - direct attribute access
            # circ_pump_status: 1=ON, 0=OFF
            if hasattr(self.spa, 'circ_pump_status'):
                circ_pump_text = "ON" if self.spa.circ_pump_status > 0 else "OFF"
                logger.info(f"Publishing circulation_pump: {circ_pump_text} (value={self.spa.circ_pump_status})")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/circulation_pump", circ_pump_text)
            
            # UV lamp - direct attribute access  
            # isUVOn: >0=ON, 0=OFF
            if hasattr(self.spa, 'isUVOn'):
                uv_text = "ON" if self.spa.isUVOn > 0 else "OFF"
                logger.info(f"Publishing uv_lamp: {uv_text} (value={self.spa.isUVOn})")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/uv_lamp", uv_text)
            
            # Spa Time - use time_hour and time_minute attributes
            if hasattr(self.spa, 'time_hour') and hasattr(self.spa, 'time_minute'):
                hour = self.spa.time_hour
                minute = self.spa.time_minute
                spa_time = f"{hour:02d}:{minute:02d}"
                logger.info(f"Publishing spa_time: {spa_time}")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/spa_time", spa_time)
            
            # Spa Date - convert from M/D/YYYY to YYYY-MM-DD
            if hasattr(self.spa, 'get_month') and hasattr(self.spa, 'get_day') and hasattr(self.spa, 'get_year'):
                month = self.spa.get_month()
                day = self.spa.get_day()
                year = self.spa.get_year()
                # Format as YYYY-MM-DD for standard date format
                spa_date = f"{year:04d}-{month:02d}-{day:02d}"
                logger.info(f"Publishing spa_date: {spa_date} (from {month}/{day}/{year})")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/spa_date", spa_date)
            
            # Filter Cycle 1 settings
            if hasattr(self.spa, 'filter1StartHour'):
                logger.info(f"Publishing filter1_start_hour: {self.spa.filter1StartHour}")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/filter1_start_hour", str(self.spa.filter1StartHour))
            
            if hasattr(self.spa, 'filter1DurationHours'):
                logger.info(f"Publishing filter1_duration_hours: {self.spa.filter1DurationHours}")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/filter1_duration_hours", str(self.spa.filter1DurationHours))
            
            if hasattr(self.spa, 'filter1Freq'):
                logger.info(f"Publishing filter1_frequency: {self.spa.filter1Freq}")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/filter1_frequency", str(self.spa.filter1Freq))
            
            # Filter Cycle 2 mode
            if hasattr(self.spa, 'filter2Mode'):
                filter2_names = {0: "Off", 1: "Auto", 2: "Manual"}
                filter2_text = filter2_names.get(self.spa.filter2Mode, "Off")
                logger.info(f"Publishing filter2_mode: {filter2_text} (value={self.spa.filter2Mode})")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/filter2_mode", filter2_text)
            
            logger.debug(f"State: {self.spa.curtemp}°F, Pump1: {pump1_text}, Light: {light_mode}, Heat: {heatmode}")
            
        except Exception as e:
            logger.error(f"Error publishing state: {e}", exc_info=True)
    
    def _tap_emit(self, direction: str, hex_str: str):
        """
        direction: "RX" or "TX"
        hex_str: lowercase hex of a single 7e..7e frame (no spaces)
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{ts}] {direction}: {hex_str}"
        if self.tap_all:
            self.tap_all.push(line)
        if direction == "RX" and self.tap_rx:
            self.tap_rx.push(line)
        if self.tap_json:
            obj = {"ts": ts, "dir": direction, "hex": hex_str}
            self.tap_json.push(json.dumps(obj, separators=(",", ":")))
    
    def _split_frames_7e(self, blob: bytes):
        """Split a byte stream into 7e..7e framed packets, returning full frames."""
        self._tx_buf.extend(blob)
        frames = []
        while True:
            try:
                start = self._tx_buf.index(0x7E)
            except ValueError:
                self._tx_buf.clear()
                break
            if start > 0:
                del self._tx_buf[:start]
            try:
                end = self._tx_buf.index(0x7E, 1)
            except ValueError:
                break
            frame = bytes(self._tx_buf[:end+1])
            del self._tx_buf[:end+1]
            frames.append(frame)
        return frames
    
    def _try_hook_writer_for_tx(self):
        """Best-effort TX capture by wrapping spa.writer.write() once it exists."""
        try:
            w = getattr(self.spa, "writer", None)
            if not w:
                return False
            if getattr(w, "_tap_wrapped", False):
                return True
            orig_write = w.write
            bridge = self

            def write_wrapper(data):
                try:
                    for frame in bridge._split_frames_7e(data):
                        bridge._tap_emit("TX", frame.hex())
                except Exception:
                    pass
                return orig_write(data)

            w.write = write_wrapper
            w._tap_wrapped = True
            logger.info("✅ Hooked spa.writer.write() for TX tap (best-effort)")
            return True
        except Exception:
            return False
    
    async def monitor_changes(self):
        """
        Monitor spa.lastupd for changes - EXACTLY like terminal UI does.
        Terminal UI checks at 10 Hz (every 100ms) and updates display when lastupd changes.
        
        Also publish periodically to show current state even if not changing.
        
        AUTO-RECONNECT: If ProLink stops responding (no updates for 60s), restart connection.
        """
        logger.info("Starting state monitor (10 Hz) with auto-reconnect")
        
        last_lastupd = 0
        last_periodic_publish = 0
        check_count = 0
        last_successful_update = time.time()
        
        while self.running:
            try:
                check_count += 1
                
                # Every 100 checks (10 seconds), log that we're alive
                if check_count % 100 == 0:
                    logger.info(f"Monitor alive: checks={check_count}, lastupd={self.spa.lastupd if self.spa else 'no spa'}")
                
                # Check if spa has new data (lastupd changed)
                if self.spa and self.spa.lastupd > 0:
                    # Reset timer whenever lastupd > 0 (meaning we've received messages)
                    # This prevents false timeouts when messages arrive but lastupd doesn't change
                    if self.spa.lastupd != last_lastupd:
                        logger.info(f"Spa updated: {self.spa.lastupd}")
                        self.publish_state()
                        last_lastupd = self.spa.lastupd
                    last_successful_update = time.time()  # Reset timer on any valid lastupd
                
                # Check if running flag was cleared (by read fail handler or timeout)
                if not self.running:
                    logger.error("Restart triggered - shutting down monitor")
                    return True  # Signal restart needed
                
                # Fallback timeout check (only if read-fail monitoring is disabled or as backup)
                # Increased to 30s to avoid false positives during normal restarts
                time_since_update = time.time() - last_successful_update
                if time_since_update > 30 and self.spa and self.spa.connected and last_lastupd > 0:
                    logger.error(f"ProLink not responding for {time_since_update:.0f}s (fallback timeout) - triggering reconnect")
                    self.restart_count += 1
                    self.running = False
                    return True
                
                # Also publish every 10 seconds regardless, so we can see what's happening
                now = time.time()
                if now - last_periodic_publish >= 10:
                    logger.info("Periodic state check...")
                    self.publish_state()
                    
                    # Publish last_seen (seconds since last message)
                    if self.last_message_time > 0:
                        seconds_since = int(now - self.last_message_time)
                        self.mqtt.publish(f"{MQTT_BASE_TOPIC}/last_seen", seconds_since, retain=True)
                    
                    # Publish restart_count
                    self.mqtt.publish(f"{MQTT_BASE_TOPIC}/restart_count", self.restart_count, retain=True)
                    
                    last_periodic_publish = now
                
                # Check at 10 Hz like terminal UI
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Monitor error: {e}", exc_info=True)
    
    async def run(self):
        """
        Main run - setup EXACTLY like terminal UI:
        1. Create spa object
        2. Add spa.check_connection_status() coroutine
        3. Add spa.listen() coroutine
        4. That's it!
        """
        self.running = True
        self.loop = asyncio.get_event_loop()
        restart_requested = False
        
        try:
            # Start tap servers early
            if self.tap_all:
                self.tap_all.start()
            if self.tap_rx:
                self.tap_rx.start()
            if self.tap_json:
                self.tap_json.start()
            
            # Setup MQTT
            self.setup_mqtt()
            
            # Create spa object - EXACTLY like terminal UI line 973
            logger.info(f"Creating spa object: {JACUZZI_IP}:{JACUZZI_PORT}")
            self.spa = jacuzzi.JacuzziSpaWifi(JACUZZI_IP, JACUZZI_PORT)
            
            # Attach custom log handler to track read failures
            if READFAIL_RESTART_ENABLE:
                read_fail_handler = ReadFailHandler(self)
                balboa_logger = logging.getLogger('balboa')
                balboa_logger.addHandler(read_fail_handler)
                logger.info(f"Read fail auto-restart: {READFAIL_RESTART_COUNT} fails in {READFAIL_WINDOW_S}s window")
            
            # Hook into spa's message processing to log everything
            original_process_message = self.spa.process_message
            
            def debug_process_message(data):
                """Wrapper to log all messages being processed"""
                # Update last message timestamp
                self.last_message_time = time.time()
                
                # Try hook writer after we know we're getting traffic
                self._try_hook_writer_for_tx()
                
                if data and len(data) > 4:
                    hex_str = data.hex()
                    msg_type_byte = data[4]
                    
                    # Decode message type
                    msg_types = {
                        0x16: "PLNK_STATUS_UPDATE (Main status - temp, pumps, etc)",
                        0x27: "PLNK_FILTER_INFO_RESP (Filter cycle info)",
                        0x19: "PLNK_PANEL_REQ (Panel request)",
                        0x1C: "PLNK_SECONDARY_FILTER_RESP (Secondary filter)",
                        0x1B: "PLNK_PRIMARY_FILTER_RESP (Primary filter)",
                        0x1D: "PLNK_PUMP_STATE_RESP (Pump state)",
                        0x1E: "PLNK_SETUP_PARAMS_RESP (Setup parameters)",
                        0x23: "PLNK_LIGHTS_UPDATE (Light status)",
                        0x94: "MODULE_IDENTIFICATION (Spa model info)",
                        0x2E: "DEVICE_CONFIG (Device configuration)",
                        0x24: "SYSTEM_INFO (System information)",
                        0x25: "SETUP_PARAMS (Setup parameters)",
                        0xC4: "PLNK_C4_STATUS_UPDATE (Encrypted status)",
                        0xCA: "PLNK_CA_LIGHTS_UPDATE (Encrypted lights)",
                    }
                    
                    msg_name = msg_types.get(msg_type_byte, f"UNKNOWN_0x{msg_type_byte:02X}")
                    
                    # Log the hex and decoded type
                    logger.info(f"📨 RX: {hex_str}")
                    logger.info(f"   └─> {msg_name}")
                    
                    # Tap RX frame out (single 7e..7e frame)
                    self._tap_emit("RX", hex_str)
                    
                    # If it's a status update, decode some key fields
                    if msg_type_byte == 0x16 and len(data) >= 26:
                        # For Jacuzzi spas (ProLink protocol):
                        # Byte 12 = current temp (not byte 7 like Balboa)
                        # Byte 14 = set temp (not byte 25 like Balboa)
                        temp_raw = data[12]
                        settemp_raw = data[14]
                        temp = temp_raw if temp_raw != 255 else None
                        settemp = settemp_raw
                        
                        # Also show pump states (byte 15)
                        pump_byte = data[15]
                        pump1 = (pump_byte & 0x0C) >> 2  # Bits 3,2
                        pump2 = (pump_byte & 0x30) >> 4  # Bits 5,4
                        
                        # HEAT MODE INVESTIGATION - Byte 10 detailed breakdown
                        byte10 = data[10]
                        heatmode = (byte10 >> 4) & 0x03  # Bits 5,4
                        heatstate = byte10 & 0x03  # Bits 1,0
                        
                        logger.info(f"       Temp: {temp}°F, SetTemp: {settemp}°F, Pump1: {pump1}, Pump2: {pump2}")
                        logger.info(f"       🔥 BYTE 10: 0x{byte10:02X} = 0b{byte10:08b}")
                        logger.info(f"          HeatMode (bits 5,4): {heatmode} | HeatState (bits 1,0): {heatstate}")
                        
                        # HEATER STATUS - Check bit 0 of byte 16 (electrical verification)
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
                
                # Call original method
                return original_process_message(data)
            
            self.spa.process_message = debug_process_message
            
            # Run spa coroutines - EXACTLY like terminal UI lines 985-986
            logger.info("Starting spa coroutines")
            connection_task = asyncio.create_task(self.spa.check_connection_status())
            listen_task = asyncio.create_task(self.spa.listen())
            monitor_task = asyncio.create_task(self.monitor_changes())
            
            logger.info("Bridge running - pure pyjacuzzi mode")
            
            # Wait for monitor to complete (either manual stop or timeout)
            restart_requested = await monitor_task
                
        except KeyboardInterrupt:
            logger.info("Shutting down (Ctrl+C)")
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
        finally:
            self.running = False
            logger.info("Cleaning up...")
            
            # Disconnect from spa
            if self.spa:
                try:
                    logger.info("Disconnecting from spa...")
                    await self.spa.disconnect()
                    await asyncio.sleep(1)  # Give it time to close socket
                    logger.info("Spa disconnected")
                except Exception as e:
                    logger.error(f"Error disconnecting spa: {e}")
            
            # Disconnect from MQTT
            if self.mqtt:
                self.mqtt.loop_stop()
                self.mqtt.disconnect()
            
            # Stop tap servers
            try:
                if self.tap_all:
                    self.tap_all.stop()
                if self.tap_rx:
                    self.tap_rx.stop()
                if self.tap_json:
                    self.tap_json.stop()
            except Exception:
                pass
            
            logger.info("Cleanup complete")
            
            # Exit with code 1 ONLY if auto-reconnect was triggered
            if restart_requested:
                logger.info("Triggering Docker restart...")
                import sys
                sys.exit(1)
    
    def signal_handler(self, sig, frame):
        logger.info("Signal received, stopping")
        self.running = False


async def main():
    logger.info("=" * 60)
    logger.info("Pure pyjacuzzi MQTT Bridge")
    logger.info(f"Spa: {JACUZZI_IP}:{JACUZZI_PORT}")
    logger.info(f"MQTT: {MQTT_BROKER}:{MQTT_PORT}")
    logger.info("Using pyjacuzzi EXACTLY like terminal UI")
    logger.info("=" * 60)
    
    bridge = PureJacuzziMQTTBridge()
    
    signal.signal(signal.SIGINT, bridge.signal_handler)
    signal.signal(signal.SIGTERM, bridge.signal_handler)
    
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())

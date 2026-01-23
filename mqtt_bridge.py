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
import sys
import time

import socket
import threading
from datetime import datetime


import paho.mqtt.client as mqtt

# Import pyjacuzzi
sys.path.insert(0, '/app')
import jacuzzi

# Configuration
JACUZZI_IP = os.getenv('JACUZZI_IP', '192.168.1.100')
JACUZZI_PORT = int(os.getenv('JACUZZI_PORT', 4257))
MQTT_BROKER = os.getenv('MQTT_BROKER', 'localhost')
MQTT_PORT = int(os.getenv('MQTT_PORT', 1883))
MQTT_USERNAME = os.getenv('MQTT_USERNAME', '')
MQTT_PASSWORD = os.getenv('MQTT_PASSWORD', '')
MQTT_CLIENT_ID = os.getenv('MQTT_CLIENT_ID', 'jacuzzi-mqtt-bridge')
MQTT_DISCOVERY_PREFIX = os.getenv('MQTT_DISCOVERY_PREFIX', 'homeassistant')
MQTT_BASE_TOPIC = os.getenv('MQTT_BASE_TOPIC', 'jacuzzi')

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

# Auto-restart controls
READFAIL_RESTART_ENABLE = os.getenv('READFAIL_RESTART_ENABLE', '1') == '1'
READFAIL_RESTART_COUNT = int(os.getenv('READFAIL_RESTART_COUNT', 3))
READFAIL_WINDOW_S = int(os.getenv('READFAIL_WINDOW_S', 5))

# Optional debug/tap ports (plain text + JSON)
ENABLE_TAP_8889 = os.getenv('ENABLE_TAP_8889', '0') == '1'
ENABLE_RXONLY_8890 = os.getenv('ENABLE_RXONLY_8890', '0') == '1'
ENABLE_JSON_8891 = os.getenv('ENABLE_JSON_8891', '0') == '1'
TAP_PORT_8889 = int(os.getenv('TAP_PORT_8889', 8889))
TAP_PORT_8890 = int(os.getenv('TAP_PORT_8890', 8890))
TAP_PORT_8891 = int(os.getenv('TAP_PORT_8891', 8891))

# Logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
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



def mqtt_connect():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, MQTT_CLIENT_ID)
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.connect(MQTT_BROKER, MQTT_PORT)
    client.loop_start()
    return client


def publish_discovery(client):
    """Publish Home Assistant MQTT discovery for basic entities."""
    dev = {
        "identifiers": ["jacuzzi_spa"],
        "name": "Jacuzzi Hot Tub",
        "manufacturer": "Jacuzzi",
    }

    def pub(topic, payload):
        client.publish(topic, json.dumps(payload), retain=True)

    # Temperature sensor
    pub(f"{MQTT_DISCOVERY_PREFIX}/sensor/jacuzzi_temp/config", {
        "name": "Jacuzzi Temperature",
        "state_topic": f"{MQTT_BASE_TOPIC}/temperature",
        "unit_of_measurement": "°F",
        "device_class": "temperature",
        "unique_id": "jacuzzi_temp",
        "device": dev,
    })

    # Target temperature number
    pub(f"{MQTT_DISCOVERY_PREFIX}/number/jacuzzi_target/config", {
        "name": "Jacuzzi Target Temperature",
        "state_topic": f"{MQTT_BASE_TOPIC}/target_temperature",
        "command_topic": f"{MQTT_BASE_TOPIC}/target_temperature/set",
        "min": 50,
        "max": 104,
        "step": 1,
        "unit_of_measurement": "°F",
        "unique_id": "jacuzzi_target",
        "device": dev,
    })

    # Pump 1 switch
    pub(f"{MQTT_DISCOVERY_PREFIX}/switch/jacuzzi_pump_1/config", {
        "name": "Jacuzzi Pump 1",
        "state_topic": f"{MQTT_BASE_TOPIC}/pump_1",
        "command_topic": f"{MQTT_BASE_TOPIC}/pump_1/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "unique_id": "jacuzzi_pump_1",
        "device": dev,
    })

    # Pump 2 switch
    pub(f"{MQTT_DISCOVERY_PREFIX}/switch/jacuzzi_pump_2/config", {
        "name": "Jacuzzi Pump 2",
        "state_topic": f"{MQTT_BASE_TOPIC}/pump_2",
        "command_topic": f"{MQTT_BASE_TOPIC}/pump_2/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "unique_id": "jacuzzi_pump_2",
        "device": dev,
    })

    # Light mode select
    pub(f"{MQTT_DISCOVERY_PREFIX}/select/jacuzzi_light_mode/config", {
        "name": "Jacuzzi Light Mode",
        "state_topic": f"{MQTT_BASE_TOPIC}/light_mode",
        "command_topic": f"{MQTT_BASE_TOPIC}/light_mode/set",
        "options": ["Off", "Blue", "Green", "Orange", "Red", "Violet", "Aqua", "Blend"],
        "unique_id": "jacuzzi_light_mode",
        "device": dev,
    })

    # Light brightness number
    pub(f"{MQTT_DISCOVERY_PREFIX}/number/jacuzzi_light_brightness/config", {
        "name": "Jacuzzi Light Brightness",
        "state_topic": f"{MQTT_BASE_TOPIC}/light_brightness",
        "command_topic": f"{MQTT_BASE_TOPIC}/light_brightness/set",
        "min": 0,
        "max": 100,
        "step": 1,
        "unique_id": "jacuzzi_light_brightness",
        "device": dev,
    })

    # Heat mode select
    pub(f"{MQTT_DISCOVERY_PREFIX}/select/jacuzzi_heat_mode/config", {
        "name": "Jacuzzi Heat Mode",
        "state_topic": f"{MQTT_BASE_TOPIC}/heat_mode",
        "command_topic": f"{MQTT_BASE_TOPIC}/heat_mode/set",
        "options": ["Eco", "Auto", "Day"],
        "unique_id": "jacuzzi_heat_mode",
        "device": dev,
        "icon": "mdi:heat-wave",
    })

    # Heat state sensor
    pub(f"{MQTT_DISCOVERY_PREFIX}/sensor/jacuzzi_heat_state/config", {
        "name": "Jacuzzi Heat State",
        "state_topic": f"{MQTT_BASE_TOPIC}/heat_state",
        "unique_id": "jacuzzi_heat_state",
        "device": dev,
        "icon": "mdi:radiator",
    })

    # Heater on binary sensor
    pub(f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/jacuzzi_heater_on/config", {
        "name": "Jacuzzi Heater On",
        "state_topic": f"{MQTT_BASE_TOPIC}/heater_on",
        "payload_on": "ON",
        "payload_off": "OFF",
        "unique_id": "jacuzzi_heater_on",
        "device": dev,
        "device_class": "heat",
        "icon": "mdi:fire",
    })

    # Circulation pump binary sensor
    pub(f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/jacuzzi_circ_pump/config", {
        "name": "Jacuzzi Circulation Pump",
        "state_topic": f"{MQTT_BASE_TOPIC}/circulation_pump",
        "payload_on": "ON",
        "payload_off": "OFF",
        "unique_id": "jacuzzi_circ_pump",
        "device": dev,
        "device_class": "running",
        "icon": "mdi:pump",
    })

    # UV lamp binary sensor
    pub(f"{MQTT_DISCOVERY_PREFIX}/binary_sensor/jacuzzi_uv_lamp/config", {
        "name": "Jacuzzi UV Lamp",
        "state_topic": f"{MQTT_BASE_TOPIC}/uv_lamp",
        "payload_on": "ON",
        "payload_off": "OFF",
        "unique_id": "jacuzzi_uv_lamp",
        "device": dev,
        "device_class": "running",
        "icon": "mdi:lightbulb-cfl",
    })


class PureJacuzziMQTTBridge:
    def __init__(self):
        self.spa = None
        self.mqtt = None
        self.running = False
        self.loop = None

        # Read fail tracking for auto-restart
        self.read_fail_times = []  # List of timestamps when read fails occurred
        self.restart_count = 0  # Track number of auto-restarts
        self.last_message_time = 0  # Track last time we received a message

        # Raw-frame-derived heater state (from 7e25 status frames indices 0x14 and 0x22)
        # Cached so MQTT reflects what the wire says even if pyjacuzzi mapping differs.
        self._last_heater_on = None   # "ON" / "OFF"
        self._last_heat_state = None  # "Heating" / "Idle" (for now)

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
          though it only has OFF/ON. That makes change_pump() misbehave.
        """
        if desired_state not in (0, 1):
            raise ValueError("desired_state must be 0 or 1 for 2-state pump")

        # Read current, normalize (>0 is ON)
        cur = self.spa.get_pump(pump_num, False)
        cur_norm = 1 if (cur and cur > 0) else 0
        if cur_norm == desired_state:
            return

        # Force internal status to 0/1 so change_pump uses correct arithmetic
        try:
            if hasattr(self.spa, "pump_status") and isinstance(self.spa.pump_status, (list, tuple)) and len(self.spa.pump_status) > pump_num:
                self.spa.pump_status[pump_num] = cur_norm
        except Exception:
            pass

        # Force max states for 2-state pump to 1
        try:
            if hasattr(self.spa, "pump_array") and isinstance(self.spa.pump_array, (list, tuple)) and len(self.spa.pump_array) > pump_num:
                self.spa.pump_array[pump_num] = 1
        except Exception:
            pass

        # Now call change_pump and let pyjacuzzi send the right button-press sequence
        # change_pump expects 0=OFF, 1=ON for 2-state
        await self.spa.change_pump(pump_num, desired_state)

    def on_mqtt_message(self, client, userdata, message):
        """Handle MQTT commands"""
        topic = message.topic
        payload = message.payload.decode().strip()
        logger.info(f"📩 MQTT CMD {topic} = {payload}")

        try:
            # Set target temp
            if topic == f"{MQTT_BASE_TOPIC}/target_temperature/set":
                temp = float(payload)
                # pyjacuzzi expects integer-ish settemp
                asyncio.run_coroutine_threadsafe(self.spa.send_temp_change(temp), self.loop)

            # Pump 1
            elif topic == f"{MQTT_BASE_TOPIC}/pump_1/set":
                desired = 1 if payload.upper() == "ON" else 0
                asyncio.run_coroutine_threadsafe(self._set_2state_pump(1, desired), self.loop)

            # Pump 2
            elif topic == f"{MQTT_BASE_TOPIC}/pump_2/set":
                desired = 1 if payload.upper() == "ON" else 0
                asyncio.run_coroutine_threadsafe(self._set_2state_pump(2, desired), self.loop)

            # Light mode
            elif topic == f"{MQTT_BASE_TOPIC}/light_mode/set":
                # Reverse map based on names in publish_state()
                name_to_val = {
                    "Off": 0,
                    "Blue": 2,
                    "Green": 3,
                    "Orange": 5,
                    "Red": 6,
                    "Violet": 7,
                    "Aqua": 9,
                    "Blend": 0x80,
                }
                val = name_to_val.get(payload, 0)
                asyncio.run_coroutine_threadsafe(self.spa.change_light(val), self.loop)

            # Light brightness
            elif topic == f"{MQTT_BASE_TOPIC}/light_brightness/set":
                b = int(float(payload))
                asyncio.run_coroutine_threadsafe(self.spa.change_brightness(b), self.loop)

            # Heat mode (Eco/Auto/Day)
            elif topic == f"{MQTT_BASE_TOPIC}/heat_mode/set":
                # Your published options are Eco, Auto, Day (text-based)
                mapping = {"Auto": 0, "Eco": 1, "Day": 2}
                if payload not in mapping:
                    logger.warning(f"Unknown heat_mode payload: {payload}")
                    return
                mode_val = mapping[payload]
                asyncio.run_coroutine_threadsafe(self.spa.send_heatmode_change(mode_val), self.loop)

        except Exception as e:
            logger.error(f"MQTT cmd error: {e}", exc_info=True)

    def publish_state(self):
        """Publish current spa state - reading attributes like terminal UI does"""
        if not self.spa:
            logger.debug("No spa object")
            return

        try:
            # Debug: Check spa state
            logger.info(
                f"Spa state: connected={self.spa.connected}, "
                f"config_loaded={self.spa.config_loaded}, "
                f"lastupd={self.spa.lastupd}, "
                f"curtemp={self.spa.curtemp}, "
                f"settemp={self.spa.settemp if hasattr(self.spa, 'settemp') else 'N/A'}"
            )

            logger.info(
                f"curtemp type: {type(self.spa.curtemp)}, value: {self.spa.curtemp}, > 0: {self.spa.curtemp > 0 if self.spa.curtemp else 'N/A'}"
            )

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
            mode_names = {
                0: "Off",
                2: "Blue",
                3: "Green",
                5: "Orange",
                6: "Red",
                7: "Violet",
                9: "Aqua",
                0x80: "Blend",
            }
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

            # Heat state / heater_on:
            # Prefer raw-frame-derived state (indices 0x14 and 0x22) if available,
            # otherwise fall back to pyjacuzzi.
            if self._last_heat_state is not None and self._last_heater_on is not None:
                heatstate_text = self._last_heat_state
                heater_on = self._last_heater_on
                logger.info(f"Publishing heat_state (raw): {heatstate_text}")
                logger.info(f"Publishing heater_on (raw): {heater_on}")
            else:
                heatstate = self.spa.get_heatstate(False)  # 0=Idle, 1=Heating, 2=Heat Waiting
                heatstate_names = {0: "Idle", 1: "Heating", 2: "Heat Waiting"}
                heatstate_text = heatstate_names.get(heatstate, f"Unknown ({heatstate})")
                heater_on = "ON" if heatstate == 1 else "OFF"
                logger.info(f"Publishing heat_state (pyjacuzzi): {heatstate_text} (value={heatstate})")
                logger.info(f"Publishing heater_on (pyjacuzzi): {heater_on}")

            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/heat_state", heatstate_text)
            self.mqtt.publish(f"{MQTT_BASE_TOPIC}/heater_on", heater_on)

            # Circulation pump - direct attribute access
            # circ_pump_status: 1=ON, 0=OFF
            if hasattr(self.spa, 'circ_pump_status'):
                circ_pump_text = "ON" if self.spa.circ_pump_status > 0 else "OFF"
                logger.info(
                    f"Publishing circulation_pump: {circ_pump_text} (value={self.spa.circ_pump_status})"
                )
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/circulation_pump", circ_pump_text)

            # UV lamp - direct attribute access
            # isUVOn: >0=ON, 0=OFF
            if hasattr(self.spa, 'isUVOn'):
                uv_text = "ON" if self.spa.isUVOn > 0 else "OFF"
                logger.info(f"Publishing uv_lamp: {uv_text} (value={self.spa.isUVOn})")
                self.mqtt.publish(f"{MQTT_BASE_TOPIC}/uv_lamp", uv_text)

        except Exception as e:
            logger.error(f"Publish error: {e}", exc_info=True)


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
    def track_read_fail(self):
        """Track read fails and request restart if too many occur in a short window."""
        if not READFAIL_RESTART_ENABLE:
            return False

        now = time.time()
        # Keep only recent fails
        self.read_fail_times = [t for t in self.read_fail_times if now - t <= READFAIL_WINDOW_S]
        self.read_fail_times.append(now)

        if len(self.read_fail_times) >= READFAIL_RESTART_COUNT:
            self.restart_count += 1
            logger.warning(
                f"Auto-restart requested: {len(self.read_fail_times)} fails in {READFAIL_WINDOW_S}s window"
            )
            self.read_fail_times = []
            return True
        return False

    async def monitor_changes(self):
        """Monitor and publish state periodically."""
        while self.running:
            self.publish_state()
            await asyncio.sleep(5)

    async def run(self):
        self.running = True
        self.loop = asyncio.get_running_loop()

        # Start tap servers early
        if self.tap_all:
            self.tap_all.start()
        if self.tap_rx:
            self.tap_rx.start()
        if self.tap_json:
            self.tap_json.start()


        # MQTT
        self.mqtt = mqtt_connect()
        publish_discovery(self.mqtt)

        # Subscribe command topics
        cmd_topics = [
            f"{MQTT_BASE_TOPIC}/target_temperature/set",
            f"{MQTT_BASE_TOPIC}/pump_1/set",
            f"{MQTT_BASE_TOPIC}/pump_2/set",
            f"{MQTT_BASE_TOPIC}/light_mode/set",
            f"{MQTT_BASE_TOPIC}/light_brightness/set",
            f"{MQTT_BASE_TOPIC}/heat_mode/set",
        ]
        for t in cmd_topics:
            self.mqtt.subscribe(t)
        self.mqtt.on_message = self.on_mqtt_message

        # Spa
        self.spa = jacuzzi.JacuzziSpaWifi(JACUZZI_IP, JACUZZI_PORT)

        try:
            logger.info(f"Connecting to spa at {JACUZZI_IP}:{JACUZZI_PORT}")

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

                        logger.info(
                            f"       Temp: {temp}°F, SetTemp: {settemp}°F, Pump1: {pump1}, Pump2: {pump2}"
                        )
                        logger.info(f"       🔥 BYTE 10: 0x{byte10:02X} = 0b{byte10:08b}")
                        logger.info(
                            f"          HeatMode (bits 5,4): {heatmode} | HeatState (bits 1,0): {heatstate}"
                        )

                        # ---- RAW heater_on / heat_state correction (indices 0x14 and 0x22) ----
                        # Observed pairs (from your change logger):
                        #   (data[0x14], data[0x22]) == (0x00, 0x00) => heater ON / heating (or heating-permitted)
                        #   (data[0x14], data[0x22]) == (0x80, 0x01) => heater OFF / not heating
                        # Publish immediately and cache for publish_state().
                        if len(data) > 0x22:
                            b14 = data[0x14]
                            b22 = data[0x22]

                            raw_heater_on = None
                            if (b14 == 0x00 and b22 == 0x00):
                                raw_heater_on = "ON"
                            elif (b14 == 0x80 and b22 == 0x01):
                                raw_heater_on = "OFF"

                            if raw_heater_on is not None:
                                raw_heat_state = "Heating" if raw_heater_on == "ON" else "Idle"
                                self._last_heater_on = raw_heater_on
                                self._last_heat_state = raw_heat_state

                                logger.info(
                                    f"       🔥 RAW heater flags: b14=0x{b14:02X} b22=0x{b22:02X} "
                                    f"-> heater_on={raw_heater_on}, heat_state={raw_heat_state}"
                                )
                                try:
                                    if self.mqtt:
                                        self.mqtt.publish(
                                            f"{MQTT_BASE_TOPIC}/heater_on", raw_heater_on
                                        )
                                        self.mqtt.publish(
                                            f"{MQTT_BASE_TOPIC}/heat_state", raw_heat_state
                                        )
                                except Exception as e:
                                    logger.debug(
                                        f"MQTT publish failed for raw heater flags: {e}"
                                    )

                # Call original method
                return original_process_message(data)

            self.spa.process_message = debug_process_message

            # Run spa coroutines - EXACTLY like terminal UI lines 985-986
            logger.info("Starting spa coroutines")
            connection_task = asyncio.create_task(self.spa.check_connection_status())
            listen_task = asyncio.create_task(self.spa.listen())
            monitor_task = asyncio.create_task(self.monitor_changes())

            logger.info("Bridge running - pure pyjacuzzi mode")

            # Wait for monitor to complete
            await monitor_task

        except KeyboardInterrupt:
            logger.info("Shutting down (Ctrl+C)")
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            # If read fail, consider restart
            if self.track_read_fail():
                # Stop loop to let container restart policy handle it
                self.running = False
        finally:
            self.running = False
            try:
                if self.mqtt:
                    self.mqtt.loop_stop()
                    self.mqtt.disconnect()
            except Exception:
                pass
            try:
                if self.tap_all:
                    self.tap_all.stop()
                if self.tap_rx:
                    self.tap_rx.stop()
                if self.tap_json:
                    self.tap_json.stop()
            except Exception:
                pass


def main():
    bridge = PureJacuzziMQTTBridge()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(*_):
        logger.info("Shutdown requested")
        bridge.running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    loop.run_until_complete(bridge.run())


if __name__ == "__main__":
    main()


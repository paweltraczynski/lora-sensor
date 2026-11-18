import time
import board
import busio
import digitalio
import socketpool
import ssl
import json

# Adafruit hardware drivers.
import adafruit_rfm9x
import adafruit_dht
from adafruit_minimqtt.adafruit_minimqtt import MQTT
# https://github.com/dhalbert/CircuitPython_LCD
from lcd.lcd import LCD
from lcd.i2c_pcf8574_interface import I2CPCF8574Interface

# Custom libraries.
from wifi_connection import WiFiConnection

# Import config_local.py if user has created it.
try:
    import config_local as config
# Otherwise import config.py (assuming the user has customized it).
except ImportError:
    import config

class IndoorModule:
    """
    Controls the indoor module.

    The module:
    - Receives LoRa messages from the outdoor module
    - Reads indoor DHT22 sensor
    - Displays readings on an LCD
    - Connects to Wi-Fi
    - Connects to MQTT broker
    - Publishes readings to MQTT.

    The LED:
    - Blinks 2 times quickly on LoRa message receival
    - Blinks 10 times quickly on a successful Wi-Fi connection
    - Blinks 6 times slowly on a failed Wi-Fi connection
    """
    def __init__(self):
        # Initialize the LED.
        self.led = digitalio.DigitalInOut(board.GP5)
        self.led.direction = digitalio.Direction.OUTPUT

        # Initialize RFM9x.
        spi = busio.SPI(board.GP18, MOSI = board.GP19, MISO = board.GP16)
        cs = digitalio.DigitalInOut(board.GP17)
        rst = digitalio.DigitalInOut(board.GP20)

        try:
            self.rfm9x = adafruit_rfm9x.RFM9x(spi, cs, rst, 868.0)

            # Configure the LoRa link parameters
            # Conducted transmit power, dBm.
            self.rfm9x.tx_power = 13
            # Signal bandwidth: 125 kHz.
            self.rfm9x.signal_bandwidth = 125000
            self.rfm9x.spreading_factor = 7
            # Forward error correction: 4/5 error-correction coding
            self.rfm9x.coding_rate = 5
            self.rfm9x.preamble_length = 8
            self.rfm9x.enable_crc = True

            print('LoRa radio initialized at 868 MHz.')
        except Exception as error:
            print('LoRa radio initialization failed:', error)
            self.flashOnFault()

        # Error tracking and reporting dictionary.
        self.errors = {}

        # Initialize the DHT22 driver.
        try:
            self.dht_sensor = adafruit_dht.DHT22(board.GP14)
        except Exception as error:
            # Preserve startup failure for later status reports.
            self.dht_sensor = None
            self.errorHandler('dht', 'raise', 'DHT22 initialization error: ' + str(error))

        # Initialize the LCD and print placeholders.
        try:
            i2c = busio.I2C(scl = board.GP1, sda = board.GP0)
            self.lcd = LCD(I2CPCF8574Interface(i2c, 0x27), num_rows = 2, num_cols = 16)
            self.printPlaceholders()
        except Exception as error:
            self.lcd = None
            self.errorHandler('lcd', 'raise', 'LCD initialization error: ' + str(error))

        # Initialize the Wi-Fi.
        # Connection is made in the main loop.
        self.wifi = WiFiConnection(config.wifi_ssid, config.wifi_password)
        self.wifi_has_connected = self.wifi.isConnected()

        # Initialize the MQTT client.
        self.mqtt_topic = config.mqtt_topic
        self.mqtt_client = MQTT(
            broker = config.mqtt_server,
            port = config.mqtt_port,
            username = config.mqtt_username,
            password = config.mqtt_password,
            socket_pool = socketpool.SocketPool(self.wifi.wlan),
            is_ssl = True,
            ssl_context = ssl.create_default_context()
        )

        self.mqtt_connected = False
        self.mqtt_last_attempt = None
        self.mqtt_last_publish = None
        self.mqtt_last_service = None

        # Most recent indoor and outdoor data.
        self.indoor_temperature = None
        self.indoor_humidity = None
        self.outdoor_temperature = None
        self.outdoor_humidity = None

        # Track indoor and outdoor data refresh times.
        self.indoor_last_refresh = None
        self.outdoor_last_received = None

        # LoRa device ID.
        self.device_id = config.lora_device_id

        # When the module has started.
        self.module_started_at = time.monotonic()

    def errorHandler(self, key, action, description=''):
        """
        Records and logs an error, or clears an existing error entry.

        :param key: Error identifier used in self.errors.
        :param action: 'raise' to record and log, or 'clear' to remove if present.
        :param description: Error message; used only for the 'raise' action.
        """
        if action == 'raise':
            if self.errors.get(key) != description:
                self.errors[key] = description
                print(description)
        elif action == 'clear':
            if key in self.errors:
                self.errors.pop(key)
        else:
            raise ValueError('Unknown error action: ' + str(action))

    def connectWifi(self):
        """
        Connects/reconnects Wi-Fi.

        If the connection to Wi-Fi fails, an error is set.
        If the connection succeeds, the error is NOT cleared so
        that it can be included in the next successful MQTT publication.
        """
        try:
            if not self.wifi.isConnected():
                # Report an error if the Wi-Fi has disconnected.
                if self.wifi_has_connected:
                    self.errorHandler('wifi', 'raise', 'Wi-Fi disconnected.')

                # A lost Wi-Fi link invalidates the MQTT session.
                self.mqtt_connected = False
                # Try to reconnect to Wi-Fi.
                previous_attempt = self.wifi.wifi_last_attempt
                self.wifi.connect()

                # Indicate that Wi-Fi has been connected.
                if self.wifi.isConnected():
                    self.wifi_has_connected = True
                    self.flashLed(10, 0.1)
                elif self.wifi.wifi_last_attempt != previous_attempt:
                    # A cooldown skip leaves the attempt timestamp unchanged.
                    # Report an error if the Wi-Fi failed to connect.
                    if not self.wifi_has_connected:
                        self.errorHandler('wifi', 'raise', 'Wi-Fi connection failed.')
                    self.flashLed(6, 0.4)
            else:
                self.wifi_has_connected = True

        # Report any other Wi-Fi connection errors.
        except Exception as error:
            self.errorHandler('wifi', 'raise', 'Wi-Fi connection error: ' + str(error))
            # A lost Wi-Fi link invalidates the MQTT session.
            self.mqtt_connected = False

    def connectMqtt(self):
        """
        Connects/reconnects to MQTT.

        If the connection to MQTT fails, an error is set.
        If the connection succeeds, the error is NOT cleared so
        that it can be included in the next successful MQTT publication.

        :return: True if connected, False if not.
        """
        # MQTT connection state is verified by MQTT loop() and publish().
        if self.mqtt_connected:
            return True

        # Don't try to reconnect more often than every 10 seconds.
        now = time.monotonic()
        if (self.mqtt_last_attempt is not None
                and now - self.mqtt_last_attempt < 10):
            return False

        self.mqtt_last_attempt = now

        # Try to connect to MQTT. Record an error on failure.
        try:
            self.mqtt_client.connect()
        except Exception as error:
            self.errorHandler('mqtt', 'raise', 'MQTT connection error: ' + str(error))

            self.mqtt_connected = False
            self.mqtt_last_attempt = time.monotonic()

            try:
                self.mqtt_client.disconnect()
            except Exception:
                pass
            return False

        # Retain prior MQTT errors until their recovery report is published.
        self.mqtt_connected = True
        return True

    def publishMqtt(self, message):
        """
        Publishes the message using MQTT.

        If the publishing to MQTT fails, an error is set.
        If the publishing to MQTT succeeds, the error is cleared.

        :param message: The message to publish.
        """
        try:
            # Service MQTT every ten seconds, independently of publication.
            now = time.monotonic()
            if self.mqtt_last_service is None or now - self.mqtt_last_service >= 10:
                self.mqtt_client.loop(timeout=1)
                self.mqtt_last_service = time.monotonic()

            # Throttle publication using the configured interval in seconds.
            now = time.monotonic()
            if (self.mqtt_last_publish is not None
                    and now - self.mqtt_last_publish < config.mqtt_publish_interval):
                return

            # Publish the message and save the timestamp.
            self.mqtt_client.publish(self.mqtt_topic, message)
            self.mqtt_last_publish = time.monotonic()

        except Exception as error:
            self.errorHandler('mqtt', 'raise', 'MQTT connection or publish error: ' + str(error))

            # Reset MQTT connection state on errors.
            self.mqtt_connected = False
            self.mqtt_last_attempt = time.monotonic()
            try:
                self.mqtt_client.disconnect()
            except Exception:
                pass
        else:
            # If everything worked, then clear network errors.
            print('Sent MQTT message:', message)
            self.errorHandler('wifi', 'clear')
            # Retained network errors have now been reported and can be cleared.
            self.errorHandler('mqtt', 'clear')

    def flashLed(self, times, duration):
        """
        Flashes the LED a specified number of times.

        :param times: How many times to flash the LED.
        :param duration: Duration of each on and off period, in seconds.
        """
        for _ in range(times):
            self.led.value = True
            time.sleep(duration)
            self.led.value = False
            time.sleep(duration)

    def flashOnFault(self):
        """
        Flashes the LED indefinitely after an initialization failure.

        Blocks normal operation until interrupted or reset.
        """
        while True:
            self.flashLed(1, 0.2)

    def receiveOutdoor(self):
        """
        Receives outdoor measurements using LoRa.

        If a valid outdoor packet doesn't arrive within 2 minutes, an error is set.
        If a valid packet from the configured outdoor device arrives, the error is cleared.
        LoRa radio receive() failures also set an error.
        """
        # If the outdoor readings have been updated.
        outdoor_updated = False

        # Get the LoRa packet.
        try:
            packet = self.rfm9x.receive(timeout=0.5, keep_listening=True)
        except Exception as error:
            self.errorHandler('lora', 'raise', 'LoRa receive error: ' + str(error))
            packet = None
        else:
            self.errorHandler('lora', 'clear')

        if packet is not None:
            # Malformed packets must not interrupt local DHT22 and MQTT.
            # Check whether the packet contains valid JSON.
            try:
                data = json.loads(packet.decode('utf-8'))
            except (UnicodeError, ValueError) as error:
                print('Ignoring invalid LoRa message:', error)
            else:
                if not isinstance(data, dict):
                    print('Ignoring LoRa message: expected a JSON object.')
                # Ignore packets from other devices.
                elif data.get('device_id') != self.device_id:
                    pass
                # Ignore packets missing either measurement.
                elif 'temperature' not in data or 'humidity' not in data:
                    print('Ignoring LoRa message: missing temperature or humidity.')
                elif (
                    type(data['temperature']) not in (int, float)
                    or type(data['humidity']) not in (int, float)
                ):
                    print('Ignoring LoRa message: temperature and humidity must be numeric.')
                # DHT22 measurement limits also bound LCD field widths.
                elif not (-40 <= data['temperature'] <= 80 and 0 <= data['humidity'] <= 100):
                    print('Ignoring LoRa message: measurements exceed DHT22 ranges.')
                # At this point the packet is valid and accepted.
                else:
                    self.outdoor_temperature = data['temperature']
                    self.outdoor_humidity = data['humidity']
                    self.outdoor_last_received = time.monotonic()
                    print('Received LoRa message:', data)
                    self.flashLed(2, 0.15)
                    outdoor_updated = True

        # Report an error if a LoRa packet doesn't arrive in 2 minutes.
        if self.outdoor_last_received is None:
            reference = self.module_started_at
        else:
            reference = self.outdoor_last_received

        if time.monotonic() - reference >= 120:
            self.errorHandler('outdoor', 'raise', 'Outdoor LoRa message missing for 2 minutes.')
        else:
            self.errorHandler('outdoor', 'clear')

        return outdoor_updated

    def readIndoor(self):
        """
        Reads indoor DHT22 measurements.

        If the reading fails, an error is set.
        If the reading succeeds, the error is cleared.
        
        :return: True if there is a new reading, False otherwise.
        """
        # Don't read the indoor data more often than every 10 seconds
        # as it would look weird if it updates LCD too often.
        now = time.monotonic()
        if (self.indoor_last_refresh is not None
                and now - self.indoor_last_refresh < 10):
            return False

        try:
            # Retry failed initialization within the existing read cooldown.
            if self.dht_sensor is None:
                self.dht_sensor = adafruit_dht.DHT22(board.GP14)
            temperature = self.dht_sensor.temperature
            humidity = self.dht_sensor.humidity

        except Exception as error:
            self.errorHandler('dht', 'raise', 'DHT22 error: ' + str(error))
            return False

        else:
            self.indoor_temperature = temperature
            self.indoor_humidity = humidity
            self.errorHandler('dht', 'clear')
            return True

        finally:
            self.indoor_last_refresh = time.monotonic()

    def printPlaceholders(self):
        """
        Initializes the LCD with placeholder contents.
        """
        # LCD methods:
        # lcd.clear()
        # lcd.home()
        # lcd.set_cursor_pos(0, 0)
        # lcd.print('Text')
        # lcd.create_char(location = 0, bitmap = [])
        # lcd.command(value)
        self.lcd.clear()

        # Print icons.
        self.lcd.create_char(location = 0, bitmap = [
            0x04, 0x0E, 0x04, 0x0E,
            0x04, 0x0E, 0x0A, 0x0E
        ])
        self.lcd.set_cursor_pos(0, 0)
        self.lcd.write(0x00)

        self.lcd.create_char(location = 1, bitmap = [
            0x04, 0x04, 0x0E, 0x0A,
            0x1B, 0x11, 0x11, 0x0E
        ])
        self.lcd.set_cursor_pos(0, 9)
        self.lcd.write(0x01)

        # Print placeholders.
        self.lcd.set_cursor_pos(0, 1)
        self.lcd.print(' --.-C')

        self.lcd.set_cursor_pos(0, 11)
        self.lcd.print('--.-%')

        self.lcd.set_cursor_pos(1, 1)
        self.lcd.print(' --.-C')

        self.lcd.set_cursor_pos(1, 11)
        self.lcd.print('--.-%')

    def printOutdoor(self):
        """
        Prints formatted outdoor temperature and humidity on the LCD.
        """
        # Skip if the outdoor reading is not yet available.
        if self.outdoor_temperature is None or self.outdoor_humidity is None:
            return

        temperature = self.outdoor_temperature
        humidity = self.outdoor_humidity

        # Print formatted outdoor temperature.
        temperature = '{:5.1f}'.format(temperature)

        self.lcd.set_cursor_pos(0, 1)
        self.lcd.print(temperature)

        # Print formatted outdoor humidity.
        if humidity > 99.9:
            humidity = 99.9

        self.lcd.set_cursor_pos(0, 11)
        self.lcd.print('{:4.1f}'.format(humidity))

    def printIndoor(self):
        """
        Prints formatted indoor temperature and humidity on the LCD.
        """
        # Skip if the indoor reading is not yet available.
        if self.indoor_temperature is None or self.indoor_humidity is None:
            return

        temperature = self.indoor_temperature
        humidity = self.indoor_humidity

        # Print formatted indoor temperature.
        temperature = '{:5.1f}'.format(temperature)

        self.lcd.set_cursor_pos(1, 1)
        self.lcd.print(temperature)

        # Print formatted indoor humidity.
        if humidity > 99.9:
            humidity = 99.9

        self.lcd.set_cursor_pos(1, 11)
        self.lcd.print('{:4.1f}'.format(humidity))

    def run(self):
        """
        Main loop.

        Handles receiving LoRa messages, reading indoor DHT22,
        keeping Wi-Fi alive, and sending MQTT messages.
        """
        print('Listening for LoRa messages.')

        while True:
            # Check for LoRa messages on each iteration.
            outdoor_updated = self.receiveOutdoor()

            # Read indoor DHT22 sensor.
            indoor_updated = self.readIndoor()

            # Print readings if they were updated.
            if self.lcd is not None and (outdoor_updated or indoor_updated):
                try:
                    if outdoor_updated:
                        self.printOutdoor()
                    if indoor_updated:
                        self.printIndoor()
                except Exception as error:
                    self.errorHandler('lcd', 'raise', 'LCD update error: ' + str(error))
                else:
                    self.errorHandler('lcd', 'clear')

            # Recover Wi-Fi if connection was lost, before attempting MQTT.
            self.connectWifi()

            # Publish to MQTT only after both Wi-Fi and MQTT are connected.
            if self.wifi.isConnected() and self.connectMqtt():
                # Prepare the status info.
                if self.errors:
                    status = '|'.join(self.errors.values())
                elif self.outdoor_last_received is None:
                    status = 'Waiting for first outdoor reading.'
                else:
                    status = 'Everything works.'

                # Prepare the MQTT message with readings and the status.
                message = json.dumps({
                    'indoor_temperature': self.indoor_temperature,
                    'indoor_humidity': self.indoor_humidity,
                    'outdoor_temperature': self.outdoor_temperature,
                    'outdoor_humidity': self.outdoor_humidity,
                    'status': status,
                }, separators=(',', ':'))

                self.publishMqtt(message)

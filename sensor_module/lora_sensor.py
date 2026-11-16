import time
import board
import busio
import digitalio
import json
import sys

# Adafruit hardware drivers.
import adafruit_rfm9x
import adafruit_dht

class LoRaSensor:
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

            print('LoRa radio initialized at 868 MHz')
        except Exception as error:
            print('LoRa radio initialization failed:', error)
            self.flashOnFault()

        # Initialize the DHT22 driver.
        try:
            self.dht_sensor = adafruit_dht.DHT22(board.GP4)
        except Exception as error:
            print('DHT22 sensor initialization failed:', error)
            self.flashOnFault()

        self.device_id = 'lora_sensor_1'
        self.sequence_number = 0

    def flashLed(self,times, duration):
        """
        Flashes the LED a specified number of times.

        :param times: How many times to flash the LED.
        :param duration: The duration of each flash, in seconds.
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

    def run(self):
        """
        Reads sensor measurements and transmit them periodically.
        """
        while True:
            # Read both measurements; on failure, indicate the error and retry
            # after a delay.
            try:
                temperature = self.dht_sensor.temperature
                humidity = self.dht_sensor.humidity
            except RuntimeError as error:
                print("DHT22 read error:", error)
                self.flashLed(10, 0.2)
                time.sleep(5)
                continue

            # Encode the sensor data as a JSON string.
            data = json.dumps({
                'device_id': self.device_id,
                'sequence': self.sequence_number,
                'temperature': temperature,
                'humidity': humidity,
            }, separators = (',', ':'))

            self.sequence_number += 1

            # Transmit the measurement; indicate success or failure with the LED.
            try:
                self.rfm9x.send(bytes(data, 'utf-8'))
            except Exception as error:
                print('Radio transmission error:')
                sys.print_exception(error)
                self.flashLed(10, 0.2)
            else:
                print('Sent:', data)
                self.flashLed(2, 0.15)

            time.sleep(5)

import time
import wifi

class WiFiConnection:
    """
    Manages a Wi-Fi connection.

    :param ssid: The Wi-Fi network name.
    :param password: The Wi-Fi network password.
    :param retry_interval: Minimum time between connection attempts, in seconds.
    """

    def __init__(self, ssid, password, retry_interval = 10):
        self.wlan = wifi.radio
        self.ssid = ssid
        self.password = password
        self.wifi_last_attempt = None
        self.wifi_retry_interval = retry_interval

    def connect(self):
        """
        Connects to the Wi-Fi network, retries if the SSID is unavailable.
        """
        if self.wlan.connected:
            return True

        current_ticks = time.monotonic()

        # Do not retry connecting more often than the configured interval.
        if (
            self.wifi_last_attempt is not None and
            current_ticks - self.wifi_last_attempt < self.wifi_retry_interval
        ):
            return False

        # Initiate a Wi-Fi connection.
        try:
            self.wlan.connect(self.ssid, self.password, timeout=10)
        except OSError as error:
            # If the connection failed, then try to disconnect before retrying.
            try:
                self.wlan.stop_station()
            except OSError:
                pass

            print('Wi-Fi connection failed:', error)

            return False
        finally:
            # Measure the retry delay from the end of this attempt.
            self.wifi_last_attempt = time.monotonic()

        print('Established Wi-Fi connection.')
        return True

    def isConnected(self):
        """
        Returns True if the WLAN interface is connected.
        """
        return self.wlan.connected

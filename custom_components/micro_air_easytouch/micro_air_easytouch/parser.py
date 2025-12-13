# Standard library imports for basic functionality
from __future__ import annotations
import logging
import asyncio
import time
import json

# Bluetooth-related imports for device communication
from bleak import BLEDevice
from bleak.exc import BleakError, BleakDBusError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
    retry_bluetooth_connection_error,
)

from bluetooth_data_tools import short_address
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo
from sensor_state_data import SensorDeviceClass, SensorUpdate, Units
from sensor_state_data.enum import StrEnum

from ..const import DOMAIN
from .const import UUIDS

_LOGGER = logging.getLogger(__name__)

from functools import wraps
def retry_authentication(retries=3, delay=1):
    """Custom retry decorator for authentication attempts."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(retries):
                try:
                    result = await func(*args, **kwargs)
                    if result:
                        _LOGGER.debug("Authentication successful on attempt %d/%d", attempt + 1, retries)
                        return True
                    _LOGGER.debug("Authentication returned False on attempt %d/%d", attempt + 1, retries)
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
                        continue
                except Exception as e:
                    last_exception = e
                    _LOGGER.debug("Authentication attempt %d/%d failed: %s", attempt + 1, retries, str(e))
                    if attempt < retries - 1:
                        await asyncio.sleep(delay)
                        continue
            if last_exception:
                _LOGGER.error("Authentication failed after %d attempts: %s", retries, str(last_exception))
            else:
                _LOGGER.error("Authentication failed after %d attempts with no exception", retries)
            return False
        return wrapper
    return decorator

class MicroAirEasyTouchSensor(StrEnum):
    """Enumeration of all available sensors for the MicroAir EasyTouch device."""
    FACE_PLATE_TEMPERATURE = "face_plate_temperature"
    CURRENT_MODE = "current_mode"
    MODE = "mode"
    FAN_MODE = "fan_mode"
    AUTO_HEAT_SP = "autoHeat_sp"
    AUTO_COOL_SP = "autoCool_sp"
    COOL_SP = "cool_sp"
    HEAT_SP = "heat_sp"
    DRY_SP = "dry_sp"

class MicroAirEasyTouchBluetoothDeviceData(BluetoothData):
    """Main class for handling MicroAir EasyTouch device data and communication."""

    def __init__(self, password: str | None = None, email: str | None = None) -> None:
        """Initialize the device data handler with optional credentials."""
        super().__init__()
        self._password = password
        self._email = email
        self._client = None
        self._max_delay = 6.0
        self._notification_task = None

    def _get_operation_delay(self, hass, address: str, operation: str) -> float:
        """Calculate delay for specific operations from persistent storage."""
        device_delays = hass.data.setdefault(DOMAIN, {}).setdefault('device_delays', {}).get(address, {})
        return device_delays.get(operation, {}).get('delay', 0.0)

    def _increase_operation_delay(self, hass, address: str, operation: str) -> float:
        """Increase delay for specific operation and device with persistence."""
        delays = hass.data.setdefault(DOMAIN, {}).setdefault('device_delays', {})
        if address not in delays:
            delays[address] = {}
        if operation not in delays[address]:
            delays[address][operation] = {'delay': 0.0, 'failures': 0}
        current = delays[address][operation]
        current['failures'] += 1
        current['delay'] = min(0.5 * (2 ** min(current['failures'], 3)), self._max_delay)
        _LOGGER.debug("Increased delay for %s:%s to %.1fs (failures: %d)", address, operation, current['delay'], current['failures'])
        return current['delay']

    def _adjust_operation_delay(self, hass, address: str, operation: str) -> None:
        """Adjust delay for specific operation after success, reducing gradually."""
        delays = hass.data.setdefault(DOMAIN, {}).setdefault('device_delays', {})
        if address in delays and operation in delays[address]:
            current = delays[address][operation]
            if current['failures'] > 0:
                current['failures'] = max(0, current['failures'] - 1)
                current['delay'] = max(0.0, current['delay'] * 0.75)
                _LOGGER.debug("Adjusted delay for %s:%s to %.1fs (failures: %d)", address, operation, current['delay'], current['failures'])
            if current['failures'] == 0 and current['delay'] < 0.1:
                current['delay'] = 0.0
                _LOGGER.debug("Reset delay for %s:%s to 0.0s", address, operation)

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        _LOGGER.debug("Parsing MicroAirEasyTouch BLE advertisement data: %s", service_info)
        self.set_device_manufacturer("MicroAirEasyTouch")
        self.set_device_type("Thermostat")
        name = f"{service_info.name} {short_address(service_info.address)}"
        self.set_device_name(name)
        self.set_title(name)

    def decrypt(self, data: bytes) -> dict:
        """Parse and decode the device status data."""
        status = json.loads(data)
        info = status['Z_sts']['0']
        param = status['PRM']
        modes = {0: "off", 5: "heat_on", 4: "heat", 3: "cool_on", 2: "cool", 1: "fan", 11: "auto"}
        fan_modes_full = {0: "off", 1: "manualL", 2: "manualH", 65: "cycledL", 66: "cycledH", 128: "full auto"}
        fan_modes_fan_only = {0: "off", 1: "low", 2: "high"}
        hr_status = {}
        hr_status['SN'] = status['SN']
        hr_status['autoHeat_sp'] = info[0]
        hr_status['autoCool_sp'] = info[1]
        hr_status['cool_sp'] = info[2]
        hr_status['heat_sp'] = info[3]
        hr_status['dry_sp'] = info[4]
        hr_status['fan_mode_num'] = info[6]  # Fan setting in fan-only mode
        hr_status['cool_fan_mode_num'] = info[7]  # Fan setting in cool mode
        hr_status['auto_fan_mode_num'] = info[9]  # Fan setting in auto mode
        hr_status['mode_num'] = info[10]
        hr_status['heat_fan_mode_num'] = info[11]  # Fan setting in heat mode
        hr_status['facePlateTemperature'] = info[12]
        hr_status['current_mode_num'] = info[15]
        hr_status['ALL'] = status

        if 7 in param:
            hr_status['off'] = True
        if 15 in param:
            hr_status['on'] = True

        # Map modes
        if hr_status['current_mode_num'] in modes:
            hr_status['current_mode'] = modes[hr_status['current_mode_num']]
        if hr_status['mode_num'] in modes:
            hr_status['mode'] = modes[hr_status['mode_num']]

        # Map fan modes based on current mode
        current_mode = hr_status.get('mode', "off")
        
        # Store the raw fan mode numbers and their string representations
        if current_mode == "fan":
            fan_num = info[6]
            hr_status['fan_mode_num'] = fan_num
            hr_status['fan_mode'] = fan_modes_fan_only.get(fan_num, "off")
        elif current_mode == "cool":
            fan_num = info[7]
            hr_status['cool_fan_mode_num'] = fan_num
            hr_status['cool_fan_mode'] = fan_modes_full.get(fan_num, "full auto")
        elif current_mode == "heat":
            fan_num = info[11]
            hr_status['heat_fan_mode_num'] = fan_num
            hr_status['heat_fan_mode'] = fan_modes_full.get(fan_num, "full auto")
        elif current_mode == "auto":
            fan_num = info[9]
            hr_status['auto_fan_mode_num'] = fan_num
            hr_status['auto_fan_mode'] = fan_modes_full.get(fan_num, "full auto")

        return hr_status

    @retry_bluetooth_connection_error(attempts=7)
    async def _connect_to_device_impl(self, ble_device: BLEDevice):
        """Internal implementation of device connection."""
        try:
            self._client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.address,
                timeout=20.0
            )
            if not self._client.services:
                await asyncio.sleep(2)
            if not self._client.services:
                _LOGGER.error("No services available after connecting")
                return False
            return self._client
        except Exception as e:
            _LOGGER.error("Connection error: %s", str(e))
            raise
    
    async def _connect_to_device(self, ble_device: BLEDevice, ble_lock: asyncio.Lock | None = None):
        """Connect to the device with retries."""
        # Serialize connection attempts to prevent concurrent connections
        if ble_lock:
            async with ble_lock:
                return await self._connect_to_device_impl(ble_device)
        else:
            return await self._connect_to_device_impl(ble_device)

    @retry_authentication(retries=3, delay=2)
    async def authenticate(self, password: str) -> bool:
        """Authenticate with the device using the provided password."""
        try:
            if not self._client or not self._client.is_connected:
                await asyncio.sleep(1)
                if not self._client or not self._client.is_connected:
                    await self._connect_to_device(self._ble_device)
                    await asyncio.sleep(0.5)
                if not self._client or not self._client.is_connected:
                    _LOGGER.error("Client not connected after reconnecting")
                    return False
            if not self._client.services:
                await self._client.discover_services()
                await asyncio.sleep(1)
                if not self._client.services:
                    _LOGGER.error("Services not discovered")
                    return False
            password_bytes = password.encode('utf-8')
            await self._client.write_gatt_char(UUIDS["passwordCmd"], password_bytes, response=True)
            _LOGGER.debug("Authentication sent successfully")
            return True
        except Exception as e:
            _LOGGER.error("Authentication failed: %s", str(e))
            if self._client and self._client.is_connected:
                await self._client.disconnect()
            self._client = None
            return False

    async def _write_gatt_with_retry(self, hass, uuid: str, data: bytes, ble_device: BLEDevice, retries: int = 3, ble_lock: asyncio.Lock | None = None) -> bool:
        """Write GATT characteristic with retry and adaptive delay."""
        # Serialize writes to prevent concurrent operations
        if ble_lock:
            async with ble_lock:
                return await self._write_gatt_with_retry_impl(hass, uuid, data, ble_device, retries)
        else:
            return await self._write_gatt_with_retry_impl(hass, uuid, data, ble_device, retries)
    
    async def _write_gatt_with_retry_impl(self, hass, uuid: str, data: bytes, ble_device: BLEDevice, retries: int = 3) -> bool:
        """Internal implementation of GATT write with retry."""
        last_error = None
        for attempt in range(retries):
            try:
                if not self._client or not self._client.is_connected:
                    # Note: _reconnect_and_authenticate will handle its own locking
                    if not await self._reconnect_and_authenticate(hass, ble_device, None):
                        return False
                write_delay = self._get_operation_delay(hass, ble_device.address, 'write')
                if write_delay > 0:
                    await asyncio.sleep(write_delay)
                await self._client.write_gatt_char(uuid, data, response=True)
                self._adjust_operation_delay(hass, ble_device.address, 'write')
                return True
            except BleakError as e:
                last_error = e
                if attempt < retries - 1:
                    delay = self._increase_operation_delay(hass, ble_device.address, 'write')
                    _LOGGER.debug("GATT write failed, attempt %d/%d. Delay: %.1f", attempt + 1, retries, delay)
                    continue
        _LOGGER.error("GATT write failed after %d attempts: %s", retries, str(last_error))
        return False

    async def _reconnect_and_authenticate(self, hass, ble_device: BLEDevice, ble_lock: asyncio.Lock | None = None) -> bool:
        """Reconnect and re-authenticate with adaptive delays."""
        # Note: This is called from within locked contexts, so we don't lock again here
        # The lock is passed through to _connect_to_device which handles it
        try:
            connect_delay = self._get_operation_delay(hass, ble_device.address, 'connect')
            if connect_delay > 0:
                await asyncio.sleep(connect_delay)
            # Pass None for lock since we're already in a locked context if called from locked method
            self._client = await self._connect_to_device_impl(ble_device)
            if not self._client or not self._client.is_connected:
                self._increase_operation_delay(hass, ble_device.address, 'connect')
                return False
            self._adjust_operation_delay(hass, ble_device.address, 'connect')
            auth_delay = self._get_operation_delay(hass, ble_device.address, 'auth')
            if auth_delay > 0:
                await asyncio.sleep(auth_delay)
            auth_result = await self.authenticate(self._password)
            if auth_result:
                self._adjust_operation_delay(hass, ble_device.address, 'auth')
            else:
                self._increase_operation_delay(hass, ble_device.address, 'auth')
            return auth_result
        except Exception as e:
            _LOGGER.error("Reconnection failed: %s", str(e))
            self._increase_operation_delay(hass, ble_device.address, 'connect')
            return False

    async def _read_gatt_with_retry(self, hass, characteristic, ble_device: BLEDevice, retries: int = 3, ble_lock: asyncio.Lock | None = None) -> bytes | None:
        """Read GATT characteristic with retry and operation-specific delay."""
        # Serialize reads to prevent concurrent operations
        if ble_lock:
            async with ble_lock:
                return await self._read_gatt_with_retry_impl(hass, characteristic, ble_device, retries)
        else:
            return await self._read_gatt_with_retry_impl(hass, characteristic, ble_device, retries)
    
    async def _read_gatt_with_retry_impl(self, hass, characteristic, ble_device: BLEDevice, retries: int = 3) -> bytes | None:
        """Internal implementation of GATT read with retry."""
        last_error = None
        for attempt in range(retries):
            try:
                if not self._client or not self._client.is_connected:
                    # Note: _reconnect_and_authenticate will handle its own locking
                    if not await self._reconnect_and_authenticate(hass, ble_device, None):
                        return None
                read_delay = self._get_operation_delay(hass, ble_device.address, 'read')
                if read_delay > 0:
                    await asyncio.sleep(read_delay)
                result = await self._client.read_gatt_char(characteristic)
                self._adjust_operation_delay(hass, ble_device.address, 'read')
                return result
            except BleakError as e:
                last_error = e
                if attempt < retries - 1:
                    delay = self._increase_operation_delay(hass, ble_device.address, 'read')
                    _LOGGER.debug("GATT read failed, attempt %d/%d. Delay: %.1f", attempt + 1, retries, delay)
                    continue
        _LOGGER.error("GATT read failed after %d attempts: %s", retries, str(last_error))
        return None

    async def reboot_device(self, hass, ble_device: BLEDevice, ble_lock: asyncio.Lock | None = None) -> bool:
        """Reboot the device by sending reset command."""
        # Serialize reboot to prevent concurrent operations
        if ble_lock:
            async with ble_lock:
                return await self._reboot_device_impl(hass, ble_device)
        else:
            return await self._reboot_device_impl(hass, ble_device)
    
    async def _reboot_device_impl(self, hass, ble_device: BLEDevice) -> bool:
        """Internal implementation of device reboot."""
        try:
            self._ble_device = ble_device
            self._client = await self._connect_to_device_impl(ble_device)
            if not self._client or not self._client.is_connected:
                _LOGGER.error("Failed to connect for reboot")
                return False
            if not await self.authenticate(self._password):
                _LOGGER.error("Failed to authenticate for reboot")
                return False
            write_delay = self._get_operation_delay(hass, ble_device.address, 'write')
            if write_delay > 0:
                await asyncio.sleep(write_delay)
            reset_cmd = {"Type": "Change", "Changes": {"zone": 0, "reset": " OK"}}
            cmd_bytes = json.dumps(reset_cmd).encode()
            try:
                await self._client.write_gatt_char(UUIDS["jsonCmd"], cmd_bytes, response=True)
                _LOGGER.info("Reboot command sent successfully")
                return True
            except BleakError as e:
                if "Error" in str(e) and "133" in str(e):
                    _LOGGER.info("Device is rebooting as expected")
                    return True
                _LOGGER.error("Failed to send reboot command: %s", str(e))
                self._increase_operation_delay(hass, ble_device.address, 'write')
                return False
        except Exception as e:
            _LOGGER.error("Error during reboot: %s", str(e))
            return False
        finally:
            try:
                if self._client and self._client.is_connected:
                    await self._client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error disconnecting after reboot: %s", str(e))
            self._client = None
            self._ble_device = None

    async def send_command(self, hass, ble_device: BLEDevice, command: dict, ble_lock: asyncio.Lock | None = None) -> bool:
        """Send command to device."""
        # Serialize command sending to prevent concurrent operations
        if ble_lock:
            async with ble_lock:
                return await self._send_command_impl(hass, ble_device, command)
        else:
            return await self._send_command_impl(hass, ble_device, command)
    
    async def _send_command_impl(self, hass, ble_device: BLEDevice, command: dict) -> bool:
        """Internal implementation of command sending."""
        try:
            if not self._client or not self._client.is_connected:
                self._client = await self._connect_to_device_impl(ble_device)
                if not self._client or not self._client.is_connected:
                    return False
                if not await self.authenticate(self._password):
                    return False
            command_bytes = json.dumps(command).encode()
            # Pass None for lock since we're already in a locked context
            return await self._write_gatt_with_retry_impl(hass, UUIDS["jsonCmd"], command_bytes, ble_device)
        except Exception as e:
            _LOGGER.error("Error sending command: %s", str(e))
            return False
        finally:
            try:
                if self._client and self._client.is_connected:
                    await self._client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error disconnecting: %s", str(e))
            self._client = None
    
    async def send_command_and_read(
        self, hass, ble_device: BLEDevice, command: dict, read_uuid: str, ble_lock: asyncio.Lock | None = None
    ) -> bytes | None:
        """
        Send command and read response in a single connection.
        This is more efficient than send_command() followed by _read_gatt_with_retry()
        as it avoids disconnecting and reconnecting.
        """
        # Serialize operations to prevent concurrent access
        if ble_lock:
            async with ble_lock:
                return await self._send_command_and_read_impl(hass, ble_device, command, read_uuid)
        else:
            return await self._send_command_and_read_impl(hass, ble_device, command, read_uuid)
    
    async def _send_command_and_read_impl(
        self, hass, ble_device: BLEDevice, command: dict, read_uuid: str
    ) -> bytes | None:
        """Internal implementation of send command and read."""
        try:
            # Connect if needed
            if not self._client or not self._client.is_connected:
                self._client = await self._connect_to_device_impl(ble_device)
                if not self._client or not self._client.is_connected:
                    return None
                if not await self.authenticate(self._password):
                    return None
            
            # Send command
            command_bytes = json.dumps(command).encode()
            write_success = await self._write_gatt_with_retry_impl(hass, UUIDS["jsonCmd"], command_bytes, ble_device)
            if not write_success:
                return None
            
            # Small delay to allow device to process
            await asyncio.sleep(0.5)
            
            # Read response (reuse existing connection)
            read_delay = self._get_operation_delay(hass, ble_device.address, 'read')
            if read_delay > 0:
                await asyncio.sleep(read_delay)
            
            try:
                result = await self._client.read_gatt_char(read_uuid)
                self._adjust_operation_delay(hass, ble_device.address, 'read')
                return result
            except BleakError as e:
                _LOGGER.debug("GATT read failed: %s", str(e))
                self._increase_operation_delay(hass, ble_device.address, 'read')
                return None
                
        except Exception as e:
            _LOGGER.error("Error in send_command_and_read: %s", str(e))
            return None
        finally:
            # Disconnect after both operations complete
            try:
                if self._client and self._client.is_connected:
                    await self._client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error disconnecting: %s", str(e))
            self._client = None
"""Support for MicroAirEasyTouch climate control."""
from __future__ import annotations

import asyncio
import logging
import json
import time
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from . import get_ble_device_with_adapter
from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .micro_air_easytouch.const import (
    UUIDS,
    HA_MODE_TO_EASY_MODE,
    EASY_MODE_TO_HA_MODE,
    FAN_MODES_FULL,
    FAN_MODES_FAN_ONLY,
    FAN_MODES_REVERSE,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MicroAirEasyTouch climate platform."""
    data = hass.data[DOMAIN][config_entry.entry_id]["data"]
    entity = MicroAirEasyTouchClimate(data, config_entry.unique_id, config_entry.entry_id)
    async_add_entities([entity])
    
    # Store entity reference so it can be updated from advertisement callbacks
    if "entities" not in hass.data[DOMAIN][config_entry.entry_id]:
        hass.data[DOMAIN][config_entry.entry_id]["entities"] = []
    hass.data[DOMAIN][config_entry.entry_id]["entities"].append(entity)

class MicroAirEasyTouchClimate(ClimateEntity):
    """Representation of MicroAirEasyTouch Climate."""

    _attr_has_entity_name = True
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = list(HA_MODE_TO_EASY_MODE.keys())
    _attr_should_poll = True
    # Poll more frequently for better responsiveness (15 seconds)
    # State changes also update immediately after commands
    SCAN_INTERVAL = 15

    # Map our modes to Home Assistant fan icons
    _FAN_MODE_ICONS = {
        "off": "mdi:fan-off",
        "low": "mdi:fan-speed-1",
        "high": "mdi:fan-speed-3",
        "manualL": "mdi:fan-speed-1",
        "manualH": "mdi:fan-speed-3",
        "cycledL": "mdi:fan-clock",
        "cycledH": "mdi:fan-clock",
        "full auto": "mdi:fan-auto",
    }

    # Map HVAC modes to icons
    _HVAC_MODE_ICONS = {
        HVACMode.OFF: "mdi:power",
        HVACMode.HEAT: "mdi:fire",
        HVACMode.COOL: "mdi:snowflake",
        HVACMode.AUTO: "mdi:autorenew",
        HVACMode.FAN_ONLY: "mdi:fan",
        HVACMode.DRY: "mdi:water-percent",
    }

    # Map device fan modes to Home Assistant standard names
    _FAN_MODE_MAP = {
        "off": "off",
        "low": "low",
        "manualL": "low",
        "cycledL": "low",
        "high": "high",
        "manualH": "high",
        "cycledH": "high",
        "full auto": "auto",
    }
    _FAN_MODE_REVERSE_MAP = {
        "off": [0],
        "low": [1, 65],
        "high": [2, 66],
        "auto": [128],
    }

    def __init__(self, data: MicroAirEasyTouchBluetoothDeviceData, mac_address: str, entry_id: str) -> None:
        """Initialize the climate."""
        self._data = data
        self._mac_address = mac_address
        self._entry_id = entry_id
        self._attr_unique_id = f"microaireasytouch_{mac_address}_climate"
        self._attr_name = "EasyTouch Climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"MicroAirEasyTouch_{mac_address}")},
            name=f"EasyTouch {mac_address}",
            manufacturer="Micro-Air",
            model="Thermostat",
        )
        self._state = {}

    @property
    def icon(self) -> str:
        """Return the entity icon."""
        return self._HVAC_MODE_ICONS.get(self.hvac_mode, "mdi:thermostat")

    @property
    def entity_picture(self) -> str | None:
        """Return the entity picture."""
        if self.fan_mode:
            return f"mdi:{self._FAN_MODE_ICONS.get(self.fan_mode, 'fan')}"
        return None

    @property
    def current_fan_icon(self) -> str:
        """Return the icon to use for the current fan mode."""
        return self._FAN_MODE_ICONS.get(self.fan_mode, "mdi:fan")

    async def _async_fetch_initial_state(self) -> None:
        """Fetch the initial state from the device."""
        ble_device = get_ble_device_with_adapter(self.hass, self._mac_address, self._entry_id)
        if not ble_device:
            _LOGGER.error("Could not find BLE device: %s", self._mac_address)
            self._state = {}
            return

        # Get the BLE lock to serialize operations
        ble_lock = None
        if self._entry_id in self.hass.data.get(DOMAIN, {}):
            ble_lock = self.hass.data[DOMAIN][self._entry_id].get("ble_lock")

        message = {"Type": "Get Status", "Zone": 0, "EM": self._data._email, "TM": int(time.time())}
        try:
            # Use combined send+read to avoid double connection (much faster)
            json_payload = await self._data.send_command_and_read(
                self.hass, ble_device, message, UUIDS["jsonReturn"], ble_lock
            )
            if json_payload:
                self._state = self._data.decrypt(json_payload.decode('utf-8'))
                _LOGGER.debug("Initial state fetched: %s", self._state)
                self.async_write_ha_state()
            else:
                # Preserve last known state instead of clearing it
                if not self._state:
                    _LOGGER.warning("No payload received for initial state and no previous state available")
                else:
                    _LOGGER.warning("No payload received for initial state, preserving last known state")
        except Exception as e:
            _LOGGER.error("Failed to fetch initial state: %s", str(e))
            # Preserve last known state instead of clearing it
            if not self._state:
                _LOGGER.warning("No previous state available after error")

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._state.get("facePlateTemperature")

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        if self.hvac_mode == HVACMode.COOL:
            return self._state.get("cool_sp")
        elif self.hvac_mode == HVACMode.HEAT:
            return self._state.get("heat_sp")
        elif self.hvac_mode == HVACMode.DRY:
            return self._state.get("dry_sp")
        return None

    @property
    def target_temperature_high(self) -> float | None:
        """Return the high target temperature."""
        if self.hvac_mode == HVACMode.AUTO:
            return self._state.get("autoCool_sp")
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """Return the low target temperature."""
        if self.hvac_mode == HVACMode.AUTO:
            return self._state.get("autoHeat_sp")
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation mode."""
        # Check current_mode_num first - this is what the device is actually doing
        # mode_num is the setpoint (what mode is configured), but current_mode_num is the actual state
        current_mode_num = self._state.get("current_mode_num", 0)
        mode_num = self._state.get("mode_num", 0)
        current_mode = self._state.get("current_mode")
        off_flag = self._state.get("off")
        on_flag = self._state.get("on")
        
        # Debug logging to help diagnose power state detection
        _LOGGER.debug(
            "hvac_mode check: current_mode_num=%s, mode_num=%s, current_mode=%s, off=%s, on=%s",
            current_mode_num, mode_num, current_mode, off_flag, on_flag
        )
        
        # If current_mode_num is 0, device is actually off regardless of mode_num
        if current_mode_num == 0:
            _LOGGER.debug("Device is OFF (current_mode_num=0)")
            return HVACMode.OFF
        
        # Check if device is powered off via param flags
        if off_flag is True:
            _LOGGER.debug("Device is OFF (off flag=True)")
            return HVACMode.OFF
        
        # If 'on' flag is explicitly False, device is off
        if on_flag is False:
            _LOGGER.debug("Device is OFF (on flag=False)")
            return HVACMode.OFF
        
        # If current_mode is "off", device is off
        if current_mode == "off":
            _LOGGER.debug("Device is OFF (current_mode='off')")
            return HVACMode.OFF
        
        # Otherwise, use mode_num (the configured/set mode)
        # This represents what mode the user has selected, even if currently idle
        result = EASY_MODE_TO_HA_MODE.get(mode_num, HVACMode.OFF)
        _LOGGER.debug("Device mode: %s (from mode_num=%s)", result, mode_num)
        return result

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        current_mode = self._state.get("current_mode")
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        elif current_mode == "fan":
            return HVACAction.FAN
        elif current_mode in ["cool", "cool_on"]:
            return HVACAction.COOLING
        elif current_mode in ["heat", "heat_on"]:
            return HVACAction.HEATING
        elif current_mode == "dry":
            return HVACAction.DRYING
        elif current_mode == "auto":
            # In auto mode, determine action based on temperature
            current_temp = self.current_temperature
            low = self.target_temperature_low
            high = self.target_temperature_high
            if current_temp is not None and low is not None and high is not None:
                if current_temp < low:
                    return HVACAction.HEATING
                elif current_temp > high:
                    return HVACAction.COOLING
            return HVACAction.IDLE
        return HVACAction.IDLE

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan mode as a standard Home Assistant name."""
        if self.hvac_mode == HVACMode.FAN_ONLY:
            fan_mode_num = self._state.get("fan_mode_num", 0)
            mode = FAN_MODES_FAN_ONLY.get(fan_mode_num, "off")
        elif self.hvac_mode == HVACMode.COOL:
            fan_mode_num = self._state.get("cool_fan_mode_num", 128)
            mode = FAN_MODES_REVERSE.get(fan_mode_num, "full auto")
        elif self.hvac_mode == HVACMode.HEAT:
            fan_mode_num = self._state.get("heat_fan_mode_num", 128)
            mode = FAN_MODES_REVERSE.get(fan_mode_num, "full auto")
        elif self.hvac_mode == HVACMode.AUTO:
            fan_mode_num = self._state.get("auto_fan_mode_num", 128)
            mode = FAN_MODES_REVERSE.get(fan_mode_num, "full auto")
        else:
            mode = "full auto"
        return self._FAN_MODE_MAP.get(mode, "auto")

    @property
    def fan_modes(self) -> list[str]:
        """Return available fan modes as standard Home Assistant names."""
        if self.hvac_mode == HVACMode.FAN_ONLY:
            return ["off", "low", "high"]
        return ["off", "low", "high", "auto"]

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        ble_device = get_ble_device_with_adapter(self.hass, self._mac_address, self._entry_id)
        if not ble_device:
            _LOGGER.error("Could not find BLE device")
            return

        # Get the BLE lock to serialize operations
        ble_lock = None
        if self._entry_id in self.hass.data.get(DOMAIN, {}):
            ble_lock = self.hass.data[DOMAIN][self._entry_id].get("ble_lock")

        changes = {"zone": 0, "power": 1}
        if ATTR_TEMPERATURE in kwargs:
            temp = int(kwargs[ATTR_TEMPERATURE])
            if self.hvac_mode == HVACMode.COOL:
                changes["cool_sp"] = temp
            elif self.hvac_mode == HVACMode.HEAT:
                changes["heat_sp"] = temp
            elif self.hvac_mode == HVACMode.DRY:
                changes["dry_sp"] = temp
        elif "target_temp_high" in kwargs and "target_temp_low" in kwargs:
            changes["autoCool_sp"] = int(kwargs["target_temp_high"])
            changes["autoHeat_sp"] = int(kwargs["target_temp_low"])

        if changes:
            message = {"Type": "Change", "Changes": changes}
            success = await self._data.send_command(self.hass, ble_device, message, ble_lock)
            if success:
                # Immediately refresh state after successful command to update UI
                # Wait a moment for device to process the change
                await asyncio.sleep(1.0)
                await self._async_fetch_initial_state()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        ble_device = get_ble_device_with_adapter(self.hass, self._mac_address, self._entry_id)
        if not ble_device:
            _LOGGER.error("Could not find BLE device")
            return

        # Get the BLE lock to serialize operations
        ble_lock = None
        if self._entry_id in self.hass.data.get(DOMAIN, {}):
            ble_lock = self.hass.data[DOMAIN][self._entry_id].get("ble_lock")

        mode = HA_MODE_TO_EASY_MODE.get(hvac_mode)
        if mode is not None:
            message = {
                "Type": "Change",
                "Changes": {
                    "zone": 0,
                    "power": 0 if hvac_mode == HVACMode.OFF else 1,
                    "mode": mode,
                },
            }
            success = await self._data.send_command(self.hass, ble_device, message, ble_lock)
            if success:
                # Immediately refresh state after successful command to update UI
                # Wait a moment for device to process the change
                await asyncio.sleep(1.0)
                await self._async_fetch_initial_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new target fan mode using standard Home Assistant names."""
        ble_device = get_ble_device_with_adapter(self.hass, self._mac_address, self._entry_id)
        if not ble_device:
            _LOGGER.error("Could not find BLE device")
            return

        # Get the BLE lock to serialize operations
        ble_lock = None
        if self._entry_id in self.hass.data.get(DOMAIN, {}):
            ble_lock = self.hass.data[DOMAIN][self._entry_id].get("ble_lock")

        # Map standard name to device value
        if self.hvac_mode == HVACMode.FAN_ONLY:
            if fan_mode == "off":
                fan_value = 0
            elif fan_mode == "low":
                fan_value = 1
            elif fan_mode == "high":
                fan_value = 2
            else:
                fan_value = 0
            message = {"Type": "Change", "Changes": {"zone": 0, "fanOnly": fan_value}}
            success = await self._data.send_command(self.hass, ble_device, message, ble_lock)
            if success:
                # Immediately refresh state after successful command to update UI
                await asyncio.sleep(1.0)
                await self._async_fetch_initial_state()
        else:
            if fan_mode == "off":
                fan_value = 0
            elif fan_mode == "low":
                fan_value = 1  # manualL
            elif fan_mode == "high":
                fan_value = 2  # manualH
            elif fan_mode == "auto":
                fan_value = 128  # full auto
            else:
                fan_value = 128
            changes = {"zone": 0}
            if self.hvac_mode == HVACMode.COOL:
                changes["coolFan"] = fan_value
            elif self.hvac_mode == HVACMode.HEAT:
                changes["heatFan"] = fan_value
            elif self.hvac_mode == HVACMode.AUTO:
                changes["autoFan"] = fan_value
            message = {"Type": "Change", "Changes": changes}
            success = await self._data.send_command(self.hass, ble_device, message, ble_lock)
            if success:
                # Immediately refresh state after successful command to update UI
                await asyncio.sleep(1.0)
                await self._async_fetch_initial_state()

    async def async_update(self) -> None:
        """Update the entity state manually if needed."""
        await self._async_fetch_initial_state()
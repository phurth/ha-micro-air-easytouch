"""MicroAirEasyTouch Integration"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Final

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from bleak import BLEDevice
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback

from .micro_air_easytouch.parser import MicroAirEasyTouchBluetoothDeviceData
from .const import DOMAIN

PLATFORMS: Final = [Platform.BUTTON, Platform.CLIMATE]
_LOGGER = logging.getLogger(__name__)


def get_ble_device_with_adapter(
    hass: HomeAssistant, address: str, entry_id: str | None = None
) -> BLEDevice | None:
    """
    Get BLE device reference, preferring the adapter used during initial setup.
    
    This helps prevent disconnections when multiple Bluetooth adapters are available
    by ensuring we use the same adapter that was used during setup.
    
    Args:
        hass: Home Assistant instance
        address: MAC address of the device
        entry_id: Config entry ID to look up stored adapter preference
        
    Returns:
        BLEDevice if found, None otherwise
    """
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    
    if not ble_device:
        return None
    
    # If we have an entry_id, check if we should prefer a specific adapter
    if entry_id and entry_id in hass.data.get(DOMAIN, {}):
        preferred_adapter = hass.data[DOMAIN][entry_id].get("adapter_source")
        if preferred_adapter:
            device_adapter = ble_device.details.get("source")
            if device_adapter != preferred_adapter:
                _LOGGER.warning(
                    "Device %s found on adapter %s, but preferred adapter is %s. "
                    "This may cause connection issues. Using current adapter.",
                    address,
                    device_adapter,
                    preferred_adapter,
                )
            else:
                _LOGGER.debug(
                    "Device %s found on preferred adapter: %s",
                    address,
                    device_adapter,
                )
        else:
            # Store the adapter if we haven't stored it yet
            device_adapter = ble_device.details.get("source")
            hass.data[DOMAIN][entry_id]["adapter_source"] = device_adapter
            _LOGGER.info(
                "Storing adapter %s for device %s (entry_id: %s)",
                device_adapter,
                address,
                entry_id,
            )
    else:
        # Log which adapter we're using
        device_adapter = ble_device.details.get("source")
        _LOGGER.debug(
            "Device %s found on adapter: %s",
            address,
            device_adapter,
        )
    
    return ble_device

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MicroAirEasyTouch from a config entry."""
    address = entry.unique_id
    assert address is not None
    password = entry.data.get(CONF_PASSWORD)
    email = entry.data.get(CONF_USERNAME)
    data = MicroAirEasyTouchBluetoothDeviceData(password=password, email=email)

    # Get initial BLE device to determine which adapter to use
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    adapter_source = None
    if ble_device:
        adapter_source = ble_device.details.get("source")
        _LOGGER.info(
            "MicroAirEasyTouch %s will use adapter: %s",
            address,
            adapter_source,
        )
    else:
        _LOGGER.warning(
            "MicroAirEasyTouch %s not found during setup, adapter will be determined on first connection",
            address,
        )

    # Create a per-device lock to serialize BLE operations
    # This prevents concurrent operations when multiple entities (zones) access the same device
    ble_lock = asyncio.Lock()
    
    # Track last update time to debounce advertisement-triggered updates
    last_advertisement_update = {"time": 0.0}
    
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "data": data,
        "adapter_source": adapter_source,
        "ble_lock": ble_lock,
        "last_advertisement_update": last_advertisement_update,
    }
    
    _LOGGER.debug(
        "Created BLE operation lock for device %s (entry_id: %s)",
        address,
        entry.entry_id,
    )

    @callback
    def _handle_bluetooth_update(service_info: BluetoothServiceInfoBleak) -> None:
        """Update device info from advertisements and trigger state update."""
        if service_info.address == address:
            _LOGGER.debug("Received BLE advertisement from %s", address)
            data._start_update(service_info)
            
            # Debounce: Only trigger update if last update was > 5 seconds ago
            # This prevents spamming updates while still being responsive
            current_time = time.time()
            if current_time - last_advertisement_update["time"] > 5.0:
                last_advertisement_update["time"] = current_time
                # Schedule async state update for all entities using this device
                # This provides faster updates than polling alone
                hass.async_create_task(_trigger_entity_updates(entry.entry_id))
    
    async def _trigger_entity_updates(entry_id: str) -> None:
        """Trigger state updates for all entities associated with this entry."""
        try:
            # Get stored entity references and trigger their updates
            entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})
            entities = entry_data.get("entities", [])
            for entity in entities:
                try:
                    # Schedule async update for the entity
                    if hasattr(entity, "async_update"):
                        await entity.async_update()
                except Exception as e:
                    _LOGGER.debug("Error updating entity: %s", str(e))
        except Exception as e:
            _LOGGER.debug("Error triggering entity updates: %s", str(e))

    hass.bus.async_listen("bluetooth_service_info", _handle_bluetooth_update)

    # Register services (import here to avoid circular import)
    from .services import async_register_services
    await async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        # Unregister services (import here to avoid circular import)
        from .services import async_unregister_services
        await async_unregister_services(hass)
    return unload_ok
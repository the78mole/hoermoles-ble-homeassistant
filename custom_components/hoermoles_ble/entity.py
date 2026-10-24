"""Base entity for Hoermoles BLE."""

from __future__ import annotations

from homeassistant.components.bluetooth.passive_update_coordinator import PassiveBluetoothCoordinatorEntity
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityDescription

from .const import CONF_PRODUCT_NAME, CONF_SERIAL_NO, DOMAIN
from .coordinator import HoermolesCoordinator


class HoermolesEntity(PassiveBluetoothCoordinatorEntity[HoermolesCoordinator]):
    """An entity of one drive; state comes from the advertisement coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HoermolesCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address = coordinator.address
        data = coordinator.entry.data
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="Hörmann",
            model=data.get(CONF_PRODUCT_NAME),
            serial_number=data.get(CONF_SERIAL_NO),
            name=coordinator.entry.title,
        )

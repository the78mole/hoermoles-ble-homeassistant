"""Hoermann BlueSecur garage door drives via the hoermoles-ble library."""

from __future__ import annotations

from dataclasses import dataclass

from hoermoles_ble import Credentials
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_QR_PREFIX, CONF_ROOT_ID, CONF_ROOT_KEY
from .controller import DriveController
from .coordinator import HoermolesCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.COVER, Platform.SENSOR]


@dataclass
class HoermolesData:
    coordinator: HoermolesCoordinator
    # None for a sensors-only entry (no key imported yet)
    controller: DriveController | None


type HoermolesConfigEntry = ConfigEntry[HoermolesData]


def credentials_from_entry(entry: ConfigEntry) -> Credentials | None:
    if CONF_ROOT_KEY not in entry.data:
        return None
    assert entry.unique_id is not None
    return Credentials(
        device_address=entry.unique_id,
        root_id=entry.data[CONF_ROOT_ID],
        root_key=bytes.fromhex(entry.data[CONF_ROOT_KEY]),
        qr_prefix=entry.data.get(CONF_QR_PREFIX, ""),
    )


async def async_setup_entry(hass: HomeAssistant, entry: HoermolesConfigEntry) -> bool:
    coordinator = HoermolesCoordinator(hass, entry)
    credentials = credentials_from_entry(entry)
    controller = DriveController(hass, credentials, entry.title) if credentials else None
    entry.runtime_data = HoermolesData(coordinator=coordinator, controller=controller)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Start listening only once the entities exist, so none misses the first update.
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HoermolesConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

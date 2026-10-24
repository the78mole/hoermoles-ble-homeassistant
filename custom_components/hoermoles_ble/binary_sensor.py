"""Binary sensors from the drive's advertisement.

Except for `moving` and `maintenance_required`, these come from the short
status packet, which an idle drive rarely sends - they can stay unknown for a
long time after a restart and show the last value seen afterwards.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HoermolesConfigEntry
from .coordinator import HoermolesCoordinator
from .entity import HoermolesEntity


@dataclass(frozen=True, kw_only=True)
class HoermolesBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[HoermolesCoordinator], bool | None]


def _flag(name: str) -> Callable[[HoermolesCoordinator], bool | None]:
    return lambda coordinator: getattr(coordinator.info, name) if coordinator.info else None


BINARY_SENSORS: tuple[HoermolesBinarySensorDescription, ...] = (
    HoermolesBinarySensorDescription(
        key="moving",
        translation_key="moving",
        device_class=BinarySensorDeviceClass.MOVING,
        value_fn=lambda coordinator: coordinator.moving,
    ),
    HoermolesBinarySensorDescription(
        key="low_battery",
        device_class=BinarySensorDeviceClass.BATTERY,
        value_fn=_flag("low_battery"),
    ),
    HoermolesBinarySensorDescription(
        key="maintenance_required",
        translation_key="maintenance_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_flag("maintenance_required"),
    ),
    HoermolesBinarySensorDescription(
        key="protection_active",
        translation_key="protection_active",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_flag("protection_active"),
    ),
    HoermolesBinarySensorDescription(
        key="vacation_mode",
        translation_key="vacation_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_flag("vacation_mode"),
    ),
    HoermolesBinarySensorDescription(
        key="emergency_mode",
        translation_key="emergency_mode",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_flag("emergency_mode"),
    ),
    HoermolesBinarySensorDescription(
        key="warning_time",
        translation_key="warning_time",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=_flag("warning_time"),
    ),
    HoermolesBinarySensorDescription(
        key="admins_can_be_teached",
        translation_key="admins_can_be_teached",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_flag("admins_can_be_teached"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: HoermolesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(HoermolesBinarySensor(coordinator, description) for description in BINARY_SENSORS)


class HoermolesBinarySensor(HoermolesEntity, BinarySensorEntity):
    entity_description: HoermolesBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator)

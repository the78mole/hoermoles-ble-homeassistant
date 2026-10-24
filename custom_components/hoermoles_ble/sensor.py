"""Sensors from the drive's advertisement."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HoermolesConfigEntry
from .coordinator import HoermolesCoordinator
from .entity import HoermolesEntity


@dataclass(frozen=True, kw_only=True)
class HoermolesSensorDescription(SensorEntityDescription):
    value_fn: Callable[[HoermolesCoordinator], float | int | None]


SENSORS: tuple[HoermolesSensorDescription, ...] = (
    HoermolesSensorDescription(
        key="position",
        translation_key="position",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda coordinator: coordinator.position,
    ),
    HoermolesSensorDescription(
        key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda coordinator: coordinator.info.rssi if coordinator.info else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: HoermolesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(HoermolesSensor(coordinator, description) for description in SENSORS)


class HoermolesSensor(HoermolesEntity, SensorEntity):
    entity_description: HoermolesSensorDescription

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self.coordinator)

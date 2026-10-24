"""Channel buttons - only for drives with an imported key."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HoermolesConfigEntry
from .const import CHANNEL_IMPULSE, CHANNEL_LIGHT, CHANNEL_PARTIAL, CHANNEL_VENTILATION
from .controller import DriveController
from .coordinator import HoermolesCoordinator
from .entity import HoermolesEntity


@dataclass(frozen=True, kw_only=True)
class HoermolesButtonDescription(ButtonEntityDescription):
    channel: int


BUTTONS: tuple[HoermolesButtonDescription, ...] = (
    HoermolesButtonDescription(key="impulse", translation_key="impulse", channel=CHANNEL_IMPULSE),
    HoermolesButtonDescription(key="partial", translation_key="partial", channel=CHANNEL_PARTIAL),
    # Verified, but not every drive model has a ventilation position.
    HoermolesButtonDescription(
        key="ventilation",
        translation_key="ventilation",
        channel=CHANNEL_VENTILATION,
        entity_registry_enabled_default=False,
    ),
    # Not verified against real hardware - the name says so.
    HoermolesButtonDescription(
        key="light",
        translation_key="light",
        channel=CHANNEL_LIGHT,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: HoermolesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    if data.controller is None:
        return
    controller = data.controller
    async_add_entities(HoermolesButton(data.coordinator, controller, description) for description in BUTTONS)


class HoermolesButton(HoermolesEntity, ButtonEntity):
    entity_description: HoermolesButtonDescription

    def __init__(
        self, coordinator: HoermolesCoordinator, controller: DriveController, description: HoermolesButtonDescription
    ) -> None:
        super().__init__(coordinator, description)
        self._controller = controller

    async def async_press(self) -> None:
        await self._controller.async_send_channel(self.entity_description.channel)

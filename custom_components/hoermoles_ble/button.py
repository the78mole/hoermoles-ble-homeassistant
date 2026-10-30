"""Channel buttons - only for drives with an imported key.

The channels follow the official app's defaults table
(Hoermann.BleApp.Core.Services.ChannelDefaultsService): 2 light, 3 partial,
6 ventilation position on the whole Supramatic/Rollmatic/SilentDrive family,
except that the plain Supramatic 4 (product class 2, id 1) has no channel 6.
A channel's category in the app is only a label stored in the app; the drive
acts on the fixed channel number.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HoermolesConfigEntry
from .const import (
    CHANNEL_IMPULSE,
    CHANNEL_LIGHT,
    CHANNEL_PARTIAL,
    CHANNEL_VENTILATION,
    CONF_PRODUCT_CLASS,
    CONF_PRODUCT_ID,
)
from .controller import DriveController
from .coordinator import HoermolesCoordinator
from .entity import HoermolesEntity


@dataclass(frozen=True, kw_only=True)
class HoermolesButtonDescription(ButtonEntityDescription):
    channel: int
    # (product class, product id) of the drives that lack this channel
    missing_on: frozenset[tuple[int, int]] = frozenset()


BUTTONS: tuple[HoermolesButtonDescription, ...] = (
    HoermolesButtonDescription(key="impulse", translation_key="impulse", channel=CHANNEL_IMPULSE),
    HoermolesButtonDescription(key="partial", translation_key="partial", channel=CHANNEL_PARTIAL),
    HoermolesButtonDescription(
        key="ventilation",
        translation_key="ventilation",
        channel=CHANNEL_VENTILATION,
        missing_on=frozenset({(2, 1)}),  # plain Supramatic 4
    ),
    # In the app's defaults table, but not yet tried on real hardware - the name says so.
    HoermolesButtonDescription(key="light", translation_key="light", channel=CHANNEL_LIGHT),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: HoermolesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    if data.controller is None:
        return
    controller = data.controller
    # Unknown product (no product data yet): offer every channel.
    product = (entry.data.get(CONF_PRODUCT_CLASS), entry.data.get(CONF_PRODUCT_ID))
    async_add_entities(
        HoermolesButton(data.coordinator, controller, description)
        for description in BUTTONS
        if product not in description.missing_on
    )


class HoermolesButton(HoermolesEntity, ButtonEntity):
    entity_description: HoermolesButtonDescription

    def __init__(
        self, coordinator: HoermolesCoordinator, controller: DriveController, description: HoermolesButtonDescription
    ) -> None:
        super().__init__(coordinator, description)
        self._controller = controller

    async def async_press(self) -> None:
        await self._controller.async_send_channel(self.entity_description.channel)

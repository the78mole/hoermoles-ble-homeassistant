"""The garage door as a cover - only for drives with an imported key.

Only channel 1 ("impulse") is verified against real hardware, and impulse is a
toggle (open / stop / close), not a direction. So by default open, close and
stop all send an impulse, and the advertised position suppresses the ones that
would do the opposite of what was asked. The direction channels 4/5 are
reverse-engineered but unverified and only used when enabled in the options.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityDescription,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HoermolesConfigEntry
from .const import CHANNEL_CLOSE, CHANNEL_IMPULSE, CHANNEL_OPEN, CONF_DISCRETE_OPEN_CLOSE
from .controller import DriveController
from .coordinator import HoermolesCoordinator
from .entity import HoermolesEntity

DESCRIPTION = CoverEntityDescription(key="door", name=None, device_class=CoverDeviceClass.GARAGE)


async def async_setup_entry(
    hass: HomeAssistant, entry: HoermolesConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = entry.runtime_data
    if data.controller is None:
        return
    async_add_entities(
        [HoermolesCover(data.coordinator, data.controller, entry.options.get(CONF_DISCRETE_OPEN_CLOSE, False))]
    )


class HoermolesCover(HoermolesEntity, CoverEntity):
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(self, coordinator: HoermolesCoordinator, controller: DriveController, discrete: bool) -> None:
        super().__init__(coordinator, DESCRIPTION)
        self._controller = controller
        self._discrete = discrete

    @property
    def current_cover_position(self) -> int | None:
        position = self.coordinator.position
        return None if position is None else round(position)

    @property
    def is_closed(self) -> bool | None:
        position = self.coordinator.position
        return None if position is None else position == 0

    @property
    def is_opening(self) -> bool:
        return bool(self.coordinator.moving) and self.coordinator.direction > 0

    @property
    def is_closing(self) -> bool:
        return bool(self.coordinator.moving) and self.coordinator.direction < 0

    def _at(self, position: float) -> bool:
        return self.coordinator.position == position and not self.coordinator.moving

    async def async_open_cover(self, **kwargs: Any) -> None:
        if self._discrete:
            await self._controller.async_send_channel(CHANNEL_OPEN)
        elif not self._at(100) and not self.is_opening:
            await self._controller.async_send_channel(CHANNEL_IMPULSE)

    async def async_close_cover(self, **kwargs: Any) -> None:
        if self._discrete:
            await self._controller.async_send_channel(CHANNEL_CLOSE)
        elif not self._at(0) and not self.is_closing:
            await self._controller.async_send_channel(CHANNEL_IMPULSE)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        # An impulse stops a moving door; on a resting one it would start it.
        if self.coordinator.moving:
            await self._controller.async_send_channel(CHANNEL_IMPULSE)

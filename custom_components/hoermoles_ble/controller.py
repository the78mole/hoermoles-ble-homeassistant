"""Control plane: one short, signed GATT exchange per command.

The drive tears the link down ~100-150 ms after the first chunk of a command,
so a connection is never held: connect, wait for the challenge, send, leave.
The per-drive lock keeps two button presses from colliding on one drive.
"""

from __future__ import annotations

import asyncio
import logging

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from hoermoles_ble import Credentials, HoermannClient
from hoermoles_ble.ble_transport import BleakTransport
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CONTROL_TIMEOUT, DOMAIN

_LOGGER = logging.getLogger(__name__)


class DriveController:
    """Sends channel commands to one drive."""

    def __init__(self, hass: HomeAssistant, credentials: Credentials, name: str) -> None:
        self._hass = hass
        self._credentials = credentials
        self._name = name
        self._lock = asyncio.Lock()

    @property
    def address(self) -> str:
        return self._credentials.device_address

    async def async_send_channel(self, channel: int) -> None:
        async with self._lock:
            ble_device = bluetooth.async_ble_device_from_address(self._hass, self.address, connectable=True)
            if ble_device is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="no_connectable_path",
                    translation_placeholders={"address": self.address},
                )
            try:
                async with asyncio.timeout(CONTROL_TIMEOUT):
                    await self._send(ble_device, channel)
            except (BleakError, TimeoutError) as exc:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={"address": self.address, "error": str(exc) or type(exc).__name__},
                ) from exc

    async def _send(self, ble_device, channel: int) -> None:
        client = await establish_connection(BleakClientWithServiceCache, ble_device, self._name, max_attempts=3)
        transport = BleakTransport.from_client(client, on_log=_LOGGER.debug)
        async with HoermannClient(transport, on_log=_LOGGER.debug) as hoermann:
            try:
                await hoermann.wait_for_any_notification(timeout=10.0)
            except TimeoutError:
                _LOGGER.debug("No initial challenge from %s, trying anyway", self.address)
            await hoermann.open_channel(self._credentials, channel=channel)
            # Writes are without response: give the stack a moment to flush the
            # last chunk before our own disconnect could cut it off. (The drive
            # usually drops the link itself within this time.)
            await asyncio.sleep(0.5)

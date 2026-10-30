"""Control plane: one short, signed GATT exchange per command.

The drive tears the link down ~100-150 ms after the first chunk of a command,
so a connection is never held: connect, wait for the challenge, send, leave.
The per-drive lock keeps two button presses from colliding on one drive.
Registration with the QR label is the same kind of exchange, just longer.
"""

from __future__ import annotations

import asyncio
import logging

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from hoermoles_ble import Credentials, HoermannClient
from hoermoles_ble.ble_transport import BleakTransport
from hoermoles_ble.transport import BleTransport
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CONTROL_TIMEOUT, DOMAIN, REGISTER_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def open_transport(ble_device: BLEDevice, name: str) -> BleTransport:
    client = await establish_connection(BleakClientWithServiceCache, ble_device, name, max_attempts=3)
    return BleakTransport.from_client(client, on_log=_LOGGER.debug)


async def async_register(ble_device: BLEDevice, name: str, qr_text: str) -> Credentials:
    """Register as a new admin with the drive's QR label. Only works while the
    drive is in teach mode (menu 19 / parameter 02); outside it the drive stays
    silent, which ends in TimeoutError after REGISTER_TIMEOUT."""
    transport = await open_transport(ble_device, name)
    async with HoermannClient(transport, on_log=_LOGGER.debug) as hoermann:
        try:
            await hoermann.wait_for_any_notification(timeout=10.0)
        except TimeoutError:
            _LOGGER.debug("No initial challenge from %s, trying anyway", ble_device.address)
        async with asyncio.timeout(REGISTER_TIMEOUT):
            return await hoermann.register(qr_text, ble_device.address, timeout=REGISTER_TIMEOUT)


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
        transport = await open_transport(ble_device, self._name)
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

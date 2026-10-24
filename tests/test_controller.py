"""Control plane against a fake GATT client - no real Bluetooth."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from bleak.exc import BleakError
from hoermoles_ble import BC_TX, Credentials
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.hoermoles_ble.controller import DriveController

from .conftest import ADDRESS, ROOT_KEY

# A Signed ENABLED-style notification is not needed: without one the client
# signs with a zero challenge, which is all this test needs to see the frame go out.


class FakeClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.mtu_size = 23
        self.writes: list[bytes] = []
        self.disconnected = False

    async def write_gatt_char(self, char: str, data: bytes, response: bool = False) -> None:
        assert char == BC_TX
        assert response is False
        self.writes.append(bytes(data))

    async def start_notify(self, char, callback) -> None:
        pass

    async def stop_notify(self, char) -> None:
        pass

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def controller(hass: HomeAssistant) -> DriveController:
    return DriveController(hass, Credentials(device_address=ADDRESS, root_id=7, root_key=ROOT_KEY), "Garage")


@pytest.fixture(autouse=True)
def fast():
    """Skip the waits for a challenge and for the flush."""
    real_sleep = asyncio.sleep

    async def no_sleep(delay, *args):
        await real_sleep(0)

    with (
        patch("hoermoles_ble.client.HoermannClient.wait_for_any_notification", side_effect=TimeoutError),
        patch("custom_components.hoermoles_ble.controller.asyncio.sleep", no_sleep),
    ):
        yield


async def test_sends_chunked_frame_and_disconnects(hass: HomeAssistant, controller: DriveController) -> None:
    client = FakeClient()
    with (
        patch(
            "custom_components.hoermoles_ble.controller.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ),
        patch("custom_components.hoermoles_ble.controller.establish_connection", return_value=client) as connect,
    ):
        await controller.async_send_channel(1)
    connect.assert_awaited_once()
    assert len(client.writes) >= 2
    assert all(len(chunk) <= 20 for chunk in client.writes)
    assert client.disconnected


async def test_no_connectable_path(hass: HomeAssistant, controller: DriveController) -> None:
    with (
        patch("custom_components.hoermoles_ble.controller.bluetooth.async_ble_device_from_address", return_value=None),
        pytest.raises(HomeAssistantError) as err,
    ):
        await controller.async_send_channel(1)
    assert err.value.translation_key == "no_connectable_path"


async def test_connection_failure(hass: HomeAssistant, controller: DriveController) -> None:
    with (
        patch(
            "custom_components.hoermoles_ble.controller.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.hoermoles_ble.controller.establish_connection", side_effect=BleakError("out of range")
        ),
        pytest.raises(HomeAssistantError) as err,
    ):
        await controller.async_send_channel(1)
    assert err.value.translation_key == "command_failed"
    assert err.value.translation_placeholders["error"] == "out of range"

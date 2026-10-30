"""Registration with the drive's QR label, against hoerfrau's simulated drive.

Only the BLE connection is swapped out: open_transport hands the real
HoermannClient a loopback transport into the simulator, so the registration
runs the actual protocol against an independent implementation of the drive.
"""

from __future__ import annotations

import base64
import time
from unittest.mock import patch

import pytest
from bleak.exc import BleakError
from hoerfrau.drive import DriveState, SimulatedDrive
from hoerfrau.loopback import LoopbackTransport
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hoermoles_ble.config_flow import CONF_QR_LABEL
from custom_components.hoermoles_ble.const import (
    CONF_PRODUCT_CLASS,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_NAME,
    CONF_QR_PREFIX,
    CONF_ROOT_ID,
    CONF_ROOT_KEY,
    CONF_SERIAL_NO,
    DOMAIN,
)

from .conftest import ADDRESS, ROOT_KEY, SERIAL, inject, long_packet


@pytest.fixture(scope="module")
def state_template() -> DriveState:
    # RSA key generation is the slow part - once per module, 1024 bit.
    return DriveState.create(serial_no=SERIAL, key_size=1024)


@pytest.fixture
def drive(state_template: DriveState, tmp_path) -> SimulatedDrive:
    return SimulatedDrive(DriveState.from_json(state_template.to_json()), state_path=tmp_path / "drive.json")


@pytest.fixture
def loopback(drive: SimulatedDrive):
    """Every BLE connection of the integration goes into the simulator."""

    async def open_transport(ble_device, name):
        return LoopbackTransport(drive)

    with patch("custom_components.hoermoles_ble.controller.open_transport", side_effect=open_transport) as mock:
        yield mock


@pytest.fixture(autouse=True)
def fast_register_timeout():
    with patch("custom_components.hoermoles_ble.controller.REGISTER_TIMEOUT", 0.5):
        yield


async def _register_form(hass: HomeAssistant, payload: bytes) -> dict:
    """Discovery through the manifest matchers -> confirm -> menu -> the register form."""
    assert await async_setup_component(hass, "bluetooth", {})
    inject(hass, payload)
    # Discovery flows start as background tasks.
    await hass.async_block_till_done(wait_background_tasks=True)
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == SOURCE_BLUETOOTH
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    assert "register" in result["menu_options"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "register"
    return result


async def _submit(hass: HomeAssistant, result: dict, label: str) -> dict:
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_QR_LABEL: label})


async def test_register_and_open_the_door(hass: HomeAssistant, drive: SimulatedDrive, loopback) -> None:
    # The long packet tells the serial, which matches the label.
    result = await _register_form(hass, long_packet())
    assert result["errors"] == {}

    result = await _submit(hass, result, drive.qr_text)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    admin = next(iter(drive.state.admins.values()))
    assert result["data"] == {
        CONF_ROOT_ID: admin.root_id,
        CONF_ROOT_KEY: admin.root_key.hex(),
        CONF_QR_PREFIX: drive.qr_prefix,
        CONF_PRODUCT_CLASS: 2,
        CONF_PRODUCT_ID: 2,
        CONF_PRODUCT_NAME: "Supramatic Serie 4",
        CONF_SERIAL_NO: str(SERIAL),
    }
    assert not drive.state.teachable
    await hass.async_block_till_done()

    # The stored key drives the door through the cover entity.
    inject(hass, long_packet())
    await hass.async_block_till_done()
    cover = er.async_get(hass).async_get_entity_id("cover", DOMAIN, f"{ADDRESS}_door")
    assert not drive.moving
    await hass.services.async_call("cover", "open_cover", {"entity_id": cover}, blocking=True)
    assert drive.moving


async def test_register_wrong_drive(hass: HomeAssistant, drive: SimulatedDrive, loopback) -> None:
    drive.state.serial_no = SERIAL + 1
    result = await _register_form(hass, long_packet())
    result = await _submit(hass, result, drive.qr_text)
    assert result["errors"] == {"base": "wrong_device"}
    loopback.assert_not_called()


@pytest.mark.parametrize(
    "label",
    [
        "nonsense",
        "0302020000030262602641451030700",  # prefix without a key
        "0302020000030262602641451030700" + base64.b64encode(b"not a key").decode(),
        "030202000003026260264145103070" + "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQ",  # 30 digits, truncated key
    ],
)
async def test_register_malformed_label(hass: HomeAssistant, label: str, loopback) -> None:
    result = await _register_form(hass, long_packet())
    result = await _submit(hass, result, label)
    assert result["errors"] == {CONF_QR_LABEL: "invalid_qr_label"}
    loopback.assert_not_called()


async def test_register_short_prefix_with_valid_key(hass: HomeAssistant, drive: SimulatedDrive, loopback) -> None:
    result = await _register_form(hass, long_packet())
    result = await _submit(hass, result, drive.qr_text[1:])
    assert result["errors"] == {CONF_QR_LABEL: "invalid_qr_label"}


async def test_reconfigure_not_in_teach_mode_then_reset(
    hass: HomeAssistant, drive: SimulatedDrive, entry_with_key: MockConfigEntry, loopback
) -> None:
    drive.state.teachable = False
    assert await hass.config_entries.async_setup(entry_with_key.entry_id)
    for packet in drive.manufacturer_packets():
        inject(hass, packet)
    await hass.async_block_till_done()

    result = await entry_with_key.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
    # The running entry's advertisement says teach mode is off: warned, not blocked.
    assert result["errors"] == {"base": "teach_mode_off"}

    started = time.monotonic()
    result = await _submit(hass, result, drive.qr_text)
    assert result["errors"] == {"base": "not_in_teach_mode"}
    assert time.monotonic() - started < 5  # not the library's hardcoded 20 s

    drive.reset_admins()  # menu 19/02
    result = await _submit(hass, result, drive.qr_text)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry_with_key.data[CONF_ROOT_KEY] != ROOT_KEY.hex()
    assert entry_with_key.data[CONF_ROOT_KEY] == drive.state.admins[entry_with_key.data[CONF_ROOT_ID]].root_key.hex()
    await hass.async_block_till_done()


async def test_reconfigure_checks_the_stored_serial(
    hass: HomeAssistant, drive: SimulatedDrive, entry_with_key: MockConfigEntry, loopback
) -> None:
    # Entry not loaded, so no live advertisement: the serial stored in the entry decides.
    entry_with_key.runtime_data = None
    drive.state.serial_no = SERIAL + 1
    result = await entry_with_key.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "register"})
    assert result["errors"] == {}
    result = await _submit(hass, result, drive.qr_text)
    assert result["errors"] == {"base": "wrong_device"}
    loopback.assert_not_called()


async def test_register_without_connectable_path(hass: HomeAssistant, drive: SimulatedDrive, loopback) -> None:
    result = await _register_form(hass, long_packet())
    with patch("custom_components.hoermoles_ble.config_flow.async_ble_device_from_address", return_value=None):
        result = await _submit(hass, result, drive.qr_text)
    assert result["errors"] == {"base": "no_connectable_path"}


async def test_register_connection_fails(hass: HomeAssistant, drive: SimulatedDrive) -> None:
    result = await _register_form(hass, long_packet())
    with patch("custom_components.hoermoles_ble.controller.open_transport", side_effect=BleakError("gone")):
        result = await _submit(hass, result, drive.qr_text)
    assert result["errors"] == {"base": "cannot_connect"}

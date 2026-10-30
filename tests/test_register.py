"""Registration with the drive's QR label, against hoerfrau's simulated drive.

Only the BLE connection is swapped out: open_transport hands the real
HoermannClient a loopback transport into the simulator, so the registration
runs the actual protocol against an independent implementation of the drive.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from dbus_fast import BusType, Message, MessageType
from hoerfrau.drive import Admin, DriveState, SimulatedDrive
from hoerfrau.loopback import LoopbackTransport
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

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
from custom_components.hoermoles_ble.controller import open_transport
from custom_components.hoermoles_ble.simulator import (
    DEBUG_ADVERTISEMENT_CHAR,
    POLL_INTERVAL_IDLE,
    POLL_INTERVAL_MOVING,
    async_prefer_le,
    is_simulator,
)

from .conftest import ADDRESS, ROOT_KEY, SERIAL, inject, long_packet, service_info

SIM_NAME = "HFSIM0307"


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


class DebugClient:
    """The simulator's debug characteristic, as hoerfrau's macOS peripheral serves it."""

    def __init__(self, drive: SimulatedDrive) -> None:
        self._drive = drive

    async def read_gatt_char(self, uuid: str) -> bytes:
        assert uuid == DEBUG_ADVERTISEMENT_CHAR
        short, long = self._drive.manufacturer_packets()
        doc = {"company_id": 1972, "packets": [short.hex(), long.hex()], "serial_no": self._drive.state.serial_no}
        return json.dumps(doc, separators=(",", ":")).encode()

    async def disconnect(self) -> None:
        pass


@pytest.fixture
def debug_characteristic(drive: SimulatedDrive):
    with patch(
        "custom_components.hoermoles_ble.simulator.establish_connection", return_value=DebugClient(drive)
    ) as mock:
        yield mock


async def _register_form(hass: HomeAssistant, payload: bytes | None = None, name: str = "Hoermann") -> dict:
    """Discovery through the manifest matchers -> confirm -> menu -> the register form."""
    assert await async_setup_component(hass, "bluetooth", {})
    inject(hass, payload, name=name)
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


# --- the simulator (HFSIM…, no manufacturer data) ------------------------------


async def test_simulator_registration(
    hass: HomeAssistant, drive: SimulatedDrive, loopback, debug_characteristic
) -> None:
    drive.state.teachable = False
    result = await _register_form(hass, None, name=SIM_NAME)
    # Serial and teach mode come from the debug characteristic.
    assert result["errors"] == {"base": "teach_mode_off"}

    drive.reset_admins()
    result = await _submit(hass, result, drive.qr_text)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL_NO] == str(SERIAL)
    await hass.async_block_till_done()


async def test_simulator_serial_mismatch(
    hass: HomeAssistant, drive: SimulatedDrive, loopback, debug_characteristic
) -> None:
    result = await _register_form(hass, None, name=SIM_NAME)
    label = drive.qr_text
    drive.state.serial_no = SERIAL + 1  # the simulator now advertises another serial
    result = await _submit(hass, result, label)
    assert result["errors"] == {"base": "wrong_device"}


async def test_simulator_debug_read_fails(hass: HomeAssistant, loopback) -> None:
    with patch("custom_components.hoermoles_ble.simulator.establish_connection", side_effect=BleakError("busy")):
        result = await _register_form(hass, None, name=SIM_NAME)
    assert result["errors"] == {}


async def test_user_step_lists_simulator(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.hoermoles_ble.config_flow.async_discovered_service_info",
        return_value=[service_info(None, name=SIM_NAME), service_info(None, "11:22:33:44:55:66", name="Phone")],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert list(result["data_schema"].schema[CONF_ADDRESS].container) == [ADDRESS]


async def _tick(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_simulator_sensors_are_polled(
    hass: HomeAssistant, drive: SimulatedDrive, entry_with_key: MockConfigEntry, loopback, debug_characteristic
) -> None:
    drive.state.admins[7] = Admin(7, ROOT_KEY, "ha")  # the key in entry_with_key
    assert await hass.config_entries.async_setup(entry_with_key.entry_id)
    inject(hass, None, name=SIM_NAME)  # first simulator advertisement starts the polling
    await _tick(hass, 1)
    assert debug_characteristic.call_count == 1
    assert hass.states.get("sensor.garage_position").state == "0.0"
    assert hass.states.get("binary_sensor.garage_moving").state == "off"

    # The door starts moving on the drive: seen at the next idle read (30 s) ...
    drive.impulse()
    drive.tick(3)
    await _tick(hass, POLL_INTERVAL_IDLE + 1)
    assert debug_characteristic.call_count == 2
    assert hass.states.get("sensor.garage_position").state == "20.0"
    assert hass.states.get("binary_sensor.garage_moving").state == "on"
    # ... and polled faster while it moves.
    await _tick(hass, POLL_INTERVAL_IDLE + 1 + POLL_INTERVAL_MOVING + 1)
    assert debug_characteristic.call_count == 3

    # A command asks for an early read.
    await hass.services.async_call("button", "press", {"entity_id": "button.garage_impulse"}, blocking=True)
    assert not drive.moving  # the impulse stopped it
    await _tick(hass, POLL_INTERVAL_IDLE + POLL_INTERVAL_MOVING + 4)
    assert debug_characteristic.call_count == 4
    assert hass.states.get("binary_sensor.garage_moving").state == "off"
    assert await hass.config_entries.async_unload(entry_with_key.entry_id)


async def test_real_drive_is_never_polled(
    hass: HomeAssistant, drive: SimulatedDrive, entry_with_key: MockConfigEntry
) -> None:
    assert await hass.config_entries.async_setup(entry_with_key.entry_id)
    with patch("custom_components.hoermoles_ble.simulator.establish_connection") as connect:
        for packet in drive.manufacturer_packets():
            inject(hass, packet)
        await _tick(hass, POLL_INTERVAL_IDLE * 3)
    connect.assert_not_called()
    assert hass.states.get("sensor.garage_position").state == "0.0"
    assert await hass.config_entries.async_unload(entry_with_key.entry_id)


class FakeBus:
    """dbus_fast's MessageBus, answering every call with `reply_type`."""

    reply_type = MessageType.METHOD_RETURN
    calls: list[Message]

    def __init__(self, bus_type) -> None:
        assert bus_type is BusType.SYSTEM
        FakeBus.calls = []

    async def connect(self) -> FakeBus:
        return self

    async def call(self, message: Message) -> Message:
        FakeBus.calls.append(message)
        message.serial = len(FakeBus.calls)  # the real bus numbers its messages
        if self.reply_type is MessageType.ERROR:
            return Message.new_error(message, "org.freedesktop.DBus.Error.InvalidArgs", "No such property")
        return Message.new_method_return(message)

    def disconnect(self) -> None:
        pass


SIM_PATH = "/org/bluez/hci0/dev_9C_58_84_82_58_20"


async def test_prefer_le_pins_the_bearer() -> None:
    with patch("dbus_fast.aio.MessageBus", FakeBus):
        await async_prefer_le(BLEDevice("9C:58:84:82:58:20", SIM_NAME, {"path": SIM_PATH, "props": {}}))
    (message,) = FakeBus.calls
    assert (message.destination, message.path, message.member) == ("org.bluez", SIM_PATH, "Set")
    assert message.body[:2] == ["org.bluez.Device1", "PreferredBearer"]
    assert message.body[2].value == "le"


async def test_prefer_le_tolerates_old_bluez() -> None:
    with patch("dbus_fast.aio.MessageBus", FakeBus), patch.object(FakeBus, "reply_type", MessageType.ERROR):
        await async_prefer_le(BLEDevice("9C:58:84:82:58:20", SIM_NAME, {"path": SIM_PATH, "props": {}}))
    assert len(FakeBus.calls) == 1


async def test_prefer_le_skips_non_bluez_devices() -> None:
    with patch("dbus_fast.aio.MessageBus", side_effect=AssertionError("no D-Bus for a proxy")):
        await async_prefer_le(BLEDevice("9C:58:84:82:58:20", SIM_NAME, {"source": "esphome-proxy"}))


@pytest.mark.parametrize(("name", "pinned"), [(SIM_NAME, True), ("Hoermann", False)])
async def test_open_transport_pins_le_for_the_simulator_only(name: str, pinned: bool) -> None:
    device = BLEDevice(ADDRESS, name, {"path": SIM_PATH, "props": {}})
    with (
        patch("custom_components.hoermoles_ble.controller.async_prefer_le") as prefer_le,
        patch("custom_components.hoermoles_ble.controller.establish_connection", return_value=MagicMock()),
    ):
        await open_transport(device, "Garage")
    assert prefer_le.await_count == (1 if pinned else 0)


@pytest.mark.parametrize(
    ("name", "expected"),
    [(SIM_NAME, True), ("HFSIM", True), ("Hoermann", False), ("xHFSIM1030", False), ("hfsim1030", False)],
)
def test_simulator_match_is_narrow(name: str, expected: bool) -> None:
    assert is_simulator(service_info(None, name=name)) is expected

"""Setup, entities fed by advertisements, cover logic and diagnostics."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.hoermoles_ble.const import (
    CHANNEL_CLOSE,
    CHANNEL_IMPULSE,
    CHANNEL_OPEN,
    CHANNEL_PARTIAL,
    CONF_DISCRETE_OPEN_CLOSE,
    CONF_PRODUCT_CLASS,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_NAME,
    CONF_ROOT_KEY,
    CONF_SERIAL_NO,
    DOMAIN,
)

from .conftest import ADDRESS, KEY_DATA, SERIAL, inject, long_packet, short_packet

COVER = "cover.garage"
POSITION = "sensor.garage_position"
MOVING = "binary_sensor.garage_moving"
BATTERY = "binary_sensor.garage_battery"
IMPULSE = "button.garage_impulse"


@pytest.fixture
def send():
    with patch(
        "custom_components.hoermoles_ble.controller.DriveController.async_send_channel", new_callable=AsyncMock
    ) as mock:
        yield mock


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_sensors_only_entry_has_no_control(hass: HomeAssistant, entry_sensors_only: MockConfigEntry) -> None:
    await _setup(hass, entry_sensors_only)
    assert hass.states.get(POSITION) is not None
    assert hass.states.get(COVER) is None
    assert hass.states.get(IMPULSE) is None


async def test_long_packet_only_gives_position(hass: HomeAssistant, entry_with_key: MockConfigEntry) -> None:
    await _setup(hass, entry_with_key)
    inject(hass, long_packet(position=40))
    await hass.async_block_till_done()

    assert hass.states.get(POSITION).state == "40.0"
    assert hass.states.get(COVER).state == "open"
    assert hass.states.get(COVER).attributes["current_position"] == 40
    # status flags need the short packet
    assert hass.states.get(BATTERY).state == STATE_UNKNOWN


async def test_both_packets_learn_product_and_flags(
    hass: HomeAssistant, entry_sensors_only: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    await _setup(hass, entry_sensors_only)
    inject(hass, short_packet(position=0, low_battery=True))
    inject(hass, long_packet(position=0))
    await hass.async_block_till_done()

    assert hass.states.get(BATTERY).state == "on"
    assert hass.states.get(MOVING).state == "off"
    assert hass.states.get(POSITION).state == "0.0"
    assert entry_sensors_only.data[CONF_PRODUCT_CLASS] == 2
    assert entry_sensors_only.data[CONF_PRODUCT_NAME] == "Supramatic Serie 4"
    assert entry_sensors_only.data[CONF_SERIAL_NO] == str(SERIAL)
    device = device_registry.async_get_device_by_identifier((DOMAIN, ADDRESS), entry_sensors_only.entry_id)
    assert device.model == "Supramatic Serie 4"
    assert device.serial_number == str(SERIAL)


async def test_moving_from_status_flag_then_position(
    hass: HomeAssistant, entry_with_key: MockConfigEntry, freezer
) -> None:
    await _setup(hass, entry_with_key)
    inject(hass, short_packet(position=10, moving=True))
    inject(hass, long_packet(position=10))
    await hass.async_block_till_done()
    assert hass.states.get(MOVING).state == "on"

    # The status packet goes stale; position changes still mean movement
    freezer.tick(31)
    inject(hass, long_packet(position=20))
    await hass.async_block_till_done()
    assert hass.states.get(MOVING).state == "on"
    assert hass.states.get(COVER).state == "opening"

    inject(hass, long_packet(position=15))
    await hass.async_block_till_done()
    assert hass.states.get(COVER).state == "closing"

    # Position settled - the drive keeps repeating the identical packet, which
    # Bluetooth does not report again, so the coordinator's timer has to notice.
    freezer.tick(11)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(MOVING).state == "off"
    assert hass.states.get(COVER).state == "open"


async def _cover(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call("cover", service, {"entity_id": COVER}, blocking=True)


async def test_cover_impulse_logic(hass: HomeAssistant, entry_with_key: MockConfigEntry, send: AsyncMock) -> None:
    await _setup(hass, entry_with_key)
    inject(hass, short_packet(position=0))
    inject(hass, long_packet(position=0))
    await hass.async_block_till_done()
    assert hass.states.get(COVER).state == "closed"

    await _cover(hass, "close_cover")  # already closed: no impulse
    await _cover(hass, "stop_cover")  # not moving: no impulse
    send.assert_not_called()

    await _cover(hass, "open_cover")
    send.assert_awaited_once_with(CHANNEL_IMPULSE)

    send.reset_mock()
    inject(hass, short_packet(position=100))
    inject(hass, long_packet(position=100))
    await hass.async_block_till_done()
    await _cover(hass, "open_cover")  # already open
    send.assert_not_called()
    await _cover(hass, "close_cover")
    send.assert_awaited_once_with(CHANNEL_IMPULSE)

    send.reset_mock()
    inject(hass, short_packet(position=50, moving=True))
    await hass.async_block_till_done()
    await _cover(hass, "stop_cover")
    send.assert_awaited_once_with(CHANNEL_IMPULSE)


async def test_cover_discrete_channels(hass: HomeAssistant, send: AsyncMock) -> None:
    from .conftest import KEY_DATA

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Garage",
        data=dict(KEY_DATA),
        options={CONF_DISCRETE_OPEN_CLOSE: True},
    )
    entry.add_to_hass(hass)
    await _setup(hass, entry)
    inject(hass, long_packet(position=40))
    await hass.async_block_till_done()
    await _cover(hass, "open_cover")
    await _cover(hass, "close_cover")
    assert [call.args[0] for call in send.await_args_list] == [CHANNEL_OPEN, CHANNEL_CLOSE]


async def test_buttons(
    hass: HomeAssistant, entry_with_key: MockConfigEntry, send: AsyncMock, entity_registry: er.EntityRegistry
) -> None:
    await _setup(hass, entry_with_key)
    inject(hass, long_packet(position=0))
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": "button.garage_partial_opening"}, blocking=True)
    send.assert_awaited_once_with(CHANNEL_PARTIAL)
    # Light and ventilation follow the app's defaults table: enabled on a Supramatic 4.2.
    for entity_id in ("button.garage_light_unverified", "button.garage_ventilation_position"):
        assert entity_registry.async_get(entity_id).disabled_by is None
        assert hass.states.get(entity_id) is not None


async def test_plain_supramatic_4_has_no_ventilation(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title="Garage", data={**KEY_DATA, CONF_PRODUCT_ID: 1})
    entry.add_to_hass(hass)
    await _setup(hass, entry)
    assert entity_registry.async_get("button.garage_ventilation_position") is None
    assert entity_registry.async_get("button.garage_light_unverified") is not None


async def test_unload(hass: HomeAssistant, entry_with_key: MockConfigEntry) -> None:
    await _setup(hass, entry_with_key)
    assert await hass.config_entries.async_unload(entry_with_key.entry_id)
    assert entry_with_key.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_redact_key(hass: HomeAssistant, entry_with_key: MockConfigEntry, hass_client) -> None:
    await _setup(hass, entry_with_key)
    inject(hass, long_packet(position=40))
    await hass.async_block_till_done()
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry_with_key)
    assert diagnostics["entry"]["data"][CONF_ROOT_KEY] == "**REDACTED**"
    assert diagnostics["has_key"] is True
    assert diagnostics["advertisement"]["opening_progress_percent"] == 40.0

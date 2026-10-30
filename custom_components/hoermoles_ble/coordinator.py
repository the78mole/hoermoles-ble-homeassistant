"""Passive data plane: the drive's state, read from its BLE advertisement.

No connection and no key needed. The drive splits its advertisement across a
short "status" packet and a long packet (position, maintenance, serial), and
Home Assistant hands us one manufacturer-data payload per advertisement - so
the latest payload of each kind is kept and both are parsed together, exactly
what `AdvertisementInfo.from_scan` expects. An idle drive sends only the long
packet for minutes on end, hence the status flags can be old; see `moving`.

The simulator (HFSIM..., see simulator.py) can't send manufacturer data: once
one of its advertisements shows up, the same two packets are read from its
debug characteristic on a timer instead.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bleak.exc import BleakError
from hoermoles_ble import AdvertisementInfo
from hoermoles_ble.advertisement import LONG_PACKET_MIN_LEN
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
)
from homeassistant.components.bluetooth.passive_update_coordinator import PassiveBluetoothDataUpdateCoordinator
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_PRODUCT_CLASS,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_NAME,
    CONF_SERIAL_NO,
    DOMAIN,
    MANUFACTURER_ID,
    POSITION_SETTLE_TIME,
    STATUS_PACKET_TTL,
)
from .simulator import (
    POLL_INTERVAL_IDLE,
    POLL_INTERVAL_MOVING,
    async_read_packets,
    is_simulator,
)

_LOGGER = logging.getLogger(__name__)


class HoermolesCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Accumulates the two advertisement packets of one drive."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        assert entry.unique_id is not None
        super().__init__(
            hass,
            _LOGGER,
            entry.unique_id,
            BluetoothScanningMode.PASSIVE,
            # Advertisements from non-connectable proxies are just as good for
            # the sensors; control looks for a connectable path separately.
            connectable=False,
        )
        self.entry = entry
        self.product_class: int | None = entry.data.get(CONF_PRODUCT_CLASS)
        self.info: AdvertisementInfo | None = None
        self._short_packet: bytes | None = None
        self._short_packet_time = 0.0
        self._long_packet: bytes | None = None
        self._position: float | None = None
        self._position_time = 0.0
        self.direction = 0  # +1 opening, -1 closing, 0 unknown/idle
        self._cancel_recheck: CALLBACK_TYPE | None = None
        # One BLE connection per drive at a time: commands and simulator reads.
        self.connection_lock = asyncio.Lock()
        self._simulator_rssi: int | None = None  # set once the drive turned out to be the simulator
        self._cancel_simulator_poll: CALLBACK_TYPE | None = None

    @property
    def position(self) -> float | None:
        return self.info.opening_progress_percent if self.info else None

    @property
    def status_fresh(self) -> bool:
        return self._short_packet is not None and time.monotonic() - self._short_packet_time < STATUS_PACKET_TTL

    @property
    def moving(self) -> bool | None:
        """The status packet's `in_action` flag while it is fresh, otherwise
        whether the position changed recently. None until anything is known."""
        if self.info is None:
            return None
        if self.status_fresh and self.info.in_action is not None:
            return self.info.in_action
        if self._position is None:
            return None
        return time.monotonic() - self._position_time < POSITION_SETTLE_TIME

    @callback
    def _async_handle_bluetooth_event(self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange) -> None:
        if (payload := service_info.manufacturer_data.get(MANUFACTURER_ID)) is not None:
            self._store_packet(payload)
            self._update_info(service_info.rssi)
            self._schedule_recheck()
        elif is_simulator(service_info):
            first = self._simulator_rssi is None
            self._simulator_rssi = service_info.rssi
            if first:
                self.async_poll_simulator_soon(0)
        super()._async_handle_bluetooth_event(service_info, change)

    def _store_packet(self, payload: bytes) -> None:
        if len(payload) >= LONG_PACKET_MIN_LEN:
            self._long_packet = payload
        else:
            self._short_packet = payload
            self._short_packet_time = time.monotonic()

    # --- simulator ---------------------------------------------------------

    @callback
    def async_poll_simulator_soon(self, delay: float) -> None:
        """(Re)schedule the next read of the simulator's packets; no-op for a real drive."""
        if self._simulator_rssi is None:
            return
        if self._cancel_simulator_poll is not None:
            self._cancel_simulator_poll()
        self._cancel_simulator_poll = async_call_later(self.hass, delay, self._async_start_simulator_poll)

    @callback
    def _async_start_simulator_poll(self, _now) -> None:
        self._cancel_simulator_poll = None
        # Background task of the entry: cancelled on unload, even mid-connection.
        self.entry.async_create_background_task(self.hass, self._async_poll_simulator(), "hoermoles_ble simulator poll")

    async def _async_poll_simulator(self) -> None:
        try:
            if ble_device := async_ble_device_from_address(self.hass, self.address, connectable=True):
                async with self.connection_lock:
                    packets = await async_read_packets(ble_device)
                for packet in packets:
                    self._store_packet(packet)
                self._update_info(self._simulator_rssi or 0)
                self._schedule_recheck()
                self.async_update_listeners()
        except (BleakError, TimeoutError, ValueError, KeyError) as exc:
            _LOGGER.debug("Reading the simulator's packets failed: %s", exc)
        if self._cancel_simulator_poll is None:  # a command may have asked for an earlier read
            self.async_poll_simulator_soon(POLL_INTERVAL_MOVING if self.moving else POLL_INTERVAL_IDLE)

    def _schedule_recheck(self) -> None:
        """Bluetooth only calls back on *changed* advertisements, and a resting
        drive repeats the same long packet - so nothing would ever tell the
        entities that the door stopped. Re-evaluate at the next point where the
        status packet goes stale or the position counts as settled."""
        if self._cancel_recheck is not None:
            self._cancel_recheck()
            self._cancel_recheck = None
        now = time.monotonic()
        deadlines = [
            deadline
            for deadline in (
                self._short_packet_time + STATUS_PACKET_TTL if self._short_packet is not None else 0.0,
                self._position_time + POSITION_SETTLE_TIME if self._position is not None else 0.0,
            )
            if deadline > now
        ]
        if deadlines:
            self._cancel_recheck = async_call_later(self.hass, min(deadlines) - now + 0.1, self._async_recheck)

    @callback
    def _async_recheck(self, _now) -> None:
        self._cancel_recheck = None
        self.async_update_listeners()
        self._schedule_recheck()

    @callback
    def _async_stop(self) -> None:
        if self._cancel_recheck is not None:
            self._cancel_recheck()
            self._cancel_recheck = None
        if self._cancel_simulator_poll is not None:
            self._cancel_simulator_poll()
            self._cancel_simulator_poll = None
        self._simulator_rssi = None
        super()._async_stop()

    def _update_info(self, rssi: int) -> None:
        payloads = [p for p in (self._short_packet, self._long_packet) if p is not None]
        info = AdvertisementInfo.from_scan(self.address, rssi, payloads, product_class=self.product_class)
        self.info = info

        position = info.opening_progress_percent
        if position is not None and position != self._position:
            if self._position is not None:
                self.direction = 1 if position > self._position else -1
            self._position = position
            self._position_time = time.monotonic()

        if info.product_class is not None and info.product_class != self.product_class:
            self.product_class = info.product_class
            self._remember_product(info)

    def _remember_product(self, info: AdvertisementInfo) -> None:
        """Persist product data learned from a full parse, so the long packet
        alone can be parsed after a restart, and fill in the device entry."""
        data = {
            **self.entry.data,
            CONF_PRODUCT_CLASS: info.product_class,
            CONF_PRODUCT_ID: info.product_id,
            CONF_PRODUCT_NAME: info.product_name or self.entry.data.get(CONF_PRODUCT_NAME),
            CONF_SERIAL_NO: str(info.serial_no) if info.serial_no is not None else self.entry.data.get(CONF_SERIAL_NO),
        }
        self.hass.config_entries.async_update_entry(self.entry, data=data)
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_identifier((DOMAIN, self.address), self.entry.entry_id):
            registry.async_update_device(
                device.id,
                model=data[CONF_PRODUCT_NAME],
                serial_number=data[CONF_SERIAL_NO],
            )

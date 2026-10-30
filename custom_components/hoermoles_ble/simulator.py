"""Debug support for hoerfrau, a simulated drive (github.com/DennyWeinberg/hoerfrau-ble).

The simulator runs on a Mac, and macOS can't advertise manufacturer data - so
it shows up with only a local name, HFSIM + the last 4 serial digits, and
serves the advertisement a real drive would send from a debug characteristic
instead. Everything here keys on that name prefix: a real drive never takes
these paths.
"""

from __future__ import annotations

import json
import logging

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from hoermoles_ble import AdvertisementInfo
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

_LOGGER = logging.getLogger(__name__)

SIMULATOR_NAME_PREFIX = "HFSIM"
# Read + notify, JSON: {"company_id": 1972, "packets": ["<short hex>", "<long hex>"], "serial_no": ...}
DEBUG_ADVERTISEMENT_CHAR = "686f6572-6672-6175-0002-000000000000"

# Seconds between reads of the debug characteristic, standing in for the
# advertisements the sensors would get from a real drive.
POLL_INTERVAL_MOVING = 5.0
POLL_INTERVAL_IDLE = 30.0
# After a command, read soon so the door shows as moving.
POLL_AFTER_COMMAND = 1.0


def is_simulator(service_info: BluetoothServiceInfoBleak | BLEDevice) -> bool:
    return (service_info.name or "").startswith(SIMULATOR_NAME_PREFIX)


async def async_prefer_le(ble_device: BLEDevice) -> None:
    """The Mac advertises from its public address with flags that say it also
    speaks Bluetooth Classic, so BlueZ picks a BR/EDR connection to it and fails
    (br-connection-unknown). Pin the device's bearer to LE before connecting.
    A real drive advertises as LE only and never gets here."""
    details = ble_device.details
    path = details.get("path") if isinstance(details, dict) else None
    if path is None:  # not a local BlueZ adapter, e.g. a proxy
        return
    from dbus_fast import BusType, Message, MessageType, Variant
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=path,
                interface="org.freedesktop.DBus.Properties",
                member="Set",
                signature="ssv",
                body=["org.bluez.Device1", "PreferredBearer", Variant("s", "le")],
            )
        )
    finally:
        bus.disconnect()
    if reply is not None and reply.message_type is MessageType.ERROR:
        # Older BlueZ without PreferredBearer: connect anyway.
        _LOGGER.debug("Could not prefer LE for %s: %s %s", ble_device.address, reply.error_name, reply.body)


async def async_read_packets(ble_device: BLEDevice) -> list[bytes]:
    """The two manufacturer-data payloads the simulated drive would send, read over GATT."""
    await async_prefer_le(ble_device)
    client = await establish_connection(BleakClientWithServiceCache, ble_device, SIMULATOR_NAME_PREFIX, max_attempts=2)
    try:
        raw = await client.read_gatt_char(DEBUG_ADVERTISEMENT_CHAR)
    finally:
        await client.disconnect()
    packets = [bytes.fromhex(packet) for packet in json.loads(raw)["packets"]]
    _LOGGER.debug("Simulator %s advertises %s", ble_device.address, [p.hex() for p in packets])
    return packets


async def async_read_advertisement(ble_device: BLEDevice, rssi: int) -> AdvertisementInfo:
    """The advertisement the simulated drive would send, parsed."""
    return AdvertisementInfo.from_scan(ble_device.address, rssi, await async_read_packets(ble_device))

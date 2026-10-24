"""Constants for the Hoermoles BLE integration."""

from typing import Final

DOMAIN: Final = "hoermoles_ble"

# Bluetooth SIG company identifier of Hoermann (0x07B4) - the drive's
# advertisement carries its status as manufacturer data under this id.
MANUFACTURER_ID: Final = 1972

CONF_ROOT_ID: Final = "root_id"
CONF_ROOT_KEY: Final = "root_key_hex"
CONF_QR_PREFIX: Final = "qr_prefix"
CONF_PRODUCT_CLASS: Final = "product_class"
CONF_PRODUCT_ID: Final = "product_id"
CONF_PRODUCT_NAME: Final = "product_name"
CONF_SERIAL_NO: Final = "serial_no"

# Options
CONF_DISCRETE_OPEN_CLOSE: Final = "discrete_open_close"

# The short "status" advertisement packet (moving, low battery, ...) is mostly
# sent around activity - an idle drive keeps sending only the long packet. A
# status flag older than this is not trusted for the moving state any more.
STATUS_PACKET_TTL: Final = 30.0
# Without a fresh status packet the door counts as moving while its position
# changed within this many seconds.
POSITION_SETTLE_TIME: Final = 10.0

# Channel numbers, see hoermoles_ble.GATE_ACTIONS.
CHANNEL_IMPULSE: Final = 1
CHANNEL_LIGHT: Final = 2
CHANNEL_PARTIAL: Final = 3
CHANNEL_OPEN: Final = 4
CHANNEL_CLOSE: Final = 5
CHANNEL_VENTILATION: Final = 6

# Seconds for connect + challenge + command; the drive itself drops the link
# shortly after the command anyway.
CONTROL_TIMEOUT: Final = 20.0

# Key handoff from the hoermoles-ble web app (see docs/key-handoff.md).
HANDOFF_URL_BASE: Final = "/api/hoermoles_ble/handoff"
HANDOFF_SPA_URL: Final = "https://the78mole.github.io/hoermoles-ble/app/"
HANDOFF_TTL: Final = 15 * 60
HANDOFF_MAX_FAILURES: Final = 5

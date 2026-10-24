"""Diagnostics - the raw advertisement helps with unknown drive models."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HoermolesConfigEntry
from .const import CONF_ROOT_ID, CONF_ROOT_KEY

TO_REDACT = {CONF_ROOT_KEY, CONF_ROOT_ID}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: HoermolesConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    return {
        "entry": {"data": async_redact_data(dict(entry.data), TO_REDACT), "options": dict(entry.options)},
        "has_key": entry.runtime_data.controller is not None,
        "available": coordinator.available,
        "status_fresh": coordinator.status_fresh,
        "moving": coordinator.moving,
        "advertisement": asdict(coordinator.info) if coordinator.info else None,
    }

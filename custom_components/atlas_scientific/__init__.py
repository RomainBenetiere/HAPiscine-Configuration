"""Atlas Scientific integration."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType

DOMAIN = "atlas_scientific"
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Atlas Scientific integration."""

    async def compensate_temperature(call: ServiceCall) -> None:
        """Handle temperature compensation requests."""
        _LOGGER.info("Compensate temperature to %s", call.data)

    hass.services.async_register(DOMAIN, "compensate_temp", compensate_temperature)
    return True

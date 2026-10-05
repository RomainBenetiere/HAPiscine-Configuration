"""Waveshare Modbus POE ETH Relay (8 ch) — relays timed by the board itself.

YAML configuration (see packages/raspipool/waveshare.yaml)::

    waveshare_relay:
      host: 192.168.1.200
      port: 502
      slave: 1
      scan_interval: 5
      lease_minutes: 10
      channels:
        - {channel: 1, name: Pump}
        - {channel: 3, name: ph, max_on: 30, requires: 1}

Services:
  waveshare_relay.run_for  entity_id, minutes | seconds
  waveshare_relay.stop     entity_id
"""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import WaveshareClient
from .protocol import FLASH_MAX_S, RELAY_COUNT, ModbusError
from .scheduler import ChannelConfig, RelayScheduler, RunRefused

_LOGGER = logging.getLogger(__name__)

DOMAIN = "waveshare_relay"

CONF_SLAVE = "slave"
CONF_TIMEOUT = "timeout"
CONF_LEASE = "lease_minutes"
CONF_CHANNELS = "channels"
CONF_CHANNEL = "channel"
CONF_MAX_ON = "max_on"
CONF_REQUIRES = "requires"

ATTR_MINUTES = "minutes"
ATTR_SECONDS = "seconds"

_CHANNEL_NUM = vol.All(vol.Coerce(int), vol.Range(min=1, max=RELAY_COUNT))

CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CHANNEL): _CHANNEL_NUM,
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_MAX_ON, default=0): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional(CONF_REQUIRES): _CHANNEL_NUM,
    }
)


def _unique_channels(channels: list) -> list:
    numbers = [c[CONF_CHANNEL] for c in channels]
    if len(numbers) != len(set(numbers)):
        raise vol.Invalid("duplicate channel number")
    for c in channels:
        if c.get(CONF_REQUIRES) is not None and c[CONF_REQUIRES] not in numbers:
            raise vol.Invalid(f"channel {c[CONF_CHANNEL]} requires undefined channel")
    return channels


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_HOST): cv.string,
                vol.Optional(CONF_PORT, default=502): cv.port,
                vol.Optional(CONF_SLAVE, default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=247)
                ),
                vol.Optional(CONF_SCAN_INTERVAL, default=5): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
                vol.Optional(CONF_TIMEOUT, default=3): vol.All(
                    vol.Coerce(float), vol.Range(min=0.5)
                ),
                vol.Optional(CONF_LEASE, default=10): vol.All(
                    vol.Coerce(float), vol.Range(min=1, max=FLASH_MAX_S / 60)
                ),
                vol.Required(CONF_CHANNELS): vol.All(
                    cv.ensure_list, [CHANNEL_SCHEMA], _unique_channels
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

RUN_FOR_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required("entity_id"): cv.entity_ids,
            vol.Exclusive(ATTR_MINUTES, "duration"): vol.Coerce(float),
            vol.Exclusive(ATTR_SECONDS, "duration"): vol.Coerce(float),
        }
    ),
    cv.has_at_least_one_key(ATTR_MINUTES, ATTR_SECONDS),
)
STOP_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_ids})


class WaveshareHub:
    """Glue between Home Assistant and the HA-agnostic scheduler."""

    def __init__(self, hass: HomeAssistant, conf: dict) -> None:
        self.hass = hass
        self.client = WaveshareClient(
            conf[CONF_HOST], conf[CONF_PORT], conf[CONF_SLAVE], conf[CONF_TIMEOUT]
        )
        self.channel_configs = [
            ChannelConfig(
                channel=c[CONF_CHANNEL],
                name=c[CONF_NAME],
                max_on_s=c[CONF_MAX_ON] * 60,
                requires=c.get(CONF_REQUIRES),
            )
            for c in conf[CONF_CHANNELS]
        ]
        self.scheduler = RelayScheduler(
            self.client,
            self.channel_configs,
            lease_s=conf[CONF_LEASE] * 60,
            on_change=self._on_change,
        )
        self.coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_method=self._async_poll,
            update_interval=timedelta(seconds=conf[CONF_SCAN_INTERVAL]),
        )
        #: entity_id -> channel number, filled by the switch entities.
        self.entity_channels: dict[str, int] = {}

    async def _async_poll(self) -> list[bool]:
        try:
            return await self.scheduler.refresh()
        except ModbusError as err:
            raise UpdateFailed(str(err)) from err

    @callback
    def _on_change(self) -> None:
        # State changed by a command or by the end of a run: push it now.
        self.coordinator.async_set_updated_data(list(self.scheduler.states))

    def channel_for(self, entity_id: str) -> int:
        try:
            return self.entity_channels[entity_id]
        except KeyError as err:
            raise ServiceValidationError(
                f"{entity_id} is not a {DOMAIN} switch"
            ) from err

    async def async_command(self, coro) -> None:
        try:
            await coro
        except RunRefused as err:
            raise HomeAssistantError(str(err)) from err
        except ModbusError as err:
            raise HomeAssistantError(f"Waveshare command failed: {err}") from err


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if DOMAIN not in config:
        return True
    conf = config[DOMAIN]
    hub = WaveshareHub(hass, conf)
    hass.data[DOMAIN] = hub

    await hub.coordinator.async_refresh()
    if not hub.coordinator.last_update_success:
        _LOGGER.warning(
            "Waveshare %s:%s not reachable yet, will keep retrying",
            conf[CONF_HOST], conf[CONF_PORT],
        )

    hass.async_create_task(async_load_platform(hass, "switch", DOMAIN, {}, config))

    async def handle_run_for(call: ServiceCall) -> None:
        if ATTR_SECONDS in call.data:
            seconds = call.data[ATTR_SECONDS]
        else:
            seconds = call.data[ATTR_MINUTES] * 60
        for entity_id in call.data["entity_id"]:
            await hub.async_command(hub.scheduler.run_for(hub.channel_for(entity_id), seconds))

    async def handle_stop(call: ServiceCall) -> None:
        for entity_id in call.data["entity_id"]:
            await hub.async_command(hub.scheduler.turn_off(hub.channel_for(entity_id)))

    hass.services.async_register(DOMAIN, "run_for", handle_run_for, schema=RUN_FOR_SCHEMA)
    hass.services.async_register(DOMAIN, "stop", handle_stop, schema=STOP_SCHEMA)

    async def _shutdown(_event) -> None:
        # Leases are simply no longer renewed: relays drop by themselves.
        await hub.scheduler.shutdown()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    return True

"""Switch entities for the Waveshare relay channels."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, WaveshareHub
from .scheduler import ChannelConfig


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict,
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict | None = None,
) -> None:
    if discovery_info is None:
        return
    hub: WaveshareHub = hass.data[DOMAIN]
    async_add_entities(WaveshareRelaySwitch(hub, cfg) for cfg in hub.channel_configs)


class WaveshareRelaySwitch(CoordinatorEntity, SwitchEntity):
    """One relay channel. ON is always time-limited by the board itself."""

    _attr_should_poll = False

    def __init__(self, hub: WaveshareHub, cfg: ChannelConfig) -> None:
        super().__init__(hub.coordinator)
        self._hub = hub
        self._cfg = cfg
        self._attr_name = cfg.name
        self._attr_unique_id = f"waveshare_relay_ch{cfg.channel}"
        self._attr_icon = "mdi:electric-switch"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._hub.entity_channels[self.entity_id] = self._cfg.channel

    async def async_will_remove_from_hass(self) -> None:
        self._hub.entity_channels.pop(self.entity_id, None)
        await super().async_will_remove_from_hass()

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        return bool(data[self._cfg.channel - 1])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sched = self._hub.scheduler
        run = sched.run(self._cfg.channel)
        attrs: dict[str, Any] = {
            "channel": self._cfg.channel,
            "max_on_minutes": round(self._cfg.max_on_s / 60, 1),
            "requires_channel": self._cfg.requires,
        }
        if run is None:
            attrs["mode"] = "idle"
        elif run.end is None:
            attrs["mode"] = "lease"
        else:
            attrs["mode"] = "timed"
            attrs["run_until"] = datetime.fromtimestamp(run.end_wall, timezone.utc).isoformat()
            remaining = sched.remaining_s(self._cfg.channel)
            attrs["remaining_s"] = None if remaining is None else int(remaining)
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._hub.async_command(self._hub.scheduler.turn_on(self._cfg.channel))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._hub.async_command(self._hub.scheduler.turn_off(self._cfg.channel))

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

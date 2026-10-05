"""Timed relay runs executed by the Waveshare hardware timer (Flash ON).

Home Assistant never holds a relay ON with a software ``delay``:

* run of ``duration <= FLASH_MAX_S`` (54.6 min): **one** Flash ON command; the
  board switches the relay OFF by itself, whatever happens to HA / network.
* longer runs and "unlimited" ON: **lease** mode. The board receives a Flash ON
  of ``lease_s`` which is renewed ``margin_s`` before it expires. If HA or the
  network dies, the relay drops at most ``lease_s`` later.
* a channel may ``require`` another one (chemical pumps require the filtration
  pump): the run is refused if the required relay is OFF, and the required run
  is extended so that it always outlives the dependent one.

This module is pure asyncio (no Home Assistant import) so it can be tested
against a fake Modbus server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .client import WaveshareClient
from .protocol import FLASH_MAX_S, RELAY_COUNT, ModbusError

_LOGGER = logging.getLogger(__name__)

#: Extra time given to a required channel beyond the end of a dependent run.
REQUIRED_MARGIN_S = 60.0
#: Retry period when a lease renewal fails.
RENEW_RETRY_S = 10.0


class RunRefused(Exception):
    """A run cannot be started (interlock)."""


@dataclass
class ChannelConfig:
    channel: int  # 1..8
    name: str
    max_on_s: float = 0.0  # 0 = unlimited (leased) for plain turn_on
    requires: Optional[int] = None  # channel number that must be ON


@dataclass
class Run:
    end: Optional[float]  # loop.time() deadline, None = unlimited
    end_wall: Optional[float]  # time.time() deadline, for display
    armed_until: float  # loop.time() at which the board will drop the relay
    task: Optional[asyncio.Task] = field(default=None, repr=False)


class RelayScheduler:
    def __init__(
        self,
        client: WaveshareClient,
        channels: List[ChannelConfig],
        lease_s: float = 600.0,
        margin_s: Optional[float] = None,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        if lease_s > FLASH_MAX_S:
            raise ValueError(f"lease must be <= {FLASH_MAX_S:.0f}s")
        self.client = client
        self.channels: Dict[int, ChannelConfig] = {c.channel: c for c in channels}
        self.lease_s = lease_s
        self.margin_s = margin_s if margin_s is not None else min(60.0, lease_s / 4)
        self.states: List[bool] = [False] * RELAY_COUNT
        self._runs: Dict[int, Run] = {}
        self._cmd_at: Dict[int, float] = {}
        self._on_change = on_change or (lambda: None)

    # ------------------------------------------------------------------ #
    # Read side
    # ------------------------------------------------------------------ #
    def is_on(self, channel: int) -> bool:
        return self.states[channel - 1]

    def run(self, channel: int) -> Optional[Run]:
        return self._runs.get(channel)

    def remaining_s(self, channel: int) -> Optional[float]:
        """Seconds left in the current run, ``inf`` if unlimited, None if no run."""
        run = self._runs.get(channel)
        if run is None:
            return None
        if run.end is None:
            return float("inf")
        return max(0.0, run.end - asyncio.get_running_loop().time())

    async def refresh(self) -> List[bool]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        states = await self.client.read_relays()
        for ch in range(1, RELAY_COUNT + 1):
            if self._cmd_at.get(ch, -1.0) >= started:
                # Command sent after this poll began: trust our own state.
                states[ch - 1] = self.states[ch - 1]
                continue
            run = self._runs.get(ch)
            if run is not None and not states[ch - 1] and loop.time() < run.armed_until - min(2.0, self.lease_s / 5):
                # Switched OFF outside HA (board web UI, other client...):
                # stop the run so a lease renewal never turns it back ON.
                _LOGGER.warning("CH%s switched OFF externally, run cancelled", ch)
                self._cancel(ch)
        # No on_change here: the coordinator publishes the returned states.
        self.states = states
        return list(states)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def turn_on(self, channel: int) -> None:
        """Plain ON: chemical channels get an automatic ``max_on`` flash."""
        cfg = self.channels[channel]
        await self.run_for(channel, cfg.max_on_s if cfg.max_on_s > 0 else None)

    async def turn_off(self, channel: int) -> None:
        # Interlock: dependents go OFF first.
        for dep in self.channels.values():
            if dep.requires == channel and (self.is_on(dep.channel) or dep.channel in self._runs):
                await self.turn_off(dep.channel)
        self._cancel(channel)
        self._cmd_at[channel] = asyncio.get_running_loop().time()
        await self.client.set_relay(channel - 1, False)
        self._set_state(channel, False)

    async def run_for(self, channel: int, seconds: Optional[float]) -> None:
        """Run ``channel`` for ``seconds`` (None = until turn_off, leased)."""
        cfg = self.channels[channel]
        if seconds is not None and seconds <= 0:
            _LOGGER.info("CH%s %s: duration <= 0, nothing to do", channel, cfg.name)
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        end = None if seconds is None else now + seconds

        if cfg.requires is not None:
            await self._ensure_required(cfg, end)

        self._cancel(channel)
        run = Run(end=end, end_wall=None if seconds is None else time.time() + seconds,
                  armed_until=now)
        self._runs[channel] = run
        try:
            sleep_s, final = await self._arm(channel, run)
        except ModbusError:
            self._runs.pop(channel, None)
            raise
        run.task = loop.create_task(
            self._run_loop(channel, run, sleep_s, final), name=f"waveshare-ch{channel}"
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    async def _ensure_required(self, cfg: ChannelConfig, end: Optional[float]) -> None:
        req = cfg.requires
        assert req is not None
        if not self.is_on(req):
            raise RunRefused(
                f"CH{cfg.channel} {cfg.name} refused: CH{req} "
                f"{self.channels[req].name if req in self.channels else ''} is OFF"
            )
        if end is None:
            return
        req_run = self._runs.get(req)
        if req_run is None or req_run.end is None:
            # Required channel is ON without a finite run (unlimited lease,
            # or switched ON outside HA): nothing to extend.
            return
        wanted = end + REQUIRED_MARGIN_S
        if req_run.end < wanted:
            now = asyncio.get_running_loop().time()
            _LOGGER.info(
                "Extending CH%s run by %.0fs so it outlives CH%s",
                req, wanted - req_run.end, cfg.channel,
            )
            await self.run_for(req, wanted - now)

    async def _arm(self, channel: int, run: Run):
        """Send one Flash ON. Returns (seconds until next action, is_final)."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        remaining = None if run.end is None else run.end - now
        if remaining is not None and remaining <= FLASH_MAX_S:
            if remaining <= 0:
                return 0.0, True
            self._cmd_at[channel] = now
            await self.client.flash_on(channel - 1, remaining)
            run.armed_until = now + remaining
            self._set_state(channel, True)
            return remaining, True
        self._cmd_at[channel] = now
        await self.client.flash_on(channel - 1, self.lease_s)
        run.armed_until = now + self.lease_s
        self._set_state(channel, True)
        return self.lease_s - self.margin_s, False

    async def _run_loop(self, channel: int, run: Run, sleep_s: float, final: bool) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not final:
                await asyncio.sleep(sleep_s)
                try:
                    sleep_s, final = await self._arm(channel, run)
                except ModbusError as err:
                    if loop.time() >= run.armed_until:
                        # The board already dropped the relay: never re-energise
                        # it later on our own, abort the run.
                        _LOGGER.error(
                            "CH%s lease expired during communication loss, run aborted: %s",
                            channel, err,
                        )
                        self._set_state(channel, False)
                        return
                    _LOGGER.warning("CH%s lease renewal failed, retrying: %s", channel, err)
                    sleep_s = min(RENEW_RETRY_S, max(0.5, run.armed_until - loop.time()))
            # Final flash armed: the board switches OFF by itself.
            await asyncio.sleep(sleep_s + 1.0)
            self._set_state(channel, False)
        except asyncio.CancelledError:
            raise
        finally:
            if self._runs.get(channel) is run:
                self._runs.pop(channel, None)
                self._on_change()

    def _cancel(self, channel: int) -> None:
        run = self._runs.pop(channel, None)
        if run is not None and run.task is not None:
            run.task.cancel()

    def _set_state(self, channel: int, on: bool) -> None:
        if self.states[channel - 1] != on:
            self.states[channel - 1] = on
            self._on_change()

    async def shutdown(self) -> None:
        """Stop renewing leases (relays will drop by themselves) and disconnect."""
        for ch in list(self._runs):
            self._cancel(ch)
        await self.client.close()

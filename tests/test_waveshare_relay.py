"""Tests for custom_components/waveshare_relay (no Home Assistant needed).

Run from the repository root:

    python3 -m unittest discover -s tests -v

A fake Waveshare board (asyncio Modbus TCP server with real hardware-like
Flash timers) is used so that failure scenarios (HA crash, network loss) can be
reproduced deterministically. Durations are scaled down to seconds.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import struct
import sys
import types
import unittest

# --- Import the integration modules WITHOUT executing its HA __init__.py ---
_PKG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components",
    "waveshare_relay",
)
_pkg = types.ModuleType("wsr")
_pkg.__path__ = [_PKG_DIR]  # type: ignore[attr-defined]
sys.modules["wsr"] = _pkg
protocol = importlib.import_module("wsr.protocol")
client_mod = importlib.import_module("wsr.client")
scheduler_mod = importlib.import_module("wsr.scheduler")

WaveshareClient = client_mod.WaveshareClient
RelayScheduler = scheduler_mod.RelayScheduler
ChannelConfig = scheduler_mod.ChannelConfig
RunRefused = scheduler_mod.RunRefused


# --------------------------------------------------------------------------- #
# Fake board
# --------------------------------------------------------------------------- #
class FakeWaveshare:
    """Mimics the Waveshare firmware: coils 0-7, Flash ON/OFF timers."""

    def __init__(self) -> None:
        self.relays = [False] * 8
        self._timers: dict = {}
        self.blackhole = False
        self._writers: set = set()
        self.requests: list = []
        self.server = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.cut()
        self.server.close()
        await self.server.wait_closed()
        for handle in self._timers.values():
            handle.cancel()

    def cut(self) -> None:
        """Simulate a network loss: drop and refuse every connection."""
        self.blackhole = True
        for writer in list(self._writers):
            writer.close()

    def restore(self) -> None:
        self.blackhole = False

    async def _handle(self, reader, writer) -> None:
        if self.blackhole:
            writer.close()
            return
        self._writers.add(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _proto, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                if self.blackhole:
                    break
                self.requests.append(pdu)
                resp = self._process(pdu)
                writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    def _process(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        if fc == 0x01:
            addr, count = struct.unpack(">HH", pdu[1:5])
            bits = self.relays[addr : addr + count]
            data = bytearray((count + 7) // 8)
            for i, on in enumerate(bits):
                if on:
                    data[i // 8] |= 1 << (i % 8)
            return bytes([0x01, len(data)]) + bytes(data)
        if fc == 0x05:
            addr, value = struct.unpack(">HH", pdu[1:5])
            if addr < 8:
                if value not in (0xFF00, 0x0000):
                    return bytes([0x85, 0x03])
                self._cancel(addr)
                self.relays[addr] = value == 0xFF00
            elif 0x0200 <= addr < 0x0208:
                self._flash(addr - 0x0200, True, value)
            elif 0x0400 <= addr < 0x0408:
                self._flash(addr - 0x0400, False, value)
            else:
                return bytes([0x85, 0x02])
            return pdu
        return bytes([fc | 0x80, 0x01])

    def _cancel(self, ch: int) -> None:
        handle = self._timers.pop(ch, None)
        if handle is not None:
            handle.cancel()

    def _flash(self, ch: int, state: bool, units: int) -> None:
        self._cancel(ch)
        self.relays[ch] = state

        def revert() -> None:
            self._timers.pop(ch, None)
            self.relays[ch] = not state

        self._timers[ch] = asyncio.get_running_loop().call_later(units * 0.1, revert)


# --------------------------------------------------------------------------- #
# Pure protocol tests (frames from the Waveshare wiki, minus slave id + CRC)
# --------------------------------------------------------------------------- #
class ProtocolTest(unittest.TestCase):
    def test_flash_on_frames_match_wiki(self) -> None:
        # "Relay 0 flash on: 01 05 02 00 00 07 8D B0  //700MS"
        self.assertEqual(protocol.pdu_flash_on(0, 0.7), bytes.fromhex("0502000007"))
        # "Relay 1 flash on: 01 05 02 01 00 08 9C 74  //800MS"
        self.assertEqual(protocol.pdu_flash_on(1, 0.8), bytes.fromhex("0502010008"))

    def test_relay_on_off_frames_match_wiki(self) -> None:
        # "Relay 3 on: 01 05 00 03 FF 00" / "Relay 2 off: 01 05 00 02 00 00"
        self.assertEqual(protocol.pdu_relay(3, True), bytes.fromhex("050003FF00"))
        self.assertEqual(protocol.pdu_relay(2, False), bytes.fromhex("0500020000"))

    def test_flash_units_clamped(self) -> None:
        self.assertEqual(protocol.flash_units(0.01), 1)
        self.assertEqual(protocol.flash_units(600), 6000)
        self.assertEqual(protocol.flash_units(10_000), 0x7FFF)

    def test_mbap_header(self) -> None:
        adu = protocol.build_adu(0x1234, 1, bytes.fromhex("0100000008"))
        self.assertEqual(adu, bytes.fromhex("12340000000601" "0100000008"))

    def test_decode_coils(self) -> None:
        self.assertEqual(
            protocol.decode_coils(bytes([0x01, 0x01, 0b10000101]), 8),
            [True, False, True, False, False, False, False, True],
        )

    def test_exception_response(self) -> None:
        with self.assertRaises(protocol.ModbusExceptionResponse) as ctx:
            protocol.check_response(bytes.fromhex("0502000007"), bytes([0x85, 0x02]))
        self.assertEqual(ctx.exception.code, 0x02)

    def test_invalid_channel(self) -> None:
        with self.assertRaises(ValueError):
            protocol.pdu_relay(8, True)


# --------------------------------------------------------------------------- #
# Scheduler tests against the fake board
# --------------------------------------------------------------------------- #
CHANNELS = [
    ChannelConfig(1, "Pump"),
    ChannelConfig(2, "Turbo"),
    ChannelConfig(3, "ph", max_on_s=1.0, requires=1),
]


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.board = FakeWaveshare()
        await self.board.start()
        self.client = WaveshareClient("127.0.0.1", self.board.port, timeout=0.5)
        self.sched = RelayScheduler(self.client, CHANNELS, lease_s=1.0, margin_s=0.4)
        self._orig_max = scheduler_mod.FLASH_MAX_S
        self._orig_margin = scheduler_mod.REQUIRED_MARGIN_S
        scheduler_mod.REQUIRED_MARGIN_S = 0.5

    async def asyncTearDown(self) -> None:
        scheduler_mod.FLASH_MAX_S = self._orig_max
        scheduler_mod.REQUIRED_MARGIN_S = self._orig_margin
        await self.sched.shutdown()
        await self.board.stop()

    async def test_short_run_is_a_single_hardware_flash(self) -> None:
        await self.sched.run_for(1, 1.0)
        self.assertTrue(self.board.relays[0])
        self.assertEqual(self.board.requests[-1], bytes.fromhex("050200000A"))
        await asyncio.sleep(1.3)
        self.assertFalse(self.board.relays[0], "board must switch OFF by itself")
        self.assertEqual(sum(1 for r in self.board.requests if r[0] == 5), 1)

    async def test_relay_stops_even_if_ha_dies(self) -> None:
        await self.sched.run_for(1, 1.5)
        await self.sched.shutdown()  # HA crash: no more commands at all
        self.assertTrue(self.board.relays[0])
        await asyncio.sleep(1.8)
        self.assertFalse(self.board.relays[0])

    async def test_unlimited_on_is_leased_and_drops_when_ha_dies(self) -> None:
        await self.sched.turn_on(1)  # Pump: max_on 0 -> unlimited lease
        await asyncio.sleep(2.5)
        self.assertTrue(self.board.relays[0], "lease must be renewed")
        await self.sched.shutdown()
        await asyncio.sleep(1.2)
        self.assertFalse(self.board.relays[0], "relay must drop within one lease")

    async def test_long_finite_run_uses_lease_then_final_flash(self) -> None:
        scheduler_mod.FLASH_MAX_S = 2.0  # pretend the firmware max is 2 s
        await self.sched.run_for(2, 3.5)
        await asyncio.sleep(3.0)
        self.assertTrue(self.board.relays[1])
        await asyncio.sleep(1.0)
        self.assertFalse(self.board.relays[1])
        await asyncio.sleep(0.8)  # scheduler keeps a 1 s grace after the final flash
        self.assertIsNone(self.sched.run(2))

    async def test_network_loss_never_reenergises(self) -> None:
        await self.sched.turn_on(2)
        await asyncio.sleep(0.2)
        self.board.cut()
        await asyncio.sleep(1.3)
        self.assertFalse(self.board.relays[1], "relay dropped at lease expiry")
        self.board.restore()
        await asyncio.sleep(1.5)
        self.assertFalse(self.board.relays[1], "must not be re-energised after recovery")
        self.assertIsNone(self.sched.run(2))
        self.assertFalse(self.sched.is_on(2))

    async def test_chemical_refused_when_pump_off(self) -> None:
        with self.assertRaises(RunRefused):
            await self.sched.run_for(3, 0.5)
        self.assertFalse(self.board.relays[2])

    async def test_pump_is_extended_to_outlive_chemical(self) -> None:
        await self.sched.run_for(1, 1.0)
        await self.sched.run_for(3, 2.0)
        await asyncio.sleep(2.2)
        self.assertFalse(self.board.relays[2], "chemical stopped on time")
        self.assertTrue(self.board.relays[0], "pump still running after chemical")
        await asyncio.sleep(0.6)
        self.assertFalse(self.board.relays[0])

    async def test_pump_off_cuts_chemical(self) -> None:
        await self.sched.run_for(1, 5.0)
        await self.sched.run_for(3, 3.0)
        await self.sched.turn_off(1)
        self.assertFalse(self.board.relays[0])
        self.assertFalse(self.board.relays[2])

    async def test_plain_turn_on_of_chemical_is_capped(self) -> None:
        await self.sched.run_for(1, 5.0)
        await self.sched.turn_on(3)  # max_on 1 s
        self.assertTrue(self.board.relays[2])
        await asyncio.sleep(1.3)
        self.assertFalse(self.board.relays[2])

    async def test_external_off_cancels_lease(self) -> None:
        await self.sched.turn_on(2)
        self.board.relays[1] = False  # e.g. switched OFF from the board web UI
        self.board._cancel(1)
        await self.sched.refresh()
        self.assertIsNone(self.sched.run(2))
        await asyncio.sleep(1.2)
        self.assertFalse(self.board.relays[1], "lease renewal must not turn it back ON")

    async def test_refresh_reads_states(self) -> None:
        self.board.relays[4] = True
        states = await self.sched.refresh()
        self.assertTrue(states[4])


if __name__ == "__main__":
    unittest.main()

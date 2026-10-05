"""Minimal asyncio Modbus TCP client for the Waveshare relay board.

No external dependency. One request at a time (the board is a small MCU),
automatic reconnection, one retry on transport errors. Modbus exception
responses are never retried (they are deterministic).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import List, Optional

from .protocol import (
    RELAY_COUNT,
    ModbusError,
    build_adu,
    check_response,
    decode_coils,
    pdu_flash_on,
    pdu_read_coils,
    pdu_relay,
)

_LOGGER = logging.getLogger(__name__)


class WaveshareClient:
    def __init__(self, host: str, port: int, unit_id: int = 1, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._tid = 0

    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        async with self._lock:
            await self._reset()

    async def _reset(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - best effort
                pass

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout
        )
        _LOGGER.debug("Connected to Waveshare %s:%s", self.host, self.port)

    async def request(self, pdu: bytes) -> bytes:
        async with self._lock:
            last_error: Optional[Exception] = None
            for _attempt in range(2):
                try:
                    if self._writer is None:
                        await self._connect()
                    assert self._reader is not None and self._writer is not None
                    self._tid = (self._tid + 1) & 0xFFFF
                    self._writer.write(build_adu(self._tid, self.unit_id, pdu))
                    await self._writer.drain()
                    header = await asyncio.wait_for(self._reader.readexactly(7), self.timeout)
                    tid, _proto, length, _unit = struct.unpack(">HHHB", header)
                    body = await asyncio.wait_for(
                        self._reader.readexactly(length - 1), self.timeout
                    )
                    if tid != self._tid:
                        raise ModbusError(f"transaction id mismatch ({tid} != {self._tid})")
                    return check_response(pdu, body)
                except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as err:
                    last_error = err
                    await self._reset()
            raise ModbusError(
                f"Waveshare {self.host}:{self.port} unreachable: {last_error!r}"
            ) from last_error

    # ------------------------------------------------------------------ #
    async def read_relays(self) -> List[bool]:
        resp = await self.request(pdu_read_coils(0, RELAY_COUNT))
        return decode_coils(resp, RELAY_COUNT)

    async def set_relay(self, channel_index: int, on: bool) -> None:
        await self.request(pdu_relay(channel_index, on))

    async def flash_on(self, channel_index: int, seconds: float) -> None:
        await self.request(pdu_flash_on(channel_index, seconds))

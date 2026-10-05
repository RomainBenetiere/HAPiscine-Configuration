"""Modbus TCP framing for the Waveshare Modbus POE ETH Relay (8 channels).

Pure functions only (no Home Assistant, no I/O) so they can be unit-tested
against the frames published in the Waveshare wiki:
https://www.waveshare.com/wiki/Modbus_POE_ETH_Relay

Why a hand-made client instead of HA's ``modbus`` integration?
The board's *Flash ON* command is a FC05 (write single coil) at address
``0x0200 + channel`` whose **value is the duration** in 100 ms units
(e.g. ``05 02 00 00 07`` = relay 1 ON for 700 ms). HA's ``write_coil`` can only
send ``FF00``/``0000``, so it cannot drive the hardware timer.
"""

from __future__ import annotations

import struct

FC_READ_COILS = 0x01
FC_WRITE_COIL = 0x05

COIL_ON = 0xFF00
COIL_OFF = 0x0000

FLASH_ON_BASE = 0x0200
FLASH_OFF_BASE = 0x0400

#: One flash unit = 100 ms.
FLASH_UNIT_S = 0.1
#: Largest value accepted by the firmware (0x7FFF * 100 ms ~= 54.6 min).
FLASH_MAX_UNITS = 0x7FFF
FLASH_MAX_S = FLASH_MAX_UNITS * FLASH_UNIT_S

RELAY_COUNT = 8

EXCEPTION_NAMES = {
    0x01: "ILLEGAL FUNCTION",
    0x02: "ILLEGAL DATA ADDRESS",
    0x03: "ILLEGAL DATA VALUE",
    0x04: "SERVER DEVICE FAILURE",
}


class ModbusError(Exception):
    """Communication or protocol error."""


class ModbusExceptionResponse(ModbusError):
    """The device answered with a Modbus exception (function code | 0x80)."""

    def __init__(self, function: int, code: int) -> None:
        self.function = function
        self.code = code
        name = EXCEPTION_NAMES.get(code, "UNKNOWN")
        super().__init__(f"Modbus exception FC{function:02X}: {code:#04x} {name}")


def _check_channel(channel_index: int) -> None:
    if not 0 <= channel_index < RELAY_COUNT:
        raise ValueError(f"channel index must be 0..{RELAY_COUNT - 1}, got {channel_index}")


def flash_units(seconds: float) -> int:
    """Convert seconds to firmware units (100 ms), clamped to 1..0x7FFF."""
    units = int(round(seconds / FLASH_UNIT_S))
    return max(1, min(FLASH_MAX_UNITS, units))


def pdu_read_coils(address: int, count: int) -> bytes:
    return struct.pack(">BHH", FC_READ_COILS, address, count)


def pdu_write_coil(address: int, value: int) -> bytes:
    return struct.pack(">BHH", FC_WRITE_COIL, address, value)


def pdu_relay(channel_index: int, on: bool) -> bytes:
    """Plain ON/OFF. Also cancels any running flash on that channel."""
    _check_channel(channel_index)
    return pdu_write_coil(channel_index, COIL_ON if on else COIL_OFF)


def pdu_flash_on(channel_index: int, seconds: float) -> bytes:
    """Relay ON now, switched OFF by the board itself after ``seconds``."""
    _check_channel(channel_index)
    return pdu_write_coil(FLASH_ON_BASE + channel_index, flash_units(seconds))


def build_adu(transaction_id: int, unit_id: int, pdu: bytes) -> bytes:
    """Prefix a PDU with the Modbus TCP MBAP header."""
    return struct.pack(">HHHB", transaction_id & 0xFFFF, 0, len(pdu) + 1, unit_id) + pdu


def check_response(request_pdu: bytes, response_pdu: bytes) -> bytes:
    """Validate a response PDU against its request; raise on exception/mismatch."""
    if not response_pdu:
        raise ModbusError("empty response")
    function = request_pdu[0]
    if response_pdu[0] == function | 0x80:
        code = response_pdu[1] if len(response_pdu) > 1 else 0
        raise ModbusExceptionResponse(function, code)
    if response_pdu[0] != function:
        raise ModbusError(
            f"unexpected function code {response_pdu[0]:#04x} (expected {function:#04x})"
        )
    if function == FC_WRITE_COIL and response_pdu != request_pdu:
        # FC05 answers with an exact echo of the request.
        raise ModbusError("FC05 echo mismatch")
    return response_pdu


def decode_coils(response_pdu: bytes, count: int) -> list[bool]:
    """Decode a FC01 response PDU into ``count`` booleans (LSB first)."""
    if len(response_pdu) < 2:
        raise ModbusError("short FC01 response")
    byte_count = response_pdu[1]
    data = response_pdu[2 : 2 + byte_count]
    if len(data) != byte_count or byte_count * 8 < count:
        raise ModbusError("malformed FC01 response")
    return [bool((data[i // 8] >> (i % 8)) & 1) for i in range(count)]

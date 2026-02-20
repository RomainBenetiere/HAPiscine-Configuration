"""Support for Atlas Scientific EZO sensors."""

from __future__ import annotations

import io
import logging
import time
from typing import Any

import fcntl
import serial
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import CONF_NAME, CONF_PORT, UnitOfTemperature
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

CONF_OFFSET = "offset"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_PORT): vol.Any(cv.string, cv.positive_int),
        vol.Optional(CONF_NAME, default="ezo"): cv.string,
        vol.Optional(CONF_OFFSET, default=0.0): vol.Coerce(float),
    }
)

_DEVICE_CLASSES: dict[str, SensorDeviceClass | None] = {
    "ph": getattr(SensorDeviceClass, "PH", None),
    "temperature": SensorDeviceClass.TEMPERATURE,
    "conductivity": getattr(SensorDeviceClass, "CONDUCTIVITY", None),
}


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Atlas Scientific platform."""
    sensor = await hass.async_add_executor_job(
        AtlasSensor,
        config.get(CONF_NAME),
        config.get(CONF_PORT),
        config.get(CONF_OFFSET),
    )
    async_add_entities([sensor], True)


class AtlasSensor(SensorEntity):
    """Representation of an Atlas EZO sensor."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    long_timeout = 1.5  # timeout for readings/calibrations
    short_timeout = 0.5  # timeout for regular commands
    default_i2dev = "/dev/i2c-1"

    def __init__(self, name: str, port: str, offset: float) -> None:
        """Initialize the sensor."""
        self._offset = offset
        self._base_name = name
        self._attr_name = name
        self._attr_native_value = None
        self._attr_available = True
        self._ezo_dev = None
        self._ezo_uom = None
        self._ezo_icon = None
        self._ezo_fwversion = None
        self._auto_sleep = True

        self._io_mode = "serial"
        self._port: str | int = port
        self._serial: serial.Serial | None = None
        self._file_read: io.BufferedReader | None = None
        self._file_write: io.BufferedWriter | None = None
        self._io_ready = True

        ezos = {
            "ph": ("ph", "pH", "mdi:alpha-h-circle", True),
            "orp": ("orp", "mV", "mdi:alpha-r-circle", True),
            "or": ("orp", "mV", "mdi:alpha-r-circle", True),
            "do": ("dissolved_oxygen", "mV", "mdi:alpha-x-circle", False),
            "d.o.": ("dissolved_oxygen", "mV", "mdi:alpha-x-circle", False),
            "ec": ("conductivity", "EC", "mdi:alpha-c-circle", False),
            "rtd": ("temperature", UnitOfTemperature.CELSIUS, "mdi:oil-temperature", False),
        }

        _LOGGER.debug("Checking port %s", port)
        try:
            i2c_addr = self._parse_i2c_address(port)
            if i2c_addr is not None and i2c_addr > 0:
                self._io_mode = "i2c"
                self._port = i2c_addr
                _LOGGER.info("I2C for Atlas EZO @%02x", i2c_addr)
                self._file_read = io.open(self.default_i2dev, "rb", buffering=0)
                self._file_write = io.open(self.default_i2dev, "wb", buffering=0)
                self._set_i2c_address(i2c_addr)
            else:
                self._io_mode = "serial"
                self._port = str(port)
                _LOGGER.info("Serial for Atlas EZO @%s", self._port)
                self._serial = serial.Serial(
                    self._port, 9600, timeout=3, write_timeout=3
                )

                # Reset buffer and basic configuration for serial mode.
                self._read("")
                self._read("Status")
                self._read("*OK,1")
                self._read("RESPONSE,1")
                self._read("C,0")

            info = ""
            for _ in range(5):
                info = self._read("I")
                if info:
                    _LOGGER.debug("I -> check: %s", info)
                    parts = info.lower().split(",")
                    if len(parts) > 2 and parts[1] in ezos:
                        (
                            self._ezo_dev,
                            self._ezo_uom,
                            self._ezo_icon,
                            self._auto_sleep,
                        ) = ezos[parts[1]]
                        self._ezo_fwversion = parts[2]
                        if self._base_name.endswith(f"_{self._ezo_dev}"):
                            self._attr_name = self._base_name
                        else:
                            self._attr_name = f"{self._base_name}_{self._ezo_dev}"
                        self._attr_icon = self._ezo_icon
                        self._attr_native_unit_of_measurement = self._ezo_uom
                        self._attr_device_class = _DEVICE_CLASSES.get(self._ezo_dev)
                        self._attr_unique_id = f"{self._port}_{self._attr_name}"
                        self._attr_extra_state_attributes = {
                            "firmware_version": self._ezo_fwversion
                        }
                        _LOGGER.info(
                            "Atlas EZO '%s' version %s detected",
                            self._ezo_dev,
                            self._ezo_fwversion,
                        )
                        break

            if self._ezo_dev is None:
                raise RuntimeError(f"Atlas EZO device error or unsupported: {info}")
        except Exception as err:
            _LOGGER.error("Failed to initialize Atlas EZO on %s: %s", port, err)
            self._io_ready = False
            self._attr_available = False
            self._attr_name = self._base_name
            self._attr_unique_id = f"{self._port}_{self._base_name}"
            return

    def _parse_i2c_address(self, port: str) -> int | None:
        """Parse the I2C address, if any."""
        try:
            return int(str(port), 0)
        except (TypeError, ValueError):
            return None

    def _set_i2c_address(self, addr: int) -> None:
        """Set the I2C communications to the specified address."""
        if self._file_read is None or self._file_write is None:
            return
        i2c_slave = 0x703
        fcntl.ioctl(self._file_read, i2c_slave, addr)
        fcntl.ioctl(self._file_write, i2c_slave, addr)

    def _i2c_write(self, cmd: str) -> None:
        """Send a string over I2C."""
        if self._file_write is None:
            return
        self._file_write.write((cmd + "\00").encode())

    def _i2c_read(self, num_of_bytes: int = 31) -> str:
        """Read bytes over I2C and parse the response."""
        if self._file_read is None:
            return ""
        res = self._file_read.read(num_of_bytes)
        response = [byte for byte in res if byte != 0]
        if not response:
            _LOGGER.error("I2C read returned empty response")
            return ""
        if response[0] != 1:
            _LOGGER.error("I2C read error: %s", response[0])
            return ""
        char_list = (chr(byte & ~0x80) for byte in response[1:])
        return "".join(char_list)

    def _read(self, command: str = "R", terminator: str = "\r*OK\r") -> str:
        """Read a response from the device."""
        if not self._io_ready:
            return ""
        if self._io_mode == "serial" and self._serial is not None:
            line = ""
            self._serial.write((command + "\r").encode())
            for _ in range(50):
                chunk = self._serial.read().decode(errors="ignore")
                if not chunk:
                    break
                line += chunk
                if (line.startswith("*") and line.endswith("\r")) or terminator in line:
                    break
            return line.replace(terminator, "")

        self._i2c_write(command)
        if command.upper().startswith("R") or command.upper().startswith("CAL"):
            time.sleep(self.long_timeout)
        elif command.upper().startswith("SLEEP"):
            return "sleep mode"
        else:
            time.sleep(self.short_timeout)
        return self._i2c_read().rstrip("\x00")

    def update(self) -> None:
        """Fetch new state data for the sensor."""
        if not self._io_ready:
            self._attr_native_value = None
            return
        response: Any = None
        try:
            response = self._read()
            self._attr_native_value = float(response) + self._offset
            self._attr_available = True
            _LOGGER.debug("Update %s => '%s'", self.name, self._attr_native_value)
            if self._auto_sleep:
                self._read("SLEEP")
        except (TypeError, ValueError):
            _LOGGER.warning("Invalid reading from %s: %s", self.name, response)
            self._attr_native_value = None
            self._attr_available = False
        except Exception:
            _LOGGER.exception("Error reading from %s", self.name)
            self._attr_available = False

    def __del__(self) -> None:
        """Close the sensor."""
        if self._io_mode == "i2c":
            if self._file_read is not None:
                self._file_read.close()
            if self._file_write is not None:
                self._file_write.close()
        elif self._serial is not None:
            self._serial.close()

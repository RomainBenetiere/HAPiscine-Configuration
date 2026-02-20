"""Support for controlling GPIO pins of a Raspberry Pi."""

from __future__ import annotations

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

try:
    from RPi import GPIO  # type: ignore

    HAS_GPIO = True
except (ImportError, RuntimeError) as err:
    HAS_GPIO = False

    class _GPIOStub:
        BCM = 0
        OUT = 0
        IN = 0
        PUD_DOWN = 0
        PUD_UP = 0
        BOTH = 0

        @staticmethod
        def setmode(mode):
            _LOGGER.warning("RPi.GPIO unavailable, running in stub mode.")

        @staticmethod
        def cleanup():
            return None

        @staticmethod
        def setup(port, mode, pull_up_down=None):
            return None

        @staticmethod
        def output(port, value):
            return None

        @staticmethod
        def input(port):
            return 0

        @staticmethod
        def add_event_detect(port, edge, callback=None, bouncetime=0):
            return None

    GPIO = _GPIOStub()
    _LOGGER.warning("RPi.GPIO import failed: %s", err)

DOMAIN = "rpi_gpio"
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Raspberry PI GPIO component."""
    if not HAS_GPIO:
        _LOGGER.warning("RPi.GPIO not available; rpi_gpio will be disabled.")

    async def cleanup_gpio(event):
        """Clean up GPIO before stopping."""
        await hass.async_add_executor_job(GPIO.cleanup)

    async def prepare_gpio(event):
        """Prepare GPIO when Home Assistant starts."""
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, cleanup_gpio)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, prepare_gpio)
    if HAS_GPIO:
        await hass.async_add_executor_job(GPIO.setmode, GPIO.BCM)
    return True


def setup_output(port):
    """Set up a GPIO as output."""
    GPIO.setup(port, GPIO.OUT)


def setup_input(port, pull_mode):
    """Set up a GPIO as input."""
    GPIO.setup(port, GPIO.IN, GPIO.PUD_DOWN if pull_mode == "DOWN" else GPIO.PUD_UP)


def write_output(port, value):
    """Write a value to a GPIO."""
    GPIO.output(port, value)


def read_input(port):
    """Read a value from a GPIO."""
    return GPIO.input(port)


def edge_detect(port, event_callback, bounce):
    """Add detection for RISING and FALLING events."""
    GPIO.add_event_detect(port, GPIO.BOTH, callback=event_callback, bouncetime=bounce)

"""Tests for the ESP32 RMT memory budget warning."""

from collections.abc import Callable, Generator
import contextvars
import logging

import pytest

from esphome.components import esp32_rmt
from esphome.components.esp32 import (
    KEY_ESP32,
    KEY_VARIANT,
    VARIANT_ESP32,
    VARIANT_ESP32C3,
    VARIANT_ESP32C6,
    VARIANT_ESP32H2,
    VARIANT_ESP32S2,
    VARIANT_ESP32S3,
)
from esphome.const import (
    CONF_ID,
    CONF_PLATFORM,
    CONF_RMT_SYMBOLS,
    CONF_USE_DMA,
    KEY_CORE,
    KEY_TARGET_PLATFORM,
    PLATFORM_ESP32,
)
from esphome.core import CORE
import esphome.final_validate as fv
from esphome.types import ConfigType

# Runs the budget check for a variant against a full config, returns the RMT warnings
CheckRunner = Callable[[str, ConfigType], list[str]]

# Default rmt_symbols of esp32_rmt_led_strip
STRIP_DEFAULT = {
    VARIANT_ESP32: 192,
    VARIANT_ESP32S2: 192,
    VARIANT_ESP32S3: 192,
    VARIANT_ESP32C3: 96,
    VARIANT_ESP32H2: 96,
}
# Default rmt_symbols of remote_transmitter and remote_receiver on ESP32-S2 and S3
TRANSMITTER_DEFAULT = {VARIANT_ESP32S2: 64, VARIANT_ESP32S3: 48}
RECEIVER_DEFAULT = {VARIANT_ESP32S2: 192, VARIANT_ESP32S3: 192}


def _strip(name: str, symbols: int, **extra: object) -> ConfigType:
    return {
        CONF_PLATFORM: "esp32_rmt_led_strip",
        CONF_ID: name,
        CONF_RMT_SYMBOLS: symbols,
        **extra,
    }


def _remote(name: str, symbols: int, **extra: object) -> ConfigType:
    return {CONF_ID: name, CONF_RMT_SYMBOLS: symbols, **extra}


@pytest.fixture
def run_check(
    caplog: pytest.LogCaptureFixture,
) -> Generator[CheckRunner]:
    tokens: list[contextvars.Token] = []

    def run(variant: str, config: ConfigType) -> list[str]:
        CORE.data[KEY_CORE] = {KEY_TARGET_PLATFORM: PLATFORM_ESP32}
        CORE.data[KEY_ESP32] = {KEY_VARIANT: variant}
        tokens.append(fv.full_config.set(config))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            esp32_rmt.validate_symbol_budget({})
        return [r.getMessage() for r in caplog.records if "RMT" in r.getMessage()]

    yield run
    for token in reversed(tokens):
        fv.full_config.reset(token)


@pytest.mark.parametrize(
    ("variant", "strips", "warns"),
    [
        # One strip at its default always fits
        (VARIANT_ESP32, 1, False),
        (VARIANT_ESP32S2, 1, False),
        (VARIANT_ESP32S3, 1, False),
        (VARIANT_ESP32C3, 1, False),
        # ESP32 has 8 shared blocks of 64: two default strips (3 blocks each) fit, three do not
        (VARIANT_ESP32, 2, False),
        (VARIANT_ESP32, 3, True),
        # S2 has 4 shared blocks of 64, S3 has 4 TX blocks of 48
        (VARIANT_ESP32S2, 2, True),
        (VARIANT_ESP32S3, 2, True),
        (VARIANT_ESP32C3, 2, True),
        (VARIANT_ESP32H2, 2, True),
    ],
)
def test_default_strips(
    run_check: CheckRunner, variant: str, strips: int, warns: bool
) -> None:
    """Strips left at their default rmt_symbols are counted against the chip budget."""
    symbols = STRIP_DEFAULT[variant]
    lights = [_strip(f"strip{i}", symbols) for i in range(strips)]
    assert bool(run_check(variant, {"light": lights})) is warns


def test_fan_controller_status_leds_and_neopixel_port_on_s2(
    run_check: CheckRunner,
) -> None:
    """A fan controller with internal status LEDs and an external NeoPixel port.

    Both strips left at the default of 192 need 6 of the 4 shared S2 blocks, so the
    port fails at boot with 'no free tx channels'.
    """
    lights = [_strip("status_leds", 192), _strip("neopixel_port", 192)]
    messages = run_check(VARIANT_ESP32S2, {"light": lights})
    assert len(messages) == 1
    assert "'status_leds' (192), 'neopixel_port' (192)" in messages[0]
    assert "rmt_symbols: 128" in messages[0]


@pytest.mark.parametrize(
    ("variant", "symbols"), [(VARIANT_ESP32S2, 128), (VARIANT_ESP32C6, 48)]
)
def test_fan_controller_with_fitting_rmt_symbols(
    run_check: CheckRunner, variant: str, symbols: int
) -> None:
    """The same fan controller with rmt_symbols set to fit does not warn."""
    lights = [_strip("status_leds", symbols), _strip("neopixel_port", symbols)]
    assert not run_check(variant, {"light": lights})


def test_warning_names_components_and_suggests_value(run_check: CheckRunner) -> None:
    """The warning lists every component and an rmt_symbols value that fits."""
    messages = run_check(
        VARIANT_ESP32S2,
        {"light": [_strip("status_leds", 192), _strip("neopixel_light", 192)]},
    )
    assert len(messages) == 1
    assert "256 symbols" in messages[0]
    assert "'status_leds' (192), 'neopixel_light' (192)" in messages[0]
    assert "rmt_symbols: 128" in messages[0]


@pytest.mark.parametrize(
    ("variant", "symbols"),
    [
        (VARIANT_ESP32S2, 128),
        (VARIANT_ESP32S2, 64),
        (VARIANT_ESP32S3, 96),
        (VARIANT_ESP32S3, 48),
        (VARIANT_ESP32H2, 48),
        (VARIANT_ESP32C3, 48),
    ],
)
def test_explicit_values_that_fit(
    run_check: CheckRunner, variant: str, symbols: int
) -> None:
    """Two strips with an rmt_symbols value that fits do not warn."""
    lights = [_strip("a", symbols), _strip("b", symbols)]
    assert not run_check(variant, {"light": lights})


def test_rounds_up_to_blocks(run_check: CheckRunner) -> None:
    """The driver allocates whole blocks, so 49 symbols on S3 take two blocks of 48."""
    lights = [_strip("a", 49), _strip("b", 49), _strip("c", 48)]
    messages = run_check(VARIANT_ESP32S3, {"light": lights})
    assert len(messages) == 1
    assert "request 240" in messages[0]


def test_strip_and_transmitter_share_tx(run_check: CheckRunner) -> None:
    """remote_transmitter and esp32_rmt_led_strip share the TX blocks (esphome#17894)."""
    config = {
        "light": [_strip("strip", STRIP_DEFAULT[VARIANT_ESP32S3])],
        "remote_transmitter": [_remote("ir", TRANSMITTER_DEFAULT[VARIANT_ESP32S3])],
    }
    messages = run_check(VARIANT_ESP32S3, config)
    assert len(messages) == 1
    assert "TX memory" in messages[0]


def test_receiver_uses_separate_rx_blocks(run_check: CheckRunner) -> None:
    """On S3 the receiver has its own RX blocks and does not take TX memory."""
    config = {
        "light": [_strip("strip", STRIP_DEFAULT[VARIANT_ESP32S3])],
        "remote_receiver": [_remote("rx", RECEIVER_DEFAULT[VARIANT_ESP32S3])],
    }
    assert not run_check(VARIANT_ESP32S3, config)


def test_receiver_shares_blocks_on_s2(run_check: CheckRunner) -> None:
    """On S2 a strip and a receiver fill the four shared blocks exactly."""
    config = {
        "light": [_strip("strip", 64)],
        "remote_receiver": [_remote("rx", RECEIVER_DEFAULT[VARIANT_ESP32S2])],
    }
    assert not run_check(VARIANT_ESP32S2, config)


def test_transmitter_overflows_shared_blocks_on_s2(run_check: CheckRunner) -> None:
    """On S2 a transmitter added next to a strip and a receiver no longer fits."""
    config = {
        "light": [_strip("strip", 64)],
        "remote_receiver": [_remote("rx", RECEIVER_DEFAULT[VARIANT_ESP32S2])],
        "remote_transmitter": [_remote("ir", TRANSMITTER_DEFAULT[VARIANT_ESP32S2])],
    }
    messages = run_check(VARIANT_ESP32S2, config)
    assert len(messages) == 1
    assert "TX/RX memory" in messages[0]


def test_dma_channel_uses_one_block(run_check: CheckRunner) -> None:
    """A DMA channel occupies a single block regardless of rmt_symbols."""
    lights = [_strip("a", 1024, **{CONF_USE_DMA: True}), _strip("b", 144)]
    assert not run_check(VARIANT_ESP32S3, {"light": lights})


def test_too_many_channels(run_check: CheckRunner) -> None:
    """More components than channels cannot be fixed by lowering rmt_symbols."""
    lights = [_strip(f"strip{i}", 48) for i in range(3)]
    messages = run_check(VARIANT_ESP32C3, {"light": lights})
    assert len(messages) == 1
    assert "only 2 TX channels" in messages[0]


def test_other_light_platforms_ignored(run_check: CheckRunner) -> None:
    """Lights of other platforms do not count, even with an rmt_symbols key."""
    lights = [_strip("a", 192), {**_strip("b", 192), CONF_PLATFORM: "neopixelbus"}]
    assert not run_check(VARIANT_ESP32S3, {"light": lights})


def test_check_runs_once(run_check: CheckRunner) -> None:
    """Every RMT component calls the validator; the warning is logged once."""
    lights = [_strip("a", 192), _strip("b", 192)]
    assert run_check(VARIANT_ESP32S3, {"light": lights})
    assert not run_check(VARIANT_ESP32S3, {"light": lights})

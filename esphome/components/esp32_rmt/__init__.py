from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging
from typing import Any

from esphome.components import esp32
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_PLATFORM, CONF_RMT_SYMBOLS, CONF_USE_DMA
from esphome.core import CORE
import esphome.final_validate as fv
from esphome.types import ConfigType

_LOGGER = logging.getLogger(__name__)

CODEOWNERS = ["@jesserockz"]
DOMAIN = "esp32_rmt"

VARIANTS_NO_RMT = {esp32.VARIANT_ESP32C2, esp32.VARIANT_ESP32C61}


@dataclass(frozen=True)
class RMTMemory:
    """RMT channel memory of a variant, from SOC_RMT_* in ESP-IDF soc_caps.h."""

    symbols_per_block: int
    tx_blocks: int
    rx_blocks: int
    # ESP32 and ESP32-S2 channels can transmit or receive, so both share one pool of blocks
    shared: bool = False


RMT_MEMORY = {
    esp32.VARIANT_ESP32: RMTMemory(64, 8, 8, shared=True),
    esp32.VARIANT_ESP32S2: RMTMemory(64, 4, 4, shared=True),
    esp32.VARIANT_ESP32S3: RMTMemory(48, 4, 4),
    esp32.VARIANT_ESP32P4: RMTMemory(48, 4, 4),
    esp32.VARIANT_ESP32C3: RMTMemory(48, 2, 2),
    esp32.VARIANT_ESP32C5: RMTMemory(48, 2, 2),
    esp32.VARIANT_ESP32C6: RMTMemory(48, 2, 2),
    esp32.VARIANT_ESP32H2: RMTMemory(48, 2, 2),
}

# (config domain, light platform or None, transmits) of each component that allocates an RMT channel
RMT_CONSUMERS = (
    ("light", "esp32_rmt_led_strip", True),
    ("remote_transmitter", None, True),
    ("remote_receiver", None, False),
)


@dataclass
class ESP32RMTData:
    budget_checked: bool = False


def _get_data() -> ESP32RMTData:
    if DOMAIN not in CORE.data:
        CORE.data[DOMAIN] = ESP32RMTData()
    return CORE.data[DOMAIN]


def _format_usage(consumers: list[tuple[str, int]], memory: RMTMemory) -> str:
    return ", ".join(
        f"'{name}' ({blocks * memory.symbols_per_block})" for name, blocks in consumers
    )


def _check_pool(
    direction: str,
    consumers: list[tuple[str, int]],
    available_blocks: int,
    memory: RMTMemory,
    variant: str,
) -> None:
    used_blocks = sum(blocks for _, blocks in consumers)
    if used_blocks <= available_blocks:
        return
    available = available_blocks * memory.symbols_per_block
    if len(consumers) > available_blocks:
        hint = (
            f"{variant} has only {available_blocks} {direction} channels, "
            "remove some of these components"
        )
    else:
        per_consumer = available_blocks // len(consumers) * memory.symbols_per_block
        hint = f"for example set '{CONF_RMT_SYMBOLS}: {per_consumer}' on each of them"
    _LOGGER.warning(
        "RMT %s memory on %s is %d symbols, but these components request %d "
        "(rounded up to blocks of %d): %s. Components that do not fit fail at boot "
        "with %s; %s.",
        direction,
        variant,
        available,
        used_blocks * memory.symbols_per_block,
        memory.symbols_per_block,
        _format_usage(consumers, memory),
        " or ".join(f"'no free {d.lower()} channels'" for d in direction.split("/")),
        hint,
    )


def validate_symbol_budget(config: ConfigType) -> ConfigType:
    """Final validation: warn when all RMT components together request more memory than the chip has.

    Call it from the FINAL_VALIDATE_SCHEMA of every component in RMT_CONSUMERS; the check runs once.
    """
    data = _get_data()
    if data.budget_checked or not CORE.is_esp32:
        return config
    data.budget_checked = True
    variant = esp32.get_esp32_variant()
    if (memory := RMT_MEMORY.get(variant)) is None:
        return config

    tx: list[tuple[str, int]] = []
    rx: list[tuple[str, int]] = []
    full_config = fv.full_config.get()
    for domain, platform, transmits in RMT_CONSUMERS:
        for entry in full_config.get(domain, []):
            if platform is not None and entry.get(CONF_PLATFORM) != platform:
                continue
            if (symbols := entry.get(CONF_RMT_SYMBOLS)) is None:
                continue
            # The driver rounds up to whole blocks; a DMA channel uses one block
            if entry.get(CONF_USE_DMA):
                blocks = 1
            else:
                blocks = -(-symbols // memory.symbols_per_block)
            (tx if transmits else rx).append((str(entry[CONF_ID]), blocks))

    if memory.shared:
        _check_pool("TX/RX", tx + rx, memory.tx_blocks, memory, variant)
    else:
        _check_pool("TX", tx, memory.tx_blocks, memory, variant)
        _check_pool("RX", rx, memory.rx_blocks, memory, variant)
    return config


def validate_rmt_not_supported(
    rmt_only_keys: Iterable[str],
) -> Callable[[ConfigType], ConfigType]:
    """Validate that RMT-only config keys are not used on variants without RMT hardware."""
    rmt_only_keys = set(rmt_only_keys)

    def _validator(config: ConfigType) -> ConfigType:
        if CORE.is_esp32:
            variant = esp32.get_esp32_variant()
            if variant in VARIANTS_NO_RMT:
                unsupported = rmt_only_keys.intersection(config)
                if unsupported:
                    keys = ", ".join(sorted(f"'{k}'" for k in unsupported))
                    raise cv.Invalid(
                        f"{keys} not available on {variant} (no RMT hardware)"
                    )
        return config

    return _validator


def validate_clock_resolution() -> Callable[[Any], int]:
    def _validator(value: Any) -> int:
        cv.only_on_esp32(value)
        value = cv.int_(value)
        variant = esp32.get_esp32_variant()
        if variant == esp32.VARIANT_ESP32H2 and value > 32000000:
            raise cv.Invalid(
                f"ESP32 variant {variant} has a max clock_resolution of 32000000."
            )
        if value > 80000000:
            raise cv.Invalid(
                f"ESP32 variant {variant} has a max clock_resolution of 80000000."
            )
        return value

    return _validator

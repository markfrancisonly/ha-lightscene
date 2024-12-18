import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .const import DOMAIN, DEFAULT_NAME, DATA_MANAGER

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Light Scene component from configuration.yaml."""
    if DOMAIN in config:
        _LOGGER.warning(
            "Configuration for Light Scene is managed via the UI. "
            "Please remove it from configuration.yaml."
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Scene from a config entry."""

    if DATA_MANAGER in hass.data:
        _LOGGER.error("Light Scene is already set up.")
        return False

    _LOGGER.debug("Setting up '%s' config entry", DEFAULT_NAME)

    await hass.config_entries.async_forward_entry_setups(entry, [Platform.LIGHT])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, [Platform.LIGHT]
    ):
        manager = hass.data.get(DATA_MANAGER)
        if not manager:
            _LOGGER.error("Light Scene manager not found during unload.")
            return False

        await manager.async_unload_lightscenes()
        hass.data.pop(DATA_MANAGER)
    return unload_ok

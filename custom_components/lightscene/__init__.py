import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from .const import DATA_MANAGER, DEFAULT_NAME, DOMAIN

CONF_DISABLED_SCENES = "disabled_scenes"
SYNC_FLAG = f"{DOMAIN}_syncing_registry"

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Light Scene component from configuration.yaml."""
    if DOMAIN in config:
        _LOGGER.warning(
            "Configuration for Light Scene is managed via the UI. "
            "Please remove '%s:' from configuration.yaml.",
            DOMAIN,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Scene from a config entry."""
    _LOGGER.debug("Setting up '%s' config entry", DEFAULT_NAME)

    # Ensure registry enable/disable matches options BEFORE platform setup
    await _sync_registry_enabled(hass, entry)

    # Now set up the light platform (disabled entities will be skipped by HA)
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.LIGHT])

    # Options change -> reapply registry sync + reload
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Sync registry enable/disable based on options, then reload."""
    await _sync_registry_enabled(hass, entry)
    await hass.config_entries.async_reload(entry.entry_id)


async def _sync_registry_enabled(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply enable/disable to entity registry based on options."""
    registry = er.async_get(hass)
    disabled = set(entry.options.get(CONF_DISABLED_SCENES, []))

    # Prevent feedback loop: registry updates will fire updated events
    hass.data[SYNC_FLAG] = True
    try:
        for ent in list(registry.entities.values()):
            # Only our platform
            if ent.platform != DOMAIN or ent.domain != "light":
                continue
            # unique_id is "<scene_entity_id>_light_scene" (from your light.py)
            if not ent.unique_id.endswith("_light_scene"):
                continue

            scene_entity_id = ent.unique_id[: -len("_light_scene")]
            should_disable = scene_entity_id in disabled
            try:
                if should_disable and ent.disabled_by is None:
                    await registry.async_update_entity(
                        ent.entity_id, disabled_by=RegistryEntryDisabler.INTEGRATION
                    )
                elif not should_disable and ent.disabled_by is not None:
                    await registry.async_update_entity(ent.entity_id, disabled_by=None)
            except Exception as err:  # defensive
                _LOGGER.debug("Registry update failed for %s: %s", ent.entity_id, err)
    finally:
        hass.data.pop(SYNC_FLAG, None)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, [Platform.LIGHT]
    )
    if unload_ok:
        hass.data.pop(DATA_MANAGER, None)
    return unload_ok

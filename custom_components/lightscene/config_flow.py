import logging
from typing import Any, List, Tuple

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import DEFAULT_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_SCENES = "scenes"  # UI shows "Enabled scenes"
CONF_DISABLED_SCENES = "disabled_scenes"  # stored as disabled (all - enabled)


def _all_scenes(hass: HomeAssistant) -> List[Tuple[str, str]]:
    """Return list of (entity_id, friendly_name) for supported scene entities (homeassistant platform)."""
    registry = er.async_get(hass)
    items: List[Tuple[str, str]] = []
    for ent in registry.entities.values():
        if ent.domain == "scene" and ent.platform == "homeassistant":
            state = hass.states.get(ent.entity_id)
            name = state.name if state else ent.original_name or ent.entity_id
            items.append((ent.entity_id, name))
    items.sort(key=lambda x: (x[1].lower(), x[0]))
    return items


def _select_options(hass: HomeAssistant) -> list[dict]:
    """Checkbox list with friendly labels."""
    return [{"value": eid, "label": name} for eid, name in _all_scenes(hass)]


class LightSceneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1  # enable/disable design

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        scenes = _all_scenes(self.hass)
        if not scenes:
            return self.async_abort(reason="no_scenes_found")

        all_ids = [eid for eid, _ in scenes]

        if user_input is not None:
            enabled: list[str] = user_input.get(CONF_SCENES, all_ids)
            disabled = sorted([s for s in all_ids if s not in set(enabled)])
            return self.async_create_entry(
                title=DEFAULT_NAME,
                data={},
                options={CONF_DISABLED_SCENES: disabled},
            )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_SCENES, default=all_ids): selector.selector(
                    {
                        "select": {
                            "options": _select_options(self.hass),
                            "multiple": True,
                            "mode": "list",  # renders as a checkbox list
                        }
                    }
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=data_schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return LightSceneOptionsFlow(config_entry)


class LightSceneOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        scenes = _all_scenes(self.hass)
        all_ids = [eid for eid, _ in scenes]

        if user_input is not None:
            enabled: list[str] = user_input[CONF_SCENES]
            disabled = sorted([s for s in all_ids if s not in set(enabled)])
            return self.async_create_entry(
                title="", data={CONF_DISABLED_SCENES: disabled}
            )

        opts = self.config_entry.options or {}
        disabled_set = set(opts.get(CONF_DISABLED_SCENES, []))
        enabled_current = [s for s in all_ids if s not in disabled_set]

        data_schema = vol.Schema(
            {
                vol.Required(CONF_SCENES, default=enabled_current): selector.selector(
                    {
                        "select": {
                            "options": _select_options(self.hass),
                            "multiple": True,
                            "mode": "list",
                        }
                    }
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=data_schema)

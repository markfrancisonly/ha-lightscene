"""Create a light derivative entity for each scene"""

from collections.abc import Iterable, MutableMapping, Callable
import logging
from typing import Any
import voluptuous as vol

from homeassistant.components.light import (
    LightEntity,
    ColorMode,
    # ATTR_BRIGHTNESS,
    # ATTR_TRANSITION,
    # SUPPORT_BRIGHTNESS,
    # SUPPORT_TRANSITION,
    PLATFORM_SCHEMA,
)

from homeassistant.components.scene import DOMAIN as SCENE_DOMAIN

from homeassistant.core import (
    Event,
    HomeAssistant,
    callback,
    CoreState,
    DOMAIN as HOMEASSISTANT_DOMAIN,
    EVENT_HOMEASSISTANT_STOP,
)

from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_ICON,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    EVENT_CALL_SERVICE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
)

from homeassistant.helpers import entity_platform
from homeassistant.helpers.event import async_track_state_change_event, async_call_later

# from homeassistant.helpers.reload import async_reload_integration_platforms
from homeassistant.helpers.typing import ConfigType  # , DiscoveryInfoType
from homeassistant.helpers.entityfilter import BASE_FILTER_SCHEMA  # , FILTER_SCHEMA

# import homeassistant.helpers.config_validation as cv


# todo: allow for scene brightness scaling
"""
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_BRIGHTNESS_STEP,
    ATTR_BRIGHTNESS_STEP_PCT,
    ATTR_COLOR_NAME,
    ATTR_COLOR_TEMP,
    ATTR_HS_COLOR,
    ATTR_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ATTR_WHITE_VALUE,
    ATTR_XY_COLOR,
    SUPPORT_BRIGHTNESS,
    SUPPORT_COLOR,
    SUPPORT_COLOR_TEMP,
    SUPPORT_TRANSITION,
    SUPPORT_WHITE_VALUE,
    VALID_TRANSITION,
    COLOR_MODE_RGB,
    COLOR_MODE_RGBW,
    COLOR_MODE_HS,
    COLOR_MODE_XY,
    COLOR_MODE_COLOR_TEMP,
    COLOR_MODE_BRIGHTNESS,
    ATTR_SUPPORTED_COLOR_MODES,
)
"""


CONF_FILTER = "filter"
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Optional(CONF_FILTER, default={}): BASE_FILTER_SCHEMA}
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: Callable,
    discovery_info=None,
) -> None:
    """Set up the light scene platform."""

    platform = entity_platform.current_platform.get()
    manager = LightSceneManager(hass, platform, async_add_entities)
    hass.data["LightSceneManager"] = manager

    await manager.async_start_discovery(None)


class LightSceneManager:
    """Representation of all known light scene entities."""

    def __init__(self, hass, platform, async_add_entities):
        """Initialize the light scene manager."""

        self.lightscenes = {}
        self.hass = hass
        self.platform = platform
        self.async_add_entities = async_add_entities

        self.cleanup_release = self.hass.bus.async_listen(
            EVENT_HOMEASSISTANT_STOP, self.cleanup
        )

        self.listener_scene_reloaded_release = self.hass.bus.async_listen(
            "scene_reloaded", self.async_scene_reloaded_event_listener
        )

        self.listener_scene_activated_release = self.hass.bus.async_listen(
            EVENT_CALL_SERVICE, self.async_scene_activated_event_listener
        )

    async def async_start_discovery(self, _now: Any) -> None:
        """Discover scenes and create a light for each."""

        _LOGGER.debug("Scene discovery started")

        for lightscene in self.lightscenes.values():
            _LOGGER.debug("Removed %s", lightscene.name)
            await lightscene.async_remove(force_remove=True)

        all_states = self.hass.states.async_all()

        added_entities = []
        for state in all_states:

            if state.domain != SCENE_DOMAIN:
                continue

            if ATTR_ENTITY_ID in state.attributes:
                scene_entities = state.attributes[ATTR_ENTITY_ID]
            else:
                scene_entities = None

            if ATTR_ICON in state.attributes:
                icon = state.attributes[ATTR_ICON]
            else:
                icon = None

            lightscene = LightScene(state.name, icon, state.entity_id, scene_entities)
            added_entities.append(lightscene)

            self.lightscenes[state.entity_id] = lightscene

        if len(added_entities) > 0:
            self.async_add_entities(added_entities)

    @callback
    async def async_scene_reloaded_event_listener(self, event: Event) -> None:
        """Process scene_reloaded event"""

        _LOGGER.debug("Light scenes manager detected scenes reload")

        # wait for scenes to be reloaded and then repeat discovery
        self._async_cancel_restart_discovery = async_call_later(
            self.hass, 5, self.async_start_discovery
        )

    @callback
    async def async_scene_activated_event_listener(self, event: Event) -> None:
        """Process scene.turn_on event"""

        self._async_cancel_restart_discovery = None
        domain = event.data[ATTR_DOMAIN]
        if domain != SCENE_DOMAIN:
            # not a scene service call
            return

        service = event.data[ATTR_SERVICE]
        if service != SERVICE_TURN_ON:
            # only interested in scene.turn_on events
            return

        service_data = event.data[ATTR_SERVICE_DATA]
        if ATTR_ENTITY_ID not in service_data:
            # can't determine which scene was activated without an entity_id
            return

        try:
            entity_id = service_data[ATTR_ENTITY_ID]
            if entity_id in self.lightscenes:
                await self.lightscenes[entity_id].async_process_scene_activation(event)
        except TypeError:
            _LOGGER.error(
                "TypeError: service data %s, entity id is '%s', lightscenes is '%s'",
                service_data,
                entity_id,
                self.lightscenes,
            )

    @callback
    def cleanup(self, event=None):
        """Release resources."""
        self.cleanup_release()
        self.listener_scene_reloaded_release()
        self.listener_scene_activated_release()

        if self._async_cancel_restart_discovery is not None:
            self._async_cancel_restart_discovery()


class LightScene(LightEntity):
    """Representation of a Light Scene."""

    _scene_activation_context_id: str = None
    _scene_entity_id: str = None
    _scene_entities: Iterable[str] = []
    _attr_extra_state_attributes: MutableMapping[str, Any]
    _removed: bool = False

    def __init__(
        self,
        name,
        icon,
        scene_entity_id,
        scene_entity_ids: list[str],
        entity_filter: vol.Schema = None,
    ):
        """Initialize the switch."""

        self._scene_entity_id = scene_entity_id
        self._scene_entities = scene_entity_ids
        self._entity_filter = entity_filter
        
        self._color_mode = ColorMode.ONOFF
        self._supported_color_modes: set[ColorMode] = set()
        self._supported_color_modes.add(ColorMode.ONOFF)

        self._attr_should_poll = False
        self._attr_supported_features = 0  # SUPPORT_BRIGHTNESS todo
        self._attr_brightness = 0
        self._attr_is_on = False

        self._attr_icon = icon
        self._attr_name = name
        self._attr_extra_state_attributes = {ATTR_ENTITY_ID: self._scene_entities}


    @property
    def supported_color_modes(self) -> set[ColorMode] | None:
        """Flag supported features."""
        return self._supported_color_modes
    
    @property
    def color_mode(self) -> str | None:
        """Return the color mode of the light."""
        return self._color_mode
    
    async def async_remove(self, *, force_remove: bool = False) -> None:
        if not self._removed:
            _LOGGER.debug("Removed %s", self.name)
            self._removed = True

            await super().async_remove(force_remove=True)

    async def async_added_to_hass(self):
        """Called when light scene is about to be added"""

        _LOGGER.debug("Added %s", self._attr_name)

        @callback
        async def async_state_changed_listener(event: Event):
            """Process scene child entity 'state_changed' events."""

            if self._attr_is_on is False:
                return

            entity_id = event.data.get(ATTR_ENTITY_ID, "")

            if self._entity_filter is not None and not self._entity_filter(entity_id):
                return

            new_state = event.data.get("new_state")
            external_change = new_state.context.id != self._scene_activation_context_id

            _LOGGER.debug(
                "%s %s %s change from %s context '%s'",
                self.name,
                "processed" if external_change else "ignored",
                entity_id,
                "external" if external_change else "scene activation",
                new_state.context.id,
            )

            # todo: determining if scene entity change is significant
            # old_state = event.data.get("old_state")

            if external_change:
                self.async_set_context(event.context)
                self._attr_is_on = False
                self.async_schedule_update_ha_state()

        if self._scene_entities is not None and len(self._scene_entities) > 0:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    self._scene_entities,
                    async_state_changed_listener,
                )
            )

        if self.hass.state == CoreState.running:
            self.async_schedule_update_ha_state()
            return

    async def async_process_scene_activation(self, event: Event) -> None:
        """Process scene.turn_on service call for associated scene"""

        self.async_set_context(event.context)
        self._scene_activation_context_id = event.context.id
        self._attr_is_on = True
        self.async_schedule_update_ha_state()

        _LOGGER.debug(
            "Processed %s activation from context '%s'",
            self._scene_entity_id,
            event.context.id,
        )
        # self._attr_brightness = int(255/2)

    async def async_turn_on(self, **kwargs):
        """Activate underlying scene associated with scene light"""

        try:

            await self.hass.services.async_call(
                SCENE_DOMAIN,
                SERVICE_TURN_ON,
                {ATTR_ENTITY_ID: self._scene_entity_id},
                False,
                self._context,
            )
            _LOGGER.debug("Activated %s", self._scene_entity_id)

        except Exception:
            _LOGGER.warning("Failed to activate %s", self._scene_entity_id)

    async def async_turn_off(self, **kwargs):
        """Turn off every child entity in associated scene"""

        _LOGGER.debug("%s turned off", self.name)

        for entity_id in self._scene_entities:

            if self._entity_filter is not None and not self._entity_filter(entity_id):
                continue

            entity = self.hass.states.get(entity_id)
            try:

                if entity.state == STATE_OFF:
                    continue

                await self.hass.services.async_call(
                    HOMEASSISTANT_DOMAIN,
                    SERVICE_TURN_OFF,
                    {ATTR_ENTITY_ID: entity_id},
                    False,
                    self._context,
                )
                _LOGGER.debug("Turned off %s", entity_id)

            except Exception:
                _LOGGER.warning("Failed to turn off %s", entity_id)

    # async def async_update(self):
    #     """Fetch new state data for this light"""

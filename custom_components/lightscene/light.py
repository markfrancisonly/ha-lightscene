from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Dict, cast

from homeassistant.components.homeassistant.scene import (
    DATA_PLATFORM as HOMEASSISTANT_SCENE_DATA_PLATFORM,
)
from homeassistant.components.homeassistant.scene import (
    EVENT_SCENE_RELOADED,
    HomeAssistantScene,
    SceneConfig,
)
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.components.scene import DOMAIN as SCENE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    ATTR_SERVICE_DATA,
    EVENT_CALL_SERVICE,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import Context, CoreState, Event, HomeAssistant, State
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.state import async_reproduce_state

_LOGGER = logging.getLogger(__name__)

DEFAULT_BRIGHTNESS = 255
REPRODUCE_TIMEOUT_SECONDS = 60
EVENT_NEW_STATE = "new_state"

from .const import DATA_MANAGER

# Option key used to control which scenes are disabled by default (all others enabled)
CONF_DISABLED_SCENES = "disabled_scenes"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialize config entry."""

    manager = LightSceneManager(hass, config_entry, async_add_entities)
    hass.data[DATA_MANAGER] = manager

    await manager.async_load_lightscenes(reload=False)


class LightSceneManager:
    """
    Manages LightScene entities for each discovered HomeAssistantScene.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry | None,
        async_add_entities: AddEntitiesCallback,
    ):
        """Initialize the light scene manager."""

        self.lightscenes: Dict[str, LightScene] = {}
        self.hass = hass
        self.async_add_entities = async_add_entities
        self.config_entry = config_entry

        self.listener_scene_reloaded_release = None
        self.listener_scene_activated_release = None

        _LOGGER.debug("Initialized LightSceneManager")

    async def async_scene_reloaded_event_listener(self, event: Event) -> None:
        """
        Reload integration on scene reload event
        """
        _LOGGER.debug("Scenes reloaded")
        await self.async_load_lightscenes()

    async def async_scene_activated_event_listener(self, event: Event) -> None:
        """Process scene.turn_on events to turn on corresponding LightScene entity."""
        try:
            domain = event.data.get(ATTR_DOMAIN)
            if domain != SCENE_DOMAIN:
                return

            service = event.data.get(ATTR_SERVICE)
            if service != SERVICE_TURN_ON:
                return

            service_data = event.data.get(ATTR_SERVICE_DATA, {})
            entity_id = service_data.get(ATTR_ENTITY_ID)
            if not entity_id:
                return

            if isinstance(entity_id, str):
                entity_ids = [entity_id]
            elif isinstance(entity_id, list):
                entity_ids = entity_id
            else:
                _LOGGER.warning(
                    "Unsupported scene activation entity_id type: %s", type(entity_id)
                )
                return

            for entity_id in entity_ids:
                if entity_id in self.lightscenes:
                    _LOGGER.debug(
                        "Scene activation event for %s with context %s",
                        entity_id,
                        event.context.id,
                    )
                    await self.lightscenes[
                        entity_id
                    ].async_process_scene_activation_event(event)

        except Exception as e:
            _LOGGER.error("Error in scene activation listener: %s", e)

    async def async_load_lightscenes(self, reload: bool | None = True):
        """Discover HomeAssistantScene entities and create a LightScene for each."""

        if reload:
            await self.async_unload_lightscenes()

        _LOGGER.debug(
            "Started discovery of %s platform", HOMEASSISTANT_SCENE_DATA_PLATFORM
        )

        self.listener_scene_reloaded_release = self.hass.bus.async_listen(
            EVENT_SCENE_RELOADED, self.async_scene_reloaded_event_listener
        )

        self.listener_scene_activated_release = self.hass.bus.async_listen(
            EVENT_CALL_SERVICE, self.async_scene_activated_event_listener
        )

        if HOMEASSISTANT_SCENE_DATA_PLATFORM not in self.hass.data:
            _LOGGER.warning(
                "No %s platform found. No LightScenes will be created.",
                HOMEASSISTANT_SCENE_DATA_PLATFORM,
            )
            return

        scene_platform = self.hass.data[HOMEASSISTANT_SCENE_DATA_PLATFORM]

        # Read disabled scenes from options (entities listed here will be disabled by default)
        disabled: set[str] = set()
        if self.config_entry is not None:
            opts = self.config_entry.options or {}
            if isinstance(opts.get(CONF_DISABLED_SCENES), list):
                disabled = set(opts[CONF_DISABLED_SCENES])

        new_entities = []
        for entity_id, scene_entity in scene_platform.entities.items():
            if not isinstance(scene_entity, HomeAssistantScene):
                _LOGGER.debug(
                    "Scene %s of type %s ignored during discovery",
                    entity_id,
                    type(scene_entity),
                )
                continue

            lightscene = LightScene(
                hass=self.hass,
                scene_entity_id=entity_id,
                scene_config=cast(HomeAssistantScene, scene_entity).scene_config,
                is_disabled_by_default=(entity_id in disabled),
            )
            self.lightscenes[entity_id] = lightscene
            new_entities.append(lightscene)

        if new_entities:
            self.async_add_entities(new_entities)
            _LOGGER.debug("Added %d LightScene entities.", len(self.lightscenes))

    async def async_unload_lightscenes(self):
        """Clean up event listeners and remove entities."""

        _LOGGER.debug(
            "Unloading all LightScene entities",
        )

        if self.listener_scene_reloaded_release:
            self.listener_scene_reloaded_release()

        if self.listener_scene_activated_release:
            self.listener_scene_activated_release()

        for lightscene in self.lightscenes.values():
            try:
                await lightscene.async_remove()
                _LOGGER.debug("Removed existing LightScene: %s", lightscene.name)
            except Exception as e:
                _LOGGER.error("Error removing LightScene %s: %s", lightscene.name, e)

        self.lightscenes.clear()


class LightScene(LightEntity):
    """
    A LightScene entity representing a scene with brightness scaling and context tracking.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        scene_entity_id: str,
        scene_config: SceneConfig,
        is_disabled_by_default: bool = False,
    ):
        self.hass = hass
        self.scene_entity_id = scene_entity_id
        self.scene_config = scene_config

        self._attr_should_poll = False
        self._attr_unique_id = f"{scene_entity_id}_light_scene"
        self._is_on = False
        self._context: Context | None = None
        self._internal_contexts: set[str] = set()

        # Control default enabled/disabled status in the entity registry
        self._attr_entity_registry_enabled_default = not is_disabled_by_default

        self._scene_brightness_levels: Dict[str, int] = {}
        self._busy_reproducing_states = asyncio.Event()
        self._busy_reproducing_states.set()
        self._reproduce_task: asyncio.Task | None = None
        self._cancelled_reproduce = False

        # determine baseline brightness
        scene_brightness_values = []
        self._scene_brightness_levels = {}
        for entity_id, state in self.scene_config.states.items():
            if entity_id.startswith("light."):
                brightness = state.attributes.get(ATTR_BRIGHTNESS)
                if brightness is None:
                    brightness = 255 if state.state == STATE_ON else 0
                self._scene_brightness_levels[entity_id] = brightness

                if state.state == STATE_ON and brightness > 0:
                    scene_brightness_values.append(brightness)

        if scene_brightness_values:
            self._scene_brightness = sum(scene_brightness_values) // len(
                scene_brightness_values
            )
            self._brightness = self._scene_brightness
            self._has_brightness_control = True
        else:
            self._scene_brightness = DEFAULT_BRIGHTNESS
            self._brightness = self._scene_brightness
            self._has_brightness_control = False

        if self._has_brightness_control:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        else:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}

    @property
    def name(self) -> str:
        """Return the name of the scene."""
        return self.scene_config.name

    @property
    def icon(self) -> str | None:
        """Return the icon of the scene."""
        return self.scene_config.icon

    @property
    def is_on(self) -> bool:
        """Return true if device is on (brightness above 0)."""
        return self._is_on

    @property
    def brightness(self) -> int | None:
        return self._brightness

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the scene state attributes."""
        attributes: dict[str, Any] = {ATTR_ENTITY_ID: list(self.scene_config.states)}
        return attributes

    async def async_added_to_hass(self):
        """
        Called when LightScene is added to hass
        """

        if self._has_brightness_control:
            _LOGGER.debug(
                "LightScene %s added to hass with baseline scene brightness=%d",
                self.name,
                self._scene_brightness,
            )
        else:
            _LOGGER.debug("LightScene %s added to hass", self.name)

        scene_entities = list(self.scene_config.states)
        if scene_entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    scene_entities,
                    self.async_process_state_changed_event,
                )
            )

        if self.hass.state == CoreState.running:
            self.async_schedule_update_ha_state()

    async def async_process_state_changed_event(self, event: Event):
        """If external changes (unrecognized context), turn off LightScene."""

        if not self._busy_reproducing_states.is_set() or not self._is_on:
            return

        entity_id = event.data.get(ATTR_ENTITY_ID, "")
        new_state = event.data.get(EVENT_NEW_STATE)

        if new_state is None or new_state.context is None:
            return

        if self._context and new_state.context.id == self._context.id:
            internal = True
        elif new_state.context.id in self._internal_contexts:
            internal = True
        else:
            internal = False

        if not internal:
            _LOGGER.info(
                "%s deactivated due to external change in entity %s",
                self.name,
                entity_id,
            )
            self._set_off()

    async def async_process_scene_activation_event(self, event: Event) -> None:
        """
        Called when the underlying scene is activated externally.
        """

        if event is None or event.context is None:
            return

        _LOGGER.info("Scene '%s' activated.", self.scene_config.name)

        await self._busy_reproducing_states.wait()

        new_context = event.context
        self._context = new_context
        self.async_set_context(new_context)
        self._internal_contexts.add(new_context.id)

        self._is_on = True
        self._brightness = self._scene_brightness

        _LOGGER.debug(
            "%s is now on at baseline brightness %d", self.name, self._brightness
        )

        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        """
        Turn on or adjust brightness of the LightScene.
        """
        await self._cancel_in_flight()
        await self._busy_reproducing_states.wait()

        try:
            new_context = (
                Context(self._context.user_id, self._context.id)
                if self._context
                else Context()
            )
            self._context = new_context
            self.async_set_context(new_context)
            self._internal_contexts.add(new_context.id)

            target_brightness = kwargs.get(ATTR_BRIGHTNESS)

            if target_brightness is None:
                target_brightness = self._scene_brightness

            if self._has_brightness_control:

                # Compute scale factor to map baseline brightness to target brightness
                scale_factor = target_brightness / self._scene_brightness
                self._brightness = target_brightness

                _LOGGER.info(
                    "%s turned on with target_brightness=%d, scale_factor=%.4f",
                    self.name,
                    target_brightness,
                    scale_factor,
                )

            else:
                _LOGGER.info("%s turned on", self.name)
                scale_factor = None

            states_to_reproduce = []

            for entity_id, config_state in self.scene_config.states.items():
                attrs = dict(config_state.attributes)

                if self._has_brightness_control and entity_id.startswith("light."):

                    initial_brightness = self._scene_brightness_levels.get(entity_id, 0)

                    # When light is on and has a known initial brightness in scene
                    if initial_brightness > 0:

                        # Apply scale factor
                        scaled_brightness = int(initial_brightness * scale_factor)

                        # Ensure brightness stays within [1, 255] for on lights
                        scaled_brightness = max(1, min(255, scaled_brightness))
                        attrs[ATTR_BRIGHTNESS] = scaled_brightness

                        _LOGGER.debug(
                            "%s scaled %s brightness from %d to %d",
                            self.name,
                            entity_id,
                            initial_brightness,
                            scaled_brightness,
                        )

                # For non-lights or when no scaling is needed, reproduce original scene state
                states_to_reproduce.append(State(entity_id, config_state.state, attrs))

            self._is_on = True
            self.async_write_ha_state()

            await self._start_reproduce(states_to_reproduce, "states")
        except asyncio.CancelledError:
            if self._cancelled_reproduce:
                _LOGGER.debug("Reproduce task cancelled for %s", self.name)
                return
            raise
        except Exception as e:
            _LOGGER.error("Error turning on LightScene %s: %s", self.name, e)
            raise

        finally:
            self._reproduce_task = None
            self._cancelled_reproduce = False
            self._busy_reproducing_states.set()

    async def async_turn_off(self, **kwargs):
        """
        Turn off the LightScene by setting all entities off using async_reproduce_state.
        """
        await self._cancel_in_flight()
        await self._busy_reproducing_states.wait()

        try:
            # if not self._is_on:
            #     return

            _LOGGER.debug("Toggling off all entities of LightScene %s", self.name)

            off_states = []
            for entity_id in self.scene_config.states:
                off_states.append(State(entity_id, STATE_OFF, {}))

            self._set_off()

            await self._start_reproduce(off_states, "off states")
        except asyncio.CancelledError:
            if self._cancelled_reproduce:
                _LOGGER.debug("Reproduce task cancelled for %s", self.name)
                return
            raise
        except Exception as e:
            _LOGGER.error("Error turning off LightScene %s: %s", self.name, e)
            raise

        finally:
            self._reproduce_task = None
            self._cancelled_reproduce = False
            self._busy_reproducing_states.set()

    def _set_off(self):
        """
        Set the LightScene off without changing underlying entities
        """

        self._is_on = False
        self._internal_contexts.clear()
        self.async_write_ha_state()

        _LOGGER.info("%s turned off", self.name)

    async def _cancel_in_flight(self) -> None:
        """Cancel any in-flight reproduce task so a new toggle can proceed."""
        task = self._reproduce_task
        if task is None or task.done():
            return

        _LOGGER.debug("Cancelling in-flight reproduce task for %s", self.name)
        self._cancelled_reproduce = True
        await self._drain_reproduce_task()
        self._cancelled_reproduce = False
        # Ensure waiters are unblocked even if the task was cancelled mid-flight.
        self._busy_reproducing_states.set()

    async def _drain_reproduce_task(self) -> None:
        """Cancel and await an in-flight reproduce task after a timeout."""
        task = self._reproduce_task
        if task is None or task.done():
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._reproduce_task = None

    async def _start_reproduce(self, states: list[State], label: str) -> None:
        """Reproduce states with timeout handling."""
        self._busy_reproducing_states.clear()
        self._reproduce_task = asyncio.create_task(
            async_reproduce_state(self.hass, states, context=self._context)
        )
        try:
            await asyncio.wait_for(
                self._reproduce_task, timeout=REPRODUCE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timed out reproducing %s for %s after %s seconds",
                label,
                self.name,
                REPRODUCE_TIMEOUT_SECONDS,
            )
            await self._drain_reproduce_task()

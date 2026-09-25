from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from functools import partial
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
    EVENT_HOMEASSISTANT_STARTED,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, CoreState, Event, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.state import async_reproduce_state

from .const import DATA_MANAGER

_LOGGER = logging.getLogger(__name__)

DEFAULT_BRIGHTNESS = 255
REPRODUCE_TIMEOUT_SECONDS = 60

# State matching: a LightScene is ON when its members' CURRENT states match the
# scene at ANY uniform brightness scale (see SceneStateCoordinator). Evaluation
# is debounced so fades/reproduces settle before on-ness is decided; tolerances
# absorb device quantization (e.g. Z-Wave's 0-99 dimmer scale).
SETTLE_SECONDS = 1.0
BRIGHTNESS_TOLERANCE = 5          # per member, on the 0-255 scale
COLOR_TEMP_KELVIN_TOLERANCE = 150 # more drift than this is a different scene
COLOR_TEMP_MIREDS_TOLERANCE = 10
HS_HUE_TOLERANCE = 10.0
HS_SAT_TOLERANCE = 10.0
RGB_TOLERANCE = 30                # euclidean distance
XY_TOLERANCE = 0.05

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
    # An entry reload removes the entities; the bus listeners must go with them,
    # or this manager keeps reacting to scene reloads after it is gone.
    config_entry.async_on_unload(manager.async_release)

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
        self.coordinator = SceneStateCoordinator(hass, self)

        _LOGGER.debug("Initialized LightSceneManager")

    async def async_scene_reloaded_event_listener(self, event: Event) -> None:
        """
        Reload integration on scene reload event
        """
        _LOGGER.debug("Scenes reloaded")
        await self.async_load_lightscenes(reload=True)

    @staticmethod
    @callback
    def _scene_turn_on_filter(event_data) -> bool:
        """Bus-side filter: only scene.turn_on service calls reach the listener.

        EVENT_CALL_SERVICE fires for EVERY service call in the system; without
        this filter each one scheduled an async task just to check the domain
        and return. The filter runs synchronously inside the bus and schedules
        nothing for non-matching events."""
        return (
            event_data.get(ATTR_DOMAIN) == SCENE_DOMAIN
            and event_data.get(ATTR_SERVICE) == SERVICE_TURN_ON
        )

    async def async_scene_activated_event_listener(self, event: Event) -> None:
        """Process scene.turn_on events to turn on corresponding LightScene entity."""
        try:
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

    async def async_load_lightscenes(self, reload: bool = False):
        """Discover HomeAssistantScene entities and keep one LightScene per scene.

        On a scene reload, lights for scenes that still exist are updated in
        place; only scenes that disappeared are removed and new ones added.
        """

        if not reload:
            self.listener_scene_reloaded_release = self.hass.bus.async_listen(
                EVENT_SCENE_RELOADED, self.async_scene_reloaded_event_listener
            )
            self.listener_scene_activated_release = self.hass.bus.async_listen(
                EVENT_CALL_SERVICE,
                self.async_scene_activated_event_listener,
                event_filter=self._scene_turn_on_filter,
            )

        _LOGGER.debug(
            "Started discovery of %s platform", HOMEASSISTANT_SCENE_DATA_PLATFORM
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

        discovered: Dict[str, SceneConfig] = {}
        for entity_id, scene_entity in scene_platform.entities.items():
            if not isinstance(scene_entity, HomeAssistantScene):
                _LOGGER.debug(
                    "Scene %s of type %s ignored during discovery",
                    entity_id,
                    type(scene_entity),
                )
                continue
            discovered[entity_id] = cast(HomeAssistantScene, scene_entity).scene_config

        for entity_id in [e for e in self.lightscenes if e not in discovered]:
            await self._async_remove_lightscene(self.lightscenes.pop(entity_id))

        new_entities = []
        for entity_id, scene_config in discovered.items():
            if (lightscene := self.lightscenes.get(entity_id)) is not None:
                lightscene.async_update_scene(scene_config)
                continue
            lightscene = LightScene(
                hass=self.hass,
                manager=self,
                scene_entity_id=entity_id,
                scene_config=scene_config,
                is_disabled_by_default=(entity_id in disabled),
            )
            self.lightscenes[entity_id] = lightscene
            new_entities.append(lightscene)

        if new_entities:
            self.async_add_entities(new_entities)
        _LOGGER.debug(
            "%d LightScene entities, %d new.", len(self.lightscenes), len(new_entities)
        )

        # (Re)index memberships and (re)subscribe the shared state listener.
        self.coordinator.async_rebuild()

    @staticmethod
    async def _async_remove_lightscene(lightscene: "LightScene") -> None:
        # Disabled entities were never added; removed ones are detached.
        if lightscene.hass is None or lightscene.platform is None:
            return
        try:
            await lightscene.async_remove()
            _LOGGER.debug("Removed LightScene: %s", lightscene.name)
        except Exception as e:
            _LOGGER.error("Error removing LightScene %s: %s", lightscene.name, e)

    @callback
    def async_release(self) -> None:
        """Stop the coordinator and drop the bus listeners."""
        self.coordinator.async_shutdown()

        if self.listener_scene_reloaded_release:
            self.listener_scene_reloaded_release()
            self.listener_scene_reloaded_release = None

        if self.listener_scene_activated_release:
            self.listener_scene_activated_release()
            self.listener_scene_activated_release = None


class SceneStateCoordinator:
    """Central routing + matching engine: derives each LightScene's on-ness from
    the CURRENT states of its members, so a scene reached by any path (manual
    dimming, another automation, restart restore) reads as on — ground truth
    instead of activation bookkeeping.

    One state subscription covers the union of all scene members (HA core routes
    it keyed by entity_id, so dispatch stays O(1)); a reverse index maps each
    change to the affected scenes; evaluation is debounced per scene so light
    fades and our own reproduces settle before on-ness is decided.
    """

    def __init__(self, hass: HomeAssistant, manager: "LightSceneManager") -> None:
        self.hass = hass
        self.manager = manager
        self._member_to_scenes: Dict[str, set] = {}
        self._scale_bands: Dict[str, tuple] = {}
        self._strict_supersets: Dict[str, list] = {}
        self._unsub_states = None
        self._unsub_started = None
        self._timers: Dict[str, Any] = {}

    def async_rebuild(self) -> None:
        """(Re)index members and (re)subscribe after discovery or scene reload."""
        self._unsubscribe_states()
        self._member_to_scenes = {}
        for scene_id, scene in self.manager.lightscenes.items():
            for member in scene.scene_config.states:
                self._member_to_scenes.setdefault(member, set()).add(scene_id)
                # Group lights hide member changes behind an aggregate that may
                # not move (e.g. offsetting changes); index the members too so
                # their individual changes re-evaluate the scene.
                for expanded in self._group_members(member):
                    self._member_to_scenes.setdefault(expanded, set()).add(scene_id)

        # Ambiguity families: scenes with IDENTICAL member sets and proportional
        # targets are indistinguishable under free scaling (a single shared
        # light is the degenerate case — any level fits some scale). Within a
        # family the brightness ORDER is the only distinguishing data: the tiers
        # partition the level axis into EXCLUSIVE bands, each ending where the
        # next tier begins, so exactly one tier claims any level (Kitchen 75 /
        # Cooking 127: at 127 Cooking takes over and Kitchen reads off). The
        # dimmest tier keeps free downscale, the brightest free upscale.
        families: Dict[tuple, list] = {}
        for scene in self.manager.lightscenes.values():
            families.setdefault(self._family_key(scene), []).append(scene)
        self._scale_bands = {}
        for group in families.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda member: member.scene_brightness)
            for index, scene in enumerate(group):
                lower = 0.0 if index == 0 else scene.scene_brightness - BRIGHTNESS_TOLERANCE
                upper = (
                    group[index + 1].scene_brightness - BRIGHTNESS_TOLERANCE
                    if index + 1 < len(group)
                    else None
                )
                self._scale_bands[scene.scene_entity_id] = (lower, upper)
            _LOGGER.debug(
                "Ambiguity family %s: exclusive level bands %s",
                [member.name for member in group],
                {member.name: self._scale_bands[member.scene_entity_id] for member in group},
            )

        # Most-specific-match-wins: a scene whose member set is a strict
        # SUBSET of another scene's matches coincidentally whenever the
        # superset scene is active — its members sit at those levels because
        # the superset scene put them there, and free downscale then reads
        # the subset scene as a dimmed variant (Master Bath {bath_light}
        # inside Shower {shower_light, bath_light}: Shower on showed BOTH
        # on). At evaluation a matching scene is suppressed while any
        # strict-superset scene also matches; once the superset stops
        # matching, the subset's own match stands on its own.
        member_sets = {
            scene_id: set(scene.scene_config.states)
            for scene_id, scene in self.manager.lightscenes.items()
        }
        self._strict_supersets = {
            scene_id: [
                other_id
                for other_id, other_members in member_sets.items()
                if other_id != scene_id and members < other_members
            ]
            for scene_id, members in member_sets.items()
        }
        # A suppressed subset scene must re-evaluate when the SUPERSET's
        # members change (its own members may not move when the superset
        # scene ends) — index it under those members too.
        for scene_id, supersets in self._strict_supersets.items():
            for other_id in supersets:
                for member in member_sets[other_id]:
                    self._member_to_scenes.setdefault(member, set()).add(scene_id)

        if self._member_to_scenes:
            self._unsub_states = async_track_state_change_event(
                self.hass, list(self._member_to_scenes), self._handle_member_event
            )

        # First sweep: immediately when running; else once HA has fully started
        # (members restore/report at varying times during boot — each arrival
        # re-triggers evaluation through the subscription anyway).
        if self.hass.state == CoreState.running:
            self.async_schedule_all()
        elif self._unsub_started is None:
            self._unsub_started = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._handle_started
            )

    def _group_members(self, entity_id: str, depth: int = 0) -> list:
        """Member entity_ids of a group light (recursive, bounded), else []."""
        if depth >= 2 or not entity_id.startswith("light."):
            return []
        state = self.hass.states.get(entity_id)
        members = state.attributes.get(ATTR_ENTITY_ID) if state else None
        if not isinstance(members, (list, tuple)):
            return []
        expanded = []
        for member in members:
            if isinstance(member, str):
                expanded.append(member)
                expanded.extend(self._group_members(member, depth + 1))
        return expanded

    @staticmethod
    def _family_key(scene: "LightScene") -> tuple:
        """Scenes with equal keys are scale-indistinguishable: same members,
        same non-scalable states, proportional brightness targets."""
        parts = []
        for entity_id in sorted(scene.targets):
            target = scene.targets[entity_id]
            brightness = target.get("brightness")
            if (
                target["state"] == STATE_ON
                and isinstance(brightness, (int, float))
                and brightness > 0
            ):
                # Normalize against the scene's own baseline: proportional
                # scenes collapse to the same ratios.
                parts.append((entity_id, "on", round(brightness / scene.scene_brightness, 2)))
            else:
                parts.append((entity_id, target["state"], None))
        return tuple(parts)

    @callback
    def _handle_started(self, _event: Event) -> None:
        self._unsub_started = None
        self.async_schedule_all()

    def async_schedule_all(self) -> None:
        for scene_id in self.manager.lightscenes:
            self.async_schedule(scene_id)

    @callback
    def _handle_member_event(self, event: Event) -> None:
        entity_id = event.data.get(ATTR_ENTITY_ID)
        for scene_id in self._member_to_scenes.get(entity_id, ()):
            self.async_schedule(scene_id)

    def async_schedule(self, scene_id: str, delay: float = SETTLE_SECONDS) -> None:
        """Debounced: evaluate once events stop for `delay` (resets per event)."""
        self._cancel_timer(scene_id)
        self._timers[scene_id] = async_call_later(
            self.hass, delay, partial(self._evaluate, scene_id)
        )

    def _cancel_timer(self, scene_id: str) -> None:
        timer = self._timers.pop(scene_id, None)
        if timer:
            timer()

    @callback
    def _evaluate(self, scene_id: str, _now=None) -> None:
        self._timers.pop(scene_id, None)
        scene = self.manager.lightscenes.get(scene_id)
        if scene is None:
            return
        if scene.is_reproducing:
            # Our own service calls are mid-flight; look again once they settle.
            self.async_schedule(scene_id)
            return
        matched, brightness = self._match(scene)
        if matched is None:
            # A member is unavailable/unknown (e.g. still restoring after boot):
            # no verdict — keep the current (possibly restored) state. The member
            # reporting in re-triggers evaluation through the subscription.
            return
        if matched:
            # Most-specific-match-wins (see async_rebuild): suppressed while
            # any strict-superset scene also matches.
            for other_id in self._strict_supersets.get(scene_id, ()):
                other = self.manager.lightscenes.get(other_id)
                if other is not None and self._match(other)[0]:
                    matched, brightness = False, None
                    break
        scene.apply_match(matched, brightness)

    def _match(self, scene: "LightScene"):
        """Scale-aware match of current member states against the scene.

        Returns (matched, effective_brightness). Matched means: one uniform
        scale factor maps every baseline to the current brightness (within
        tolerance), off-members are off, colors haven't drifted, and non-light
        members are in their scene state. A member with no usable state yields
        None (no verdict) rather than False.
        """
        scales = []  # (current, baseline) for scalable on-lights
        for entity_id, target in scene.targets.items():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return None, None

            if target["state"] != STATE_ON:
                if state.state != target["state"]:
                    return False, None
                continue

            if state.state != STATE_ON:
                return False, None
            if not entity_id.startswith("light."):
                continue

            baseline = target.get("brightness") or 0

            # A group light reads ON when ANY member is on, and its brightness
            # is the mean of the ON members — one lamp could impersonate
            # "everything at max". The scene's group target means EVERY member
            # at that level, so match the members individually (a uniform
            # scale across them still counts, like any other member).
            group_members = state.attributes.get(ATTR_ENTITY_ID)
            if isinstance(group_members, (list, tuple)) and group_members:
                verdict = self._match_group(target, baseline, group_members, scales)
                if verdict is not True:
                    return verdict, None
                continue

            if not self._color_matches(target, state):
                return False, None
            current = state.attributes.get(ATTR_BRIGHTNESS)
            if baseline > 0 and isinstance(current, (int, float)):
                scales.append((int(current), baseline))

        if not scales:
            return True, None

        mean_scale = sum(current / baseline for current, baseline in scales) / len(scales)
        if mean_scale <= 0:
            return False, None

        # Tiered mode disambiguation: within an ambiguity family, exactly one
        # tier claims any level — this scene's band runs from its own baseline
        # to the next tier's (dimmest reaches down to 0, brightest up forever).
        band = self._scale_bands.get(scene.scene_entity_id)
        if band is not None:
            level = mean_scale * scene.scene_brightness
            lower, upper = band
            if level < lower or (upper is not None and level >= upper):
                return False, None

        for current, baseline in scales:
            expected = max(1, min(255, round(baseline * mean_scale)))
            if abs(current - expected) > BRIGHTNESS_TOLERANCE:
                return False, None

        return True, max(1, min(255, round(scene.scene_brightness * mean_scale)))

    def _match_group(self, target, baseline, members, scales, depth: int = 0):
        """Match every member of a group target individually.

        Tri-state like _match: True (all members fit), False (a member is off
        or drifted), None (a member has no usable state -> no verdict)."""
        if depth >= 2:
            return True
        for member_id in members:
            if not isinstance(member_id, str):
                continue
            state = self.hass.states.get(member_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return None
            if state.state != STATE_ON:
                return False
            nested = state.attributes.get(ATTR_ENTITY_ID)
            if isinstance(nested, (list, tuple)) and nested:
                verdict = self._match_group(target, baseline, nested, scales, depth + 1)
                if verdict is not True:
                    return verdict
                continue
            if not self._color_matches(target, state):
                return False
            current = state.attributes.get(ATTR_BRIGHTNESS)
            if baseline > 0 and isinstance(current, (int, float)):
                scales.append((int(current), baseline))
        return True

    @staticmethod
    def _color_matches(target, state: State) -> bool:
        """Colors are a match-breaker, not scaled: drifted color != same scene."""
        color = target.get("color")
        if not color:
            return True
        kind, want = color
        have = state.attributes.get(kind)
        if have is None:
            return False
        try:
            if kind == "color_temp_kelvin":
                return abs(have - want) <= COLOR_TEMP_KELVIN_TOLERANCE
            if kind == "color_temp":
                return abs(have - want) <= COLOR_TEMP_MIREDS_TOLERANCE
            if kind == "hs_color":
                hue_delta = abs(((have[0] - want[0]) + 180) % 360 - 180)
                return hue_delta <= HS_HUE_TOLERANCE and abs(have[1] - want[1]) <= HS_SAT_TOLERANCE
            if kind == "rgb_color":
                return sum((h - w) ** 2 for h, w in zip(have, want)) ** 0.5 <= RGB_TOLERANCE
            if kind == "xy_color":
                return all(abs(h - w) <= XY_TOLERANCE for h, w in zip(have, want))
        except (TypeError, IndexError):
            return False
        return True

    def async_shutdown(self) -> None:
        self._unsubscribe_states()
        if self._unsub_started:
            self._unsub_started()
            self._unsub_started = None
        for scene_id in list(self._timers):
            self._cancel_timer(scene_id)

    def _unsubscribe_states(self) -> None:
        if self._unsub_states:
            self._unsub_states()
            self._unsub_states = None


class LightScene(LightEntity, RestoreEntity):
    """
    A LightScene entity representing a scene with brightness scaling and
    state-matched on-ness (see SceneStateCoordinator).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        manager: "LightSceneManager",
        scene_entity_id: str,
        scene_config: SceneConfig,
        is_disabled_by_default: bool = False,
    ):
        self.hass = hass
        self.manager = manager
        self.scene_entity_id = scene_entity_id
        self.scene_config = scene_config

        self._attr_should_poll = False
        self._attr_unique_id = f"{scene_entity_id}_light_scene"
        self._is_on = False

        # Control default enabled/disabled status in the entity registry
        self._attr_entity_registry_enabled_default = not is_disabled_by_default

        self._busy_reproducing_states = asyncio.Event()
        self._busy_reproducing_states.set()
        self._reproduce_task: asyncio.Task | None = None
        self._cancelled_reproduce = False

        self._compile()
        self._brightness = self._scene_brightness

    @callback
    def async_update_scene(self, scene_config: SceneConfig) -> None:
        """Take a reloaded scene's config without leaving Home Assistant: a
        remove and re-add would pass every scene light through unavailable."""
        self.scene_config = scene_config
        self._compile()
        if self.hass is not None and self.entity_id is not None:
            self.async_write_ha_state()

    def _compile(self) -> None:
        """Derive targets, baseline brightness and color mode from the scene."""
        self._scene_brightness_levels: Dict[str, int] = {}

        # Compile the matching target table (used by the coordinator) while
        # determining the baseline brightness.
        scene_brightness_values = []
        self._targets: Dict[str, Dict[str, Any]] = {}
        for entity_id, state in self.scene_config.states.items():
            target: Dict[str, Any] = {"state": state.state}
            if entity_id.startswith("light."):
                brightness = state.attributes.get(ATTR_BRIGHTNESS)
                if brightness is None:
                    brightness = 255 if state.state == STATE_ON else 0
                self._scene_brightness_levels[entity_id] = brightness
                target["brightness"] = brightness

                if state.state == STATE_ON and brightness > 0:
                    scene_brightness_values.append(brightness)

                # Capture the scene's color (first color representation found);
                # matching treats color drift as "not this scene".
                for kind in ("color_temp_kelvin", "color_temp", "hs_color", "rgb_color", "xy_color"):
                    value = state.attributes.get(kind)
                    if value is not None:
                        target["color"] = (
                            kind,
                            tuple(value) if isinstance(value, (list, tuple)) else value,
                        )
                        break
            self._targets[entity_id] = target

        # All-off scenes only ever turn on via explicit activation — otherwise
        # they would read "on" any time their lights happen to be off.
        self._auto_on_eligible = any(t["state"] == STATE_ON for t in self._targets.values())

        if scene_brightness_values:
            self._scene_brightness = sum(scene_brightness_values) // len(
                scene_brightness_values
            )
            self._has_brightness_control = True
        else:
            self._scene_brightness = DEFAULT_BRIGHTNESS
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
    def targets(self) -> Dict[str, Dict[str, Any]]:
        """Per-member matching targets compiled from the scene config."""
        return self._targets

    @property
    def scene_brightness(self) -> int:
        """Baseline brightness the scale factor is expressed against."""
        return self._scene_brightness

    @property
    def is_reproducing(self) -> bool:
        """True while this entity's own reproduce is in flight."""
        return not self._busy_reproducing_states.is_set()

    @callback
    def apply_match(self, matched: bool, brightness: int | None) -> None:
        """Coordinator verdict: member states match (a uniform scale of) this
        scene, or not. Turning ON requires auto-on eligibility (all-off scenes
        stay activation-driven); turning OFF always applies."""
        if self.hass is None or self.entity_id is None:
            return

        if matched:
            if not self._is_on and not self._auto_on_eligible:
                return
            new_brightness = self._brightness
            if brightness is not None and self._has_brightness_control:
                new_brightness = brightness
            if self._is_on and new_brightness == self._brightness:
                return
            was_on = self._is_on
            self._is_on = True
            self._brightness = new_brightness
            _LOGGER.info(
                "%s %s current light states%s",
                self.name,
                "matches" if not was_on else "re-matched",
                f" at brightness {new_brightness}" if self._has_brightness_control else "",
            )
            self.async_write_ha_state()
        elif self._is_on:
            self._is_on = False
            _LOGGER.info("%s no longer matches current light states", self.name)
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Return the scene state attributes."""
        attributes: dict[str, Any] = {ATTR_ENTITY_ID: list(self.scene_config.states)}
        return attributes

    async def async_added_to_hass(self):
        """Restore a provisional state; the coordinator's first sweep after the
        members report replaces it with matched ground truth. State watching is
        centralized in the coordinator — no per-entity subscription here."""

        await super().async_added_to_hass()

        last = await self.async_get_last_state()
        if last is not None:
            self._is_on = last.state == STATE_ON
            restored = last.attributes.get(ATTR_BRIGHTNESS)
            if (
                self._has_brightness_control
                and isinstance(restored, (int, float))
                and restored > 0
            ):
                self._brightness = int(restored)

        if self._has_brightness_control:
            _LOGGER.debug(
                "LightScene %s added to hass with baseline scene brightness=%d",
                self.name,
                self._scene_brightness,
            )
        else:
            _LOGGER.debug("LightScene %s added to hass", self.name)

        if self.hass.state == CoreState.running:
            self.async_schedule_update_ha_state()

    async def async_process_scene_activation_event(self, event: Event) -> None:
        """Optimistic fast path: the underlying scene was activated. The
        coordinator confirms (or corrects) once member states settle."""

        if event is None:
            return

        _LOGGER.info("Scene '%s' activated.", self.scene_config.name)

        if event.context is not None:
            self.async_set_context(event.context)

        self._is_on = True
        self._brightness = self._scene_brightness
        self.async_write_ha_state()
        self.manager.coordinator.async_schedule(self.scene_entity_id)

    async def async_turn_on(self, **kwargs):
        """
        Turn on or adjust brightness of the LightScene.
        """
        await self._cancel_in_flight()
        await self._busy_reproducing_states.wait()

        try:
            reproduce_context = Context()
            self.async_set_context(reproduce_context)

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

            await self._start_reproduce(states_to_reproduce, "states", reproduce_context)
        except asyncio.CancelledError:
            if self._cancelled_reproduce:
                _LOGGER.debug("Reproduce task cancelled for %s", self.name)
                return
            raise
        except Exception as e:
            _LOGGER.error("Error turning on LightScene %s: %s", self.name, e)
            raise

        finally:
            self._cancelled_reproduce = False
            # Only tidy up OUR finished work: when this invocation was cancelled
            # by a newer toggle, that toggle may already own a live reproduce
            # task — clearing its handle / setting the busy event here would
            # unblock waiters mid-reproduce.
            if self._reproduce_task is not None and self._reproduce_task.done():
                self._reproduce_task = None
            if self._reproduce_task is None:
                self._busy_reproducing_states.set()
            # Confirm the optimistic state against reality once things settle.
            self.manager.coordinator.async_schedule(self.scene_entity_id)

    async def async_turn_off(self, **kwargs):
        """
        Turn off the LightScene by setting all entities off using async_reproduce_state.
        """
        await self._cancel_in_flight()
        await self._busy_reproducing_states.wait()

        try:
            _LOGGER.debug("Toggling off all entities of LightScene %s", self.name)

            reproduce_context = Context()
            self.async_set_context(reproduce_context)

            off_states = []
            for entity_id in self.scene_config.states:
                off_states.append(State(entity_id, STATE_OFF, {}))

            self._set_off()

            await self._start_reproduce(off_states, "off states", reproduce_context)
        except asyncio.CancelledError:
            if self._cancelled_reproduce:
                _LOGGER.debug("Reproduce task cancelled for %s", self.name)
                return
            raise
        except Exception as e:
            _LOGGER.error("Error turning off LightScene %s: %s", self.name, e)
            raise

        finally:
            self._cancelled_reproduce = False
            # See async_turn_on: never clobber a newer toggle's live reproduce.
            if self._reproduce_task is not None and self._reproduce_task.done():
                self._reproduce_task = None
            if self._reproduce_task is None:
                self._busy_reproducing_states.set()
            # Confirm the optimistic state against reality once things settle.
            self.manager.coordinator.async_schedule(self.scene_entity_id)

    def _set_off(self):
        """
        Set the LightScene off without changing underlying entities
        """

        self._is_on = False
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

    async def _start_reproduce(
        self, states: list[State], label: str, context: Context | None = None
    ) -> None:
        """Reproduce states with timeout handling."""
        self._busy_reproducing_states.clear()
        self._reproduce_task = asyncio.create_task(
            async_reproduce_state(self.hass, states, context=context)
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

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)


class LightSceneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Light Scene."""

    VERSION = 1
    MINOR_VERSION = 0

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step when user adds the integration."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        return self.async_create_entry(title=DEFAULT_NAME, data={}, options={})

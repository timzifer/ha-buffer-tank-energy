"""Config flow for Buffer Tank Energy integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AMBIENT_TEMP_ENTITY,
    CONF_EMA_SMOOTHING,
    CONF_INSULATION_R_VALUE,
    CONF_MAX_TEMPERATURE,
    CONF_PROBE_EMA_SMOOTHING,
    CONF_PROBE_ENTITY,
    CONF_PROBE_NAME,
    CONF_PROBE_POSITION,
    CONF_RETURN_TEMP_ENTITY,
    CONF_TANK_HEIGHT,
    CONF_TANK_VOLUME,
    CONF_THRESHOLD_HYSTERESIS,
    CONF_THRESHOLD_MIN_TEMP,
    CONF_THRESHOLD_NAME,
    CONF_THRESHOLD_PROBE_ID,
    CONFIG_ENTRY_VERSION,
    DEFAULT_EMA_SMOOTHING,
    DEFAULT_MAX_TEMPERATURE,
    DEFAULT_PROBE_EMA_SMOOTHING,
    DEFAULT_THRESHOLD_HYSTERESIS,
    DOMAIN,
    SUBENTRY_PROBE,
    SUBENTRY_THRESHOLD,
)


def _parent_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the schema for the parent entry (tank properties)."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_TANK_VOLUME,
                default=defaults.get(CONF_TANK_VOLUME, 500),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=10,
                    max=100000,
                    step=1,
                    unit_of_measurement="L",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_TANK_HEIGHT,
                default=defaults.get(CONF_TANK_HEIGHT, 1500),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=100,
                    max=10000,
                    step=1,
                    unit_of_measurement="mm",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_MAX_TEMPERATURE,
                default=defaults.get(CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=30,
                    max=100,
                    step=1,
                    unit_of_measurement="°C",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(
                CONF_EMA_SMOOTHING,
                default=defaults.get(CONF_EMA_SMOOTHING, DEFAULT_EMA_SMOOTHING),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.05,
                    max=1.0,
                    step=0.05,
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(
                CONF_RETURN_TEMP_ENTITY,
                description={
                    "suggested_value": defaults.get(CONF_RETURN_TEMP_ENTITY)
                },
            ): EntitySelector(
                EntitySelectorConfig(domain=["sensor", "input_number"])
            ),
            vol.Optional(
                CONF_AMBIENT_TEMP_ENTITY,
                description={
                    "suggested_value": defaults.get(CONF_AMBIENT_TEMP_ENTITY)
                },
            ): EntitySelector(
                EntitySelectorConfig(domain=["sensor", "input_number"])
            ),
            vol.Optional(
                CONF_INSULATION_R_VALUE,
                description={
                    "suggested_value": defaults.get(CONF_INSULATION_R_VALUE)
                },
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.1,
                    max=20.0,
                    step=0.1,
                    unit_of_measurement="m²·K/W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _clean_optional(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty optional fields so they stay absent from entry.data."""
    cleaned = dict(data)
    for key in (
        CONF_RETURN_TEMP_ENTITY,
        CONF_AMBIENT_TEMP_ENTITY,
        CONF_INSULATION_R_VALUE,
    ):
        if key in cleaned and not cleaned[key]:
            cleaned.pop(key)
    return cleaned


class BufferTankEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the top-level config flow for Buffer Tank Energy."""

    VERSION = CONFIG_ENTRY_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options-flow handler."""
        return BufferTankEnergyOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentry types supported by this integration."""
        return {
            SUBENTRY_PROBE: ProbeSubentryFlow,
            SUBENTRY_THRESHOLD: ThresholdSubentryFlow,
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Single-step parent flow: tank properties + optional entities."""
        if user_input is not None:
            data = _clean_optional(user_input)
            return self.async_create_entry(
                title=f"Buffer Tank ({int(data[CONF_TANK_VOLUME])}L)",
                data=data,
            )

        return self.async_show_form(step_id="user", data_schema=_parent_schema())


class BufferTankEnergyOptionsFlow(OptionsFlow):
    """Options flow — edits the parent entry's tank properties."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Edit parent config values."""
        if user_input is not None:
            data = _clean_optional(user_input)
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=data
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=_parent_schema(dict(self._config_entry.data)),
        )


class ProbeSubentryFlow(ConfigSubentryFlow):
    """Flow for adding / editing probe subentries."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle adding a new probe."""
        return await self._async_handle(user_input, defaults=None, reconfigure=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing an existing probe."""
        subentry = self._get_reconfigure_subentry()
        return await self._async_handle(
            user_input, defaults=dict(subentry.data), reconfigure=True
        )

    async def _async_handle(
        self,
        user_input: dict[str, Any] | None,
        defaults: dict[str, Any] | None,
        reconfigure: bool,
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        tank_height = entry.data[CONF_TANK_HEIGHT]
        errors: dict[str, str] = {}

        if user_input is not None:
            position = user_input[CONF_PROBE_POSITION]
            if position > tank_height:
                errors[CONF_PROBE_POSITION] = "position_exceeds_height"
            else:
                clean = {
                    CONF_PROBE_NAME: user_input[CONF_PROBE_NAME],
                    CONF_PROBE_POSITION: position,
                    CONF_PROBE_ENTITY: user_input.get(CONF_PROBE_ENTITY) or None,
                    CONF_PROBE_EMA_SMOOTHING: user_input[CONF_PROBE_EMA_SMOOTHING],
                }
                title = clean[CONF_PROBE_NAME]
                if reconfigure:
                    return self.async_update_and_abort(
                        entry,
                        self._get_reconfigure_subentry(),
                        data=clean,
                        title=title,
                    )
                return self.async_create_entry(title=title, data=clean)

        defaults = defaults or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PROBE_NAME,
                    default=defaults.get(CONF_PROBE_NAME, "Probe"),
                ): str,
                vol.Required(
                    CONF_PROBE_POSITION,
                    default=defaults.get(CONF_PROBE_POSITION, int(tank_height / 2)),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=tank_height,
                        step=1,
                        unit_of_measurement="mm",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_PROBE_ENTITY,
                    description={
                        "suggested_value": defaults.get(CONF_PROBE_ENTITY)
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain=["sensor", "input_number"])
                ),
                vol.Optional(
                    CONF_PROBE_EMA_SMOOTHING,
                    default=defaults.get(
                        CONF_PROBE_EMA_SMOOTHING, DEFAULT_PROBE_EMA_SMOOTHING
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.05,
                        max=1.0,
                        step=0.05,
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
            }
        )
        step_id = "reconfigure" if reconfigure else "user"
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={"tank_height": str(int(tank_height))},
        )


class ThresholdSubentryFlow(ConfigSubentryFlow):
    """Flow for adding / editing threshold subentries."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle adding a new threshold."""
        return await self._async_handle(user_input, defaults=None, reconfigure=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle editing an existing threshold."""
        subentry = self._get_reconfigure_subentry()
        return await self._async_handle(
            user_input, defaults=dict(subentry.data), reconfigure=True
        )

    async def _async_handle(
        self,
        user_input: dict[str, Any] | None,
        defaults: dict[str, Any] | None,
        reconfigure: bool,
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        probe_options = [
            {"value": sid, "label": sub.title}
            for sid, sub in entry.subentries.items()
            if sub.subentry_type == SUBENTRY_PROBE
        ]

        errors: dict[str, str] = {}

        if not probe_options:
            return self.async_abort(reason="no_probes")

        if user_input is not None:
            clean = {
                CONF_THRESHOLD_NAME: user_input[CONF_THRESHOLD_NAME],
                CONF_THRESHOLD_PROBE_ID: user_input[CONF_THRESHOLD_PROBE_ID],
                CONF_THRESHOLD_MIN_TEMP: user_input[CONF_THRESHOLD_MIN_TEMP],
                CONF_THRESHOLD_HYSTERESIS: user_input[CONF_THRESHOLD_HYSTERESIS],
            }
            title = clean[CONF_THRESHOLD_NAME]
            if reconfigure:
                return self.async_update_and_abort(
                    entry,
                    self._get_reconfigure_subentry(),
                    data=clean,
                    title=title,
                )
            return self.async_create_entry(title=title, data=clean)

        defaults = defaults or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_THRESHOLD_NAME,
                    default=defaults.get(CONF_THRESHOLD_NAME, "Threshold"),
                ): str,
                vol.Required(
                    CONF_THRESHOLD_PROBE_ID,
                    default=defaults.get(
                        CONF_THRESHOLD_PROBE_ID, probe_options[0]["value"]
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=probe_options,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_THRESHOLD_MIN_TEMP,
                    default=defaults.get(CONF_THRESHOLD_MIN_TEMP, 50),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=120,
                        step=1,
                        unit_of_measurement="°C",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_THRESHOLD_HYSTERESIS,
                    default=defaults.get(
                        CONF_THRESHOLD_HYSTERESIS, DEFAULT_THRESHOLD_HYSTERESIS
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=30,
                        step=0.5,
                        unit_of_measurement="K",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        step_id = "reconfigure" if reconfigure else "user"
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
        )

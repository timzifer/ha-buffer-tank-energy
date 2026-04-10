"""Config flow for Buffer Tank Energy integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_AMBIENT_TEMP_ENTITY,
    CONF_INSULATION_R_VALUE,
    CONF_MAX_TEMPERATURE,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SENSOR_ENTITY,
    CONF_SENSOR_POSITION,
    CONF_SENSORS,
    CONF_TANK_HEIGHT,
    CONF_TANK_VOLUME,
    DEFAULT_MAX_TEMPERATURE,
    DOMAIN,
)


class BufferTankEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Buffer Tank Energy."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return BufferTankEnergyOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Tank dimensions."""
        if user_input is not None:
            self._data[CONF_TANK_VOLUME] = user_input[CONF_TANK_VOLUME]
            self._data[CONF_TANK_HEIGHT] = user_input[CONF_TANK_HEIGHT]
            self._data[CONF_SENSORS] = []
            return await self.async_step_sensors()

        schema = vol.Schema(
            {
                vol.Required(CONF_TANK_VOLUME): NumberSelector(
                    NumberSelectorConfig(
                        min=10,
                        max=100000,
                        step=1,
                        unit_of_measurement="L",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_TANK_HEIGHT): NumberSelector(
                    NumberSelectorConfig(
                        min=100,
                        max=10000,
                        step=1,
                        unit_of_measurement="mm",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Add temperature sensors."""
        errors: dict[str, str] = {}

        if user_input is not None:
            position = user_input[CONF_SENSOR_POSITION]
            tank_height = self._data[CONF_TANK_HEIGHT]

            if position > tank_height:
                errors["sensor_position"] = "position_exceeds_height"
            else:
                self._data[CONF_SENSORS].append(
                    {
                        CONF_SENSOR_ENTITY: user_input[CONF_SENSOR_ENTITY],
                        CONF_SENSOR_POSITION: position,
                    }
                )

                if user_input.get("add_another", False):
                    return await self.async_step_sensors()

                if len(self._data[CONF_SENSORS]) < 2:
                    errors["base"] = "min_two_sensors"
                else:
                    return await self.async_step_optional()

        tank_height = self._data[CONF_TANK_HEIGHT]

        schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR_ENTITY): EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor", "input_number"],
                    )
                ),
                vol.Required(CONF_SENSOR_POSITION): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=tank_height,
                        step=1,
                        unit_of_measurement="mm",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional("add_another", default=False): BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="sensors",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "count": str(len(self._data.get(CONF_SENSORS, []))),
            },
        )

    async def async_step_optional(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3: Optional settings."""
        if user_input is not None:
            if user_input.get(CONF_RETURN_TEMP_ENTITY):
                self._data[CONF_RETURN_TEMP_ENTITY] = user_input[
                    CONF_RETURN_TEMP_ENTITY
                ]
            if user_input.get(CONF_AMBIENT_TEMP_ENTITY):
                self._data[CONF_AMBIENT_TEMP_ENTITY] = user_input[
                    CONF_AMBIENT_TEMP_ENTITY
                ]
            if user_input.get(CONF_INSULATION_R_VALUE):
                self._data[CONF_INSULATION_R_VALUE] = user_input[
                    CONF_INSULATION_R_VALUE
                ]
            if user_input.get(CONF_MAX_TEMPERATURE):
                self._data[CONF_MAX_TEMPERATURE] = user_input[CONF_MAX_TEMPERATURE]

            return self.async_create_entry(
                title=f"Buffer Tank ({self._data[CONF_TANK_VOLUME]}L)",
                data=self._data,
            )

        schema = vol.Schema(
            {
                vol.Optional(CONF_RETURN_TEMP_ENTITY): EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor", "input_number"],
                    )
                ),
                vol.Optional(CONF_AMBIENT_TEMP_ENTITY): EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor", "input_number"],
                    )
                ),
                vol.Optional(CONF_INSULATION_R_VALUE): NumberSelector(
                    NumberSelectorConfig(
                        min=0.1,
                        max=20.0,
                        step=0.1,
                        unit_of_measurement="m²·K/W",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_MAX_TEMPERATURE, default=DEFAULT_MAX_TEMPERATURE
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=30,
                        max=100,
                        step=1,
                        unit_of_measurement="°C",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="optional", data_schema=schema)


class BufferTankEnergyOptionsFlow(OptionsFlow):
    """Handle options flow for Buffer Tank Energy."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options - start with tank dimensions."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data[CONF_TANK_VOLUME] = user_input[CONF_TANK_VOLUME]
            self._data[CONF_TANK_HEIGHT] = user_input[CONF_TANK_HEIGHT]

            if user_input.get("reconfigure_sensors", False):
                self._data[CONF_SENSORS] = []
                return await self.async_step_sensors()

            # Validate existing sensor positions against new tank height
            new_height = user_input[CONF_TANK_HEIGHT]
            invalid_sensors = [
                s
                for s in self._data.get(CONF_SENSORS, [])
                if s[CONF_SENSOR_POSITION] > new_height
            ]
            if invalid_sensors:
                errors["base"] = "sensors_exceed_new_height"
            else:
                return await self.async_step_optional()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_TANK_VOLUME,
                    default=self._data.get(CONF_TANK_VOLUME),
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
                    default=self._data.get(CONF_TANK_HEIGHT),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=100,
                        max=10000,
                        step=1,
                        unit_of_measurement="mm",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional("reconfigure_sensors", default=False): BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "sensor_count": str(len(self._data.get(CONF_SENSORS, []))),
            },
        )

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Add temperature sensors in options flow."""
        errors: dict[str, str] = {}

        if user_input is not None:
            position = user_input[CONF_SENSOR_POSITION]
            tank_height = self._data[CONF_TANK_HEIGHT]

            if position > tank_height:
                errors["sensor_position"] = "position_exceeds_height"
            else:
                self._data[CONF_SENSORS].append(
                    {
                        CONF_SENSOR_ENTITY: user_input[CONF_SENSOR_ENTITY],
                        CONF_SENSOR_POSITION: position,
                    }
                )

                if user_input.get("add_another", False):
                    return await self.async_step_sensors()

                if len(self._data[CONF_SENSORS]) < 2:
                    errors["base"] = "min_two_sensors"
                else:
                    return await self.async_step_optional()

        tank_height = self._data[CONF_TANK_HEIGHT]

        schema = vol.Schema(
            {
                vol.Required(CONF_SENSOR_ENTITY): EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor", "input_number"],
                    )
                ),
                vol.Required(CONF_SENSOR_POSITION): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=tank_height,
                        step=1,
                        unit_of_measurement="mm",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional("add_another", default=False): BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="sensors",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "count": str(len(self._data.get(CONF_SENSORS, []))),
            },
        )

    async def async_step_optional(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Optional settings in options flow."""
        if user_input is not None:
            self._data[CONF_RETURN_TEMP_ENTITY] = user_input.get(
                CONF_RETURN_TEMP_ENTITY
            )
            self._data[CONF_AMBIENT_TEMP_ENTITY] = user_input.get(
                CONF_AMBIENT_TEMP_ENTITY
            )
            self._data[CONF_INSULATION_R_VALUE] = user_input.get(
                CONF_INSULATION_R_VALUE
            )
            self._data[CONF_MAX_TEMPERATURE] = user_input.get(
                CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE
            )

            self.hass.config_entries.async_update_entry(
                self._config_entry, data=self._data
            )
            return self.async_create_entry(title="", data={})

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RETURN_TEMP_ENTITY,
                    description={
                        "suggested_value": self._data.get(CONF_RETURN_TEMP_ENTITY)
                    },
                ): EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor", "input_number"],
                    )
                ),
                vol.Optional(
                    CONF_AMBIENT_TEMP_ENTITY,
                    description={
                        "suggested_value": self._data.get(CONF_AMBIENT_TEMP_ENTITY)
                    },
                ): EntitySelector(
                    EntitySelectorConfig(
                        domain=["sensor", "input_number"],
                    )
                ),
                vol.Optional(
                    CONF_INSULATION_R_VALUE,
                    description={
                        "suggested_value": self._data.get(CONF_INSULATION_R_VALUE)
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
                vol.Optional(
                    CONF_MAX_TEMPERATURE,
                    description={
                        "suggested_value": self._data.get(
                            CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE
                        )
                    },
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=30,
                        max=100,
                        step=1,
                        unit_of_measurement="°C",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="optional", data_schema=schema)

"""Sensor platform for Buffer Tank Energy integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .calculator import (
    TankGeometry,
    calculate_average_temperature,
    calculate_heat_loss,
    calculate_stored_energy,
    determine_reference_temperature,
)
from .const import (
    CONF_AMBIENT_TEMP_ENTITY,
    CONF_INSULATION_R_VALUE,
    CONF_RETURN_TEMP_ENTITY,
    CONF_SENSOR_ENTITY,
    CONF_SENSOR_POSITION,
    CONF_SENSORS,
    CONF_TANK_HEIGHT,
    CONF_TANK_VOLUME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Buffer Tank Energy sensors from a config entry."""
    data = entry.data
    geometry = TankGeometry(data[CONF_TANK_VOLUME], data[CONF_TANK_HEIGHT])

    # Collect all entity IDs to track
    sensor_configs = data[CONF_SENSORS]
    tracked_entities: list[str] = [s[CONF_SENSOR_ENTITY] for s in sensor_configs]

    return_temp_entity = data.get(CONF_RETURN_TEMP_ENTITY)
    ambient_temp_entity = data.get(CONF_AMBIENT_TEMP_ENTITY)
    r_value = data.get(CONF_INSULATION_R_VALUE)

    if return_temp_entity:
        tracked_entities.append(return_temp_entity)
    if ambient_temp_entity:
        tracked_entities.append(ambient_temp_entity)

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Buffer Tank Energy",
        model=f"{data[CONF_TANK_VOLUME]}L / {data[CONF_TANK_HEIGHT]}mm",
        entry_type=DeviceEntryType.SERVICE,
    )

    entities: list[BufferTankBaseSensor] = [
        BufferTankEnergySensor(
            entry=entry,
            geometry=geometry,
            sensor_configs=sensor_configs,
            return_temp_entity=return_temp_entity,
            ambient_temp_entity=ambient_temp_entity,
            device_info=device_info,
        ),
        BufferTankAverageTemperatureSensor(
            entry=entry,
            geometry=geometry,
            sensor_configs=sensor_configs,
            return_temp_entity=return_temp_entity,
            ambient_temp_entity=ambient_temp_entity,
            device_info=device_info,
        ),
    ]

    # Only add heat loss sensor if both ambient and insulation are configured
    if ambient_temp_entity and r_value:
        entities.append(
            BufferTankHeatLossSensor(
                entry=entry,
                geometry=geometry,
                sensor_configs=sensor_configs,
                ambient_temp_entity=ambient_temp_entity,
                r_value=r_value,
                device_info=device_info,
            )
        )

    async_add_entities(entities)


def _get_float_state(hass: HomeAssistant, entity_id: str) -> float | None:
    """Get the float value of an entity state, or None if unavailable."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


class BufferTankBaseSensor(SensorEntity):
    """Base class for buffer tank sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the base sensor."""
        self._entry = entry
        self._geometry = geometry
        self._sensor_configs = sensor_configs
        self._attr_device_info = device_info
        self._unsub_listeners: list = []

    def _get_sensor_readings(self) -> list[tuple[float, float]]:
        """Get current sensor readings as (position_m, temperature) tuples."""
        readings: list[tuple[float, float]] = []
        for config in self._sensor_configs:
            temp = _get_float_state(self.hass, config[CONF_SENSOR_ENTITY])
            if temp is not None:
                position_m = config[CONF_SENSOR_POSITION] / 1000.0
                readings.append((position_m, temp))
        return readings

    async def async_added_to_hass(self) -> None:
        """Register state change listeners when added to hass."""
        tracked = [c[CONF_SENSOR_ENTITY] for c in self._sensor_configs]
        tracked.extend(self._get_extra_tracked_entities())

        @callback
        def _state_changed(event: Event) -> None:
            """Handle state changes."""
            self._update_state()
            self.async_write_ha_state()

        self._unsub_listeners.append(
            async_track_state_change_event(self.hass, tracked, _state_changed)
        )

        # Initial calculation
        self._update_state()

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listeners."""
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

    def _get_extra_tracked_entities(self) -> list[str]:
        """Return additional entity IDs to track (override in subclasses)."""
        return []

    def _update_state(self) -> None:
        """Update the sensor state (override in subclasses)."""


class BufferTankEnergySensor(BufferTankBaseSensor):
    """Sensor for the stored thermal energy in the buffer tank."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_translation_key = "stored_energy"

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        return_temp_entity: str | None,
        ambient_temp_entity: str | None,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize stored energy sensor."""
        super().__init__(entry, geometry, sensor_configs, device_info)
        self._return_temp_entity = return_temp_entity
        self._ambient_temp_entity = ambient_temp_entity
        self._attr_unique_id = f"{entry.entry_id}_stored_energy"
        self._attr_name = "Stored Energy"

    def _get_extra_tracked_entities(self) -> list[str]:
        """Track return and ambient temp entities."""
        extra: list[str] = []
        if self._return_temp_entity:
            extra.append(self._return_temp_entity)
        if self._ambient_temp_entity:
            extra.append(self._ambient_temp_entity)
        return extra

    def _update_state(self) -> None:
        """Calculate stored energy."""
        readings = self._get_sensor_readings()
        if not readings:
            self._attr_native_value = None
            return

        return_temp = (
            _get_float_state(self.hass, self._return_temp_entity)
            if self._return_temp_entity
            else None
        )
        ambient_temp = (
            _get_float_state(self.hass, self._ambient_temp_entity)
            if self._ambient_temp_entity
            else None
        )

        sensor_temps = [t for _, t in readings]
        ref_temp = determine_reference_temperature(
            return_temp, ambient_temp, sensor_temps
        )

        energy_kwh, profile = calculate_stored_energy(
            self._geometry, readings, ref_temp
        )

        self._attr_native_value = round(energy_kwh, 2)
        self._attr_extra_state_attributes = {
            "reference_temperature": round(ref_temp, 1),
            "temperature_min": round(min(profile), 1) if profile else None,
            "temperature_max": round(max(profile), 1) if profile else None,
            "temperature_avg": (
                round(sum(profile) / len(profile), 1) if profile else None
            ),
            "sensor_count": len(readings),
        }


class BufferTankHeatLossSensor(BufferTankBaseSensor):
    """Sensor for the heat loss power of the buffer tank."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _attr_translation_key = "heat_loss"

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        ambient_temp_entity: str,
        r_value: float,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize heat loss sensor."""
        super().__init__(entry, geometry, sensor_configs, DeviceInfo())
        self._ambient_temp_entity = ambient_temp_entity
        self._r_value = r_value
        self._attr_unique_id = f"{entry.entry_id}_heat_loss"
        self._attr_name = "Heat Loss"
        self._attr_device_info = device_info

    def _get_extra_tracked_entities(self) -> list[str]:
        """Track ambient temp entity."""
        return [self._ambient_temp_entity]

    def _update_state(self) -> None:
        """Calculate heat loss."""
        readings = self._get_sensor_readings()
        if not readings:
            self._attr_native_value = None
            return

        ambient_temp = _get_float_state(self.hass, self._ambient_temp_entity)
        if ambient_temp is None:
            self._attr_native_value = None
            return

        from .calculator import interpolate_temperature_profile

        profile = interpolate_temperature_profile(readings, self._geometry.height_m)
        power_watts = calculate_heat_loss(
            self._geometry, profile, ambient_temp, self._r_value
        )

        self._attr_native_value = round(power_watts, 0)
        self._attr_extra_state_attributes = {
            "ambient_temperature": round(ambient_temp, 1),
            "r_value": self._r_value,
            "surface_area_m2": round(self._geometry.surface_area_m2, 2),
        }


class BufferTankAverageTemperatureSensor(BufferTankBaseSensor):
    """Sensor for the average temperature of the buffer tank."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_translation_key = "average_temperature"

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        return_temp_entity: str | None,
        ambient_temp_entity: str | None,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize average temperature sensor."""
        super().__init__(entry, geometry, sensor_configs, device_info)
        self._attr_unique_id = f"{entry.entry_id}_average_temperature"
        self._attr_name = "Average Temperature"

    def _update_state(self) -> None:
        """Calculate average temperature."""
        readings = self._get_sensor_readings()
        if not readings:
            self._attr_native_value = None
            return

        from .calculator import interpolate_temperature_profile

        profile = interpolate_temperature_profile(readings, self._geometry.height_m)
        avg = calculate_average_temperature(profile)

        self._attr_native_value = round(avg, 1) if avg is not None else None

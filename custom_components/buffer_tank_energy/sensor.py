"""Sensor platform for Buffer Tank Energy integration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .calculator import (
    TankGeometry,
    calculate_average_temperature,
    calculate_heat_loss,
    calculate_max_energy,
    calculate_state_of_charge,
    calculate_stored_energy,
    calculate_temperature_spread,
    determine_reference_temperature,
    interpolate_temperature_profile,
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
    EMA_SMOOTHING_ALPHA,
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
    max_temperature = data.get(CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE)

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
        BufferTankTemperatureSpreadSensor(
            entry=entry,
            geometry=geometry,
            sensor_configs=sensor_configs,
            device_info=device_info,
        ),
        BufferTankStateOfChargeSensor(
            entry=entry,
            geometry=geometry,
            sensor_configs=sensor_configs,
            return_temp_entity=return_temp_entity,
            ambient_temp_entity=ambient_temp_entity,
            max_temperature=max_temperature,
            device_info=device_info,
        ),
        BufferTankChargeDischargePowerSensor(
            entry=entry,
            geometry=geometry,
            sensor_configs=sensor_configs,
            return_temp_entity=return_temp_entity,
            ambient_temp_entity=ambient_temp_entity,
            device_info=device_info,
        ),
    ]

    # Only add heat loss sensors if both ambient and insulation are configured
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
        entities.append(
            BufferTankCumulativeHeatLossSensor(
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
        self._startup_ready = False
        self._required_sensor_count = len(sensor_configs)

    def _get_sensor_readings(self) -> list[tuple[float, float]]:
        """Get current sensor readings as (position_m, temperature) tuples."""
        readings: list[tuple[float, float]] = []
        for config in self._sensor_configs:
            temp = _get_float_state(self.hass, config[CONF_SENSOR_ENTITY])
            if temp is not None:
                position_m = config[CONF_SENSOR_POSITION] / 1000.0
                readings.append((position_m, temp))
        return readings

    def _check_startup_ready(self, readings: list[tuple[float, float]]) -> bool:
        """Check if all configured sensors have reported, marking startup as complete."""
        if self._startup_ready:
            return True
        if len(readings) >= self._required_sensor_count:
            self._startup_ready = True
            return True
        _LOGGER.debug(
            "%s: Waiting for sensors (%d/%d available)",
            self.entity_id,
            len(readings),
            self._required_sensor_count,
        )
        return False

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

        if not self._check_startup_ready(readings):
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

        sensor_positions = {
            config[CONF_SENSOR_ENTITY]: f"{config[CONF_SENSOR_POSITION]} mm"
            for config in self._sensor_configs
        }

        self._attr_extra_state_attributes = {
            "reference_temperature": round(ref_temp, 1),
            "temperature_min": round(min(profile), 1) if profile else None,
            "temperature_max": round(max(profile), 1) if profile else None,
            "temperature_avg": (
                round(sum(profile) / len(profile), 1) if profile else None
            ),
            "sensor_count": len(readings),
            "sensor_positions": sensor_positions,
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
        self._ema_heat_loss: float | None = None

    def _get_extra_tracked_entities(self) -> list[str]:
        """Track ambient temp entity."""
        return [self._ambient_temp_entity]

    def _update_state(self) -> None:
        """Calculate heat loss."""
        readings = self._get_sensor_readings()
        if not readings:
            self._attr_native_value = None
            return

        if not self._check_startup_ready(readings):
            return

        ambient_temp = _get_float_state(self.hass, self._ambient_temp_entity)
        if ambient_temp is None:
            self._attr_native_value = None
            return

        profile = interpolate_temperature_profile(readings, self._geometry.height_m)
        raw_power_watts = calculate_heat_loss(
            self._geometry, profile, ambient_temp, self._r_value
        )

        # Apply EMA smoothing to reduce noise from sensor fluctuations
        if self._ema_heat_loss is None:
            self._ema_heat_loss = raw_power_watts
        else:
            self._ema_heat_loss = (
                EMA_SMOOTHING_ALPHA * raw_power_watts
                + (1 - EMA_SMOOTHING_ALPHA) * self._ema_heat_loss
            )

        self._attr_native_value = round(self._ema_heat_loss, 0)
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

        if not self._check_startup_ready(readings):
            return

        profile = interpolate_temperature_profile(readings, self._geometry.height_m)
        avg = calculate_average_temperature(profile)

        self._attr_native_value = round(avg, 1) if avg is not None else None


class BufferTankTemperatureSpreadSensor(BufferTankBaseSensor):
    """Sensor for the temperature spread (max - min) of the buffer tank."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_translation_key = "temperature_spread"

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        device_info: DeviceInfo,
    ) -> None:
        """Initialize temperature spread sensor."""
        super().__init__(entry, geometry, sensor_configs, device_info)
        self._attr_unique_id = f"{entry.entry_id}_temperature_spread"
        self._attr_name = "Temperature Spread"

    def _update_state(self) -> None:
        """Calculate temperature spread."""
        readings = self._get_sensor_readings()
        if not readings:
            self._attr_native_value = None
            return

        if not self._check_startup_ready(readings):
            return

        profile = interpolate_temperature_profile(readings, self._geometry.height_m)
        spread = calculate_temperature_spread(profile)

        self._attr_native_value = round(spread, 1) if spread is not None else None


class BufferTankStateOfChargeSensor(BufferTankBaseSensor):
    """Sensor for the state of charge (SoC) of the buffer tank."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "state_of_charge"

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        return_temp_entity: str | None,
        ambient_temp_entity: str | None,
        max_temperature: float,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize state of charge sensor."""
        super().__init__(entry, geometry, sensor_configs, device_info)
        self._return_temp_entity = return_temp_entity
        self._ambient_temp_entity = ambient_temp_entity
        self._max_temperature = max_temperature
        self._attr_unique_id = f"{entry.entry_id}_state_of_charge"
        self._attr_name = "State of Charge"

    def _get_extra_tracked_entities(self) -> list[str]:
        """Track return and ambient temp entities."""
        extra: list[str] = []
        if self._return_temp_entity:
            extra.append(self._return_temp_entity)
        if self._ambient_temp_entity:
            extra.append(self._ambient_temp_entity)
        return extra

    def _update_state(self) -> None:
        """Calculate state of charge."""
        readings = self._get_sensor_readings()
        if not readings:
            self._attr_native_value = None
            return

        if not self._check_startup_ready(readings):
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

        energy_kwh, _ = calculate_stored_energy(self._geometry, readings, ref_temp)
        max_energy = calculate_max_energy(
            self._geometry, self._max_temperature, ref_temp
        )
        soc = calculate_state_of_charge(energy_kwh, max_energy)

        self._attr_native_value = round(soc, 1) if soc is not None else None
        self._attr_extra_state_attributes = {
            "max_temperature": self._max_temperature,
            "max_energy_kwh": round(max_energy, 2),
            "current_energy_kwh": round(energy_kwh, 2),
            "reference_temperature": round(ref_temp, 1),
        }


class BufferTankChargeDischargePowerSensor(BufferTankBaseSensor):
    """Sensor for the charge/discharge power of the buffer tank."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2
    _attr_translation_key = "charge_discharge_power"

    MIN_UPDATE_INTERVAL_S = 30

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        return_temp_entity: str | None,
        ambient_temp_entity: str | None,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize charge/discharge power sensor."""
        super().__init__(entry, geometry, sensor_configs, device_info)
        self._return_temp_entity = return_temp_entity
        self._ambient_temp_entity = ambient_temp_entity
        self._attr_unique_id = f"{entry.entry_id}_charge_discharge_power"
        self._attr_name = "Charge/Discharge Power"
        self._previous_energy: float | None = None
        self._previous_timestamp: datetime | None = None
        self._previous_sensor_count: int | None = None
        self._ema_power: float | None = None

    def _get_extra_tracked_entities(self) -> list[str]:
        """Track return and ambient temp entities."""
        extra: list[str] = []
        if self._return_temp_entity:
            extra.append(self._return_temp_entity)
        if self._ambient_temp_entity:
            extra.append(self._ambient_temp_entity)
        return extra

    def _update_state(self) -> None:
        """Calculate charge/discharge power from energy change over time."""
        readings = self._get_sensor_readings()
        if not readings:
            return

        if not self._check_startup_ready(readings):
            return

        # Reset baseline if available sensor count changed to prevent power spikes
        current_sensor_count = len(readings)
        if (
            self._previous_sensor_count is not None
            and current_sensor_count != self._previous_sensor_count
        ):
            self._previous_energy = None
            self._previous_timestamp = None
            self._ema_power = None
        self._previous_sensor_count = current_sensor_count

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

        energy_kwh, _ = calculate_stored_energy(self._geometry, readings, ref_temp)
        now = datetime.now(timezone.utc)

        if self._previous_energy is not None and self._previous_timestamp is not None:
            time_delta_s = (now - self._previous_timestamp).total_seconds()

            if time_delta_s < self.MIN_UPDATE_INTERVAL_S:
                return  # Too soon, keep previous value

            time_delta_hours = time_delta_s / 3600.0
            raw_power_kw = (energy_kwh - self._previous_energy) / time_delta_hours

            # Apply EMA smoothing to reduce noise from sensor fluctuations
            if self._ema_power is None:
                self._ema_power = raw_power_kw
            else:
                self._ema_power = (
                    EMA_SMOOTHING_ALPHA * raw_power_kw
                    + (1 - EMA_SMOOTHING_ALPHA) * self._ema_power
                )

            self._attr_native_value = round(self._ema_power, 2)

        self._previous_energy = energy_kwh
        self._previous_timestamp = now


class BufferTankCumulativeHeatLossSensor(BufferTankBaseSensor, RestoreEntity):
    """Sensor for the cumulative heat loss energy of the buffer tank."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_translation_key = "cumulative_heat_loss"

    MAX_REASONABLE_GAP_S = 3600  # 1 hour

    def __init__(
        self,
        entry: ConfigEntry,
        geometry: TankGeometry,
        sensor_configs: list[dict[str, Any]],
        ambient_temp_entity: str,
        r_value: float,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize cumulative heat loss sensor."""
        super().__init__(entry, geometry, sensor_configs, device_info)
        self._ambient_temp_entity = ambient_temp_entity
        self._r_value = r_value
        self._attr_unique_id = f"{entry.entry_id}_cumulative_heat_loss"
        self._attr_name = "Cumulative Heat Loss"
        self._total_energy_kwh: float = 0.0
        self._last_heat_loss_watts: float | None = None
        self._last_update_time: datetime | None = None

    def _get_extra_tracked_entities(self) -> list[str]:
        """Track ambient temp entity."""
        return [self._ambient_temp_entity]

    async def async_added_to_hass(self) -> None:
        """Restore previous state and set up listeners."""
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._total_energy_kwh = float(last_state.state)
            except (ValueError, TypeError):
                self._total_energy_kwh = 0.0

            last_update = last_state.attributes.get("last_update_time")
            if last_update:
                try:
                    self._last_update_time = datetime.fromisoformat(last_update)
                except (ValueError, TypeError):
                    pass

        await super().async_added_to_hass()

    def _update_state(self) -> None:
        """Accumulate heat loss energy over time."""
        readings = self._get_sensor_readings()
        if not readings:
            return  # Keep accumulated total

        if not self._check_startup_ready(readings):
            return

        ambient_temp = _get_float_state(self.hass, self._ambient_temp_entity)
        if ambient_temp is None:
            return  # Keep accumulated total

        profile = interpolate_temperature_profile(readings, self._geometry.height_m)
        current_heat_loss_watts = calculate_heat_loss(
            self._geometry, profile, ambient_temp, self._r_value
        )

        now = datetime.now(timezone.utc)

        if (
            self._last_update_time is not None
            and self._last_heat_loss_watts is not None
        ):
            time_delta_s = (now - self._last_update_time).total_seconds()

            if 0 < time_delta_s <= self.MAX_REASONABLE_GAP_S:
                avg_power_watts = (
                    self._last_heat_loss_watts + current_heat_loss_watts
                ) / 2.0
                time_delta_hours = time_delta_s / 3600.0
                energy_increment_kwh = avg_power_watts * time_delta_hours / 1000.0
                self._total_energy_kwh += energy_increment_kwh

        self._last_heat_loss_watts = current_heat_loss_watts
        self._last_update_time = now

        self._attr_native_value = round(self._total_energy_kwh, 3)
        self._attr_extra_state_attributes = {
            "current_heat_loss_watts": round(current_heat_loss_watts, 0),
            "last_update_time": now.isoformat(),
        }

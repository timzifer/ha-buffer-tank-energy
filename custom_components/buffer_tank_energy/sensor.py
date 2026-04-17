"""Sensor platform for Buffer Tank Energy integration."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .calculator import calculate_heat_loss, calculate_max_energy, calculate_state_of_charge
from .const import (
    CONF_EMA_SMOOTHING,
    CONF_INSULATION_R_VALUE,
    CONF_MAX_TEMPERATURE,
    CONF_PROBE_EMA_SMOOTHING,
    CONF_PROBE_ENTITY,
    CONF_PROBE_NAME,
    CONF_TANK_HEIGHT,
    CONF_TANK_VOLUME,
    DEFAULT_EMA_SMOOTHING,
    DEFAULT_MAX_TEMPERATURE,
    DEFAULT_PROBE_EMA_SMOOTHING,
    DOMAIN,
    SUBENTRY_PROBE,
)
from .coordinator import BufferTankCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities for the parent entry and for probe subentries."""
    coordinator: BufferTankCoordinator = hass.data[DOMAIN][entry.entry_id]

    device_info = _tank_device_info(entry)

    parent_entities: list[SensorEntity] = [
        BufferTankEnergySensor(coordinator, entry, device_info),
        BufferTankAverageTemperatureSensor(coordinator, entry, device_info),
        BufferTankTemperatureSpreadSensor(coordinator, entry, device_info),
        BufferTankStateOfChargeSensor(coordinator, entry, device_info),
        BufferTankChargeDischargePowerSensor(coordinator, entry, device_info),
        BufferTankStratificationIndexSensor(coordinator, entry, device_info),
        BufferTankStratificationMonotonicitySensor(coordinator, entry, device_info),
        BufferTankGradientConcentrationSensor(coordinator, entry, device_info),
        BufferTankThermoclinePositionSensor(coordinator, entry, device_info),
        BufferTankThermoclineStrengthSensor(coordinator, entry, device_info),
        BufferTankThermoclineThicknessSensor(coordinator, entry, device_info),
        BufferTankThermoclineSharpnessSensor(coordinator, entry, device_info),
    ]

    ambient_entity = coordinator.ambient_temp_entity
    r_value = entry.data.get(CONF_INSULATION_R_VALUE)
    if ambient_entity and r_value:
        parent_entities.append(
            BufferTankHeatLossSensor(coordinator, entry, device_info, r_value)
        )
        parent_entities.append(
            BufferTankCumulativeHeatLossSensor(coordinator, entry, device_info, r_value)
        )

    async_add_entities(parent_entities)

    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_PROBE:
            continue
        if subentry.data.get(CONF_PROBE_ENTITY):
            # Physical probe already exposed by its source entity — no HA entity here.
            continue
        probe_device_info = _probe_device_info(entry, subentry)
        async_add_entities(
            [
                BufferTankVirtualProbeSensor(
                    coordinator, entry, subentry, probe_device_info
                )
            ],
            config_subentry_id=subentry_id,
        )


def _tank_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the shared device info for the tank."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Buffer Tank Energy",
        model=f"{entry.data[CONF_TANK_VOLUME]}L / {entry.data[CONF_TANK_HEIGHT]}mm",
        entry_type=DeviceEntryType.SERVICE,
    )


def _probe_device_info(entry: ConfigEntry, subentry: ConfigSubentry) -> DeviceInfo:
    """Return device info for a virtual-probe subentry, linked to the tank."""
    name = subentry.data.get(CONF_PROBE_NAME) or subentry.title
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_probe_{subentry.subentry_id}")},
        name=name,
        manufacturer="Buffer Tank Energy",
        model="Virtual probe",
        via_device=(DOMAIN, entry.entry_id),
        entry_type=DeviceEntryType.SERVICE,
    )


class _BufferTankEntity(CoordinatorEntity[BufferTankCoordinator], SensorEntity):
    """Common base for parent sensor entities bound to the tank device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        unique_suffix: str,
    ) -> None:
        """Initialize common entity state."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"


class BufferTankEnergySensor(_BufferTankEntity):
    """Stored thermal energy in the buffer tank."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_translation_key = "stored_energy"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the stored-energy sensor."""
        super().__init__(coordinator, entry, device_info, "stored_energy")

    @property
    def native_value(self) -> float | None:
        """Return the stored energy in kWh."""
        data = self.coordinator.data
        if not data or not data.ready:
            return None
        return round(data.energy_kwh, 2)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose temperature-profile statistics."""
        data = self.coordinator.data
        if not data or not data.ready:
            return None
        profile = data.profile
        probe_positions = {
            p.name: f"{int(p.position_m * 1000)} mm"
            for p in self.coordinator.physical_probes
        }
        return {
            "reference_temperature": round(data.ref_temp, 1),
            "temperature_min": round(min(profile), 1) if profile else None,
            "temperature_max": round(max(profile), 1) if profile else None,
            "temperature_avg": (
                round(sum(profile) / len(profile), 1) if profile else None
            ),
            "sensor_count": len(data.readings),
            "probe_positions": probe_positions,
        }


class BufferTankAverageTemperatureSensor(_BufferTankEntity):
    """Average temperature across the 100-layer profile."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_translation_key = "average_temperature"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the average-temperature sensor."""
        super().__init__(coordinator, entry, device_info, "average_temperature")

    @property
    def native_value(self) -> float | None:
        """Return the volume-averaged temperature."""
        data = self.coordinator.data
        if not data or not data.ready or data.avg_temperature is None:
            return None
        return round(data.avg_temperature, 1)


class BufferTankTemperatureSpreadSensor(_BufferTankEntity):
    """Temperature spread (hottest minus coldest layer)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1
    _attr_translation_key = "temperature_spread"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the temperature-spread sensor."""
        super().__init__(coordinator, entry, device_info, "temperature_spread")

    @property
    def native_value(self) -> float | None:
        """Return the temperature spread."""
        data = self.coordinator.data
        if not data or not data.ready or data.spread is None:
            return None
        return round(data.spread, 1)


class BufferTankStateOfChargeSensor(_BufferTankEntity):
    """State of charge relative to a configurable maximum temperature."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "state_of_charge"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the state-of-charge sensor."""
        super().__init__(coordinator, entry, device_info, "state_of_charge")
        self._max_temperature = entry.data.get(
            CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of charge in percent."""
        data = self.coordinator.data
        if not data or not data.ready:
            return None
        max_energy = calculate_max_energy(
            self.coordinator.geometry, self._max_temperature, data.ref_temp
        )
        soc = calculate_state_of_charge(data.energy_kwh, max_energy)
        return round(soc, 1) if soc is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose SoC supporting numbers."""
        data = self.coordinator.data
        if not data or not data.ready:
            return None
        max_energy = calculate_max_energy(
            self.coordinator.geometry, self._max_temperature, data.ref_temp
        )
        return {
            "max_temperature": self._max_temperature,
            "max_energy_kwh": round(max_energy, 2),
            "current_energy_kwh": round(data.energy_kwh, 2),
            "reference_temperature": round(data.ref_temp, 1),
        }


class BufferTankChargeDischargePowerSensor(_BufferTankEntity):
    """Charge/discharge power derived from the energy delta."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_suggested_display_precision = 2
    _attr_translation_key = "charge_discharge_power"

    MIN_UPDATE_INTERVAL_S = 30

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the power sensor."""
        super().__init__(coordinator, entry, device_info, "charge_discharge_power")
        self._ema_alpha = entry.data.get(CONF_EMA_SMOOTHING, DEFAULT_EMA_SMOOTHING)
        self._previous_energy: float | None = None
        self._previous_timestamp: datetime | None = None
        self._previous_sensor_count: int | None = None
        self._ema_power: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Advance the EMA/delta state and expose a new power value."""
        data = self.coordinator.data
        if not data or not data.ready:
            super()._handle_coordinator_update()
            return

        current_count = len(data.readings)
        if (
            self._previous_sensor_count is not None
            and current_count != self._previous_sensor_count
        ):
            self._previous_energy = None
            self._previous_timestamp = None
            self._ema_power = None
        self._previous_sensor_count = current_count

        now = datetime.now(timezone.utc)
        energy_kwh = data.energy_kwh

        if self._previous_energy is not None and self._previous_timestamp is not None:
            time_delta_s = (now - self._previous_timestamp).total_seconds()
            if time_delta_s < self.MIN_UPDATE_INTERVAL_S:
                super()._handle_coordinator_update()
                return

            time_delta_hours = time_delta_s / 3600.0
            raw_power_kw = (energy_kwh - self._previous_energy) / time_delta_hours

            if self._ema_power is None:
                self._ema_power = raw_power_kw
            else:
                self._ema_power = (
                    self._ema_alpha * raw_power_kw
                    + (1 - self._ema_alpha) * self._ema_power
                )
            self._attr_native_value = round(self._ema_power, 2)

        self._previous_energy = energy_kwh
        self._previous_timestamp = now
        super()._handle_coordinator_update()


class BufferTankHeatLossSensor(_BufferTankEntity):
    """Instantaneous heat loss through the tank insulation."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0
    _attr_translation_key = "heat_loss"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        r_value: float,
    ) -> None:
        """Initialize the heat-loss sensor."""
        super().__init__(coordinator, entry, device_info, "heat_loss")
        self._r_value = r_value
        self._ema_alpha = entry.data.get(CONF_EMA_SMOOTHING, DEFAULT_EMA_SMOOTHING)
        self._ema_heat_loss: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Compute a smoothed heat-loss power value."""
        data = self.coordinator.data
        if not data or not data.ready or data.ambient_temp is None:
            self._attr_native_value = None
            super()._handle_coordinator_update()
            return

        raw_power_watts = calculate_heat_loss(
            self.coordinator.geometry, data.profile, data.ambient_temp, self._r_value
        )
        if self._ema_heat_loss is None:
            self._ema_heat_loss = raw_power_watts
        else:
            self._ema_heat_loss = (
                self._ema_alpha * raw_power_watts
                + (1 - self._ema_alpha) * self._ema_heat_loss
            )
        self._attr_native_value = round(self._ema_heat_loss, 0)
        self._attr_extra_state_attributes = {
            "ambient_temperature": round(data.ambient_temp, 1),
            "r_value": self._r_value,
            "surface_area_m2": round(self.coordinator.geometry.surface_area_m2, 2),
        }
        super()._handle_coordinator_update()


class BufferTankCumulativeHeatLossSensor(_BufferTankEntity, RestoreEntity):
    """Heat loss energy accumulated since installation (restored across restarts)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3
    _attr_translation_key = "cumulative_heat_loss"

    MAX_REASONABLE_GAP_S = 3600

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
        r_value: float,
    ) -> None:
        """Initialize the cumulative-heat-loss sensor."""
        super().__init__(coordinator, entry, device_info, "cumulative_heat_loss")
        self._r_value = r_value
        self._total_energy_kwh = 0.0
        self._last_heat_loss_watts: float | None = None
        self._last_update_time: datetime | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous accumulated value."""
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

    @callback
    def _handle_coordinator_update(self) -> None:
        """Accumulate trapezoidal heat-loss energy increments."""
        data = self.coordinator.data
        if not data or not data.ready or data.ambient_temp is None:
            super()._handle_coordinator_update()
            return

        current_heat_loss_watts = calculate_heat_loss(
            self.coordinator.geometry, data.profile, data.ambient_temp, self._r_value
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
                self._total_energy_kwh += avg_power_watts * time_delta_hours / 1000.0

        self._last_heat_loss_watts = current_heat_loss_watts
        self._last_update_time = now

        self._attr_native_value = round(self._total_energy_kwh, 3)
        self._attr_extra_state_attributes = {
            "current_heat_loss_watts": round(current_heat_loss_watts, 0),
            "last_update_time": now.isoformat(),
        }
        super()._handle_coordinator_update()


class BufferTankStratificationIndexSensor(_BufferTankEntity):
    """Composite stratification quality index (0-100 %)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "stratification_index"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the stratification-index sensor."""
        super().__init__(coordinator, entry, device_info, "stratification_index")

    @property
    def native_value(self) -> float | None:
        """Return the stratification index as a percentage."""
        data = self.coordinator.data
        if not data or not data.ready or data.stratification is None:
            return None
        return round(data.stratification.index * 100.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the underlying components of the index."""
        data = self.coordinator.data
        if not data or not data.ready or data.stratification is None:
            return None
        metrics = data.stratification
        return {
            "temperature_span_k": round(metrics.temperature_span_k, 2),
            "span_normalized": round(metrics.span_normalized, 3),
            "monotonicity": round(metrics.monotonicity, 3),
            "gradient_concentration": round(metrics.gradient_concentration, 3),
        }


class BufferTankStratificationMonotonicitySensor(_BufferTankEntity):
    """Monotonicity component of the stratification index (0-100 %)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "stratification_monotonicity"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the monotonicity sensor."""
        super().__init__(coordinator, entry, device_info, "stratification_monotonicity")

    @property
    def native_value(self) -> float | None:
        """Return the monotonicity as a percentage."""
        data = self.coordinator.data
        if not data or not data.ready or data.stratification is None:
            return None
        return round(data.stratification.monotonicity * 100.0, 1)


class BufferTankGradientConcentrationSensor(_BufferTankEntity):
    """Gradient concentration component of the stratification index (0-100 %)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "gradient_concentration"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the gradient-concentration sensor."""
        super().__init__(coordinator, entry, device_info, "gradient_concentration")

    @property
    def native_value(self) -> float | None:
        """Return the gradient concentration as a percentage."""
        data = self.coordinator.data
        if not data or not data.ready or data.stratification is None:
            return None
        return round(data.stratification.gradient_concentration * 100.0, 1)


class BufferTankThermoclinePositionSensor(_BufferTankEntity):
    """Height of the steepest temperature gradient, relative to tank height."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "thermocline_position"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the thermocline-position sensor."""
        super().__init__(coordinator, entry, device_info, "thermocline_position")

    @property
    def native_value(self) -> float | None:
        """Return the thermocline height as a percentage of the tank height."""
        data = self.coordinator.data
        if not data or not data.ready or data.thermocline is None:
            return None
        return round(data.thermocline.position_fraction * 100.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the thermocline position in millimetres."""
        data = self.coordinator.data
        if not data or not data.ready or data.thermocline is None:
            return None
        return {"position_mm": round(data.thermocline.position_m * 1000.0, 0)}


class BufferTankThermoclineStrengthSensor(_BufferTankEntity):
    """Peak vertical temperature gradient (K/m)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/m"
    _attr_suggested_display_precision = 2
    _attr_translation_key = "thermocline_strength"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the thermocline-strength sensor."""
        super().__init__(coordinator, entry, device_info, "thermocline_strength")

    @property
    def native_value(self) -> float | None:
        """Return the peak |dT/dz| in K/m."""
        data = self.coordinator.data
        if not data or not data.ready or data.thermocline is None:
            return None
        return round(data.thermocline.strength_k_per_m, 2)


class BufferTankThermoclineThicknessSensor(_BufferTankEntity):
    """Thickness of the thermocline expressed as a fraction of the tank height."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1
    _attr_translation_key = "thermocline_thickness"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the thermocline-thickness sensor."""
        super().__init__(coordinator, entry, device_info, "thermocline_thickness")

    @property
    def native_value(self) -> float | None:
        """Return the thermocline thickness as a percentage of tank height."""
        data = self.coordinator.data
        if not data or not data.ready or data.thermocline is None:
            return None
        return round(data.thermocline.thickness_fraction * 100.0, 1)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the thermocline thickness in millimetres."""
        data = self.coordinator.data
        if not data or not data.ready or data.thermocline is None:
            return None
        return {"thickness_mm": round(data.thermocline.thickness_m * 1000.0, 0)}


class BufferTankThermoclineSharpnessSensor(_BufferTankEntity):
    """Thermocline sharpness — peak gradient divided by thermocline thickness."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "K/m²"
    _attr_suggested_display_precision = 2
    _attr_translation_key = "thermocline_sharpness"

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize the thermocline-sharpness sensor."""
        super().__init__(coordinator, entry, device_info, "thermocline_sharpness")

    @property
    def native_value(self) -> float | None:
        """Return the thermocline sharpness in K/m²."""
        data = self.coordinator.data
        if (
            not data
            or not data.ready
            or data.thermocline is None
            or data.thermocline.sharpness_k_per_m2 is None
        ):
            return None
        return round(data.thermocline.sharpness_k_per_m2, 2)


class BufferTankVirtualProbeSensor(
    CoordinatorEntity[BufferTankCoordinator], SensorEntity, RestoreEntity
):
    """Virtual probe that reports an interpolated temperature at a fixed height."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize a virtual probe entity for a subentry."""
        super().__init__(coordinator)
        self._subentry_id = subentry.subentry_id
        self._attr_unique_id = f"{entry.entry_id}_probe_{subentry.subentry_id}"
        self._attr_name = None
        self._attr_device_info = device_info
        alpha = subentry.data.get(
            CONF_PROBE_EMA_SMOOTHING, DEFAULT_PROBE_EMA_SMOOTHING
        )
        self._ema_alpha = max(0.0, min(1.0, float(alpha)))
        self._ema_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Seed the EMA with the last state so smoothing survives restarts."""
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._ema_value = float(last_state.state)
            except (ValueError, TypeError):
                self._ema_value = None
        await super().async_added_to_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Advance the EMA toward the new interpolated sample."""
        data = self.coordinator.data
        if data and data.ready:
            raw = data.probe_temps.get(self._subentry_id)
            if raw is not None:
                if self._ema_alpha >= 1.0 or self._ema_value is None:
                    self._ema_value = raw
                else:
                    self._ema_value = (
                        self._ema_alpha * raw
                        + (1.0 - self._ema_alpha) * self._ema_value
                    )
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        """Return the smoothed probe temperature."""
        if self._ema_value is None:
            return None
        return round(self._ema_value, 1)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose the raw (unsmoothed) sample and smoothing factor."""
        data = self.coordinator.data
        raw = (
            data.probe_temps.get(self._subentry_id)
            if data and data.ready
            else None
        )
        return {
            "ema_smoothing": self._ema_alpha,
            "raw_temperature": round(raw, 1) if raw is not None else None,
        }

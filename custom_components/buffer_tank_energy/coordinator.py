"""Coordinator for Buffer Tank Energy integration.

Centralises state tracking and temperature-profile calculation so that all
sensor/binary_sensor entities share a single interpolation pass per update.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .calculator import (
    DEFAULT_STRATIFICATION_REFERENCE_SPAN_K,
    StratificationMetrics,
    TankGeometry,
    ThermoclineMetrics,
    calculate_average_temperature,
    calculate_stored_energy,
    calculate_stratification,
    calculate_temperature_spread,
    calculate_thermocline,
    determine_reference_temperature,
    sample_temperature_profile,
)
from .const import (
    CONF_AMBIENT_TEMP_ENTITY,
    CONF_MAX_TEMPERATURE,
    CONF_PROBE_ENTITY,
    CONF_PROBE_POSITION,
    CONF_PROBE_ROLE,
    CONF_RETURN_TEMP_ENTITY,
    CONF_TANK_HEIGHT,
    CONF_TANK_VOLUME,
    DEFAULT_MAX_TEMPERATURE,
    DEFAULT_PROBE_ROLE,
    DOMAIN,
    PROBE_ROLES,
    SUBENTRY_PROBE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ProbeConfig:
    """Resolved configuration of a single probe subentry."""

    subentry_id: str
    name: str
    position_m: float
    entity_id: str | None  # None = virtual probe (interpolated)
    role: str = DEFAULT_PROBE_ROLE  # "sensor" or "outlet" — display hint


@dataclass
class CoordinatorData:
    """Snapshot of the tank state shared with all entities."""

    readings: list[tuple[float, float]] = field(default_factory=list)
    profile: list[float] = field(default_factory=list)
    avg_temperature: float | None = None
    spread: float | None = None
    ref_temp: float = 0.0
    energy_kwh: float = 0.0
    probe_temps: dict[str, float] = field(default_factory=dict)
    return_temp: float | None = None
    ambient_temp: float | None = None
    stratification: StratificationMetrics | None = None
    thermocline: ThermoclineMetrics | None = None
    ready: bool = False


class BufferTankCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Keeps the tank state in sync with Home Assistant's state machine."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=5),
        )
        self.entry = entry
        self.geometry = TankGeometry(
            entry.data[CONF_TANK_VOLUME], entry.data[CONF_TANK_HEIGHT]
        )
        self._return_temp_entity: str | None = entry.data.get(CONF_RETURN_TEMP_ENTITY)
        self._ambient_temp_entity: str | None = entry.data.get(CONF_AMBIENT_TEMP_ENTITY)
        self._max_temperature: float = entry.data.get(
            CONF_MAX_TEMPERATURE, DEFAULT_MAX_TEMPERATURE
        )
        self.probes: list[ProbeConfig] = self._load_probes()
        self._startup_ready = False

    def _load_probes(self) -> list[ProbeConfig]:
        """Read probe configurations from the entry's subentries."""
        probes: list[ProbeConfig] = []
        for subentry_id, subentry in self.entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_PROBE:
                continue
            data = subentry.data
            position_mm = float(data[CONF_PROBE_POSITION])
            entity_id = data.get(CONF_PROBE_ENTITY) or None
            role = data.get(CONF_PROBE_ROLE, DEFAULT_PROBE_ROLE)
            if role not in PROBE_ROLES:
                role = DEFAULT_PROBE_ROLE
            probes.append(
                ProbeConfig(
                    subentry_id=subentry_id,
                    name=subentry.title,
                    position_m=position_mm / 1000.0,
                    entity_id=entity_id,
                    role=role,
                )
            )
        probes.sort(key=lambda p: p.position_m)
        return probes

    @property
    def physical_probes(self) -> list[ProbeConfig]:
        """Return only probes that reference a real HA entity."""
        return [p for p in self.probes if p.entity_id]

    @property
    def return_temp_entity(self) -> str | None:
        """Return the configured return-temperature entity id."""
        return self._return_temp_entity

    @property
    def ambient_temp_entity(self) -> str | None:
        """Return the configured ambient-temperature entity id."""
        return self._ambient_temp_entity

    async def async_config_entry_first_refresh(self) -> None:
        """Register state listeners and do the first refresh."""
        tracked: list[str] = [p.entity_id for p in self.physical_probes]
        if self._return_temp_entity:
            tracked.append(self._return_temp_entity)
        if self._ambient_temp_entity:
            tracked.append(self._ambient_temp_entity)

        if tracked:
            @callback
            def _state_changed(event: Event) -> None:
                self.hass.async_create_task(self.async_request_refresh())

            self.entry.async_on_unload(
                async_track_state_change_event(self.hass, tracked, _state_changed)
            )

        await super().async_config_entry_first_refresh()

    async def _async_update_data(self) -> CoordinatorData:
        """Recompute the shared tank state."""
        readings: list[tuple[float, float]] = []
        for probe in self.physical_probes:
            temp = _get_float_state(self.hass, probe.entity_id)  # type: ignore[arg-type]
            if temp is not None:
                readings.append((probe.position_m, temp))

        required = len(self.physical_probes)
        if not self._startup_ready:
            if required > 0 and len(readings) >= required:
                self._startup_ready = True
            else:
                _LOGGER.debug(
                    "Waiting for probe sensors (%d/%d available)",
                    len(readings),
                    required,
                )

        ready = self._startup_ready and len(readings) >= 2

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

        if not ready:
            return CoordinatorData(
                readings=readings,
                return_temp=return_temp,
                ambient_temp=ambient_temp,
                ready=False,
            )

        sensor_temps = [t for _, t in readings]
        ref_temp = determine_reference_temperature(
            return_temp, ambient_temp, sensor_temps
        )
        energy_kwh, profile = calculate_stored_energy(
            self.geometry, readings, ref_temp
        )

        samples = sample_temperature_profile(readings, self.geometry.height_m)
        stratification: StratificationMetrics | None = None
        thermocline: ThermoclineMetrics | None = None
        if samples is not None:
            reference_span = self._max_temperature - ref_temp
            if reference_span <= 0:
                reference_span = DEFAULT_STRATIFICATION_REFERENCE_SPAN_K
            stratification = calculate_stratification(samples, reference_span)
            thermocline = calculate_thermocline(samples)

        probe_temps: dict[str, float] = {}
        layer_height = self.geometry.height_m / len(profile) if profile else 0.0
        for probe in self.probes:
            if probe.entity_id:
                temp = _get_float_state(self.hass, probe.entity_id)
                if temp is not None:
                    probe_temps[probe.subentry_id] = temp
            elif profile and layer_height > 0:
                layer_index = min(
                    int(probe.position_m / layer_height), len(profile) - 1
                )
                layer_index = max(layer_index, 0)
                probe_temps[probe.subentry_id] = profile[layer_index]

        return CoordinatorData(
            readings=readings,
            profile=profile,
            avg_temperature=calculate_average_temperature(profile),
            spread=calculate_temperature_spread(profile),
            ref_temp=ref_temp,
            energy_kwh=energy_kwh,
            probe_temps=probe_temps,
            return_temp=return_temp,
            ambient_temp=ambient_temp,
            stratification=stratification,
            thermocline=thermocline,
            ready=True,
        )


def _get_float_state(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read an HA entity state as float, or None if unavailable."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None

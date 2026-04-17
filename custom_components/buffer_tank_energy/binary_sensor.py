"""Binary sensor platform for Buffer Tank Energy threshold subentries."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_THRESHOLD_HYSTERESIS,
    CONF_THRESHOLD_MIN_TEMP,
    CONF_THRESHOLD_NAME,
    CONF_THRESHOLD_PROBE_ID,
    DEFAULT_THRESHOLD_HYSTERESIS,
    DOMAIN,
    SUBENTRY_PROBE,
    SUBENTRY_THRESHOLD,
)
from .coordinator import BufferTankCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create binary sensors for threshold subentries."""
    coordinator: BufferTankCoordinator = hass.data[DOMAIN][entry.entry_id]

    probe_subentries = {
        sid
        for sid, sub in entry.subentries.items()
        if sub.subentry_type == SUBENTRY_PROBE
    }

    for subentry_id, subentry in entry.subentries.items():
        if subentry.subentry_type != SUBENTRY_THRESHOLD:
            continue
        probe_id = subentry.data.get(CONF_THRESHOLD_PROBE_ID)
        if probe_id not in probe_subentries:
            _LOGGER.warning(
                "Threshold subentry %s references missing probe %s — "
                "entity will be unavailable",
                subentry.title,
                probe_id,
            )
        async_add_entities(
            [
                BufferTankThresholdSensor(
                    coordinator,
                    entry,
                    subentry,
                    _threshold_device_info(entry, subentry),
                )
            ],
            config_subentry_id=subentry_id,
        )


def _threshold_device_info(
    entry: ConfigEntry, subentry: ConfigSubentry
) -> DeviceInfo:
    """Return device info for a threshold subentry, linked to the tank."""
    name = subentry.data.get(CONF_THRESHOLD_NAME) or subentry.title
    return DeviceInfo(
        identifiers={
            (DOMAIN, f"{entry.entry_id}_threshold_{subentry.subentry_id}")
        },
        name=name,
        manufacturer="Buffer Tank Energy",
        model="Threshold",
        via_device=(DOMAIN, entry.entry_id),
        entry_type=DeviceEntryType.SERVICE,
    )


class BufferTankThresholdSensor(
    CoordinatorEntity[BufferTankCoordinator], BinarySensorEntity, RestoreEntity
):
    """On when the referenced probe temperature is above the configured minimum."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BufferTankCoordinator,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        device_info: DeviceInfo,
    ) -> None:
        """Initialize a threshold entity."""
        super().__init__(coordinator)
        data = subentry.data
        self._probe_id: str = data[CONF_THRESHOLD_PROBE_ID]
        self._min_temp: float = float(data[CONF_THRESHOLD_MIN_TEMP])
        self._hysteresis: float = float(
            data.get(CONF_THRESHOLD_HYSTERESIS, DEFAULT_THRESHOLD_HYSTERESIS)
        )
        self._attr_unique_id = f"{entry.entry_id}_threshold_{subentry.subentry_id}"
        self._attr_name = None
        self._attr_device_info = device_info
        self._is_on: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous on/off state before the first update."""
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self._is_on = last_state.state == "on"
        await super().async_added_to_hass()

    @property
    def available(self) -> bool:
        """Available while the coordinator is healthy and the probe has a value."""
        if not super().available:
            return False
        data = self.coordinator.data
        if not data or not data.ready:
            return False
        return self._probe_id in data.probe_temps

    @property
    def is_on(self) -> bool | None:
        """Return the latched threshold state."""
        return self._is_on

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose threshold parameters and current temperature."""
        data = self.coordinator.data
        current = (
            data.probe_temps.get(self._probe_id)
            if data and data.probe_temps
            else None
        )
        return {
            "min_temperature": self._min_temp,
            "hysteresis": self._hysteresis,
            "current_temperature": (
                round(current, 1) if current is not None else None
            ),
            "probe_subentry_id": self._probe_id,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Apply hysteresis based on the referenced probe temperature."""
        data = self.coordinator.data
        if data and data.ready:
            temp = data.probe_temps.get(self._probe_id)
            if temp is not None:
                if self._is_on is None:
                    self._is_on = temp >= self._min_temp
                elif self._is_on and temp < self._min_temp - self._hysteresis:
                    self._is_on = False
                elif not self._is_on and temp >= self._min_temp:
                    self._is_on = True
        super()._handle_coordinator_update()

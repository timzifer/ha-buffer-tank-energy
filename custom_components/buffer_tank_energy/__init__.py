"""The Buffer Tank Energy integration."""

from __future__ import annotations

import logging
import uuid

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_PROBE_ENTITY,
    CONF_PROBE_NAME,
    CONF_PROBE_POSITION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    LEGACY_CONF_SENSOR_ENTITY,
    LEGACY_CONF_SENSOR_POSITION,
    LEGACY_CONF_SENSORS,
    SUBENTRY_PROBE,
)
from .coordinator import BufferTankCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Buffer Tank Energy from a config entry."""
    coordinator = BufferTankCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    _detach_tank_from_subentries(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


def _detach_tank_from_subentries(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Strip stale subentry references from the tank device.

    Older versions of this integration registered probe/threshold entities
    against the tank device itself. Those device↔subentry links persist in
    the device registry even after the entities move to per-subentry child
    devices, which makes the UI show the tank device under every subentry
    and risks it being deleted when a subentry is removed.
    """
    device_reg = dr.async_get(hass)
    device = device_reg.async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    if device is None:
        return
    linked = device.config_entries_subentries.get(entry.entry_id, set())
    for subentry_id in linked - {None}:
        device_reg.async_update_device(
            device.id,
            remove_config_entry_id=entry.entry_id,
            remove_config_subentry_id=subentry_id,
        )
        _LOGGER.debug(
            "Detached tank device %s from stale subentry %s",
            device.id,
            subentry_id,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy (v1) config entries to the subentry-based layout."""
    if entry.version >= CONFIG_ENTRY_VERSION:
        return True

    _LOGGER.info(
        "Migrating Buffer Tank Energy entry %s from v%d to v%d",
        entry.entry_id,
        entry.version,
        CONFIG_ENTRY_VERSION,
    )

    legacy_sensors = list(entry.data.get(LEGACY_CONF_SENSORS, []))
    new_data = {k: v for k, v in entry.data.items() if k != LEGACY_CONF_SENSORS}

    hass.config_entries.async_update_entry(
        entry, data=new_data, version=CONFIG_ENTRY_VERSION
    )

    for index, sensor in enumerate(legacy_sensors, start=1):
        entity_id = sensor.get(LEGACY_CONF_SENSOR_ENTITY)
        position = sensor.get(LEGACY_CONF_SENSOR_POSITION)
        title = f"Probe {index}"
        subentry = ConfigSubentry(
            data={
                CONF_PROBE_NAME: title,
                CONF_PROBE_POSITION: position,
                CONF_PROBE_ENTITY: entity_id,
            },
            subentry_id=uuid.uuid4().hex,
            subentry_type=SUBENTRY_PROBE,
            title=title,
            unique_id=None,
        )
        hass.config_entries.async_add_subentry(entry, subentry)

    _LOGGER.info(
        "Migrated %d legacy sensor(s) into probe subentries", len(legacy_sensors)
    )
    return True

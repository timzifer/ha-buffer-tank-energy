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

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


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
    """Migrate older config entries to the current layout."""
    if entry.version >= CONFIG_ENTRY_VERSION:
        return True

    _LOGGER.info(
        "Migrating Buffer Tank Energy entry %s from v%d to v%d",
        entry.entry_id,
        entry.version,
        CONFIG_ENTRY_VERSION,
    )

    if entry.version < 2:
        _migrate_v1_to_v2(hass, entry)

    if entry.version < 3:
        _migrate_v2_to_v3(hass, entry)

    hass.config_entries.async_update_entry(entry, version=CONFIG_ENTRY_VERSION)
    return True


def _migrate_v1_to_v2(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move the legacy sensor list into probe subentries."""
    legacy_sensors = list(entry.data.get(LEGACY_CONF_SENSORS, []))
    new_data = {k: v for k, v in entry.data.items() if k != LEGACY_CONF_SENSORS}
    hass.config_entries.async_update_entry(entry, data=new_data)

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


def _migrate_v2_to_v3(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Detach the tank device from any probe/threshold subentries.

    Older releases registered virtual-probe and threshold entities using the
    tank's DeviceInfo while still passing ``config_subentry_id``. Home
    Assistant then linked the shared tank device to each of those subentries.
    Deleting the subentry afterwards would try to remove the tank device as
    well. This migration drops every subentry link from the tank device so
    only the current child-device links remain.
    """
    dev_reg = dr.async_get(hass)
    tank_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )
    if tank_device is None:
        return

    stale = [
        sid
        for sid in tank_device.config_entries_subentries.get(entry.entry_id, set())
        if sid is not None
    ]
    for subentry_id in stale:
        dev_reg.async_update_device(
            tank_device.id,
            remove_config_entry_id=entry.entry_id,
            remove_config_subentry_id=subentry_id,
        )

    if stale:
        _LOGGER.info(
            "Detached tank device from %d stale subentry association(s)",
            len(stale),
        )

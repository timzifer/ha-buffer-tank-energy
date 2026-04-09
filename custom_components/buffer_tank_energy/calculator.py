"""Physics calculations for buffer tank energy."""

from __future__ import annotations

import math

from .const import KJ_TO_KWH, NUM_LAYERS, WATER_DENSITY, WATER_SPECIFIC_HEAT


class TankGeometry:
    """Represents the physical geometry of a cylindrical buffer tank."""

    def __init__(self, volume_liters: float, height_mm: float) -> None:
        """Initialize tank geometry.

        Args:
            volume_liters: Tank volume in liters.
            height_mm: Tank height in millimeters.
        """
        self.volume_m3 = volume_liters / 1000.0
        self.height_m = height_mm / 1000.0
        self.radius_m = math.sqrt(self.volume_m3 / (math.pi * self.height_m))
        self.cross_section_m2 = math.pi * self.radius_m**2
        self.surface_area_m2 = (
            2 * math.pi * self.radius_m * self.height_m  # Mantel
            + 2 * math.pi * self.radius_m**2  # Ober- + Unterseite
        )


def interpolate_temperature_profile(
    sensors: list[tuple[float, float]],
    tank_height_m: float,
    num_layers: int = NUM_LAYERS,
) -> list[float]:
    """Build a temperature profile by interpolating between sensors.

    Sensors below the lowest sensor position use the lowest sensor temperature.
    Sensors above the highest sensor position use the highest sensor temperature.
    Between sensors, linear interpolation is used.

    Args:
        sensors: List of (position_m, temperature_celsius) sorted by position.
        tank_height_m: Total tank height in meters.
        num_layers: Number of discrete layers.

    Returns:
        List of temperatures for each layer (bottom to top).
    """
    if not sensors:
        return [0.0] * num_layers

    sorted_sensors = sorted(sensors, key=lambda s: s[0])
    layer_height = tank_height_m / num_layers
    temperatures: list[float] = []

    for i in range(num_layers):
        # Center height of this layer
        h = (i + 0.5) * layer_height

        # Below lowest sensor: use lowest sensor temperature
        if h <= sorted_sensors[0][0]:
            temperatures.append(sorted_sensors[0][1])
            continue

        # Above highest sensor: use highest sensor temperature
        if h >= sorted_sensors[-1][0]:
            temperatures.append(sorted_sensors[-1][1])
            continue

        # Between two sensors: linear interpolation
        for j in range(len(sorted_sensors) - 1):
            pos_low, temp_low = sorted_sensors[j]
            pos_high, temp_high = sorted_sensors[j + 1]
            if pos_low <= h <= pos_high:
                if pos_high == pos_low:
                    temperatures.append(temp_low)
                else:
                    fraction = (h - pos_low) / (pos_high - pos_low)
                    temp = temp_low + fraction * (temp_high - temp_low)
                    temperatures.append(temp)
                break

    return temperatures


def calculate_stored_energy(
    geometry: TankGeometry,
    sensors: list[tuple[float, float]],
    reference_temp: float,
) -> tuple[float, list[float]]:
    """Calculate the total stored thermal energy in the buffer tank.

    Args:
        geometry: Tank geometry.
        sensors: List of (position_m, temperature_celsius).
        reference_temp: Reference temperature in celsius for energy calculation.

    Returns:
        Tuple of (energy_kwh, temperature_profile).
    """
    profile = interpolate_temperature_profile(sensors, geometry.height_m)
    layer_height = geometry.height_m / NUM_LAYERS
    layer_volume = geometry.cross_section_m2 * layer_height  # m³
    layer_mass = layer_volume * WATER_DENSITY  # kg

    total_energy_kj = 0.0
    for temp in profile:
        delta_t = temp - reference_temp
        if delta_t > 0:
            total_energy_kj += layer_mass * WATER_SPECIFIC_HEAT * delta_t

    energy_kwh = total_energy_kj * KJ_TO_KWH
    return energy_kwh, profile


def calculate_heat_loss(
    geometry: TankGeometry,
    temperature_profile: list[float],
    ambient_temp: float,
    r_value: float,
) -> float:
    """Calculate heat loss power through insulation.

    Args:
        geometry: Tank geometry.
        temperature_profile: List of layer temperatures.
        ambient_temp: Ambient temperature in celsius.
        r_value: Thermal resistance in m²·K/W.

    Returns:
        Heat loss power in watts.
    """
    if not temperature_profile or r_value <= 0:
        return 0.0

    avg_temp = sum(temperature_profile) / len(temperature_profile)
    delta_t = avg_temp - ambient_temp

    if delta_t <= 0:
        return 0.0

    # P = ΔT × A / R  (Watts)
    power_watts = delta_t * geometry.surface_area_m2 / r_value
    return power_watts


def calculate_average_temperature(temperature_profile: list[float]) -> float | None:
    """Calculate the average temperature from the profile.

    Args:
        temperature_profile: List of layer temperatures.

    Returns:
        Average temperature in celsius, or None if profile is empty.
    """
    if not temperature_profile:
        return None
    return sum(temperature_profile) / len(temperature_profile)


def calculate_max_energy(
    geometry: TankGeometry,
    max_temperature: float,
    reference_temp: float,
) -> float:
    """Calculate the maximum storable energy at a given max temperature.

    Args:
        geometry: Tank geometry.
        max_temperature: Maximum water temperature in celsius.
        reference_temp: Reference temperature in celsius.

    Returns:
        Maximum energy in kWh.
    """
    delta_t = max_temperature - reference_temp
    if delta_t <= 0:
        return 0.0

    total_mass = geometry.volume_m3 * WATER_DENSITY  # kg
    total_energy_kj = total_mass * WATER_SPECIFIC_HEAT * delta_t
    return total_energy_kj * KJ_TO_KWH


def calculate_state_of_charge(
    current_energy: float, max_energy: float
) -> float | None:
    """Calculate the state of charge as a percentage.

    Args:
        current_energy: Current stored energy in kWh.
        max_energy: Maximum storable energy in kWh.

    Returns:
        State of charge in percent (0-100), or None if max_energy is zero.
    """
    if max_energy <= 0:
        return None
    soc = (current_energy / max_energy) * 100.0
    return max(0.0, min(100.0, soc))


def calculate_temperature_spread(temperature_profile: list[float]) -> float | None:
    """Calculate the temperature spread (max - min) of the profile.

    Args:
        temperature_profile: List of layer temperatures.

    Returns:
        Temperature spread in celsius, or None if profile is empty.
    """
    if not temperature_profile:
        return None
    return max(temperature_profile) - min(temperature_profile)


def determine_reference_temperature(
    return_temp: float | None,
    ambient_temp: float | None,
    sensor_temperatures: list[float],
) -> float:
    """Determine the reference temperature for energy calculation.

    Priority: return temp > ambient temp > minimum sensor temperature.

    Args:
        return_temp: Return water temperature if available.
        ambient_temp: Ambient temperature if available.
        sensor_temperatures: List of all sensor temperatures.

    Returns:
        Reference temperature in celsius.
    """
    if return_temp is not None:
        return return_temp
    if ambient_temp is not None:
        return ambient_temp
    if sensor_temperatures:
        return min(sensor_temperatures)
    return 0.0

"""Physics calculations for buffer tank energy."""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from .const import KJ_TO_KWH, NUM_LAYERS, WATER_DENSITY, WATER_SPECIFIC_HEAT

# Number of spline samples used to derive stratification/thermocline metrics.
PROFILE_SAMPLE_COUNT = 201

# Fraction of peak |dT/dz| used to delimit the thermocline region.
THERMOCLINE_WIDTH_ALPHA = 0.5

# Minimum top-to-bottom temperature span before a thermocline is reported.
MIN_THERMOCLINE_SPAN_K = 0.5

# Fallback reference span (K) used to normalise ΔT when no other basis exists.
DEFAULT_STRATIFICATION_REFERENCE_SPAN_K = 30.0

# Lower bound applied to the stratification reference span to avoid blow-up.
MIN_STRATIFICATION_REFERENCE_SPAN_K = 10.0


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


def _dedupe_sorted_sensors(
    sensors: list[tuple[float, float]],
) -> tuple[list[float], list[float]]:
    """Sort sensors by position and merge duplicates by averaging temperature."""
    buckets: dict[float, list[float]] = {}
    for pos, temp in sensors:
        buckets.setdefault(pos, []).append(temp)
    xs = sorted(buckets.keys())
    ys = [sum(buckets[x]) / len(buckets[x]) for x in xs]
    return xs, ys


def _pchip_tangents(xs: list[float], ys: list[float]) -> list[float]:
    """Compute Fritsch–Carlson monotone cubic Hermite tangents at each knot.

    The slopes are chosen so that the resulting piecewise cubic Hermite
    interpolant is monotone on every segment where the data is monotone,
    which eliminates the overshoot that a natural cubic spline can produce
    across a sharp jump (e.g. a thermocline between two probes).

    Reference: F. N. Fritsch and R. E. Carlson, "Monotone piecewise cubic
    interpolation", SIAM J. Numer. Anal. 17(2), 1980.
    """
    n = len(xs)
    if n < 2:
        return [0.0] * n

    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    delta = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]

    if n == 2:
        return [delta[0], delta[0]]

    tangents = [0.0] * n

    for i in range(1, n - 1):
        d_prev = delta[i - 1]
        d_next = delta[i]
        if d_prev * d_next <= 0.0:
            # Sign change or flat segment → local extremum, enforce zero slope.
            tangents[i] = 0.0
        else:
            w1 = 2.0 * h[i] + h[i - 1]
            w2 = h[i] + 2.0 * h[i - 1]
            tangents[i] = (w1 + w2) / (w1 / d_prev + w2 / d_next)

    # One-sided, monotonicity-safe estimate at the left boundary.
    t0 = ((2.0 * h[0] + h[1]) * delta[0] - h[0] * delta[1]) / (h[0] + h[1])
    if t0 * delta[0] <= 0.0:
        t0 = 0.0
    elif delta[0] * delta[1] <= 0.0 and abs(t0) > abs(3.0 * delta[0]):
        t0 = 3.0 * delta[0]
    tangents[0] = t0

    # One-sided, monotonicity-safe estimate at the right boundary.
    tn = (
        (2.0 * h[-1] + h[-2]) * delta[-1] - h[-1] * delta[-2]
    ) / (h[-1] + h[-2])
    if tn * delta[-1] <= 0.0:
        tn = 0.0
    elif delta[-1] * delta[-2] <= 0.0 and abs(tn) > abs(3.0 * delta[-1]):
        tn = 3.0 * delta[-1]
    tangents[-1] = tn

    return tangents


def _eval_hermite_segment(
    x: float,
    i: int,
    xs: list[float],
    ys: list[float],
    tangents: list[float],
) -> float:
    """Evaluate the cubic Hermite polynomial on segment [xs[i], xs[i+1]]."""
    h = xs[i + 1] - xs[i]
    t = (x - xs[i]) / h
    t2 = t * t
    t3 = t2 * t
    h00 = 2.0 * t3 - 3.0 * t2 + 1.0
    h10 = t3 - 2.0 * t2 + t
    h01 = -2.0 * t3 + 3.0 * t2
    h11 = t3 - t2
    return (
        h00 * ys[i]
        + h10 * h * tangents[i]
        + h01 * ys[i + 1]
        + h11 * h * tangents[i + 1]
    )


def _eval_hermite_segment_derivative(
    x: float,
    i: int,
    xs: list[float],
    ys: list[float],
    tangents: list[float],
) -> float:
    """Analytic first derivative of the cubic Hermite polynomial."""
    h = xs[i + 1] - xs[i]
    t = (x - xs[i]) / h
    dh00 = 6.0 * t * t - 6.0 * t
    dh10 = 3.0 * t * t - 4.0 * t + 1.0
    dh01 = -6.0 * t * t + 6.0 * t
    dh11 = 3.0 * t * t - 2.0 * t
    return (
        dh00 * ys[i] / h
        + dh10 * tangents[i]
        + dh01 * ys[i + 1] / h
        + dh11 * tangents[i + 1]
    )


def interpolate_temperature_profile(
    sensors: list[tuple[float, float]],
    tank_height_m: float,
    num_layers: int = NUM_LAYERS,
) -> list[float]:
    """Build a temperature profile using Fritsch–Carlson monotone PCHIP.

    The interpolant is C¹-continuous and monotone on every segment where the
    data is monotone, so it cannot overshoot between two probes (unlike a
    natural cubic spline, which can generate unphysical peaks across a sharp
    thermocline). Outside the sensor range the profile is clamped to the
    nearest probe temperature, matching the physical assumption that the
    topmost and bottommost layers of a stratified tank sit at approximately
    the temperature of the closest probe.

    Args:
        sensors: List of (position_m, temperature_celsius).
        tank_height_m: Total tank height in meters.
        num_layers: Number of discrete layers.

    Returns:
        List of temperatures for each layer (bottom to top).
    """
    if not sensors:
        return [0.0] * num_layers

    xs, ys = _dedupe_sorted_sensors(sensors)
    n = len(xs)
    layer_height = tank_height_m / num_layers

    if n == 1:
        return [ys[0]] * num_layers

    tangents = _pchip_tangents(xs, ys)

    temperatures: list[float] = []
    for i in range(num_layers):
        h = (i + 0.5) * layer_height
        if h <= xs[0]:
            temperatures.append(ys[0])
        elif h >= xs[-1]:
            temperatures.append(ys[-1])
        else:
            # bisect_right returns insertion index; segment index is idx-1
            idx = bisect.bisect_right(xs, h) - 1
            if idx >= n - 1:
                idx = n - 2
            temperatures.append(
                _eval_hermite_segment(h, idx, xs, ys, tangents)
            )

    return temperatures


@dataclass
class TemperatureSamples:
    """Uniform samples of the temperature spline and its first derivative."""

    positions_m: list[float]
    temperatures: list[float]
    gradients_k_per_m: list[float]
    tank_height_m: float


def sample_temperature_profile(
    sensors: list[tuple[float, float]],
    tank_height_m: float,
    num_samples: int = PROFILE_SAMPLE_COUNT,
) -> TemperatureSamples | None:
    """Sample T(z) and dT/dz uniformly along the tank height.

    Uses the same PCHIP interpolant as ``interpolate_temperature_profile``
    on a finer grid and returns the analytic derivative so that
    stratification and thermocline metrics are consistent with the rendered
    profile. Outside the sensor range the profile is clamped (gradient = 0)
    to avoid unphysical extrapolation.
    """
    if not sensors or tank_height_m <= 0 or num_samples < 2:
        return None

    xs, ys = _dedupe_sorted_sensors(sensors)
    n = len(xs)
    if n == 0:
        return None

    dz = tank_height_m / (num_samples - 1)
    positions = [i * dz for i in range(num_samples)]

    if n == 1:
        return TemperatureSamples(
            positions_m=positions,
            temperatures=[ys[0]] * num_samples,
            gradients_k_per_m=[0.0] * num_samples,
            tank_height_m=tank_height_m,
        )

    tangents = _pchip_tangents(xs, ys)

    temperatures: list[float] = []
    gradients: list[float] = []
    for z in positions:
        if z <= xs[0]:
            temperatures.append(ys[0])
            gradients.append(0.0)
        elif z >= xs[-1]:
            temperatures.append(ys[-1])
            gradients.append(0.0)
        else:
            idx = bisect.bisect_right(xs, z) - 1
            if idx >= n - 1:
                idx = n - 2
            temperatures.append(
                _eval_hermite_segment(z, idx, xs, ys, tangents)
            )
            gradients.append(
                _eval_hermite_segment_derivative(z, idx, xs, ys, tangents)
            )

    return TemperatureSamples(
        positions_m=positions,
        temperatures=temperatures,
        gradients_k_per_m=gradients,
        tank_height_m=tank_height_m,
    )


def _trapezoid(values: list[float], dz: float) -> float:
    """Simple trapezoidal integration over a uniform grid."""
    if len(values) < 2:
        return 0.0
    total = 0.5 * (values[0] + values[-1])
    for v in values[1:-1]:
        total += v
    return total * dz


@dataclass
class StratificationMetrics:
    """Composite and component metrics describing the tank stratification."""

    index: float  # 0..1 — weighted combination of the components
    span_normalized: float  # 0..1 — top-bottom spread vs reference span
    monotonicity: float  # 0..1 — 1 = strictly increasing with z
    gradient_concentration: float  # 0..1 — 0 = uniform, 1 = delta-like peak
    temperature_span_k: float  # T(H) - T(0)


def calculate_stratification(
    samples: TemperatureSamples,
    reference_span_k: float,
    weights: tuple[float, float, float] = (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0),
) -> StratificationMetrics | None:
    """Compute the stratification index and its three components.

    The tank convention is z=0 at the bottom and z=H at the top, so a well
    stratified tank has dT/dz >= 0 and T(H) > T(0).
    """
    positions = samples.positions_m
    gradients = samples.gradients_k_per_m
    temperatures = samples.temperatures
    if len(positions) < 2:
        return None

    dz = positions[1] - positions[0]
    if dz <= 0:
        return None

    temperature_span = temperatures[-1] - temperatures[0]

    reference = max(reference_span_k, MIN_STRATIFICATION_REFERENCE_SPAN_K)
    span_normalized = max(0.0, min(temperature_span / reference, 1.0))

    abs_grad = [abs(g) for g in gradients]
    wrong_way = [max(0.0, -g) for g in gradients]
    total_abs = _trapezoid(abs_grad, dz)
    if total_abs <= 1e-9:
        monotonicity = 1.0
        gradient_concentration = 0.0
    else:
        inversion_integral = _trapezoid(wrong_way, dz)
        monotonicity = 1.0 - inversion_integral / total_abs
        monotonicity = max(0.0, min(monotonicity, 1.0))
        mean_abs = total_abs / (positions[-1] - positions[0])
        max_abs = max(abs_grad)
        gradient_concentration = (
            (max_abs - mean_abs) / max_abs if max_abs > 0 else 0.0
        )
        gradient_concentration = max(0.0, min(gradient_concentration, 1.0))

    w1, w2, w3 = weights
    total_weight = w1 + w2 + w3
    if total_weight <= 0:
        return None
    index = (
        w1 * span_normalized + w2 * monotonicity + w3 * gradient_concentration
    ) / total_weight

    return StratificationMetrics(
        index=max(0.0, min(index, 1.0)),
        span_normalized=span_normalized,
        monotonicity=monotonicity,
        gradient_concentration=gradient_concentration,
        temperature_span_k=temperature_span,
    )


@dataclass
class ThermoclineMetrics:
    """Location, intensity and extent of the steepest gradient zone."""

    position_m: float
    position_fraction: float  # 0..1 of tank height
    strength_k_per_m: float  # |dT/dz| at the peak
    thickness_m: float
    thickness_fraction: float
    sharpness_k_per_m2: float | None


def calculate_thermocline(
    samples: TemperatureSamples,
    width_alpha: float = THERMOCLINE_WIDTH_ALPHA,
    min_span_k: float = MIN_THERMOCLINE_SPAN_K,
) -> ThermoclineMetrics | None:
    """Locate the thermocline as the peak of |dT/dz| and measure its extent."""
    positions = samples.positions_m
    gradients = samples.gradients_k_per_m
    temperatures = samples.temperatures
    if len(positions) < 2:
        return None

    if abs(temperatures[-1] - temperatures[0]) < min_span_k:
        return None

    abs_grad = [abs(g) for g in gradients]
    peak_index = max(range(len(abs_grad)), key=lambda i: abs_grad[i])
    peak_value = abs_grad[peak_index]
    if peak_value <= 0:
        return None

    threshold = width_alpha * peak_value

    lo = peak_index
    while lo > 0 and abs_grad[lo - 1] >= threshold:
        lo -= 1
    hi = peak_index
    while hi < len(abs_grad) - 1 and abs_grad[hi + 1] >= threshold:
        hi += 1

    thickness_m = positions[hi] - positions[lo]
    height = samples.tank_height_m
    thickness_fraction = thickness_m / height if height > 0 else 0.0
    sharpness = peak_value / thickness_m if thickness_m > 0 else None

    return ThermoclineMetrics(
        position_m=positions[peak_index],
        position_fraction=(positions[peak_index] / height) if height > 0 else 0.0,
        strength_k_per_m=peak_value,
        thickness_m=thickness_m,
        thickness_fraction=thickness_fraction,
        sharpness_k_per_m2=sharpness,
    )


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

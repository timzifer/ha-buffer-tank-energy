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


def _natural_cubic_spline_second_derivs(
    xs: list[float], ys: list[float]
) -> list[float]:
    """Solve for the second derivatives at knots of a natural cubic spline.

    Uses the Thomas algorithm on the tridiagonal system arising from the
    continuity of the first derivative at interior knots, with the natural
    boundary condition M[0] = M[n-1] = 0.
    """
    n = len(xs)
    m = [0.0] * n
    if n < 3:
        return m

    h = [xs[i + 1] - xs[i] for i in range(n - 1)]

    # Tridiagonal system for interior M[1..n-2]
    lower = [0.0] * (n - 2)
    diag = [0.0] * (n - 2)
    upper = [0.0] * (n - 2)
    rhs = [0.0] * (n - 2)
    for i in range(1, n - 1):
        k = i - 1
        lower[k] = h[i - 1]
        diag[k] = 2.0 * (h[i - 1] + h[i])
        upper[k] = h[i]
        rhs[k] = 6.0 * (
            (ys[i + 1] - ys[i]) / h[i] - (ys[i] - ys[i - 1]) / h[i - 1]
        )

    # Forward sweep
    for k in range(1, n - 2):
        factor = lower[k] / diag[k - 1]
        diag[k] -= factor * upper[k - 1]
        rhs[k] -= factor * rhs[k - 1]

    # Back substitution
    interior = [0.0] * (n - 2)
    interior[-1] = rhs[-1] / diag[-1]
    for k in range(n - 4, -1, -1):
        interior[k] = (rhs[k] - upper[k] * interior[k + 1]) / diag[k]

    for i, value in enumerate(interior, start=1):
        m[i] = value
    return m


def _eval_cubic_spline_segment(
    x: float,
    i: int,
    xs: list[float],
    ys: list[float],
    m: list[float],
) -> float:
    """Evaluate the natural cubic spline on segment [xs[i], xs[i+1]]."""
    h = xs[i + 1] - xs[i]
    a = xs[i + 1] - x
    b = x - xs[i]
    return (
        m[i] * a**3 / (6.0 * h)
        + m[i + 1] * b**3 / (6.0 * h)
        + (ys[i] / h - m[i] * h / 6.0) * a
        + (ys[i + 1] / h - m[i + 1] * h / 6.0) * b
    )


def interpolate_temperature_profile(
    sensors: list[tuple[float, float]],
    tank_height_m: float,
    num_layers: int = NUM_LAYERS,
) -> list[float]:
    """Build a temperature profile using a natural cubic spline.

    The spline is C1‑continuous, so temperature and slope match at every
    sensor knot. Below the lowest and above the highest sensor the profile
    is extended linearly using the spline's slope at the respective
    boundary knot.

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

    if n == 2:
        slope = (ys[1] - ys[0]) / (xs[1] - xs[0])

        def evaluate(h: float) -> float:
            return ys[0] + slope * (h - xs[0])

        return [evaluate((i + 0.5) * layer_height) for i in range(num_layers)]

    second_derivs = _natural_cubic_spline_second_derivs(xs, ys)

    h0 = xs[1] - xs[0]
    h_last = xs[-1] - xs[-2]
    slope_low = (ys[1] - ys[0]) / h0 - h0 / 6.0 * (
        2.0 * second_derivs[0] + second_derivs[1]
    )
    slope_high = (ys[-1] - ys[-2]) / h_last + h_last / 6.0 * (
        second_derivs[-2] + 2.0 * second_derivs[-1]
    )

    temperatures: list[float] = []
    for i in range(num_layers):
        h = (i + 0.5) * layer_height
        if h <= xs[0]:
            temperatures.append(ys[0] + slope_low * (h - xs[0]))
        elif h >= xs[-1]:
            temperatures.append(ys[-1] + slope_high * (h - xs[-1]))
        else:
            # bisect_right returns insertion index; segment index is idx-1
            idx = bisect.bisect_right(xs, h) - 1
            if idx >= n - 1:
                idx = n - 2
            temperatures.append(
                _eval_cubic_spline_segment(h, idx, xs, ys, second_derivs)
            )

    return temperatures


def _eval_cubic_spline_segment_derivative(
    x: float,
    i: int,
    xs: list[float],
    ys: list[float],
    m: list[float],
) -> float:
    """Analytic first derivative of the natural cubic spline on [xs[i], xs[i+1]]."""
    h = xs[i + 1] - xs[i]
    a = xs[i + 1] - x
    b = x - xs[i]
    return (
        -m[i] * a * a / (2.0 * h)
        + m[i + 1] * b * b / (2.0 * h)
        - (ys[i] / h - m[i] * h / 6.0)
        + (ys[i + 1] / h - m[i + 1] * h / 6.0)
    )


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

    The sampler uses the same natural cubic spline as the energy calculation
    but evaluates it on a finer grid and returns the analytic derivative so
    that stratification and thermocline metrics can be computed directly from
    the continuous profile.
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

    if n == 2:
        slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
        temps = [ys[0] + slope * (z - xs[0]) for z in positions]
        return TemperatureSamples(
            positions_m=positions,
            temperatures=temps,
            gradients_k_per_m=[slope] * num_samples,
            tank_height_m=tank_height_m,
        )

    second_derivs = _natural_cubic_spline_second_derivs(xs, ys)
    h0 = xs[1] - xs[0]
    h_last = xs[-1] - xs[-2]
    slope_low = (ys[1] - ys[0]) / h0 - h0 / 6.0 * (
        2.0 * second_derivs[0] + second_derivs[1]
    )
    slope_high = (ys[-1] - ys[-2]) / h_last + h_last / 6.0 * (
        second_derivs[-2] + 2.0 * second_derivs[-1]
    )

    temperatures: list[float] = []
    gradients: list[float] = []
    for z in positions:
        if z <= xs[0]:
            temperatures.append(ys[0] + slope_low * (z - xs[0]))
            gradients.append(slope_low)
        elif z >= xs[-1]:
            temperatures.append(ys[-1] + slope_high * (z - xs[-1]))
            gradients.append(slope_high)
        else:
            idx = bisect.bisect_right(xs, z) - 1
            if idx >= n - 1:
                idx = n - 2
            temperatures.append(
                _eval_cubic_spline_segment(z, idx, xs, ys, second_derivs)
            )
            gradients.append(
                _eval_cubic_spline_segment_derivative(z, idx, xs, ys, second_derivs)
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

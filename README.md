# Buffer Tank Energy

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

A [Home Assistant](https://www.home-assistant.io/) custom integration that calculates stored thermal energy, charge/discharge power, heat loss and more for hot water buffer tanks — based on multiple temperature sensors at different heights.

## Features

- **Stored Energy (kWh)** — Total thermal energy stored in the tank, calculated from a 100-layer temperature profile with linear interpolation between sensors.
- **State of Charge (%)** — How full the tank is relative to a configurable maximum temperature.
- **Charge/Discharge Power (kW)** — Rate of energy change over time, with configurable EMA smoothing to reduce sensor noise.
- **Heat Loss (W)** — Estimated heat loss through insulation based on R-value, surface area and ambient temperature. Also EMA-smoothed.
- **Cumulative Heat Loss (kWh)** — Accumulated heat loss energy over time (survives restarts).
- **Average Temperature (°C)** — Volume-weighted average temperature of the tank.
- **Temperature Spread (°C)** — Difference between hottest and coldest layer.

## How It Works

The tank is modeled as a vertical cylinder divided into **100 discrete layers** of equal height. Temperature sensors at known positions are used to build a temperature profile:

| Zone | Method |
|------|--------|
| Below lowest sensor | Clamped to lowest sensor temperature |
| Between two sensors | Linear interpolation |
| Above highest sensor | Clamped to highest sensor temperature |

For each layer, the stored energy is calculated as:

```
E_layer = m_layer * c_water * (T_layer - T_reference)
```

The total stored energy is the sum across all 100 layers, converted to kWh.

### Reference Temperature Priority

The reference temperature (baseline for "zero energy") is determined by priority:

1. Return water temperature sensor (if configured)
2. Ambient temperature sensor (if configured)
3. Minimum sensor temperature

### Power Smoothing (EMA)

The charge/discharge power and heat loss sensors apply an **Exponential Moving Average** to reduce noise from sensor fluctuations. The smoothing factor (alpha) is configurable:

- **Lower values** (e.g. 0.1) = smoother output, slower reaction to real changes
- **Higher values** (e.g. 0.5) = faster response, more noise
- **1.0** = no smoothing (raw values)
- **Default: 0.2** — good balance between noise damping and responsiveness

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** > **Custom repositories**
3. Add `https://github.com/timzifer/ha-buffer-tank-energy` as an **Integration**
4. Install **Buffer Tank Energy**
5. Restart Home Assistant

### Manual

1. Copy `custom_components/buffer_tank_energy/` to your `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

The integration is configured via the UI — no YAML needed.

### Step 1: Tank Dimensions

- **Tank Volume** (liters) — total water volume of the tank
- **Tank Height** (mm) — total height of the tank

### Step 2: Temperature Sensors

Add at least **2 temperature sensors** at different heights. For each sensor:

- **Sensor Entity** — a `sensor.*` or `input_number.*` entity providing temperature in °C
- **Position** (mm from bottom) — the physical height of the sensor in the tank

### Step 3: Optional Settings

- **Return Temperature Sensor** — return water temperature, used as energy reference
- **Ambient Temperature Sensor** — ambient air temperature around the tank
- **Insulation R-Value** (m²·K/W) — thermal resistance of insulation (required for heat loss calculation, together with ambient sensor)
- **Maximum Temperature** (°C) — temperature representing 100% SoC (default: 80°C)
- **Power Smoothing Factor** — EMA alpha for power sensors (default: 0.2)

All optional settings can be changed later via the integration's **Configure** button.

## Sensors Created

| Sensor | Unit | Description |
|--------|------|-------------|
| Stored Energy | kWh | Total thermal energy above reference temperature |
| State of Charge | % | Energy as percentage of maximum capacity |
| Charge/Discharge Power | kW | Rate of energy change (positive = charging) |
| Average Temperature | °C | Mean temperature across all layers |
| Temperature Spread | °C | Max - Min temperature |
| Heat Loss | W | Estimated power lost through insulation* |
| Cumulative Heat Loss | kWh | Total heat loss energy over time* |

\* *Only created when both ambient temperature sensor and R-value are configured.*

## License

MIT

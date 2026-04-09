"""Constants for the Buffer Tank Energy integration."""

import math

DOMAIN = "buffer_tank_energy"

# Config keys
CONF_TANK_VOLUME = "tank_volume"  # Liters
CONF_TANK_HEIGHT = "tank_height"  # mm
CONF_SENSORS = "sensors"
CONF_SENSOR_ENTITY = "sensor_entity"
CONF_SENSOR_POSITION = "sensor_position"  # mm from bottom
CONF_RETURN_TEMP_ENTITY = "return_temp_entity"
CONF_AMBIENT_TEMP_ENTITY = "ambient_temp_entity"
CONF_INSULATION_R_VALUE = "insulation_r_value"  # m²·K/W

# Physics constants
WATER_SPECIFIC_HEAT = 4.186  # kJ/(kg·K)
WATER_DENSITY = 1000.0  # kg/m³
NUM_LAYERS = 100  # Number of discrete layers for energy calculation
KJ_TO_KWH = 1 / 3600  # Conversion factor kJ -> kWh

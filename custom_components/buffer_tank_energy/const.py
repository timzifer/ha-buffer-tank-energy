"""Constants for the Buffer Tank Energy integration."""

DOMAIN = "buffer_tank_energy"

# Config entry version.
#   v1 -> v2: legacy sensor list is migrated into probe subentries.
#   v2 -> v3: stale tank-device ↔ probe/threshold subentry associations left
#             behind by older releases are cleared.
CONFIG_ENTRY_VERSION = 3

# Subentry types
SUBENTRY_PROBE = "probe"
SUBENTRY_THRESHOLD = "threshold"

# Parent config keys
CONF_TANK_VOLUME = "tank_volume"  # Liters
CONF_TANK_HEIGHT = "tank_height"  # mm
CONF_RETURN_TEMP_ENTITY = "return_temp_entity"
CONF_AMBIENT_TEMP_ENTITY = "ambient_temp_entity"
CONF_INSULATION_R_VALUE = "insulation_r_value"  # m²·K/W
CONF_MAX_TEMPERATURE = "max_temperature"  # °C (for SoC calculation)
CONF_EMA_SMOOTHING = "ema_smoothing"  # EMA alpha factor for power sensors

# Probe subentry keys
CONF_PROBE_NAME = "name"
CONF_PROBE_POSITION = "position"  # mm from bottom
CONF_PROBE_ENTITY = "entity_id"  # optional — empty means virtual probe
CONF_PROBE_EMA_SMOOTHING = "ema_smoothing"  # EMA alpha for virtual probe smoothing
CONF_PROBE_ROLE = "role"  # "sensor" or "outlet" — display hint for virtual probes

# Probe roles (display hint for frontend cards)
PROBE_ROLE_SENSOR = "sensor"
PROBE_ROLE_OUTLET = "outlet"
PROBE_ROLES = (PROBE_ROLE_SENSOR, PROBE_ROLE_OUTLET)

# Threshold subentry keys
CONF_THRESHOLD_NAME = "name"
CONF_THRESHOLD_PROBE_ID = "probe_subentry_id"
CONF_THRESHOLD_MIN_TEMP = "min_temp"
CONF_THRESHOLD_HYSTERESIS = "hysteresis"

# Legacy (v1) keys, kept for migration only
LEGACY_CONF_SENSORS = "sensors"
LEGACY_CONF_SENSOR_ENTITY = "sensor_entity"
LEGACY_CONF_SENSOR_POSITION = "sensor_position"

DEFAULT_MAX_TEMPERATURE = 80.0  # °C
DEFAULT_EMA_SMOOTHING = 0.2  # Good balance of noise damping and responsiveness
DEFAULT_PROBE_EMA_SMOOTHING = 1.0  # 1.0 = no smoothing, passes raw value through
DEFAULT_PROBE_ROLE = PROBE_ROLE_SENSOR
DEFAULT_THRESHOLD_HYSTERESIS = 2.0  # K

# Physics constants
WATER_SPECIFIC_HEAT = 4.186  # kJ/(kg·K)
WATER_DENSITY = 1000.0  # kg/m³
NUM_LAYERS = 100  # Number of discrete layers for energy calculation
KJ_TO_KWH = 1 / 3600  # Conversion factor kJ -> kWh

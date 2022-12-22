"""
Custom integration to integrate stiebel_eltron_isg with Home Assistant.

For more details about this integration, please refer to
https://github.com/pail23/stiebel_eltron_isg
"""
import asyncio
from datetime import timedelta
import logging
import threading
from typing import Dict


import voluptuous as vol
from pymodbus.client import ModbusTcpClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Config, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    STARTUP_MESSAGE,
    ACTUAL_TEMPERATURE,
    TARGET_TEMPERATURE,
    ACTUAL_TEMPERATURE_FEK,
    TARGET_TEMPERATURE_FEK,
    ACTUAL_HUMIDITY,
    DEWPOINT_TEMPERATURE,
    OUTDOOR_TEMPERATURE,
    ACTUAL_TEMPERATURE_HK1,
    TARGET_TEMPERATURE_HK1,
    ACTUAL_TEMPERATURE_HK2,
    TARGET_TEMPERATURE_HK2,
    ACTUAL_TEMPERATURE_BUFFER,
    TARGET_TEMPERATURE_BUFFER,
    ACTUAL_TEMPERATURE_WATER,
    TARGET_TEMPERATURE_WATER,
    SOURCE_TEMPERATURE,
    PRODUCED_HEATING_TODAY,
    PRODUCED_HEATING_TOTAL,
    PRODUCED_WATER_HEATING_TODAY,
    PRODUCED_WATER_HEATING_TOTAL,
    CONSUMED_HEATING_TODAY,
    CONSUMED_HEATING_TOTAL,
    CONSUMED_WATER_HEATING_TODAY,
    CONSUMED_WATER_HEATING_TOTAL,
    CONSUMED_POWER,
    HEATPUMPT_AVERAGE_POWER,
    IS_HEATING,
    IS_HEATING_WATER,
    IS_SUMMER_MODE,
    IS_COOLING,
)

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER: logging.Logger = logging.getLogger(__package__)


STIEBEL_ELTRON_ISG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT): cv.string,
        vol.Optional(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): cv.positive_int,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({cv.slug: STIEBEL_ELTRON_ISG_SCHEMA})}, extra=vol.ALLOW_EXTRA
)


#MNI TODO: Put somewhere meaningful
ISG_TYPE_2 = 2   #16bit signed,   scale 0.1
ISG_TYPE_6 = 6   #16bit unsigned, scale 1
ISG_TYPE_7 = 7   #16bit signed,   scale 0.01
ISG_TYPE_8 = 8   #8bit  unsigned, scale 1
ISG_TYPE_C = 128 #composite value: multiple values as list



async def async_setup(hass: HomeAssistant, config: Config):
    """Set up this integration using YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    host = entry.data.get(CONF_HOST)
    port = entry.data.get(CONF_PORT)
    scan_interval = entry.data[CONF_SCAN_INTERVAL]

    coordinator = StiebelEltronModbusDataCoordinator(hass, host, port, scan_interval)
    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = coordinator

    for platform in PLATFORMS:
        if entry.options.get(platform, True):
            coordinator.platforms.append(platform)
            hass.async_add_job(
                hass.config_entries.async_forward_entry_setup(entry, platform)
            )

    entry.add_update_listener(async_reload_entry)
    return True


def get_isg_scaled_value(temp) -> float:
    return temp * 0.1 if temp != -32768 else None


class StiebelEltronModbusDataCoordinator(DataUpdateCoordinator):
    """Thread safe wrapper class for pymodbus."""

    def __init__(
        self,
        hass,
        host,
        port,
        scan_interval,
    ):
        """Initialize the Modbus hub."""
        self._hass = hass
        self._host = host
        self._client = ModbusTcpClient(host=host, port=port)
        self._lock = threading.Lock()
        self._scan_interval = timedelta(seconds=scan_interval)
        self.platforms = []

        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=self._scan_interval
        )

    def close(self):
        """Disconnect client."""
        with self._lock:
            self._client.close()

    def connect(self):
        """Connect client."""
        with self._lock:
            self._client.connect()

    def read_input_registers(self, slave, address, count):
        """Read input registers."""
        with self._lock:
            return self._client.read_input_registers(address, count, slave)

    async def _async_update_data(self) -> Dict:
        """Time to update."""
        try:
            return self.read_modbus_data()
        except Exception as exception:
            raise UpdateFailed() from exception

    def read_modbus_data(self) -> Dict:
        #MNI TODO: Cache result of device identification. It wont change.
        id = self.read_modbus_controller_id()

        if 103 == id:
            result = {
                #**self.read_modbus_energy(),
                #**self.read_modbus_system_state(),
                **self.read_modbus_system_values_lw(),
                "controller_id": id,
            }
        else:
            result = {
                **self.read_modbus_energy(),
                **self.read_modbus_system_state(),
                **self.read_modbus_system_values(),
                "controller_id": id,
            }
        return result

    def read_modbus_system_state(self) -> Dict:
        result = {}
        inverter_data = self.read_input_registers(slave=1, address=2500, count=1)
        if not inverter_data.isError():
            decoder = BinaryPayloadDecoder.fromRegisters(
                inverter_data.registers, byteorder=Endian.Big
            )
            state = decoder.decode_16bit_uint()
            is_heating = (state & (1 << 4)) != 0
            result[IS_HEATING] = is_heating
            is_heating_water = (state & (1 << 5)) != 0
            result[IS_HEATING_WATER] = is_heating_water
            result[CONSUMED_POWER] = (
                HEATPUMPT_AVERAGE_POWER if is_heating_water or is_heating else 0.0
            )

            result[IS_SUMMER_MODE] = (state & (1 << 7)) != 0
            result[IS_COOLING] = (state & (1 << 8)) != 0

        return result
    
    def parse_modbus_values_by_map(self, values_map:list, decoder:BinaryPayloadDecoder) -> Dict:
        """Decode modbus registers into Dict by a value map laying out the type of each value."""
        result = {}

        for (valname, valtype) in values_map:
            #If multiple values need to be combined, parser will return all of them as list to be combined after parsing
            composite_val = False
            if valtype & ISG_TYPE_C:
                composite_val = True
                valtype &=~ ISG_TYPE_C

            if ISG_TYPE_2 == valtype:
                value = get_isg_scaled_value(
                    decoder.decode_16bit_int()
                )
            elif ISG_TYPE_6 == valtype:
                value = decoder.decode_16bit_uint()
            else:
                raise ValueError("Unknown register type!")
            
            if valname != None:
                if composite_val:
                    result.setdefault(valname, []).append(value)
                else:
                    result[valname] = value

        return result

    def read_modbus_system_values_LW(self) -> Dict:
        ACTUAL_HUMIDITY_HK1 = "actual_humidity_hk1"
        ACTUAL_HUMIDITY_HK2 = "actual_humidity_hk2"
        ACTUAL_CIRCUIT_TEMPERATURE_HK1 = "actual_temperature_hk1"
        TARGET_CIRCUIT_TEMPERATURE_HK1 = "target_temperature_hk1"
        ACTUAL_CIRCUIT_TEMPERATURE_HK2 = "actual_temperature_hk2"
        TARGET_CIRCUIT_TEMPERATURE_HK2 = "target_temperature_hk2"
        SUPPLY_TEMPERATURE = "supply_temperature"
        RETURN_TEMPERATURE = "return_temperature"
        PRESSURE_CIRCUIT = "pressure_circuit"
        COMPRESSOR_STARTS = "compressor_starts"

        system_values_map_LW = [
            [ACTUAL_TEMPERATURE_HK1, ISG_TYPE_2],
            [TARGET_TEMPERATURE_HK1, ISG_TYPE_2],
            [ACTUAL_HUMIDITY_HK1, ISG_TYPE_2],
            [ACTUAL_TEMPERATURE_HK2, ISG_TYPE_2],
            [ACTUAL_TEMPERATURE_HK2, ISG_TYPE_2],

            [ACTUAL_HUMIDITY_HK2, ISG_TYPE_2],
            [OUTDOOR_TEMPERATURE, ISG_TYPE_2],
            [ACTUAL_CIRCUIT_TEMPERATURE_HK1, ISG_TYPE_2],
            [TARGET_CIRCUIT_TEMPERATURE_HK1, ISG_TYPE_2],
            [ACTUAL_CIRCUIT_TEMPERATURE_HK2, ISG_TYPE_2],

            [TARGET_CIRCUIT_TEMPERATURE_HK2, ISG_TYPE_2],
            [SUPPLY_TEMPERATURE, ISG_TYPE_2],
            [RETURN_TEMPERATURE, ISG_TYPE_2],
            [PRESSURE_CIRCUIT, ISG_TYPE_2],
            [None, ISG_TYPE_2], #volume_flow

            [ACTUAL_TEMPERATURE_WATER, ISG_TYPE_2],
            [TARGET_TEMPERATURE_WATER, ISG_TYPE_2],
            [None, ISG_TYPE_6], #inlet_actual_fan_speed
            [None, ISG_TYPE_6], #inlet_target_volume_flow
            [None, ISG_TYPE_6], #outlet_actual_fan_speed

            [None, ISG_TYPE_6], #outlet_target_volume_flow
            [None, ISG_TYPE_6], #outlet_humidity
            [None, ISG_TYPE_2], #outlet_temperature
            [None, ISG_TYPE_2], #outlet_dew_point
            [None, ISG_TYPE_2], #hk1_dew_point

            [None, ISG_TYPE_2], #hk2_dew_point
            [None, ISG_TYPE_2], #collector_temperature
            [None, ISG_TYPE_2], #hot_gas_temperature
            [None, ISG_TYPE_7], #high_pressure
            [None, ISG_TYPE_7], #low_pressure

            [COMPRESSOR_STARTS, ISG_TYPE_6|ISG_TYPE_C], #compressor_starts (documented differently, but this is only the high part: *1000)
            [None, ISG_TYPE_2], #compressor_rpm
            [None, ISG_TYPE_6], #mixed_water_volume
            [COMPRESSOR_STARTS, ISG_TYPE_6|ISG_TYPE_C], #compressor_starts (undocumented, but this seems to be the low part: [0..999])
        ]

        result = {}
        inverter_data = self.read_input_registers(slave=1, address=0, count=34)
        if not inverter_data.isError():
            decoder = BinaryPayloadDecoder.fromRegisters(
                inverter_data.registers, byteorder=Endian.Big
            )

        result = self.parse_modbus_values_by_map(system_values_map_LW, decoder)

        #Convert composite value to single number
        result[COMPRESSOR_STARTS] = result[COMPRESSOR_STARTS][0]*1000 + result[COMPRESSOR_STARTS][1]

        return result

    def read_modbus_system_values(self) -> Dict:
        result = {}
        inverter_data = self.read_input_registers(slave=1, address=500, count=40)
        if not inverter_data.isError():
            decoder = BinaryPayloadDecoder.fromRegisters(
                inverter_data.registers, byteorder=Endian.Big
            )
            result[ACTUAL_TEMPERATURE] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[TARGET_TEMPERATURE] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[ACTUAL_TEMPERATURE_FEK] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[TARGET_TEMPERATURE_FEK] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[ACTUAL_HUMIDITY] = get_isg_scaled_value(decoder.decode_16bit_int())
            result[DEWPOINT_TEMPERATURE] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[OUTDOOR_TEMPERATURE] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[ACTUAL_TEMPERATURE_HK1] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            hk1_target = get_isg_scaled_value(decoder.decode_16bit_int())
            result[TARGET_TEMPERATURE_HK1] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[ACTUAL_TEMPERATURE_HK2] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[TARGET_TEMPERATURE_HK2] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            decoder.skip_bytes(10)
            result[ACTUAL_TEMPERATURE_BUFFER] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[TARGET_TEMPERATURE_BUFFER] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            decoder.skip_bytes(4)
            result[ACTUAL_TEMPERATURE_WATER] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            result[TARGET_TEMPERATURE_WATER] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
            decoder.skip_bytes(24)
            result[SOURCE_TEMPERATURE] = get_isg_scaled_value(
                decoder.decode_16bit_int()
            )
        return result

    def read_modbus_energy(self) -> Dict:
        result = {}
        inverter_data = self.read_input_registers(slave=1, address=3500, count=22)
        if not inverter_data.isError():
            decoder = BinaryPayloadDecoder.fromRegisters(
                inverter_data.registers, byteorder=Endian.Big
            )
            produced_heating_today = decoder.decode_16bit_uint()
            produced_heating_total_low = decoder.decode_16bit_uint()
            produced_heating_total_high = decoder.decode_16bit_uint()
            produced_water_today = decoder.decode_16bit_uint()
            produced_water_total_low = decoder.decode_16bit_uint()
            produced_water_total_high = decoder.decode_16bit_uint()
            decoder.skip_bytes(8)  # Skip NHZ
            consumed_heating_today = decoder.decode_16bit_uint()
            consumed_heating_total_low = decoder.decode_16bit_uint()
            consumed_heating_total_high = decoder.decode_16bit_uint()
            consumed_water_today = decoder.decode_16bit_uint()
            consumed_water_total_low = decoder.decode_16bit_uint()
            consumed_water_total_high = decoder.decode_16bit_uint()

            result[PRODUCED_HEATING_TODAY] = produced_heating_today
            result[PRODUCED_HEATING_TOTAL] = (
                produced_heating_total_high * 1000 + produced_heating_total_low
            )
            result[PRODUCED_WATER_HEATING_TODAY] = produced_water_today
            result[PRODUCED_WATER_HEATING_TOTAL] = (
                produced_water_total_high * 1000 + produced_water_total_low
            )
            result[CONSUMED_HEATING_TODAY] = consumed_heating_today
            result[CONSUMED_HEATING_TOTAL] = (
                consumed_heating_total_high * 1000 + consumed_heating_total_low
            )
            result[CONSUMED_WATER_HEATING_TODAY] = consumed_water_today
            result[CONSUMED_WATER_HEATING_TOTAL] = (
                consumed_water_total_high * 1000 + consumed_water_total_low
            )
        return result
    
    def read_modbus_controller_id(self) -> int:
        controller_id = 0
        inverter_data = self.read_input_registers(slave=1, address=5001, count=1)
        if not inverter_data.isError():
            decoder = BinaryPayloadDecoder.fromRegisters(
                inverter_data.registers, byteorder=Endian.Big
            )
            controller_id = decoder.decode_16bit_uint()
        return controller_id


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
            ]
        )
    )
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)

FAHRENHEIT = "fahrenheit"
CELSIUS = "celsius"
METRIC = "metric"
METERS_PER_SECOND = "meters per second"
MILES_PER_HOUR = "miles per hour"


class WeatherConfig:
    _temperature_unit = None
    _speed_unit = None

    def __init__(self, core_config: dict, settings: dict):
        self.core_config = core_config
        self.settings = settings
        config_location = self.core_config["location"]
        self.latitude = config_location["coordinate"]["latitude"]
        self.longitude = config_location["coordinate"]["longitude"]
        city = config_location["city"]
        state = city["state"]
        country = state["country"]
        self.city = city["name"]
        self.state = state["name"]
        self.country = country["name"]

    @property
    def speed_unit(self) -> str:
        """Use the core configuration to determine the unit of speed.

        Returns: (str) 'meters_sec' or 'mph'
        """
        if self._speed_unit is None:
            system_unit = self.core_config.get('system_unit')
            if system_unit == METRIC:
                self._speed_unit = METERS_PER_SECOND
            else:
                self._speed_unit = MILES_PER_HOUR

        return self._speed_unit

    @property
    def temperature_unit(self) -> str:
        """Use the core configuration to determine the unit of temperature.

        Returns: "celsius" or "fahrenheit"
        """
        if self._temperature_unit is None:
            unit_from_settings = self.settings.get("units")
            measurement_system = self.core_config['system_unit']
            if unit_from_settings is None:
                if measurement_system == METRIC:
                    self._temperature_unit = CELSIUS
                else:
                    self._temperature_unit = FAHRENHEIT
            else:
                if unit_from_settings.lower() == FAHRENHEIT:
                    self._temperature_unit = FAHRENHEIT
                elif unit_from_settings.lower() == CELSIUS:
                    self._temperature_unit = CELSIUS

        return self._temperature_unit

from mycroft.util.log import LOG
from .weather_config import MILES_PER_HOUR

MILES_PER_HOUR_MULTIPLIER = 2.23694
WIND_DIRECTION_CONVERSION = (
    (22.5, "N"),
    (67.5, "NE"),
    (112.5, "E"),
    (157.5, "SE"),
    (202.5, "S"),
    (247.5, "SW"),
    (292.5, "W"),
    (337.5, "NW")
)


class Wind:
    _direction = None
    _speed = None
    _strength = None

    def __init__(self, weather, speed_unit):
        self._wind = weather.get_wind()
        self.speed_unit = speed_unit

    @property
    def speed(self) -> int:
        if self._speed is None:
            self._speed = self._wind["speed"]
            if self.speed_unit == MILES_PER_HOUR:
                self._speed *= 2.23694
            self._speed = round(self._speed)

        return self._speed

    @property
    def strength(self):
        if self._strength is None:
            if self._wind["speed"] <= 2.2352:
                self._strength = "light"
            elif self._wind["speed"] <= 6.7056:
                self._strength = "medium"
            else:
                self._strength = "hard"

        return self._strength

    @property
    def direction(self):
        degree = self._wind.get("deg")
        if self._direction is None and degree is not None:
            if degree >= 337.5:
                self._direction = "N"
            else:
                for min_degree, direction in WIND_DIRECTION_CONVERSION:
                    if degree < min_degree:
                        self._direction = direction
                        break

        return self._direction


class WeatherReport:
    def __init__(self, weather_config):
        """ Creates a report base with location, unit. """
        self.weather_config = weather_config
        self.short_location = weather_config.city
        self.long_location = ', '.join([
            weather_config.city,
            weather_config.state,
            weather_config.country
        ])
        self.temperature_unit = weather_config.temperature_unit
        self.condition = None
        self.condition_category = None
        self.icon = None
        self.temperature = None
        self.minimum_temperature = None
        self.maximum_temperature = None
        self.humidity = None
        self.wind = None
        self.wind_format = None
        self.forecast_time = None
        self.time = None
        self.day = None
        self.wind_unit = None

    def add_forecast(self, forecast):
        self.condition = forecast.get_detailed_status()
        self.condition_category = forecast.get_status()
        self.icon = forecast.get_weather_icon_name()
        self.temperature = self.get_temperature(forecast, 'temp')
        self.minimum_temperature = self.get_temperature(forecast, 'min')
        self.maximum_temperature = self.get_temperature(forecast, 'max')

        # Min and Max temps not available in 3hr forecast
        self.humidity = forecast.get_humidity()
        self.wind = Wind(forecast, self.weather_config.speed_unit)
        self.forecast_time = forecast.get_reference_time(timeformat='date')

    def get_temperature(self, weather, key: str) -> str:
        """Extract one of the temperatures from the weather data

        Typical values include: 'temp', 'min', 'max', 'morn', 'day', 'night'
        NOTE: The 3-hour forecast uses different temperature keys
        for min and max (temp_min and temp_max).

        :param weather: weather forecast object from the weather API
        :param key: the temperature value to extract
        :return: empty string if temperature value not found or the value.
        """
        temperatures = weather.get_temperature(self.temperature_unit)
        temperature = temperatures.get(key)
        if temperature is None and key == "min":
            temperature = temperatures.get("temp_min")
        elif temperature is None and key == "max":
            temperature = temperatures.get("temp_max")

        if temperature is None:
            LOG.warning('No {} temperature available'.format(key))
            temperature = ''
        else:
            temperature = str(int(round(temperature)))

        return temperature

    def format_wind(self):
        wind = dict(
            speed=self.wind.speed,
            direction=self.wind.direction,
            strength=self.wind.strength
        )
        return self.wind_format.format(**wind)

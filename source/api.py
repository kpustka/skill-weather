import json
import time
from copy import deepcopy

from pyowm.webapi25.forecaster import Forecaster
from pyowm.webapi25.forecastparser import ForecastParser
from pyowm.webapi25.observationparser import ObservationParser
from requests import HTTPError, Response

from mycroft.api import Api
from mycroft.util.log import LOG
from .weather import WeatherReport
from .util import LocationNotFoundError

MINUTES = 60  # Minutes to seconds multiplier

APIErrors = (LocationNotFoundError, HTTPError)


class OpenWeatherMapApi(Api):
    def __init__(self):
        super().__init__(path="owm")

    def get_weather_for_coordinates(self, measurement_system, latitude, longitude):
        """Use the One Call API to get local weather conditions."""
        query_parameters = dict(
            exclude="minutely",
            lat=latitude,
            lon=longitude,
            units=measurement_system
        )
        api_request = dict(path="/onecall", query=query_parameters)
        response = self.request(api_request)
        local_weather = WeatherReport(response)

        return local_weather


class OWMApi(Api):
    ''' Wrapper that defaults to the Mycroft cloud proxy so user's don't need
        to get their own OWM API keys '''

    def __init__(self):
        super(OWMApi, self).__init__("owm")
        self.owmlang = "en"
        self.encoding = "utf8"
        self.observation = ObservationParser()
        self.forecast = ForecastParser()
        self.query_cache = {}
        self.location_translations = {}

    @staticmethod
    def get_language(lang):
        """
        OWM supports 31 languages, see https://openweathermap.org/current#multi

        Convert language code to owm language, if missing use 'en'
        """

        owmlang = 'en'

        # some special cases
        if lang == 'zh-zn' or lang == 'zh_zn':
            return 'zh_zn'
        elif lang == 'zh-tw' or lang == 'zh_tw':
            return 'zh_tw'

        # special cases cont'd
        lang = lang.lower().split("-")
        lookup = {
            'sv': 'se',
            'cs': 'cz',
            'ko': 'kr',
            'lv': 'la',
            'uk': 'ua'
        }
        if lang[0] in lookup:
            return lookup[lang[0]]

        owmsupported = ['ar', 'bg', 'ca', 'cz', 'da', 'de', 'el', 'en', 'fa', 'fi',
                        'fr', 'gl', 'hr', 'hu', 'it', 'ja', 'kr', 'la', 'lt',
                        'mk', 'nl', 'pl', 'pt', 'ro', 'ru', 'se', 'sk', 'sl',
                        'es', 'tr', 'ua', 'vi']

        if lang[0] in owmsupported:
            return lang[0]

        if (len(lang) == 2):
            if lang[1] in owmsupported:
                return lang[1]
        return owmlang

    def build_query(self, params):
        params.get("query").update({"lang": self.owmlang})
        return params.get("query")

    def request(self, data):
        """ Caching the responses """
        req_hash = hash(json.dumps(data, sort_keys=True))
        cache = self.query_cache.get(req_hash, (0, None))
        # check for caches with more days data than requested
        if data['query'].get('cnt') and cache == (0, None):
            test_req_data = deepcopy(data)
            while test_req_data['query']['cnt'] < 16 and cache == (0, None):
                test_req_data['query']['cnt'] += 1
                test_hash = hash(json.dumps(test_req_data, sort_keys=True))
                test_cache = self.query_cache.get(test_hash, (0, None))
                if test_cache != (0, None):
                    cache = test_cache
        # Use cached response if value exists and was fetched within 15 min
        now = time.monotonic()
        if now > (cache[0] + 15 * MINUTES) or cache[1] is None:
            resp = super().request(data)
            # 404 returned as JSON-like string in some instances
            if isinstance(resp, str) and '{"cod":"404"' in resp:
                r = Response()
                r.status_code = 404
                raise HTTPError(resp, response=r)
            self.query_cache[req_hash] = (now, resp)
        else:
            LOG.debug('Using cached OWM Response from {}'.format(cache[0]))
            resp = cache[1]
        return resp

    def get_data(self, response):
        return response.text

    def weather_at_location(self, name):
        if name == '':
            raise LocationNotFoundError('The location couldn\'t be found')

        q = {"q": name}
        try:
            data = self.request({
                "path": "/weather",
                "query": q
            })
            return self.observation.parse_JSON(data), name
        except HTTPError as e:
            if e.response.status_code == 404:
                name = ' '.join(name.split()[:-1])
                return self.weather_at_location(name)
            raise

    def weather_at_place(self, name, lat, lon):
        if lat and lon:
            q = {"lat": lat, "lon": lon}
        else:
            if name in self.location_translations:
                name = self.location_translations[name]
            response, trans_name = self.weather_at_location(name)
            self.location_translations[name] = trans_name
            return response

        data = self.request({
            "path": "/weather",
            "query": q
        })
        return self.observation.parse_JSON(data)

    def three_hours_forecast(self, name, lat, lon):
        if lat and lon:
            q = {"lat": lat, "lon": lon}
        else:
            if name in self.location_translations:
                name = self.location_translations[name]
            q = {"q": name}

        data = self.request({
            "path": "/forecast",
            "query": q
        })
        return self.to_forecast(data, "3h")

    def _daily_forecast_at_location(self, name, limit):
        if name in self.location_translations:
            name = self.location_translations[name]
        orig_name = name
        while name != '':
            try:
                q = {"q": name}
                if limit is not None:
                    q["cnt"] = limit
                data = self.request({
                    "path": "/forecast/daily",
                    "query": q
                })
                forecast = self.to_forecast(data, 'daily')
                self.location_translations[orig_name] = name
                return forecast
            except HTTPError as e:
                if e.response.status_code == 404:
                    # Remove last word in name
                    name = ' '.join(name.split()[:-1])

        raise LocationNotFoundError('The location couldn\'t be found')

    def daily_forecast(self, name, lat, lon, limit=None):
        if lat and lon:
            q = {"lat": lat, "lon": lon}
        else:
            return self._daily_forecast_at_location(name, limit)

        if limit is not None:
            q["cnt"] = limit
        data = self.request({
            "path": "/forecast/daily",
            "query": q
        })
        return self.to_forecast(data, "daily")

    def to_forecast(self, data, interval):
        forecast = self.forecast.parse_JSON(data)
        if forecast is not None:
            forecast.set_interval(interval)
            return Forecaster(forecast)
        else:
            return None

    def set_OWM_language(self, lang):
        self.owmlang = lang

        # Certain OWM condition information is encoded using non-utf8
        # encodings. If another language needs similar solution add them to the
        # encodings dictionary
        encodings = {
            'se': 'latin1'
        }
        self.encoding = encodings.get(lang, 'utf8')



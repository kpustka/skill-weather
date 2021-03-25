# Copyright 2017, Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Mycroft skill for communicating weather information

This skill uses the Open Weather Map API (https://openweathermap.org) and
the PyOWM wrapper for it.  For more info, see:

General info on PyOWM:
    https://www.slideshare.net/csparpa/pyowm-my-first-open-source-project
OWM doc for APIs used:
    https://openweathermap.org/current - current
    https://openweathermap.org/forecast5 - three hour forecast
    https://openweathermap.org/forecast16 - daily forecasts
PyOWM docs:
    https://media.readthedocs.org/pdf/pyowm/latest/pyowm.pdf
"""

import time
from datetime import datetime, timedelta
from multi_key_dict import multi_key_dict
from typing import Optional

from adapt.intent import IntentBuilder
import pytz

import mycroft.audio
from mycroft import MycroftSkill, intent_handler
from mycroft.messagebus.message import Message
from mycroft.util.format import (
    nice_date, nice_time, nice_number, pronounce_number, join_list
)
from mycroft.util.parse import extract_datetime, extract_number
from mycroft.util.time import now_local, to_utc, to_local
from .source import (
    APIErrors,
    CELSIUS,
    FAHRENHEIT,
    LocationNotFoundError,
    METERS_PER_SECOND,
    MILES_PER_HOUR,
    OWMApi,
    WeatherConfig,
    WeatherReport,
    Wind
)


MINUTES = 60  # Minutes to seconds multiplier


# Windstrength limits in miles per hour
WINDSTRENGTH_MPH = {
    'hard': 20,
    'medium': 11
}


# Windstrenght limits in m/s
WINDSTRENGTH_MPS = {
    'hard': 9,
    'medium': 5
}


class WeatherSkill(MycroftSkill):
    def __init__(self):
        super().__init__("WeatherSkill")
        self.weather_api = OWMApi()
        self.weather_config = WeatherConfig(self.config_core)

        # Build a dictionary to translate OWM weather-conditions
        # codes into the Mycroft weather icon codes
        # (see https://openweathermap.org/weather-conditions)
        self.CODES = multi_key_dict()
        self.CODES['01d', '01n'] = 0                # clear
        self.CODES['02d', '02n', '03d', '03n'] = 1  # partly cloudy
        self.CODES['04d', '04n'] = 2                # cloudy
        self.CODES['09d', '09n'] = 3                # light rain
        self.CODES['10d', '10n'] = 4                # raining
        self.CODES['11d', '11n'] = 5                # stormy
        self.CODES['13d', '13n'] = 6                # snowing
        self.CODES['50d', '50n'] = 7                # windy/misty

        # Use Mycroft proxy if no private key provided
        self.settings["api_key"] = None
        self.settings["use_proxy"] = True

    def initialize(self):
        if self.weather_api:
            self.weather_api.set_OWM_language(lang=OWMApi.get_language(self.lang))

        self.schedule_for_daily_use()
        try:
            self.mark2_forecast()
        except Exception as e:
            self.log.warning('Could not prepare forecasts. '
                             '({})'.format(repr(e)))

    def prime_weather_cache(self):
        # If not already cached, this will reach out for current conditions
        report = self._initialize_report(None)
        try:
            self.weather_api.weather_at_place(
                report.city, report.latitude,
                report.longitude).get_weather()
            self.weather_api.daily_forecast(
                report.city, report.latitude, report.longitude, limit=16)
        except Exception as e:
            self.log.error('Failed to prime weather cache '
                           '({})'.format(repr(e)))

    def schedule_for_daily_use(self):
        # Assume the user has a semi-regular schedule.  Whenever this method
        # is called, it will establish a 45 minute window of pre-cached
        # weather info for the next day allowing for snappy responses to the
        # daily query.
        self.prime_weather_cache()
        self.cancel_scheduled_event("precache1")
        self.cancel_scheduled_event("precache2")
        self.cancel_scheduled_event("precache3")
        self.schedule_repeating_event(self.prime_weather_cache, None,
                                      60*60*24,         # One day in seconds
                                      name="precache1")
        self.schedule_repeating_event(self.prime_weather_cache, None,
                                      60*60*24-60*15,   # One day - 15 minutes
                                      name="precache2")
        self.schedule_repeating_event(self.prime_weather_cache, None,
                                      60*60*24+60*15,   # One day + 15 minutes
                                      name="precache3")

    def get_coming_days_forecast(self, forecast, unit, days=None):
        """
            Get weather forcast for the coming days and returns them as a list

            Parameters:
                forecast: OWM weather
                unit: Temperature unit
                dt: Reference time
                days: number of days to get forecast for, defaults to 4

            Returns: List of dicts containg weather info
        """
        days = days or 4
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        forecast_list = []
        # Get tomorrow and 4 days forward
        for weather in list(forecast.get_weathers())[1:5]:
            result_temp = weather.get_temperature(unit)
            day_num = datetime.weekday(
                datetime.fromtimestamp(weather.get_reference_time()))
            result_temp_day = weekdays[day_num]
            forecast_list.append({
                "weathercode": self.CODES[weather.get_weather_icon_name()],
                "max": round(result_temp['max']),
                "min": round(result_temp['min']),
                "date": result_temp_day
            })
        return forecast_list

    def mark2_forecast(self):
        """ Builds forecast for the upcoming days for the Mark-2 display."""
        report = self._initialize_report()
        daily_forecast = self.weather_api.daily_forecast(
            report.city, report.latitude, report.latitude, limit=5
        )
        if daily_forecast is None:
            self._report_no_data()
            return

        f = daily_forecast.get_forecast()
        forecast_list = self.get_coming_days_forecast(
            f, self.weather_config.temperature_unit)

        if "gui" in dir(self):
            forecast = {}
            forecast['first'] = forecast_list[0:2]
            forecast['second'] = forecast_list[2:4]
            self.gui['forecast'] = forecast

    # DATETIME BASED QUERIES
    # Handle: what is the weather like?
    @intent_handler(IntentBuilder("").one_of("Weather", "Forecast")
                    .require("Query").optionally("Location")
                    .optionally("Today").build())
    def handle_current_weather(self, message):
        try:
            self.log.debug("Handler: handle_current_weather")
            # Get a date from requests like "weather for next Tuesday"
            today, _ = self.__extract_datetime("today")
            when, _ = self.__extract_datetime(message.data.get('utterance'),
                                              lang=self.lang)
            if when and when != today:
                self.log.debug("Doing a forecast {} {}".format(today, when))
                return self.handle_forecast(message)

            report = self.__populate_report(message)

            if report is None:
                self._report_no_data()
                return

            self.__report_weather(
                "current", report,
                separate_min_max='Location' not in message.data)
            self.mark2_forecast(report)

            # Establish the daily cadence
            self.schedule_for_daily_use()
        except APIErrors as e:
            self.log.exception(repr(e))
            self.__api_error(e)
        except Exception as e:
            self.log.exception("Error: {0}".format(e))

    @intent_handler("whats.weather.like.intent")
    def handle_current_weather_alt(self, message):
        self.handle_current_weather(message)

    @intent_handler(IntentBuilder("").one_of("Weather", "Forecast")
                    .one_of("Now", "Today").optionally("Location").build())
    def handle_current_weather_simple(self, message):
        self.handle_current_weather(message)

    @intent_handler("what.is.three.day.forecast.intent")
    def handle_three_day_forecast(self, message):
        """ Handler for three day forecast without specified location

        Examples:   "What is the 3 day forecast?"
                    "What is the weather forecast?"
        """
        report = self._initialize_report(message)

        try:
            self.report_multiday_forecast(report)
        except APIErrors as e:
            self.__api_error(e)
        except Exception as e:
            self.log.exception("Error: {0}".format(e))

    @intent_handler("what.is.three.day.forecast.location.intent")
    def handle_three_day_forecast_location(self, message):
        """ Handler for three day forecast for a specific location

        Example: "What is the 3 day forecast for London?"
        """
        # padatious lowercases everything including these keys
        message.data['Location'] = message.data.pop('location')
        return self.handle_three_day_forecast(message)

    @intent_handler("what.is.two.day.forecast.intent")
    def handle_two_day_forecast(self, message):
        """ Handler for two day forecast with no specified location

        Examples:   "What's the weather like next Monday and Tuesday?"
                    "What's the weather gonna be like in the coming days?"
        """
        # TODO consider merging in weekend intent

        report = self._initialize_report(message)
        if message.data.get('day_one'):
            # report two or more specific days
            days = []
            day_num = 1
            day = message.data['day_one']
            while day:
                day_dt, _ = self.__extract_datetime(day)
                days.append(day_dt)
                day_num += 1
                next_day = 'day_{}'.format(pronounce_number(day_num))
                day = message.data.get(next_day)

        try:
            if message.data.get('day_one'):
                # report two or more specific days
                self.report_multiday_forecast(report, set_days=days)
            else:
                # report next two days
                self.report_multiday_forecast(report, num_days=2)

        except APIErrors as e:
            self.__api_error(e)
        except Exception as e:
            self.log.exception("Error: {0}".format(e))

    @intent_handler("what.is.multi.day.forecast.intent")
    def handle_multi_day_forecast(self, message):
        """ Handler for multiple day forecast with no specified location

        Examples:   "What's the weather like in the next 4 days?"
        """

        report = self._initialize_report(message)
        # report x number of days
        when, _ = self.__extract_datetime("tomorrow")
        num_days = int(extract_number(message.data['num']))

        if self.voc_match(message.data['num'], 'Couple'):
            self.report_multiday_forecast(report, num_days=2)

        self.report_multiday_forecast(report, when,
                                      num_days=num_days)

    # Handle: What is the weather forecast tomorrow?
    @intent_handler(IntentBuilder("").one_of("Weather", "Forecast")
                    .optionally("Query").require("RelativeDay")
                    .optionally("Location").build())
    def handle_forecast(self, message):
        report = self._initialize_report(message)

        # Get a date from spoken request
        when, _ = self.__extract_datetime(message.data.get('utterance'),
                                          lang=self.lang)
        today, _ = self.__extract_datetime('today', lang='en-us')

        if today == when:
            self.handle_current_weather(message)
            return

        self.report_forecast(report, when)

        # Establish the daily cadence
        self.schedule_for_daily_use()

    # Handle: What's the weather later?
    @intent_handler(IntentBuilder("").require("Query").require(
        "Weather").optionally("Location").require("Later").build())
    def handle_next_hour(self, message):
        report = self._initialize_report(message)

        # Get near-future forecast
        three_hour_forecast = self.weather_api.three_hours_forecast(
            report.long_location,
            self.weather_config.latitude,
            self.weather_config.longitude
        )
        forecast_weather = three_hour_forecast.get_forecast().get_weathers()[0]

        if forecast_weather is None:
            self._report_no_data()
            return

        # NOTE: The 3-hour forecast uses different temperature labels,
        # temp, temp_min and temp_max.
        report.add_forecast(forecast_weather)
        self.__report_weather("hour", report)

    # Handle: What's the weather tonight / tomorrow morning?
    @intent_handler(IntentBuilder("").require("RelativeTime")
                    .one_of("Weather", "Forecast").optionally("Query")
                    .optionally("RelativeDay").optionally("Location").build())
    def handle_weather_at_time(self, message):
        self.log.debug("Handler: handle_weather_at_time")
        when, _ = self.__extract_datetime(
            message.data.get('utterance'), lang=self.lang)
        now = datetime.utcnow()
        time_diff = (when - now)
        mins_diff = (time_diff.days * 1440) + (time_diff.seconds / 60)

        if mins_diff < 120:
            self.handle_current_weather(message)
        else:
            report = self.__populate_report(message)

            if report is None:
                self._report_no_data()
                return
            self.__report_weather("at.time", report)

    @intent_handler(IntentBuilder("").require("Query").one_of(
        "Weather", "Forecast").require("Weekend").require(
        "Next").optionally("Location").build())
    def handle_next_weekend_weather(self, message):
        """ Handle next weekends weather """

        report = self._initialize_report(message)
        when, _ = self.__extract_datetime('next saturday', lang='en-us')
        self.report_forecast(report, when)
        when, _ = self.__extract_datetime('next sunday', lang='en-us')
        self.report_forecast(report, when)

    @intent_handler(IntentBuilder("").require("Query")
                    .one_of("Weather", "Forecast").require("Weekend")
                    .optionally("Location").build())
    def handle_weekend_weather(self, message):
        """ Handle weather for weekend. """
        report = self._initialize_report(message)

        # Get a date from spoken request
        when, _ = self.__extract_datetime('this saturday', lang='en-us')
        self.report_forecast(report, when)
        when, _ = self.__extract_datetime('this sunday', lang='en-us')
        self.report_forecast(report, when)

    @intent_handler(IntentBuilder("").optionally("Query")
                    .one_of("Weather", "Forecast").require("Week")
                    .optionally("Location").build())
    def handle_week_weather(self, message):
        """ Handle weather for week.
            Speaks overview of week, not daily forecasts """
        report = self._initialize_report(message)
        when, _ = self.__extract_datetime(message.data['utterance'])
        today, _ = self.__extract_datetime("today")
        if not when:
            when = today
        days = [when + timedelta(days=i) for i in range(7)]
        # Fetch forecasts/reports for week
        forecasts = [dict(self.__populate_forecast(report, day,
                                                   preface_day=False))
                     if day != today
                     else dict(self.__populate_current(report, day))
                     for day in days]

        if forecasts is None:
            self._report_no_data()
            return

        # collate forecasts
        collated = {'condition': [], 'condition_cat': [], 'icon': [],
                    'temp': [], 'temp_min': [], 'temp_max': []}
        for fc in forecasts:
            for attribute in collated.keys():
                collated[attribute].append(fc.get(attribute))

        # analyse for commonality/difference
        primary_category = max(collated['condition_cat'],
                               key=collated['condition_cat'].count)
        days_with_primary_cat, conditions_in_primary_cat = [], []
        days_with_other_cat = {}
        for i, item in enumerate(collated['condition_cat']):
            if item == primary_category:
                days_with_primary_cat.append(i)
                conditions_in_primary_cat.append(collated['condition'][i])
            else:
                if not days_with_other_cat.get(item):
                    days_with_other_cat[item] = []
                days_with_other_cat[item].append(i)
        primary_condition = max(conditions_in_primary_cat,
                                key=conditions_in_primary_cat.count)

        # CONSTRUCT DIALOG
        speak_category = self.translate_namedvalues('condition.category')
        # 0. Report period starting day
        if days[0] == today:
            dialog = self.translate('this.week')
        else:
            speak_day = self.__to_day(days[0])
            dialog = self.translate('from.day', {'day': speak_day})

        # 1. whichever is longest (has most days), report as primary
        # if over half the days => "it will be mostly {cond}"
        speak_primary = speak_category[primary_category]
        seq_primary_days = self.__get_seqs_from_list(days_with_primary_cat)
        if len(days_with_primary_cat) >= (len(days) / 2):
            dialog = self.concat_dialog(dialog,
                                        'weekly.conditions.mostly.one',
                                        {'condition': speak_primary})
        elif seq_primary_days:
            # if condition occurs on sequential days, report date range
            dialog = self.concat_dialog(dialog,
                                        'weekly.conditions.seq.start',
                                        {'condition': speak_primary})
            for seq in seq_primary_days:
                if seq is not seq_primary_days[0]:
                    dialog = self.concat_dialog(dialog, 'and')
                day_from = self.__to_day(days[seq[0]])
                day_to = self.__to_day(days[seq[-1]])
                dialog = self.concat_dialog(dialog,
                                            'weekly.conditions.seq.period',
                                            {'from': day_from,
                                                'to': day_to})
        else:
            # condition occurs on random days
            dialog = self.concat_dialog(dialog,
                                        'weekly.conditions.some.days',
                                        {'condition': speak_primary})
        self.speak_dialog(dialog)

        # 2. Any other conditions present:
        dialog = ""
        dialog_list = []
        for cat in days_with_other_cat:
            spoken_cat = speak_category[cat]
            cat_days = days_with_other_cat[cat]
            seq_days = self.__get_seqs_from_list(cat_days)
            for seq in seq_days:
                if seq is seq_days[0]:
                    seq_dialog = spoken_cat
                else:
                    seq_dialog = self.translate('and')
                day_from = self.__to_day(days[seq[0]])
                day_to = self.__to_day(days[seq[-1]])
                seq_dialog = self.concat_dialog(
                    seq_dialog,
                    self.translate('weekly.conditions.seq.period',
                                   {'from': day_from,
                                    'to': day_to}))
                dialog_list.append(seq_dialog)
            if not seq_days:
                for day in cat_days:
                    speak_day = self.__to_day(days[day])
                    dialog_list.append(self.translate(
                        'weekly.condition.on.day',
                        {'condition': collated['condition'][day],
                            'day': speak_day}))
        dialog = join_list(dialog_list, 'and')
        self.speak_dialog(dialog)

        # 3. Report temps:
        temp_ranges = {
            'low_min': min(collated['temp_min']),
            'low_max': max(collated['temp_min']),
            'high_min': min(collated['temp_max']),
            'high_max': max(collated['temp_max'])
        }
        self.speak_dialog('weekly.temp.range', temp_ranges)

    # CONDITION BASED QUERY HANDLERS ####
    @intent_handler(IntentBuilder("").require("Temperature")
                    .require("Query").optionally("Location")
                    .optionally("Unit").optionally("Today")
                    .optionally("Now").build())
    def handle_current_temperature(self, message):
        return self.__handle_typed(message, 'temperature')

    @intent_handler('simple.temperature.intent')
    def handle_simple_temperature(self, message):
        return self.__handle_typed(message, 'temperature')

    @intent_handler(IntentBuilder("").require("Query").require("High")
                    .optionally("Temperature").optionally("Location")
                    .optionally("Unit").optionally("RelativeDay")
                    .optionally("Now").build())
    def handle_high_temperature(self, message):
        return self.__handle_typed(message, 'high.temperature')

    @intent_handler(IntentBuilder("").require("Query").require("Low")
                    .optionally("Temperature").optionally("Location")
                    .optionally("Unit").optionally("RelativeDay")
                    .optionally("Now").build())
    def handle_low_temperature(self, message):
        return self.__handle_typed(message, 'low.temperature')

    @intent_handler(IntentBuilder("").require("ConfirmQuery").require(
        "Windy").optionally("Location").build())
    def handle_isit_windy(self, message):
        """ Handler for utterances similar to "is it windy today?" """
        report = self.__populate_report(message)

        if report is None:
            self._report_no_data()
            return

        if self.weather_config.speed_unit == MILES_PER_HOUR:
            limits = WINDSTRENGTH_MPH
            report.wind_unit = self.translate(MILES_PER_HOUR)
        else:
            limits = WINDSTRENGTH_MPS
            report.wind_unit = self.translate(METERS_PER_SECOND)

        dialog = []
        if report.day is not None:
            dialog.append('forecast')
        if "Location" not in message.data:
            dialog.append('local')
        dialog.append(report.wind.strength)
        dialog = '.'.join(dialog)
        self.speak_dialog(dialog, report)

    @intent_handler(IntentBuilder("").require("ConfirmQueryCurrent").one_of(
        "Hot", "Cold").optionally("Location").optionally("Today").build())
    def handle_isit_hot(self, message):
        """ Handler for utterances similar to
        is it hot today?, is it cold? etc
        """
        return self.__handle_typed(message, 'hot')

    # TODO This seems to present current temp, or possibly just hottest temp
    @intent_handler(IntentBuilder("").optionally("How").one_of("Hot", "Cold")
                    .one_of("ConfirmQueryFuture", "ConfirmQueryCurrent")
                    .optionally("Location").optionally("RelativeDay").build())
    def handle_how_hot_or_cold(self, message):
        """ Handler for utterances similar to
        how hot will it be today?, how cold will it be? , etc
        """
        response_type = 'high.temperature' if message.data.get('Hot') \
            else 'low.temperature'
        return self.__handle_typed(message, response_type)

    @intent_handler(IntentBuilder("").require("How").one_of("Hot", "Cold")
                    .one_of("ConfirmQueryFuture", "ConfirmQueryCurrent")
                    .optionally("Location").optionally("RelativeDay").build())
    def handle_how_hot_or_cold_alt(self, message):
        self.handle_how_hot_or_cold(message)

    @intent_handler(IntentBuilder("").require("ConfirmQuery")
                    .require("Snowing").optionally("Location").build())
    def handle_isit_snowing(self, message):
        """ Handler for utterances similar to "is it snowing today?"
        """
        report = self.__populate_report(message)

        if report is None:
            self._report_no_data()
            return

        dialog = self.__select_condition_dialog(message, report,
                                                "snow", "snowing")
        self.speak_dialog(dialog, report)

    @intent_handler(IntentBuilder("").require("ConfirmQuery").require(
        "Clear").optionally("Location").build())
    def handle_isit_clear(self, message):
        """ Handler for utterances similar to "is it clear skies today?"
        """
        report = self.__populate_report(message)

        if report is None:
            self._report_no_data()
            return

        dialog = self.__select_condition_dialog(message, report, "clear")
        self.speak_dialog(dialog, report)

    @intent_handler(IntentBuilder("").require("ConfirmQuery").require(
        "Cloudy").optionally("Location").optionally("RelativeTime").build())
    def handle_isit_cloudy(self, message):
        """ Handler for utterances similar to "is it cloudy skies today?"
        """
        report = self.__populate_report(message)

        if report is None:
            self._report_no_data()
            return

        dialog = self.__select_condition_dialog(message, report, "cloudy")
        self.speak_dialog(dialog, report)

    @intent_handler(IntentBuilder("").require("ConfirmQuery").require(
        "Foggy").optionally("Location").build())
    def handle_isit_foggy(self, message):
        """ Handler for utterances similar to "is it foggy today?"
        """
        report = self.__populate_report(message)

        if report is None:
            self._report_no_data()
            return

        dialog = self.__select_condition_dialog(message, report, "fog",
                                                "foggy")
        self.speak_dialog(dialog, report)

    @intent_handler(IntentBuilder("").require("ConfirmQuery").require(
        "Raining").optionally("Location").build())
    def handle_isit_raining(self, message):
        """ Handler for utterances similar to "is it raining today?"
        """
        report = self.__populate_report(message)

        if report is None:
            self._report_no_data()
            return

        dialog = self.__select_condition_dialog(message, report, "rain",
                                                "raining")
        self.speak_dialog(dialog, report)

    @intent_handler("do.i.need.an.umbrella.intent")
    def handle_need_umbrella(self, message):
        self.handle_isit_raining(message)

    @intent_handler(IntentBuilder("").require("ConfirmQuery").require(
        "Storm").optionally("Location").build())
    def handle_isit_storming(self, message):
        """ Handler for utterances similar to "is it storming today?"
        """
        report = self.__populate_report(message)

        if report is None:
            self._report_no_data()
            return

        dialog = self.__select_condition_dialog(message, report, "storm")
        self.speak_dialog(dialog, report)

    # Handle: When will it rain again?
    @intent_handler(IntentBuilder("").require("When").optionally(
        "Next").require("Precipitation").optionally("Location").build())
    def handle_next_precipitation(self, message):
        report = self._initialize_report(message)

        # Get a date from spoken request
        today, _ = self.__extract_datetime("today")
        when, _ = self.__extract_datetime(message.data.get('utterance'),
                                          lang=self.lang)

        # search the forecast for precipitation
        daily_forecast = self.weather_api.daily_forecast(
            report.long_location,
            self.weather_config.latitude,
            self.weather_config.longitude,
            limit=10
        )

        weathers = daily_forecast.get_forecast()

        if weathers is None:
            self._report_no_data()
            return

        weathers = weathers.get_weathers()
        for weather in weathers:

            forecastDate = datetime.fromtimestamp(weather.get_reference_time())

            if when and when != today:
                # User asked about a specific date, is this it?
                if forecastDate.date() != when.date():
                    continue

            rain = weather.get_rain()
            if rain and rain["all"] > 0:
                data = {
                    "modifier": "",
                    "precip": "rain",
                    "day": self.__to_day(forecastDate, preface=True)
                }
                if rain["all"] < 10:
                    data["modifier"] = self.__translate("light")
                elif rain["all"] > 20:
                    data["modifier"] = self.__translate("heavy")

                self.speak_dialog("precipitation expected", data)
                return

            snow = weather.get_snow()
            if snow and snow["all"] > 0:
                data = {
                    "modifier": "",
                    "precip": "snow",
                    "day": self.__to_day(forecastDate, preface=True)
                }
                if snow["all"] < 10:
                    data["modifier"] = self.__translate("light")
                elif snow["all"] > 20:
                    data["modifier"] = self.__translate("heavy")

                self.speak_dialog("precipitation expected", data)
                return

        self.speak_dialog("no precipitation expected", report)

    # Handle: How humid is it?
    @intent_handler(IntentBuilder("").require("Query").require("Humidity")
                    .optionally("RelativeDay").optionally("Location").build())
    def handle_humidity(self, message):
        report = self._initialize_report(message)

        when, _ = self.__extract_datetime(message.data.get('utterance'),
                                          lang=self.lang)
        today, _ = self.__extract_datetime("today")
        if when is None or when == today:
            weather_at_place = self.weather_api.weather_at_place(
                report.long_location,
                self.weather_config.latitude,
                self.weather_config.longitude
            )
            weather = weather_at_place.get_weather()
        else:
            # Get forecast for that day
            weather = self.__get_forecast(
                when,
                report.long_location,
                self.weather_config.latitude,
                self.weather_config.longitude
            )

        if weather is None:
            self._report_no_data()
            return

        if weather.get_humidity() == 0:
            self.speak_dialog("do not know")
            return

        value = self.translate('percentage.number',
                               {'num': str(weather.get_humidity())})
        loc = message.data.get('Location')
        self.__report_condition(self.__translate("humidity"), value, when, loc)

    # Handle: How windy is it?
    @intent_handler(IntentBuilder("").require("Query").require("Windy")
                    .optionally("Location").optionally("ConfirmQuery")
                    .optionally("RelativeDay").build())
    def handle_windy(self, message):
        report = self._initialize_report(message)

        when, _ = self.__extract_datetime(message.data.get('utterance'))
        today, _ = self.__extract_datetime("today")
        if when is None or when == today:
            weather_at_place = self.weather_api.weather_at_place(
                report.long_location,
                self.weather_config.latitude,
                self.weather_config.longitude
            )
            weather = weather_at_place.get_weather()
        else:
            # Get forecast for that day
            weather = self.__get_forecast(
                when,
                report.long_location,
                self.weather_config.latitude,
                self.weather_config.longitude
            )

        if weather is None:
            self._report_no_data()
            return

        if not weather or weather.get_wind() == 0:
            self.speak_dialog("do not know")
            return

        wind = Wind(weather, report.temperature_unit)
        if wind.direction is not None:
            direction = self.__translate(wind.direction)
            value = self.__translate(
                "wind.speed.dir",
                data=dict(
                    dir=direction,
                    speed=nice_number(wind.speed),
                    unit=report.temperature_unit
                )
            )
        else:
            value = self.__translate(
                "wind.speed",
                data=dict(
                    speed=nice_number(wind.speed),
                    unit=report.temperature_unit
                )
            )
        loc = message.data.get('Location')
        self.__report_condition(self.__translate("winds"), value, when, loc)
        self.speak_dialog('wind.strength.' + wind.strength)

    # Handle: When is the sunrise?
    @intent_handler(IntentBuilder("").one_of("Query", "When")
                    .optionally("Location").require("Sunrise").build())
    def handle_sunrise(self, message):
        report = self._initialize_report(message)
        when, _ = self.__extract_datetime(message.data.get('utterance'))
        today, _ = self.__extract_datetime("today")
        if when is None or when.date() == today.date():
            weather_at_place = self.weather_api.weather_at_place(
                report.long_location,
                self.weather_config.latitude,
                self.weather_config.longitude
            )
            weather = weather_at_place.get_weather()
            if weather is None:
                self._report_no_data()
                return
        else:
            # Get forecast for that day
            # weather = self.__get_forecast(when, report['full_location'],
            #                               report['lat'], report['lon'])

            # There appears to be a bug in OWM, it can't extract the sunrise/
            # sunset from forecast objects.  As of March 2018 OWM said it was
            # "in the roadmap". Just say "I don't know" for now
            weather = None
        if not weather or weather.get_humidity() == 0:
            self.speak_dialog("do not know")
            return

        # uses device tz so if not set (eg Mark 1) this is UTC.
        dtSunrise = datetime.fromtimestamp(weather.get_sunrise_time())
        if time.tzname == ("UTC", "UTC"):
            dtSunrise = self.__to_Local(dtSunrise.replace(tzinfo=pytz.utc))
        spoken_time = self.__nice_time(dtSunrise, use_ampm=True)
        self.speak_dialog('sunrise', {'time': spoken_time})

    # Handle: When is the sunset?
    @intent_handler(IntentBuilder("").one_of("Query", "When")
                    .optionally("Location").require("Sunset").build())
    def handle_sunset(self, message):
        report = self._initialize_report(message)

        when, _ = self.__extract_datetime(message.data.get('utterance'))
        today, _ = self.__extract_datetime("today")
        if when is None or when.date() == today.date():
            weather_at_place = self.weather_api.weather_at_place(
                report.long_location,
                self.weather_config.latitude,
                self.weather_config.longitude
            )
            weather = weather_at_place.get_weather()
            if weather is None:
                self._report_no_data()
                return
        else:
            # Get forecast for that day
            # weather = self.__get_forecast(when, report['full_location'],
            #                               report['lat'], report['lon'])

            # There appears to be a bug in OWM, it can't extract the sunrise/
            # sunset from forecast objects.  As of March 2018 OWM said it was
            # "in the roadmap". Just say "I don't know" for now
            weather = None
        if not weather or weather.get_humidity() == 0:
            self.speak_dialog("do not know")
            return

        # uses device tz so if not set (eg Mark 1) this is UTC.
        dtSunset = datetime.fromtimestamp(weather.get_sunset_time())
        if time.tzname == ("UTC", "UTC"):
            dtSunset = self.__to_Local(dtSunset.replace(tzinfo=pytz.utc))
        spoken_time = self.__nice_time(dtSunset, use_ampm=True)
        self.speak_dialog('sunset', {'time': spoken_time})

    def _initialize_report(self, message=None):
        """Initialize a weather report.

        Arguments:
            message: Message bus event message
        """
        report = WeatherReport(self.weather_config)
        if message is not None:
            intent_location = self._get_requested_location(message)
            report.short_location = intent_location
            report.long_location = intent_location
            report.temperature_unit = self._get_requested_unit(message)

        return report

    def __handle_typed(self, message, response_type):
        # Get a date from requests like "weather for next Tuesday"
        today, _ = self.__extract_datetime("today")
        when, _ = self.__extract_datetime(
            message.data.get('utterance'), lang=self.lang)

        report = self._initialize_report(message)
        if when and when.date() != today.date():
            self.log.debug("Doing a forecast {} {}".format(today, when))
            return self.report_forecast(report, when,
                                        dialog=response_type)
        report = self.__populate_report(message)
        if report is None:
            return self._report_no_data()

        if report.get('time'):
            self.__report_weather("at.time", report, response_type)
        else:
            self.__report_weather('current', report, response_type)
        self.mark2_forecast(report)

    def __populate_report(self, message):
        unit = self._get_requested_unit(message)
        # Get a date from requests like "weather for next Tuesday"
        today, _ = self.__extract_datetime('today', lang='en-us')
        when, _ = self.__extract_datetime(
            message.data.get('utterance'), lang=self.lang)
        when = when or today  # Get todays date if None was found
        self.log.debug('extracted when: {}'.format(when))

        report = self._initialize_report(message)

        # Check if user is asking for a specific time today
        if when.date() == today.date() and when.time() != today.time():
            self.log.info("Forecast for time: {}".format(when))
            return self.__populate_for_time(report, when, unit)
        # Check if user is asking for a specific day
        elif today.date() != when.date():
            # Doesn't seem to be hitable, safety?
            self.log.info("Forecast for: {} {}".format(today, when))
            return self.__populate_forecast(report, when, unit,
                                            preface_day=True)
        # Otherwise user is asking for weather right now
        else:
            self.log.info("Forecast for now")
            return self.__populate_current(report, unit)

        return None

    def __populate_for_time(self, report: WeatherReport, when, unit=None):
        # TODO localize time to report location
        three_hr_fcs = self.weather_api.three_hours_forecast(
            report.long_location,
            self.weather_config.latitude,
            self.weather_config.longitude
        )

        if three_hr_fcs is None:
            return None

        if not three_hr_fcs:
            return None
        earliest_fc = three_hr_fcs.get_forecast().get_weathers()[0]
        if when < earliest_fc.get_reference_time(timeformat='date'):
            fc_weather = earliest_fc
        else:
            try:
                fc_weather = three_hr_fcs.get_weather_at(when)
            except Exception as e:
                # fc_weather = three_hr_fcs.get_forecast().get_weathers()[0]
                self.log.error("Error: {0}".format(e))
                return None

        report.add_forecast(fc_weather)
        # Min and Max temps not available in 3hr forecast
        report.humidity = self.translate(
            'percentage.number', {'num': report.humidity}
        )
        report.wind_format = '{speed}'
        fc_time = fc_weather.get_reference_time(timeformat='date')
        report.time = self.__to_time_period(self.__to_Local(fc_time))
        report.day = self.__to_day(when, preface=True)

        return report

    def __populate_current(self, report: WeatherReport, unit=None):
        # Return None if report is None
        if report is None:
            return None

        # Get current conditions
        weather_at_place = self.weather_api.weather_at_place(
            report.short_location,
            self.weather_config.latitude,
            self.weather_config.longitude
        )
        current_weather = weather_at_place.get_weather()
        if current_weather is None:
            return None

        today = current_weather.get_reference_time(timeformat='date')
        self.log.debug("Populating report for now: {}".format(today))

        # Get forecast for the day
        # can get 'min', 'max', 'eve', 'morn', 'night', 'day'
        # Set time to 12 instead of 00 to accomodate for timezones
        forecast_weather = self.__get_forecast(
            self.__to_Local(today),
            report.short_location,
            self.weather_config.latitude,
            self.weather_config.longitude
        )
        if forecast_weather is None:
            return None

        # Change encoding of the localized report to utf8 if needed
        report.add_forecast(current_weather)
        if self.weather_api.encoding != 'utf8':
            report.condition.encode(self.weather_api.encoding).decode('utf8')
        report.condition = self.__translate(report.condition)
        report.humidity = self.translate(
            'percentage.number', {'num': report.humidity}
        )

        report.wind_format = "{speed} {direction}"
        today, _ = self.__extract_datetime('today', lang='en-us')
        report.day = self.__to_day(today, preface=True)

        return report

    def __populate_forecast(self, report, when, preface_day=False):
        """ Populate the report and return it.

        Arguments:
            report (WeatherReport): report base
            when : date for report

        Returns: None if no report available otherwise dict with weather info
        """
        self.log.debug("Populating forecast report for: {}".format(when))

        # Return None if report is None
        if report is None:
            return None

        forecast = self.__get_forecast(
            when,
            report.short_location,
            self.weather_config.latitude,
            self.weather_config.longitude
        )

        if forecast is None:
            return None  # No forecast available

        # This converts a status like "sky is clear" to new text and tense,
        # because you don't want: "Friday it will be 82 and the sky is clear",
        # it should be 'Friday it will be 82 and the sky will be clear'
        # or just 'Friday it will be 82 and clear.
        # TODO: Run off of status IDs instead of text `.get_weather_code()`?
        report.add_forecast(forecast)
        report.condition = self.__translate(report.condition, True)
        # Can get temps for 'min', 'max', 'eve', 'morn', 'night', 'day'
        report.humidity = self.translate(
            'percentage.number', {'num': report.humidity}
        )
        report.day = self.__to_day(when, preface_day)

        return report

    def __select_condition_dialog(self, message, report, noun, exp=None):
        """ Select the relevant dialog file for condition based reports.

        A condition can for example be "snow" or "rain".

        Arguments:
            message (obj): message from user
            report (WeatherReport): weather report data
            noun (string): name of condition eg snow
            exp (string): condition as verb or adjective eg Snowing

        Returns:
            dialog (string): name of dialog file
        """
        if report is None:
            # Empty report most likely caused by location not found
            return 'do not know'

        if exp is None:
            exp = noun
        alternative_voc = '{}Alternatives'.format(noun.capitalize())
        if self.voc_match(report.condition, exp.capitalize()):
            dialog = 'affirmative.condition'
        elif report.time is not None:
            # Standard response for time based dialog eg 'evening'
            if self.voc_match(report.condition, alternative_voc):
                dialog = 'cond.alternative'
            else:
                dialog = 'no.cond.predicted'
        elif self.voc_match(report.condition, alternative_voc):
            dialog = '{}.alternative'.format(exp.lower())
        else:
            dialog = 'no.{}.predicted'.format(noun.lower())

        if "Location" not in message.data:
            dialog = 'local.' + dialog
        if report.day is not None:
            dialog = 'forecast.' + dialog
        if (report.time is not None and
                ('at.time.' + dialog) in self.dialog_renderer.templates):
            dialog = 'at.time.' + dialog
        return dialog

    def report_forecast(self, report, when, dialog='weather', unit=None,
                        preface_day=True):
        """ Speak forecast for specific day.

        Arguments:
            report (dict): report base
            when : date for report
            dialog (str): dialog type, defaults to 'weather'
            unit: Unit type to use when presenting
            preface_day (bool): if appropriate day preface should be added
                                eg "on Tuesday" but NOT "on tomorrow"
        """
        report = self.__populate_forecast(report, when, unit, preface_day)
        if report is None:
            data = {'day': self.__to_day(when, preface_day)}
            self._report_no_data(data)
            return

        self.__report_weather('forecast', report, rtype=dialog)

    def report_multiday_forecast(self, report, when=None,
                                 num_days=3, set_days=None, dialog='weather',
                                 unit=None, preface_day=True):
        """ Speak forecast for multiple sequential days.

        Arguments:
            report (dict): report base
            when (datetime): date of first day for report, defaults to today
            num_days (int): number of days to report, defaults to 3
            set_days (list(datetime)): list of specific days to report
            dialog (str): dialog type, defaults to 'weather'
            unit: Unit type to use when presenting, defaults to user preference
            preface_day (bool): if appropriate day preface should be added
                                eg "on Tuesday" but NOT "on tomorrow"
        """

        today, _ = self.__extract_datetime('today')
        if when is None:
            when = today

        if set_days:
            days = set_days
        else:
            days = [when + timedelta(days=i) for i in range(num_days)]

        no_report = list()
        for day in days:
            if day == today:
                self.__populate_current(report, day)
                report.day = self.__to_day(day, preface_day)
                self.__report_weather('forecast', report, rtype=dialog)
            else:
                report = self.__populate_forecast(report, day, unit,
                                                  preface_day)
                if report is None:
                    no_report.append(self.__to_day(day, False))
                    continue
                self.__report_weather('forecast', report, rtype=dialog)

        if no_report:
            dates = join_list(no_report, 'and')
            dates = self.translate('on') + ' ' + dates
            data = {'day': dates}
            self._report_no_data(data)

    def __report_weather(self, timeframe, report, rtype='weather',
                         separate_min_max=False):
        """ Report the weather verbally and visually.

        Produces an utterance based on the timeframe and rtype parameters.
        The report also provides location context. The dialog file used will
        be:
            "timeframe(.local).rtype"

        Arguments:
            timeframe (str): 'current' or 'future'.
            report (WeatherReport): Dictionary with report information (temperatures
                           and such.
            rtype (str): report type, defaults to 'weather'
            separate_min_max (bool): a separate dialog for min max temperatures
                                     will be output if True (default: False)
        """

        # Convert code to matching weather icon on Mark 1
        if report.short_location:
            report.short_location = self.weather_api.location_translations.get(
                report.short_location, report.short_location
            )
        weather_code = str(report.icon)
        img_code = self.CODES[weather_code]

        # Display info on a screen
        # Mark-2
        self.gui["current"] = report.temperature
        self.gui["min"] = report.minimum_temperature
        self.gui["max"] = report.maximum_temperature
        self.gui["location"] = report.long_location.replace(', ', '\n')
        self.gui["condition"] = report.condition
        self.gui["icon"] = report.icon
        self.gui["weathercode"] = img_code
        self.gui["humidity"] = report.humidity or "--"
        self.gui["wind"] = report.format_wind() or "--"
        self.gui.show_pages(
            ["weather.qml", "highlow.qml", "forecast1.qml", "forecast2.qml"]
        )
        # Mark-1
        self.enclosure.deactivate_mouth_events()
        self.enclosure.weather_display(img_code, report.temperature)

        dialog_name = timeframe
        if report.short_location == self.location_pretty:
            dialog_name += ".local"
        dialog_name += "." + rtype
        self.log.debug("Dialog: " + dialog_name)
        self.speak_dialog(dialog_name, report)

        # Just show the icons while still speaking
        mycroft.audio.wait_while_speaking()

        # Speak the high and low temperatures
        if separate_min_max:
            self.speak_dialog('min.max', report)
            self.gui.show_page("highlow.qml")
            mycroft.audio.wait_while_speaking()

        self.enclosure.activate_mouth_events()
        self.enclosure.mouth_reset()

    def __report_condition(self, name, value, when, location=None):
        # Report a specific value
        data = {
            "condition": name,
            "value": value,
        }
        report_type = "report.condition"
        today, _ = self.__extract_datetime("today")
        if when and when.date() != today.date():
            data["day"] = self.__to_day(when, preface=True)
            report_type += ".future"
        if location:
            data["location"] = location
            report_type += ".at.location"
        self.speak_dialog(report_type, data)

    def __get_forecast(self, when, location, lat, lon):
        """ Get a forecast for the given time and location.

        Arguments:
            when (datetime): Local datetime for report
            location: location
            lat: Latitude for report
            lon: Longitude for report
        """

        # search for the requested date in the returned forecast data
        forecasts = self.weather_api.daily_forecast(location, lat, lon, limit=14)
        forecasts = forecasts.get_forecast()
        for weather in forecasts.get_weathers():
            forecastDate = weather.get_reference_time("date")
            if forecastDate.date() == when.date():
                # found the right day, now format up the results
                return weather

        # No forecast for the given day
        return None

    def _get_requested_unit(self, message: Message) -> Optional[str]:
        """Determine if the temperature unit was specified in the utterance.

        Arguments:
            message: message bus message from intent service

        Returns:
            'fahrenheit' or 'celsius' if unit is in the intent, otherwise None
        """
        intent_unit = None
        message_unit = message.data.get("Unit")
        if intent_unit is not None:
            if self.voc_match(message_unit, FAHRENHEIT.title()):
                intent_unit = FAHRENHEIT
            else:
                intent_unit = CELSIUS

        return intent_unit

    def _get_requested_location(self, message: Message) -> Optional[str]:
        """Determine if a geographical location was specified in the utterance.

        Arguments:
            message: message bus message from intent service

        Returns:
            geographical location if in the intent, otherwise None
        """
        return message.data.get("Location", None)

    def concat_dialog(self, current, dialog, data=None):
        return current + " " + self.translate(dialog, data)

    def __get_seqs_from_list(self, nums):
        """Get lists of sequential numbers from list.

        Arguments:
            nums (list): list of int eg indices

        Returns:
            None if no sequential numbers found
            seq_nums (list[list]): list of sequence lists
        """
        current_seq, seq_nums = [], []
        seq_active = False
        for idx, day in enumerate(nums):
            if idx+1 < len(nums) and nums[idx+1] == (day + 1):
                current_seq.append(day)
                seq_active = True
            elif seq_active:
                # last day in sequence
                current_seq.append(day)
                seq_nums.append(current_seq.copy())
                current_seq = []
                seq_active = False

        # if len(seq_nums) == 0:
        #     return None
        return seq_nums

    def __get_temperature(self, weather, key, unit=None):
        # Extract one of the temperatures from the weather data.
        # Typically it has: 'temp', 'min', 'max', 'morn', 'day', 'night'
        try:
            unit = unit or self.weather_config.temperature_unit
            # fallback to general temperature if missing
            temp = weather.get_temperature(unit)[key]
            if temp is not None:
                return str(int(round(temp)))
            else:
                return ''
        except Exception as e:
            self.log.warning('No temperature available ({})'.format(repr(e)))
            return ''

    def __api_error(self, e):
        if isinstance(e, LocationNotFoundError):
            self.speak_dialog('location.not.found')
        elif e.response.status_code == 401:
            from mycroft import Message
            self.bus.emit(Message("mycroft.not.paired"))
        else:
            self._report_no_data()

    def _report_no_data(self, data: dict = None) -> None:
        """Do processes when Report Processes malfunction

        Arguments:
            data: Needed data for dialog on weather error processing
        """
        if data is None:
            self.speak_dialog("cant.get.forecast")
        else:
            self.speak_dialog("no.forecast", data)

    def __to_day(self, when, preface=False):
        """ Provide date in speakable form

            Arguments:
                when (datetime)
                preface (bool): if appropriate preface should be included
                                eg "on Monday" but NOT "on tomorrow"
            Returns:
                string: the speakable date text
        """
        now = datetime.now()
        speakable_date = nice_date(when, lang=self.lang, now=now)
        # Test if speakable_date is a relative reference eg "tomorrow"
        days_diff = (when.date() - now.date()).days
        if preface and (-1 > days_diff or days_diff > 1):
            speakable_date = "{} {}".format(self.translate('on.date'),
                                            speakable_date)
        # If day is less than a week in advance, just say day of week.
        if days_diff <= 6:
            speakable_date = speakable_date.split(',')[0]
        return speakable_date

    def __to_Local(self, when):
        try:
            # First try with modern mycroft.util.time functions
            return to_local(when)
        except Exception:
            # Fallback to the old pytz code
            if not when.tzinfo:
                when = when.replace(tzinfo=pytz.utc)
            timezone = pytz.timezone(self.location["timezone"]["code"])
            return when.astimezone(timezone)

    def __to_time_period(self, when):
        # Translate a specific time '9am' to period of the day 'morning'
        hour = when.time().hour
        period = None
        if hour >= 1 and hour < 5:
            period = "early morning"
        if hour >= 5 and hour < 12:
            period = "morning"
        if hour >= 12 and hour < 17:
            period = "afternoon"
        if hour >= 17 and hour < 20:
            period = "evening"
        if hour >= 20 or hour < 1:
            period = "overnight"
        if period is None:
            self.log.error("Unable to parse time as a period of day")
        return period

    # Suggestion TODO: Add a parameter to extract_datetime to add a default Timezone
    def __extract_datetime(self, text, anchorDate=None, lang=None, default_time=None):
        # Change timezone returned by extract_datetime from Local to UTC
        extracted_dt = extract_datetime(text, anchorDate, lang, default_time)
        if extracted_dt is None:
            # allow calls to unpack values even if None returned.
            return (None, None)
        when, text = extracted_dt
        return to_utc(when), text

    def __translate(self, condition, future=False, data=None):
        # behaviour of method dialog_renderer.render(...) has changed - instead
        # of exception when given template is not found now simply the
        # templatename is returned!?!
        if (future and
                (condition + ".future") in self.dialog_renderer.templates):
            return self.translate(condition + ".future", data)
        if condition in self.dialog_renderer.templates:
            return self.translate(condition, data)
        else:
            return condition

    def __nice_time(self, dt, lang="en-us", speech=True, use_24hour=False,
                    use_ampm=False):
        # compatibility wrapper for nice_time
        nt_supported_languages = ['en', 'es', 'it', 'fr', 'de',
                                  'hu', 'nl', 'da']
        if not (lang[0:2] in nt_supported_languages):
            lang = "en-us"
        return nice_time(dt, lang, speech, use_24hour, use_ampm)


def create_skill():
    return WeatherSkill()

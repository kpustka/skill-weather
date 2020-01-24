# Copyright 2020, Mycroft AI Inc.
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


import pytz
from datetime import datetime, timedelta
from mycroft import MycroftSkill
from mycroft.util.format import nice_date
from mycroft.util.format import nice_time as m_nice_time
from mycroft.util.log import LOG
from mycroft.util.parse import extract_datetime as m_extract_datetime

try:
    from mycroft.util.time import to_local as m_to_local
except Exception:
    pass

# These methods only exist as temporary work arounds to other Mycroft utils
# They should be removed as soon as possible.


def extract_datetime(text, anchorDate=None, lang=None, default_time=None):
    # Change timezone returned by extract_datetime from Local to UTC
    extracted_dt = m_extract_datetime(text, anchorDate, lang, default_time)
    if extracted_dt is None:
        # allow calls to unpack values even if None returned.
        return (None, None)
    when, text = extracted_dt
    return when.replace(tzinfo=pytz.utc), text


def nice_time(dt, lang="en-us", speech=True, use_24hour=False, use_ampm=False):
    # compatibility wrapper for nice_time
    nt_supported_languages = ["en", "es", "it", "fr", "de", "hu", "nl", "da"]
    if not (lang[0:2] in nt_supported_languages):
        lang = "en-us"
    return m_nice_time(dt, lang, speech, use_24hour, use_ampm)


def to_local(when, location_code):
    try:
        # First try with modern mycroft.util.time functions
        return m_to_local(when)
    except Exception:
        # Fallback to the old pytz code
        if not when.tzinfo:
            when = when.replace(tzinfo=pytz.utc)
        timezone = pytz.timezone(location_code)
        return when.astimezone(timezone)


def to_time_period(when):
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
        LOG.error("Unable to parse time as a period of day")
    return period

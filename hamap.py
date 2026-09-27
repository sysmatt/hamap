#!/usr/bin/env python3
"""hamap - High-resolution ham radio contact map generator from ADIF logs."""

import argparse
import configparser
import logging
import logging.handlers
import math
import os
import re
import sys
import unicodedata
import warnings
from collections import Counter, defaultdict

# Set matplotlib backend before pyplot import; check argv directly so this
# works regardless of import order.
import matplotlib
if '--preview' not in sys.argv:
    matplotlib.use('Agg')

HAMAP_DIR = os.path.expanduser('~/.hamap')

# Redirect cartopy's data cache to ~/.hamap/cartopy before any cartopy
# submodule is imported.
os.makedirs(HAMAP_DIR, exist_ok=True)
import cartopy                                    # noqa: E402  (must follow makedirs)
cartopy.config['data_dir'] = os.path.join(HAMAP_DIR, 'cartopy')

import cartopy.crs as ccrs                        # noqa: E402
import cartopy.feature as cfeature                # noqa: E402
import matplotlib.patches as mpatches             # noqa: E402
import matplotlib.pyplot as plt                   # noqa: E402
import matplotlib.ticker as mticker               # noqa: E402
import numpy as np                                # noqa: E402


# =============================================================================
# BEGIN APPLOG
# =============================================================================

class AppLogger:
    """
    Application logger with TRACE/VERBOSE/DEBUG/INFO levels and configurable destinations.

    Custom levels:
        TRACE   =  5   super-verbose full dumps
        VERBOSE = 15   informative detail beyond normal INFO

    Destinations: console stream (stderr by default), log file, syslog.
    Console output: plain message for INFO, [LEVEL] prefix for all other levels.
    File/syslog output: includes program name, timestamp, and level.

    Construction:
        log = AppLogger("myprog", level=AppLogger.VERBOSE)
        log = AppLogger.from_args(args, "myprog")
        log = AppLogger.from_args(args, "myprog", cfg=config)  # args > cfg > defaults

    Config file ([logging] section):
        [logging]
        level   = verbose    # trace | debug | verbose | info
        logfile = /path/to/myprog.log
        syslog  = false
    """

    TRACE   = 5
    VERBOSE = 15

    # Register custom levels at class-definition time
    logging.addLevelName(TRACE,   "TRACE")
    logging.addLevelName(VERBOSE, "VERBOSE")

    # Inject .trace() and .verbose() into logging.Logger
    def _trace_m(self, msg, *a, **kw):
        if self.isEnabledFor(5):  self._log(5,  msg, a, **kw)
    def _verbose_m(self, msg, *a, **kw):
        if self.isEnabledFor(15): self._log(15, msg, a, **kw)
    logging.Logger.trace   = _trace_m
    logging.Logger.verbose = _verbose_m
    del _trace_m, _verbose_m

    class _ConsoleFormatter(logging.Formatter):
        """Plain message for INFO; [LEVEL] prefix for everything else."""
        def __init__(self, plain_levels=None):
            super().__init__()
            self._plain = frozenset(plain_levels if plain_levels is not None else [logging.INFO])

        def format(self, record):
            msg = record.getMessage()
            if record.levelno not in self._plain:
                msg = f"[{record.levelname}] {msg}"
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                msg = f"{msg}\n{record.exc_text}"
            return msg

    def __init__(
        self,
        name: str,
        level: int = logging.INFO,
        *,
        stream=sys.stderr,
        logfile: str | None = None,
        use_syslog: bool = False,
        plain_levels: list[int] | None = None,
    ):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        self._logger.handlers.clear()
        self._logger.propagate = False

        if stream is not None:
            h = logging.StreamHandler(stream)
            h.setLevel(level)
            h.setFormatter(self._ConsoleFormatter(plain_levels=plain_levels))
            self._logger.addHandler(h)

        if logfile:
            h = logging.FileHandler(logfile, encoding="utf-8")
            h.setLevel(level)
            h.setFormatter(logging.Formatter(
                fmt=f"%(asctime)s {name} [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            self._logger.addHandler(h)

        if use_syslog:
            address = "/dev/log" if os.path.exists("/dev/log") else ("localhost", 514)
            h = logging.handlers.SysLogHandler(address=address)
            h.setLevel(level)
            h.setFormatter(logging.Formatter(f"{name}: [%(levelname)s] %(message)s"))
            self._logger.addHandler(h)

    @classmethod
    def from_args(
        cls,
        args,
        program_name: str,
        cfg: configparser.ConfigParser | None = None,
        **kwargs,
    ) -> "AppLogger":
        """
        Build from an argparse Namespace, optionally merging a ConfigParser.
        Args take precedence over the [logging] config section.
        """
        _lvl = {"trace": cls.TRACE, "debug": logging.DEBUG,
                "verbose": cls.VERBOSE, "info": logging.INFO}

        cfg_level   = _lvl.get((cfg.get("logging", "level", fallback="") if cfg else "").lower(), logging.INFO)
        cfg_logfile = cfg.get("logging", "logfile", fallback=None)        if cfg else None
        cfg_syslog  = cfg.getboolean("logging", "syslog", fallback=False) if cfg else False

        if   getattr(args, "trace",   False): level = cls.TRACE
        elif getattr(args, "debug",   False): level = logging.DEBUG
        elif getattr(args, "verbose", False): level = cls.VERBOSE
        else:                                 level = cfg_level

        logfile    = getattr(args, "logfile", None) or cfg_logfile
        use_syslog = getattr(args, "syslog", False) or cfg_syslog

        return cls(name=program_name, level=level, logfile=logfile, use_syslog=use_syslog, **kwargs)

    def trace(self,   msg, *a, **kw): self._logger.trace(msg,   *a, **kw)
    def verbose(self, msg, *a, **kw): self._logger.verbose(msg, *a, **kw)
    def debug(self,   msg, *a, **kw): self._logger.debug(msg,   *a, **kw)
    def info(self,    msg, *a, **kw): self._logger.info(msg,    *a, **kw)
    def warning(self, msg, *a, **kw): self._logger.warning(msg, *a, **kw)
    def error(self,   msg, *a, **kw): self._logger.error(msg,   *a, **kw)
    def critical(self,msg, *a, **kw): self._logger.critical(msg,*a, **kw)

# =============================================================================
# END APPLOG
# =============================================================================


# --------------------------------------------------------------------------- #
# Band → colour mapping
# --------------------------------------------------------------------------- #

BAND_COLORS = {
    '160m': '#CC0000',
    '80m':  '#FF4400',
    '60m':  '#FF6600',
    '40m':  '#FF9900',
    '30m':  '#FFCC00',
    '20m':  '#33CC33',
    '17m':  '#00CCBB',
    '15m':  '#3399FF',
    '12m':  '#8844FF',
    '10m':  '#CC00FF',
    '6m':   '#FF00CC',
    '4m':   '#FF66CC',
    '2m':   '#FF99DD',
    '70cm': '#AAAAAA',
    '23cm': '#CCCCCC',
}
UNKNOWN_COLOR = '#FFFF44'

# Remapping from ADIF COUNTRY values to Natural Earth names, kept only for
# cases where normalization + fuzzy matching cannot bridge the gap on their own.
_DXCC_TO_MAPUNIT = {
    # Wholly different names
    'United States':         'United States of America',
    'European Russia':       'Russia',
    'Asiatic Russia':        'Russia',
    'Asiatic Turkey':        'Turkey',
    'European Turkey':       'Turkey',
    'North Korea':           'Dem. Rep. Korea',
    'South Korea':           'Republic of Korea',
    'Laos':                  'Lao PDR',
    'East Timor':            'Timor-Leste',
    'Hong Kong':             'Hong Kong S.A.R.',
    'Macau':                 'Macao S.A.R',
    'North Macedonia':       'Macedonia',
    'South Sudan':           'S. Sudan',
    'Vatican City':          'Vatican',
    'Ivory Coast':           "Côte d'Ivoire",
    'Cape Verde':            'Cabo Verde',
    'Western Sahara':        'W. Sahara',
    # ADIF singular vs NE plural/compound (norm expansion covers many, not these)
    'Madeira Island':        'Madeira',
    'Reunion Island':        'Reunion',
    'Ascension Island':      'Ascension',
    # 'Sint' ≠ 'Saint' — expansion can't bridge this
    'St. Maarten':           'Sint Maarten',
    # Short ADIF name vs NE full compound name — fuzzy ratio too low
    'St Vincent':            'Saint Vincent and the Grenadines',
    'St. Vincent':           'Saint Vincent and the Grenadines',
    'US Virgin Islands':     'United States Virgin Islands',
    # Multi-entity DXCC mapped to best single NE polygon
    'Dodecanese':            'Greece',
    'Ceuta and Melilla':     'Ceuta',
    # admin_1 records with empty name_en — must map to native-language name field
    'Sardinia':              'Sardegna',        # Italian admin_1 name
    'Canary Islands':        'Islas Canarias',  # Spanish admin_1 name
}


_BAND_ORDER = ['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m',
               '12m', '10m', '6m', '4m', '2m', '70cm', '23cm']


def _band_sort_key(band):
    try:
        return _BAND_ORDER.index(band)
    except ValueError:
        return 1000 if band == '?' else 999


# --------------------------------------------------------------------------- #
# Built-in country centroid lookup (lat, lon) — fallback when no grid square
# --------------------------------------------------------------------------- #

COUNTRY_CENTROIDS = {
    # North America
    'United States':       (39.50, -98.35),
    'Canada':              (60.00, -95.00),
    'Mexico':              (23.60, -102.50),
    # Central America / Caribbean
    'Cuba':                (22.00,  -79.50),
    'Jamaica':             (18.10,  -77.30),
    'Puerto Rico':         (18.20,  -66.50),
    'Costa Rica':          ( 9.70,  -83.70),
    'Panama':              ( 8.50,  -80.00),
    'Guatemala':           (15.80,  -90.20),
    'Honduras':            (15.20,  -86.20),
    'Nicaragua':           (12.90,  -85.20),
    'El Salvador':         (13.80,  -88.90),
    'Belize':              (17.20,  -88.50),
    'Bahamas':             (25.00,  -77.40),
    'Dominican Republic':  (18.70,  -70.20),
    'Haiti':               (19.00,  -72.30),
    'Trinidad and Tobago': (10.70,  -61.20),
    'Barbados':            (13.20,  -59.50),
    'Cayman Islands':      (19.30,  -81.40),
    'Guadeloupe':          (16.30,  -61.40),
    'Martinique':          (14.60,  -61.00),
    'Bermuda':             (32.30,  -64.80),
    'Aruba':               (12.50,  -70.00),
    'Curacao':             (12.20,  -68.90),
    'Virgin Islands':      (18.35,  -64.85),
    # South America
    'Brazil':              (-10.00,  -53.00),
    'Argentina':           (-34.00,  -64.00),
    'Chile':               (-30.00,  -71.00),
    'Colombia':            (  4.00,  -73.00),
    'Peru':                (-10.00,  -76.00),
    'Venezuela':           (  8.00,  -66.00),
    'Bolivia':             (-17.00,  -65.00),
    'Ecuador':             ( -2.00,  -77.50),
    'Paraguay':            (-23.30,  -58.20),
    'Uruguay':             (-33.00,  -56.00),
    'Guyana':              (  5.00,  -59.00),
    'Suriname':            (  4.00,  -56.00),
    'French Guiana':       (  4.00,  -53.00),
    # Europe
    'England':             ( 52.50,   -1.50),
    'United Kingdom':      ( 54.00,   -2.50),
    'Scotland':            ( 56.50,   -4.00),
    'Wales':               ( 52.30,   -3.50),
    'Northern Ireland':    ( 54.70,   -6.50),
    'Ireland':             ( 53.00,   -8.00),
    'France':              ( 46.00,    2.00),
    'Germany':             ( 51.00,   10.00),
    'Italy':               ( 42.80,   12.80),
    'Spain':               ( 40.00,   -3.70),
    'Portugal':            ( 39.50,   -8.00),
    'Netherlands':         ( 52.30,    5.30),
    'Belgium':             ( 50.80,    4.50),
    'Switzerland':         ( 47.00,    8.20),
    'Austria':             ( 47.50,   14.00),
    'Sweden':              ( 62.00,   15.00),
    'Norway':              ( 65.00,   13.00),
    'Finland':             ( 64.00,   26.00),
    'Denmark':             ( 56.00,   10.00),
    'Poland':              ( 52.00,   20.00),
    'Czech Republic':      ( 49.80,   15.50),
    'Slovakia':            ( 48.70,   19.70),
    'Hungary':             ( 47.20,   19.40),
    'Romania':             ( 45.90,   25.00),
    'Bulgaria':            ( 42.70,   25.50),
    'Greece':              ( 39.00,   22.00),
    'Turkey':              ( 39.00,   35.00),
    'Ukraine':             ( 49.00,   32.00),
    'Russia':              ( 60.00,  100.00),
    'European Russia':     ( 58.00,   40.00),
    'Asiatic Russia':      ( 60.00,  100.00),
    'Belarus':             ( 53.70,   28.00),
    'Serbia':              ( 44.00,   21.00),
    'Croatia':             ( 45.10,   15.20),
    'Slovenia':            ( 46.10,   14.80),
    'Bosnia and Herzegovina': (44.20, 17.90),
    'Montenegro':          ( 42.70,   19.40),
    'North Macedonia':     ( 41.60,   21.70),
    'Albania':             ( 41.20,   20.20),
    'Kosovo':              ( 42.60,   21.10),
    'Lithuania':           ( 56.00,   24.00),
    'Latvia':              ( 57.00,   25.00),
    'Estonia':             ( 58.60,   25.00),
    'Luxembourg':          ( 49.80,    6.10),
    'Iceland':             ( 65.00,  -18.00),
    'Malta':               ( 35.90,   14.50),
    'Cyprus':              ( 35.10,   33.40),
    'San Marino':          ( 43.90,   12.50),
    'Andorra':             ( 42.50,    1.50),
    'Monaco':              ( 43.70,    7.40),
    'Liechtenstein':       ( 47.10,    9.50),
    'Vatican City':        ( 41.90,   12.45),
    'Moldova':             ( 47.40,   28.50),
    'Faroe Islands':       ( 62.00,   -6.80),
    'Azores':              ( 38.50,  -28.00),
    'Canary Islands':      ( 28.30,  -15.50),
    'Madeira':             ( 32.80,  -17.00),
    'Gibraltar':           ( 36.14,   -5.35),
    # Asia
    'Japan':               ( 36.20,  138.30),
    'China':               ( 35.00,  103.00),
    'India':               ( 20.60,   79.00),
    'South Korea':         ( 37.00,  127.50),
    'North Korea':         ( 40.00,  127.00),
    'Taiwan':              ( 23.70,  121.00),
    'Hong Kong':           ( 22.40,  114.10),
    'Macau':               ( 22.20,  113.50),
    'Indonesia':           ( -5.00,  120.00),
    'Philippines':         ( 13.00,  122.00),
    'Thailand':            ( 15.00,  101.00),
    'Vietnam':             ( 16.00,  108.00),
    'Malaysia':            (  3.00,  109.00),
    'Singapore':           (  1.30,  103.80),
    'Bangladesh':          ( 23.70,   90.40),
    'Pakistan':            ( 30.00,   69.00),
    'Sri Lanka':           (  7.90,   80.70),
    'Nepal':               ( 28.40,   84.10),
    'Myanmar':             ( 17.00,   96.00),
    'Cambodia':            ( 12.60,  104.90),
    'Laos':                ( 18.20,  103.90),
    'Afghanistan':         ( 34.50,   65.00),
    'Iran':                ( 32.00,   53.70),
    'Iraq':                ( 33.20,   43.70),
    'Saudi Arabia':        ( 24.00,   45.00),
    'Israel':              ( 31.50,   34.80),
    'Jordan':              ( 31.00,   36.00),
    'Lebanon':             ( 33.90,   35.50),
    'Syria':               ( 35.00,   38.00),
    'Kuwait':              ( 29.30,   47.70),
    'United Arab Emirates': (24.00,   54.00),
    'Qatar':               ( 25.30,   51.20),
    'Bahrain':             ( 26.00,   50.60),
    'Oman':                ( 21.00,   57.00),
    'Yemen':               ( 15.50,   47.50),
    'Kazakhstan':          ( 48.00,   68.00),
    'Uzbekistan':          ( 41.40,   64.60),
    'Kyrgyzstan':          ( 41.20,   74.80),
    'Tajikistan':          ( 38.90,   71.30),
    'Turkmenistan':        ( 39.00,   59.60),
    'Mongolia':            ( 46.50,  103.50),
    'Azerbaijan':          ( 40.30,   47.60),
    'Georgia':             ( 42.00,   43.50),
    'Armenia':             ( 40.10,   45.00),
    'Maldives':            (  3.20,   73.20),
    'Bhutan':              ( 27.50,   90.50),
    'Brunei':              (  4.50,  114.70),
    'East Timor':          ( -8.90,  125.70),
    # Africa
    'South Africa':        (-29.00,   25.10),
    'Egypt':               ( 27.00,   30.00),
    'Nigeria':             ( 10.00,    8.70),
    'Ethiopia':            (  9.10,   40.50),
    'Kenya':               (  0.00,   38.00),
    'Tanzania':            ( -6.00,   35.00),
    'Uganda':              (  1.40,   32.30),
    'Algeria':             ( 28.00,    2.70),
    'Morocco':             ( 32.00,   -5.00),
    'Tunisia':             ( 34.00,    9.00),
    'Libya':               ( 27.00,   17.00),
    'Sudan':               ( 15.00,   30.00),
    'South Sudan':         (  7.00,   30.00),
    'Ghana':               (  7.90,   -1.00),
    'Cameroon':            (  4.20,   12.40),
    'Angola':              (-11.20,   17.90),
    'Mozambique':          (-18.70,   35.50),
    'Zimbabwe':            (-19.00,   29.90),
    'Zambia':              (-13.10,   27.90),
    'Botswana':            (-22.30,   24.70),
    'Namibia':             (-22.60,   17.10),
    'Madagascar':          (-20.00,   47.00),
    'Democratic Republic of the Congo': (-2.90, 24.00),
    'Congo':               ( -0.30,   15.80),
    'Senegal':             ( 14.50,  -14.50),
    'Mali':                ( 17.60,   -2.00),
    'Niger':               ( 17.60,    8.10),
    'Chad':                ( 15.50,   18.70),
    'Somalia':             ( 10.00,   46.20),
    'Rwanda':              ( -2.00,   29.90),
    'Burundi':             ( -3.40,   29.90),
    'Malawi':              (-13.30,   34.30),
    'Liberia':             (  6.40,   -9.40),
    'Sierra Leone':        (  8.50,  -11.80),
    'Guinea':              ( 11.70,  -11.60),
    "Cote d'Ivoire":       (  7.50,   -5.50),
    'Ivory Coast':         (  7.50,   -5.50),
    'Burkina Faso':        ( 12.40,   -1.60),
    'Togo':                (  8.60,    0.80),
    'Benin':               (  9.30,    2.30),
    'Mauritania':          ( 20.30,  -10.90),
    'Djibouti':            ( 11.80,   42.60),
    'Eritrea':             ( 15.20,   39.80),
    'Mozambique':          (-18.70,   35.50),
    'Reunion':             (-21.10,   55.50),
    'Mauritius':           (-20.20,   57.50),
    'Seychelles':          ( -4.70,   55.50),
    'Cape Verde':          ( 16.00,  -24.00),
    'Sao Tome and Principe': (0.30,    6.70),
    'Equatorial Guinea':   (  1.70,   10.30),
    'Gabon':               ( -1.00,   11.80),
    'Central African Republic': (7.00, 21.00),
    # Oceania / Pacific
    'Australia':           (-25.70,  134.50),
    'New Zealand':         (-41.30,  173.30),
    'Papua New Guinea':    ( -6.30,  143.90),
    'Fiji':                (-18.00,  179.00),
    'Solomon Islands':     ( -9.00,  160.00),
    'Vanuatu':             (-16.00,  167.00),
    'Samoa':               (-13.60, -172.40),
    'American Samoa':      (-14.30, -170.70),
    'Tonga':               (-20.00, -175.00),
    'Guam':                ( 13.50,  144.80),
    'Hawaii':              ( 20.50, -157.00),
    'New Caledonia':       (-21.30,  165.60),
    'French Polynesia':    (-17.70, -149.40),
    'Marshall Islands':    (  7.10,  171.10),
    'Micronesia':          (  7.40,  150.60),
    'Palau':               (  7.50,  134.60),
    'Kiribati':            (  1.90,  -157.40),
    'Nauru':               ( -0.50,  166.90),
    'Tuvalu':              ( -8.50,  179.20),
    'Cook Islands':        (-21.20, -159.80),
    'Niue':                (-19.10, -169.90),
}


# --------------------------------------------------------------------------- #
# US state and Canadian province centroids  (for --label-states)
# --------------------------------------------------------------------------- #

STATE_CENTROIDS = {
    # US States
    'Alabama':       ( 32.8,  -86.8), 'Alaska':         ( 64.0, -153.0),
    'Arizona':       ( 34.3, -111.1), 'Arkansas':        ( 34.7,  -92.4),
    'California':    ( 36.8, -119.4), 'Colorado':        ( 39.0, -105.5),
    'Connecticut':   ( 41.6,  -72.7), 'Delaware':        ( 39.1,  -75.4),
    'Florida':       ( 27.8,  -81.5), 'Georgia':         ( 32.7,  -83.4),
    'Hawaii':        ( 20.5, -157.0), 'Idaho':           ( 44.3, -114.5),
    'Illinois':      ( 40.6,  -89.2), 'Indiana':         ( 40.3,  -86.1),
    'Iowa':          ( 42.0,  -93.1), 'Kansas':          ( 38.5,  -98.4),
    'Kentucky':      ( 37.6,  -84.7), 'Louisiana':       ( 31.0,  -91.8),
    'Maine':         ( 45.3,  -69.0), 'Maryland':        ( 39.0,  -76.8),
    'Massachusetts': ( 42.3,  -71.8), 'Michigan':        ( 44.3,  -85.4),
    'Minnesota':     ( 46.3,  -94.3), 'Mississippi':     ( 32.7,  -89.6),
    'Missouri':      ( 38.4,  -92.5), 'Montana':         ( 47.0, -110.0),
    'Nebraska':      ( 41.5,  -99.5), 'Nevada':          ( 39.0, -117.0),
    'New Hampshire': ( 44.0,  -71.6), 'New Jersey':      ( 40.1,  -74.5),
    'New Mexico':    ( 34.5, -106.1), 'New York':        ( 42.7,  -75.4),
    'North Carolina':( 35.6,  -79.4), 'North Dakota':    ( 47.5, -100.3),
    'Ohio':          ( 40.4,  -82.7), 'Oklahoma':        ( 35.6,  -97.5),
    'Oregon':        ( 44.1, -120.5), 'Pennsylvania':    ( 40.9,  -77.8),
    'Rhode Island':  ( 41.7,  -71.5), 'South Carolina':  ( 33.9,  -80.9),
    'South Dakota':  ( 44.4, -100.2), 'Tennessee':       ( 35.9,  -86.5),
    'Texas':         ( 31.5,  -99.3), 'Utah':            ( 39.4, -111.1),
    'Vermont':       ( 44.1,  -72.7), 'Virginia':        ( 37.5,  -79.0),
    'Washington':    ( 47.4, -120.5), 'West Virginia':   ( 38.6,  -80.6),
    'Wisconsin':     ( 44.8,  -89.6), 'Wyoming':         ( 43.0, -107.6),
    # Canadian Provinces and Territories
    'Alberta':       ( 54.0, -114.4), 'British Columbia':( 54.0, -124.0),
    'Manitoba':      ( 55.0,  -97.0), 'New Brunswick':   ( 46.5,  -66.5),
    'Newfoundland and Labrador': (53.0, -60.0),
    'Northwest Territories': (64.3, -119.2),
    'Nova Scotia':   ( 44.8,  -63.2), 'Nunavut':         ( 70.0,  -86.0),
    'Ontario':       ( 51.0,  -85.0), 'Prince Edward Island': (46.4, -63.4),
    'Quebec':        ( 53.0,  -72.0), 'Saskatchewan':    ( 54.0, -106.4),
    'Yukon':         ( 63.0, -135.0),
}


# --------------------------------------------------------------------------- #
# ADIF parsing
# --------------------------------------------------------------------------- #

def _parse_adif_fields(text):
    """Extract {NAME: value} pairs from a block of ADIF text."""
    fields = {}
    pat = re.compile(r'<([^:>]+):(\d+)(?::[^>]*)?>', re.IGNORECASE)
    pos = 0
    while pos < len(text):
        m = pat.search(text, pos)
        if not m:
            break
        name = m.group(1).upper()
        length = int(m.group(2))
        start = m.end()
        value = text[start:start + length].strip()
        if value and name not in ('EOR', 'EOH'):
            fields[name] = value
        pos = start + length
    return fields


def parse_adif(path, log):
    """Return (header_fields, [qso_records]) from an ADIF file."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except FileNotFoundError:
        log.error("File not found: %s", path)
        sys.exit(1)

    eoh = text.upper().find('<EOH>')
    if eoh != -1:
        header_text = text[:eoh]
        body_text = text[eoh + 5:]
    else:
        header_text = ''
        body_text = text

    header = _parse_adif_fields(header_text)

    records = []
    for chunk in re.split(r'<EOR>', body_text, flags=re.IGNORECASE):
        chunk = chunk.strip()
        if not chunk:
            continue
        fields = _parse_adif_fields(chunk)
        if fields.get('CALL'):
            records.append(fields)

    log.verbose("Parsed %d QSO records from %s", len(records), path)
    return header, records


# --------------------------------------------------------------------------- #
# Location resolution
# --------------------------------------------------------------------------- #

def maidenhead_to_latlon(grid):
    """Convert a Maidenhead locator (4 or 6 chars) to (lat, lon) centre."""
    if not grid or len(grid) < 4:
        return None
    g = grid.upper().strip()
    if not re.match(r'^[A-R]{2}[0-9]{2}', g):
        return None

    lon = (ord(g[0]) - ord('A')) * 20.0 - 180.0
    lat = (ord(g[1]) - ord('A')) * 10.0 - 90.0
    lon += int(g[2]) * 2.0
    lat += int(g[3]) * 1.0

    if len(g) >= 6 and g[4].isalpha() and g[5].isalpha():
        lon += (ord(g[4].upper()) - ord('A')) * (2.0 / 24.0)
        lat += (ord(g[5].upper()) - ord('A')) * (1.0 / 24.0)
        lon += 1.0 / 24.0     # centre of subsquare
        lat += 0.5 / 24.0
    else:
        lon += 1.0             # centre of 2° × 1° square
        lat += 0.5

    return (lat, lon)


def maidenhead_bounds(grid):
    """(lon0, lat0, lon1, lat1) of a 4- or 6-char Maidenhead square, or None."""
    g = (grid or '').upper().strip()
    if not re.match(r'^[A-R]{2}[0-9]{2}', g):
        return None
    lon = (ord(g[0]) - 65) * 20.0 - 180.0 + int(g[2]) * 2.0
    lat = (ord(g[1]) - 65) * 10.0 - 90.0 + int(g[3]) * 1.0
    if len(g) >= 6 and 'A' <= g[4] <= 'X' and 'A' <= g[5] <= 'X':
        lon += (ord(g[4]) - 65) * (2.0 / 24.0)
        lat += (ord(g[5]) - 65) * (1.0 / 24.0)
        return lon, lat, lon + 2.0 / 24.0, lat + 1.0 / 24.0
    return lon, lat, lon + 2.0, lat + 1.0


def _country_centroid(name):
    if not name:
        return None
    direct = COUNTRY_CENTROIDS.get(name)
    if direct:
        return direct
    name_lower = name.lower()
    for k, v in COUNTRY_CENTROIDS.items():
        if k.lower() == name_lower:
            return v
    return None


def _parse_adif_coord(s):
    """
    ADIF LAT/LON ('XDDD MM.MMM', e.g. N043 31.031 = 43° 31.031′ N) → degrees.
    Plain decimal degrees are accepted too.  Returns None if unparseable.
    """
    s = (s or '').strip().upper()
    m = re.match(r'^([NSEW])\s*(\d{1,3})\s+(\d{1,2}(?:\.\d+)?)$', s)
    if m:
        val = int(m.group(2)) + float(m.group(3)) / 60.0
        return -val if m.group(1) in 'SW' else val
    try:
        return float(s)
    except ValueError:
        return None


def resolve_location(qso, log):
    """
    Return (lat, lon) for a QSO record, or None if unresolvable.  Records
    where it came from in qso['_LOC_SRC'] ('latlon', 'grid' or 'country'), so
    callers can tell a real position from a country-centroid stand-in.
    """
    # 1. Explicit LAT/LON fields (all-zero values are placeholders)
    lat = _parse_adif_coord(qso.get('LAT', ''))
    lon = _parse_adif_coord(qso.get('LON', ''))
    if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0) \
            and -90 <= lat <= 90 and -180 <= lon <= 180:
        qso['_LOC_SRC'] = 'latlon'
        return (lat, lon)

    # 2. Maidenhead grid square
    grid = qso.get('GRIDSQUARE', '')
    if grid:
        pos = maidenhead_to_latlon(grid)
        if pos:
            qso['_LOC_SRC'] = 'grid'
            return pos

    # 3. Country centroid fallback
    centroid = _country_centroid(qso.get('COUNTRY', ''))
    if centroid:
        qso['_LOC_SRC'] = 'country'
        return centroid

    log.trace("No location for %s (grid=%r, country=%r)",
              qso.get('CALL', '?'), grid, qso.get('COUNTRY', ''))
    return None


# --------------------------------------------------------------------------- #
# Offline data setup
# --------------------------------------------------------------------------- #

def do_setup(log):
    """Pre-download all Natural Earth data needed for offline map rendering."""
    log.info("Downloading Natural Earth map data to %s", HAMAP_DIR)
    log.info("Requires an internet connection — this may take a few minutes.")

    import cartopy.io.shapereader as shpreader

    tasks = [
        ('physical',  'land',              '10m'),
        ('physical',  'ocean',             '10m'),
        ('physical',  'coastline',         '10m'),
        ('physical',  'lakes',             '10m'),
        ('cultural',  'admin_0_countries',         '10m'),
        ('cultural',  'admin_1_states_provinces',  '10m'),
        ('physical',  'land',              '110m'),
        ('physical',  'ocean',             '110m'),
        ('physical',  'coastline',         '110m'),
        ('cultural',  'admin_0_countries', '110m'),
        ('cultural',  'admin_0_map_units',  '10m'),
    ]

    ok = 0
    for category, name, resolution in tasks:
        log.info("  %s/%s @ %s ...", category, name, resolution)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                shpreader.natural_earth(resolution=resolution, category=category, name=name)
            log.info("    cached")
            ok += 1
        except Exception as exc:
            log.warning("    FAILED: %s", exc)

    log.info("Setup complete: %d/%d datasets cached at %s",
             ok, len(tasks), os.path.join(HAMAP_DIR, 'cartopy'))


# --------------------------------------------------------------------------- #
# Map generation
# --------------------------------------------------------------------------- #

_MAP_BG     = '#0d1b2a'    # deep navy — ocean / figure background
_LAND_COLOR = '#2e2a22'    # warm sand (dark)
_COAST_CLR  = '#5e5444'    # coastline colour
_GRID_CLR   = '#1e3050'    # graticule colour
_BORDER_CLR = '#4a4234'    # country borders
_STATE_CLR  = '#655a48'    # US state / CA province borders

# Axes rect within the figure (left, bottom, width, height as fractions)
_AX_RECT    = [0.0, 0.04, 1.0, 0.92]

_DOT_SIZE   = 14           # contact dot marker area (pt²)
_CALL_CLR   = '#e8eef4'    # callsign text inside info boxes
_BOX_MAX_ROWS = 6          # callsign rows per info box before wrapping to columns
_COL_GAP    = 1.5          # blank characters between callsign columns
_BOX_PAD    = 0.45         # info box inner padding, in character heights
_OCEAN_DETOUR = 2.5        # --ocean-boxes: max open-water distance vs nearest spot
_OCEAN_MAX_DEG = 8.0       # --ocean-boxes: ...and never further than this from the dot
_EXTRA_RINGS = 2           # placer: rings searched beyond the first with a free spot
_CROSS_PENALTY = 2.0       # placer: cost of a leader crossing, in box heights of distance

# Latitude limits for --extent poles (Greenland/Svalbard in, Antarctica out)
_POLES_LAT  = (-60.0, 84.0)

# Allowed lon/lat span ratio for --extent auto
_AUTO_ASPECT = (1.0, 4.0)


def _lighten(hex_color, frac):
    """Blend *hex_color* toward white by *frac* (0..1) — for text on dark fills."""
    r, g, b = (int(hex_color.lstrip('#')[i:i + 2], 16) for i in (0, 2, 4))
    return '#{:02x}{:02x}{:02x}'.format(*(round(c + (255 - c) * frac) for c in (r, g, b)))


def _contrast(a, b):
    """WCAG contrast ratio between two hex colours."""
    def lum(hx):
        c = [int(hx.lstrip('#')[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
        c = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _text_safe(color, bg=_MAP_BG, target=4.5):
    """*color* lightened just enough to reach WCAG *target* contrast on *bg*."""
    frac, out = 0.0, color
    while _contrast(out, bg) < target and frac < 1.0:
        frac += 0.02
        out = _lighten(color, frac)
    return out


# Band colours for callsign text on the dark box fill: 160 m, 12 m and 10 m
# are lightened to reach 4.5:1; the rest already pass unchanged.
BAND_TEXT_COLORS  = {b: _text_safe(c) for b, c in BAND_COLORS.items()}
UNKNOWN_TEXT_CLR  = _text_safe(UNKNOWN_COLOR)


def band_text_color(band):
    return BAND_TEXT_COLORS.get(band, UNKNOWN_TEXT_CLR)


def compute_extent(points, args):
    """
    Return (lon0, lon1, lat0, lat1) for the map view.

    full  — whole world
    poles — whole world, polar regions trimmed
    auto  — bounding box of *points* (contacts + home) plus a margin, with the
            aspect ratio clamped to _AUTO_ASPECT.
    """
    if args.extent == 'full' or not points:
        return (-180.0, 180.0, -90.0, 90.0)
    if args.extent == 'poles':
        return (-180.0, 180.0, *_POLES_LAT)

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)

    # Margin: room for info boxes, which prefer to sit above their dots
    pad_lon = max(10.0, 0.12 * (lon1 - lon0))
    pad_lat = max(6.0,  0.12 * (lat1 - lat0))
    lon0, lon1 = lon0 - pad_lon, lon1 + pad_lon
    lat0, lat1 = lat0 - pad_lat, lat1 + pad_lat * 1.5

    # Keep the aspect (lon span / lat span) sane: no tall slivers, no thin strips.
    # Within the limits the figure height simply follows the extent.
    span_lon = min(lon1 - lon0, 360.0)
    span_lat = min(lat1 - lat0, 180.0)
    if span_lon / span_lat < _AUTO_ASPECT[0]:
        span_lon = span_lat * _AUTO_ASPECT[0]
    elif span_lon / span_lat > _AUTO_ASPECT[1]:
        span_lat = span_lon / _AUTO_ASPECT[1]

    def _fit(lo, hi, span, wlo, whi):
        """Centre *span* on [lo, hi], then shift/clip into [wlo, whi]."""
        span = min(span, whi - wlo)
        c    = (lo + hi) / 2.0
        a, b = c - span / 2.0, c + span / 2.0
        if a < wlo:
            a, b = wlo, wlo + span
        if b > whi:
            a, b = whi - span, whi
        return a, b

    lon0, lon1 = _fit(lon0, lon1, span_lon, -180.0, 180.0)
    lat0, lat1 = _fit(lat0, lat1, span_lat,  -90.0,  90.0)
    return (lon0, lon1, lat0, lat1)


def figure_height_for(extent, width):
    """Figure height (in) that gives the extent square pixels — no letterboxing."""
    lon0, lon1, lat0, lat1 = extent
    return width * (lat1 - lat0) / (lon1 - lon0) / _AX_RECT[3]


# --------------------------------------------------------------------------- #
# Grouping contacts by grid square / location
# --------------------------------------------------------------------------- #

def _country_areas(log):
    """{lower(country name): area in deg²} from Natural Earth, for label priority."""
    import cartopy.io.shapereader as shpreader
    areas = {}
    try:
        shp = shpreader.natural_earth(
            resolution='10m', category='cultural', name='admin_0_countries')
        for rec in shpreader.Reader(shp).records():
            a = rec.attributes
            for f in ('NAME', 'NAME_LONG', 'ADMIN', 'FORMAL_EN'):
                if a.get(f):
                    areas.setdefault(a[f].lower(), rec.geometry.area)
    except Exception as exc:
        log.debug("Country areas unavailable, label priority disabled: %s", exc)
    return areas


_NE_ABBREVS = {'st': 'saint', 'ste': 'sainte', 'is': 'islands', 'isl': 'islands'}


def _ne_norm(s):
    """
    Normalise a place name for fuzzy comparison:
      - strip Unicode accents
      - fold '&' → 'and'
      - remove remaining punctuation
      - expand common geographic abbreviations (St.→saint, Is.→islands)
      - lowercase, collapse whitespace
    """
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'\s*&\s*', ' and ', s.lower())
    s = re.sub(r'[^\w\s]', ' ', s)
    words = [_NE_ABBREVS.get(w, w) for w in s.split()]
    return ' '.join(words)


def load_country_geometries(countries, log):
    """
    Return {country_name: shapely_geometry} for each ADIF COUNTRY name given.

    Matching pipeline (first hit wins):
      1. Explicit remap via _DXCC_TO_MAPUNIT
      2. Exact case-insensitive lookup in admin_0_map_units 10m (sovereign +
         overseas territories — covers small islands absent from 110m)
      3. Normalised lookup (strips accents, expands St./Is. abbreviations,
         folds &→and) — catches Cayman Is.↔Islands, Côte d'Ivoire, etc.
      4. Same two passes against admin_1_states_provinces 10m — catches
         sub-national DXCC entities (Alaska, Hawaii, Sardinia, Crete, …)
      5. difflib fuzzy match (cutoff 0.80) on the normalised admin_0 names
         — catches Faroe↔Faeroe, slight spelling variants

    Unmatched entities get no fill polygon; their label box is still drawn.
    """
    import cartopy.io.shapereader as shpreader
    import difflib

    ne_exact = {}   # lower(name) → geometry
    ne_norm  = {}   # _ne_norm(name) → geometry

    def _add(val, geom):
        val = (val or '').strip()
        if not val:
            return
        ne_exact.setdefault(val.lower(), geom)
        ne_norm.setdefault(_ne_norm(val), geom)

    # ── Primary: admin_0_map_units at 10m ────────────────────────────────────
    # At 10m, small territories (Guernsey, Isle of Man, Anguilla, …) are present
    # as separate polygons that are absent from the coarser 110m file.
    log.verbose("Country shapes: loading admin_0_map_units (10m)...")
    shp0 = shpreader.natural_earth(
        resolution='10m', category='cultural', name='admin_0_map_units')
    # Specific unit names first, broad ones (sovereign / admin country) second,
    # so e.g. 'France' maps to metropolitan France, not French Guiana, whose
    # SOVEREIGNT is also 'France'.
    recs0 = list(shpreader.Reader(shp0).records())
    for fields in (('NAME', 'NAME_LONG', 'GEOUNIT', 'SUBUNIT', 'BRK_NAME', 'NAME_CIAWF'),
                   ('ADMIN', 'SOVEREIGNT', 'FORMAL_EN', 'NAME_ALT')):
        for rec in recs0:
            for f in fields:
                _add(rec.attributes.get(f, ''), rec.geometry)

    # Snapshot the admin_0 normalised keys for fuzzy pass (step 5)
    ne_norm_keys_a0 = list(ne_norm.keys())

    # ── Secondary: admin_1_states_provinces at 10m ───────────────────────────
    # Needed for sub-national DXCC entities: Alaska, Hawaii, Sardinia (Sardegna
    # / name_en=Sardinia), Crete (Kriti / name_en=Crete), Balearic Islands, …
    log.verbose("Country shapes: loading admin_1_states_provinces (10m)...")
    shp1 = shpreader.natural_earth(
        resolution='10m', category='cultural', name='admin_1_states_provinces')
    for rec in shpreader.Reader(shp1).records():
        a = rec.attributes
        for f in ('name', 'name_en', 'name_alt', 'gn_name', 'woe_name'):
            _add(a.get(f, ''), rec.geometry)

    geometries = {}
    for country in countries:
        if not country:
            continue

        # Step 1: explicit remap
        target = _DXCC_TO_MAPUNIT.get(country, country)

        # Step 2: exact (case-insensitive)
        geom = ne_exact.get(target.lower())

        # Step 3: normalised
        if not geom:
            geom = ne_norm.get(_ne_norm(target))

        # Step 4: try original ADIF name through exact + norm (in case remapping
        # pointed at a wrong target but the original name actually matches)
        if not geom and target != country:
            geom = ne_exact.get(country.lower()) or ne_norm.get(_ne_norm(country))

        # Step 5: fuzzy against admin_0 normalised names only
        if not geom:
            q = _ne_norm(target)
            hits = difflib.get_close_matches(q, ne_norm_keys_a0, n=1, cutoff=0.80)
            if hits:
                geom = ne_norm[hits[0]]
                log.debug("Country shapes: fuzzy-matched %r → %r", country, hits[0])

        if geom:
            geometries[country] = geom
        else:
            log.debug("Country shapes: no match for %r (tried %r)",
                      country, target)

    log.verbose("Country shapes: matched %d of %d countries",
                len(geometries), sum(1 for c in countries if c))
    return geometries


# --------------------------------------------------------------------------- #
# Label placement  (pixel-coordinate greedy placer)
# --------------------------------------------------------------------------- #

def _ax_pixel_fns(ax, args):
    """Closures for pixel ↔ lat/lon conversion at render resolution."""
    dpi      = args.dpi
    fig_w_in, fig_h_in = ax.figure.get_size_inches()
    fig_w_px = fig_w_in * dpi
    fig_h_px = fig_h_in * dpi
    x0, y0, w, h = _AX_RECT      # figure is sized to the extent, so no aspect shrink
    ax_x0    = x0 * fig_w_px
    ax_y0    = y0 * fig_h_px
    ax_w_px  = w * fig_w_px
    ax_h_px  = h * fig_h_px
    lon0, lon1, lat0, lat1 = ax.hamap_extent
    lon_span, lat_span = lon1 - lon0, lat1 - lat0

    def ll_to_px(lon, lat):
        return (ax_x0 + (lon - lon0) / lon_span * ax_w_px,
                ax_y0 + (lat - lat0) / lat_span * ax_h_px)

    def px_to_ll(cx, cy):
        return (lat0 + (cy - ax_y0) / ax_h_px * lat_span,
                lon0 + (cx - ax_x0) / ax_w_px * lon_span)

    return ll_to_px, px_to_ll, ax_x0, ax_y0, ax_w_px, ax_h_px


# Candidate placement angles: straight up first, then sweeping outward.
# Using cos/sin convention: 0° = right, 90° = up in screen space.
_PLACE_ANGLES = [90, 60, 120, 30, 150, 0, 180, -30, -150,
                 -60, -120, -90, 75, 105, 45, 135, 15, 165,
                 -15, -165, -45, -135, -75, -105]


def place_labels(groups, args, log, ax, pre_placed=None, land_prep=None):
    """
    Two-pass greedy label placer working entirely in display (pixel) coordinates.

    pre_placed: optional list of [cx, cy, hw, hh, key] obstacles already on the
                map (e.g. country/state text labels) that callsign boxes must avoid.
    land_prep:  optional shapely PreparedGeometry of the world land polygons.
                When provided (--ocean-boxes) the placer also looks for a spot
                where the whole box is over open water, and takes it unless it
                is more than _OCEAN_DETOUR times as far as the nearest spot
                (and never more than _OCEAN_MAX_DEG from the dot).

    Returns (positions, boxes_px) where:
        positions = {key: (label_lat, label_lon)}   — box centres in data coords
        boxes_px  = {key: (cx, cy, hw, hh)}         — box geometry in pixels
    """
    font_pt = args.font_size
    dpi     = args.dpi

    ll_to_px, px_to_ll, ax_x0, ax_y0, ax_w_px, ax_h_px = _ax_pixel_fns(ax, args)

    # ── Per-group helpers ────────────────────────────────────────────────────
    def _box_px(info):
        """(w_px, h_px) of the box as drawn: header, rule, band rows, footer."""
        char_h  = font_pt * dpi / 72.0
        char_w  = char_h * 0.62
        leading = char_h * 1.2
        pad     = char_h * _BOX_PAD
        width, lines, _, _ = box_metrics(info)
        w_px = width * char_w + 2 * pad
        h_px = pad + char_h + pad * 0.5 + 1.0 + lines * leading + pad
        if info.get('more'):
            h_px += leading
        return w_px, h_px

    dot_r_px = math.sqrt(_DOT_SIZE / math.pi) * (dpi / 72.0) + 2.0
    # --ocean-boxes never sends a box further than _OCEAN_MAX_DEG out to sea
    ocean_cap_px = _OCEAN_MAX_DEG * (ll_to_px(1.0, 0.0)[0] - ll_to_px(0.0, 0.0)[0])
    from shapely.geometry import box as _sbox
    gap_px   = max(4.0, font_pt * dpi / 72.0 * 0.6)

    dot_obs = {k: ll_to_px(info['dot_lon'], info['dot_lat'])
               for k, info in groups.items()}

    # Pre-load fixed obstacles (country/state labels, etc.)
    placed    = list(pre_placed) if pre_placed else []
    positions = {}

    # ── Spatial index: obstacles bucketed into fixed-size pixel cells ────────
    # Boxes, dots and fixed labels all live here; entries are [cx, cy, hw, hh, key].
    cell_px = 256.0
    grid    = defaultdict(list)

    def _cells(cx, cy, hw, hh):
        x0 = int((cx - hw - gap_px) // cell_px)
        x1 = int((cx + hw + gap_px) // cell_px)
        y0 = int((cy - hh - gap_px) // cell_px)
        y1 = int((cy + hh + gap_px) // cell_px)
        return [(i, j) for i in range(x0, x1 + 1) for j in range(y0, y1 + 1)]

    def _index(entry):
        for c in _cells(*entry[:4]):
            grid[c].append(entry)

    def _unindex(entry):
        for c in _cells(*entry[:4]):
            grid[c].remove(entry)

    for entry in placed:
        _index(entry)
    for k, (dx, dy) in dot_obs.items():
        _index([dx, dy, dot_r_px, dot_r_px, k])

    # ── Leader lines (dot → box centre) in their own cell index ─────────────
    leaders = {}                      # key → (x0, y0, x1, y1)
    lgrid   = defaultdict(set)

    def _lcells(seg):
        x0, y0, x1, y1 = seg
        return [(i, j) for i in range(int(min(x0, x1) // cell_px), int(max(x0, x1) // cell_px) + 1)
                for j in range(int(min(y0, y1) // cell_px), int(max(y0, y1) // cell_px) + 1)]

    def _lset(key, cx, cy):
        _lclear(key)
        seg = (*dot_obs[key], cx, cy)
        leaders[key] = seg
        for c in _lcells(seg):
            lgrid[c].add(key)

    def _lclear(key):
        seg = leaders.pop(key, None)
        if seg:
            for c in _lcells(seg):
                lgrid[c].discard(key)

    def _cross(p, q):
        """True if segments p and q properly intersect (shared ends don't count)."""
        def orient(ax_, ay_, bx_, by_, cx_, cy_):
            v = (bx_ - ax_) * (cy_ - ay_) - (by_ - ay_) * (cx_ - ax_)
            return int(v > 1e-9) - int(v < -1e-9)
        a, b = (p[0], p[1]), (p[2], p[3])
        c, d = (q[0], q[1]), (q[2], q[3])
        return (orient(*a, *b, *c) * orient(*a, *b, *d) < 0 and
                orient(*c, *d, *a) * orient(*c, *d, *b) < 0)

    def _crossings(seg, skip=()):
        """Keys of placed leaders that *seg* crosses."""
        near = set()
        for c in _lcells(seg):
            near |= lgrid.get(c, set())
        # sorted: set order varies between runs (string hashing), and the
        # swap pass must visit crossings in the same order every time
        return sorted(k for k in near if k not in skip and _cross(seg, leaders[k]))

    def _in_bounds(cx, cy, hw, hh, margin=4.0):
        return (cx - hw >= ax_x0 + margin and
                cx + hw <= ax_x0 + ax_w_px - margin and
                cy - hh >= ax_y0 + margin and
                cy + hh <= ax_y0 + ax_h_px - margin)

    def _collides(cx, cy, hw, hh, skip_key=None):
        for c in _cells(cx, cy, hw, hh):
            for e in grid.get(c, ()):
                if e[4] == skip_key:
                    continue
                if (abs(cx - e[0]) < hw + e[2] + gap_px and
                        abs(cy - e[1]) < hh + e[3] + gap_px):
                    return True
        return False

    def _try_place(info, skip_key=None):
        """
        Nearest free spot for the box, searching outward from the dot.

        For each direction the base offset is the distance at which the box
        edge just clears the dot (so a wide box sits snugly above or below),
        then rings step outward by about one box height.
        """
        dx, dy     = ll_to_px(info['dot_lon'], info['dot_lat'])
        w_px, h_px = _box_px(info)
        hw, hh     = w_px / 2.0, h_px / 2.0
        clear      = dot_r_px + gap_px
        step       = max(h_px, 2.0 * gap_px)
        max_extra  = (clear + math.hypot(hw, hh)) * 24.0
        rays = []
        for ang_deg in _PLACE_ANGLES:
            c, s = math.cos(math.radians(ang_deg)), math.sin(math.radians(ang_deg))
            tx = (hw + clear) / abs(c) if abs(c) > 1e-6 else math.inf
            ty = (hh + clear) / abs(s) if abs(s) > 1e-6 else math.inf
            rays.append((c, s, min(tx, ty)))

        def _sweep(ocean_only=False, max_dist=None):
            """
            Free spots from the first ring that has any plus the next
            _EXTRA_RINGS rings; the winner is the nearest after a penalty for
            each existing leader line its own leader would cross (a slight
            bias keeps the preferred-direction order for ties).  Spots
            further than *max_dist* from the dot are not considered.
            """
            limit = max_extra if max_dist is None else min(max_extra, max_dist)
            extra, first, found = 0.0, None, []
            while extra <= limit:
                for n, (c, s, t0) in enumerate(rays):
                    cx = dx + (t0 + extra) * c
                    cy = dy + (t0 + extra) * s
                    if not _in_bounds(cx, cy, hw, hh):
                        continue
                    if max_dist is not None and math.hypot(cx - dx, cy - dy) > max_dist:
                        continue
                    if _collides(cx, cy, hw, hh, skip_key=skip_key):
                        continue
                    if ocean_only:
                        blat, blon = px_to_ll(cx - hw, cy - hh)
                        tlat, tlon = px_to_ll(cx + hw, cy + hh)
                        if land_prep.intersects(_sbox(blon, blat, tlon, tlat)):
                            continue   # touches land — skip during ocean pass
                    found.append((cx, cy, n))
                if found and first is None:
                    first = extra
                if first is not None and extra >= first + _EXTRA_RINGS * step:
                    break
                extra += step
            if not found:
                return None
            pen = _CROSS_PENALTY * step
            cx, cy, _ = min(found, key=lambda f: (
                math.hypot(f[0] - dx, f[1] - dy)
                + pen * len(_crossings((dx, dy, f[0], f[1]), skip=(skip_key,)))
                + 0.5 * f[2]))
            return cx, cy, hw, hh

        best = _sweep(ocean_only=False)
        if land_prep is None:
            return best
        # --ocean-boxes: take open water only when it isn't a big detour over
        # the nearest spot of any kind, so boxes with room beside their dot
        # stay there and only crowded ones head offshore.
        reach = ocean_cap_px if best is None else min(
            ocean_cap_px, math.hypot(best[0] - dx, best[1] - dy) * _OCEAN_DETOUR)
        return _sweep(ocean_only=True, max_dist=reach) or best

    # Crowded areas first: they have the fewest good spots, so let them claim
    # those before sparse neighbours spill in. Ties → busier groups first.
    # On a 1.6k-QSO log (with the ring search) p90 leader length fell ~25%, max ~30%.
    win    = 16.0 * font_pt * dpi / 72.0          # neighbourhood size, px
    bucket = defaultdict(int)
    for x, y in dot_obs.values():
        bucket[(int(x // win), int(y // win))] += 1

    def _crowd(k):
        bx, by = int(dot_obs[k][0] // win), int(dot_obs[k][1] // win)
        return sum(bucket.get((bx + i, by + j), 0) for i in (-1, 0, 1) for j in (-1, 0, 1))

    order = sorted(groups.keys(), key=lambda k: (-_crowd(k), -len(groups[k]['qsos'])))

    log.verbose("Label placement pass 1 (%d groups)...", len(order))
    boxes = {}
    for key in order:
        info   = groups[key]
        result = _try_place(info, skip_key=key)
        if result:
            cx, cy, hw, hh = result
        else:
            dx, dy     = ll_to_px(info['dot_lon'], info['dot_lat'])
            w_px, h_px = _box_px(info)
            hw, hh     = w_px / 2.0, h_px / 2.0
            cx, cy     = dx, dy + dot_r_px + hh + gap_px
            log.debug("Label placement fallback for %s", key)
        boxes[key] = [cx, cy, hw, hh, key]
        _index(boxes[key])
        _lset(key, cx, cy)
        positions[key] = px_to_ll(cx, cy)

    # Pass 2: boxes placed early may now find nothing better, but boxes that
    # were pushed out can sometimes slot into gaps left by the ring search.
    log.verbose("Label placement pass 2: relaxing distant labels...")
    improved = 0
    for key in sorted(boxes, key=lambda k: -math.hypot(boxes[k][0] - dot_obs[k][0],
                                                       boxes[k][1] - dot_obs[k][1])):
        entry    = boxes[key]
        dx, dy   = dot_obs[key]
        cur_dist = math.hypot(entry[0] - dx, entry[1] - dy)
        if cur_dist <= (entry[2] + entry[3] + dot_r_px + gap_px) * 1.5:
            continue
        _unindex(entry)
        _lclear(key)
        result = _try_place(groups[key], skip_key=key)
        if result and math.hypot(result[0] - dx, result[1] - dy) < cur_dist * 0.80:
            entry[:4] = result
            positions[key] = px_to_ll(result[0], result[1])
            improved += 1
        _index(entry)
        _lset(key, entry[0], entry[1])

    if improved:
        log.verbose("  Moved %d label(s) closer to their dots.", improved)

    def _n_crossings():
        return sum(len(_crossings(leaders[k], skip=(k,))) for k in leaders) // 2

    # Pass 3: two crossing leaders always get shorter in total when their
    # boxes trade places (triangle inequality), so swap wherever both boxes
    # still fit and no more crossings are created than removed.
    before, swaps = _n_crossings(), 0
    for _ in range(6):
        changed = False
        for a in list(boxes):
            for b in _crossings(leaders[a], skip=(a,)):
                if b not in boxes or a == b:
                    continue
                ea, eb = boxes[a], boxes[b]
                pa, pb = (ea[0], ea[1]), (eb[0], eb[1])
                old_len = (math.dist(dot_obs[a], pa) + math.dist(dot_obs[b], pb))
                new_len = (math.dist(dot_obs[a], pb) + math.dist(dot_obs[b], pa))
                if new_len >= old_len:
                    continue
                _unindex(ea); _unindex(eb)
                ok = (_in_bounds(pb[0], pb[1], ea[2], ea[3]) and
                      _in_bounds(pa[0], pa[1], eb[2], eb[3]) and
                      not _collides(pb[0], pb[1], ea[2], ea[3], skip_key=a) and
                      not _collides(pa[0], pa[1], eb[2], eb[3], skip_key=b) and
                      not (abs(pb[0] - pa[0]) < ea[2] + eb[2] + gap_px and
                           abs(pb[1] - pa[1]) < ea[3] + eb[3] + gap_px))
                if ok:
                    old_x = (len(_crossings(leaders[a], skip=(a, b))) +
                             len(_crossings(leaders[b], skip=(a, b))) + 1)
                    na, nb = (*dot_obs[a], *pb), (*dot_obs[b], *pa)
                    new_x = (len(_crossings(na, skip=(a, b))) +
                             len(_crossings(nb, skip=(a, b))) + int(_cross(na, nb)))
                    ok = new_x < old_x
                if ok:
                    ea[0], ea[1], eb[0], eb[1] = pb[0], pb[1], pa[0], pa[1]
                    positions[a] = px_to_ll(*pb)
                    positions[b] = px_to_ll(*pa)
                    _lset(a, *pb); _lset(b, *pa)
                    swaps += 1
                    changed = True
                _index(ea); _index(eb)
                if ok:
                    break
        if not changed:
            break
    log.verbose("  Leader crossings: %d → %d after %d swap(s)", before, _n_crossings(), swaps)

    placed.extend(boxes.values())
    boxes_px = {key: (cx, cy, hw, hh)
                for cx, cy, hw, hh, key in placed if key in groups}

    # Placement quality: leader line lengths (dot centre → box centre), in px
    if boxes_px:
        lens = sorted(math.hypot(cx - dot_obs[k][0], cy - dot_obs[k][1])
                      for k, (cx, cy, hw, hh) in boxes_px.items())
        log.verbose("  Leader length px: median %.0f, p90 %.0f, max %.0f, total %.0f",
                    lens[len(lens) // 2], lens[int(len(lens) * 0.9)], lens[-1], sum(lens))
    return positions, boxes_px


# --------------------------------------------------------------------------- #
# Map figure assembly
# --------------------------------------------------------------------------- #

def _qso_band(qso):
    return qso.get('BAND', '').strip().lower() or '?'


def box_sections(info, box_calls):
    """
    Info box body as band rows: [(label, band, items), ...], lowest band
    first, '?' (no BAND) last.  A station worked on several bands appears in
    each of their rows.  Sets info['more'] to a "+k more" footer when capped.

    box_calls None → every callsign; N → the N busiest callsigns;
    0 → a summary: calls and grids (unlabelled), then QSOs per band.
    """
    qsos     = info['qsos']
    per_call = Counter(q.get('CALL', '') for q in qsos)
    calls    = sorted(per_call)
    if box_calls == 0:
        n_grids = len({q.get('GRIDSQUARE', '').upper()[:4] for q in qsos
                       if q.get('GRIDSQUARE')})
        head = [f"{len(calls)} call{'s' if len(calls) != 1 else ''}"]
        if n_grids:
            head.append(f"{n_grids} grid{'s' if n_grids != 1 else ''}")
        per_band = Counter(_qso_band(q) for q in qsos)
        return [(None, None, head)] + [
            (b, b, [f"{n} QSO{'s' if n != 1 else ''}"])
            for b, n in sorted(per_band.items(), key=lambda kv: _band_sort_key(kv[0]))]
    shown = calls
    if box_calls is not None and len(calls) > box_calls:
        shown = sorted(sorted(calls, key=lambda c: (-per_call[c], c))[:box_calls])
        info['more'] = f"+{len(calls) - box_calls} more"
    shown = set(shown)
    by_band = defaultdict(set)
    for q in qsos:
        if q.get('CALL', '') in shown:
            by_band[_qso_band(q)].add(q.get('CALL', ''))
    return [(b, b, sorted(by_band[b])) for b in sorted(by_band, key=_band_sort_key)]


def box_columns(sections, area_box, summary):
    """Callsign columns for a box: region boxes aim for ~2.5:1 lines:columns,
    grid boxes wrap once they pass _BOX_MAX_ROWS lines."""
    n = sum(len(items) for _, _, items in sections)
    if summary or n <= 1:
        return 1
    if area_box:
        return max(1, round(math.sqrt(n / 2.5)))
    ncols = 1
    while (sum(math.ceil(len(items) / ncols) for _, _, items in sections) > _BOX_MAX_ROWS
           and ncols < n):
        ncols += 1
    return ncols


def box_metrics(info):
    """
    Layout of an info box in characters: (width, content lines, band-label
    column width, item width).  Shared by the placer and the renderer.
    """
    secs, ncols = info['sections'], info['ncols']
    lab    = max((len(l) for l, _, _ in secs if l), default=0)
    lab_w  = lab + 1 if lab else 0
    item_w = max((len(i) for _, _, items in secs for i in items), default=0)
    body_w = lab_w + ncols * item_w + (ncols - 1) * _COL_GAP if item_w else 0
    lines  = sum(max(1, math.ceil(len(items) / ncols)) for _, _, items in secs)
    width  = max(len(info['label_header']), body_w, len(info.get('more') or ''))
    return width, lines, lab_w, item_w


_US_NAMES = {'united states', 'united states of america', 'usa'}


def _region_anchor(geom, pts):
    """
    (lat, lon) of a region's dot: centroid of the region's largest part within
    ~5° of its contacts, kept inside the region.  For a typical state that is
    the whole state; for Russia or Australia it is the part actually worked.
    """
    from shapely.geometry import MultiPoint
    try:
        near    = MultiPoint([(lon, lat) for lat, lon in pts]).convex_hull.buffer(5.0)
        clipped = geom.intersection(near)
        if not clipped.is_empty and clipped.area > 0:
            geom = clipped
    except Exception:
        pass            # invalid geometry — fall back to the whole region
    main = max(getattr(geom, 'geoms', [geom]), key=lambda g: g.area)
    c    = main.centroid
    if not main.contains(c):
        c = main.representative_point()      # e.g. crescent-shaped regions
    return c.y, c.x


# Map units, fine → coarse.  Boxes (--boxes), fills (--fill) and colours all
# pick one of these; colour always follows the coarser of the box and fill units.
UNITS      = ('grid', 'grid4', 'region', 'country')
_UNIT_RANK = {u: i for i, u in enumerate(UNITS)}
_AREA_UNITS = ('region', 'country')


class _StateLookup:
    """
    US state / Canadian province for a QSO: the ADIF STATE field when valid,
    else a point-in-polygon test against Natural Earth admin-1 shapes (Canadian
    logs rarely carry STATE), else the nearest state/province within ~2° (grid
    centres often land in a lake or offshore).
    """

    def __init__(self):
        import cartopy.io.shapereader as shpreader
        from shapely.prepared import prep
        shp = shpreader.natural_earth(
            resolution='10m', category='cultural', name='admin_1_states_provinces')
        self.admin1 = [(r.attributes['adm0_a3'], (r.attributes.get('postal') or '').upper(),
                        r.attributes.get('name') or '', r.geometry)
                       for r in shpreader.Reader(shp).records()
                       if r.attributes.get('adm0_a3') in ('USA', 'CAN')]
        self.us_postal = {p: name for adm, p, name, _ in self.admin1 if adm == 'USA'}
        self.geom_of   = {f'{adm}-{p}': g for adm, p, name, g in self.admin1}
        self.prepared  = [(adm, p, name, g.bounds, prep(g)) for adm, p, name, g in self.admin1]

    def _pip(self, lat, lon, adm_want):
        from shapely.geometry import Point
        pt = Point(lon, lat)
        for adm, p, name, (x0, y0, x1, y1), pg in self.prepared:
            # Same-country only: border states' polygons include lake halves
            if (adm == adm_want and x0 <= lon <= x1 and y0 <= lat <= y1
                    and pg.contains(pt)):
                return p, name
        d, p, name = min((g.distance(pt), p, name)
                         for adm, p, name, g in self.admin1 if adm == adm_want)
        return (p, name) if d < 2.0 else None

    def state_of(self, qso, lat, lon):
        """(key, name, geometry) for US/CA QSOs, else None."""
        cl = qso.get('COUNTRY', '').strip().lower()
        if cl in _US_NAMES:
            adm = 'USA'
        elif cl == 'canada':
            adm = 'CAN'
        else:
            return None
        st = qso.get('STATE', '').strip().upper()
        if adm == 'USA' and st in self.us_postal:
            hit = (st, self.us_postal[st])
        elif qso.get('_LOC_SRC') in ('latlon', 'grid'):
            hit = self._pip(lat, lon, adm)
        else:
            return None     # position is only the country centroid — no state
        if not hit:
            return None
        key = f'{adm}-{hit[0]}'
        return key, hit[1], self.geom_of[key]


class UnitIndex:
    """
    Each located QSO's key in every map unit, plus unit labels and shapes.

      grid     the square as logged (usually 6 characters)
      grid4    the 4-character square
      region   US state / Canadian province, otherwise country
      country  the ADIF COUNTRY entity (Alaska, Hawaii, ... are their own)

    QSOs without a grid fall back to their country (or rounded position) in the
    grid units.  Region/country shapes come from Natural Earth and are loaded
    only when *shapes* is true; grid shapes are computed from the locator.
    """

    def __init__(self, qsos_with_pos, shapes, log):
        from shapely.geometry import box as _sbox
        self.qsos  = qsos_with_pos
        self.keys  = []
        self.label = {u: {} for u in UNITS}
        self.geom  = {u: {} for u in UNITS}
        states     = _StateLookup() if shapes else None
        countries  = set()

        for qso, (lat, lon) in qsos_with_pos:
            grid    = qso.get('GRIDSQUARE', '').upper().strip()
            country = qso.get('COUNTRY', '').strip()
            if country:
                fb_key, fb_label = f'C:{country}', country
            else:
                fb_key   = f'_POS_{lat:.0f}_{lon:.0f}'
                fb_label = (f"{abs(lat):.0f}°{'N' if lat >= 0 else 'S'} "
                            f"{abs(lon):.0f}°{'E' if lon >= 0 else 'W'}")
            countries.add(country)
            k = {}
            for unit, g in (('grid', grid), ('grid4', grid[:4])):
                if g:
                    k[unit] = g
                    self.label[unit][g] = g
                    if g not in self.geom[unit]:
                        b = maidenhead_bounds(g)
                        self.geom[unit][g] = _sbox(*b) if b else None
                else:
                    k[unit] = fb_key
                    self.label[unit][fb_key] = fb_label[:14]
                    self.geom[unit].setdefault(fb_key, None)
            k['country'] = fb_key
            self.label['country'][fb_key] = fb_label[:18]
            st = states.state_of(qso, lat, lon) if states else None
            if st:
                k['region'] = st[0]
                self.label['region'][st[0]] = st[1][:18]
                self.geom['region'][st[0]] = st[2]
            else:
                k['region'] = fb_key
                self.label['region'][fb_key] = fb_label[:18]
            self.keys.append(k)

        if shapes:
            cg = load_country_geometries(sorted(c for c in countries if c), log)
            for c, g in cg.items():
                self.geom['country'][f'C:{c}'] = g
            for key in self.label['region']:
                if key not in self.geom['region']:
                    # US/Canada are split into states: a QSO with no known
                    # state must not tint the whole country
                    split = key[2:].lower() in _US_NAMES or key == 'C:Canada'
                    self.geom['region'][key] = (None if split
                                                else self.geom['country'].get(key))

    def majority(self, idx, unit):
        """Most common *unit* key among the QSOs at positions *idx*."""
        return Counter(self.keys[i][unit] for i in idx).most_common(1)[0][0]


def build_groups(index, unit):
    """
    One group (info box / dot / line) per *unit* key.  Grid groups sit at their
    first QSO's position; region/country groups at the centroid of the area
    actually worked (see _region_anchor).
    """
    groups = {}
    for i, (qso, pos) in enumerate(index.qsos):
        k = index.keys[i][unit]
        g = groups.setdefault(k, {'qsos': [], 'pts': [], 'idx': [],
                                  'label_header': index.label[unit][k]})
        g['qsos'].append(qso)
        g['pts'].append(pos)
        g['idx'].append(i)
    for k, g in groups.items():
        geom = index.geom[unit].get(k)
        if unit in _AREA_UNITS and geom is not None:
            g['geom'] = geom
            g['dot_lat'], g['dot_lon'] = _region_anchor(geom, g['pts'])
        elif unit in _AREA_UNITS:
            g['dot_lat'] = sum(p[0] for p in g['pts']) / len(g['pts'])
            g['dot_lon'] = sum(p[1] for p in g['pts']) / len(g['pts'])
        else:
            g['dot_lat'], g['dot_lon'] = g['pts'][0]
    return groups


# Region palette: based on the dataviz reference dark categorical set (all in
# OKLCH L 0.48-0.67 and >= 3:1 on both the navy ocean and the sand land), with
# its green lifted from #008300 to clear 3:1 on sand, and its red swapped for a
# neutral grey — red and magenta read alike as thin box borders; the grey cut
# weakly separated neighbour links ~25% on a 1.6k-QSO log.  No 8-colour set
# keeps *every* pair distinct (e.g. magenta/aqua under deuteranopia), so the
# colouring below is palette-aware and keeps weak pairs apart where it can.
REGION_PALETTE = ['#3987e5', '#d95926', '#199e70', '#c98500',
                  '#d55181', '#2f9a2f', '#9085e9', '#89939e']
_REGION_NEAR_DEG  = 1.5     # regions this close (shapes) count as neighbours
_POINT_NEAR_DEG   = 2.5     # ... for keys without a shape (no grid / unmatched)
_BOX_NEAR_CHARS   = 5.0     # boxes / dots this close (in char heights) too
_REGION_GOOD_DE   = 15.0    # neighbour colour separation (ΔE) that is 'enough'


def _palette_distances(palette):
    """
    Pairwise perceptual distance matrix (OKLab ΔE × 100) — the minimum over
    normal vision and simulated deuteranopia / protanopia (Machado 2009).
    """
    def lin(c):
        c = int(c, 16) / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def oklab(rgb):
        r, g, b = rgb
        l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
        m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
        s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
        return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
                1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
                0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)

    sims = {
        'normal': None,
        'deutan': ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413),
                   (-0.011820, 0.042940, 0.968881)),
        'protan': ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216),
                   (-0.003882, -0.048116, 1.051998)),
    }
    rgbs = [tuple(lin(h.lstrip('#')[i:i + 2]) for i in (0, 2, 4)) for h in palette]
    labs = {}
    for name, m in sims.items():
        labs[name] = [oklab(rgb if m is None else tuple(
            min(1.0, max(0.0, sum(m[r][c] * rgb[c] for c in range(3))))
            for r in range(3))) for rgb in rgbs]
    n = len(palette)
    return [[min(100.0 * math.dist(labs[s][i], labs[s][j]) for s in sims)
             for j in range(n)] for i in range(n)]


def assign_colors(index, cu, groups, boxes_px, ll_to_px, char_px, log):
    """
    Colour every *cu*-unit key (state, country or grid square) so neighbours
    look clearly different; returns {key: colour}.  Each group gets info['ck'],
    the cu key most of its QSOs fall in (a grid box takes its state's colour).

    Neighbours are keys whose shapes lie close (regions within
    _REGION_NEAR_DEG; grid squares that touch, corners included), plus keys
    whose boxes or dots end up close on the page after placement — ocean-
    placed boxes can sit together although their regions don't touch.
    Colouring is DSatur-style (most-constrained key first); each key takes the
    palette colour most distinct from its coloured neighbours (anything
    ≥ _REGION_GOOD_DE counts as distinct enough), ties going to the least-used
    colour to keep the map balanced.
    """
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    for info in groups.values():
        info['ck'] = index.majority(info['idx'], cu)

    keys = sorted({k[cu] for k in index.keys})
    idx  = {k: i for i, k in enumerate(keys)}
    adj  = {k: set() for k in keys}

    def _link(a, b):
        if a != b:
            adj[a].add(b)
            adj[b].add(a)

    # 1. geographic neighbours
    grid_unit = cu not in _AREA_UNITS
    shaped    = [k for k in keys if index.geom[cu].get(k) is not None]
    if shaped:
        shapes = [index.geom[cu][k] if grid_unit else index.geom[cu][k].simplify(0.05)
                  for k in shaped]
        tree = STRtree(shapes)
        near = 0.01 if grid_unit else _REGION_NEAR_DEG
        for i, j in zip(*tree.query(shapes, predicate='dwithin', distance=near)):
            _link(shaped[i], shaped[j])
    # keys without a shape (no grid / unmatched country): mean QSO position
    pos = defaultdict(list)
    for i, k in enumerate(index.keys):
        pos[k[cu]].append(index.qsos[i][1])
    loose = [k for k in keys if index.geom[cu].get(k) is None]
    if loose:
        pts  = [Point(sum(p[1] for p in pos[k]) / len(pos[k]),
                      sum(p[0] for p in pos[k]) / len(pos[k])) for k in loose]
        everything = [index.geom[cu][k] for k in shaped] + pts
        tree = STRtree(everything)
        allk = shaped + loose
        for i, j in zip(*tree.query(pts, predicate='dwithin', distance=_POINT_NEAR_DEG)):
            _link(loose[i], allk[j])

    # 2. neighbours on the page: boxes near boxes, dots near other boxes
    near_px = _BOX_NEAR_CHARS * char_px
    dots = {g: ll_to_px(info['dot_lon'], info['dot_lat']) for g, info in groups.items()}
    bk   = [g for g in groups if g in boxes_px]
    for i, a in enumerate(bk):
        ax_, ay_, ahw, ahh = boxes_px[a]
        for b in bk[i + 1:]:
            bx_, by_, bhw, bhh = boxes_px[b]
            if (abs(ax_ - bx_) < ahw + bhw + near_px and
                    abs(ay_ - by_) < ahh + bhh + near_px):
                _link(groups[a]['ck'], groups[b]['ck'])
        for g, (dx, dy) in dots.items():
            if abs(dx - ax_) < ahw + near_px and abs(dy - ay_) < ahh + near_px:
                _link(groups[a]['ck'], groups[g]['ck'])

    # 3. palette-aware DSatur colouring
    dist    = _palette_distances(REGION_PALETTE)
    ncol    = len(REGION_PALETTE)
    color   = {}
    used    = [0] * ncol
    pending = set(keys)
    while pending:
        k = max(pending, key=lambda v: (len({color[n] for n in adj[v] if n in color}),
                                        len(adj[v]), -idx[v]))
        nbr = [color[n] for n in adj[k] if n in color]
        # Separation beyond _REGION_GOOD_DE counts as equally good, so the
        # least-used colour wins among those instead of the same few
        # "far from everything" hues winning every time.
        c = max(range(ncol), key=lambda c: (
            min(_REGION_GOOD_DE, min((dist[c][o] for o in nbr), default=1e9)),
            -used[c], -c))
        color[k] = c
        used[c] += 1
        pending.discard(k)

    clashes = sum(1 for k in keys for n in adj[k] if color[n] == color[k]) // 2
    weak = sum(1 for k in keys for n in adj[k]
               if dist[color[k]][color[n]] < _REGION_GOOD_DE) // 2
    log.verbose("Colours by %s: %d keys, %d neighbour links (max %d per key); "
                "same colour: %d, weakly separated (ΔE < %.0f): %d",
                cu, len(keys), sum(len(v) for v in adj.values()) // 2,
                max((len(v) for v in adj.values()), default=0), clashes,
                _REGION_GOOD_DE, weak)
    log.verbose("  colour use: %s", ' '.join(f"{REGION_PALETTE[c]}:{used[c]}" for c in range(ncol)))
    return {k: REGION_PALETTE[color[k]] for k in keys}


def _summary_text(qsos_with_pos, args, index=None):
    """Multi-line text for the statistics box (station, counts, dates, filters)."""
    total       = len(qsos_with_pos)
    unique_cs   = len({qso.get('CALL', '')     for qso, _ in qsos_with_pos})
    glen        = None if args.boxes == 'grid' else 4
    unique_grid = len({qso.get('GRIDSQUARE', '').upper().strip()[:glen]
                       for qso, _ in qsos_with_pos
                       if qso.get('GRIDSQUARE', '').strip()})
    countries   = len({qso.get('COUNTRY', '')
                       for qso, _ in qsos_with_pos if qso.get('COUNTRY')})

    dates = sorted(qso.get('QSO_DATE', '') for qso, _ in qsos_with_pos
                   if qso.get('QSO_DATE'))
    station = next((qso.get(f) for qso, _ in qsos_with_pos
                    for f in ('STATION_CALLSIGN', 'OPERATOR') if qso.get(f)), '')

    # (label, value) rows; None is a separator rule
    rows = [('Contacts:', f"{total:,}"),
            ('Grids:', f"{unique_grid:,}"),
            ('Callsigns:', f"{unique_cs:,}"),
            ('Countries:', f"{countries:,}")]
    # Worked US states / Canadian provinces (WAS / RAC): from the region lookup
    # when shapes were loaded, else from the ADIF STATE field
    if index is not None and index.geom['region']:
        regions = {k['region'] for k in index.keys}
        n_us = sum(r.startswith('USA-') for r in regions)
        n_ca = sum(r.startswith('CAN-') for r in regions)
    else:
        def _st(names):
            return len({q.get('STATE', '').strip().upper() for q, _ in qsos_with_pos
                        if q.get('COUNTRY', '').strip().lower() in names
                        and q.get('STATE', '').strip()})
        n_us, n_ca = _st(_US_NAMES), _st({'canada'})
    if n_us:
        rows.append(('US states:', f"{n_us}"))
    if n_ca:
        rows.append(('CA provinces:', f"{n_ca}"))
    if dates:
        rows += [None, ('First QSO:', _fmt_date(dates[0])),
                 ('Last QSO:', _fmt_date(dates[-1]))]

    filter_rows = []
    if getattr(args, 'start', None):
        filter_rows.append(('Start:', _fmt_date(args.start)))
    if getattr(args, 'end', None):
        filter_rows.append(('End:', _fmt_date(args.end)))
    if getattr(args, 'tail_days', None) is not None:
        filter_rows.append(('Tail days:', f"{args.tail_days:,}"))
    if getattr(args, 'tail', None) is not None:
        filter_rows.append(('Tail recs:', f"{args.tail:,}"))
    if filter_rows:
        rows += [None] + filter_rows

    # Fixed-width columns so the values right-align
    w  = max(len(r[0]) for r in rows if r) + 2
    vw = max(len(r[1]) for r in rows if r)
    all_lines = [station.upper().center(w + vw)] if station else []
    all_lines += ['─' * (w + vw) if r is None else f"{r[0]:<{w}}{r[1]:>{vw}}"
                  for r in rows]

    return '\n'.join(all_lines)


_GRIDLINE_CLR = '#4a6a8a'   # Maidenhead overlay lines / field labels


def draw_grid_lines(ax, proj, extent, level, geo_scale):
    """
    Maidenhead overlay within *extent*: 'fields' draws the 20° × 10° field
    lines with their two-letter labels; 'squares' adds faint 2° × 1° lines.
    """
    lon0, lon1, lat0, lat1 = extent

    def _lines(dlon, dlat, lw, alpha):
        x = math.ceil(lon0 / dlon) * dlon
        while x <= lon1:
            ax.plot([x, x], [lat0, lat1], transform=proj, color=_GRIDLINE_CLR,
                    linewidth=lw, alpha=alpha, zorder=2.6)
            x += dlon
        y = math.ceil(lat0 / dlat) * dlat
        while y <= lat1:
            ax.plot([lon0, lon1], [y, y], transform=proj, color=_GRIDLINE_CLR,
                    linewidth=lw, alpha=alpha, zorder=2.6)
            y += dlat

    if level == 'squares':
        _lines(2.0, 1.0, 0.15, 0.35)
    _lines(20.0, 10.0, 0.5, 0.7)
    for fi in range(18):
        for fj in range(18):
            clon = -180.0 + fi * 20.0 + 10.0
            clat = -90.0 + fj * 10.0 + 5.0
            if lon0 < clon < lon1 and lat0 < clat < lat1:
                ax.text(clon, clat, chr(65 + fi) + chr(65 + fj), transform=proj,
                        fontsize=18 * geo_scale, color=_GRIDLINE_CLR, alpha=0.55,
                        fontweight='bold', ha='center', va='center',
                        fontfamily='monospace', zorder=2.7)


def generate_map(qsos_with_pos, home_pos, args, log):
    """Build and return a matplotlib Figure containing the contact map."""
    proj     = ccrs.PlateCarree()

    pts = [pos for _, pos in qsos_with_pos] + ([home_pos] if home_pos else [])
    extent = compute_extent(pts, args)
    fig_h  = figure_height_for(extent, args.width)
    log.verbose("Extent (%s): lon %.1f..%.1f, lat %.1f..%.1f → %.1f×%.1f in",
                args.extent, *extent, args.width, fig_h)

    fig = plt.figure(figsize=(args.width, fig_h), facecolor=_MAP_BG)
    ax  = fig.add_axes(_AX_RECT, projection=proj, facecolor=_MAP_BG)
    ax.set_extent(extent, crs=proj)
    ax.hamap_extent = extent

    # ---- Natural Earth background ----------------------------------------
    log.verbose("Loading map features...")
    ax.add_feature(cfeature.NaturalEarthFeature(
        'physical', 'ocean', '10m', facecolor=_MAP_BG, edgecolor='none'))
    ax.add_feature(cfeature.NaturalEarthFeature(
        'physical', 'land', '10m', facecolor=_LAND_COLOR, edgecolor='none'))
    ax.add_feature(cfeature.NaturalEarthFeature(
        'physical', 'lakes', '10m', facecolor=_MAP_BG,
        edgecolor=_COAST_CLR, linewidth=0.20))
    ax.add_feature(cfeature.NaturalEarthFeature(
        'physical', 'coastline', '10m', facecolor='none',
        edgecolor=_COAST_CLR, linewidth=0.35))
    ax.add_feature(cfeature.NaturalEarthFeature(
        'cultural', 'admin_0_countries', '10m', facecolor='none',
        edgecolor=_BORDER_CLR, linewidth=0.20))

    if args.borders == 'states':
        # US states and Canadian provinces (the regions --boxes/--fill region use)
        import cartopy.io.shapereader as shpreader
        shp1 = shpreader.natural_earth(
            resolution='10m', category='cultural', name='admin_1_states_provinces')
        ax.add_geometries(
            [r.geometry for r in shpreader.Reader(shp1).records()
             if r.attributes.get('adm0_a3') in ('USA', 'CAN')],
            crs=proj, facecolor='none', edgecolor=_STATE_CLR, linewidth=0.3)

    # ---- Graticule -------------------------------------------------------
    gl = ax.gridlines(linewidth=0.15, color=_GRID_CLR, alpha=0.8,
                      linestyle='-', draw_labels=False)
    gl.xlocator = mticker.MultipleLocator(30)
    gl.ylocator = mticker.MultipleLocator(30)

    # Geographic label fonts scale with canvas width so they stay proportionally
    # readable at any size, independent of the box --font-size setting.
    _geo_scale = args.width / 48.0   # 1.0 at 48 in
    lon0, lon1, lat0, lat1 = extent

    # ---- Maidenhead overlay (--grid-lines) -------------------------------
    if args.grid_lines != 'none':
        draw_grid_lines(ax, proj, extent, args.grid_lines, _geo_scale)

    # ---- Group contacts into boxes; index every QSO by map unit ----------
    show_boxes  = args.boxes != 'none'
    group_unit  = args.boxes if show_boxes else 'grid'     # dots / lines per grid
    fill_unit   = None if args.fill == 'none' else args.fill
    color_unit  = max([group_unit] + ([fill_unit] if fill_unit else []),
                      key=lambda u: _UNIT_RANK[u])
    need_shapes = any(u in _AREA_UNITS for u in (group_unit, fill_unit, color_unit))
    log.verbose("Grouping contacts (boxes: %s, fill: %s, colour by: %s)...",
                args.boxes, args.fill, color_unit)
    index  = UnitIndex(qsos_with_pos, need_shapes, log)
    groups = build_groups(index, group_unit)
    log.verbose("  %d groups from %d QSOs", len(groups), len(qsos_with_pos))

    # Box contents as band rows, wrapped into columns so boxes stay compact
    for info in groups.values():
        info['sections'] = box_sections(info, args.box_calls)
        info['ncols']    = box_columns(info['sections'], group_unit in _AREA_UNITS,
                                       args.box_calls == 0)

    # ll_to_px / px_to_ll needed for label obstacles and box geometry
    ll_to_px, px_to_ll, ax_x0, ax_y0, ax_w_px, ax_h_px = _ax_pixel_fns(ax, args)

    # ---- Fixed text labels (countries / states) — drawn first, act as obstacles
    pre_placed = []

    def _text_obs(name, lat, lon, fpt):
        """Pixel-space obstacle rect for a single-line text label."""
        ch  = fpt * args.dpi / 72.0
        cw  = ch * 0.62
        pad = ch * 0.5
        cx, cy = ll_to_px(lon, lat)
        hw = len(name) * cw / 2 + pad
        hh = ch * 1.35 / 2 + pad * 0.5
        return [cx, cy, hw, hh, f'_LBL_{name}']

    cfont = max(2.0, 5.1 * _geo_scale)
    sfont = max(1.8, 4.5 * _geo_scale)

    def _place_geo_label(name, lat, lon, fpt, color, alpha):
        """Draw a geographic label unless it is off-map or overlaps one already drawn."""
        if not (lon0 < lon < lon1 and lat0 < lat < lat1):
            return False
        obs = _text_obs(name, lat, lon, fpt)
        for o in pre_placed:
            if abs(obs[0] - o[0]) < obs[2] + o[2] and abs(obs[1] - o[1]) < obs[3] + o[3]:
                return False
        pre_placed.append(obs)
        ax.text(lon, lat, name, transform=proj, fontsize=fpt, color=color,
                va='center', ha='center', fontfamily='monospace',
                alpha=alpha, zorder=5)
        return True

    # States first (more specific), then countries largest-first, so crowded
    # regions keep the most useful names and drop micro-states.  Region boxes
    # already name their state, so state labels are skipped then.
    if args.names in ('states', 'all') and group_unit != 'region':
        log.verbose("Drawing state/province labels...")
        for state, (slat, slon) in STATE_CENTROIDS.items():
            _place_geo_label(state, slat, slon, sfont, '#8899aa', 0.55)

    if args.names in ('countries', 'all'):
        log.verbose("Drawing country labels...")
        areas = _country_areas(log)
        order = sorted(COUNTRY_CENTROIDS.items(),
                       key=lambda kv: -areas.get(kv[0].lower(), 0.0))
        # Micro-states (San Marino, Monaco, ...) are too small to label usefully
        dropped = sum(areas.get(c.lower(), 1.0) < 0.1 or
                      not _place_geo_label(c, clat, clon, cfont, '#7a9aaa', 0.65)
                      for c, (clat, clon) in order)
        log.verbose("  %d country labels skipped (off-map or overlapping)", dropped)

    # ---- Land mask for ocean-preferring placement (--ocean-boxes) --------
    _land_prep = None
    if args.ocean_boxes and show_boxes:
        try:
            import cartopy.io.shapereader as shpreader
            from shapely.ops import unary_union
            from shapely.prepared import prep as _sprep
            log.verbose("Preparing land mask for ocean-preferring box placement...")
            _land_shp   = shpreader.natural_earth(
                resolution='110m', category='physical', name='land')
            _land_union = unary_union(list(shpreader.Reader(_land_shp).geometries()))
            _land_prep  = _sprep(_land_union)
        except Exception as exc:
            log.debug("Land geometry unavailable, ocean preference disabled: %s", exc)

    # ---- Corner overlays (stats box, band legend) as placement obstacles --
    summary_text  = _summary_text(qsos_with_pos, args, index)
    _pt = args.dpi / 72.0
    s_lines = summary_text.split('\n')
    s_fpt   = 14 * _geo_scale
    s_w = (max(len(l) for l in s_lines) * 0.62 + 1.4) * s_fpt * _pt
    s_h = (len(s_lines) * 1.2 + 1.4) * s_fpt * _pt
    sizes = {'stats': (s_w, s_h)}
    # Band key: QSOs per band, swatches in the callsign text colours
    band_qsos   = Counter(_qso_band(q) for q, _ in qsos_with_pos)
    band_labels = {b: f"{b:<5}{band_qsos[b]:>7,}" for b in band_qsos}
    present_bands = sorted(band_qsos, key=_band_sort_key)
    if present_bands:
        l_fpt  = 16 * _geo_scale
        l_cols = max(1, len(present_bands) // 8)
        l_rows = math.ceil(len(present_bands) / l_cols) + 1          # + title
        l_w = l_cols * (4.0 + max(len(v) for v in band_labels.values()) * 0.62) * l_fpt * _pt
        l_h = (l_rows * 1.3 + 1.5) * l_fpt * _pt
        sizes['legend'] = (l_w, l_h)

    # Put each overlay in the corner covering the fewest dots (ties → preferred)
    corner_m = 0.6 * s_fpt * _pt      # gap between overlay and map edge (px)
    corners  = ('lower left', 'lower right', 'upper left', 'upper right')

    def _corner_rect(corner, w, h):
        cx = (ax_x0 + corner_m + w / 2 if 'left' in corner
              else ax_x0 + ax_w_px - corner_m - w / 2)
        cy = (ax_y0 + corner_m + h / 2 if 'lower' in corner
              else ax_y0 + ax_h_px - corner_m - h / 2)
        return cx, cy, w / 2, h / 2

    dots_px = [ll_to_px(i['dot_lon'], i['dot_lat']) for i in groups.values()]
    if home_pos:
        dots_px.append(ll_to_px(home_pos[1], home_pos[0]))

    def _crowd(rect):
        cx, cy, hw, hh = rect
        return sum(abs(x - cx) < hw * 1.25 and abs(y - cy) < hh * 1.25
                   for x, y in dots_px)

    overlay_corner = {}
    for name, pref in (('stats', 'lower left'), ('legend', 'lower right')):
        if name not in sizes:
            continue
        free = [c for c in corners if c not in overlay_corner.values()]
        best = min(free, key=lambda c: (_crowd(_corner_rect(c, *sizes[name])),
                                        c != pref))
        overlay_corner[name] = best
        pre_placed.append([*_corner_rect(best, *sizes[name]), f'_{name.upper()}'])

    if home_pos:
        hx, hy = ll_to_px(home_pos[1], home_pos[0])
        star_r = 14 * args.dpi / 72.0 / 2      # markersize 14 pt
        pre_placed.append([hx, hy, star_r, star_r, '_HOME'])

    # ---- Compute label positions -----------------------------------------
    if show_boxes:
        log.verbose("Computing label positions...")
        label_pos, boxes_px = place_labels(groups, args, log, ax,
                                           pre_placed=pre_placed,
                                           land_prep=_land_prep)
    else:
        label_pos, boxes_px = {}, {}

    # ---- Colours: by the coarser of the box and fill units ---------------
    colors = assign_colors(index, color_unit, groups, boxes_px, ll_to_px,
                           args.font_size * args.dpi / 72.0, log)
    for info in groups.values():
        info['color'] = colors[info['ck']]

    # ---- Fill worked regions / grid squares (--fill) ---------------------
    if fill_unit:
        from matplotlib.colors import to_rgba
        members = defaultdict(list)
        for i, k in enumerate(index.keys):
            members[k[fill_unit]].append(i)
        grid_fill = fill_unit not in _AREA_UNITS
        n_filled = 0
        for fk, idx in members.items():
            geom = index.geom[fill_unit].get(fk)
            if geom is None:
                continue
            c = colors[index.majority(idx, color_unit)]
            ax.add_geometries(
                [geom], crs=proj,
                facecolor=to_rgba(c, 0.30 if grid_fill else 0.16),
                edgecolor=to_rgba(c, 0.60 if grid_fill else 0.45),
                linewidth=0.3 if grid_fill else 0.4, zorder=2.5)
            n_filled += 1
        log.verbose("Filled %d %s shapes", n_filled, fill_unit)

    # ---- Great-circle lines (one arc per unique dot) ---------------------
    if home_pos and not args.no_lines:
        home_lat, home_lon = home_pos
        # Fade and thin the lines as their number grows so a big log reads as
        # a fan of paths, not a solid wash of colour.
        line_dots = [(i['dot_lat'], i['dot_lon'], i['color']) for i in groups.values()]
        n_lines = len(line_dots)
        density = min(1.0, math.sqrt(40.0 / max(n_lines, 1)))
        l_alpha = args.line_alpha if args.line_alpha is not None else max(0.12, 0.45 * density)
        l_width = args.line_width if args.line_width is not None else max(0.4, 0.8 * density)
        log.verbose("Drawing %d great-circle lines (alpha %.2f, width %.2f)...",
                    n_lines, l_alpha, l_width)
        # Most common colour first, so rarer ones draw on top of it
        color_freq = Counter(d[2] for d in line_dots)
        for d_lat, d_lon, d_color in sorted(line_dots, key=lambda d: -color_freq[d[2]]):
            for seg_lons, seg_lats in _great_circle_segments(
                    home_lat, home_lon, d_lat, d_lon):
                ax.plot(
                    seg_lons, seg_lats, transform=proj,
                    color=d_color, alpha=l_alpha, linewidth=l_width,
                    solid_capstyle='round', zorder=3,
                )

    # ---- Leader lines (dot → label box) ----------------------------------
    if show_boxes:
        for key, info in groups.items():
            if key not in label_pos:
                continue
            lbl_lat, lbl_lon = label_pos[key]
            ax.plot(
                [info['dot_lon'], lbl_lon],
                [info['dot_lat'], lbl_lat],
                transform=proj,
                color=info['color'], alpha=0.70, linewidth=0.5,
                solid_capstyle='round', zorder=6,
            )

    # ---- Contact dot markers ---------------------------------------------
    dots = [(i['dot_lat'], i['dot_lon'], i['color']) for i in groups.values()]
    log.verbose("Plotting %d contact dots...", len(dots))
    if dots:
        ax.scatter(
            [d[1] for d in dots], [d[0] for d in dots],
            transform=proj,
            s=_DOT_SIZE, color=[d[2] for d in dots], alpha=0.95,
            linewidths=0.6, edgecolors=_MAP_BG,
            zorder=7,
        )

    # ---- Label boxes (drawn on top of leader line ends) ------------------
    if show_boxes:
        log.verbose("Drawing %d label boxes...", len(label_pos))
        char_h  = args.font_size * args.dpi / 72.0
        leading = char_h * 1.2
        pad_px  = char_h * _BOX_PAD

        for key, info in groups.items():
            if key not in label_pos or key not in boxes_px:
                continue
            lbl_lat, lbl_lon = label_pos[key]
            cx, cy, hw, hh   = boxes_px[key]
            color = info['color']

            # Background + border rectangle
            top_lat,  left_lon  = px_to_ll(cx - hw, cy + hh)
            bot_lat,  right_lon = px_to_ll(cx + hw, cy - hh)
            rect = mpatches.FancyBboxPatch(
                (left_lon, bot_lat), right_lon - left_lon, top_lat - bot_lat,
                boxstyle='round,pad=0',
                transform=proj,
                facecolor=_MAP_BG, alpha=0.95,
                edgecolor=color, linewidth=0.8,
                zorder=8,
            )
            ax.add_patch(rect)

            # Header text
            hdr_y_px   = cy + hh - pad_px - char_h * 0.5
            hdr_lat, _ = px_to_ll(cx, hdr_y_px)
            ax.text(lbl_lon, hdr_lat, info['label_header'],
                    transform=proj,
                    fontsize=args.font_size, color=_lighten(color, 0.35),
                    fontweight='bold',
                    va='center', ha='center',
                    fontfamily='monospace', zorder=9)

            # Separator line — tight below header baseline
            sep_y_px   = cy + hh - pad_px - char_h - pad_px * 0.25
            sep_lat, _ = px_to_ll(cx, sep_y_px)
            _, sep_l   = px_to_ll(cx - hw, sep_y_px)
            _, sep_r   = px_to_ll(cx + hw, sep_y_px)
            ax.plot([sep_l, sep_r], [sep_lat, sep_lat],
                    transform=proj, color=color,
                    linewidth=0.5, alpha=0.85,
                    solid_capstyle='butt', zorder=9)

            # Band rows: label column, then callsigns in columns, in band colour
            _, _, lab_w, item_w = box_metrics(info)
            char_w = char_h * 0.62
            ncols  = info['ncols']
            x_lab  = cx - hw + pad_px
            x_body = x_lab + lab_w * char_w
            top    = sep_y_px - pad_px * 0.25
            for label, band, items in info['sections']:
                nl  = max(1, math.ceil(len(items) / ncols))
                clr = (band_text_color(band) if band and args.band_colors == 'on'
                       else _CALL_CLR)
                if label:
                    lab_lat, lab_lon = px_to_ll(x_lab, top - leading / 2)
                    ax.text(lab_lon, lab_lat, label, transform=proj,
                            fontsize=args.font_size, color=clr, fontweight='bold',
                            va='center', ha='left', fontfamily='monospace', zorder=9)
                col_lat, _ = px_to_ll(cx, top - nl * leading / 2)
                x0 = x_body if label else x_lab           # unlabelled rows: no indent
                for ci in range(ncols):
                    col = items[ci * nl:(ci + 1) * nl]
                    if not col:
                        continue
                    col = col + [' '] * (nl - len(col))   # keep columns top-aligned
                    _, col_lon = px_to_ll(x0 + ci * (item_w + _COL_GAP) * char_w, 0)
                    ax.text(col_lon, col_lat, '\n'.join(col), transform=proj,
                            fontsize=args.font_size, color=clr,
                            va='center', ha='left', fontfamily='monospace', zorder=9)
                top -= nl * leading

            # "+k more" footer when the callsign list was capped
            if info.get('more'):
                more_lat, _ = px_to_ll(cx, cy - hh + pad_px + char_h * 0.5)
                ax.text(lbl_lon, more_lat, info['more'],
                        transform=proj,
                        fontsize=args.font_size, color=_lighten(color, 0.35),
                        fontstyle='italic',
                        va='center', ha='center',
                        fontfamily='monospace', zorder=9)

    # ---- Home station marker ---------------------------------------------
    if home_pos:
        home_lat, home_lon = home_pos
        ax.plot(home_lon, home_lat, transform=proj,
                marker='*', color='#FFFF00', markersize=14,
                markeredgecolor='#FF8800', markeredgewidth=0.8,
                zorder=10)

    # ---- Band legend -----------------------------------------------------
    legend_patches = [
        mpatches.Patch(color=(band_text_color(b) if args.band_colors == 'on'
                              else _CALL_CLR), label=band_labels[b])
        for b in present_bands
    ]
    if legend_patches:
        leg = ax.legend(
            handles=legend_patches,
            loc=overlay_corner['legend'],
            prop={'family': 'monospace', 'size': 16 * _geo_scale},
            title='QSOs by band', title_fontsize=16 * _geo_scale,
            borderaxespad=corner_m / (16 * _geo_scale * _pt),
            framealpha=0.90, facecolor=_MAP_BG,
            edgecolor='#3a5a7a', labelcolor='#c8d8e8',
            ncol=max(1, len(legend_patches) // 8), borderpad=0.5,
        )
        leg.get_title().set_color('#c8d8e8')
        leg.set_zorder(20)

    # ---- Summary statistics box (lower-left, axes-relative) -------------

    s_corner = overlay_corner['stats']
    s_inset  = corner_m + 0.55 * s_fpt * _pt      # edge gap + bbox pad
    ax.text(
        s_inset / ax_w_px if 'left' in s_corner else 1 - s_inset / ax_w_px,
        s_inset / ax_h_px if 'lower' in s_corner else 1 - s_inset / ax_h_px,
        summary_text,
        transform=ax.transAxes,
        fontsize=s_fpt, color='#c8d8e8',
        va='bottom' if 'lower' in s_corner else 'top',
        ha='left' if 'left' in s_corner else 'right',
        fontfamily='monospace',
        bbox=dict(
            facecolor=_MAP_BG, alpha=0.90,
            boxstyle='round,pad=0.55',
            edgecolor='#3a5a7a', linewidth=0.7,
        ),
        zorder=15,
    )

    fig.text(0.5, 0.005, 'generated by hamap', ha='center', va='bottom',
             color='#4a8a4a', fontsize=10 * _geo_scale, alpha=0.8)

    return fig, groups


# --------------------------------------------------------------------------- #
# HTML export  (Plotly interactive map)
# --------------------------------------------------------------------------- #

def _great_circle_path(lat1, lon1, lat2, lon2, n=50):
    """Return (lats, lons) lists interpolating a great-circle arc."""
    phi1, lam1 = math.radians(lat1), math.radians(lon1)
    phi2, lam2 = math.radians(lat2), math.radians(lon2)
    d = 2.0 * math.asin(math.sqrt(
        math.sin((phi2 - phi1) / 2.0) ** 2 +
        math.cos(phi1) * math.cos(phi2) * math.sin((lam2 - lam1) / 2.0) ** 2
    ))
    if d < 1e-8:
        return [lat1, lat2], [lon1, lon2]
    lats, lons = [], []
    for i in range(n + 1):
        f  = i / n
        A  = math.sin((1.0 - f) * d) / math.sin(d)
        B  = math.sin(f * d) / math.sin(d)
        x  = A * math.cos(phi1) * math.cos(lam1) + B * math.cos(phi2) * math.cos(lam2)
        y  = A * math.cos(phi1) * math.sin(lam1) + B * math.cos(phi2) * math.sin(lam2)
        z  = A * math.sin(phi1) + B * math.sin(phi2)
        lats.append(math.degrees(math.atan2(z, math.sqrt(x * x + y * y))))
        lons.append(math.degrees(math.atan2(y, x)))
    return lats, lons


def _great_circle_segments(lat1, lon1, lat2, lon2):
    """
    Densely sampled great-circle arc as [(lons, lats), ...] in PlateCarree,
    split at the antimeridian with both halves extended to the ±180° edge.
    """
    dist_deg = math.degrees(2.0 * math.asin(min(1.0, math.sqrt(
        math.sin(math.radians(lat2 - lat1) / 2.0) ** 2 +
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
        math.sin(math.radians(lon2 - lon1) / 2.0) ** 2))))
    lats, lons = _great_circle_path(lat1, lon1, lat2, lon2,
                                    n=max(8, int(dist_deg * 2)))
    segs = [([lons[0]], [lats[0]])]
    for i in range(1, len(lons)):
        a, b = lons[i - 1], lons[i]
        if abs(b - a) > 180.0:
            edge   = 180.0 if a > 0 else -180.0
            b_unw  = b + (360.0 if a > 0 else -360.0)
            f      = (edge - a) / (b_unw - a)
            e_lat  = lats[i - 1] + f * (lats[i] - lats[i - 1])
            segs[-1][0].append(edge)
            segs[-1][1].append(e_lat)
            segs.append(([-edge], [e_lat]))
        segs[-1][0].append(b)
        segs[-1][1].append(lats[i])
    return segs


def _geojson_features(geoms, simplify):
    """
    GeoJSON features {id: str(i)} for [(i, shapely_geom)], simplified and with
    clockwise exterior rings (what d3-geo, and so Plotly, expects for fills).
    Coordinates rounded to 3 decimals to keep the HTML small.
    """
    from shapely.geometry import mapping, Polygon, MultiPolygon
    from shapely.geometry.polygon import orient

    def _round(c):
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)):
                return [round(c[0], 3), round(c[1], 3)]
            return [_round(x) for x in c]
        return c

    feats = []
    for i, g in geoms:
        if simplify:
            g = g.simplify(simplify, preserve_topology=True)
        polys = [p for p in getattr(g, 'geoms', [g]) if isinstance(p, Polygon) and not p.is_empty]
        if not polys:
            continue
        g = MultiPolygon([orient(p, sign=-1.0) for p in polys])
        m = mapping(g)
        feats.append({'type': 'Feature', 'id': str(i), 'properties': {},
                      'geometry': {'type': m['type'], 'coordinates': _round(m['coordinates'])}})
    return feats


def generate_html_plotly(qsos_with_pos, args, home_pos, out_path, log):
    """
    Write a fully self-contained interactive HTML map using Plotly.

    Every map unit (grid, grid4, region, country) is pre-computed — its dots,
    fill shapes and neighbour-aware colours — and a collapsible panel in the
    page switches between them live: what the dots/popups cover, what is
    filled, which bands are shown, and the reference layers.  The command-line
    options only set the panel's starting state (also settable in the URL
    hash, e.g. map.html#fill=grid4&dots=region&bands=20m,40m&pin=Ohio).
    """
    import json
    try:
        import plotly.graph_objects as go
    except ImportError:
        log.error("plotly is required for --html.  Install: pip install plotly")
        return

    # ── Pre-compute every unit: groups, colours, coarser-unit majorities ──────
    log.verbose("HTML: indexing QSOs by grid, grid4, region and country...")
    index = UnitIndex(qsos_with_pos, True, log)
    units = {}
    for u in UNITS:
        groups = build_groups(index, u)
        keys   = list(groups)
        pos    = {k: i for i, k in enumerate(keys)}
        colors = assign_colors(index, u, groups, {}, lambda lon, lat: (lon, lat), 1.0, log)
        units[u] = {'keys': keys, 'pos': pos, 'groups': groups, 'colors': colors}
    pal_idx = {c: i for i, c in enumerate(REGION_PALETTE)}

    payload_units = {}
    for u in UNITS:
        U = units[u]
        gl = [U['groups'][k] for k in U['keys']]
        maj = {}
        for v in UNITS:
            if _UNIT_RANK[v] > _UNIT_RANK[u]:
                maj[v] = [units[v]['pos'][index.majority(g['idx'], v)] for g in gl]
        payload_units[u] = {
            'label': [g['label_header'] for g in gl],
            'lat':   [round(g['dot_lat'], 4) for g in gl],
            'lon':   [round(g['dot_lon'], 4) for g in gl],
            'q':     [g['idx'] for g in gl],
            'color': [pal_idx[U['colors'][k]] for k in U['keys']],
            'maj':   maj,
        }

    # [call, band, name, date, qth].  Deliberately no QSO time: the HTML is
    # meant to be shared, and exact contact times are too much to publish.
    def _qrow(q):
        return [q.get('CALL', ''), _qso_band(q), q.get('NAME', '').strip(),
                _fmt_date(q.get('QSO_DATE', '')),
                (q.get('QTH', '') or q.get('CITY', '')).strip()]
    qrows = [_qrow(q) for q, _ in qsos_with_pos]

    band_qsos = Counter(r[1] for r in qrows)
    bands = sorted(band_qsos, key=_band_sort_key)
    band_clr = {b: (band_text_color(b) if args.band_colors == 'on' else _CALL_CLR)
                for b in bands}

    # ── Traces.  JS fills in colours, visibility and lines; T maps roles → index
    traces, T = [], {'fill': {}, 'dots': {}, 'labels': {}, 'lines': []}

    def _add(tr):
        traces.append(tr)
        return len(traces) - 1

    # Fills: one choropleth per unit, z = palette index (+0.5) on a stepped scale
    n_pal = len(REGION_PALETTE)
    cscale = []
    for i, c in enumerate(REGION_PALETTE):
        cscale += [[i / n_pal, c], [(i + 1) / n_pal, c]]
    for u in UNITS:
        geoms = [(i, index.geom[u].get(k)) for i, k in enumerate(units[u]['keys'])
                 if index.geom[u].get(k) is not None]
        feats = _geojson_features(geoms, 0.02 if u in _AREA_UNITS else None)
        payload_units[u]['shape'] = sorted(int(f['id']) for f in feats)
        T['fill'][u] = _add(go.Choropleth(
            geojson={'type': 'FeatureCollection', 'features': feats},
            featureidkey='id', locations=[], z=[],
            zmin=0, zmax=n_pal, colorscale=cscale, showscale=False,
            marker=dict(opacity=0.45 if u not in _AREA_UNITS else 0.30,
                        line=dict(width=0.4, color='rgba(220,230,240,0.25)')),
            hoverinfo='skip', visible=False, name=f'fill-{u}'))

    # Initial view: the page zooms, so 'auto' shows the whole world rather
    # than cropping the globe; 'poles' still trims the polar regions.
    lon0, lon1, lat0, lat1 = ((-180.0, 180.0, *_POLES_LAT) if args.extent == 'poles'
                              else (-180.0, 180.0, -90.0, 90.0))

    # Reference layers

    def _grid_lines(dlon, dlat):
        lats, lons = [], []
        x = -180.0
        while x <= 180.0:
            ys = [y / 1.0 for y in range(-90, 91, 5)]
            lats += ys + [None]; lons += [x] * len(ys) + [None]
            x += dlon
        y = -90.0
        while y <= 90.0:
            xs = [float(v) for v in range(-180, 181, 2)]
            lats += [y] * len(xs) + [None]; lons += xs + [None]
            y += dlat
        return lats, lons

    la, lo = _grid_lines(2.0, 1.0)
    T['gsquares'] = _add(go.Scattergeo(lat=la, lon=lo, mode='lines', hoverinfo='skip',
                                       line=dict(width=0.5, color=_GRIDLINE_CLR),
                                       opacity=0.35, visible=False, name='squares'))
    la, lo = _grid_lines(20.0, 10.0)
    T['gfields'] = _add(go.Scattergeo(lat=la, lon=lo, mode='lines', hoverinfo='skip',
                                      line=dict(width=1.2, color=_GRIDLINE_CLR),
                                      opacity=0.7, visible=False, name='fields'))
    flat, flon, ftxt = [], [], []
    for fi in range(18):
        for fj in range(18):
            flon.append(-170.0 + fi * 20.0); flat.append(-85.0 + fj * 10.0)
            ftxt.append(chr(65 + fi) + chr(65 + fj))
    T['gflabels'] = _add(go.Scattergeo(lat=flat, lon=flon, text=ftxt, mode='text',
                                       hoverinfo='skip', visible=False, name='field-labels',
                                       textfont=dict(size=18, color=_GRIDLINE_CLR,
                                                     family='monospace')))

    import cartopy.io.shapereader as shpreader
    slats, slons = [], []
    try:
        shp1 = shpreader.natural_earth(resolution='10m', category='cultural',
                                       name='admin_1_states_provinces')
        for rec in shpreader.Reader(shp1).records():
            if rec.attributes.get('adm0_a3') not in ('USA', 'CAN'):
                continue
            g = rec.geometry.simplify(0.02, preserve_topology=True)
            for poly in getattr(g, 'geoms', [g]):
                xs, ys = poly.exterior.xy
                slons.extend([round(v, 3) for v in xs] + [None])
                slats.extend([round(v, 3) for v in ys] + [None])
    except Exception as exc:
        log.debug("State borders unavailable: %s", exc)
    T['borders'] = _add(go.Scattergeo(lat=slats, lon=slons, mode='lines', hoverinfo='skip',
                                      line=dict(width=0.6, color=_STATE_CLR),
                                      visible=False, name='state-borders'))
    T['cnames'] = _add(go.Scattergeo(
        lat=[v[0] for v in COUNTRY_CENTROIDS.values()],
        lon=[v[1] for v in COUNTRY_CENTROIDS.values()],
        text=list(COUNTRY_CENTROIDS), mode='text', hoverinfo='skip', visible=False,
        textfont=dict(size=13, color='#7a9aaa', family='monospace'), name='country-names'))
    T['snames'] = _add(go.Scattergeo(
        lat=[v[0] for v in STATE_CENTROIDS.values()],
        lon=[v[1] for v in STATE_CENTROIDS.values()],
        text=list(STATE_CENTROIDS), mode='text', hoverinfo='skip', visible=False,
        textfont=dict(size=11, color='#8899aa', family='monospace'), name='state-names'))

    # Great-circle lines: one trace per palette colour (a trace has one colour)
    for c in REGION_PALETTE:
        T['lines'].append(_add(go.Scattergeo(
            lat=[], lon=[], mode='lines', hoverinfo='skip', visible=False,
            line=dict(width=1.0, color=c), name='lines')))

    # Dots and their name labels, one trace per unit
    for u in UNITS:
        T['dots'][u] = _add(go.Scattergeo(
            lat=[], lon=[], mode='markers', visible=False, name=f'dots-{u}',
            marker=dict(size=10, color=[], opacity=0.95,
                        line=dict(width=1, color=_MAP_BG)),
            hovertext=[], hoverinfo='text'))
        T['labels'][u] = _add(go.Scattergeo(
            lat=[], lon=[], text=[], mode='text', textposition='top center',
            hoverinfo='skip', visible=False, name=f'labels-{u}',
            textfont=dict(size=12, color=[], family='monospace')))

    if home_pos:
        T['home'] = _add(go.Scattergeo(
            lat=[home_pos[0]], lon=[home_pos[1]], mode='markers', name='home',
            marker=dict(size=16, symbol='star', color='#FFFF00',
                        line=dict(color='#FF8800', width=1)),
            hovertemplate='Home station<extra></extra>'))

    fig = go.Figure(data=traces)
    fig.update_geos(
        projection_type='natural earth',
        showland=True,        landcolor=_LAND_COLOR,
        showocean=True,       oceancolor=_MAP_BG,
        showlakes=True,       lakecolor=_MAP_BG,
        showrivers=False,
        showcoastlines=True,  coastlinecolor=_COAST_CLR, coastlinewidth=0.6,
        showcountries=True,   countrycolor='#6a6050',   countrywidth=0.6,
        showsubunits=False,
        bgcolor=_MAP_BG,
        lataxis=dict(showgrid=True, gridcolor=_GRID_CLR, dtick=30, range=[lat0, lat1]),
        lonaxis=dict(showgrid=True, gridcolor=_GRID_CLR, dtick=30, range=[lon0, lon1]),
    )
    fig.update_layout(
        paper_bgcolor=_MAP_BG, margin=dict(l=0, r=0, t=0, b=0), showlegend=False,
        hoverlabel=dict(bgcolor=_MAP_BG, bordercolor='#3a5a7a', align='left',
                        font=dict(family='monospace', size=13, color='#c8d8e8')),
    )

    # ── Payload + starting state for the page script ─────────────────────────
    names = args.names
    start = {
        'fill':     args.fill,
        'dots':     args.boxes if args.boxes != 'none' else 'grid',
        'lines':    bool(home_pos) and not args.no_lines,
        'labels':   True,
        'cnames':   names in ('countries', 'all'),
        'snames':   names in ('states', 'all'),
        'borders':  args.borders == 'states',
        'gfields':  args.grid_lines in ('fields', 'squares'),
        'gsquares': args.grid_lines == 'squares',
    }
    payload = {
        'T': T, 'units': payload_units, 'order': list(UNITS), 'qsos': qrows,
        'palette': REGION_PALETTE, 'bands': bands, 'bg': _MAP_BG,
        'bandCount': {b: band_qsos[b] for b in bands}, 'bandColor': band_clr,
        'home': list(home_pos) if home_pos else None,
        'lineAlpha': args.line_alpha, 'start': start,
        'stats': _summary_text(qsos_with_pos, args, index),
    }
    script = ('var HAMAP = ' + json.dumps(payload, separators=(',', ':')) + ';\n'
              + _HTML_APP_JS)

    fig.write_html(
        out_path, include_plotlyjs=True, full_html=True,
        config=dict(scrollZoom=True, displaylogo=False,
                    modeBarButtonsToRemove=['select2d', 'lasso2d']),
        post_script=script,
        default_width='100%', default_height='100vh',
    )
    size_mb = os.path.getsize(out_path) / 1e6
    log.info("HTML saved: %s (%.1f MB)", out_path, size_mb)


# Page script for --html: control panel, live re-colouring / filtering,
# pinned draggable popups.  Reads the HAMAP payload defined just before it.
_HTML_APP_JS = r"""
(function () {
  var H = HAMAP, T = H.T, RANK = {grid: 0, grid4: 1, region: 2, country: 3};
  var AREA = {region: 1, country: 1};
  var S = JSON.parse(JSON.stringify(H.start));
  S.bands = {}; H.bands.forEach(function (b) { S.bands[b] = true; });
  var pinned = {}, zTop = 10000, gd, panelOpen = true;
  try { if (localStorage.getItem('hamap.panel') === '0') panelOpen = false; } catch (e) {}

  // ---- URL hash: #fill=..&dots=..&bands=20m,40m&lines=0&pin=Ohio ----------
  var pinReq = [];
  (location.hash || '').replace(/^#/, '').split('&').forEach(function (kv) {
    if (!kv) return;
    var p = kv.split('='), k = decodeURIComponent(p[0]), v = decodeURIComponent(p[1] || '');
    if (k === 'fill' || k === 'dots') S[k] = v;
    else if (k === 'bands') { H.bands.forEach(function (b) { S.bands[b] = v.split(',').indexOf(b) >= 0; }); }
    else if (k === 'pin') pinReq = v.split(',');
    else if (k === 'panel') panelOpen = v !== '0';
    else if (k === 'view') S.view = v.split(',').map(Number);   // lat,lon,zoom
    else if (k in S) S[k] = (v === '1' || v === 'true' || v === 'on');
  });

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function active(qi) { return S.bands[H.qsos[qi][1]]; }
  function groupActive(u, gi) { return H.units[u].q[gi].some(active); }
  function colorUnit() {
    var us = [S.dots, S.fill].filter(function (u) { return u && u !== 'none'; });
    if (!us.length) return 'grid';
    return us.reduce(function (a, b) { return RANK[a] >= RANK[b] ? a : b; });
  }
  function palIdx(u, gi) {
    var cu = colorUnit(), U = H.units[u];
    if (RANK[cu] <= RANK[u]) return U.color[gi];
    return H.units[cu].color[U.maj[cu][gi]];
  }

  // ---- great circle -------------------------------------------------------
  function gc(la1, lo1, la2, lo2) {
    var r = Math.PI / 180, p1 = la1 * r, l1 = lo1 * r, p2 = la2 * r, l2 = lo2 * r;
    var d = 2 * Math.asin(Math.sqrt(Math.pow(Math.sin((p2 - p1) / 2), 2) +
            Math.cos(p1) * Math.cos(p2) * Math.pow(Math.sin((l2 - l1) / 2), 2)));
    var lats = [], lons = [];
    if (d < 1e-8) return [[la1, la2], [lo1, lo2]];
    var n = Math.max(8, Math.round(d / r * 2));
    for (var i = 0; i <= n; i++) {
      var f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
      var x = A * Math.cos(p1) * Math.cos(l1) + B * Math.cos(p2) * Math.cos(l2);
      var y = A * Math.cos(p1) * Math.sin(l1) + B * Math.cos(p2) * Math.sin(l2);
      var z = A * Math.sin(p1) + B * Math.sin(p2);
      lats.push(Math.atan2(z, Math.sqrt(x * x + y * y)) / r); lons.push(Math.atan2(y, x) / r);
    }
    return [lats, lons];
  }

  // ---- render: rebuild every dynamic trace from the state -----------------
  function render() {
    var D = gd.data;
    H.order.forEach(function (u) {
      var U = H.units[u], n = U.label.length;
      // fill
      var ft = D[T.fill[u]], locs = [], zs = [];
      if (S.fill === u) U.shape.forEach(function (gi) {
        if (groupActive(u, gi)) { locs.push(String(gi)); zs.push(palIdx(u, gi) + 0.5); }
      });
      ft.locations = locs; ft.z = zs; ft.visible = S.fill === u;
      // dots + labels
      var dt = D[T.dots[u]], lt = D[T.labels[u]], on = S.dots === u;
      var la = [], lo = [], col = [], hov = [], txt = [], tcol = [];
      for (var gi = 0; gi < n; gi++) {
        var qs = U.q[gi].filter(active), c = H.palette[palIdx(u, gi)];
        var ok = on && qs.length > 0;
        la.push(ok ? U.lat[gi] : null); lo.push(ok ? U.lon[gi] : null); col.push(c);
        var calls = {}; qs.forEach(function (qi) { calls[H.qsos[qi][0]] = 1; });
        hov.push(esc(U.label[gi]) + ' — ' + qs.length + ' QSO' + (qs.length === 1 ? '' : 's')
                 + ', ' + Object.keys(calls).length + ' call' + (Object.keys(calls).length === 1 ? '' : 's')
                 + '<br><i>click for details</i>');
        txt.push(ok ? U.label[gi] : ''); tcol.push(c);
      }
      dt.lat = la; dt.lon = lo; dt.marker.color = col; dt.hovertext = hov; dt.visible = on;
      lt.lat = la; lt.lon = lo; lt.text = txt; lt.textfont.color = tcol;
      lt.visible = on && S.labels && !!AREA[u];
    });
    // great-circle lines, bucketed by colour
    var buckets = H.palette.map(function () { return [[], []]; }), nl = 0;
    if (S.lines && H.home && S.dots !== 'none') {
      var U = H.units[S.dots];
      for (var gi = 0; gi < U.label.length; gi++) {
        if (!groupActive(S.dots, gi)) continue;
        var p = gc(H.home[0], H.home[1], U.lat[gi], U.lon[gi]), b = buckets[palIdx(S.dots, gi)];
        b[0].push.apply(b[0], p[0]); b[0].push(null); b[1].push.apply(b[1], p[1]); b[1].push(null);
        nl++;
      }
    }
    var alpha = H.lineAlpha !== null ? H.lineAlpha
              : Math.max(0.15, 0.5 * Math.min(1, Math.sqrt(40 / Math.max(nl, 1))));
    T.lines.forEach(function (ti, i) {
      D[ti].lat = buckets[i][0]; D[ti].lon = buckets[i][1];
      D[ti].opacity = alpha; D[ti].visible = S.lines && nl > 0;
    });
    // Dot names already name each worked state/country: map names would repeat them
    var dotNames = S.labels && !!AREA[S.dots];
    D[T.cnames].visible = S.cnames && !dotNames; D[T.snames].visible = S.snames && !dotNames;
    D[T.borders].visible = S.borders;
    D[T.gfields].visible = S.gfields || S.gsquares; D[T.gflabels].visible = S.gfields || S.gsquares;
    D[T.gsquares].visible = S.gsquares;
    gd.layout.datarevision = (gd.layout.datarevision || 0) + 1;
    Plotly.react(gd, D, gd.layout);
    Object.keys(pinned).forEach(function (k) { pinned[k].refresh(); });
    if (gd) reposition();
    writeHash();
  }

  function writeHash() {
    var off = H.bands.filter(function (b) { return !S.bands[b]; });
    var h = 'fill=' + S.fill + '&dots=' + S.dots;
    if (off.length) h += '&bands=' + H.bands.filter(function (b) { return S.bands[b]; }).join(',');
    ['lines', 'labels', 'cnames', 'snames', 'borders', 'gfields', 'gsquares'].forEach(function (k) {
      if (S[k] !== H.start[k]) h += '&' + k + '=' + (S[k] ? 1 : 0);
    });
    if (S.view) h += '&view=' + S.view.map(function (x) { return +x.toFixed(2); }).join(',');
    history.replaceState(null, '', '#' + h);
  }

  // ---- popups ----------------------------------------------------------------
  function popupHTML(u, gi) {
    var U = H.units[u], c = H.palette[palIdx(u, gi)];
    var qs = U.q[gi].filter(active), byBand = {};
    qs.forEach(function (qi) { var q = H.qsos[qi]; (byBand[q[1]] = byBand[q[1]] || []).push(q); });
    var calls = {}; qs.forEach(function (qi) { calls[H.qsos[qi][0]] = 1; });
    var h = '<div style="font-weight:bold;color:' + c + ';font-size:15px">' + esc(U.label[gi]) + '</div>'
          + '<div style="color:#8899aa;margin-bottom:6px">' + qs.length + ' QSO' + (qs.length === 1 ? '' : 's')
          + ' · ' + Object.keys(calls).length + ' call' + (Object.keys(calls).length === 1 ? '' : 's') + '</div>';
    if (!qs.length) return h + '<div style="color:#8899aa">No QSOs on the selected bands</div>';
    H.bands.forEach(function (b) {
      var rows = byBand[b]; if (!rows) return;
      rows.sort(function (x, y) { return (x[0] + x[3]).localeCompare(y[0] + y[3]); });
      h += '<div style="margin-top:4px;border-top:1px solid #1e3050;padding-top:3px">'
         + '<span style="color:' + H.bandColor[b] + ';font-weight:bold">' + esc(b) + '</span></div>';
      rows.forEach(function (q) {
        var meta = [q[3], q[4]].filter(Boolean).map(esc).join(' · ');
        h += '<div style="padding-left:8px"><b style="color:' + H.bandColor[b] + '">' + esc(q[0]) + '</b>'
           + (q[2] ? '&nbsp;<span style="color:#aabbd0">' + esc(q[2]) + '</span>' : '')
           + (meta ? '<br><span style="color:#607890;padding-left:10px">' + meta + '</span>' : '') + '</div>';
      });
    });
    return h;
  }

  // ---- popups follow the map: dot lon/lat → pixel position in gd -----------
  function dotPx(lon, lat) {
    var geo = gd._fullLayout.geo, sp = geo && geo._subplot;
    if (!sp || !sp.projection) return null;
    var p = sp.projection([lon, lat]);
    if (!p || !isFinite(p[0]) || !isFinite(p[1])) return null;
    // Offset of the geo layer inside gd, calibrated from a dot Plotly actually
    // drew (d3 keeps its lon/lat on the node), so it matches pixel-for-pixel.
    var node = gd.querySelector('.scattergeo .point');
    var d = node && node.__data__;
    if (!d || !d.lonlat) return null;
    var q = sp.projection(d.lonlat), r = node.getBoundingClientRect(), g = gd.getBoundingClientRect();
    return [p[0] + (r.left + r.width / 2 - g.left - q[0]),
            p[1] + (r.top + r.height / 2 - g.top - q[1])];
  }
  var leaderSvg = null;
  function reposition() {
    if (!leaderSvg) {
      leaderSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      leaderSvg.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;'
        + 'pointer-events:none;overflow:visible;z-index:9500';
      gd.appendChild(leaderSvg);
    }
    var lines = '';
    Object.keys(pinned).forEach(function (k) {
      var P = pinned[k], d = dotPx(P.lon, P.lat);
      if (!d) { P.el.style.display = 'none'; return; }
      P.el.style.display = '';
      var x = d[0] + P.off[0], y = d[1] + P.off[1];
      P.el.style.left = x + 'px'; P.el.style.top = y + 'px';
      // leader from the dot to the nearest point of the popup's edge
      var w = P.el.offsetWidth, h = P.el.offsetHeight;
      var ex = Math.max(x, Math.min(d[0], x + w)), ey = Math.max(y, Math.min(d[1], y + h));
      lines += '<line x1="' + d[0] + '" y1="' + d[1] + '" x2="' + ex + '" y2="' + ey
             + '" stroke="' + P.color() + '" stroke-width="1.5" stroke-opacity="0.85"/>'
             + '<circle cx="' + d[0] + '" cy="' + d[1] + '" r="7" fill="none" stroke="'
             + P.color() + '" stroke-width="2"/>';
    });
    leaderSvg.innerHTML = lines;
  }

  function pin(u, gi, x, y) {
    var key = u + ':' + gi;
    if (pinned[key]) { pinned[key].el.remove(); delete pinned[key]; reposition(); return; }
    var el = document.createElement('div');
    el.style.cssText = 'position:absolute;left:' + x + 'px;top:' + y + 'px;background:' + '#0d1b2a'
      + ';border:1px solid #3a5a7a;border-radius:5px;font:13px/1.45 monospace;color:#c8d8e8;'
      + 'min-width:220px;max-width:380px;box-shadow:0 3px 12px rgba(0,0,0,.7);z-index:' + (++zTop);
    var bar = document.createElement('div');
    bar.style.cssText = 'display:flex;justify-content:space-between;align-items:center;'
      + 'background:#1a3a5a;border-bottom:1px solid #2a5070;border-radius:4px 4px 0 0;padding:2px 6px;cursor:move';
    bar.innerHTML = '<span style="color:#4a7a9a;letter-spacing:3px;user-select:none">&#8942;&#8942;&#8942;</span>';
    var x_ = document.createElement('button');
    x_.innerHTML = '&times;'; x_.title = 'Dismiss';
    x_.style.cssText = 'background:none;border:none;color:#8899aa;cursor:pointer;font-size:16px;line-height:1';
    x_.onclick = function (e) { e.stopPropagation(); el.remove(); delete pinned[key]; reposition(); };
    bar.appendChild(x_);
    var body = document.createElement('div');
    body.style.cssText = 'padding:6px 10px 8px;max-height:60vh;overflow-y:auto';
    el.appendChild(bar); el.appendChild(body);
    el.onclick = function (e) { e.stopPropagation(); };
    bar.addEventListener('mousedown', function (e) {
      if (e.target === x_) return;
      e.preventDefault(); el.style.zIndex = ++zTop;
      var sx = e.clientX, sy = e.clientY, sl = el.offsetLeft, st = el.offsetTop;
      function mv(e) {
        var P = pinned[key], d = P && dotPx(P.lon, P.lat);
        if (!d) return;
        P.off = [sl + e.clientX - sx - d[0], st + e.clientY - sy - d[1]];
        reposition();
      }
      function up() { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); }
      document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
    });
    var U = H.units[u], d0 = null;
    pinned[key] = {el: el, lon: U.lon[gi], lat: U.lat[gi], off: [0, 0],
      color: function () { return H.palette[palIdx(u, gi)]; },
      refresh: function () {
        if (S.dots !== u) { el.remove(); delete pinned[key]; return; }
        body.innerHTML = popupHTML(u, gi);
      }};
    pinned[key].refresh();
    gd.appendChild(el);
    d0 = dotPx(U.lon[gi], U.lat[gi]);
    if (d0) pinned[key].off = [x - d0[0], y - d0[1]];   // keep the offset as the map moves
    reposition();
  }

  // ---- control panel -----------------------------------------------------------
  function panel() {
    var p = document.createElement('div');
    p.style.cssText = 'position:absolute;left:10px;top:10px;z-index:9000;background:rgba(13,27,42,.93);'
      + 'border:1px solid #3a5a7a;border-radius:6px;font:13px/1.5 monospace;color:#c8d8e8;'
      + 'box-shadow:0 3px 12px rgba(0,0,0,.6);max-height:calc(100vh - 40px);overflow-y:auto';
    var head = document.createElement('div');
    head.style.cssText = 'padding:5px 10px;cursor:pointer;font-weight:bold;user-select:none;color:#dce8f4';
    var body = document.createElement('div');
    body.style.cssText = 'padding:2px 10px 8px';
    function setOpen(o) { panelOpen = o; body.style.display = o ? '' : 'none';
      head.textContent = (o ? '▾' : '▸') + ' Map options';
      try { localStorage.setItem('hamap.panel', o ? '1' : '0'); } catch (e) {} }
    head.onclick = function () { setOpen(!panelOpen); };
    setOpen(panelOpen);
    var NAMES = {none: 'none', grid: 'grid', grid4: 'grid4', region: 'state/country', country: 'country'};
    function radios(title, key) {
      var d = document.createElement('div');
      d.innerHTML = '<div style="color:#8899aa;margin-top:6px">' + title + '</div>';
      ['none', 'region', 'country', 'grid4', 'grid'].forEach(function (u) {
        var l = document.createElement('label');
        l.style.cssText = 'display:inline-block;margin-right:8px;cursor:pointer';
        var r = document.createElement('input'); r.type = 'radio'; r.name = key; r.checked = S[key] === u;
        r.onchange = function () { S[key] = u; render(); };
        l.appendChild(r); l.appendChild(document.createTextNode(' ' + NAMES[u])); d.appendChild(l);
      });
      return d;
    }
    function check(label, key, color) {
      var l = document.createElement('label');
      l.style.cssText = 'display:block;cursor:pointer' + (color ? ';color:' + color : '');
      var c = document.createElement('input'); c.type = 'checkbox';
      c.checked = key.charAt(0) === '#' ? S.bands[key.slice(1)] : S[key];
      c.onchange = function () {
        if (key.charAt(0) === '#') S.bands[key.slice(1)] = c.checked; else S[key] = c.checked;
        render(); };
      l.appendChild(c); l.appendChild(document.createTextNode(' ' + label));
      return [l, c];
    }
    body.appendChild(radios('Fill', 'fill'));
    body.appendChild(radios('Dots & popups', 'dots'));
    var lay = document.createElement('div');
    lay.innerHTML = '<div style="color:#8899aa;margin-top:6px">Layers</div>';
    [['great-circle lines', 'lines'], ['dot names (state/country)', 'labels'],
     ['country names', 'cnames'], ['state names', 'snames'], ['state borders', 'borders'],
     ['grid fields', 'gfields'], ['grid squares', 'gsquares']].forEach(function (x) {
      if (x[1] === 'lines' && !H.home) return;
      lay.appendChild(check(x[0], x[1])[0]); });
    body.appendChild(lay);
    var bd = document.createElement('div');
    bd.innerHTML = '<div style="color:#8899aa;margin-top:6px">QSOs by band</div>';
    var boxes = [];
    H.bands.forEach(function (b) {
      var w = check((b + '        ').slice(0, 6) + String(H.bandCount[b]).padStart(6), '#' + b, H.bandColor[b]);
      w[0].style.whiteSpace = 'pre'; boxes.push([b, w[1]]); bd.appendChild(w[0]);
    });
    var btns = document.createElement('div'); btns.style.marginTop = '3px';
    [['all', true], ['none', false]].forEach(function (x) {
      var btn = document.createElement('button'); btn.textContent = x[0];
      btn.style.cssText = 'font:12px monospace;margin-right:6px;background:#1a3a5a;color:#c8d8e8;'
        + 'border:1px solid #3a5a7a;border-radius:3px;cursor:pointer';
      btn.onclick = function () { boxes.forEach(function (bx) { S.bands[bx[0]] = x[1]; bx[1].checked = x[1]; });
        render(); };
      btns.appendChild(btn);
    });
    bd.appendChild(btns); body.appendChild(bd);
    p.appendChild(head); p.appendChild(body);
    return p;
  }

  function stats() {
    var s = document.createElement('pre');
    s.textContent = H.stats;
    s.style.cssText = 'position:absolute;left:10px;bottom:10px;z-index:8000;margin:0;padding:8px 12px;'
      + 'background:rgba(13,27,42,.9);border:1px solid #3a5a7a;border-radius:6px;'
      + 'font:12px/1.35 monospace;color:#c8d8e8;pointer-events:none';
    return s;
  }

  function init() {
    gd = document.querySelector('.js-plotly-plot');
    if (!gd || !gd.data) { setTimeout(init, 100); return; }
    document.body.style.margin = '0';
    document.body.style.background = H.bg;
    document.body.style.overflow = 'hidden';
    gd.style.position = 'relative';
    gd.appendChild(panel()); gd.appendChild(stats());
    gd.on('plotly_click', function (ev) {
      if (!ev || !ev.points || !ev.points.length) return;
      var pt = ev.points[0], u = null;
      Object.keys(T.dots).forEach(function (k) { if (T.dots[k] === pt.curveNumber) u = k; });
      if (!u) return;
      var r = gd.getBoundingClientRect();
      pin(u, pt.pointNumber, ev.event.clientX - r.left + 14, ev.event.clientY - r.top - 14);
    });
    render();
    gd.on('plotly_relayouting', reposition);
    window.addEventListener('resize', function () { setTimeout(reposition, 50); });
    // Remember pan / zoom in the URL so the view can be bookmarked
    gd.on('plotly_relayout', function () {
      reposition();
      var g = gd._fullLayout.geo;
      if (!g || !g.center) return;
      S.view = [g.center.lat, g.center.lon, g.projection.scale];
      writeHash();
    });
    function pinRequested() {
      pinReq.forEach(function (name, i) {
        var U = H.units[S.dots], gi = U.label.indexOf(name);
        // opposite side from the panel, cascading
        if (gi >= 0) pin(S.dots, gi, gd.clientWidth - 420 - 40 * i, 60 + 40 * i);
      });
    }
    // Popups from the URL anchor to their dots once the URL view is applied
    if (S.view && S.view.length === 3) {
      Plotly.relayout(gd, {'geo.center.lat': S.view[0], 'geo.center.lon': S.view[1],
                           'geo.projection.scale': S.view[2]}).then(pinRequested);
    } else {
      pinRequested();
    }
  }
  init();
}());
"""


# --------------------------------------------------------------------------- #
# Date filtering
# --------------------------------------------------------------------------- #

def _parse_date_arg(s):
    """Normalise a user-supplied date to YYYYMMDD for ADIF comparison."""
    clean = s.strip().replace('-', '')
    if not re.match(r'^\d{8}$', clean):
        raise argparse.ArgumentTypeError(
            f"Invalid date {s!r} — expected YYYY-MM-DD or YYYYMMDD"
        )
    return clean


def _fmt_date(s):
    """Format a YYYYMMDD string as YYYY-MM-DD for display."""
    if s and len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def filter_records(records, args, log):
    """Apply --start / --end / --tail-days / --tail filters. Returns filtered list."""
    from datetime import date, timedelta

    original = len(records)

    def _date(rec):
        return rec.get('QSO_DATE', '')

    if args.start:
        before  = len(records)
        records = [r for r in records if not _date(r) or _date(r) >= args.start]
        log.verbose("--start %s: %d → %d records", args.start, before, len(records))

    if args.end:
        before  = len(records)
        records = [r for r in records if not _date(r) or _date(r) <= args.end]
        log.verbose("--end %s: %d → %d records", args.end, before, len(records))

    if args.tail_days is not None:
        cutoff  = (date.today() - timedelta(days=args.tail_days)).strftime('%Y%m%d')
        before  = len(records)
        no_date = sum(1 for r in records if not _date(r))
        records = [r for r in records if not _date(r) or _date(r) >= cutoff]
        if no_date:
            log.warning("%d QSO(s) had no QSO_DATE and were kept without "
                        "--tail-days filtering", no_date)
        log.verbose("--tail-days %d (cutoff %s): %d → %d records",
                    args.tail_days, cutoff, before, len(records))

    if args.tail is not None:
        before  = len(records)
        records = records[-args.tail:]
        log.verbose("--tail %d: %d → %d records", args.tail, before, len(records))

    if len(records) != original:
        log.info("Date/count filters: %d → %d QSOs", original, len(records))

    return records


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def _box_calls_arg(s):
    """--box-calls value: 'all' (no cap) or a non-negative integer."""
    if s.lower() == 'all':
        return None
    n = int(s)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 'all' or >= 0")
    return n


def _line_alpha_arg(s):
    """--line-alpha value: 'auto' or an opacity 0..1."""
    if s.lower() == 'auto':
        return None
    a = float(s)
    if not 0.0 <= a <= 1.0:
        raise argparse.ArgumentTypeError("must be 'auto' or between 0 and 1")
    return a


def _line_width_arg(s):
    """--line-width value: 'auto' or a width in points > 0."""
    if s.lower() == 'auto':
        return None
    w = float(s)
    if w <= 0:
        raise argparse.ArgumentTypeError("must be 'auto' or > 0")
    return w


def build_parser():
    p = argparse.ArgumentParser(
        prog='hamap',
        description='Generate a high-resolution world map from an ADIF ham radio log.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  hamap contacts.adif                      # save PNG (default)\n"
            "  hamap contacts.adif --image              # explicit image mode\n"
            "  hamap contacts.adif --html               # interactive Plotly HTML\n"
            "  hamap contacts.adif --output map.png\n"
            "  hamap contacts.adif --html --output map.html\n"
            "  hamap contacts.adif --preview\n"
            "  hamap contacts.adif --my-grid EN82\n"
            "  hamap contacts.adif --extent full        # whole world incl. poles\n"
            "  hamap contacts.adif --start 2025-01-01 --end 2025-03-31\n"
            "  hamap contacts.adif --tail-days 30\n"
            "  hamap contacts.adif --tail 500\n"
            "  hamap contacts.adif --profile big --box-calls 20\n"
            "  hamap contacts.adif --show-config        # effective settings + sources\n"
            "  hamap --setup\n"
        ),
    )
    prof = p.add_argument_group('profiles')
    prof.add_argument('--profile', metavar='NAME',
                      help="Settings preset: 'auto' (default: picks small or big from "
                           "the log size), 'small', 'big', or one defined in the config "
                           "file. Command-line options override the profile.")
    prof.add_argument('--config', metavar='FILE',
                      help='Profile config file (default: ~/.hamap/config.ini)')
    prof.add_argument('--show-config', action='store_true',
                      help='Print the effective settings and where each came from, then exit')
    p.add_argument('adif_file', metavar='FILE', nargs='?',
                   help='ADIF log file to map')
    p.add_argument('--output', '-o', metavar='FILE',
                   help='Output file (default: <input>.png or <input>.html)')

    out_mode = p.add_mutually_exclusive_group()
    out_mode.add_argument('--image', action='store_true',
                          help='Generate a PNG image (default)')
    out_mode.add_argument('--html', action='store_true',
                          help='Generate a self-contained interactive HTML map (Plotly)')

    p.add_argument('--preview', action='store_true',
                   help='Open the image in a window instead of saving (image mode only)')
    p.add_argument('--my-grid', metavar='GRID',
                   help='Home station Maidenhead grid square (e.g. EN82)')
    p.add_argument('--setup', action='store_true',
                   help='Download offline map data to ~/.hamap/ and exit')

    content = p.add_argument_group(
        'map content',
        '  Units: grid = square as logged, grid4 = 4-char square,\n'
        '         region = US state / Canadian province, else country,\n'
        '         country = country.\n'
        '  Colour links what belongs together: dots, lines, boxes and fills take\n'
        '  the colour of the coarser of the --boxes and --fill units, and\n'
        '  neighbours always get distinct colours.')
    content.add_argument('--boxes', choices=('grid', 'grid4', 'region', 'country', 'none'),
                         default='grid',
                         help='What one info box covers (default: grid); none = dots only')
    content.add_argument('--box-calls', type=_box_calls_arg, metavar='N',
                         help="Callsigns per info box: 'all' (default), N for the N busiest "
                              'plus a "+k more" footer, 0 for a summary (counts per band)')
    content.add_argument('--band-colors', choices=('on', 'off'), default='on',
                         help='Colour callsigns in info boxes by band (default: on); '
                              'the band rows and band key are shown either way')
    content.add_argument('--fill', choices=('none', 'grid', 'grid4', 'region', 'country'),
                         default='none',
                         help='Tint every worked unit of this kind (default: none)')
    content.add_argument('--names', choices=('none', 'countries', 'states', 'all'),
                         default='none',
                         help='Geographic name labels (default: none)')
    content.add_argument('--borders', choices=('countries', 'states'), default='countries',
                         help='countries (default), or states to add US state / '
                              'Canadian province borders')
    content.add_argument('--grid-lines', choices=('none', 'fields', 'squares'),
                         default='none',
                         help='Maidenhead overlay: fields = 20°×10° lines + labels, '
                              'squares = also 2°×1° lines (default: none)')
    content.add_argument('--ocean-boxes', action=argparse.BooleanOptionalAction, default=False,
                         help='Place crowded info boxes over open water when that is '
                              'not a big detour, leaving land for inland boxes')

    lines = p.add_argument_group('great-circle lines')
    lines.add_argument('--no-lines', action='store_true',
                       help='Skip great-circle lines to contacts')
    lines.add_argument('--lines', dest='no_lines', action='store_false',
                       help='Draw great-circle lines (undoes --no-lines from a profile)')
    lines.add_argument('--line-alpha', type=_line_alpha_arg, metavar='A',
                       help="Line opacity 0..1 or 'auto' (default: auto, "
                            "0.45 for small logs fading to 0.12 for large ones)")
    lines.add_argument('--line-width', type=_line_width_arg, metavar='PT',
                       help="Line width in points, or 'auto' (default: auto, "
                            "0.8 for small logs thinning to 0.4 for large ones)")

    canvas = p.add_argument_group('canvas')
    canvas.add_argument('--dpi', type=int, default=300,
                        help='Output resolution in DPI (default: 300)')
    canvas.add_argument('--width', type=float, default=48.0,
                        help='Figure width in inches (default: 48)')
    # Height now follows the map extent; accepted for backward compatibility.
    canvas.add_argument('--height', type=float, help=argparse.SUPPRESS)
    canvas.add_argument('--extent', choices=('auto', 'full', 'poles'), default='auto',
                        help='Map area: auto = fit contacts + margin (default), '
                             'full = whole world, poles = world without polar regions')
    canvas.add_argument('--font-size', type=float, default=3.0, metavar='PT',
                        help='Info box font size in points (default: 3.0)')

    filt = p.add_argument_group('filtering')
    filt.add_argument('--confirmed', choices=('lotw',),
                      help='Only include confirmed QSOs: lotw = LOTW_QSL_RCVD=Y')
    filt.add_argument('--start', metavar='DATE', type=_parse_date_arg,
                      help='Only include QSOs on or after DATE (YYYY-MM-DD or YYYYMMDD)')
    filt.add_argument('--end', metavar='DATE', type=_parse_date_arg,
                      help='Only include QSOs on or before DATE (YYYY-MM-DD or YYYYMMDD)')
    tail_grp = filt.add_mutually_exclusive_group()
    tail_grp.add_argument('--tail', metavar='N', type=int,
                          help='Only process the last N QSOs (after date filters)')
    tail_grp.add_argument('--tail-days', metavar='N', type=int,
                          help='Only process QSOs from the last N days')

    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument('--verbose', action='store_true',
                           help='Show processing details')
    verbosity.add_argument('--debug', action='store_true',
                           help='Show debug output')
    verbosity.add_argument('--trace', action='store_true',
                           help='Show full trace output')
    p.add_argument('--logfile', metavar='FILE',
                   help='Also write log output to FILE')
    p.add_argument('--syslog', action='store_true',
                   help='Also send log output to syslog')

    return p


# --------------------------------------------------------------------------- #
# Profiles:  built-in defaults  <  profile (+ what it extends)  <  command line
# --------------------------------------------------------------------------- #

# Built-in profiles, keyed by long option name (as in the config file)
BUILTIN_PROFILES = {
    'small': {
        'boxes':      'grid',
        'fill':       'region',
        'names':      'all',
        'borders':    'states',
    },
    'big': {
        'boxes':       'region',
        'box-calls':   12,
        'fill':        'region',
        'names':       'all',
        'borders':     'states',
        'ocean-boxes': True,
        'line-alpha':  0.40,
        'line-width':  0.4,
        'width':       64,
    },
    'grids': {
        'boxes':      'grid4',
        'fill':       'grid4',
        'grid-lines': 'squares',
        'names':      'countries',
    },
    'dxcc': {
        'confirmed':   'lotw',
        'boxes':       'country',
        'box-calls':   12,
        'fill':        'country',
        'names':       'countries',
        'ocean-boxes': True,
        'no-lines':    True,
    },
}
_AUTO_BIG_GRIDS = 300      # 'auto' picks big at or above this many 4-char grids

# Options that name inputs/outputs or control the profile system itself
_NOT_IN_PROFILES = {'adif_file', 'output', 'profile', 'config', 'show_config',
                    'setup', 'help',
                    'preview'}     # picks the matplotlib backend before profiles load
_UNSET = object()


class Profiles:
    """
    Resolves a profile name into a full argparse Namespace.

    Config file format (INI), keys are long option names without dashes:

        [defaults]
        profile = big

        [profile:poster]
        extends = big
        width   = 80
        extent  = full

    A config profile with a built-in's name replaces the built-in; use
    'extends' to build on one instead.
    """

    def __init__(self, parser, defaults, explicit, config_path):
        self.parser   = parser
        self.defaults = defaults
        self.explicit = explicit
        self.actions  = {s[2:]: a for a in parser._actions
                         for s in a.option_strings if s.startswith('--')}
        self.profiles = {n: {'extends': None, 'values': list(v.items()), 'from': 'built-in'}
                         for n, v in BUILTIN_PROFILES.items()}
        self.default_profile = None
        self.config_path     = config_path
        if config_path and os.path.exists(config_path):
            cp = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
            try:
                cp.read(config_path)
            except configparser.Error as exc:
                parser.error(f"{config_path}: {exc}")
            for sec in cp.sections():
                if sec.startswith('profile:'):
                    body = dict(cp[sec])
                    self.profiles[sec.split(':', 1)[1].strip()] = {
                        'extends': body.pop('extends', None),
                        'values':  list(body.items()),
                        'from':    config_path,
                    }
                elif sec == 'defaults':
                    self.default_profile = cp[sec].get('profile')

    def _convert(self, pname, key, raw):
        """(dest, value) for one profile entry, validated like the command line."""
        a = self.actions.get(key)
        if a is None or a.dest in _NOT_IN_PROFILES:
            self.parser.error(f"profile '{pname}': unknown or unsupported option '{key}'")
        if a.nargs == 0:                                   # on/off flag
            if isinstance(raw, bool):
                on = raw
            elif str(raw).lower() in ('1', 'yes', 'true', 'on'):
                on = True
            elif str(raw).lower() in ('0', 'no', 'false', 'off'):
                on = False
            else:
                self.parser.error(f"profile '{pname}': {key} needs yes/no, got {raw!r}")
            if isinstance(a, argparse.BooleanOptionalAction):
                return a.dest, (not on) if key.startswith('no-') else on
            return a.dest, on if a.const else not on       # store_true / store_false
        try:
            val = a.type(str(raw)) if a.type else raw
        except (ValueError, argparse.ArgumentTypeError) as exc:
            self.parser.error(f"profile '{pname}': {key}: {exc}")
        if a.choices and val not in a.choices:
            self.parser.error(f"profile '{pname}': {key} must be one of "
                              f"{', '.join(map(str, a.choices))}")
        return a.dest, val

    def names(self):
        return sorted(self.profiles)

    def resolve(self, name):
        chain, n = [], name
        while n:
            if n in chain:
                self.parser.error(f"profile '{name}': 'extends' loop at '{n}'")
            if n not in self.profiles:
                self.parser.error(f"unknown profile '{n}' (available: auto, "
                                  f"{', '.join(self.names())})")
            chain.append(n)
            n = self.profiles[n]['extends']

        values  = dict(self.defaults)
        sources = {k: 'default' for k in values}
        for pname in reversed(chain):                      # base profile first
            for key, raw in self.profiles[pname]['values']:
                dest, val = self._convert(pname, key, raw)
                values[dest], sources[dest] = val, f'profile {pname}'
        for k, v in self.explicit.items():
            values[k], sources[k] = v, 'command line'

        args = argparse.Namespace(**values)
        args.profile, args.profile_chain = name, chain
        args.profile_sources, args.profiles = sources, self
        args.profile_note = ''
        return args


def parse_args(argv=None):
    """
    Parse the command line and apply the selected profile.

    With profile 'auto' the returned args use 'small' provisionally
    (args.profile == 'auto'); main() re-resolves once the log is read.
    """
    parser   = build_parser()
    defaults = vars(parser.parse_args([]))

    # Parse again with every default replaced by a sentinel, so options typed
    # on the command line are known even when they equal the default.
    probe = build_parser()
    probe.set_defaults(**{k: _UNSET for k in defaults})
    explicit = {k: v for k, v in vars(probe.parse_args(argv)).items() if v is not _UNSET}

    config_path = explicit.get('config') or os.path.join(HAMAP_DIR, 'config.ini')
    if explicit.get('config') and not os.path.exists(config_path):
        parser.error(f"config file not found: {config_path}")
    profiles = Profiles(parser, defaults, explicit, config_path)
    name     = explicit.get('profile') or profiles.default_profile or 'auto'

    args = profiles.resolve('small' if name == 'auto' else name)
    args.profile = name
    return args


def choose_auto_profile(args, records, log):
    """Resolve profile 'auto' from the (filtered) log size; returns new args."""
    n4 = len({r.get('GRIDSQUARE', '').strip().upper()[:4]
              for r in records if r.get('GRIDSQUARE', '').strip()})
    chosen = 'big' if n4 >= _AUTO_BIG_GRIDS else 'small'
    new = args.profiles.resolve(chosen)
    new.profile_note = (f"auto → {chosen} ({n4} distinct 4-char grids; "
                        f"big at ≥ {_AUTO_BIG_GRIDS})")
    log.info("Profile: %s", new.profile_note)
    return new


def show_config(args):
    """Print effective settings and their sources."""
    src = args.profile_sources
    print(f"Profile: {args.profile_note or args.profile}"
          + (f"  (chain: {' → '.join(args.profile_chain)})"
             if len(args.profile_chain) > 1 else ''))
    cfg = args.profiles.config_path
    print(f"Config:  {cfg}{'' if cfg and os.path.exists(cfg) else '  (not found)'}")
    print(f"Profiles available: auto, {', '.join(args.profiles.names())}")
    print()
    skip = _NOT_IN_PROFILES | {'height'}
    for dest in sorted(k for k in src if k not in skip):
        val = getattr(args, dest)
        if val is None:
            shown = {'box_calls': 'all', 'line_alpha': 'auto',
                     'line_width': 'auto'}.get(dest, '-')
        elif isinstance(val, bool):
            shown = 'yes' if val else 'no'
        else:
            shown = val
        mark = '' if src[dest] == 'default' else '  *'
        print(f"  {dest.replace('_', '-'):<16} {str(shown):<14} {src[dest]}{mark}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    args = parse_args()
    log = AppLogger.from_args(args, 'hamap')

    if args.setup:
        do_setup(log)
        return

    if not args.adif_file:
        if args.show_config:
            if args.profile == 'auto':
                args.profile_note = "auto (decided per log: give a FILE to see which)"
            show_config(args)
            return
        log.error("No ADIF file specified. Use --help for usage.")
        sys.exit(1)

    # ---- Parse ADIF -------------------------------------------------------
    header, records = parse_adif(args.adif_file, log)
    log.info("Loaded %d QSO records from %s", len(records), args.adif_file)

    # ---- Apply date / count filters ---------------------------------------
    records = filter_records(records, args, log)
    if not records:
        log.error("No QSOs remain after filtering.")
        sys.exit(1)

    # ---- Resolve profile 'auto' now that the log size is known -----------
    if args.profile == 'auto':
        args = choose_auto_profile(args, records, log)
    else:
        log.verbose("Profile: %s", ' → '.join(args.profile_chain))
    if args.show_config:
        show_config(args)
        return

    # ---- --confirmed: keep only confirmed QSOs ---------------------------
    if args.confirmed == 'lotw':
        before  = len(records)
        records = [r for r in records if r.get('LOTW_QSL_RCVD', '').upper() == 'Y']
        log.info("--confirmed lotw: %d → %d LoTW-confirmed QSOs", before, len(records))
        if not records:
            log.error("No LoTW-confirmed QSOs found (LOTW_QSL_RCVD=Y). "
                      "Ensure your ADIF export includes LoTW confirmation fields.")
            sys.exit(1)

    # ---- Determine home station location ----------------------------------
    home_pos = None
    if args.my_grid:
        home_pos = maidenhead_to_latlon(args.my_grid)
        if home_pos:
            log.verbose("Home: --my-grid %s → %.4f, %.4f", args.my_grid, *home_pos)
        else:
            log.warning("Could not parse home grid square: %s", args.my_grid)
    else:
        my_grid = header.get('MY_GRIDSQUARE', '')
        if not my_grid:
            for rec in records[:10]:
                my_grid = rec.get('MY_GRIDSQUARE', '')
                if my_grid:
                    break
        if my_grid:
            home_pos = maidenhead_to_latlon(my_grid)
            if home_pos:
                log.verbose("Home: ADIF MY_GRIDSQUARE %s → %.4f, %.4f",
                            my_grid, *home_pos)

    if not home_pos:
        log.info("Home station location unknown — great-circle lines disabled.")
        log.info("  Provide one with --my-grid GRID (e.g. --my-grid EN82).")

    # ---- Resolve contact locations ----------------------------------------
    qsos_with_pos = []
    skipped = 0
    for qso in records:
        pos = resolve_location(qso, log)
        if pos:
            qsos_with_pos.append((qso, pos))
        else:
            skipped += 1
            log.debug("Skipped %s — no location data", qso.get('CALL', '?'))

    log.info("Located %d of %d QSOs (%d skipped — no grid/country data)",
             len(qsos_with_pos), len(records), skipped)

    if not qsos_with_pos:
        log.error("No QSOs with resolvable locations — cannot generate map.")
        log.error("Ensure your ADIF file contains GRIDSQUARE or COUNTRY fields.")
        sys.exit(1)

    # ---- Determine output mode and path -----------------------------------
    if args.html:
        ext      = '.html'
        out_path = args.output or (
            os.path.splitext(os.path.abspath(args.adif_file))[0] + ext)

        log.info("Generating interactive HTML map...")
        generate_html_plotly(qsos_with_pos, args, home_pos, out_path, log)

    else:
        # Image mode (default, or explicit --image)
        out_path = args.output or (
            os.path.splitext(os.path.abspath(args.adif_file))[0] + '.png')

        log.info("Generating map (%.0f in wide @ %d DPI = ~%.0f px, extent: %s)...",
                 args.width, args.dpi, args.width * args.dpi, args.extent)

        fig, _ = generate_map(qsos_with_pos, home_pos, args, log)

        if args.preview:
            log.info("Opening preview window...")
            plt.show()
        else:
            log.info("Saving to %s...", out_path)
            fig.savefig(out_path, dpi=args.dpi, bbox_inches='tight',
                        facecolor=fig.get_facecolor())
            log.info("Saved: %s", out_path)

        plt.close(fig)


if __name__ == '__main__':
    main()

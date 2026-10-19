"""Background live-data collector and authenticated cache authority.

The terminal frontend never performs Internet requests.  This daemon is the
only Information Network component with outbound access; all UI requests are
served from its bounded SQLite cache.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import email.utils
import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
import math
import os
import queue
import random
import re
import socket
import sqlite3
import ssl
import stat
import threading
import time
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import connect
from .identity import validate
from .rpc import RPCError, serve
from .validate import integer, text

try:
    from sgp4.api import Satrec, jday
    from sgp4 import omm
except ImportError:  # Covered by the service health/status output.
    Satrec = None
    jday = None
    omm = None


USER_AGENT = os.environ.get("MARS_INFORMATION_USER_AGENT", "MarsHotelInformationNetwork/1.0")
MAX_RESPONSE = 1_048_576
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_ARTICLE_TEXT = 160_000
ARTICLE_CHUNK = 7_000
ARTICLE_LEASE_SECONDS = 300
ALLOWED_HOSTS = {
    "api.open-meteo.com",
    "geocoding-api.open-meteo.com",
    "api.weather.gov",
    "services.swpc.noaa.gov",
    "earthquake.usgs.gov",
    "celestrak.org",
    "www.nasa.gov",
    "science.nasa.gov",
    "www.cisa.gov",
    "globalvoices.org",
    "advox.globalvoices.org",
    "www.eff.org",
    "en.wikipedia.org",
    "api.tvmaze.com",
    "api.massive.com",
    "query1.finance.yahoo.com",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
  cache_key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_health (
  source TEXT PRIMARY KEY,
  last_attempt INTEGER NOT NULL,
  last_success INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS preferences (
  account_id INTEGER PRIMARY KEY,
  location_name TEXT NOT NULL,
  latitude REAL NOT NULL,
  longitude REAL NOT NULL,
  timezone TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS searches (
  query TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS article_leases (
  article_id TEXT NOT NULL,
  session_key TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY(article_id, session_key)
);
"""

NEWS_FEEDS = {
    "us": [
        ("Global Voices - United States",
         "https://globalvoices.org/-/world/north-america/usa/feed/",
         "global-voices-full"),
    ],
    "world": [
        ("Global Voices",
         "https://globalvoices.org/feed/?cat=-28", "global-voices-full"),
    ],
    "technology": [
        ("EFF Deeplinks", "https://www.eff.org/rss/updates.xml", "eff-full"),
        ("NASA Technology", "https://www.nasa.gov/technology/feed/", "feed-full"),
    ],
    "cybersecurity": [
        ("Global Voices Advox", "https://advox.globalvoices.org/feed/",
         "global-voices-full"),
        ("CISA Cybersecurity Advisories",
         "https://www.cisa.gov/cybersecurity-advisories/all.xml", "cisa-page"),
    ],
    "science": [
        ("NASA Science and Missions", "https://www.nasa.gov/feed/", "feed-full"),
    ],
    "space": [
        ("NASA News Releases", "https://www.nasa.gov/news-release/feed/",
         "feed-full"),
    ],
}

GLOBAL_INTERVALS = {
    "solar": 300,
    "earthquakes": 300,
    "iss": 7200,
    "news:us": 900,
    "news:world": 900,
    "news:technology": 900,
    "news:cybersecurity": 900,
    "news:science": 900,
    "news:space": 900,
    "movies": 3600,
    "television": 3600,
    "markets": 3600,
}

MARKET_BENCHMARKS = (
    ("DIA", "Dow Jones ETF proxy", "ETF PROXY"),
    ("SPY", "S&P 500 ETF proxy", "ETF PROXY"),
    ("I:COMP", "NASDAQ Composite", "INDEX"),
    ("QQQ", "Nasdaq-100 ETF proxy", "ETF PROXY"),
)

UNOFFICIAL_INDEX_BENCHMARKS = (
    ("^DJI", "Dow Jones Industrial Average"),
    ("^GSPC", "S&P 500"),
)

MARKET_PROXY_FALLBACKS = {
    "^DJI": ("DIA", "Dow Jones ETF proxy", "ETF PROXY"),
    "^GSPC": ("SPY", "S&P 500 ETF proxy", "ETF PROXY"),
}

MOVIE_WINDOW_DAYS = 90
TV_WINDOW_DAYS = 10

HF_BANDS = (
    ("160m", 1.8), ("80m", 3.5), ("40m", 7.0), ("30m", 10.1),
    ("20m", 14.0), ("17m", 18.1), ("15m", 21.0), ("12m", 24.9),
    ("10m", 28.0),
)


def _number(value: object, default: float = 0.0) -> float:
    """Return a finite provider number without allowing NaN/Inf downstream."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def solar_elevation(latitude: float, longitude: float,
                    when: datetime) -> float:
    """Approximate solar elevation for local day/night classification.

    This compact NOAA-style equation-of-time calculation is intentionally used
    only to select day, twilight, or night heuristics. It is not an
    ionospheric propagation model.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    utc = when.astimezone(timezone.utc)
    day = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60.0 + utc.second / 3600.0
    gamma = 2.0 * math.pi / 365.0 * (day - 1 + (hour - 12.0) / 24.0)
    equation = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma) - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma))
    declination = (
        0.006918 - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma))
    solar_minutes = (utc.hour * 60.0 + utc.minute + utc.second / 60.0
                     + 4.0 * longitude + equation) % 1440.0
    hour_angle = math.radians(solar_minutes / 4.0 - 180.0)
    latitude_radians = math.radians(latitude)
    sine = (math.sin(latitude_radians) * math.sin(declination)
            + math.cos(latitude_radians) * math.cos(declination)
            * math.cos(hour_angle))
    return math.degrees(math.asin(max(-1.0, min(1.0, sine))))


def _condition(score: int) -> str:
    return ("POOR", "FAIR", "GOOD")[max(0, min(2, score))]


def local_hf_outlook(solar: dict[str, object], location: dict[str, object],
                     when: datetime | None = None) -> dict[str, object]:
    """Build a conservative location-aware HF operating outlook.

    The result combines local solar illumination with NOAA-observed F10.7,
    planetary Kp, and NOAA R/S scales. It deliberately does not claim path,
    MUF, signal-level, or contact-probability prediction; those require a
    destination and station parameters under a model such as ITU-R P.533.
    """
    latitude = _number(location.get("latitude"))
    longitude = _number(location.get("longitude"))
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise RPCError("saved location coordinates are invalid")
    timezone_name = str(location.get("timezone") or "UTC")
    try:
        local_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone_name = "UTC"
        local_zone = timezone.utc
    instant = when or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    elevation = solar_elevation(latitude, longitude, instant)
    period = ("DAYLIGHT" if elevation >= 0.0 else
              ("TWILIGHT" if elevation >= -12.0 else "NIGHT"))

    flux = _number(solar.get("forecast_flux"), _number(solar.get("flux"), 70.0))
    observed_kp = _number(solar.get("kp"))
    forecast_kp = max(observed_kp, _number(solar.get("forecast_kp_24h")))
    radio_scale = int(_number(solar.get("radio_blackout_scale")))
    radiation_scale = int(_number(solar.get("solar_radiation_scale")))
    high_latitude = abs(latitude) >= 55.0

    # Scores are deliberately broad. They are a band-selection starting point,
    # never a guaranteed opening. Only the named NOAA quantities adjust them.
    day_scores = {"160m": 0, "80m": 0, "40m": 1, "30m": 2,
                  "20m": 2, "17m": 1, "15m": 1, "12m": 0, "10m": 0}
    night_scores = {"160m": 2, "80m": 2, "40m": 2, "30m": 1,
                    "20m": 1, "17m": 0, "15m": 0, "12m": 0, "10m": 0}
    for band, threshold in (("17m", 80), ("15m", 90), ("12m", 105), ("10m", 115)):
        if flux >= threshold:
            day_scores[band] += 1
        if flux >= threshold + 20:
            day_scores[band] += 1
    if flux < 75:
        day_scores["20m"] -= 1
        day_scores["17m"] -= 1

    now_day_scores = dict(day_scores)
    now_night_scores = dict(night_scores)

    def apply_geomagnetic(scoresets: tuple[dict[str, int], ...], kp_value: float) -> None:
        disturbance = 2 if kp_value >= 6 else (1 if kp_value >= 4 else 0)
        for scores in scoresets:
            for band in scores:
                scores[band] -= disturbance
        if high_latitude and (kp_value >= 4 or radiation_scale >= 1):
            for scores in scoresets:
                for band in scores:
                    scores[band] -= 1

    apply_geomagnetic((day_scores, night_scores), forecast_kp)
    apply_geomagnetic((now_day_scores, now_night_scores), observed_kp)
    if radio_scale:
        # Solar X-ray absorption affects the sunlit hemisphere. Lower bands
        # usually suffer first; a major event can impair the whole HF range.
        for band in day_scores:
            penalty = (radio_scale if band in ("160m", "80m", "40m", "30m")
                       else max(1, radio_scale - 1))
            day_scores[band] -= penalty
            now_day_scores[band] -= penalty

    rows: list[dict[str, object]] = []
    for band, mhz in HF_BANDS:
        day = max(0, min(2, day_scores[band]))
        night = max(0, min(2, night_scores[band]))
        if period == "DAYLIGHT":
            current = max(0, min(2, now_day_scores[band]))
        elif period == "NIGHT":
            current = max(0, min(2, now_night_scores[band]))
        else:
            now_day = max(0, min(2, now_day_scores[band]))
            now_night = max(0, min(2, now_night_scores[band]))
            current = (min(now_day, now_night) if abs(now_day - now_night) > 1
                       else max(now_day, now_night))
        rows.append({"band": band, "mhz": mhz, "now": _condition(current),
                     "day": _condition(day), "night": _condition(night)})

    local_time = instant.astimezone(local_zone)
    cautions = ["ZIP/city sets the station location; this is not a point-to-point prediction."]
    if forecast_kp >= 4:
        cautions.append(
            f"Forecast Kp up to {forecast_kp:g} may disrupt normal HF paths in the next 24 hours.")
    if radio_scale:
        cautions.append(f"NOAA R{radio_scale} conditions may absorb sunlit HF signals.")
    if high_latitude:
        cautions.append("High-latitude paths are especially sensitive to geomagnetic activity.")
    return {
        "location_name": str(location.get("location_name") or location.get("name") or "Saved location"),
        "timezone": timezone_name,
        "local_time": local_time.isoformat(timespec="minutes"),
        "solar_elevation": round(elevation, 1),
        "period": period,
        "basis_flux": flux,
        "basis_kp": observed_kp,
        "forecast_kp_24h": forecast_kp,
        "rows": rows,
        "cautions": cautions,
        "method": "LOCAL HEURISTIC / LOW CONFIDENCE",
    }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise RPCError("provider redirect refused")


def clean_text(value: object, maximum: int = 12_000) -> str:
    """Turn provider text/HTML into passive terminal-safe Unicode."""
    raw = str(value)
    # Do not route untrusted provider markup through HTMLParser.  These simple,
    # bounded substitutions preserve paragraph boundaries but never interpret
    # tags, attributes, scripts, entities, or terminal content as commands.
    raw = re.sub(r"(?is)<(?:script|style|noscript|template|svg)(?:\s[^>]{0,1000})?>.*?"
                 r"</(?:script|style|noscript|template|svg)\s*>", "", raw)
    raw = re.sub(r"(?i)<li(?:\s[^>]{0,1000})?>", "\n* ", raw)
    raw = re.sub(r"(?i)</?(?:p|br|div|h[1-6]|tr|table|blockquote|section|article)"
                 r"(?:\s[^>]{0,1000})?>", "\n", raw)
    raw = re.sub(r"(?i)</?(?:td|th)(?:\s[^>]{0,1000})?>", "  ", raw)
    # Tags from modern CMS feeds can contain very large responsive-image
    # attributes. The response itself is already byte-bounded, so remove the
    # complete tag instead of leaking the tail of a long <img> into a terminal.
    raw = re.sub(r"(?s)<[^>]*>", "", raw)
    value = html.unescape(raw)
    result: list[str] = []
    blank = False
    for character in str(value).replace("\r\n", "\n").replace("\r", "\n"):
        code = ord(character)
        if character == "\n":
            if not blank:
                result.append(character)
            blank = True
        elif character == "\t":
            result.append("    ")
            blank = False
        elif code < 32 or code == 127 or 0x80 <= code <= 0x9f:
            continue
        else:
            result.append(character)
            blank = False
        if len(result) >= maximum:
            break
    return "".join(result).strip()


def _validate_public_dns(host: str) -> None:
    """Reject provider names resolving only to local/special-use addresses."""
    answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not answers:
        raise RPCError("provider DNS returned no addresses")
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_unspecified
                or address.is_reserved):
            raise RPCError("provider resolved to a non-public address")


def fetch(url: str, accept: str, maximum: int = MAX_RESPONSE,
          authorization: str = "") -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise RPCError("provider is not allowlisted")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise RPCError("invalid provider URL")
    _validate_public_dns(parsed.hostname)
    opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if authorization:
        headers["Authorization"] = authorization
    request = Request(url, headers=headers)
    with opener.open(request, timeout=15) as response:
        content_type = response.headers.get_content_type().lower()
        if "json" in accept:
            permitted_types = {"application/json", "application/geo+json"}
        elif "text/html" in accept:
            permitted_types = {"text/html", "application/xhtml+xml"}
        else:
            permitted_types = {"application/rss+xml", "application/atom+xml",
                               "application/xml", "text/xml"}
        if content_type not in permitted_types:
            raise RPCError("provider returned an unexpected content type")
        length = response.headers.get("Content-Length")
        if length and int(length) > maximum:
            raise RPCError("provider response is too large")
        raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise RPCError("provider response is too large")
        return raw


def fetch_json(url: str, maximum: int = MAX_RESPONSE) -> Any:
    raw = fetch(url, "application/json", maximum)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RPCError("provider returned invalid JSON") from exc


def fetch_bearer_json(url: str, bearer: str,
                      maximum: int = MAX_RESPONSE) -> Any:
    """Fetch JSON with a secret header, never a credential-bearing URL."""
    raw = fetch(url, "application/json", maximum,
                authorization="Bearer " + bearer)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RPCError("provider returned invalid JSON") from exc


def read_private_secret(path: str) -> str:
    """Read a small no-follow, owner-only runtime secret."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077:
            raise RuntimeError("market API secret must be a private regular file")
        raw = os.read(descriptor, 257)
    finally:
        os.close(descriptor)
    if len(raw) > 256:
        raise RuntimeError("market API secret is too long")
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("market API secret is not ASCII") from exc
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", value):
        raise RuntimeError("market API secret has invalid format")
    return value


def normalize_market_series(payload: object, symbol: str,
                            label: str, kind: str) -> dict[str, object]:
    """Validate daily aggregate bars and calculate close-to-close movement."""
    if (not isinstance(payload, dict)
            or payload.get("status") not in {"OK", "DELAYED"}):
        raise RPCError("market provider returned an unsuccessful response")
    rows = payload.get("results")
    if not isinstance(rows, list) or not rows or len(rows) > 20:
        raise RPCError("market provider returned no usable daily prices")
    normalized: list[dict[str, float | int]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RPCError("market provider returned malformed daily prices")
        timestamp = integer(row.get("t"), "market timestamp", 946684800000,
                            4102444800000)
        item: dict[str, float | int] = {"timestamp": timestamp // 1000}
        for field in ("o", "h", "l", "c"):
            item[field] = _finite(row.get(field), 0.000001, 100_000_000,
                                  f"market {field}")
        volume = row.get("v")
        item["volume"] = (0.0 if volume is None else
                          _finite(volume, 0, 10_000_000_000_000,
                                  "market volume"))
        normalized.append(item)
    normalized.sort(key=lambda item: int(item["timestamp"]))
    latest = normalized[-1]
    previous_close = (float(normalized[-2]["c"]) if len(normalized) > 1
                      else float(latest["o"]))
    change = float(latest["c"]) - previous_close
    percent = (change / previous_close * 100.0) if previous_close else 0.0
    return {
        "symbol": symbol, "label": clean_text(label, 80), "kind": kind,
        "open": round(float(latest["o"]), 4),
        "high": round(float(latest["h"]), 4),
        "low": round(float(latest["l"]), 4),
        "close": round(float(latest["c"]), 4),
        "previous_close": round(previous_close, 4),
        "change": round(change, 4), "percent": round(percent, 4),
        "volume": int(float(latest["volume"])),
        "market_date": datetime.fromtimestamp(
            int(latest["timestamp"]), timezone.utc).strftime("%Y-%m-%d"),
    }


def normalize_unofficial_index_chart(payload: object, symbol: str,
                                     label: str) -> dict[str, object]:
    """Validate a small Yahoo chart response without trusting its metadata."""
    if not isinstance(payload, dict):
        raise RPCError("unofficial index source returned malformed data")
    chart = payload.get("chart")
    if not isinstance(chart, dict) or chart.get("error") is not None:
        raise RPCError("unofficial index source returned an error")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1:
        raise RPCError("unofficial index source returned no result")
    result = results[0]
    if not isinstance(result, dict):
        raise RPCError("unofficial index source returned a malformed result")
    meta = result.get("meta")
    if not isinstance(meta, dict) or meta.get("symbol") != symbol:
        raise RPCError("unofficial index source returned the wrong symbol")
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if (not isinstance(timestamps, list) or not 2 <= len(timestamps) <= 10
            or not isinstance(indicators, dict)):
        raise RPCError("unofficial index source returned invalid observations")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or len(quotes) != 1 \
            or not isinstance(quotes[0], dict):
        raise RPCError("unofficial index source returned invalid prices")
    quote = quotes[0]
    fields: dict[str, list[object]] = {}
    for field in ("open", "high", "low", "close", "volume"):
        values = quote.get(field)
        if not isinstance(values, list) or len(values) != len(timestamps):
            raise RPCError("unofficial index source returned mismatched prices")
        fields[field] = values
    normalized: list[dict[str, float | int]] = []
    for offset, raw_timestamp in enumerate(timestamps):
        if fields["close"][offset] is None:
            continue
        timestamp = integer(raw_timestamp, "index timestamp", 946684800,
                            4102444800)
        item: dict[str, float | int] = {"timestamp": timestamp}
        for field in ("open", "high", "low", "close"):
            item[field] = _finite(fields[field][offset], 0.000001,
                                  100_000_000, f"index {field}")
        volume = fields["volume"][offset]
        item["volume"] = (0.0 if volume is None else
                          _finite(volume, 0, 10_000_000_000_000,
                                  "index volume"))
        normalized.append(item)
    if len(normalized) < 2:
        raise RPCError("unofficial index source returned too few prices")
    normalized.sort(key=lambda item: int(item["timestamp"]))
    latest, previous = normalized[-1], normalized[-2]
    previous_close = float(previous["close"])
    change = float(latest["close"]) - previous_close
    return {
        "symbol": symbol, "label": clean_text(label, 80),
        "kind": "UNOFFICIAL",
        "open": round(float(latest["open"]), 4),
        "high": round(float(latest["high"]), 4),
        "low": round(float(latest["low"]), 4),
        "close": round(float(latest["close"]), 4),
        "previous_close": round(previous_close, 4),
        "change": round(change, 4),
        "percent": round(change / previous_close * 100.0, 4),
        "volume": int(float(latest["volume"])),
        "market_date": datetime.fromtimestamp(
            int(latest["timestamp"]), timezone.utc).strftime("%Y-%m-%d"),
    }


class _MovieTableParser(HTMLParser):
    """Extract rendered cells from top-level MediaWiki schedule tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.row: list[dict[str, object]] | None = None
        self.cell: dict[str, object] | None = None
        self.tables: list[list[list[dict[str, object]]]] = []
        self.table: list[list[dict[str, object]]] | None = None

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self.depth == 0:
                self.table = []
            self.depth += 1
        elif self.depth == 1 and tag == "tr":
            self.row = []
        elif self.depth == 1 and tag in ("th", "td") and self.row is not None:
            self.cell = {"text": [], "attrs": dict(attrs)}

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            pieces = self.cell["text"]
            if isinstance(pieces, list):
                pieces.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.depth == 1 and tag in ("th", "td") and self.cell is not None:
            pieces = self.cell["text"]
            text_value = (" ".join(str(part) for part in pieces)
                          if isinstance(pieces, list) else "")
            self.cell["text"] = re.sub(r"\s+", " ", text_value).strip()
            if self.row is not None:
                self.row.append(self.cell)
            self.cell = None
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            if self.table is not None and self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.depth:
            self.depth -= 1
            if self.depth == 0 and self.table is not None:
                self.tables.append(self.table)
                self.table = None


def parse_movie_schedule(document: str, year: int, start: datetime,
                         end: datetime) -> list[dict[str, object]]:
    """Parse a bounded rendered Wikipedia film-calendar section."""
    if len(document) > 700_000:
        raise RPCError("movie schedule is too large")
    parser = _MovieTableParser()
    parser.feed(document)
    results: list[dict[str, object]] = []
    for table in parser.tables:
        if not table:
            continue
        headings = {str(cell.get("text", "")).casefold() for cell in table[0]}
        if "opening" not in headings or "title" not in headings:
            continue
        month: int | None = None
        day: int | None = None
        for row in table[1:]:
            cells = list(row)
            if not cells:
                continue
            attrs = cells[0].get("attrs", {})
            marker = (str(attrs.get("data-sort-value", ""))
                      if isinstance(attrs, dict) else "")
            if re.fullmatch(r"0[1-9]|1[0-2]", marker):
                month = int(marker)
                cells.pop(0)
            if cells and re.fullmatch(r"\d{1,2}", str(cells[0].get("text", ""))):
                day = int(str(cells.pop(0).get("text", "")))
            if month is None or day is None or len(cells) < 2:
                continue
            try:
                release = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                continue
            if release < start or release > end:
                continue
            title = clean_text(cells[0].get("text", ""), 240)
            title = re.sub(r"(?:\s*\[\s*\d+\s*\])+\s*$", "", title).strip(" †")
            if not title:
                continue
            company = clean_text(cells[1].get("text", ""), 300)
            details = (clean_text(cells[2].get("text", ""), 1200)
                       if len(cells) > 2 else "")
            results.append({"title": title, "date": release.strftime("%Y-%m-%d"),
                            "company": company, "details": details})
    return results


def parse_tv_schedule(payload: object) -> list[dict[str, object]]:
    """Normalize new-series and returning-season premieres from TVmaze."""
    if not isinstance(payload, list):
        raise RPCError("TV provider returned invalid data")
    rows: list[dict[str, object]] = []
    for episode in payload[:500]:
        if not isinstance(episode, dict) or episode.get("number") != 1:
            continue
        embedded = episode.get("_embedded", {})
        show = embedded.get("show", {}) if isinstance(embedded, dict) else {}
        if not isinstance(show, dict):
            show = {}
        name = clean_text(show.get("name", ""), 240)
        if not name:
            links = episode.get("_links", {})
            show_link = links.get("show", {}) if isinstance(links, dict) else {}
            name = (clean_text(show_link.get("name", ""), 240)
                    if isinstance(show_link, dict) else "")
        if not name:
            continue
        try:
            season = int(episode.get("season", 0))
        except (TypeError, ValueError):
            continue
        if season < 1 or season > 9999:
            continue
        network = show.get("network") or show.get("webChannel") or {}
        network_name = (clean_text(network.get("name", "TBA"), 120)
                        if isinstance(network, dict) else "TBA")
        genres = show.get("genres", [])
        genre_text = (", ".join(clean_text(item, 40) for item in genres[:5])
                      if isinstance(genres, list) else "")
        identifier = show.get("id", 0)
        rows.append({
            "show": name,
            "date": clean_text(episode.get("airdate", ""), 10),
            "time": clean_text(episode.get("airtime", ""), 8),
            "kind": "NEW SERIES" if season == 1 else "NEW SEASON",
            "season": season,
            "episode": clean_text(episode.get("name", "Episode 1"), 240),
            "network": network_name or "TBA",
            "genres": genre_text,
            "summary": clean_text(show.get("summary", ""), 1600),
            "url": clean_text(show.get("url", ""), 500),
            "id": int(identifier) if str(identifier).isdigit() else 0,
        })
    return rows


def parse_time(value: object) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
            return int(parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            return 0


def parse_feed(raw: bytes, source: str, maximum: int = MAX_RESPONSE,
               allow_full_text: bool = False,
               full_text_field: str = "encoded") -> list[dict[str, object]]:
    if len(raw) > maximum or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise RPCError("unsafe XML feed")
    try:
        document = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RPCError("provider returned invalid XML") from exc
    # RSS/Atom providers are allowlisted, but their bytes remain untrusted.  A
    # small field extractor avoids recursive XML expansion and native Expat
    # parser attack surface.  It intentionally supports only the fields used by
    # this read-only wire service and rejects structurally incomplete feeds.
    blocks = re.findall(r"(?is)<item(?:\s[^>]{0,1000})?>(.*?)</item>", document)
    atom = False
    if not blocks:
        blocks = re.findall(r"(?is)<(?:[A-Za-z0-9_-]+:)?entry(?:\s[^>]{0,1000})?>(.*?)"
                            r"</(?:[A-Za-z0-9_-]+:)?entry>", document)
        atom = True
    if not blocks:
        raise RPCError("provider returned invalid XML")

    def field(block: str, names: tuple[str, ...]) -> str:
        for name in names:
            escaped = re.escape(name)
            match = re.search(
                rf"(?is)<(?:[A-Za-z0-9_-]+:)?{escaped}(?:\s[^>]{{0,1000}})?>(.*?)"
                rf"</(?:[A-Za-z0-9_-]+:)?{escaped}\s*>", block)
            if match:
                value = match.group(1).strip()
                cdata = re.fullmatch(r"(?is)<!\[CDATA\[(.*)\]\]>", value)
                return cdata.group(1) if cdata else value
        return ""

    rows: list[dict[str, object]] = []
    for item in blocks[:40]:
        title = clean_text(field(item, ("title",)), 240)
        summary = clean_text(field(item, ("description", "summary", "content")), 1200)
        author = clean_text(field(item, ("creator", "author")), 160)
        body = ""
        article_type = "SUMMARY"
        if allow_full_text:
            # WordPress and many other RSS publishers place the publisher-
            # supplied article in content:encoded.  It is deliberately read
            # only for sources whose reuse policy has been reviewed.
            fields = {
                "encoded": ("encoded",),
                "description": ("encoded", "description"),
                "content": ("encoded", "content"),
            }.get(full_text_field)
            if fields is None:
                raise RPCError("invalid full-text feed field")
            supplied = field(item, fields)
            if supplied:
                body = clean_text(supplied, MAX_ARTICLE_TEXT + 1)
                if len(body) > MAX_ARTICLE_TEXT:
                    body = body[:MAX_ARTICLE_TEXT].rstrip()
                    article_type = "EXCERPT"
                elif body:
                    article_type = "FULL TEXT"
        published = parse_time(field(item, ("pubDate", "published", "updated")))
        link = field(item, ("link",))
        if atom and not link:
            match = re.search(r"(?is)<(?:[A-Za-z0-9_-]+:)?link\s+[^>]{0,1000}"
                              r"href\s*=\s*(['\"])(.*?)\1", item)
            if match:
                link = match.group(2)
        if title:
            rows.append({"title": title, "summary": summary or "No summary supplied.",
                         "source": source, "published": published,
                         "type": article_type, "link": clean_text(link, 500),
                         "author": author, "body": body})
    return rows


def prepare_open_article(row: dict[str, object], mode: str,
                         body: str) -> tuple[str, str, str]:
    """Attach source-required credit and reject non-sublicensed partner text."""
    if not body:
        return "", str(row.get("type", "SUMMARY")), ""
    author = clean_text(row.get("author", ""), 160) or "Global Voices contributors"
    link = clean_text(row.get("link", ""), 500)
    if mode == "global-voices-full":
        folded = body.casefold()
        # Global Voices' CC BY policy covers content it creates. Some feed
        # entries reproduce partner work under a bilateral sharing agreement;
        # that permission is not assumed to be sublicensable to this BBS.
        partner_markers = (
            "content-sharing agreement",
            "content sharing agreement",
            "republished with permission",
        )
        if any(marker in folded for marker in partner_markers):
            return "", "SUMMARY", "Partner contribution; summary only"
        notice = (
            f"This story by {author} originally appeared on Global Voices.\n"
            f"Original: {link}\n"
            "License: Creative Commons Attribution 3.0 (CC BY 3.0).\n"
            "Terminal edition: HTML layout and media omitted; text was not "
            "editorially rewritten.\n\n"
        )
        return notice + body, str(row.get("type", "FULL TEXT")), "CC BY 3.0"
    if mode == "eff-full":
        notice = (
            f"By {author}. Originally published by the Electronic Frontier "
            "Foundation.\nOriginal: " + link + "\n"
            "License: Creative Commons Attribution 4.0 (CC BY 4.0).\n"
            "Terminal edition: HTML layout and media omitted; text was not "
            "editorially rewritten.\n\n"
        )
        return notice + body, str(row.get("type", "FULL TEXT")), "CC BY 4.0"
    return body, str(row.get("type", "FULL TEXT")), ""


def extract_cisa_article(raw: bytes) -> str:
    """Extract the official advisory body from a bounded CISA HTML page."""
    if len(raw) > 2_000_000:
        raise RPCError("CISA article is too large")
    try:
        document = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RPCError("CISA returned invalid article text") from exc
    start = re.search(
        r'(?is)<div\s+class=["\'][^"\']*l-page-section--rich-text[^"\']*["\'][^>]*>',
        document)
    if not start:
        raise RPCError("CISA article body was not found")
    tail = document[start.end():]
    end = re.search(
        r'(?is)<div\s+class=["\'][^"\']*l-constrain\s+l-page-section--rich-text'
        r'[^"\']*["\'][^>]*>|<div\s+class=["\'][^"\']*l-full__footer', tail)
    if end:
        tail = tail[:end.start()]
    body = clean_text(tail, MAX_ARTICLE_TEXT + 1)
    if not body:
        raise RPCError("CISA article body was empty")
    return body


def weather_description(code: object) -> str:
    descriptions = {
        0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle",
        55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Heavy freezing drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
        67: "Heavy freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
        77: "Snow grains", 80: "Light showers", 81: "Showers", 82: "Heavy showers",
        85: "Snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
        96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
    }
    try:
        return descriptions.get(int(code), "Unknown")
    except (TypeError, ValueError):
        return "Unknown"


def compass(degrees: object) -> str:
    try:
        points = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        return points[int((float(degrees) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return "?"


def _json_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _json_ascii_clip(value: object, maximum: int) -> str:
    """Bound the bytes a string consumes in an ensure_ascii JSON envelope."""
    result: list[str] = []
    used = 0
    for character in str(value):
        fragment = json.dumps(character, ensure_ascii=True)[1:-1]
        size = len(fragment.encode("ascii"))
        if used + size > maximum:
            break
        result.append(character)
        used += size
    return "".join(result).rstrip()


def _finite(value: object, minimum: float, maximum: float, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RPCError(f"invalid {name}") from exc
    if not math.isfinite(result) or result < minimum or result > maximum:
        raise RPCError(f"invalid {name}")
    return result


class Information:
    def __init__(self, db_path: str, session_socket: str, online: bool = True,
                 massive_key: str = ""):
        self.db_path = db_path
        self.local = threading.local()
        self.article_lock = threading.RLock()
        self._db().executescript(SCHEMA)
        self.session_socket = session_socket
        self.online = online
        self.massive_key = massive_key
        self.market_lock = threading.Lock()
        self.work: queue.PriorityQueue[tuple[int, int, str, object]] = \
            queue.PriorityQueue(maxsize=256)
        self.pending: set[str] = set()
        self.lock = threading.RLock()
        self.sequence = 0
        if online:
            threading.Thread(target=self._scheduler, name="scheduler", daemon=True).start()
            for number in range(2):
                threading.Thread(target=self._collector, name=f"collector-{number + 1}",
                                 daemon=True).start()

    def _db(self) -> sqlite3.Connection:
        """Return a durable connection owned by the current thread."""
        database = getattr(self.local, "database", None)
        if database is None:
            database = connect(self.db_path)
            self.local.database = database
        return database

    def _enqueue(self, kind: str, value: object = None, priority: int = 0) -> None:
        key = f"{kind}:{value!r}"
        with self.lock:
            if key in self.pending:
                return
            try:
                self.sequence += 1
                self.work.put_nowait((priority, self.sequence, kind, value))
                self.pending.add(key)
            except queue.Full:
                return

    def _health(self, source: str, error: str = "") -> None:
        now = int(time.time())
        database = self._db()
        with database:
            if error:
                database.execute(
                    "INSERT INTO source_health(source,last_attempt,last_error,failures) "
                    "VALUES(?,?,?,1) ON CONFLICT(source) DO UPDATE SET "
                    "last_attempt=excluded.last_attempt,last_error=excluded.last_error,"
                    "failures=source_health.failures+1", (source, now, clean_text(error, 300)))
            else:
                database.execute(
                    "INSERT INTO source_health(source,last_attempt,last_success,last_error,failures) "
                    "VALUES(?,?,?,'',0) ON CONFLICT(source) DO UPDATE SET "
                    "last_attempt=excluded.last_attempt,last_success=excluded.last_success,"
                    "last_error='',failures=0", (source, now, now))

    def _put(self, key: str, payload: object, source: str, ttl: int,
             maximum: int = 48_000) -> None:
        encoded = _json_text(payload)
        # Cached items are returned inside a 64 KiB RPC envelope.  Leave room
        # for metadata and JSON escaping instead of discovering oversize only
        # when the service writes its response.
        if len(encoded.encode("ascii")) > maximum:
            raise RPCError("normalized provider data is too large")
        now = int(time.time())
        database = self._db()
        with self.article_lock, database:
            database.execute(
                "INSERT INTO cache(cache_key,payload,source,fetched_at,expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload=excluded.payload,source=excluded.source,"
                "fetched_at=excluded.fetched_at,expires_at=excluded.expires_at",
                (key, encoded, source, now, now + ttl))
            database.execute("DELETE FROM article_leases WHERE expires_at<?", (now,))
            total = int(database.execute(
                "SELECT coalesce(sum(length(payload)),0) FROM cache").fetchone()[0])
            while total > MAX_CACHE_BYTES:
                oldest = database.execute(
                    "SELECT c.cache_key,length(c.payload) FROM cache c "
                    "WHERE c.cache_key NOT LIKE 'article:%' OR NOT EXISTS ("
                    "SELECT 1 FROM article_leases l "
                    "WHERE l.article_id=substr(c.cache_key,9) AND l.expires_at>=?) "
                    "ORDER BY c.fetched_at,c.cache_key LIMIT 1", (now,)).fetchone()
                if oldest is None:
                    break
                database.execute("DELETE FROM cache WHERE cache_key=?", (oldest[0],))
                total -= int(oldest[1])
        self._health(source)

    def _article_is_leased(self, article_id: str) -> bool:
        now = int(time.time())
        database = self._db()
        database.execute("DELETE FROM article_leases WHERE expires_at<?", (now,))
        return database.execute(
            "SELECT 1 FROM article_leases WHERE article_id=? AND expires_at>=? LIMIT 1",
            (article_id, now)).fetchone() is not None

    def _lease_article(self, article_id: str, session_key: str) -> None:
        expires = int(time.time()) + ARTICLE_LEASE_SECONDS
        database = self._db()
        with self.article_lock, database:
            database.execute(
                "INSERT INTO article_leases(article_id,session_key,expires_at) "
                "VALUES(?,?,?) ON CONFLICT(article_id,session_key) DO UPDATE SET "
                "expires_at=excluded.expires_at", (article_id, session_key, expires))

    def _release_article(self, article_id: str, session_key: str) -> None:
        database = self._db()
        with self.article_lock, database:
            database.execute(
                "DELETE FROM article_leases WHERE article_id=? AND session_key=?",
                (article_id, session_key))

    def _store_article(self, article_id: str, payload: dict[str, object],
                       source: str) -> dict[str, object]:
        """Refresh an article unless a reader is transferring that version."""
        with self.article_lock:
            cached = self._get(f"article:{article_id}")
            if cached is not None and self._article_is_leased(article_id):
                return dict(cached["data"])
            self._put(f"article:{article_id}", payload, source, 3600,
                      maximum=1_000_000)
            return payload

    def _prune_articles(self) -> int:
        """Keep only current-wire articles, except active transfers."""
        database = self._db()
        with self.article_lock, database:
            now = int(time.time())
            database.execute("DELETE FROM article_leases WHERE expires_at<?", (now,))
            current: set[str] = set()
            for row in database.execute(
                    "SELECT payload FROM cache WHERE cache_key LIKE 'news:%'"):
                try:
                    items = json.loads(row[0])
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            article_id = str(item.get("article_id", ""))
                            if re.fullmatch(r"[0-9a-f]{32}", article_id):
                                current.add(article_id)
            removed = 0
            for row in database.execute(
                    "SELECT cache_key FROM cache WHERE cache_key LIKE 'article:%'"):
                article_id = str(row[0])[8:]
                if article_id in current or self._article_is_leased(article_id):
                    continue
                database.execute("DELETE FROM cache WHERE cache_key=?", (row[0],))
                removed += 1
            return removed

    def _get(self, key: str) -> dict[str, object] | None:
        row = self._db().execute("SELECT * FROM cache WHERE cache_key=?", (key,)).fetchone()
        if row is None:
            return None
        return {"data": json.loads(row["payload"]), "source": row["source"],
                "fetched_at": row["fetched_at"], "expires_at": row["expires_at"],
                "stale": int(row["expires_at"]) < int(time.time())}

    def _scheduler(self) -> None:
        next_global = {key: 0.0 for key in GLOBAL_INTERVALS}
        next_weather = 0.0
        while True:
            now = time.monotonic()
            for key, interval in GLOBAL_INTERVALS.items():
                if now >= next_global[key]:
                    self._enqueue(key, priority=10)
                    next_global[key] = now + interval
            # Weather is refreshed only for saved locations.
            if now >= next_weather:
                for row in self._db().execute(
                        "SELECT DISTINCT latitude,longitude,location_name,timezone FROM preferences"):
                    self._enqueue("weather", dict(row), priority=10)
                next_weather = now + 300
            time.sleep(1.0)

    def _collector(self) -> None:
        while True:
            try:
                _priority, _sequence, kind, value = self.work.get(timeout=1.0)
            except queue.Empty:
                continue
            key = f"{kind}:{value!r}"
            try:
                self._run_task(kind, value)
            except Exception as exc:  # Keep last-known-good data.
                self._health(kind.split(":", 1)[0], str(exc))
                failures = self._db().execute(
                    "SELECT failures FROM source_health WHERE source=?",
                    (kind.split(":", 1)[0],)).fetchone()
                count = int(failures[0]) if failures else 1
                time.sleep(min(30.0, (2 ** min(count, 5)) + random.random()))
            finally:
                with self.lock:
                    self.pending.discard(key)
                self.work.task_done()

    def _run_task(self, kind: str, value: object) -> None:
        if kind.startswith("news:"):
            self._collect_news(kind.split(":", 1)[1])
        elif kind == "solar":
            self._collect_solar()
        elif kind == "earthquakes":
            self._collect_earthquakes()
        elif kind == "iss":
            self._collect_iss()
        elif kind == "movies":
            self._collect_movies()
        elif kind == "television":
            self._collect_television()
        elif kind == "markets":
            self._collect_markets()
        elif kind == "market_quote":
            self._collect_market_quote(str(value))
        elif kind == "search":
            self._collect_search(str(value))
        elif kind == "weather" and isinstance(value, dict):
            self._collect_weather(value)
        else:
            raise RPCError("unknown collection task")

    def _market_series(self, symbol: str, label: str,
                       kind: str) -> dict[str, object]:
        if not self.massive_key:
            raise RPCError("Massive market data is not configured")
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=12)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        encoded_symbol = symbol.replace(":", "%3A")
        url = (f"https://api.massive.com/v2/aggs/ticker/{encoded_symbol}/range/"
               f"1/day/{start}/{end}?adjusted=true&sort=asc&limit=12")
        # Serialize provider calls so concurrent lookups cannot create an
        # uncontrolled burst. Massive enforces the Basic-plan rate limit.
        with self.market_lock:
            payload = fetch_bearer_json(url, self.massive_key, 128_000)
        return normalize_market_series(payload, symbol, label, kind)

    def _unofficial_index_series(self, symbol: str,
                                 label: str) -> dict[str, object]:
        encoded_symbol = symbol.replace("^", "%5E")
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{encoded_symbol}?interval=1d&range=5d")
        with self.market_lock:
            payload = fetch_json(url, 64_000)
        return normalize_unofficial_index_chart(payload, symbol, label)

    def _collect_markets(self) -> None:
        if not self.massive_key:
            raise RPCError("Massive market data is not configured")
        rows: list[dict[str, object]] = []
        errors: list[str] = []
        unofficial_count = 0
        for symbol, label in UNOFFICIAL_INDEX_BENCHMARKS:
            try:
                rows.append(self._unofficial_index_series(symbol, label))
                unofficial_count += 1
            except Exception as exc:
                errors.append(f"{symbol}: {clean_text(exc, 120)}")
                fallback = MARKET_PROXY_FALLBACKS[symbol]
                try:
                    rows.append(self._market_series(*fallback))
                except Exception as fallback_exc:
                    errors.append(
                        f"{fallback[0]}: {clean_text(fallback_exc, 120)}")
        for symbol, label, kind in MARKET_BENCHMARKS:
            if symbol in {"DIA", "SPY"}:
                continue
            try:
                rows.append(self._market_series(symbol, label, kind))
            except Exception as exc:
                errors.append(f"{symbol}: {clean_text(exc, 120)}")
        if not rows:
            raise RPCError("; ".join(errors) or "no market prices available")
        source = "Massive Basic end-of-day market data"
        if unofficial_count:
            source = "Yahoo Finance chart (unofficial) + " + source
        self._put("markets", {
            "delay": "BEST EFFORT / REFRESHED HOURLY", "benchmarks": rows,
            "notice": ("Unofficial Dow and S&P values may be delayed, "
                       "inaccurate, or unavailable. ETF proxies appear when "
                       "the unofficial source fails."),
            "partial_errors": errors[:6],
        }, source, 7200)

    def _collect_market_quote(self, symbol: str) -> None:
        try:
            quote = self._market_series(symbol, symbol, "STOCK OR ETF")
            quote["available"] = True
            ttl = 7200
        except Exception as exc:
            quote = {"available": False, "symbol": symbol,
                     "error": clean_text(exc, 160)}
            ttl = 300
        self._put(f"market:{symbol}", quote,
                  "Massive Basic end-of-day market data", ttl)

    def _collect_movies(self) -> None:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=14)
        end = now + timedelta(days=MOVIE_WINDOW_DAYS)
        sections: set[tuple[int, int]] = set()
        cursor = start
        while cursor <= end:
            sections.add((cursor.year, ((cursor.month - 1) // 3) + 2))
            cursor += timedelta(days=31)
        sections.add((end.year, ((end.month - 1) // 3) + 2))
        rows: list[dict[str, object]] = []
        revisions: list[str] = []
        errors: list[str] = []
        for year, section in sorted(sections):
            page = f"List of American films of {year}"
            url = "https://en.wikipedia.org/w/api.php?" + urlencode({
                "action": "parse", "page": page, "section": str(section),
                "prop": "text|revid", "format": "json", "formatversion": "2",
            })
            try:
                payload = fetch_json(url, 700_000)
                parsed = payload.get("parse", {}) if isinstance(payload, dict) else {}
                document = parsed.get("text", "") if isinstance(parsed, dict) else ""
                revision = parsed.get("revid", "") if isinstance(parsed, dict) else ""
                if not document:
                    raise RPCError("movie calendar section was unavailable")
                rows.extend(parse_movie_schedule(str(document), year, start, end))
                revisions.append(f"{year} rev {revision}")
            except Exception as exc:
                errors.append(f"{year} section {section}: {exc}")
        if not rows:
            raise RPCError("; ".join(errors) or "no movie releases")
        unique: dict[tuple[str, str], dict[str, object]] = {}
        for row in rows:
            unique[(str(row["date"]), str(row["title"]).casefold())] = row
        ordered = sorted(unique.values(), key=lambda item: (
            str(item["date"]), str(item["title"])))
        result: list[dict[str, object]] = []
        for item in ordered:
            candidate = {
                "title": _json_ascii_clip(item.get("title", ""), 500),
                "date": item.get("date", ""),
                "company": _json_ascii_clip(item.get("company", ""), 500),
                "details": _json_ascii_clip(item.get("details", ""), 700),
            }
            trial = {"items": result + [candidate], "revisions": revisions,
                     "window_end": end.strftime("%Y-%m-%d")}
            if len(_json_text(trial).encode("ascii")) > 44_000:
                break
            result.append(candidate)
        self._put(
            "entertainment:movies",
            {"items": result, "revisions": revisions,
             "window_end": end.strftime("%Y-%m-%d")},
            "Wikipedia contributors — List of American films (CC BY-SA 4.0)",
            7200)

    def _collect_television(self) -> None:
        today = datetime.now(timezone.utc).date()
        rows: list[dict[str, object]] = []
        errors: list[str] = []
        consecutive_errors = 0
        for offset in range(TV_WINDOW_DAYS):
            date_value = (today + timedelta(days=offset)).isoformat()
            for path in (f"/schedule?country=US&date={date_value}",
                         f"/schedule/web?country=&date={date_value}"):
                try:
                    rows.extend(parse_tv_schedule(fetch_json(
                        "https://api.tvmaze.com" + path, 1_000_000)))
                    consecutive_errors = 0
                except Exception as exc:
                    errors.append(f"{date_value}: {exc}")
                    consecutive_errors += 1
                    if consecutive_errors >= 4:
                        break
                time.sleep(0.55)  # Respect the documented public rate limit.
            if consecutive_errors >= 4:
                break
        if not rows:
            raise RPCError("; ".join(errors[-4:]) or "no television premieres")
        unique: dict[tuple[int, int, str], dict[str, object]] = {}
        for row in rows:
            marker = (int(row.get("id", 0)), int(row["season"]), str(row["date"]))
            unique[marker] = row
        ordered = sorted(unique.values(), key=lambda item: (
            str(item["date"]), str(item.get("time", "")), str(item["show"])))
        result: list[dict[str, object]] = []
        for item in ordered:
            candidate = dict(item)
            for field, maximum in (("show", 500), ("episode", 500),
                                   ("network", 250), ("genres", 250),
                                   ("summary", 900), ("url", 700)):
                candidate[field] = _json_ascii_clip(candidate.get(field, ""), maximum)
            trial = {"items": result + [candidate],
                     "window_end": (today + timedelta(days=TV_WINDOW_DAYS - 1)).isoformat()}
            if len(_json_text(trial).encode("ascii")) > 44_000:
                break
            result.append(candidate)
        self._put(
            "entertainment:television",
            {"items": result,
             "window_end": (today + timedelta(days=TV_WINDOW_DAYS - 1)).isoformat()},
            "TVmaze public API (CC BY-SA)", 7200)

    def _collect_news(self, category: str) -> None:
        rows: list[dict[str, object]] = []
        errors: list[str] = []
        modes: dict[str, str] = {}
        for source, url, mode in NEWS_FEEDS[category]:
            try:
                limit = 2_000_000 if url.startswith("https://www.cisa.gov/") else MAX_RESPONSE
                full_field = "description" if mode == "eff-full" else "encoded"
                rows.extend(parse_feed(fetch(
                    url, "application/rss+xml, application/atom+xml, text/xml", limit),
                    source, limit,
                    allow_full_text=mode in {
                        "feed-full", "global-voices-full", "eff-full"},
                    full_text_field=full_field))
                modes[source] = mode
            except Exception as exc:
                errors.append(f"{source}: {exc}")
        if not rows:
            raise RPCError("; ".join(errors) or "no news items")
        seen: set[tuple[str, str]] = set()
        unique = []
        for row in sorted(rows, key=lambda item: int(item["published"]), reverse=True):
            marker = (str(row["source"]), str(row["title"]).casefold())
            if marker not in seen:
                seen.add(marker)
                unique.append(row)
        page_fetch_budget = 10
        index_rows: list[dict[str, object]] = []
        for row in unique[:20]:
            body = str(row.pop("body", ""))
            mode = modes.get(str(row["source"]), "summary")
            if body and mode in {"global-voices-full", "eff-full"}:
                body, article_type, rights = prepare_open_article(row, mode, body)
                row["type"] = article_type
                if rights:
                    row["rights"] = rights
            article_id = hashlib.sha256(
                (str(row["source"]) + "\n" + str(row.get("link", ""))).encode("utf-8")
            ).hexdigest()[:32]
            cached = self._get(f"article:{article_id}")
            if body:
                payload = {"title": row["title"], "link": row["link"],
                           "type": row["type"], "body": body}
                stored = self._store_article(article_id, payload, str(row["source"]))
                row["type"] = stored.get("type", row["type"])
                row["article_id"] = article_id
            elif row.get("rights") == "Partner contribution; summary only":
                # A previous refresh may have cached this URL before its
                # partner-rights notice was recognized. Never revive it.
                pass
            elif (mode == "cisa-page" and page_fetch_budget
                  and not (cached and self._article_is_leased(article_id))):
                link = str(row.get("link", ""))
                parsed = urlsplit(link)
                if (parsed.scheme == "https" and parsed.hostname == "www.cisa.gov"
                        and not parsed.username and not parsed.password
                        and parsed.port in (None, 443)
                        and parsed.path.startswith("/news-events/")):
                    try:
                        body = extract_cisa_article(fetch(
                            link, "text/html", 2_000_000))
                        article_type = "FULL TEXT"
                        if len(body) > MAX_ARTICLE_TEXT:
                            body = body[:MAX_ARTICLE_TEXT].rstrip()
                            article_type = "EXCERPT"
                        stored = self._store_article(
                            article_id,
                            {"title": row["title"], "link": link,
                             "type": article_type, "body": body},
                            str(row["source"]))
                        row["type"] = stored.get("type", article_type)
                        row["article_id"] = article_id
                    except Exception as exc:
                        errors.append(f"{row['source']} article: {exc}")
                    page_fetch_budget -= 1
            elif cached:
                cached_data = dict(cached["data"])
                row["type"] = cached_data.get("type", row["type"])
                row["article_id"] = article_id
            # Complete bodies live in separately chunked article records. The
            # headline index stays below the RPC envelope even when providers
            # use Unicode-heavy titles, authors, or summaries.
            row["title"] = _json_ascii_clip(row.get("title", ""), 500)
            row["summary"] = _json_ascii_clip(row.get("summary", ""), 700)
            row["author"] = _json_ascii_clip(row.get("author", ""), 250)
            row["link"] = _json_ascii_clip(row.get("link", ""), 700)
            index_rows.append(row)
        self._put(f"news:{category}", index_rows,
                  ", ".join(s for s, _, _ in NEWS_FEEDS[category]), 1800)
        self._prune_articles()

    def _collect_search(self, query: str) -> None:
        url = "https://geocoding-api.open-meteo.com/v1/search?" + urlencode({
            "name": query, "count": 10, "language": "en", "format": "json"})
        obj = fetch_json(url, 256_000)
        rows = []
        for item in obj.get("results", [])[:10] if isinstance(obj, dict) else []:
            lat = _finite(item.get("latitude"), -90, 90, "latitude")
            lon = _finite(item.get("longitude"), -180, 180, "longitude")
            parts = [clean_text(item.get("name", ""), 80),
                     clean_text(item.get("admin1", ""), 80),
                     clean_text(item.get("country", ""), 80)]
            name = ", ".join(part for part in parts if part)
            rows.append({"name": name, "latitude": lat, "longitude": lon,
                         "timezone": clean_text(item.get("timezone", "UTC"), 64)})
        now = int(time.time())
        database = self._db()
        with database:
            database.execute(
                "INSERT INTO searches(query,payload,fetched_at,expires_at) VALUES(?,?,?,?) "
                "ON CONFLICT(query) DO UPDATE SET payload=excluded.payload,"
                "fetched_at=excluded.fetched_at,expires_at=excluded.expires_at",
                (query, _json_text(rows), now, now + 86400))
        self._health("geocoding")

    def _weather_key(self, lat: float, lon: float) -> str:
        return f"weather:{lat:.4f}:{lon:.4f}"

    def _collect_weather(self, location: dict[str, object]) -> None:
        lat = _finite(location.get("latitude"), -90, 90, "latitude")
        lon = _finite(location.get("longitude"), -180, 180, "longitude")
        params = {
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m",
            "hourly": "temperature_2m,weather_code,precipitation_probability",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "auto", "forecast_days": "7",
        }
        forecast = fetch_json("https://api.open-meteo.com/v1/forecast?" + urlencode(params))
        alert_url = "https://api.weather.gov/alerts/active?" + urlencode({"point": f"{lat:.4f},{lon:.4f}"})
        alerts_obj = fetch_json(alert_url, 512_000)
        current = forecast.get("current", {})
        daily = forecast.get("daily", {})
        hourly = forecast.get("hourly", {})
        result = {
            "location": clean_text(location.get("location_name", "Selected location"), 160),
            "timezone": clean_text(forecast.get("timezone", location.get("timezone", "UTC")), 64),
            "current": {
                "time": current.get("time", ""), "temperature": current.get("temperature_2m"),
                "feels_like": current.get("apparent_temperature"),
                "humidity": current.get("relative_humidity_2m"),
                "wind_speed": current.get("wind_speed_10m"),
                "wind_direction": compass(current.get("wind_direction_10m")),
                "conditions": weather_description(current.get("weather_code")),
            },
            "daily": [], "hourly": [], "alerts": [],
        }
        for index, day in enumerate(daily.get("time", [])[:7]):
            result["daily"].append({
                "time": day, "high": daily.get("temperature_2m_max", [None] * 7)[index],
                "low": daily.get("temperature_2m_min", [None] * 7)[index],
                "precipitation": daily.get("precipitation_probability_max", [None] * 7)[index],
                "conditions": weather_description(daily.get("weather_code", [None] * 7)[index]),
            })
        now_text = str(current.get("time", ""))
        start = next((i for i, item in enumerate(hourly.get("time", [])) if str(item) >= now_text), 0)
        for index in range(start, min(start + 24, len(hourly.get("time", [])))):
            result["hourly"].append({
                "time": hourly["time"][index], "temperature": hourly["temperature_2m"][index],
                "precipitation": hourly["precipitation_probability"][index],
                "conditions": weather_description(hourly["weather_code"][index]),
            })
        for feature in alerts_obj.get("features", [])[:20] if isinstance(alerts_obj, dict) else []:
            prop = feature.get("properties", {})
            result["alerts"].append({
                "event": clean_text(prop.get("event", "Weather alert"), 160),
                "severity": clean_text(prop.get("severity", "Unknown"), 40),
                "headline": clean_text(prop.get("headline", ""), 400),
                "description": clean_text(prop.get("description", ""), 5000),
                "instruction": clean_text(prop.get("instruction", ""), 3000),
                "expires": parse_time(prop.get("expires")),
            })
        self._put(self._weather_key(lat, lon), result, "Open-Meteo; NOAA/NWS", 1200)

    def _collect_solar(self) -> None:
        kp = fetch_json("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json", 128_000)
        kp_forecast = fetch_json(
            "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json",
            128_000)
        flux = fetch_json("https://services.swpc.noaa.gov/products/10cm-flux-30-day.json", 128_000)
        flux_forecast = fetch_json(
            "https://services.swpc.noaa.gov/json/45-day-forecast.json", 128_000)
        scales = fetch_json("https://services.swpc.noaa.gov/products/noaa-scales.json", 128_000)
        alerts = fetch_json("https://services.swpc.noaa.gov/products/alerts.json", 512_000)
        latest_kp = kp[-1] if isinstance(kp, list) and kp else {}
        latest_flux = flux[-1] if isinstance(flux, list) and flux else {}
        today = scales.get("0", {}) if isinstance(scales, dict) else {}
        now = datetime.now(timezone.utc)
        forecast_end = now + timedelta(hours=24)
        future_kp = []
        for item in kp_forecast if isinstance(kp_forecast, list) else []:
            try:
                stamp = datetime.fromisoformat(
                    str(item.get("time_tag", "")).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                if (now <= stamp <= forecast_end
                        and item.get("observed") != "observed"):
                    future_kp.append(_number(item.get("kp")))
            except (AttributeError, TypeError, ValueError):
                continue
        daily_flux = []
        if isinstance(flux_forecast, dict):
            for item in flux_forecast.get("data", []):
                if isinstance(item, dict) and item.get("metric") == "f107":
                    daily_flux.append(item)
        forecast_flux = daily_flux[0].get("value") if daily_flux else None
        result = {
            "kp": latest_kp.get("Kp"), "kp_time": latest_kp.get("time_tag", ""),
            "flux": latest_flux.get("flux"), "flux_time": latest_flux.get("time_tag", ""),
            "geomagnetic": today.get("G", {}).get("Text", "unknown"),
            "radio_blackout": today.get("R", {}).get("Text", "unknown"),
            "solar_radiation": today.get("S", {}).get("Text", "unknown"),
            "radio_blackout_scale": today.get("R", {}).get("Scale") or 0,
            "solar_radiation_scale": today.get("S", {}).get("Scale") or 0,
            "forecast_kp_24h": max(future_kp) if future_kp else None,
            "forecast_flux": forecast_flux,
            "forecast_issued": (flux_forecast.get("issued", "")
                                if isinstance(flux_forecast, dict) else ""),
            "alerts": [{"issued": clean_text(item.get("issue_datetime", ""), 80),
                        "message": clean_text(item.get("message", ""), 6000)}
                       for item in alerts[:12] if isinstance(item, dict)],
        }
        self._put("solar", result, "NOAA Space Weather Prediction Center", 600)

    def _collect_earthquakes(self) -> None:
        feeds = {
            "significant": "significant_week.geojson",
            "day": "all_day.geojson",
            "week": "2.5_week.geojson",
        }
        result: dict[str, object] = {}
        for name, filename in feeds.items():
            obj = fetch_json("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/" + filename)
            rows = []
            # A terminal page does not need hundreds of events at once, and
            # the authenticated RPC response must remain below 64 KiB.
            for feature in obj.get("features", [])[:40]:
                prop = feature.get("properties", {})
                coordinates = feature.get("geometry", {}).get("coordinates", [None, None, None])
                rows.append({"id": clean_text(feature.get("id", ""), 80),
                             "magnitude": prop.get("mag"),
                             "place": clean_text(prop.get("place", "Unknown location"), 240),
                             "time": int(prop.get("time", 0)) // 1000,
                             "updated": int(prop.get("updated", 0)) // 1000,
                             "depth_km": coordinates[2] if len(coordinates) > 2 else None,
                             "latitude": coordinates[1] if len(coordinates) > 1 else None,
                             "longitude": coordinates[0] if coordinates else None,
                             "url": clean_text(prop.get("url", ""), 500)})
            result[name] = rows
        self._put("earthquakes", result, "U.S. Geological Survey", 600)

    def _collect_iss(self) -> None:
        obj = fetch_json("https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=JSON", 64_000)
        if not isinstance(obj, list) or not obj:
            raise RPCError("CelesTrak returned no ISS elements")
        self._put("iss", obj[0], "CelesTrak (NORAD 25544 GP data)", 10800)

    def _cached_or_queue(self, key: str, task: str,
                         value: object = None) -> dict[str, object]:
        row = self._get(key)
        if row is None or bool(row["stale"]):
            self._enqueue(task, value)
        if row is None:
            raise RPCError("data refresh pending; try again shortly")
        return row

    def _location(self, account_id: int) -> sqlite3.Row | None:
        return self._db().execute(
            "SELECT * FROM preferences WHERE account_id=?", (account_id,)).fetchone()

    def handle(self, req: dict[str, object], peer: tuple[int, int, int]) -> dict[str, object]:
        op = req["op"]
        if op == "health":
            self._db().execute("SELECT 1").fetchone()
            return {"status": "ok", "collector": "online" if self.online else "fixture"}
        ident = validate(self.session_socket, req.get("token"), peer[1])
        data = req["data"]
        if not isinstance(data, dict):
            raise RPCError("invalid data")
        account_id = int(ident["account_id"])
        if op == "status":
            if not ident.get("is_admin"):
                raise RPCError("administrator capability required")
            rows = self._db().execute(
                "SELECT * FROM source_health ORDER BY source").fetchall()
            return {"sources": [dict(row) for row in rows]}
        if op == "location_get":
            row = self._location(account_id)
            return {"location": dict(row) if row else None}
        if op == "location_search":
            query = text(data.get("query", ""), "location", 80, 2).strip()
            row = self._db().execute(
                "SELECT * FROM searches WHERE query=?", (query,)).fetchone()
            if row is None or int(row["expires_at"]) < int(time.time()):
                self._enqueue("search", query)
            return {"results": json.loads(row["payload"]) if row else [],
                    "pending": row is None}
        if op == "location_set":
            query = text(data.get("query", ""), "location", 80, 2).strip()
            index = integer(data.get("index"), "location selection", 0, 9)
            row = self._db().execute(
                "SELECT payload FROM searches WHERE query=?", (query,)).fetchone()
            if row is None:
                raise RPCError("location search expired; search again")
            choices = json.loads(row["payload"])
            if index >= len(choices):
                raise RPCError("invalid location selection")
            selected = choices[index]
            database = self._db()
            with database:
                database.execute(
                    "INSERT INTO preferences(account_id,location_name,latitude,longitude,timezone,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                    "location_name=excluded.location_name,latitude=excluded.latitude,"
                    "longitude=excluded.longitude,timezone=excluded.timezone,updated_at=excluded.updated_at",
                    (account_id, selected["name"], selected["latitude"], selected["longitude"],
                     selected["timezone"], int(time.time())))
            selected["location_name"] = selected["name"]
            self._enqueue("weather", selected)
            return {"location": selected}
        if op == "weather":
            location = self._location(account_id)
            if location is None:
                return {"location": None, "cache": None}
            return {"location": dict(location), "cache": self._cached_or_queue(
                self._weather_key(float(location["latitude"]), float(location["longitude"])),
                "weather", dict(location))}
        if op == "news":
            category = text(data.get("category"), "category", 32, 1)
            if category not in NEWS_FEEDS:
                raise RPCError("unknown news category")
            return {"category": category, "cache": self._cached_or_queue(
                f"news:{category}", f"news:{category}")}
        if op == "article":
            category = text(data.get("category"), "category", 32, 1)
            if category not in NEWS_FEEDS:
                raise RPCError("unknown news category")
            article_id = text(data.get("article_id"), "article identifier", 32, 32)
            if not re.fullmatch(r"[0-9a-f]{32}", article_id):
                raise RPCError("invalid article identifier")
            offset = integer(data.get("offset", 0), "article offset", 0,
                             MAX_ARTICLE_TEXT)
            session_key = hashlib.sha256(
                str(req.get("token", "")).encode("utf-8")).hexdigest()[:32]
            self._lease_article(article_id, session_key)
            try:
                with self.article_lock:
                    cached = self._get(f"article:{article_id}")
                    if cached is None:
                        self._enqueue(f"news:{category}")
                        raise RPCError("article pending refresh")
                    payload = dict(cached["data"])
                    body = str(payload.get("body", ""))
                    if offset > len(body):
                        raise RPCError("invalid article offset")
                    end = min(offset + ARTICLE_CHUNK, len(body))
                    result = {"article_id": article_id,
                              "title": payload.get("title", ""),
                              "type": payload.get("type", "FULL TEXT"),
                              "body": body[offset:end], "next_offset": end,
                              "complete": end == len(body),
                              "source": cached["source"],
                              "fetched_at": cached["fetched_at"],
                              "stale": cached["stale"]}
                if result["complete"]:
                    self._release_article(article_id, session_key)
                return result
            except Exception:
                self._release_article(article_id, session_key)
                raise
        if op == "solar":
            cache = self._cached_or_queue("solar", "solar")
            location = self._location(account_id)
            return {"cache": cache,
                    "location": dict(location) if location else None,
                    "outlook": (local_hf_outlook(dict(cache["data"]), dict(location))
                                if location else None)}
        if op == "earthquakes":
            return {"cache": self._cached_or_queue("earthquakes", "earthquakes")}
        if op == "entertainment":
            kind = text(data.get("kind"), "entertainment category", 16, 1)
            if kind == "movies":
                return {"kind": kind, "cache": self._cached_or_queue(
                    "entertainment:movies", "movies")}
            if kind == "television":
                return {"kind": kind, "cache": self._cached_or_queue(
                    "entertainment:television", "television")}
            raise RPCError("unknown entertainment category")
        if op == "markets":
            if not self.massive_key:
                return {"available": False,
                        "reason": "Massive end-of-day market data is not configured."}
            return {"available": True,
                    "cache": self._cached_or_queue("markets", "markets")}
        if op == "market_quote":
            if not self.massive_key:
                raise RPCError("market data is not configured")
            symbol = text(data.get("symbol", ""), "ticker symbol", 10, 1).upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
                raise RPCError("enter a valid U.S. ticker symbol")
            return {"symbol": symbol, "cache": self._cached_or_queue(
                f"market:{symbol}", "market_quote", symbol)}
        if op == "iss":
            location = self._location(account_id)
            cached = self._cached_or_queue("iss", "iss")
            result = {"cache": cached, "location": dict(location) if location else None}
            if Satrec is not None:
                result["position"] = iss_position(cached["data"], datetime.now(timezone.utc))
                if location:
                    result["passes"] = iss_passes(cached["data"], float(location["latitude"]),
                                                  float(location["longitude"]))
            else:
                result["calculation_error"] = "SGP4 library unavailable"
            return result
        raise RPCError("unknown operation")


def _satellite(element: dict[str, object]) -> Any:
    if Satrec is None or omm is None:
        raise RPCError("SGP4 library unavailable")
    satellite = Satrec()
    omm.initialize(satellite, {key: str(value) for key, value in element.items()})
    return satellite


def _gmst(moment: datetime) -> float:
    jd = 2440587.5 + moment.timestamp() / 86400.0
    t = (jd - 2451545.0) / 36525.0
    degrees = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + \
        0.000387933 * t * t - t * t * t / 38710000.0
    return math.radians(degrees % 360.0)


def _eci(element: dict[str, object], moment: datetime) -> tuple[float, float, float, float]:
    sat = _satellite(element)
    second = moment.second + moment.microsecond / 1_000_000
    jd_value, fraction = jday(moment.year, moment.month, moment.day,
                              moment.hour, moment.minute, second)
    error, position, velocity = sat.sgp4(jd_value, fraction)
    if error:
        raise RPCError(f"SGP4 propagation error {error}")
    return position[0], position[1], position[2], math.sqrt(sum(v * v for v in velocity))


def _ecef(element: dict[str, object], moment: datetime) -> tuple[float, float, float, float]:
    x, y, z, speed = _eci(element, moment)
    theta = _gmst(moment)
    return (x * math.cos(theta) + y * math.sin(theta),
            -x * math.sin(theta) + y * math.cos(theta), z, speed)


def _geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    a = 6378.137
    e2 = 6.69437999014e-3
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(8):
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat = math.atan2(z + e2 * n * math.sin(lat), p)
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), alt


def iss_position(element: dict[str, object], moment: datetime) -> dict[str, object]:
    x, y, z, speed = _ecef(element, moment)
    lat, lon, altitude = _geodetic(x, y, z)
    return {"time": int(moment.timestamp()), "latitude": round(lat, 3),
            "longitude": round(lon, 3), "altitude_km": round(altitude, 1),
            "velocity_km_s": round(speed, 3), "element_epoch": element.get("EPOCH", "")}


def _look(element: dict[str, object], lat_deg: float, lon_deg: float,
          moment: datetime) -> tuple[float, float]:
    x, y, z, _ = _ecef(element, moment)
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    a, e2 = 6378.137, 6.69437999014e-3
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    ox = n * math.cos(lat) * math.cos(lon)
    oy = n * math.cos(lat) * math.sin(lon)
    oz = n * (1 - e2) * math.sin(lat)
    dx, dy, dz = x - ox, y - oy, z - oz
    east = -math.sin(lon) * dx + math.cos(lon) * dy
    north = (-math.sin(lat) * math.cos(lon) * dx
             - math.sin(lat) * math.sin(lon) * dy + math.cos(lat) * dz)
    up = (math.cos(lat) * math.cos(lon) * dx
          + math.cos(lat) * math.sin(lon) * dy + math.sin(lat) * dz)
    elevation = math.degrees(math.atan2(up, math.hypot(east, north)))
    azimuth = math.degrees(math.atan2(east, north)) % 360
    return elevation, azimuth


def iss_passes(element: dict[str, object], lat: float, lon: float) -> list[dict[str, object]]:
    start = datetime.now(timezone.utc).replace(microsecond=0)
    step = timedelta(seconds=60)
    passes: list[dict[str, object]] = []
    active: dict[str, object] | None = None
    for index in range(48 * 60 + 1):
        moment = start + index * step
        elevation, azimuth = _look(element, lat, lon, moment)
        visible = elevation >= 10.0
        if visible and active is None:
            active = {"start": int(moment.timestamp()), "start_azimuth": round(azimuth),
                      "maximum_elevation": round(elevation, 1), "end": 0,
                      "end_azimuth": round(azimuth)}
        elif visible and active is not None:
            active["maximum_elevation"] = max(float(active["maximum_elevation"]), round(elevation, 1))
            active["end_azimuth"] = round(azimuth)
        elif not visible and active is not None:
            active["end"] = int(moment.timestamp())
            passes.append(active)
            active = None
            if len(passes) >= 6:
                break
    return passes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--session-socket", required=True)
    parser.add_argument("--offline", action="store_true", help="disable network collector for fixtures")
    parser.add_argument("--massive-api-key-file")
    args = parser.parse_args()
    massive_key = (read_private_secret(args.massive_api_key_file)
                   if args.massive_api_key_file else "")
    serve(args.socket, Information(args.db, args.session_socket,
                                   not args.offline, massive_key).handle)


if __name__ == "__main__":
    main()

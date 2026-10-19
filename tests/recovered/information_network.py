#!/usr/bin/env python3
"""Deterministic security and parser regression tests for Information Network."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timezone

from marsv2.informationd import (Information, MARKET_BENCHMARKS,
                                UNOFFICIAL_INDEX_BENCHMARKS, _NoRedirect,
                                clean_text,
                                _json_ascii_clip,
                                extract_cisa_article, fetch, parse_feed,
                                local_hf_outlook, solar_elevation,
                                parse_movie_schedule, parse_tv_schedule,
                                prepare_open_article, normalize_market_series,
                                normalize_unofficial_index_chart)
from marsv2.rpc import MAX_PACKET, RPCError, encode_packet


assert [row[0] for row in MARKET_BENCHMARKS[:2]] == ["DIA", "SPY"]
assert [row[0] for row in UNOFFICIAL_INDEX_BENCHMARKS] == ["^DJI", "^GSPC"]

kentucky = {"location_name": "Lawrenceburg, Kentucky", "latitude": 38.0373,
            "longitude": -84.8966, "timezone": "America/New_York"}
quiet_solar = {"flux": 145, "forecast_flux": 140, "kp": 1,
               "forecast_kp_24h": 2, "radio_blackout_scale": 0,
               "solar_radiation_scale": 0}
summer_day = datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc)
summer_night = datetime(2026, 6, 21, 5, 0, tzinfo=timezone.utc)
assert solar_elevation(38.0373, -84.8966, summer_day) > 60
assert solar_elevation(38.0373, -84.8966, summer_night) < -15
day_outlook = local_hf_outlook(quiet_solar, kentucky, summer_day)
night_outlook = local_hf_outlook(quiet_solar, kentucky, summer_night)
day_bands = {row["band"]: row for row in day_outlook["rows"]}
night_bands = {row["band"]: row for row in night_outlook["rows"]}
assert day_outlook["period"] == "DAYLIGHT" and day_bands["10m"]["now"] == "GOOD"
assert night_outlook["period"] == "NIGHT" and night_bands["80m"]["now"] == "GOOD"
assert night_bands["10m"]["now"] == "POOR"
storm = local_hf_outlook({**quiet_solar, "forecast_kp_24h": 7}, kentucky,
                         summer_day)
storm_bands = {row["band"]: row for row in storm["rows"]}
assert storm_bands["20m"]["now"] == "GOOD"
assert storm_bands["20m"]["day"] == "POOR"
observed_storm = local_hf_outlook(
    {**quiet_solar, "kp": 7, "forecast_kp_24h": 7}, kentucky, summer_day)
assert {row["band"]: row for row in observed_storm["rows"]}["20m"]["now"] == "POOR"
assert "not a point-to-point prediction" in " ".join(storm["cautions"])


dirty = "<p>Hello <b>Mars</b></p><script>ignored text</script>\x1b]52;c;evil\x07\r\nNext"
clean = clean_text(dirty)
assert "\x1b" not in clean and "\x07" not in clean
assert "Hello Mars" in clean and "Next" in clean

rss = b"""<?xml version='1.0'?><rss><channel><item>
<title>Safe &amp; current</title><description><![CDATA[<p>Summary</p>]]></description>
<pubDate>Thu, 15 Oct 2026 12:00:00 GMT</pubDate><link>https://example.test/item</link>
</item></channel></rss>"""
rows = parse_feed(rss, "Fixture Wire")
assert rows[0]["title"] == "Safe & current"
assert rows[0]["summary"] == "Summary"
assert rows[0]["type"] == "SUMMARY"

full_rss = b"""<?xml version='1.0'?><rss xmlns:content='urn:test'><channel><item>
<title>Complete dispatch</title><description>Short summary</description>
<content:encoded><![CDATA[<h2>Report</h2><p>First paragraph.</p>
<script>terminal_attack()</script><p>Second paragraph.</p>]]></content:encoded>
<link>https://www.nasa.gov/example</link></item></channel></rss>"""
full_rows = parse_feed(full_rss, "NASA fixture", allow_full_text=True)
assert full_rows[0]["type"] == "FULL TEXT"
assert "First paragraph" in full_rows[0]["body"]
assert "terminal_attack" not in full_rows[0]["body"]

eff_rss = b"""<?xml version='1.0'?><rss xmlns:dc='urn:test'><channel><item>
<title>Digital rights report</title><dc:creator>Ada</dc:creator>
<description><![CDATA[<p>Complete EFF article.</p>]]></description>
<link>https://www.eff.org/deeplinks/example</link></item></channel></rss>"""
eff_rows = parse_feed(eff_rss, "EFF fixture", allow_full_text=True,
                      full_text_field="description")
assert eff_rows[0]["author"] == "Ada"
assert eff_rows[0]["body"] == "Complete EFF article."
eff_body, eff_type, eff_rights = prepare_open_article(
    eff_rows[0], "eff-full", str(eff_rows[0]["body"]))
assert eff_type == "FULL TEXT" and eff_rights == "CC BY 4.0"
assert "By Ada" in eff_body and "https://www.eff.org/" in eff_body

gv_rss = b"""<?xml version='1.0'?><rss xmlns:content='urn:test'
xmlns:dc='urn:dc'><channel><item><title>Open report</title>
<dc:creator><![CDATA[Grace Reporter]]></dc:creator>
<description>Summary</description><content:encoded><![CDATA[
<p>Complete Global Voices report.</p>]]></content:encoded>
<link>https://globalvoices.org/example/</link></item></channel></rss>"""
gv_rows = parse_feed(gv_rss, "Global Voices fixture", allow_full_text=True)
gv_body, gv_type, gv_rights = prepare_open_article(
    gv_rows[0], "global-voices-full", str(gv_rows[0]["body"]))
assert gv_type == "FULL TEXT" and gv_rights == "CC BY 3.0"
assert "Grace Reporter" in gv_body and "Terminal edition" in gv_body
long_tag = '<img src="x" data-responsive="' + ('z' * 5000) + '">Visible text'
assert clean_text(long_tag) == "Visible text"

partner_body, partner_type, partner_rights = prepare_open_article(
    gv_rows[0], "global-voices-full",
    "This item was republished with permission under a content-sharing agreement.")
assert not partner_body and partner_type == "SUMMARY"
assert partner_rights == "Partner contribution; summary only"

clipped = _json_ascii_clip("Mars 🚀 " * 1000, 700)
assert len(clipped.encode("utf-8")) < 700
assert len(__import__("json").dumps(clipped, ensure_ascii=True).encode("ascii")) <= 702

cisa_html = b"""<html><body><div class='l-page-section l-page-section--rich-text'>
<h2>Summary</h2><p>Official advisory body.</p><ul><li>Mitigation one</li></ul>
</div><div class='l-constrain l-page-section--rich-text'>footer</div></body></html>"""
cisa_body = extract_cisa_article(cisa_html)
assert "Official advisory body" in cisa_body and "Mitigation one" in cisa_body
assert "footer" not in cisa_body

for hostile in (
    b"<!DOCTYPE rss [<!ENTITY x 'boom'>]><rss>&x;</rss>",
    b"<rss><broken></rss>",
):
    try:
        parse_feed(hostile, "Fixture")
    except RPCError:
        pass
    else:
        raise AssertionError("unsafe or malformed XML accepted")

movie_html = """<table class='wikitable'><tr><th colspan='2'>Opening</th>
<th>Title</th><th>Production company</th><th>Cast and crew</th></tr>
<tr><th data-sort-value='10'>OCTOBER</th><td>16</td>
<td>Safe Mars Movie [12]</td><td>Red Planet Pictures</td>
<td>Ada (director); Grace, Linus</td></tr>
<tr><td>23</td><td>Second Feature</td><td>Terminal Films</td>
<td>Mars cast</td></tr></table>"""
movie_rows = parse_movie_schedule(
    movie_html, 2026, datetime(2026, 10, 1, tzinfo=timezone.utc),
    datetime(2026, 11, 1, tzinfo=timezone.utc))
assert [row["title"] for row in movie_rows] == ["Safe Mars Movie", "Second Feature"]
assert movie_rows[0]["date"] == "2026-10-16"
assert movie_rows[1]["company"] == "Terminal Films"

tv_rows = parse_tv_schedule([{
    "name": "Pilot", "season": 1, "number": 1, "airdate": "2026-10-20",
    "airtime": "21:00", "_links": {"show": {"name": "Fallback Name"}},
    "_embedded": {"show": {
        "id": 42, "name": "Mars Series", "genres": ["Science-Fiction"],
        "summary": "<p>Safe summary.</p>", "url": "https://www.tvmaze.com/shows/42",
        "network": {"name": "Mars TV"},
    }},
}, {
    "name": "Not a premiere", "season": 1, "number": 2,
}])
assert len(tv_rows) == 1 and tv_rows[0]["show"] == "Mars Series"
assert tv_rows[0]["kind"] == "NEW SERIES" and tv_rows[0]["network"] == "Mars TV"
assert tv_rows[0]["summary"] == "Safe summary."

for invalid_movies in ("x" * 700_001, "<table><tr><td>not a schedule</td></tr></table>"):
    try:
        parsed = parse_movie_schedule(
            invalid_movies, 2026, datetime(2026, 10, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc))
        if invalid_movies.startswith("x"):
            raise AssertionError("oversized movie calendar accepted")
        assert parsed == []
    except RPCError:
        assert invalid_movies.startswith("x")

try:
    parse_tv_schedule({"not": "a schedule"})
except RPCError:
    pass
else:
    raise AssertionError("malformed TV schedule accepted")

hostile_tv = parse_tv_schedule([{
    "name": "Pilot", "season": 1, "number": 1, "airdate": "2026-10-20",
    "_embedded": {"show": {"id": 99, "name": "Mars\x1b]52;c;bad\x07",
                              "summary": "<script>bad()</script><p>Visible</p>"}},
}])
assert hostile_tv[0]["show"] == "Mars]52;c;bad"
assert hostile_tv[0]["summary"] == "Visible"

market = normalize_market_series({"status": "DELAYED", "results": [
    {"o": 100, "h": 110, "l": 95, "c": 105, "v": 1000,
     "t": 1791921600000},
    {"o": 106, "h": 115, "l": 104, "c": 113, "v": 2000,
     "t": 1792008000000},
]}, "MARS", "Mars Industries", "STOCK OR ETF")
assert market["close"] == 113 and market["change"] == 8
assert round(float(market["percent"]), 2) == 7.62
assert market["volume"] == 2000

unofficial_market = normalize_unofficial_index_chart({
    "chart": {"error": None, "result": [{
        "meta": {"symbol": "^DJI"},
        "timestamp": [1791921600, 1792008000],
        "indicators": {"quote": [{
            "open": [52000, 52100], "high": [52200, 52300],
            "low": [51900, 52000], "close": [52100, 52250],
            "volume": [1000, 2000],
        }]},
    }]},
}, "^DJI", "Dow Jones Industrial Average")
assert unofficial_market["close"] == 52250
assert unofficial_market["change"] == 150
assert unofficial_market["kind"] == "UNOFFICIAL"
for malformed_market in ({"status": "ERROR"}, {"status": "OK", "results": []},
                         {"status": "OK", "results": [{"c": "nan"}]}):
    try:
        normalize_market_series(malformed_market, "MARS", "Mars", "STOCK")
    except (RPCError, TypeError, ValueError):
        pass
    else:
        raise AssertionError("malformed market response accepted")

try:
    _NoRedirect().redirect_request(None, None, 302, "moved", {},
                                   "https://api.open-meteo.com/elsewhere")
except RPCError:
    pass
else:
    raise AssertionError("provider redirect accepted")

for url in (
    "http://api.open-meteo.com/",
    "https://127.0.0.1/data",
    "https://user:password@api.open-meteo.com/data",
    "https://api.open-meteo.com:444/data",
):
    try:
        fetch(url, "application/json")
    except RPCError:
        pass
    else:
        raise AssertionError(f"SSRF boundary accepted {url}")

with tempfile.TemporaryDirectory() as directory:
    service = Information(os.path.join(directory, "information.db"), "/not/used", online=False)
    service._put("fixture", {"text": "cached"}, "Local fixture", 60)
    cached = service._get("fixture")
    assert cached and cached["data"]["text"] == "cached" and not cached["stale"]
    service._put("stale", {"text": "old"}, "Local fixture", -1)
    assert service._get("stale")["stale"] is True
    size = service._db().execute(
        "SELECT sum(length(payload)) FROM cache").fetchone()[0]
    assert size < 1024
    long_body = "Mars paragraph.\n" * 4000
    service._put("article:test", {"body": long_body}, "Local fixture", 60,
                 maximum=1_000_000)
    assert service._get("article:test")["data"]["body"] == long_body

    current_id, reading_id, old_id = "a" * 32, "b" * 32, "c" * 32
    for article_id, body in ((current_id, "current"), (reading_id, "reading"),
                             (old_id, "obsolete")):
        service._put(f"article:{article_id}", {"body": body, "type": "FULL TEXT"},
                     "Local fixture", 60, maximum=1_000_000)
    service._put("news:us", [{"article_id": current_id}], "Local fixture", 60)
    service._lease_article(reading_id, "test-session")
    removed = service._prune_articles()
    assert removed >= 2  # obsolete plus the earlier invalid fixture article
    assert service._get(f"article:{old_id}") is None
    assert service._get(f"article:{current_id}") is not None
    assert service._get(f"article:{reading_id}") is not None
    stored = service._store_article(
        reading_id, {"body": "replacement", "type": "FULL TEXT"}, "Local fixture")
    assert stored["body"] == "reading"
    service._release_article(reading_id, "test-session")
    assert service._prune_articles() == 1
    assert service._get(f"article:{reading_id}") is None
    refreshed = service._store_article(
        current_id, {"body": "refreshed", "type": "FULL TEXT"}, "Local fixture")
    assert refreshed["body"] == "refreshed"
    assert service._get(f"article:{current_id}")["data"]["body"] == "refreshed"

bounded = {"v": 1, "op": "response", "ok": True,
           "data": {"events": [{"place": "x" * 240} for _ in range(40)]}}
assert len(encode_packet(bounded)) < MAX_PACKET

print("PASS information parser, sanitization, SSRF allowlist, and cache tests")

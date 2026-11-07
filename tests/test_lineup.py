#!/usr/bin/env python3
"""Offline tests for the combined Pluto and Samsung lineup."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import generate_pluto_m3u as pluto  # noqa: E402
import merge_lineup  # noqa: E402
import samsung_tvplus  # noqa: E402
from merge_lineup import LiveChannel, Programme, PublishError  # noqa: E402

WINDOW_START = datetime(2026, 11, 7, 8, tzinfo=timezone.utc)
WINDOW_END = WINDOW_START + timedelta(hours=6)
EPG_URL = "https://raw.githubusercontent.com/radowitz/Pluto/main/epg/us.xml"


def programme(hours: float, title: str = "Show", description: str = "", icon: str = "") -> Programme:
    return Programme(
        start=WINDOW_START,
        stop=WINDOW_START + timedelta(hours=hours),
        title=title,
        description=description,
        icon=icon,
    )


def channel(
    provider: str,
    source_id: str,
    name: str,
    group: str,
    *,
    hours: float = 1,
    description: str = "",
    logo: str = "https://cdn.example/logo.png",
    number: int | None = 1,
    stream: str | None = None,
) -> LiveChannel:
    return LiveChannel(
        provider=provider,
        source_id=source_id,
        name=name,
        group=group,
        stream_url=stream or f"https://cdn.example/{provider}/{source_id}.m3u8",
        logo=logo,
        number=number,
        programmes=[programme(hours, description=description)],
    )


class NameAndDedupeTests(unittest.TestCase):
    def test_normalized_names_keep_regional_stations(self) -> None:
        self.assertEqual(merge_lineup.name_key("PGA TOUR"), merge_lineup.name_key("PGA Tour"))
        self.assertEqual(merge_lineup.name_key("NEWSMAX2"), merge_lineup.name_key("Newsmax2"))
        self.assertEqual(merge_lineup.name_key("Field & Stream"), merge_lineup.name_key("Field and Stream"))
        self.assertNotEqual(merge_lineup.name_key("CBS News"), merge_lineup.name_key("CBS News Baltimore"))
        self.assertNotEqual(merge_lineup.name_key("CBS News Baltimore"), merge_lineup.name_key("CBS News Detroit"))
        self.assertNotEqual(merge_lineup.name_key("NBC News NOW"), merge_lineup.name_key("NBC Sports NOW"))
        self.assertNotEqual(merge_lineup.name_key("MLB"), merge_lineup.name_key("MLB Channel"))

    def test_dedupe_prefers_validated_stream_then_guide(self) -> None:
        pluto_channel = channel("pluto", "p1", "CBS News 24/7", "News + Opinion", hours=1, number=100)
        samsung_channel = channel("samsung", "s1", "CBS News 24/7", "News & Opinion", hours=4, description="Live", number=1110)
        kept, notes = merge_lineup.dedupe_by_name(
            [pluto_channel, samsung_channel],
            WINDOW_START,
            WINDOW_END,
            {pluto_channel.tvg_id: "ok", samsung_channel.tvg_id: "ok"},
        )
        self.assertEqual([item.tvg_id for item in kept], ["samsung-s1"])
        self.assertTrue(any("kept 'CBS News 24/7' [samsung" in note for note in notes))

        kept, _ = merge_lineup.dedupe_by_name(
            [pluto_channel, samsung_channel],
            WINDOW_START,
            WINDOW_END,
            {pluto_channel.tvg_id: "ok", samsung_channel.tvg_id: "geo"},
        )
        self.assertEqual(kept[0].provider, "pluto")

        kept, _ = merge_lineup.dedupe_by_name(
            [pluto_channel, samsung_channel],
            WINDOW_START,
            WINDOW_END,
            {pluto_channel.tvg_id: "ok", samsung_channel.tvg_id: "bad"},
        )
        self.assertEqual(kept[0].provider, "pluto")

    def test_equal_guides_prefer_pluto_and_distinct_names_remain(self) -> None:
        pluto_channel = channel("pluto", "p1", "BBC News", "News + Opinion", hours=2, number=50)
        samsung_channel = channel("samsung", "s1", "BBC News", "News & Opinion", hours=2, number=1140)
        baltimore = channel("samsung", "b", "CBS News Baltimore", "News & Opinion", number=2)
        detroit = channel("pluto", "d", "CBS News Detroit", "Local News", number=3)
        kept, _ = merge_lineup.dedupe_by_name(
            [samsung_channel, pluto_channel, baltimore, detroit],
            WINDOW_START,
            WINDOW_END,
            {},
        )
        by_name = {item.name: item.provider for item in kept}
        self.assertEqual(by_name["BBC News"], "pluto")
        self.assertEqual(by_name["CBS News Baltimore"], "samsung")
        self.assertEqual(by_name["CBS News Detroit"], "pluto")

    def test_both_invalid_streams_drop_the_name(self) -> None:
        first = channel("pluto", "p", "UFC", "Sports")
        second = channel("samsung", "s", "UFC", "Sports & Outdoors")
        kept, notes = merge_lineup.dedupe_by_name(
            [first, second],
            WINDOW_START,
            WINDOW_END,
            {first.tvg_id: "bad", second.tvg_id: "bad"},
        )
        self.assertEqual(kept, [])
        self.assertTrue(notes)

    def test_provider_ids_do_not_collide_and_logos_match_the_guide(self) -> None:
        rows = merge_lineup.sort_channels(
            [
                channel("samsung", "same", "NASCAR Channel", "Motor Sports", number=2505, hours=2),
                channel("pluto", "same", "NFL Channel", "Sports", number=10, hours=2),
                channel("pluto", "earth", "BBC Earth", "Animals + Nature", number=4, hours=2),
                channel("samsung", "news", "WSOC Charlotte", "News & Opinion", number=1122, hours=2),
            ]
        )
        self.assertEqual([item.group for item in rows], ["News & Opinion", "Sports", "Motor Sports", "Animals + Nature"])
        playlist = merge_lineup.build_m3u(rows, EPG_URL, WINDOW_START, WINDOW_START + timedelta(hours=20))
        guide, count = merge_lineup.build_epg(rows)
        entries = merge_lineup.assert_publishable(playlist, guide, epg_url=EPG_URL, min_channels=4)
        self.assertEqual(count, 4)
        self.assertEqual([item[0] for item in entries], ["samsung-news", "pluto-same", "samsung-same", "pluto-earth"])
        self.assertIn('tvg-logo="https://cdn.example/logo.png"', playlist)
        self.assertIn('group-title="Samsung TV Plus / Motor Sports"', playlist)
        self.assertIn('group-title="Pluto TV / Sports"', playlist)
        self.assertIn('<icon src="https://cdn.example/logo.png" />', guide)
        self.assertNotIn("jmp2.uk", playlist)
        self.assertNotIn("jmp2.uk", guide)

    def test_xml_escapes_logo_and_title_text(self) -> None:
        row = channel("samsung", "x", "Tom & Jerry", "Kids", description="A & B <tag>")
        row.programmes[0].icon = "https://cdn.example/ep.png?a=1&b=2"
        guide, _ = merge_lineup.build_epg([row])
        self.assertIn("Tom &amp; Jerry", guide)
        self.assertIn("A &amp; B &lt;tag&gt;", guide)
        self.assertIn("a=1&amp;b=2", guide)
        self.assertNotIn("a=1&b=2", guide)


class PublishGuardTests(unittest.TestCase):
    def test_rejects_proxy_urls_shrinkage_and_mismatched_ids(self) -> None:
        row = channel("pluto", "1", "CBS News 24/7", "News + Opinion", stream="https://jmp2.uk/stvp-1.m3u8")
        playlist = merge_lineup.build_m3u([row], EPG_URL, WINDOW_START, WINDOW_START)
        guide, _ = merge_lineup.build_epg([row])
        with self.assertRaises(PublishError):
            merge_lineup.assert_publishable(playlist, guide, epg_url=EPG_URL, min_channels=1)

        row.stream_url = "https://cdn.example/ok.m3u8"
        playlist = merge_lineup.build_m3u([row], EPG_URL, WINDOW_START, WINDOW_START)
        guide, _ = merge_lineup.build_epg([row])
        with self.assertRaises(PublishError):
            merge_lineup.assert_publishable(playlist, guide, epg_url=EPG_URL, min_channels=1, previous_count=40)

        broken = guide.replace('channel id="pluto-1"', 'channel id="other-1"')
        with self.assertRaises(PublishError):
            merge_lineup.assert_publishable(playlist, broken, epg_url=EPG_URL, min_channels=1)

    def test_manifest_and_logo_classification(self) -> None:
        self.assertEqual(merge_lineup.classify_manifest(200, b"#EXTM3U\n#EXT-X-STREAM-INF:"), "ok")
        self.assertEqual(merge_lineup.classify_manifest(403, b"Access Denied"), "geo")
        self.assertEqual(merge_lineup.classify_manifest(451, b""), "geo")
        self.assertEqual(merge_lineup.classify_manifest(404, b"missing"), "bad")
        self.assertEqual(merge_lineup.classify_manifest(200, b"<html>no</html>"), "bad")
        self.assertEqual(merge_lineup.classify_manifest(503, b""), "unknown")
        self.assertEqual(merge_lineup.classify_logo(200, "image/png", b""), "ok")
        self.assertEqual(merge_lineup.classify_logo(200, "application/octet-stream", b"\x89PNG\r\n"), "ok")
        self.assertEqual(merge_lineup.classify_logo(404, "image/png", b""), "bad")
        self.assertEqual(merge_lineup.classify_logo(200, "text/html", b"<html>"), "bad")


class SamsungSelectionTests(unittest.TestCase):
    def test_required_groups_include_motor_sports_and_skip_drm(self) -> None:
        catalog = {}
        samples = {
            "News & Opinion": ("USNEWS", "CBS News 24/7", 1110),
            "Sports & Outdoors": ("USSPORT", "NFL Channel", 2310),
            "Motor Sports": ("USMOTOR", "NASCAR Channel", 2505),
            "Music": ("USMUSIC", "Vevo Pop", 3000),
        }
        for group, (channel_id, name, number) in samples.items():
            catalog[channel_id] = {"name": name, "group": group, "chno": number, "logo": f"https://logo.example/{channel_id}.png"}
        catalog["USDRM"] = {
            "name": "Locked Sports",
            "group": "Sports & Outdoors",
            "chno": 2311,
            "logo": "https://logo.example/drm.png",
            "license_url": "https://license.example/widevine",
        }
        selected = samsung_tvplus.select_channels(catalog)
        names = {row["name"] for row in selected}
        self.assertEqual(names, {"CBS News 24/7", "NFL Channel", "NASCAR Channel"})
        self.assertTrue(all(row["logo"].startswith("https://logo.example/") for row in selected))

    def test_missing_motor_sports_aborts(self) -> None:
        catalog = {
            "a": {"name": "CBS News 24/7", "group": "News & Opinion", "chno": 1, "logo": "https://logo.example/a.png"},
            "b": {"name": "NFL Channel", "group": "Sports & Outdoors", "chno": 2, "logo": "https://logo.example/b.png"},
        }
        with self.assertRaises(RuntimeError) as caught:
            samsung_tvplus.select_channels(catalog)
        self.assertIn("Motor Sports", str(caught.exception))

    def test_provider_redirect_stops_at_the_first_direct_manifest(self) -> None:
        provider = "https://dai.google.com/linear/hls/event/abc/master.m3u8?dai-ah=1"
        nxt, found = samsung_tvplus.provider_from_hop("https://jmp2.uk/stvp-US1", 302, provider, b"")
        self.assertIsNone(nxt)
        self.assertEqual(found, provider)

        nxt, found = samsung_tvplus.provider_from_hop("https://jmp2.uk/stvp-US1", 302, "https://jmp2.uk/stvp-US1b", b"")
        self.assertEqual(nxt, "https://jmp2.uk/stvp-US1b")
        self.assertIsNone(found)

        with self.assertRaises(RuntimeError):
            samsung_tvplus.provider_from_hop(
                "https://jmp2.uk/stvp-US1",
                302,
                "https://secret.workers.dev/live/master.m3u8",
                b"",
            )
        with self.assertRaises(RuntimeError):
            samsung_tvplus.accept_provider_url("https://user:pass@cdn.example/a.m3u8")

    def test_epg_ids_and_window(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <tv>
          <channel id="USNEWS"><display-name>CBS News 24/7</display-name>
            <icon src="https://tvpnlogopus.samsungcloud.tv/logo.png" />
          </channel>
          <channel id="OTHER"><display-name>Music</display-name></channel>
          <programme start="20261107080000 +0000" stop="20261107090000 +0000" channel="USNEWS">
            <title>The Daily Report</title>
            <desc>Morning news</desc>
            <icon src="https://tvpnlogopus.samsungcloud.tv/ep.png" />
          </programme>
          <programme start="20261107230000 +0000" stop="20261108000000 +0000" channel="USNEWS">
            <title>Too late</title>
          </programme>
          <programme start="20261107080000 +0000" stop="20261107090000 +0000" channel="OTHER">
            <title>Songs</title>
          </programme>
        </tv>
        """.encode()
        programmes, icons = samsung_tvplus.index_epg(xml, {"USNEWS"}, WINDOW_START, WINDOW_END)
        self.assertEqual(icons["USNEWS"], "https://tvpnlogopus.samsungcloud.tv/logo.png")
        self.assertEqual([item["title"] for item in programmes["USNEWS"]], ["The Daily Report"])
        self.assertEqual(programmes["USNEWS"][0]["description"], "Morning news")


class PlutoFilterTests(unittest.TestCase):
    def test_published_pluto_groups_are_unchanged(self) -> None:
        self.assertEqual(
            pluto.KEEP_GROUPS,
            ["News + Opinion", "Sports", "Animals + Nature", "Local News"],
        )
        self.assertEqual(pluto.PARTIAL_GROUPS["History + Science"], ["cnn originals", "cnn"])
        self.assertEqual(pluto.PREFERRED_DUPLICATE_GROUP, "Local News")

    def test_filter_keeps_cnn_originals_and_local_duplicate(self) -> None:
        def row(name: str, group: str, channel_id: str) -> dict:
            return {"_id": channel_id, "name": name, "category": group, "isStitched": True, "number": 1}

        channels = [
            row("CBS News 24/7", "News + Opinion", "1"),
            row("CBS News Baltimore", "News + Opinion", "2"),
            row("NFL Channel", "Sports", "3"),
            row("BBC Earth", "Animals + Nature", "4"),
            row("CBS News Baltimore", "Local News", "5"),
            row("CNN Originals", "History + Science", "6"),
            row("History Channel", "History + Science", "7"),
            row("Movie Hub", "Movies", "8"),
        ]
        kept = pluto.dedupe_channels(pluto.filter_channels(channels))
        names = [item["name"] for item in kept]
        self.assertIn("CNN Originals", names)
        self.assertNotIn("History Channel", names)
        self.assertNotIn("Movie Hub", names)
        baltimore = [item for item in kept if item["name"] == "CBS News Baltimore"]
        self.assertEqual(len(baltimore), 1)
        self.assertEqual(baltimore[0]["_id"], "5")
        self.assertEqual(baltimore[0]["category"], "Local News")


if __name__ == "__main__":
    unittest.main()

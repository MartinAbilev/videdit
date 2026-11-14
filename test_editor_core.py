import io
import os
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from editor_core import (
    Clip,
    EditItem,
    build_premiere_xml,
    load_environment_file,
    load_project,
    local_prompt_plan,
    read_png_frame,
    save_project,
    vision_prompt_plan,
)


class EditorCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        media_dir = Path(tempfile.gettempdir()).resolve()
        self.clips = [
            Clip(media_dir / "city_night.mp4", 12.0, False, True, 1920, 1080),
            Clip(media_dir / "mountain_sunrise.jpg", 5.0, True, False, 1200, 800),
            Clip(media_dir / "ocean.mp4", 20.0, False, False, 1280, 720),
        ]

    def test_local_plan_matches_prompt_and_limits_fast_clips(self) -> None:
        items = local_prompt_plan(self.clips, "fast city night montage")
        self.assertEqual([item.clip.name for item in items], ["city_night.mp4", "mountain_sunrise.jpg", "ocean.mp4"])
        self.assertEqual([item.duration for item in items], [3.0, 3.0, 3.0])

    def test_local_plan_sorts_short_clips_when_requested(self) -> None:
        items = local_prompt_plan(self.clips, "make a short edit")
        self.assertEqual([item.clip.name for item in items], ["mountain_sunrise.jpg", "city_night.mp4", "ocean.mp4"])

    def test_premiere_xml_preserves_trim_and_audio_track(self) -> None:
        items = [EditItem(self.clips[0], 2.0, 6.0), EditItem(self.clips[1])]
        document = build_premiere_xml(items, "Test & Edit")
        root = ET.fromstring(document)
        self.assertEqual(root.findtext("./sequence/name"), "Test & Edit")
        video_items = root.findall("./sequence/media/video/track/clipitem")
        audio_items = root.findall("./sequence/media/audio/track/clipitem")
        self.assertEqual(len(video_items), 2)
        self.assertEqual(len(audio_items), 1)
        self.assertEqual(video_items[0].findtext("in"), "60")
        self.assertEqual(video_items[0].findtext("out"), "180")
        self.assertEqual(video_items[1].findtext("start"), "120")
        self.assertIn("file://", video_items[0].findtext("./file/pathurl") or "")

    def test_empty_premiere_timeline_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_premiere_xml([])

    def test_project_round_trip_preserves_relative_media_and_trim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_path = root / "media" / "scene.mp4"
            media_path.parent.mkdir()
            media_path.write_bytes(b"test media placeholder")
            project_path = root / "edit.json"
            clip = Clip(media_path, 8.0, False, True, 1920, 1080)
            timeline = [EditItem(clip, 1.25, 4.5)]

            save_project(project_path, [clip], timeline, "a calm scene")
            media, loaded_timeline, prompt = load_project(project_path)

            self.assertEqual(prompt, "a calm scene")
            self.assertEqual(media[0].path, media_path.resolve())
            self.assertEqual(loaded_timeline[0].clip, media[0])
            self.assertEqual((loaded_timeline[0].start, loaded_timeline[0].end), (1.25, 4.5))

    def test_project_load_rejects_missing_media(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "edit.json"
            project_path.write_text(
                '{"format":"promptcut-project","version":1,"media":['
                '{"path":"missing.mp4","duration":5,"is_image":false,'
                '"has_audio":false}],"timeline":[]}',
                encoding="utf-8",
            )
            with self.assertRaises(FileNotFoundError):
                load_project(project_path)

    def test_environment_file_loads_quoted_values_and_preserves_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "export PROMPTCUT_TEST_KEY = 'from file' # comment\n"
                "PROMPTCUT_EXISTING=from-file\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "PROMPTCUT_TEST_KEY": "",
                    "PROMPTCUT_EXISTING": "from-system",
                },
                clear=False,
            ):
                load_environment_file(env_path)
                self.assertEqual(os.environ["PROMPTCUT_TEST_KEY"], "from file")
                self.assertEqual(os.environ["PROMPTCUT_EXISTING"], "from-system")

    def test_environment_file_rejects_malformed_lines_without_partial_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("PROMPTCUT_TEMP=value\nnot-an-assignment\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=False):
                with self.assertRaisesRegex(ValueError, r"\.env:2"):
                    load_environment_file(env_path)
                self.assertNotIn("PROMPTCUT_TEMP", os.environ)

    def test_vision_api_429_has_rate_limit_guidance(self) -> None:
        response = HTTPError(
            "https://api.example.test/v1/chat/completions",
            429,
            "Too Many Requests",
            None,
            io.BytesIO(b'{"error":{"message":"Rate limit reached"}}'),
        )
        with (
            patch("editor_core.extract_sample_frame", return_value=b"frame"),
            patch("editor_core.urllib.request.urlopen", side_effect=response),
        ):
            with self.assertRaisesRegex(RuntimeError, r"GPT API rate limit \(HTTP 429\)") as error:
                vision_prompt_plan(
                    [self.clips[1]],
                    "make an edit",
                    "test-key",
                    "gpt-4o-mini",
                    "https://api.example.test/v1",
                )
        self.assertIn("Rate limit reached", str(error.exception))
        self.assertIn("check API usage and billing limits", str(error.exception))

    def test_png_stream_reader_extracts_concatenated_frames(self) -> None:
        def chunk(chunk_type: bytes, data: bytes) -> bytes:
            checksum = zlib.crc32(chunk_type + data)
            return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b"")
        )
        stream = io.BytesIO(png + png)
        self.assertEqual(read_png_frame(stream), png)
        self.assertEqual(read_png_frame(stream), png)
        self.assertIsNone(read_png_frame(stream))


if __name__ == "__main__":
    unittest.main()


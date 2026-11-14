from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError


IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


@dataclass(frozen=True)
class Clip:
    path: Path
    duration: float
    is_image: bool
    has_audio: bool
    width: int = 0
    height: int = 0

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class EditItem:
    clip: Clip
    start: float = 0.0
    end: float = 0.0

    def __post_init__(self) -> None:
        if self.end == 0.0:
            self.end = self.clip.duration

    @property
    def duration(self) -> float:
        return self.end - self.start


def save_project(
    project_path: Path,
    media: list[Clip],
    timeline: list[EditItem],
    prompt: str = "",
) -> None:
    project_path = project_path.resolve()
    media_indexes = {clip.path.resolve(): index for index, clip in enumerate(media)}
    assets = []
    for clip in media:
        try:
            media_path = os.path.relpath(clip.path.resolve(), project_path.parent)
        except ValueError:
            media_path = str(clip.path.resolve())
        assets.append(
            {
                "path": media_path,
                "duration": clip.duration,
                "is_image": clip.is_image,
                "has_audio": clip.has_audio,
                "width": clip.width,
                "height": clip.height,
            }
        )

    edits = []
    for item in timeline:
        media_index = media_indexes.get(item.clip.path.resolve())
        if media_index is None:
            raise ValueError(f"{item.clip.name} is on the timeline but missing from the media bin.")
        if not (math.isfinite(item.start) and math.isfinite(item.end)):
            raise ValueError(f"{item.clip.name} has invalid trim points.")
        if not 0 <= item.start < item.end <= item.clip.duration:
            raise ValueError(f"{item.clip.name} has trim points outside its duration.")
        edits.append({"media_index": media_index, "start": item.start, "end": item.end})

    document = {
        "format": "promptcut-project",
        "version": 1,
        "prompt": prompt,
        "media": assets,
        "timeline": edits,
    }
    project_path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def load_project(project_path: Path) -> tuple[list[Clip], list[EditItem], str]:
    project_path = project_path.resolve()
    try:
        document = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read project file: {error}") from error
    if not isinstance(document, dict) or document.get("format") != "promptcut-project":
        raise ValueError("This is not a PromptCut project file.")
    if document.get("version") != 1:
        raise ValueError(f"Unsupported PromptCut project version: {document.get('version')!r}")

    raw_media = document.get("media")
    raw_timeline = document.get("timeline")
    if not isinstance(raw_media, list) or not isinstance(raw_timeline, list):
        raise ValueError("The project must contain media and timeline arrays.")

    clips: list[Clip] = []
    for index, asset in enumerate(raw_media):
        if not isinstance(asset, dict) or not isinstance(asset.get("path"), str):
            raise ValueError(f"Media entry {index + 1} is invalid.")
        media_path = Path(asset["path"])
        if not media_path.is_absolute():
            media_path = project_path.parent / media_path
        media_path = media_path.resolve()
        if not media_path.is_file():
            raise FileNotFoundError(f"Project media file not found: {media_path}")
        try:
            duration = float(asset["duration"])
            width = int(asset.get("width", 0))
            height = int(asset.get("height", 0))
            is_image = asset["is_image"]
            has_audio = asset["has_audio"]
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Media entry {index + 1} has invalid metadata.") from error
        if (
            not math.isfinite(duration)
            or duration <= 0
            or width < 0
            or height < 0
            or not isinstance(is_image, bool)
            or not isinstance(has_audio, bool)
        ):
            raise ValueError(f"Media entry {index + 1} has invalid metadata.")
        clips.append(Clip(media_path, duration, is_image, has_audio, width, height))

    items: list[EditItem] = []
    for index, edit in enumerate(raw_timeline):
        if not isinstance(edit, dict):
            raise ValueError(f"Timeline entry {index + 1} is invalid.")
        try:
            media_index = edit["media_index"]
            start = float(edit["start"])
            end = float(edit["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Timeline entry {index + 1} has invalid trim points.") from error
        if isinstance(media_index, bool) or not isinstance(media_index, int):
            raise ValueError(f"Timeline entry {index + 1} has an invalid media index.")
        if not 0 <= media_index < len(clips):
            raise ValueError(f"Timeline entry {index + 1} references missing media.")
        clip = clips[media_index]
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= clip.duration):
            raise ValueError(f"Timeline entry {index + 1} has invalid trim points.")
        items.append(EditItem(clip, start, end))

    prompt = document.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("The project prompt must be text.")
    return clips, items, prompt


def probe_clip(path: Path) -> Clip:
    path = path.resolve()
    is_image = path.suffix.lower() in IMAGE_EXTENSIONS
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Could not read {path.name}")
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Could not read media metadata for {path.name}") from error

    streams = metadata.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    duration_value = metadata.get("format", {}).get("duration")
    duration = float(duration_value) if duration_value not in (None, "N/A") else 0.0
    if is_image:
        duration = 5.0
    if not is_image and duration <= 0:
        raise RuntimeError(f"{path.name} has no readable duration")
    if not is_image and not video_stream:
        raise RuntimeError(f"{path.name} does not contain a video stream")
    return Clip(
        path=path,
        duration=duration,
        is_image=is_image,
        has_audio=has_audio,
        width=int(video_stream.get("width", 0)),
        height=int(video_stream.get("height", 0)),
    )


def local_prompt_plan(clips: list[Clip], prompt: str) -> list[EditItem]:
    if not clips:
        raise ValueError("Import at least one video or image first.")

    prompt_words = {
        word for word in re.findall(r"[a-z0-9]+", prompt.lower()) if len(word) > 2
    }
    stop_words = {
        "the", "and", "for", "with", "that", "this", "make", "video", "edit",
        " footage", "from", "into", "please", "want", "show",
    }
    prompt_words -= stop_words
    scored = [
        (
            sum(
                1
                for word in prompt_words
                if word in clip.path.stem.lower().replace("_", " ").replace("-", " ")
            ),
            index,
            clip,
        )
        for index, clip in enumerate(clips)
    ]
    if any(score for score, _, _ in scored):
        scored.sort(key=lambda result: (-result[0], result[1]))
    elif "short" in prompt.lower() or "fast" in prompt.lower():
        scored.sort(key=lambda result: (result[2].duration, result[1]))
    elif "slow" in prompt.lower() or "long" in prompt.lower():
        scored.sort(key=lambda result: (-result[2].duration, result[1]))

    max_length = 3.0 if "fast" in prompt.lower() or "short" in prompt.lower() else None
    items = []
    for _, _, clip in scored:
        end = min(clip.duration, max_length) if max_length else clip.duration
        items.append(EditItem(clip=clip, start=0.0, end=end))
    return items


def extract_sample_frame(clip: Clip, at_seconds: float) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as image_file:
        image_path = Path(image_file.name)
    try:
        command = ["ffmpeg", "-v", "error", "-y"]
        if not clip.is_image:
            command.extend(["-ss", f"{at_seconds:.3f}"])
        command.extend(
            [
                "-i",
                str(clip.path),
                "-frames:v",
                "1",
                "-vf",
                "scale=512:-2",
                str(image_path),
            ]
        )
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"Could not sample {clip.name}")
        return image_path.read_bytes()
    finally:
        image_path.unlink(missing_ok=True)


def vision_prompt_plan(
    clips: list[Clip],
    prompt: str,
    api_key: str,
    model: str,
    base_url: str,
) -> tuple[list[EditItem], list[str]]:
    if not clips:
        raise ValueError("Import at least one video or image first.")
    if not api_key:
        raise ValueError("Set OPENAI_API_KEY before using cloud vision.")

    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Review the sampled frames from the numbered video/image assets and make "
                "a concise edit that follows the user's prompt. Return only JSON with an "
                '"edits" array. Each entry must have "asset_index" (zero-based integer), '
                '"in_seconds" and "out_seconds" (numbers). Use only supplied assets, '
                "keep source times within their durations, and do not invent footage. "
                "Include each relevant asset at most once.\n"
                f"User prompt: {prompt}\nAssets:\n"
                + "\n".join(
                    f"{index}: {clip.name}, duration {clip.duration:.2f}s, "
                    f"resolution {clip.width}x{clip.height}"
                    for index, clip in enumerate(clips)
                )
            ),
        }
    ]
    for index, clip in enumerate(clips):
        sample_times = (
            [clip.duration * fraction for fraction in (0.2, 0.5, 0.8)]
            if not clip.is_image
            else [0.0]
        )
        for sample_time in sample_times:
            frame = extract_sample_frame(clip, sample_time)
            content.append(
                {
                    "type": "text",
                    "text": f"Asset {index}, sampled at {sample_time:.2f} seconds:",
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,"
                        + base64.b64encode(frame).decode("ascii"),
                        "detail": "low",
                    },
                }
            )

    payload = json.dumps(
        {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            response_body = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cloud vision request failed: {error}") from error

    try:
        response_text = response_body["choices"][0]["message"]["content"]
        plan = json.loads(response_text)
        raw_edits = plan["edits"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("The vision service returned an invalid edit plan.") from error
    if not isinstance(raw_edits, list) or not raw_edits:
        raise RuntimeError("The vision service returned an empty edit plan.")

    items: list[EditItem] = []
    rationales: list[str] = []
    seen: set[int] = set()
    for edit in raw_edits:
        try:
            asset_index = int(edit["asset_index"])
            start = float(edit["in_seconds"])
            end = float(edit["out_seconds"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("The vision service returned an invalid clip entry.") from error
        if asset_index < 0 or asset_index >= len(clips) or asset_index in seen:
            raise RuntimeError("The vision service returned an unknown or repeated asset.")
        clip = clips[asset_index]
        if not (0 <= start < end <= clip.duration):
            raise RuntimeError(f"The vision service returned invalid times for {clip.name}.")
        seen.add(asset_index)
        items.append(EditItem(clip=clip, start=start, end=end))
        rationale = edit.get("reason")
        if rationale:
            rationales.append(f"{clip.name}: {rationale}")
    return items, rationales


def build_premiere_xml(items: list[EditItem], title: str = "PromptCut Edit") -> str:
    if not items:
        raise ValueError("The timeline is empty.")
    fps = 30
    root = ET.Element("xmeml", {"version": "5"})
    sequence = ET.SubElement(root, "sequence", {"id": "sequence-1"})
    ET.SubElement(sequence, "name").text = title
    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = str(fps)
    ET.SubElement(rate, "ntsc").text = "FALSE"
    total_frames = sum(max(1, round(item.duration * fps)) for item in items)
    ET.SubElement(sequence, "duration").text = str(total_frames)

    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    video_track = ET.SubElement(video, "track")
    audio_track = ET.SubElement(ET.SubElement(media, "audio"), "track")

    file_ids: dict[Path, str] = {}
    cursor = 0
    for index, item in enumerate(items):
        clip = item.clip
        source_id = file_ids.setdefault(clip.path, f"file-{len(file_ids) + 1}")
        duration_frames = max(1, round(item.duration * fps))
        in_frame = round(item.start * fps)
        out_frame = in_frame + duration_frames
        _append_xml_clip(
            video_track,
            clip,
            source_id,
            index,
            cursor,
            cursor + duration_frames,
            in_frame,
            out_frame,
            include_file=True,
            fps=fps,
        )
        if clip.has_audio:
            _append_xml_clip(
                audio_track,
                clip,
                source_id,
                index,
                cursor,
                cursor + duration_frames,
                in_frame,
                out_frame,
                include_file=False,
                fps=fps,
                audio=True,
            )
        cursor += duration_frames

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _append_xml_clip(
    track: ET.Element,
    clip: Clip,
    source_id: str,
    index: int,
    timeline_start: int,
    timeline_end: int,
    source_in: int,
    source_out: int,
    include_file: bool,
    fps: int,
    audio: bool = False,
) -> None:
    clipitem = ET.SubElement(track, "clipitem", {"id": f"clipitem-{index + 1}-{'a' if audio else 'v'}"})
    ET.SubElement(clipitem, "name").text = clip.name
    ET.SubElement(clipitem, "duration").text = str(timeline_end - timeline_start)
    rate = ET.SubElement(clipitem, "rate")
    ET.SubElement(rate, "timebase").text = str(fps)
    ET.SubElement(rate, "ntsc").text = "FALSE"
    ET.SubElement(clipitem, "start").text = str(timeline_start)
    ET.SubElement(clipitem, "end").text = str(timeline_end)
    ET.SubElement(clipitem, "in").text = str(source_in)
    ET.SubElement(clipitem, "out").text = str(source_out)
    if include_file:
        file_element = ET.SubElement(clipitem, "file", {"id": source_id})
        ET.SubElement(file_element, "name").text = clip.name
        ET.SubElement(file_element, "pathurl").text = clip.path.as_uri()
    else:
        ET.SubElement(clipitem, "file", {"id": source_id})
    if audio:
        ET.SubElement(clipitem, "enabled").text = "TRUE"
        ET.SubElement(clipitem, "ismasterclip").text = "TRUE"


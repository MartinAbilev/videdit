from __future__ import annotations

import base64
import os
import subprocess
import threading
import tempfile
import time
import tkinter as tk
import xml.etree.ElementTree as ET
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from editor_core import (
    Clip,
    EditItem,
    build_premiere_xml,
    extract_sample_frame,
    local_prompt_plan,
    probe_clip,
    load_project,
    save_project,
    vision_prompt_plan,
)


MEDIA_TYPES = [
    ("Video and image files", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mpg *.mpeg *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif"),
    ("All files", "*.*"),
]


class VideoEditor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PromptCut — Video Editor")
        self.geometry("1180x760")
        self.minsize(900, 600)

        self.media: list[Clip] = []
        self.timeline: list[EditItem] = []
        self.selected_index: int | None = None
        self.preview_image: tk.PhotoImage | None = None
        self._busy = False
        self.project_path: Path | None = None
        self._pixels_per_second = 60.0
        self._timeline_left = 42
        self._drag: dict[str, object] | None = None
        self._media_drag_origin: tuple[int, int] | None = None
        self._media_drag_index: int | None = None
        self._media_dragged = False
        self._playhead = 0.0
        self._playing = False
        self._preparing_playback = False
        self._playback_path: Path | None = None
        self._playback_clip: Clip | None = None
        self._playback_process: subprocess.Popen[bytes] | None = None
        self._playback_started = 0.0
        self._playback_offset = 0.0
        self._playback_after: str | None = None
        self._seek_audio_after: str | None = None
        self._playback_generation = 0
        self._frame_pending = False
        self._last_frame_request = 0.0
        self.status = tk.StringVar(value="Import videos or images to get started.")
        self.in_value = tk.StringVar(value="0.00")
        self.out_value = tk.StringVar(value="0.00")
        self.cloud_vision = tk.BooleanVar(value=False)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self, padding=(8, 8, 8, 5))
        toolbar.grid(row=0, column=0, sticky="ew")
        for label, command in (
            ("Open Project", self.open_project),
            ("Save Project", self.save_current_project),
            ("Save As", self.save_project_as),
            ("Import media", self.import_media),
            ("Add to timeline", self.add_selected_media),
            ("Remove", self.remove_timeline_item),
            ("Move up", lambda: self.move_timeline_item(-1)),
            ("Move down", lambda: self.move_timeline_item(1)),
            ("Export MP4", self.export_mp4),
            ("Premiere XML", self.export_premiere),
        ):
            ttk.Button(toolbar, text=label, command=command).pack(side="left", padx=(0, 5))

        upper = ttk.Panedwindow(self, orient="horizontal")
        upper.grid(row=1, column=0, sticky="nsew", padx=8)
        library = ttk.Frame(upper, padding=6)
        workspace = ttk.Frame(upper, padding=6)
        upper.add(library, weight=1)
        upper.add(workspace, weight=4)

        ttk.Label(library, text="MEDIA BIN").pack(anchor="w", pady=(0, 5))
        self.media_list = tk.Listbox(library, height=14, exportselection=False)
        self.media_list.pack(fill="both", expand=True)
        self.media_list.bind("<<ListboxSelect>>", self._preview_media_selection)
        self.media_list.bind("<Double-Button-1>", lambda _event: self.add_selected_media())
        self.media_list.bind("<ButtonPress-1>", self._media_drag_start)
        self.media_list.bind("<B1-Motion>", self._media_drag_motion)
        self.media_list.bind("<ButtonRelease-1>", self._media_drag_end)

        ttk.Label(library, text="PROMPT EDIT").pack(anchor="w", pady=(14, 5))
        self.prompt = tk.Text(library, height=6, wrap="word")
        self.prompt.pack(fill="x")
        self.prompt.insert("1.0", "Create a short, energetic edit with the best moments.")
        ttk.Checkbutton(
            library,
            text="Use cloud vision (uploads sample frames)",
            variable=self.cloud_vision,
        ).pack(anchor="w", pady=(7, 2))
        ttk.Label(
            library,
            text="Requires OPENAI_API_KEY. Leave unchecked for local filename/duration matching.",
            wraplength=225,
        ).pack(anchor="w")
        self.generate_button = ttk.Button(
            library, text="Generate edit from prompt", command=self.generate_edit
        )
        self.generate_button.pack(fill="x", pady=(8, 0))

        preview_and_timeline = ttk.Panedwindow(workspace, orient="vertical")
        preview_and_timeline.pack(fill="both", expand=True)
        preview_frame = ttk.Frame(preview_and_timeline)
        timeline_frame = ttk.Frame(preview_and_timeline)
        preview_and_timeline.add(preview_frame, weight=3)
        preview_and_timeline.add(timeline_frame, weight=2)

        ttk.Label(preview_frame, text="PREVIEW").pack(anchor="w", pady=(0, 4))
        self.preview = tk.Canvas(
            preview_frame, background="#17191d", highlightthickness=0, height=280
        )
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Configure>", self._draw_preview)

        timeline_header = ttk.Frame(timeline_frame)
        timeline_header.pack(fill="x", pady=(8, 4))
        ttk.Label(timeline_header, text="TIMELINE").pack(side="left")
        self.play_button = ttk.Button(timeline_header, text="Play", command=self.toggle_playback)
        self.play_button.pack(side="left", padx=(12, 3))
        ttk.Button(timeline_header, text="Stop", command=self.stop_playback).pack(side="left", padx=3)
        ttk.Button(timeline_header, text="−", width=3, command=lambda: self.zoom_timeline(0.8)).pack(
            side="right", padx=(3, 0)
        )
        ttk.Button(timeline_header, text="+", width=3, command=lambda: self.zoom_timeline(1.25)).pack(
            side="right"
        )

        canvas_frame = ttk.Frame(timeline_frame)
        canvas_frame.pack(fill="both", expand=True)
        self.timeline_canvas = tk.Canvas(
            canvas_frame, background="#202329", height=145, highlightthickness=0
        )
        self.timeline_scrollbar = ttk.Scrollbar(
            canvas_frame, orient="horizontal", command=self.timeline_canvas.xview
        )
        self.timeline_canvas.configure(xscrollcommand=self.timeline_scrollbar.set)
        self.timeline_canvas.grid(row=0, column=0, sticky="nsew")
        self.timeline_scrollbar.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)
        self.timeline_canvas.bind("<Configure>", self._draw_timeline)
        self.timeline_canvas.bind("<ButtonPress-1>", self._timeline_mouse_down)
        self.timeline_canvas.bind("<B1-Motion>", self._timeline_mouse_move)
        self.timeline_canvas.bind("<ButtonRelease-1>", self._timeline_mouse_up)

        trim = ttk.Frame(timeline_frame, padding=(0, 6, 0, 0))
        trim.pack(fill="x")
        ttk.Label(trim, text="Selected clip in:").pack(side="left")
        ttk.Entry(trim, textvariable=self.in_value, width=8).pack(side="left", padx=(4, 10))
        ttk.Label(trim, text="out:").pack(side="left")
        ttk.Entry(trim, textvariable=self.out_value, width=8).pack(side="left", padx=4)
        ttk.Button(trim, text="Apply trim", command=self.apply_trim).pack(side="left", padx=5)

        ttk.Label(self, textvariable=self.status, anchor="w", padding=(9, 5)).grid(
            row=2, column=0, sticky="ew"
        )

    def import_media(self) -> None:
        paths = filedialog.askopenfilenames(title="Import video and image files", filetypes=MEDIA_TYPES)
        added = 0
        for raw_path in paths:
            path = Path(raw_path)
            if any(clip.path == path.resolve() for clip in self.media):
                continue
            try:
                clip = probe_clip(path)
            except (OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("Could not import media", str(error))
                continue
            self.media.append(clip)
            self.media_list.insert("end", f"{clip.name}  ({clip.duration:.1f}s)")
            added += 1
        if added:
            self.status.set(f"Imported {added} media file(s).")

    def open_project(self) -> None:
        filename = filedialog.askopenfilename(
            title="Open PromptCut project",
            filetypes=[("PromptCut project", "*.json"), ("JSON files", "*.json")],
        )
        if not filename:
            return
        try:
            media, timeline, prompt = load_project(Path(filename))
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not open project", str(error))
            return
        self._invalidate_playback()
        self.project_path = Path(filename).resolve()
        self.media = media
        self.timeline = timeline
        self.selected_index = None
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", prompt)
        self.media_list.delete(0, "end")
        for clip in media:
            self.media_list.insert("end", f"{clip.name}  ({clip.duration:.1f}s)")
        self._refresh_timeline(0 if timeline else None)
        self.title(f"PromptCut — {self.project_path.name}")
        self.status.set(f"Opened project: {self.project_path}")

    def save_current_project(self) -> None:
        if self.project_path is None:
            self.save_project_as()
            return
        self._write_project(self.project_path)

    def save_project_as(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Save PromptCut project",
            defaultextension=".json",
            filetypes=[("PromptCut project", "*.json")],
        )
        if not filename:
            return
        path = Path(filename).resolve()
        self._write_project(path)

    def _write_project(self, path: Path) -> None:
        try:
            save_project(path, self.media, self.timeline, self.prompt.get("1.0", "end").strip())
        except (OSError, ValueError) as error:
            messagebox.showerror("Could not save project", str(error))
            return
        self.project_path = path
        self.title(f"PromptCut — {path.name}")
        self.status.set(f"Project saved: {path}")

    def add_selected_media(self) -> None:
        selection = self.media_list.curselection()
        if not selection:
            self.status.set("Select a media file first.")
            return
        self.add_media_to_timeline(selection[0])

    def add_media_to_timeline(self, media_index: int, insert_at: int | None = None) -> None:
        if not 0 <= media_index < len(self.media):
            return
        self._invalidate_playback()
        item = EditItem(self.media[media_index])
        if insert_at is None:
            self.timeline.append(item)
            insert_at = len(self.timeline) - 1
        else:
            insert_at = max(0, min(insert_at, len(self.timeline)))
            self.timeline.insert(insert_at, item)
        self._refresh_timeline(insert_at)

    def remove_timeline_item(self) -> None:
        index = self._selected_timeline_index()
        if index is None:
            return
        self._invalidate_playback()
        self.timeline.pop(index)
        self._refresh_timeline(min(index, len(self.timeline) - 1))

    def move_timeline_item(self, offset: int) -> None:
        index = self._selected_timeline_index()
        if index is None:
            return
        target = index + offset
        if target < 0 or target >= len(self.timeline):
            return
        self._invalidate_playback()
        self.timeline[index], self.timeline[target] = self.timeline[target], self.timeline[index]
        self._refresh_timeline(target)

    def apply_trim(self) -> None:
        index = self._selected_timeline_index()
        if index is None:
            return
        try:
            start, end = float(self.in_value.get()), float(self.out_value.get())
        except ValueError:
            messagebox.showerror("Invalid trim", "In and out points must be numbers in seconds.")
            return
        item = self.timeline[index]
        if not 0 <= start < end <= item.clip.duration:
            messagebox.showerror(
                "Invalid trim",
                f"Choose times where 0 <= in < out <= {item.clip.duration:.2f} seconds.",
            )
            return
        self._invalidate_playback()
        item.start, item.end = start, end
        self._refresh_timeline(index)

    def _refresh_timeline(self, selected: int | None = None) -> None:
        if not self.timeline:
            self.selected_index = None
            self._playhead = 0.0
        elif selected is not None and selected >= 0:
            self.selected_index = min(selected, len(self.timeline) - 1)
        elif self.selected_index is not None:
            self.selected_index = min(self.selected_index, len(self.timeline) - 1)
        if self.timeline:
            self._playhead = min(self._playhead, sum(item.duration for item in self.timeline))
        self._draw_timeline()
        if self.selected_index is not None:
            self._show_edit_item(self.selected_index)
        self.status.set(f"Timeline: {len(self.timeline)} clip(s), {sum(i.duration for i in self.timeline):.1f}s.")

    def _selected_timeline_index(self) -> int | None:
        return self.selected_index

    def _show_edit_item(self, index: int) -> None:
        if not 0 <= index < len(self.timeline):
            return
        item = self.timeline[index]
        self.in_value.set(f"{item.start:.2f}")
        self.out_value.set(f"{item.end:.2f}")
        self._show_clip(item.clip, min(item.start, max(0.0, item.clip.duration - 0.05)))

    def _timeline_positions(self) -> list[tuple[float, float]]:
        positions = []
        cursor = float(self._timeline_left)
        for item in self.timeline:
            end = cursor + item.duration * self._pixels_per_second
            positions.append((cursor, end))
            cursor = end
        return positions

    def _draw_timeline(self, _event: tk.Event | None = None) -> None:
        if not hasattr(self, "timeline_canvas"):
            return
        canvas = self.timeline_canvas
        canvas.delete("all")
        height = max(130, canvas.winfo_height())
        positions = self._timeline_positions()
        total_width = positions[-1][1] if positions else self._timeline_left
        width = max(canvas.winfo_width(), int(total_width + 60), 500)
        canvas.configure(scrollregion=(0, 0, width, height))
        canvas.create_rectangle(0, 0, width, height, fill="#202329", outline="")

        ruler_y = 25
        canvas.create_line(0, ruler_y, width, ruler_y, fill="#59616d")
        total_duration = sum(item.duration for item in self.timeline)
        tick_seconds = max(1, round(48 / self._pixels_per_second))
        for second in range(0, int(total_duration) + tick_seconds + 1, tick_seconds):
            x = self._timeline_left + second * self._pixels_per_second
            canvas.create_line(x, ruler_y - 4, x, ruler_y + 5, fill="#9299a3")
            canvas.create_text(
                x + 3,
                8,
                text=f"{second // 60:02d}:{second % 60:02d}",
                anchor="w",
                fill="#c6cbd2",
                font=("Segoe UI", 8),
            )

        track_top, track_bottom = 39, min(height - 10, 112)
        canvas.create_rectangle(
            self._timeline_left,
            track_top,
            max(self._timeline_left + 1, width - 18),
            track_bottom,
            fill="#292d34",
            outline="#414852",
        )
        colors = ("#386c9a", "#477c5c", "#835c91", "#a46b3d")
        for index, (item, (start_x, end_x)) in enumerate(zip(self.timeline, positions)):
            tag = f"clip_{index}"
            selected = index == self.selected_index
            fill = "#e0a43b" if selected else colors[index % len(colors)]
            canvas.create_rectangle(
                start_x + 1,
                track_top + 2,
                max(start_x + 4, end_x - 1),
                track_bottom - 2,
                fill=fill,
                outline="#f2d89e" if selected else "#a7b3c2",
                width=2 if selected else 1,
                tags=(tag,),
            )
            canvas.create_text(
                start_x + 12,
                (track_top + track_bottom) / 2,
                text=item.clip.name,
                anchor="w",
                fill="white",
                font=("Segoe UI", 9),
                tags=(tag,),
            )
            canvas.create_rectangle(
                start_x + 1,
                track_top + 2,
                start_x + 8,
                track_bottom - 2,
                fill="#e9edf2",
                outline="",
                tags=(tag,),
            )
            canvas.create_rectangle(
                end_x - 8,
                track_top + 2,
                end_x - 1,
                track_bottom - 2,
                fill="#e9edf2",
                outline="",
                tags=(tag,),
            )
            if end_x - start_x > 130:
                canvas.create_text(
                    start_x + 12,
                    track_bottom - 11,
                    text=f"{item.start:.1f}s – {item.end:.1f}s",
                    anchor="w",
                    fill="#e7e9ec",
                    font=("Segoe UI", 8),
                    tags=(tag,),
                )

        playhead_x = self._timeline_left + self._playhead * self._pixels_per_second
        canvas.create_line(
            playhead_x,
            ruler_y,
            playhead_x,
            track_bottom + 7,
            fill="#ff5454",
            width=2,
            tags=("playhead", "playhead_line"),
        )
        canvas.create_polygon(
            playhead_x - 5,
            ruler_y - 6,
            playhead_x + 5,
            ruler_y - 6,
            playhead_x,
            ruler_y + 1,
            fill="#ff5454",
            outline="",
            tags=("playhead", "playhead_head"),
        )

    def _update_playhead(self) -> None:
        playhead_x = self._timeline_left + self._playhead * self._pixels_per_second
        canvas = self.timeline_canvas
        line = canvas.find_withtag("playhead_line")
        if line:
            _, y1, _, y2 = canvas.coords(line[0])
            canvas.coords(line[0], playhead_x, y1, playhead_x, y2)
        head = canvas.find_withtag("playhead_head")
        if head:
            canvas.coords(
                head[0],
                playhead_x - 5,
                19,
                playhead_x + 5,
                19,
                playhead_x,
                26,
            )

    def zoom_timeline(self, factor: float) -> None:
        self._pixels_per_second = max(18.0, min(240.0, self._pixels_per_second * factor))
        self._draw_timeline()

    def _timeline_mouse_down(self, event: tk.Event) -> None:
        x = self.timeline_canvas.canvasx(event.x)
        y = event.y
        positions = self._timeline_positions()
        track_top, track_bottom = 39, min(max(130, self.timeline_canvas.winfo_height()) - 10, 112)
        for index, (start_x, end_x) in enumerate(positions):
            if start_x <= x <= end_x and track_top <= y <= track_bottom:
                self.selected_index = index
                self._show_edit_item(index)
                mode = "left" if x <= start_x + 10 else "right" if x >= end_x - 10 else "move"
                item = self.timeline[index]
                self._drag = {
                    "index": index,
                    "mode": mode,
                    "start_x": x,
                    "last_x": x,
                    "start": item.start,
                    "end": item.end,
                    "moved": False,
                }
                self._draw_timeline()
                return

        if x >= self._timeline_left:
            self._seek_timeline((x - self._timeline_left) / self._pixels_per_second)
            self._drag = {"mode": "seek"}

    def _timeline_mouse_move(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        mode = str(self._drag.get("mode"))
        if mode == "seek":
            x = self.timeline_canvas.canvasx(event.x)
            self._seek_timeline((x - self._timeline_left) / self._pixels_per_second)
            return

        index = int(self._drag["index"])
        x = self.timeline_canvas.canvasx(event.x)
        start_x = float(self._drag["start_x"])
        if abs(x - start_x) >= 3:
            self._drag["moved"] = True
        if not self._drag["moved"]:
            return
        canvas = self.timeline_canvas
        canvas.delete("drag_indicator")
        if mode == "move":
            delta_x = x - float(self._drag["last_x"])
            canvas.move(f"clip_{index}", delta_x, 0)
            self._drag["last_x"] = x
        else:
            delta = (x - start_x) / self._pixels_per_second
            item = self.timeline[index]
            if mode == "left":
                marker_x = self._timeline_positions()[index][0] + delta * self._pixels_per_second
            else:
                marker_x = self._timeline_positions()[index][1] + delta * self._pixels_per_second
            canvas.create_line(
                marker_x,
                35,
                marker_x,
                120,
                fill="#ffffff",
                dash=(3, 2),
                tags=("drag_indicator",),
            )
            original = float(self._drag["start"] if mode == "left" else self._drag["end"])
            self.status.set(f"Trim {item.clip.name}: {max(0.0, original + delta):.2f}s")

    def _timeline_mouse_up(self, event: tk.Event) -> None:
        if self._drag is None:
            return
        drag = self._drag
        self._drag = None
        mode = str(drag.get("mode"))
        if mode == "seek":
            return
        index = int(drag["index"])
        if not 0 <= index < len(self.timeline):
            self._draw_timeline()
            return
        if not drag["moved"]:
            self._draw_timeline()
            return

        x = self.timeline_canvas.canvasx(event.x)
        item = self.timeline[index]
        if mode == "move":
            remaining = [entry for i, entry in enumerate(self.timeline) if i != index]
            other_centers = [
                (left + right) / 2
                for i, (left, right) in enumerate(self._timeline_positions())
                if i != index
            ]
            insert_at = sum(center < x for center in other_centers)
            self._invalidate_playback()
            self.timeline = remaining
            self.timeline.insert(insert_at, item)
            self._refresh_timeline(insert_at)
            return

        delta = (x - float(drag["start_x"])) / self._pixels_per_second
        minimum = min(0.05, item.clip.duration)
        if mode == "left":
            new_start = max(0.0, min(float(drag["start"]) + delta, item.end - minimum))
            changed = new_start != item.start
            item.start = new_start
        else:
            new_end = min(
                item.clip.duration,
                max(float(drag["end"]) + delta, item.start + minimum),
            )
            changed = new_end != item.end
            item.end = new_end
        if changed:
            self._invalidate_playback()
        self._refresh_timeline(index)

    def _media_drag_start(self, event: tk.Event) -> None:
        self._media_drag_origin = (event.x_root, event.y_root)
        self._media_drag_index = self.media_list.nearest(event.y)
        self._media_dragged = False

    def _media_drag_motion(self, event: tk.Event) -> None:
        if self._media_drag_origin is None:
            return
        origin_x, origin_y = self._media_drag_origin
        if abs(event.x_root - origin_x) + abs(event.y_root - origin_y) > 8:
            self._media_dragged = True
            self._media_drag_index = self.media_list.nearest(event.y)
            self.status.set("Drop media onto the timeline to insert it.")

    def _media_drag_end(self, event: tk.Event) -> None:
        if self._media_dragged and self._media_drag_index is not None:
            left = self.timeline_canvas.winfo_rootx()
            top = self.timeline_canvas.winfo_rooty()
            right = left + self.timeline_canvas.winfo_width()
            bottom = top + self.timeline_canvas.winfo_height()
            if left <= event.x_root <= right and top <= event.y_root <= bottom:
                canvas_x = self.timeline_canvas.canvasx(event.x_root - left)
                positions = self._timeline_positions()
                insert_at = sum((start + end) / 2 < canvas_x for start, end in positions)
                self.add_media_to_timeline(self._media_drag_index, insert_at)
        self._media_drag_origin = None
        self._media_drag_index = None
        self._media_dragged = False

    def _preview_media_selection(self, _event: tk.Event) -> None:
        selection = self.media_list.curselection()
        if selection:
            self._show_clip(self.media[selection[0]], 0.0)

    def toggle_playback(self) -> None:
        if self._preparing_playback:
            return
        if self._playing:
            self._pause_playback()
            return
        if not self.timeline:
            messagebox.showerror("Empty timeline", "Add clips before playing the sequence.")
            return
        if self._playback_path is None:
            self._prepare_playback()
            return
        self._start_playback()

    def _prepare_playback(self) -> None:
        descriptor, filename = tempfile.mkstemp(suffix=".mp4", prefix="promptcut-preview-")
        os.close(descriptor)
        path = Path(filename)
        items = [EditItem(item.clip, item.start, item.end) for item in self.timeline]
        generation = self._playback_generation
        self._preparing_playback = True
        self.play_button.configure(state="disabled")
        self.status.set("Preparing sequence playback…")

        def worker() -> None:
            try:
                self._render_mp4(items, path)
                self.after(0, lambda: self._playback_ready(path, generation))
            except (OSError, RuntimeError, ValueError) as error:
                path.unlink(missing_ok=True)
                self.after(0, lambda: self._playback_prepare_failed(str(error), generation))

        threading.Thread(target=worker, daemon=True).start()

    def _playback_ready(self, path: Path, generation: int) -> None:
        if generation != self._playback_generation:
            path.unlink(missing_ok=True)
            return
        self._preparing_playback = False
        self.play_button.configure(state="normal")
        self._playback_path = path
        duration = sum(item.duration for item in self.timeline)
        self._playback_clip = Clip(path, duration, False, True, 1280, 720)
        self._playhead = min(self._playhead, duration)
        self._draw_timeline()
        self._start_playback()

    def _playback_prepare_failed(self, details: str, generation: int) -> None:
        if generation != self._playback_generation:
            return
        self._preparing_playback = False
        self.play_button.configure(state="normal")
        self._show_error("Could not prepare playback", details)

    def _start_playback(self) -> None:
        if self._playback_path is None:
            return
        try:
            self._start_playback_audio()
        except OSError as error:
            self._show_error("Could not start playback", str(error))
            return
        self._playing = True
        self._playback_offset = self._playhead
        self._playback_started = time.monotonic()
        self.play_button.configure(text="Pause")
        self.status.set("Playing sequence.")
        self._schedule_playback_tick()

    def _start_playback_audio(self) -> None:
        if self._playback_path is None:
            return
        self._stop_playback_audio()
        self._playback_process = subprocess.Popen(
            [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-ss",
                f"{self._playhead:.3f}",
                "-i",
                str(self._playback_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _stop_playback_audio(self) -> None:
        process = self._playback_process
        self._playback_process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _schedule_playback_tick(self) -> None:
        if self._playing:
            self._playback_after = self.after(40, self._playback_tick)

    def _playback_tick(self) -> None:
        self._playback_after = None
        if not self._playing:
            return
        duration = sum(item.duration for item in self.timeline)
        self._playhead = min(
            duration,
            self._playback_offset + time.monotonic() - self._playback_started,
        )
        self._update_playhead()
        now = time.monotonic()
        if (
            self._playback_clip is not None
            and not self._frame_pending
            and now - self._last_frame_request >= 0.3
        ):
            self._request_playback_frame(self._playhead)
            self._last_frame_request = now
        if self._playhead >= duration:
            self.stop_playback()
            return
        self._schedule_playback_tick()

    def _request_playback_frame(self, at_seconds: float) -> None:
        clip = self._playback_clip
        if clip is None:
            return
        self._frame_pending = True
        generation = self._playback_generation
        at_seconds = min(at_seconds, max(0.0, clip.duration - 0.05))

        def worker() -> None:
            try:
                frame = extract_sample_frame(clip, at_seconds)
            except (OSError, RuntimeError):
                self.after(0, lambda: self._finish_playback_frame(generation, None))
                return
            self.after(0, lambda: self._finish_playback_frame(generation, frame))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_playback_frame(self, generation: int, frame: bytes | None) -> None:
        self._frame_pending = False
        if generation != self._playback_generation or frame is None:
            return
        self._set_preview(frame, "Sequence playback", update_status=False)

    def _pause_playback(self) -> None:
        if self._playing:
            self._playhead = min(
                sum(item.duration for item in self.timeline),
                self._playback_offset + time.monotonic() - self._playback_started,
            )
        self._playing = False
        if self._playback_after is not None:
            self.after_cancel(self._playback_after)
            self._playback_after = None
        self._stop_playback_audio()
        self.play_button.configure(text="Play")
        self._update_playhead()
        self.status.set(f"Paused at {self._playhead:.2f}s.")

    def stop_playback(self) -> None:
        if self._preparing_playback:
            self._invalidate_playback()
            self.status.set("Playback preparation cancelled.")
            return
        self._playing = False
        if self._playback_after is not None:
            self.after_cancel(self._playback_after)
            self._playback_after = None
        if self._seek_audio_after is not None:
            self.after_cancel(self._seek_audio_after)
            self._seek_audio_after = None
        self._stop_playback_audio()
        self._playhead = 0.0
        self.play_button.configure(text="Play")
        self._update_playhead()
        if self.timeline:
            self._preview_timeline_time(0.0)
        self.status.set("Playback stopped.")

    def _seek_timeline(self, at_seconds: float) -> None:
        duration = sum(item.duration for item in self.timeline)
        self._playhead = max(0.0, min(at_seconds, duration))
        self._update_playhead()
        if self._playing:
            self._playback_offset = self._playhead
            self._playback_started = time.monotonic()
            if self._seek_audio_after is not None:
                self.after_cancel(self._seek_audio_after)
            self._seek_audio_after = self.after(180, self._restart_playback_audio)
        else:
            self._preview_timeline_time(self._playhead)

    def _restart_playback_audio(self) -> None:
        self._seek_audio_after = None
        if not self._playing:
            return
        try:
            self._start_playback_audio()
        except OSError as error:
            self._pause_playback()
            self._show_error("Could not seek playback", str(error))
        self._playback_offset = self._playhead
        self._playback_started = time.monotonic()

    def _preview_timeline_time(self, at_seconds: float) -> None:
        cursor = 0.0
        for item in self.timeline:
            if at_seconds <= cursor + item.duration:
                source_time = item.start + max(0.0, at_seconds - cursor)
                source_time = min(source_time, max(0.0, item.clip.duration - 0.05))
                self._show_clip(item.clip, source_time)
                return
            cursor += item.duration
        if self.timeline:
            last = self.timeline[-1]
            self._show_clip(last.clip, max(0.0, last.end - 0.05))

    def _invalidate_playback(self) -> None:
        self._playback_generation += 1
        self._preparing_playback = False
        self._playing = False
        if self._playback_after is not None:
            self.after_cancel(self._playback_after)
            self._playback_after = None
        if self._seek_audio_after is not None:
            self.after_cancel(self._seek_audio_after)
            self._seek_audio_after = None
        self._stop_playback_audio()
        self._playback_clip = None
        if self._playback_path is not None:
            self._playback_path.unlink(missing_ok=True)
            self._playback_path = None
        if hasattr(self, "play_button"):
            self.play_button.configure(text="Play", state="normal")

    def close_app(self) -> None:
        self._invalidate_playback()
        self.destroy()

    def _show_clip(self, clip: Clip, at_seconds: float) -> None:
        self.status.set(f"Loading preview: {clip.name}")

        def worker() -> None:
            try:
                frame = extract_sample_frame(clip, at_seconds)
            except (OSError, RuntimeError) as error:
                self.after(0, lambda: self._show_error("Preview failed", str(error)))
                return
            self.after(0, lambda: self._set_preview(frame, clip.name))

        threading.Thread(target=worker, daemon=True).start()

    def _set_preview(self, frame: bytes, name: str, update_status: bool = True) -> None:
        try:
            image = tk.PhotoImage(data=base64.b64encode(frame).decode("ascii"), format="png")
        except tk.TclError as error:
            self._show_error("Preview failed", str(error))
            return
        self.preview_image = image
        self._draw_preview()
        if update_status:
            self.status.set(f"Previewing {name}")

    def _draw_preview(self, _event: tk.Event | None = None) -> None:
        self.preview.delete("all")
        if self.preview_image is None:
            self.preview.create_text(
                max(1, self.preview.winfo_width() // 2),
                max(1, self.preview.winfo_height() // 2),
                text="Select a video or image to preview",
                fill="#d7dbe0",
            )
            return
        image = self.preview_image
        width, height = max(1, self.preview.winfo_width()), max(1, self.preview.winfo_height())
        factor = max(image.width() / width, image.height() / height, 1)
        if factor > 1:
            image = image.subsample(int(factor), int(factor))
        self.preview.create_image(width // 2, height // 2, image=image, anchor="center")

    def generate_edit(self) -> None:
        if self._busy:
            return
        prompt = self.prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showerror("Missing prompt", "Describe the edit you want.")
            return
        clips = list(self.media)
        if not clips:
            messagebox.showerror("No media", "Import at least one video or image first.")
            return

        use_cloud = self.cloud_vision.get()
        if use_cloud:
            if not os.environ.get("OPENAI_API_KEY"):
                messagebox.showerror(
                    "Missing API key",
                    "Set OPENAI_API_KEY in the environment before using cloud vision.",
                )
                return
            endpoint = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            if not messagebox.askyesno(
                "Upload sample frames?",
                f"Cloud vision will upload up to three sampled frames per asset to "
                f"{endpoint}. Continue?",
            ):
                return

        self._set_busy(True, "Analyzing media and creating an edit…")

        def worker() -> None:
            try:
                if use_cloud:
                    items, reasons = vision_prompt_plan(
                        clips,
                        prompt,
                        os.environ["OPENAI_API_KEY"],
                        os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                    )
                else:
                    items = local_prompt_plan(clips, prompt)
                    reasons = []
                self.after(0, lambda: self._apply_generated(items, reasons, use_cloud))
            except (OSError, RuntimeError, ValueError) as error:
                self.after(0, lambda: self._finish_with_error("Could not generate edit", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_generated(self, items: list[EditItem], reasons: list[str], cloud: bool) -> None:
        self.timeline = items
        self._refresh_timeline(0 if items else None)
        source = "Cloud vision" if cloud else "Local filename/duration matching"
        self.status.set(f"{source} generated {len(items)} clip(s).")
        if reasons:
            messagebox.showinfo("Edit notes", "\n".join(reasons[:8]))
        self._set_busy(False)

    def export_mp4(self) -> None:
        if not self.timeline:
            messagebox.showerror("Empty timeline", "Add clips to the timeline before exporting.")
            return
        output = filedialog.asksaveasfilename(
            title="Export final video",
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if not output:
            return
        items = [EditItem(item.clip, item.start, item.end) for item in self.timeline]
        self._set_busy(True, "Rendering MP4…")

        def worker() -> None:
            try:
                self._render_mp4(items, Path(output))
                self.after(0, lambda: self._finish_export(output))
            except (OSError, RuntimeError, ValueError) as error:
                self.after(0, lambda: self._finish_with_error("MP4 export failed", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_mp4(self, items: list[EditItem], output: Path) -> None:
        command = ["ffmpeg", "-y", "-v", "error"]
        filters: list[str] = []
        concat_inputs: list[str] = []
        for index, item in enumerate(items):
            duration = item.duration
            if item.clip.is_image:
                command.extend(["-loop", "1", "-framerate", "30", "-t", f"{duration:.6f}"])
            else:
                command.extend(["-ss", f"{item.start:.6f}", "-t", f"{duration:.6f}"])
            command.extend(["-i", str(item.clip.path)])
            filters.append(
                f"[{index}:v:0]setpts=PTS-STARTPTS,"
                "scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,"
                f"trim=duration={duration:.6f},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
            )
            if item.clip.has_audio:
                filters.append(
                    f"[{index}:a:0]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,"
                    f"aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}]"
                )
            else:
                filters.append(
                    f"anullsrc=r=44100:cl=stereo,atrim=duration={duration:.6f},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
            concat_inputs.extend([f"[v{index}]", f"[a{index}]"])

        filters.append(
            "".join(concat_inputs)
            + f"concat=n={len(items)}:v=1:a=1[vout][aout]"
        )
        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "FFmpeg could not render the timeline.")

    def export_premiere(self) -> None:
        if not self.timeline:
            messagebox.showerror("Empty timeline", "Add clips to the timeline before exporting.")
            return
        output = filedialog.asksaveasfilename(
            title="Export Premiere-compatible edit",
            defaultextension=".xml",
            filetypes=[("Premiere XML", "*.xml")],
        )
        if not output:
            return
        try:
            xml_text = build_premiere_xml(self.timeline)
            ET.fromstring(xml_text)
            Path(output).write_text(xml_text, encoding="utf-8")
        except (OSError, ValueError, ET.ParseError) as error:
            self._show_error("Premiere XML export failed", str(error))
            return
        self.status.set(f"Premiere XML saved: {output}")
        messagebox.showinfo(
            "Premiere XML exported",
            "Import the XML file into Premiere Pro. Media paths point to the original files.",
        )

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        self.generate_button.configure(state="disabled" if busy else "normal")
        if status:
            self.status.set(status)

    def _finish_export(self, output: str) -> None:
        self._set_busy(False, f"MP4 exported: {output}")
        messagebox.showinfo("Export complete", f"Saved final video to:\n{output}")

    def _finish_with_error(self, title: str, details: str) -> None:
        self._set_busy(False, title)
        self._show_error(title, details)

    def _show_error(self, title: str, details: str) -> None:
        messagebox.showerror(title, details)


if __name__ == "__main__":
    VideoEditor().mainloop()


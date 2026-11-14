# PromptCut

PromptCut is a small Python desktop video editor for arranging imported footage and still images, trimming clips, generating a prompt-based cut, and exporting an MP4 or a Premiere Pro-importable XML timeline.

## Requirements

- Python 3.10+
- FFmpeg, FFprobe, and FFplay available on `PATH`
- Tkinter (included with most standard Python installations)

No Python packages need to be installed.

## Run

```powershell
python main.py
```

Import supported video and image files, then drag items from the media bin into the timeline or use **Add to timeline**. Drag a clip body to reorder it; drag its left/right handles to trim the source in/out points. Click the ruler or timeline to seek. **Stop** pauses at the current time; **|◀** rewinds to the start, and the frame buttons step one frame at a time. Playback starts immediately and decodes preview frames from the active source clip; MP4 rendering only happens during export. Zoom with `+`/`−` and use the horizontal scrollbar for longer edits.

Timeline keyboard shortcuts: **Space** play/pause, **J** or **Left Arrow** previous frame, **Right Arrow** next frame, **K** pause, **L** play, **Home** rewind to start, **End** go to the last frame, **A** or **Ctrl+K** split the clip at the playhead, and **Delete** remove the selected timeline clip. Shortcuts are disabled while editing text fields.

Use **Save Project** or **Save As** to save an editable `.json` project, and **Open Project** to restore it. Media paths are stored relative to the project file when possible; keep the referenced footage in place or move it together with the project. MP4 export normalizes the picture to 1280x720 at 30 fps and includes source audio where available; silent clips receive silence so the sequence stays continuous.

## Prompt edits

With cloud vision unchecked, the prompt editor uses filenames and durations to order assets and can make a short/fast cut. This local mode does not understand image content.

For visual analysis, set `OPENAI_API_KEY`, optionally set `OPENAI_MODEL` and `OPENAI_BASE_URL`, and enable **Use GPT vision API**. OpenAI is the API provider; GPT is the model family. On startup, the app reads these values from a `.env` file next to `main.py`; non-empty system environment variables take precedence. The app asks before uploading up to three sampled frames per video (one per still image), plus filenames and media metadata, to the configured endpoint. The vision service returns a clip order and trims. The timeline editor validates and displays the generated cut before export. HTTP 429 errors include rate-limit guidance; try again later, check API usage/billing limits, or reduce the amount of footage sent for analysis.

## Premiere Pro

**Premiere XML** exports a Final Cut Pro 7 XML timeline, which Premiere Pro can import. It references the original media at its current file paths; keep those files available when opening the XML.

Run the focused tests with:

```powershell
python -m unittest -v
```

"""Native plain-text offline surface: no HTTP server, webview, HTML or links."""
from __future__ import annotations

from pathlib import Path
import base64
import json
import os
import queue
import struct
import threading

from .offline_session import OfflineSession


def local_preview(line, workspace):
    """Accept only bounded PNG bytes from this session's output directory."""
    prefix = "KAROSPACE_PREVIEW_IMG "
    if not line.startswith(prefix):
        return None
    try:
        meta = json.loads(line[len(prefix):])
        path = Path(meta["path"]).resolve()
        root = (Path(workspace) / "output").resolve()
        if root not in path.parents or path.suffix.lower() != ".png":
            return None
        with path.open("rb") as source:
            data = source.read(8 * 1024 * 1024 + 1)
        if len(data) < 24 or len(data) > 8 * 1024 * 1024 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", data[16:24])
        if not 0 < width <= 4096 or not 0 < height <= 4096:
            return None
        return base64.b64encode(data)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def opening(config):
    if config.get("input_path"):
        return "Input file: " + config["input_path"] + "\n" + (config.get("intent") or "Inspect this dataset and help me build a viewer.")
    return config.get("intent", "")


def run_chat(config):
    print("Offline • CPU model • Network blocked • No cloud fallback", flush=True)
    session = OfflineSession(config, on_progress=lambda stream, line: print(line, end="", flush=True))
    first = opening(config)
    if first:
        print("Opening request (local only):\n" + first)
        if input("Process this locally? [y/N] ").strip().lower() == "y":
            session.send(first)
    while True:
        try:
            text = input("You: ").strip()
        except EOFError:
            return
        if text in {"/quit", "/exit"}:
            return
        if text:
            session.send(text)


def run_app(config):
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    window = tk.Tk()
    window.title("KaroSpace — Offline")
    window.geometry("1000x760")
    window.configure(padx=18, pady=14)
    ttk.Label(window, text="KaroSpace · Offline", font=("Helvetica", 19, "bold")).pack(anchor="w")
    ttk.Label(window, text="Local CPU model · Network blocked · Files stay in this session folder").pack(anchor="w", pady=(4, 10))
    transcript = ScrolledText(window, wrap="word", state="disabled", font=("Helvetica", 13))
    transcript.pack(fill="both", expand=True)
    status = tk.StringVar(value="Loading the local model…")
    ttk.Label(window, textvariable=status).pack(anchor="w", pady=8)
    composer = tk.Text(window, height=5, wrap="word", font=("Helvetica", 13))
    composer.pack(fill="x")
    composer.insert("1.0", opening(config))
    row = ttk.Frame(window)
    row.pack(fill="x", pady=(10, 0))
    ttk.Label(row, text="CPU replies may take a while. Closing stops this session and its tools.").pack(side="left")
    events = queue.Queue(maxsize=1000)
    images = []
    state = {"session": None, "busy": True}

    def close():
        window.destroy()
        # A native inference thread may be in C code. Exit the confined process
        # immediately; its launcher then terminates the whole scientific group.
        os._exit(0)

    window.protocol("WM_DELETE_WINDOW", close)

    def append(text):
        transcript.configure(state="normal")
        transcript.insert("end", text + "\n\n")
        # Bound the presentation memory. No transcript is written to disk.
        if int(transcript.index("end-1c").split(".")[0]) > 5000:
            transcript.delete("1.0", "1000.0")
        transcript.configure(state="disabled")
        transcript.see("end")

    def work(text=None):
        try:
            if state["session"] is None:
                state["session"] = OfflineSession(
                    config, on_event=lambda text: events.put(("reply", text)),
                    on_progress=progress)
            if text is not None:
                state["session"].send(text)
            events.put(("ready", "Ready · Offline"))
        except Exception as exc:
            events.put(("error", f"Local operation failed: {type(exc).__name__}: {exc}"))

    def progress(stream, line):
        preview = local_preview(line, config["workspace"]) if stream == "stdout" else None
        events.put(("image", preview) if preview is not None else ("progress", line.rstrip()))

    def send():
        if state["busy"]:
            return
        text = composer.get("1.0", "end").strip()
        if not text:
            return
        state["busy"] = True
        send_button.configure(state="disabled")
        composer.delete("1.0", "end")
        append("You\n" + text)
        status.set("Working locally…")
        threading.Thread(target=work, args=(text,), daemon=True).start()

    send_button = ttk.Button(row, text="Send locally", command=send, state="disabled")
    send_button.pack(side="right")
    ttk.Button(row, text="Stop and close", command=close).pack(side="right", padx=8)
    append("Session files\n" + str(Path(config["workspace"])) +
           "\n\nThis window displays plain text only. It does not open external links or viewers.")

    def poll():
        for _ in range(100):
            try:
                kind, text = events.get_nowait()
            except queue.Empty:
                break
            if kind == "reply":
                append("KaroSpace\n" + text)
            elif kind == "image":
                try:
                    original = tk.PhotoImage(data=text)
                    factor = max(1, (original.width() + 799) // 800, (original.height() + 449) // 450)
                    image = original.subsample(factor, factor)
                    transcript.configure(state="normal")
                    transcript.image_create("end", image=image)
                    transcript.insert("end", "\nLocal section preview\n\n")
                    transcript.configure(state="disabled")
                    images.append(image)
                    del images[:-8]
                    transcript.see("end")
                except tk.TclError:
                    status.set("The local preview could not be displayed.")
            elif kind == "progress":
                status.set(text[-180:] or "Working locally…")
            elif kind == "error":
                append(text)
                state["busy"] = False
                status.set("Stopped · No cloud fallback")
                send_button.configure(state="normal" if state["session"] else "disabled")
            else:
                state["busy"] = False
                status.set(text)
                send_button.configure(state="normal")
        window.after(100, poll)

    threading.Thread(target=work, daemon=True).start()
    poll()
    window.mainloop()

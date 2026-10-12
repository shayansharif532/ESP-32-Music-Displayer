"""The companion's small user interface: a system-tray icon with "Settings", and the Idle Screen settings window.

No command-line options are needed for any of it. The window edits an `IdleStore` (idle.py); "Apply" stores the settings and the
display picks them up within a second (it sees the new `idle_v` in its next /now-playing answer).

tkinter (the window) is in the Python standard library; pystray (the tray icon) is optional - without it the companion still runs,
just without the icon (`--settings` opens the window directly).
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw

from . import idle as idle_mod
from .idle import (ACCENT_PRESETS, BG_MODE_LABELS, BG_MODES, DELAY_MAX, DELAY_MIN, THEME_LABELS, THEMES, IdleSettings, IdleStore,
                   ImageError)

log = logging.getLogger("ui")

PREVIEW = 240
TITLE = "Desk Media Companion - Idle Screen"


def _make_dpi_aware() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class SettingsWindow:
    """One settings window at a time; `open()` can be called from any thread (a second call just brings the window forward)."""

    def __init__(self, store: IdleStore) -> None:
        self.store = store
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._raise = threading.Event()

    def open(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._raise.set()
                return
            self._thread = threading.Thread(target=self._run, name="settings-ui", daemon=True)
            self._thread.start()

    def run_blocking(self) -> None:
        """For `--settings` without a running companion: the window in this thread until it is closed."""
        self._run()

    # -- the window -----------------------------------------------------------------------------------------------------
    def _run(self) -> None:
        try:
            import tkinter as tk
            from tkinter import colorchooser, filedialog, messagebox, ttk
            from PIL import ImageTk
        except ImportError as exc:                                  # a Python without Tk
            log.error("the settings window needs tkinter: %s", exc)
            return
        _make_dpi_aware()
        store = self.store
        cur = store.settings()

        root = tk.Tk()
        root.title(TITLE)
        root.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        theme_var = tk.StringVar(value=THEME_LABELS[cur.theme])
        accent_var = tk.StringVar(value=cur.accent)
        clock_var = tk.BooleanVar(value=cur.show_clock)
        clock24_var = tk.BooleanVar(value=cur.clock_24h)
        text_var = tk.StringVar(value=cur.text)
        delay_var = tk.IntVar(value=cur.delay_s)
        mode_var = tk.StringVar(value=cur.bg_mode)
        file_var = tk.StringVar(value=cur.image_name or "No picture chosen")
        status_var = tk.StringVar(value="")
        state = {"raw": None, "name": "", "remove": False, "after": None, "cache": {}}

        left = ttk.Frame(root, padding=8)
        left.grid(row=0, column=0, sticky="n")
        right = ttk.Frame(root, padding=8)
        right.grid(row=0, column=1, sticky="n")

        # theme
        ttk.Label(left, text="Theme").grid(row=0, column=0, sticky="w", **pad)
        theme_box = ttk.Combobox(left, textvariable=theme_var, values=[THEME_LABELS[t] for t in THEMES], state="readonly", width=18)
        theme_box.grid(row=0, column=1, columnspan=2, sticky="w", **pad)

        # accent
        ttk.Label(left, text="Accent colour").grid(row=1, column=0, sticky="nw", **pad)
        swatches = ttk.Frame(left)
        swatches.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        swatch_labels: dict[str, tk.Label] = {}

        def mark_swatches() -> None:
            for hexcol, lab in swatch_labels.items():
                lab.configure(relief="solid" if hexcol.upper() == accent_var.get().upper() else "flat", bd=2)

        def pick(hexcol: str) -> None:
            accent_var.set(hexcol)
            mark_swatches()
            refresh()

        for i, (name, hexcol) in enumerate(ACCENT_PRESETS.items()):
            lab = tk.Label(swatches, bg=hexcol, width=3, height=1, cursor="hand2")
            lab.grid(row=0, column=i, padx=2)
            lab.bind("<Button-1>", lambda _e, c=hexcol: pick(c))
            swatch_labels[hexcol.upper()] = lab
        ttk.Button(swatches, text="Custom...", width=9,
                   command=lambda: _custom_colour()).grid(row=0, column=len(ACCENT_PRESETS), padx=(8, 0))

        def _custom_colour() -> None:
            rgb, hexcol = colorchooser.askcolor(color=accent_var.get(), parent=root, title="Accent colour")
            if hexcol:
                pick(hexcol.upper())

        # picture
        pic = ttk.LabelFrame(left, text="Photo background (used by the Photo theme)")
        pic.grid(row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=8)
        ttk.Button(pic, text="Choose Image...", command=lambda: choose_image()).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Button(pic, text="Remove", command=lambda: remove_image()).grid(row=0, column=1, padx=2, pady=6, sticky="w")
        ttk.Label(pic, textvariable=file_var, width=34, foreground="#666").grid(row=1, column=0, columnspan=3, padx=8, sticky="w")
        modes = ttk.Frame(pic)
        modes.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 8))
        for i, m in enumerate(BG_MODES):
            ttk.Radiobutton(modes, text=BG_MODE_LABELS[m], value=m, variable=mode_var, command=lambda: refresh()).grid(row=0, column=i, padx=(0, 12))
        ttk.Label(pic, text="JPG or PNG. It is resized and prepared on this PC.", foreground="#666").grid(row=3, column=0, columnspan=3, padx=8, pady=(0, 6), sticky="w")

        # clock / text / delay
        ttk.Checkbutton(left, text="Show clock", variable=clock_var, command=lambda: refresh()).grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(left, text="24-hour", variable=clock24_var, command=lambda: refresh()).grid(row=3, column=2, sticky="w", **pad)
        ttk.Label(left, text="Custom line").grid(row=4, column=0, sticky="w", **pad)
        text_entry = ttk.Entry(left, textvariable=text_var, width=30)
        text_entry.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(left, text="Letters, numbers and basic punctuation (max 40).", foreground="#666").grid(row=5, column=1, columnspan=2, sticky="w", padx=10)
        ttk.Label(left, text="Show idle screen after").grid(row=6, column=0, sticky="w", **pad)
        spin = ttk.Spinbox(left, from_=DELAY_MIN, to=DELAY_MAX, textvariable=delay_var, width=5, command=lambda: refresh())
        spin.grid(row=6, column=1, sticky="w", **pad)
        ttk.Label(left, text="seconds of nothing playing").grid(row=6, column=2, sticky="w")

        # preview
        ttk.Label(right, text="Preview").grid(row=0, column=0, sticky="w")
        canvas = tk.Canvas(right, width=PREVIEW, height=PREVIEW, highlightthickness=1, highlightbackground="#999")
        canvas.grid(row=1, column=0, pady=4)
        ttk.Label(right, text="Roughly how the display will look.\nThe clock shows the current time.", foreground="#666").grid(row=2, column=0, sticky="w")
        hint_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=hint_var, foreground="#a60", wraplength=PREVIEW).grid(row=3, column=0, sticky="w", pady=(6, 0))

        # buttons
        bar = ttk.Frame(root, padding=(10, 0, 10, 10))
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Label(bar, textvariable=status_var, foreground="#070").pack(side="left")
        ttk.Button(bar, text="Close", command=root.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Apply", command=lambda: apply_now()).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Reset to defaults", command=lambda: reset_defaults()).pack(side="right")

        # -- behaviour ------------------------------------------------------------------------------------------------
        def theme_key() -> str:
            label = theme_var.get()
            return next((k for k, v in THEME_LABELS.items() if v == label), "dark")

        def collect() -> IdleSettings:
            try:
                delay = int(delay_var.get())
            except (tk.TclError, ValueError):
                delay = cur.delay_s
            return dataclasses.replace(store.settings(), theme=theme_key(), accent=accent_var.get(), show_clock=clock_var.get(),
                                       clock_24h=clock24_var.get(), text=text_var.get(), bg_mode=mode_var.get(), delay_s=delay).validated()

        def picture_for_preview(mode: str) -> Optional[bytes]:
            if state["remove"] and state["raw"] is None:
                return None
            key = (id(state["raw"]), mode)
            if key not in state["cache"]:
                try:
                    if state["raw"] is not None:
                        state["cache"][key] = idle_mod.render_background(state["raw"], mode)
                    else:
                        state["cache"][key] = store.preview_source(mode)
                except ImageError:
                    state["cache"][key] = None
            return state["cache"][key]

        def draw_preview() -> None:
            state["after"] = None
            s = collect()
            jpeg = picture_for_preview(s.bg_mode) if s.theme == "photo" else None
            img = idle_mod.render_preview(s, jpeg, PREVIEW)
            photo = ImageTk.PhotoImage(img)
            canvas.delete("all")
            canvas.create_image(0, 0, image=photo, anchor="nw")
            canvas.image = photo                                       # keep a reference
            hint_var.set("No picture yet: the Dark theme is shown until you choose one." if s.theme == "photo" and jpeg is None else "")

        def refresh() -> None:
            if state["after"] is None:
                state["after"] = root.after(120, draw_preview)

        def choose_image() -> None:
            path = filedialog.askopenfilename(parent=root, title="Choose a background picture",
                                              filetypes=[("Pictures", "*.jpg *.jpeg *.png"), ("All files", "*.*")])
            if not path:
                return
            try:
                with open(path, "rb") as f:
                    raw = f.read(idle_mod.SOURCE_MAX_BYTES + 1)
                idle_mod.open_picture(raw)                              # validate now, so the user hears about it before Apply
            except ImageError as exc:
                messagebox.showerror(TITLE, str(exc), parent=root)
                return
            except OSError as exc:
                messagebox.showerror(TITLE, f"The file could not be opened: {exc.strerror or exc}", parent=root)
                return
            state.update(raw=raw, name=path.replace("\\", "/").rsplit("/", 1)[-1], remove=False)
            state["cache"].clear()
            file_var.set(state["name"] + "  (not applied yet)")
            theme_var.set(THEME_LABELS["photo"])
            refresh()

        def remove_image() -> None:
            state.update(raw=None, name="", remove=True)
            state["cache"].clear()
            file_var.set("No picture chosen")
            refresh()

        def apply_now() -> None:
            s = collect()
            try:
                if state["remove"] and state["raw"] is None:
                    store.clear_picture()
                if state["raw"] is not None:
                    store.set_picture(state["raw"], state["name"], s.bg_mode)
                    state.update(raw=None, remove=False)
                    state["cache"].clear()
                store.update(theme=s.theme, accent=s.accent, show_clock=s.show_clock, clock_24h=s.clock_24h, text=s.text,
                             bg_mode=s.bg_mode, delay_s=s.delay_s)
            except ImageError as exc:
                messagebox.showerror(TITLE, str(exc), parent=root)
                return
            saved = store.settings()
            text_var.set(saved.text)                                    # shows what was kept after clean-up
            file_var.set(saved.image_name or "No picture chosen")
            status_var.set("Applied. The display updates within a second.")
            refresh()

        def reset_defaults() -> None:
            if not messagebox.askyesno(TITLE, "Reset the idle screen to the built-in defaults?\nThe chosen picture is removed.", parent=root):
                return
            d = store.reset()
            theme_var.set(THEME_LABELS[d.theme])
            accent_var.set(d.accent)
            clock_var.set(d.show_clock)
            clock24_var.set(d.clock_24h)
            text_var.set(d.text)
            delay_var.set(d.delay_s)
            mode_var.set(d.bg_mode)
            file_var.set("No picture chosen")
            state.update(raw=None, remove=False)
            state["cache"].clear()
            mark_swatches()
            status_var.set("Reset.")
            refresh()

        def poll_raise() -> None:
            if self._raise.is_set():
                self._raise.clear()
                root.deiconify()
                root.lift()
                root.attributes("-topmost", True)
                root.after(200, lambda: root.attributes("-topmost", False))
            root.after(300, poll_raise)

        theme_box.bind("<<ComboboxSelected>>", lambda _e: refresh())
        text_var.trace_add("write", lambda *_: refresh())
        delay_var.trace_add("write", lambda *_: refresh())
        mark_swatches()
        draw_preview()
        root.after(300, poll_raise)
        root.mainloop()


# ---- tray icon -------------------------------------------------------------------------------------------------------------

def make_tray_icon_image(size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size * 4
    d.rounded_rectangle((0, 0, s - 1, s - 1), s // 5, fill=(24, 26, 44, 255))
    for i, h in enumerate((0.30, 0.55, 0.42, 0.68, 0.36)):            # five "equaliser" bars in the default accent colour
        x = int(s * (0.16 + i * 0.145))
        d.rounded_rectangle((x, int(s * (0.82 - h)), x + int(s * 0.09), int(s * 0.82)), s // 40, fill=(77, 163, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def start_tray(window: SettingsWindow, on_quit: Callable[[], None]):
    """Puts the companion's icon in the Windows notification area. Returns the pystray icon, or None when pystray is not available."""
    try:
        import pystray
    except ImportError:
        log.warning("pystray is not installed: no tray icon (pip install pystray). Use --settings to open the settings window.")
        return None
    from . import autostart

    def toggle_autostart(icon, item) -> None:
        try:
            autostart.remove() if autostart.is_installed() else autostart.install()
        except OSError as exc:
            log.warning("auto-start change failed: %s", exc)

    def quit_app(icon, item) -> None:
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Settings...", lambda icon, item: window.open(), default=True),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda item: autostart.is_installed()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Desk Media Companion", quit_app),
    )
    icon = pystray.Icon("DeskMediaCompanion", make_tray_icon_image(), "Desk Media Companion", menu)
    icon.run_detached()
    return icon

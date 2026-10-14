"""The companion's windows: the Idle Screen settings window, the pairing dialog and the "Forget paired display" question (the tray icon that
opens them is tray.py).

No command-line options are needed for any of it. The window edits an `IdleStore` (idle.py); "Apply" stores the settings and the
display picks them up within a second (it sees the new `idle_v` in its next /now-playing answer). The pairing dialog drives a
`Pairing` (pairing.py): it opens the pairing window, shows the 6-digit code once the display has revealed it, and the Pair / Cancel
buttons are the user's side of the confirmation.

tkinter (the windows) is in the Python standard library; pystray (the tray icon) is optional - without it the companion still runs,
just without the icon (`--settings` opens the window directly, `--pair-console` pairs without any UI).
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from types import SimpleNamespace
from typing import Callable, Optional

from PIL import Image, ImageDraw

from . import idle as idle_mod
from .idle import (ACCENT_PRESETS, BG_MODE_LABELS, BG_MODES, DELAY_MAX, DELAY_MIN, THEME_LABELS, THEMES, IdleSettings, IdleStore,
                   ImageError)
from .pairing import Pairing

log = logging.getLogger("ui")

PREVIEW = 240
TITLE = "DeskMedia Companion - Idle Screen"
PAIR_TITLE = "DeskMedia Companion - Pair a display"
POLL_MS = 300
PAIR_ARM_S = 1.5             # the Pair button wakes up this long after the code appeared: time to LOOK at the display (a device that is not in front of you cannot show a matching code)
RESULT_MS = 1500             # how long the dialog shows its result ("Paired" ...) before it closes itself


def _make_dpi_aware() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def make_icon_image(badge: Optional[tuple[int, int, int]] = None, size: int = 64) -> Image.Image:
    """The product icon: five equaliser bars on a dark tile; `badge` adds a small status dot in the lower right corner (the tray uses it)."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), s // 5, fill=(24, 26, 44, 255))
    for i, h in enumerate((0.30, 0.55, 0.42, 0.68, 0.36)):            # the default accent colour
        x = int(s * (0.16 + i * 0.145))
        d.rounded_rectangle((x, int(s * (0.82 - h)), x + int(s * 0.09), int(s * 0.82)), s // 40, fill=(77, 163, 255, 255))
    if badge is not None:
        r = int(s * 0.17)
        cx, cy = s - r - int(s * 0.04), s - r - int(s * 0.04)
        d.ellipse((cx - r - s // 32, cy - r - s // 32, cx + r + s // 32, cy + r + s // 32), fill=(24, 26, 44, 255))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=badge + (255,))
    return img.resize((size, size), Image.LANCZOS)


def apply_window_icon(root) -> None:
    """The product icon on a Tk window (otherwise Windows shows Tk's feather). Never fails: a window without an icon still works."""
    try:
        from PIL import ImageTk
        photo = ImageTk.PhotoImage(make_icon_image(size=64), master=root)
        root.iconphoto(True, photo)
        root._dm_icon = photo                     # Tk keeps no reference of its own
    except Exception:
        log.debug("the window icon could not be set")


class SettingsWindow:
    """One settings window at a time; `open()` can be called from any thread (a second call just brings the window forward)."""

    def __init__(self, store: IdleStore, pairing: Optional[Pairing] = None) -> None:
        self.store = store
        self.pairing = pairing
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
            from PIL import ImageTk  # noqa: F401  (the preview needs it: fail here, before a window opens)
        except ImportError as exc:                                  # a Python without Tk
            log.error("the settings window needs tkinter: %s", exc)
            return
        _make_dpi_aware()
        root = tk.Tk()
        apply_window_icon(root)
        ui = self.attach(root)
        root.mainloop()
        del ui, root
        import gc
        gc.collect()          # finish off the window's Tk variables in this thread: collected from another thread (say the HTTP server's)
                              # each one makes that thread wait about a second for a main loop that no longer runs

    @staticmethod
    def _today():
        """Today's date on this PC (a method so a test can pretend it is another day)."""
        import datetime
        return datetime.date.today()

    def attach(self, root) -> SimpleNamespace:
        """Builds the window in `root` and starts its timers (separate from _run so a test can drive it with its own Tk root).
        Returns the controls a test needs."""
        import tkinter as tk
        from tkinter import colorchooser, filedialog, messagebox, ttk
        from PIL import ImageTk
        store = self.store
        cur = store.settings()

        root.title(TITLE)
        root.resizable(False, False)
        pad = {"padx": 10, "pady": 4}

        theme_var = tk.StringVar(value=THEME_LABELS[cur.theme])
        accent_var = tk.StringVar(value=cur.accent)
        clock_var = tk.BooleanVar(value=cur.show_clock)
        clock24_var = tk.BooleanVar(value=cur.clock_24h)
        date_var = tk.BooleanVar(value=cur.show_date)
        date_fmt_var = tk.StringVar(value=cur.date_format)               # the format's key; the drop-down shows it as today's date
        text_var = tk.StringVar(value=cur.text)
        delay_var = tk.IntVar(value=cur.delay_s)
        mode_var = tk.StringVar(value=cur.bg_mode)
        file_var = tk.StringVar(value=cur.image_name or "No picture chosen")
        status_var = tk.StringVar(value="")
        state = {"raw": None, "name": "", "remove": False, "after": None, "cache": {}, "today": None}

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

        # clock / date / text / delay
        ttk.Checkbutton(left, text="Show clock", variable=clock_var, command=lambda: refresh()).grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(left, text="24-hour", variable=clock24_var, command=lambda: refresh()).grid(row=3, column=2, sticky="w", **pad)
        date_frame = ttk.LabelFrame(left, text="Date")
        date_frame.grid(row=4, column=0, columnspan=3, sticky="ew", padx=10, pady=8)
        ttk.Checkbutton(date_frame, text="Show the date", variable=date_var, command=lambda: date_toggled()).grid(
            row=0, column=0, columnspan=2, padx=8, pady=(6, 2), sticky="w")
        ttk.Label(date_frame, text="Format").grid(row=1, column=0, padx=(8, 6), pady=(2, 8), sticky="w")
        date_box = ttk.Combobox(date_frame, state="readonly", width=30)
        date_box.grid(row=1, column=1, pady=(2, 8), sticky="w")
        ttk.Label(left, text="Custom line").grid(row=5, column=0, sticky="w", **pad)
        text_entry = ttk.Entry(left, textvariable=text_var, width=30)
        text_entry.grid(row=5, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(left, text="Letters, numbers and basic punctuation (max 40).", foreground="#666").grid(row=6, column=1, columnspan=2, sticky="w", padx=10)
        ttk.Label(left, text="Show idle screen after").grid(row=7, column=0, sticky="w", **pad)
        spin = ttk.Spinbox(left, from_=DELAY_MIN, to=DELAY_MAX, textvariable=delay_var, width=5, command=lambda: refresh())
        spin.grid(row=7, column=1, sticky="w", **pad)
        ttk.Label(left, text="seconds of nothing playing").grid(row=7, column=2, sticky="w")
        pair_var = tk.StringVar(value="")
        if self.pairing is not None:                                 # one line: is a display paired (pair / forget it from the tray menu)
            ttk.Label(left, textvariable=pair_var, foreground="#666").grid(row=8, column=0, columnspan=3, sticky="w", **pad)

        # preview
        ttk.Label(right, text="Preview").grid(row=0, column=0, sticky="w")
        canvas = tk.Canvas(right, width=PREVIEW, height=PREVIEW, highlightthickness=1, highlightbackground="#999")
        canvas.grid(row=1, column=0, pady=4)
        ttk.Label(right, text="Roughly how the display will look.\nThe clock and date show this PC's time.", foreground="#666").grid(row=2, column=0, sticky="w")
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
                                       clock_24h=clock24_var.get(), show_date=date_var.get(), date_format=date_fmt_var.get(),
                                       text=text_var.get(), bg_mode=mode_var.get(), delay_s=delay).validated()

        def show_date_choices() -> None:
            """The drop-down's entries are today's date in each format; they are rebuilt when the day changes while the window is open."""
            today = self._today()
            if state["today"] == today:
                return
            day_changed = state["today"] is not None
            state["today"] = today
            date_box.configure(values=[text for _fmt, text in idle_mod.date_format_choices(today.year, today.month, today.day)])
            date_box.current(idle_mod.DATE_FORMATS.index(date_fmt_var.get()))
            if day_changed:
                refresh()                                                # the preview shows the date too

        def date_toggled() -> None:
            date_box.configure(state="readonly" if date_var.get() else "disabled")
            refresh()

        def date_selected() -> None:
            index = date_box.current()
            if index >= 0:
                date_fmt_var.set(idle_mod.DATE_FORMATS[index])
                refresh()

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
                store.update(theme=s.theme, accent=s.accent, show_clock=s.show_clock, clock_24h=s.clock_24h, show_date=s.show_date,
                             date_format=s.date_format, text=s.text, bg_mode=s.bg_mode, delay_s=s.delay_s)
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
            date_var.set(d.show_date)
            date_fmt_var.set(d.date_format)
            date_box.current(idle_mod.DATE_FORMATS.index(d.date_format))
            date_box.configure(state="readonly" if d.show_date else "disabled")
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
            show_date_choices()
            if self.pairing is not None:
                pair_var.set("Display: paired" if self.pairing.paired else "Display: not paired")
            if self._raise.is_set():
                self._raise.clear()
                root.deiconify()
                root.lift()
                root.attributes("-topmost", True)
                root.after(200, lambda: root.attributes("-topmost", False))
            root.after(300, poll_raise)

        theme_box.bind("<<ComboboxSelected>>", lambda _e: refresh())
        date_box.bind("<<ComboboxSelected>>", lambda _e: date_selected())
        text_var.trace_add("write", lambda *_: refresh())
        delay_var.trace_add("write", lambda *_: refresh())
        mark_swatches()
        show_date_choices()
        date_box.configure(state="readonly" if cur.show_date else "disabled")
        draw_preview()
        root.after(300, poll_raise)
        return SimpleNamespace(show_date=date_var, date_format=date_fmt_var, date_box=date_box, canvas=canvas, status=status_var,
                               collect=collect, apply=apply_now, reset=reset_defaults, redraw=draw_preview, poll=show_date_choices,
                               toggle_date=date_toggled, select_date=date_selected)


# ---- pairing dialog ------------------------------------------------------------------------------------------------------

def pairing_view(snap: dict) -> dict:
    """What the pairing dialog shows for a `Pairing.snapshot()`: headline, detail, the code ("" until it exists), whether the Pair
    button works, and `done` = the final message once the pairing is over ("Paired" / "Cancelled" / "Timed out"), else None."""
    state, code = snap.get("state", "idle"), snap.get("code") or ""
    view = {"headline": "", "detail": "", "code": code, "can_pair": False, "done": None}
    if state == "paired":
        view["done"] = "Paired"
    elif state == "cancelled":
        view["done"] = "Cancelled"
    elif state == "expired" or (state == "idle" and not snap.get("window_open")):
        view["done"] = "Timed out"
    elif state == "idle":
        view.update(headline="Waiting for the display...", detail="Open the pairing prompt on it.")
    elif state == "offered":
        view.update(headline="A display is connecting...", detail="")
    elif state == "waiting_device":
        view.update(headline="Waiting for the display...", detail="Tap Pair on the display too, once its code matches.")
    else:                                                     # revealed / waiting_pc: the code exists
        view.update(headline="Check that the display shows the same code", detail="Only click Pair if the two codes are identical.",
                    can_pair=True)
    return view


class PairDialog:
    """The "Pair a display..." window: opens the pairing window, shows the code, Pair / Cancel; closes itself with a result message.
    One at a time; `open()` can be called from any thread (a second call brings the dialog forward)."""

    def __init__(self, pairing: Pairing) -> None:
        self.pairing = pairing
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._raise = threading.Event()

    def open(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._raise.set()
                return
            self._thread = threading.Thread(target=self._run, name="pair-ui", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:                                  # a Python without Tk
            log.error("the pairing dialog needs tkinter: %s", exc)
            return
        _make_dpi_aware()
        self.pairing.open_window()
        root = tk.Tk()
        apply_window_icon(root)
        self.attach(root)
        root.mainloop()

    def attach(self, root) -> SimpleNamespace:
        """Builds the dialog in `root` and starts polling the Pairing (separate from _run so a test can drive it with a fake).
        Returns the widgets' variables and the poll / cancel functions."""
        import tkinter as tk
        from tkinter import ttk
        pairing = self.pairing
        root.title(PAIR_TITLE)
        root.resizable(False, False)
        head_var, detail_var, code_var, timer_var = tk.StringVar(), tk.StringVar(), tk.StringVar(), tk.StringVar()
        frame = ttk.Frame(root, padding=18)
        frame.grid(row=0, column=0)
        ttk.Label(frame, textvariable=head_var, font=("Segoe UI", 12, "bold"), wraplength=340, justify="center").grid(row=0, column=0, columnspan=2)
        code_label = ttk.Label(frame, textvariable=code_var, font=("Consolas", 44, "bold"), anchor="center")
        code_label.grid(row=1, column=0, columnspan=2, pady=10)
        ttk.Label(frame, textvariable=detail_var, wraplength=340, justify="center", foreground="#555").grid(row=2, column=0, columnspan=2)
        ttk.Label(frame, textvariable=timer_var, foreground="#888").grid(row=3, column=0, columnspan=2, pady=(8, 10))
        pair_btn = ttk.Button(frame, text="Pair", command=lambda: pairing.pc_confirm())
        pair_btn.grid(row=4, column=0, padx=6, sticky="e")
        cancel_btn = ttk.Button(frame, text="Cancel", command=lambda: cancel())
        cancel_btn.grid(row=4, column=1, padx=6, sticky="w")
        state = {"closing": False}

        def finish(message: str) -> None:
            if state["closing"]:
                return
            state["closing"] = True
            head_var.set(message)
            code_var.set("")
            detail_var.set("")
            timer_var.set("")
            pair_btn.state(["disabled"])
            cancel_btn.state(["disabled"])
            root.after(RESULT_MS, root.destroy)

        def cancel() -> None:
            pairing.pc_cancel()
            finish("Cancelled")

        def poll() -> None:
            if state["closing"]:
                return
            snap = pairing.snapshot()
            view = pairing_view(snap)
            if view["done"]:
                finish(view["done"])
                return
            head_var.set(view["headline"])
            detail_var.set(view["detail"])
            code_var.set(view["code"])
            if view["code"]:
                state.setdefault("code_since", time.monotonic())
            else:
                state.pop("code_since", None)
            armed = view["can_pair"] and time.monotonic() - state.get("code_since", time.monotonic()) >= PAIR_ARM_S
            pair_btn.state(["!disabled"] if armed else ["disabled"])
            timer_var.set(f"Closes in {snap.get('seconds_left', 0)} s")
            if self._raise.is_set():
                self._raise.clear()
                root.deiconify()
                root.lift()
            root.after(POLL_MS, poll)

        root.protocol("WM_DELETE_WINDOW", cancel)
        root.attributes("-topmost", True)                          # the code must be seen while the user looks at the display
        poll()
        return SimpleNamespace(head=head_var, detail=detail_var, code=code_var, pair_button=pair_btn, cancel_button=cancel_btn,
                               poll=poll, cancel=cancel, state=state)


def forget_with_confirmation(pairing: Pairing) -> None:
    """"Forget paired display": asks first, then deletes the key. Runs in its own thread (called from the tray's menu thread)."""
    def run() -> None:
        try:
            import tkinter as tk
            from tkinter import messagebox
        except ImportError as exc:
            log.error("the confirmation needs tkinter: %s", exc)
            return
        _make_dpi_aware()
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if messagebox.askyesno(PAIR_TITLE, "Forget the paired display?\n\nIt will no longer be able to read what is playing "
                                               "until you pair it again.", parent=root):
                pairing.forget()
        finally:
            root.destroy()
    threading.Thread(target=run, name="forget-ui", daemon=True).start()

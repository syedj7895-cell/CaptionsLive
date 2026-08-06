import sys
import tkinter as tk
import tkinter.font as tkfont
import threading
import queue
import json
from pathlib import Path
import sounddevice as sd
from vosk import Model, KaldiRecognizer

if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (OSError, AttributeError, ImportError):
        pass

# --- UI theme (Windows-style dark) ---
BG = "#121212"
BG_ELEVATED = "#1e1e1e"
BG_PANEL = "#161616"
FG = "#ececec"
FG_MUTED = "#9e9e9e"
ACCENT = "#5c6bc0"
ACCENT_HOVER = "#7986cb"
ACCENT_PRESS = "#3f51b5"
CAPTION_FG = "#fafafa"
CAPTION_BOX_FILL = "#252526"
CAPTION_BOX_OUTLINE = "#3d3d42"
CAPTION_SHADOW = "#080808"
TRough = "#2a2a2a"
DIVIDER = "#2a2a2a"

CONFIG_PATH = Path(__file__).resolve().parent / "captionlite_settings.json"
DEFAULT_SETTINGS = {
    "font_size": 22,
    "overlay_alpha": 0.92,
    "caption_geometry": None,
    "control_geometry": None,
    "game_mode": False,
}

_settings_loading = False
_save_after_id = None

CAPTION_MAX_WORDS = 10
SILENCE_CLEAR_MS = 2800
_silence_clear_job = None

# Subtitle-style placement (pixels from bottom of work area)
BOTTOM_MARGIN_NORMAL = 88
BOTTOM_MARGIN_GAME = 44
GAME_MODE_FONT_DELTA = 5
CAPTION_CORNER_RADIUS = 14
SHADOW_OFFSET = 3

# When True, preserve window x,y across fits (user dragged). When False, bottom-center.
_caption_pos_manual = False
_last_geometry = None

q = queue.Queue()
model = Model("model")
recognizer = KaldiRecognizer(model, 16000)

running = False
listener_thread = None

TARGET_RATE = 16000
BLOCK_FRAMES = 2048


def _last_n_words(text, n=CAPTION_MAX_WORDS):
    if not text or not text.strip():
        return ""
    words = text.split()
    if len(words) <= n:
        return text.strip()
    return " ".join(words[-n:])


def callback(indata, frames, time, status):
    if running:
        q.put(bytes(indata))


def _get_windows_loopback_microphone():
    import soundcard as sc

    default_spk = sc.default_speaker()
    for mic in sc.all_microphones(include_loopback=True):
        if mic.isloopback and mic.name == default_spk.name:
            return mic
    try:
        return sc.get_microphone(id=default_spk.name, include_loopback=True)
    except IndexError:
        return sc.get_microphone(id=str(default_spk.id), include_loopback=True)


def _float_audio_to_mono_int16_bytes(audio):
    import numpy as np

    if audio.ndim == 2 and audio.shape[1] > 1:
        mono = audio.mean(axis=1)
    else:
        mono = audio.reshape(-1)
    mono = np.clip(mono.astype(np.float32, copy=False), -1.0, 1.0)
    pcm = (mono * 32767.0).astype(np.int16)
    return pcm.tobytes()


def _listen_windows_loopback():
    global running
    mic = _get_windows_loopback_microphone()
    with mic.recorder(
        TARGET_RATE, channels=mic.channels, blocksize=BLOCK_FRAMES
    ) as rec:
        while running:
            try:
                audio = rec.record(numframes=BLOCK_FRAMES)
            except Exception:
                continue
            if audio.size == 0:
                continue
            try:
                data = _float_audio_to_mono_int16_bytes(audio)
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "")
                    if text:
                        _set_caption_ui(text)
                else:
                    partial = json.loads(recognizer.PartialResult())
                    text = partial.get("partial", "")
                    if text:
                        _set_caption_ui(text)
            except Exception:
                continue


def _listen_microphone_sounddevice():
    global running
    with sd.RawInputStream(
        samplerate=TARGET_RATE,
        blocksize=BLOCK_FRAMES,
        dtype="int16",
        channels=1,
        callback=callback,
    ):
        while running:
            try:
                data = q.get(timeout=0.5)
            except queue.Empty:
                if not running:
                    break
                continue
            try:
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    text = result.get("text", "")
                    if text:
                        _set_caption_ui(text)
                else:
                    partial = json.loads(recognizer.PartialResult())
                    text = partial.get("partial", "")
                    if text:
                        _set_caption_ui(text)
            except Exception:
                continue


def listen():
    global running
    try:
        if sys.platform == "win32":
            _listen_windows_loopback()
        else:
            _listen_microphone_sounddevice()
    except Exception as e:
        def show_err(err=e):
            _cancel_silence_clear()
            caption_var.set(f"CaptionLite: {err}")
            try:
                _fit_caption_overlay()
            except tk.TclError:
                pass
            root.deiconify()

        root.after(0, show_err)
    finally:
        running = False


def start():
    global running, listener_thread
    root.deiconify()
    if sys.platform == "win32":
        root.after(50, sync_caption_click_through)

    if running and listener_thread is not None and listener_thread.is_alive():
        return

    if listener_thread is not None and listener_thread.is_alive():
        running = False
        listener_thread.join(timeout=3.5)
        if listener_thread.is_alive():
            return

    running = True
    listener_thread = threading.Thread(target=listen, daemon=True)
    listener_thread.start()


def stop():
    global running
    running = False
    _cancel_silence_clear()
    caption_var.set("")
    try:
        _fit_caption_overlay()
    except tk.TclError:
        pass
    root.withdraw()


def start_move(event):
    root._drag_dx = event.x_root - root.winfo_rootx()
    root._drag_dy = event.y_root - root.winfo_rooty()
    root._drag_moved = False


def do_move(event):
    global _caption_pos_manual
    root._drag_moved = True
    x = event.x_root - root._drag_dx
    y = event.y_root - root._drag_dy
    root.geometry(f"+{x}+{y}")


def end_drag(_event=None):
    global _caption_pos_manual
    if getattr(root, "_drag_moved", False):
        _caption_pos_manual = True


def _set_caption_click_through_windows(tk_root, enabled):
    """Toggle WS_EX_TRANSPARENT so mouse input passes through the caption window."""
    import ctypes

    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED

    hwnd = int(tk_root.winfo_id())
    ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex < 0:
        ex = (ex + (1 << 32)) % (1 << 32)
    if enabled:
        nex = ex | WS_EX_TRANSPARENT
    else:
        nex = ex & ~WS_EX_TRANSPARENT
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, int(nex) & 0xFFFFFFFF)
    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0, flags)


class RoundedButton(tk.Canvas):
    """Flat canvas button with rounded corners (no extra dependencies)."""

    def __init__(
        self,
        parent,
        text,
        command,
        width=120,
        height=36,
        radius=10,
        fill=ACCENT,
        fill_hover=ACCENT_HOVER,
        fill_press=ACCENT_PRESS,
        fg=FG,
        font=("Segoe UI", 10, "bold"),
        **kwargs,
    ):
        kwargs.setdefault("highlightthickness", 0)
        kwargs.setdefault("bd", 0)
        bg = kwargs.pop("bg", parent.cget("bg"))
        super().__init__(parent, width=width, height=height, bg=bg, **kwargs)
        self._command = command
        self._text = text
        self._fg = fg
        self._font = font if isinstance(font, tkfont.Font) else tkfont.Font(font=font)
        self._r = min(radius, height // 2 - 1, width // 2 - 1)
        self._fill = fill
        self._fill_hover = fill_hover
        self._fill_press = fill_press
        self._pressed = False
        self._rect = None
        self._txt_id = None
        self._draw(fill)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw(self, color):
        self.delete("all")
        w, h, r = int(self["width"]), int(self["height"]), self._r
        self._rect = self._round_rect(0, 0, w, h, r, fill=color, outline=color)
        self._txt_id = self.create_text(
            w // 2,
            h // 2,
            text=self._text,
            fill=self._fg,
            font=self._font,
        )

    def _round_rect(self, x1, y1, x2, y2, r, **kwargs):
        r = max(1, min(r, (y2 - y1) // 2, (x2 - x1) // 2))
        return self.create_polygon(
            x1 + r,
            y1,
            x2 - r,
            y1,
            x2,
            y1,
            x2,
            y1 + r,
            x2,
            y2 - r,
            x2,
            y2,
            x2 - r,
            y2,
            x1 + r,
            y2,
            x1,
            y2,
            x1,
            y2 - r,
            x1,
            y1 + r,
            x1,
            y1,
            smooth=True,
            **kwargs,
        )

    def _on_enter(self, _):
        if not self._pressed:
            self._draw(self._fill_hover)

    def _on_leave(self, _):
        if not self._pressed:
            self._draw(self._fill)

    def _on_press(self, _):
        self._pressed = True
        self._draw(self._fill_press)

    def _on_release(self, event):
        was = self._pressed
        self._pressed = False
        inside = 0 <= event.x <= int(self["width"]) and 0 <= event.y <= int(self["height"])
        self._draw(self._fill_hover if inside else self._fill)
        if was and inside and self._command:
            self._command()


def _canvas_round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    r = max(2, min(r, (y2 - y1) // 2 - 1, (x2 - x1) // 2 - 1))
    return canvas.create_polygon(
        x1 + r,
        y1,
        x2 - r,
        y1,
        x2,
        y1,
        x2,
        y1 + r,
        x2,
        y2 - r,
        x2,
        y2,
        x2 - r,
        y2,
        x1 + r,
        y2,
        x1,
        y2,
        x1,
        y2 - r,
        x1,
        y1 + r,
        x1,
        y1,
        smooth=True,
        tags="chrome",
        **kwargs,
    )


def _draw_caption_chrome(canvas, cw, ch, radius):
    canvas.delete("chrome")
    m = 2
    so = SHADOW_OFFSET
    # Soft shadow (readability on varied backgrounds)
    _canvas_round_rect(
        canvas,
        m + so,
        m + so,
        cw - m + so,
        ch - m + so,
        radius,
        fill=CAPTION_SHADOW,
        outline="",
    )
    _canvas_round_rect(
        canvas,
        m,
        m,
        cw - m,
        ch - m,
        radius,
        fill=CAPTION_BOX_FILL,
        outline=CAPTION_BOX_OUTLINE,
        width=1,
    )


root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.attributes("-alpha", 0.92)
root.configure(bg=BG)
root.option_add("*Font", "{Segoe UI} 10")

caption_var = tk.StringVar()

caption_outer = tk.Frame(root, bg=BG, highlightthickness=0)
caption_outer.pack()

caption_canvas = tk.Canvas(
    caption_outer,
    bg=BG,
    highlightthickness=0,
    bd=0,
)
caption_canvas.pack()

label = tk.Label(
    caption_canvas,
    textvariable=caption_var,
    font=("Segoe UI", 22),
    fg=CAPTION_FG,
    bg=CAPTION_BOX_FILL,
    justify=tk.CENTER,
    anchor=tk.CENTER,
    highlightthickness=0,
    bd=0,
)
_caption_label_window = caption_canvas.create_window(0, 0, window=label, anchor=tk.CENTER)


def _start_drag(event):
    start_move(event)


def _do_drag(event):
    do_move(event)


for w in (root, caption_outer, caption_canvas, label):
    w.bind("<Button-1>", _start_drag)
    w.bind("<B1-Motion>", _do_drag)
    w.bind("<ButtonRelease-1>", end_drag)


def _on_root_configure(event):
    if event.widget == root and not _settings_loading:
        schedule_save()


root.bind("<Configure>", _on_root_configure)

click_through_var = tk.BooleanVar(master=root, value=False)
game_mode_var = tk.BooleanVar(master=root, value=False)


def sync_caption_click_through():
    if sys.platform != "win32":
        return
    try:
        _set_caption_click_through_windows(root, click_through_var.get())
    except (tk.TclError, OSError, ValueError, TypeError, AttributeError):
        pass


def _on_caption_mapped(_event=None):
    if sys.platform == "win32":
        sync_caption_click_through()
    try:
        _fit_caption_overlay()
    except tk.TclError:
        pass


root.bind("<Map>", _on_caption_mapped)

control = tk.Toplevel(root)
control.title("CaptionLite")
control.configure(bg=BG_PANEL)
control.resizable(False, False)
control.attributes("-alpha", 0.98)

control_body = tk.Frame(control, bg=BG_PANEL, padx=32, pady=26)
control_body.pack(fill=tk.BOTH, expand=True)

hdr = tk.Frame(control_body, bg=BG_PANEL)
hdr.pack(fill=tk.X, pady=(0, 14))
tk.Label(
    hdr,
    text="CaptionLite",
    font=("Segoe UI", 15, "bold"),
    fg=FG,
    bg=BG_PANEL,
).pack(anchor=tk.W)
tk.Label(
    hdr,
    text="Live captions from system audio",
    font=("Segoe UI", 9),
    fg=FG_MUTED,
    bg=BG_PANEL,
).pack(anchor=tk.W, pady=(2, 0))

tk.Frame(control_body, bg=DIVIDER, height=1).pack(fill=tk.X, pady=(0, 18))

btn_row = tk.Frame(control_body, bg=BG_PANEL)
btn_row.pack(fill=tk.X, pady=(0, 10))

_start_btn = RoundedButton(
    btn_row,
    text="Start",
    command=start,
    width=118,
    height=38,
    radius=12,
    fill=ACCENT,
    fill_hover=ACCENT_HOVER,
    fill_press=ACCENT_PRESS,
    bg=BG_PANEL,
)
_start_btn.pack(side=tk.LEFT, padx=(0, 10))

_stop_btn = RoundedButton(
    btn_row,
    text="Stop",
    command=stop,
    width=118,
    height=38,
    radius=12,
    fill=BG_ELEVATED,
    fill_hover="#2c2c2c",
    fill_press="#252525",
    bg=BG_PANEL,
)
_stop_btn.pack(side=tk.LEFT)


def _base_font_from_slider():
    try:
        return max(12, min(36, int(round(float(font_scale.get())))))
    except (tk.TclError, ValueError, TypeError):
        return 22


def _effective_caption_font():
    base = _base_font_from_slider()
    if game_mode_var.get():
        return max(12, base - GAME_MODE_FONT_DELTA)
    return base


def change_size(val):
    label.config(font=("Segoe UI", _effective_caption_font()))
    try:
        _fit_caption_overlay()
    except tk.TclError:
        pass
    if not _settings_loading:
        schedule_save()


def change_alpha(val):
    root.attributes("-alpha", float(val))
    if not _settings_loading:
        schedule_save()


def on_game_mode_toggle():
    label.config(font=("Segoe UI", _effective_caption_font()))
    try:
        _fit_caption_overlay()
    except tk.TclError:
        pass
    if not _settings_loading:
        schedule_save()


def _section_label(parent, text):
    tk.Label(
        parent,
        text=text.upper(),
        font=("Segoe UI", 8),
        fg=FG_MUTED,
        bg=BG_PANEL,
        anchor=tk.W,
    ).pack(fill=tk.X, pady=(16, 6))


_section_label(control_body, "Caption bar")

ct_row = tk.Frame(control_body, bg=BG_PANEL)
ct_row.pack(fill=tk.X, pady=(0, 2))
_click_chk = tk.Checkbutton(
    ct_row,
    text="Click-through mode",
    variable=click_through_var,
    command=sync_caption_click_through,
    bg=BG_PANEL,
    fg=FG,
    selectcolor=BG_ELEVATED,
    activebackground=BG_PANEL,
    activeforeground=FG,
    highlightthickness=0,
    anchor=tk.W,
    bd=0,
    font=("Segoe UI", 10),
)
_click_chk.pack(anchor=tk.W)

tk.Label(
    control_body,
    text="Mouse passes through the caption bar to windows behind it.",
    font=("Segoe UI", 8),
    fg=FG_MUTED,
    bg=BG_PANEL,
    justify=tk.LEFT,
    wraplength=280,
).pack(anchor=tk.W, pady=(0, 6))

if sys.platform != "win32":
    _click_chk.config(state=tk.DISABLED)

_game_chk = tk.Checkbutton(
    control_body,
    text="Game mode (compact bar, lower on screen)",
    variable=game_mode_var,
    command=on_game_mode_toggle,
    bg=BG_PANEL,
    fg=FG,
    selectcolor=BG_ELEVATED,
    activebackground=BG_PANEL,
    activeforeground=FG,
    highlightthickness=0,
    anchor=tk.W,
    bd=0,
    font=("Segoe UI", 10),
)
_game_chk.pack(anchor=tk.W, pady=(4, 0))

_section_label(control_body, "Appearance")

scale_style = {
    "bg": BG_PANEL,
    "fg": FG_MUTED,
    "troughcolor": TRough,
    "activebackground": ACCENT,
    "highlightthickness": 0,
    "bd": 0,
    "sliderrelief": tk.FLAT,
    "width": 12,
    "length": 240,
}

font_row = tk.Frame(control_body, bg=BG_PANEL)
font_row.pack(fill=tk.X, pady=(0, 6))
tk.Label(
    font_row,
    text="Caption size",
    font=("Segoe UI", 9),
    fg=FG,
    bg=BG_PANEL,
    width=14,
    anchor=tk.W,
).pack(side=tk.LEFT, padx=(0, 10))
font_scale = tk.Scale(
    font_row,
    from_=12,
    to=36,
    orient=tk.HORIZONTAL,
    showvalue=False,
    command=change_size,
    **scale_style,
)
font_scale.set(22)
font_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)

alpha_row = tk.Frame(control_body, bg=BG_PANEL)
alpha_row.pack(fill=tk.X, pady=(4, 0))
tk.Label(
    alpha_row,
    text="Overlay opacity",
    font=("Segoe UI", 9),
    fg=FG,
    bg=BG_PANEL,
    width=14,
    anchor=tk.W,
).pack(side=tk.LEFT, padx=(0, 10))
alpha_scale = tk.Scale(
    alpha_row,
    from_=0.45,
    to=1.0,
    resolution=0.05,
    orient=tk.HORIZONTAL,
    showvalue=False,
    command=change_alpha,
    **scale_style,
)
alpha_scale.set(0.92)
alpha_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)


def _caption_padding_for_font(fs):
    outer = max(8, int(fs * 0.38))
    inner = max(6, int(fs * 0.32))
    if game_mode_var.get():
        outer = max(4, int(outer * 0.68))
        inner = max(4, int(inner * 0.68))
    return outer, inner


def _bottom_margin():
    return BOTTOM_MARGIN_GAME if game_mode_var.get() else BOTTOM_MARGIN_NORMAL


def _cancel_silence_clear():
    global _silence_clear_job
    if _silence_clear_job is not None:
        try:
            root.after_cancel(_silence_clear_job)
        except tk.TclError:
            pass
        _silence_clear_job = None


def _schedule_silence_clear():
    global _silence_clear_job
    _cancel_silence_clear()

    def _clear_after_silence():
        global _silence_clear_job
        _silence_clear_job = None
        try:
            caption_var.set("")
            _fit_caption_overlay()
        except tk.TclError:
            pass

    _silence_clear_job = root.after(SILENCE_CLEAR_MS, _clear_after_silence)


def _apply_caption_geometry(total_w, total_h):
    global _last_geometry
    sw = max(400, root.winfo_screenwidth())
    sh = max(400, root.winfo_screenheight())

    if _caption_pos_manual:
        x, y = root.winfo_x(), root.winfo_y()
    else:
        x = max(0, (sw - total_w) // 2)
        y = max(0, sh - total_h - _bottom_margin())

    geo = (total_w, total_h, x, y)
    if geo == _last_geometry:
        return
    _last_geometry = geo
    root.geometry(f"{total_w}x{total_h}+{x}+{y}")


def _fit_caption_overlay():
    """Size overlay; Netflix-style bottom-center unless user dragged."""
    global _last_geometry
    try:
        fs = _effective_caption_font()
        outer, inner = _caption_padding_for_font(fs)
        caption_outer.pack_configure(padx=outer, pady=outer)

        sw = max(400, root.winfo_screenwidth())
        margin = max(16, int(fs * 0.45))
        max_bar = min(sw - 2 * margin, int(sw * 0.90))
        max_line = max(160, max_bar - 2 * outer - 2 * inner - 16)
        label.config(wraplength=max_line)

        root.update_idletasks()

        txt = (caption_var.get() or "").strip()
        chrome_pad = 4 + SHADOW_OFFSET

        if not txt:
            min_w = max(120, int(fs * 5.5))
            min_h = max(36, int(fs * 1.6))
            cw = int(min_w + 2 * inner + chrome_pad * 2)
            ch = int(min_h + 2 * inner + chrome_pad * 2)
            caption_canvas.config(width=cw, height=ch)
            _draw_caption_chrome(caption_canvas, cw, ch, CAPTION_CORNER_RADIUS)
            caption_canvas.coords(_caption_label_window, cw // 2, ch // 2)
            total_w = cw + 2 * outer
            total_h = ch + 2 * outer
            _apply_caption_geometry(total_w, total_h)
            return

        lw = label.winfo_reqwidth()
        lh = label.winfo_reqheight()
        cw = int(lw + 2 * inner + chrome_pad * 2)
        ch = int(lh + 2 * inner + chrome_pad * 2)
        cw = min(cw, int(max_bar + 2 * inner + chrome_pad * 2))
        caption_canvas.config(width=cw, height=ch)
        _draw_caption_chrome(caption_canvas, cw, ch, CAPTION_CORNER_RADIUS)
        caption_canvas.coords(_caption_label_window, cw // 2, ch // 2)

        total_w = cw + 2 * outer
        total_h = ch + 2 * outer
        total_w = min(total_w, sw - 2 * margin)
        _apply_caption_geometry(total_w, total_h)
    except tk.TclError:
        pass


def _set_caption_ui(text):
    limited = _last_n_words((text or "").strip(), CAPTION_MAX_WORDS)
    if not limited:
        return

    def apply():
        try:
            _cancel_silence_clear()
            caption_var.set(limited)
            _fit_caption_overlay()
            _schedule_silence_clear()
        except tk.TclError:
            pass

    try:
        root.after_idle(apply)
    except tk.TclError:
        apply()


def load_settings_dict():
    base = {k: v for k, v in DEFAULT_SETTINGS.items()}
    if not CONFIG_PATH.is_file():
        return base
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return base
    if not isinstance(raw, dict):
        return base
    for key in base:
        if key in raw:
            base[key] = raw[key]
    return base


def save_settings_dict():
    try:
        cfg = {
            "font_size": int(round(float(font_scale.get()))),
            "overlay_alpha": float(alpha_scale.get()),
            "caption_geometry": root.geometry(),
            "control_geometry": control.geometry(),
            "game_mode": bool(game_mode_var.get()),
        }
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except (OSError, tk.TclError, ValueError, TypeError):
        pass


def schedule_save():
    global _save_after_id

    def flush():
        global _save_after_id
        _save_after_id = None
        if not _settings_loading:
            save_settings_dict()

    if _save_after_id is not None:
        try:
            root.after_cancel(_save_after_id)
        except tk.TclError:
            pass
    _save_after_id = root.after(400, flush)


def apply_saved_settings(data):
    global _settings_loading, _caption_pos_manual, _last_geometry
    _settings_loading = True
    control_geo_ok = False
    try:
        fs = int(round(float(data.get("font_size", DEFAULT_SETTINGS["font_size"]))))
        fs = max(12, min(36, fs))

        gm = data.get("game_mode", DEFAULT_SETTINGS["game_mode"])
        game_mode_var.set(bool(gm))

        cg = data.get("caption_geometry")
        if isinstance(cg, str) and cg.strip():
            try:
                root.geometry(cg.strip())
                _caption_pos_manual = True
            except tk.TclError:
                _caption_pos_manual = False
        else:
            _caption_pos_manual = False
        _last_geometry = None

        alpha = float(data.get("overlay_alpha", DEFAULT_SETTINGS["overlay_alpha"]))
        alpha = max(0.45, min(1.0, round(alpha / 0.05) * 0.05))
        alpha_scale.set(alpha)
        change_alpha(str(alpha))

        font_scale.set(fs)
        change_size(str(fs))

        cgc = data.get("control_geometry")
        if isinstance(cgc, str) and cgc.strip():
            try:
                control.geometry(cgc.strip())
                control_geo_ok = True
            except tk.TclError:
                pass
    finally:
        _settings_loading = False

    try:
        _fit_caption_overlay()
    except tk.TclError:
        pass
    return control_geo_ok


def _on_control_configure(event):
    if event.widget == control and not _settings_loading:
        schedule_save()


control.bind("<Configure>", _on_control_configure)


def _on_control_delete():
    if not _settings_loading:
        save_settings_dict()
    control.destroy()


control.protocol("WM_DELETE_WINDOW", _on_control_delete)


root.update_idletasks()
control.update_idletasks()
ctrl_w = max(320, control_body.winfo_reqwidth() + 64)
ctrl_h = control_body.winfo_reqheight() + 20
sw = root.winfo_screenwidth()
sh = root.winfo_screenheight()
cx = max(0, (sw - ctrl_w) // 2)
cy = max(0, (sh - ctrl_h) // 2 - 40)
default_control_geo = f"{ctrl_w}x{ctrl_h}+{cx}+{cy}"

_settings_data = load_settings_dict()
if not apply_saved_settings(_settings_data):
    control.geometry(default_control_geo)

root.mainloop()

"""SUPERCURSE: a tray app that reads text aloud.

Ctrl+Alt+R reads the selected text (it copies the selection, then puts your clipboard back).
Ctrl+Alt+E reads the text under the mouse pointer, without selecting and without touching the clipboard.
Pressing either hotkey while speech is playing stops it.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import winsound
from pathlib import Path

import uiautomation as auto  # sets per-monitor DPI awareness on import, so every coordinate below is in physical pixels
import comtypes.client
import pystray
from PIL import Image, ImageDraw, ImageGrab

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
logging.basicConfig(filename=HERE / "supercurse.log", level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("supercurse")
log.setLevel(logging.INFO)  # our own lines only, not comtypes' chatter

DEFAULTS = {
    "selection_hotkey": "ctrl+alt+r",
    "hover_hotkey": "ctrl+alt+e",
    "hover_unit": "sentence",  # word | line | sentence | paragraph
    "highlight": True,
    "ocr_region": [900, 200],  # width, height in pixels of the screenshot taken around the mouse for OCR
    "rate": 0,  # speech speed, -10 (slow) to 10 (fast)
}
UNITS = ["word", "line", "sentence", "paragraph"]
MAX_NAME_CHARS = 1500  # longer Name/Value text is usually a whole app's UI, not what the user is pointing at
JUNK_NAMES = {"Chrome Legacy Window"}  # what Chromium reports before its accessibility tree is built

# own DLL handles: setting argtypes on the shared ctypes.windll ones would break uiautomation's calls
user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")
user32.GetClipboardData.restype = wt.HANDLE
user32.SetClipboardData.argtypes = (wt.UINT, wt.HANDLE)
user32.SetClipboardData.restype = wt.HANDLE
user32.SetWindowPos.argtypes = (wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT)
user32.WindowFromPoint.argtypes = (wt.POINT,)
user32.WindowFromPoint.restype = wt.HWND
user32.GetAncestor.argtypes = (wt.HWND, wt.UINT)
user32.GetAncestor.restype = wt.HWND
kernel32.GlobalLock.argtypes = (wt.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wt.HGLOBAL,)
kernel32.GlobalSize.argtypes = (wt.HGLOBAL,)
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalAlloc.argtypes = (wt.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalFree.argtypes = (wt.HGLOBAL,)


# ---------- config ----------

def load_config():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
    except FileNotFoundError:
        pass
    except ValueError as e:
        log.warning("config.json is not valid JSON (%s), using defaults", e)
    if cfg["hover_unit"] not in UNITS:
        cfg["hover_unit"] = "sentence"
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), "utf-8")


# ---------- text clean-up ----------

def clean_text(text):
    """Make text pleasant to hear: drop markdown symbols, say "link" instead of reading URLs."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"(?:https?://|www\.)\S+", "link", text, flags=re.I)
    text = re.sub(r"^[ \t]*(?:#{1,6}|>+|[-*+•])[ \t]+", "", text, flags=re.M)  # headings, quotes, bullets
    text = re.sub(r"(\*\*|__|~~)(.+?)\1", r"\2", text)  # bold, strikethrough
    text = re.sub(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)", r"\1", text)  # italics
    text = text.replace("`", "").replace("￼", " ")  # U+FFFC: the placeholder Chromium puts where an image/emoji is
    return re.sub(r"\s+", " ", text).strip()


def sentence_span(text, offset):
    """(start, end) of the sentence in `text` containing character `offset`."""
    # shortcut: splits after any . ! ? so "e.g. this" becomes two sentences; upgrade to a real sentence splitter if that bites
    bounds = [0] + [m.end() for m in re.finditer(r"[.!?…]+[\"'”’)\]]*\s+", text)] + [len(text)]
    for a, b in zip(bounds, bounds[1:]):
        if offset < b:
            return a, b
    return bounds[-2], bounds[-1]


# ---------- geometry ----------

def union(rects):
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def dist(rect, x, y):
    """Distance from a point to a rectangle (0 when inside)."""
    dx = max(rect[0] - x, 0, x - rect[2])
    dy = max(rect[1] - y, 0, y - rect[3])
    return (dx * dx + dy * dy) ** 0.5


# ---------- method 1: UI Automation ----------

UIA_TEXT, UIA_NAME, OCR = "UI Automation (TextPattern)", "UI Automation (Name/Value)", "OCR"
TEXT_UNITS = {"word": auto.TextUnit.Word, "line": auto.TextUnit.Line,
              "sentence": auto.TextUnit.Paragraph, "paragraph": auto.TextUnit.Paragraph}


def text_pattern_read(pattern, x, y, unit):
    at = pattern.RangeFromPoint(x, y)
    if not at:
        return None
    rng = at.Clone()
    rng.ExpandToEnclosingUnit(TEXT_UNITS[unit], waitTime=0)
    raw = rng.GetText()
    text = raw.strip()
    if unit == "sentence" and text:
        head = rng.Clone()
        head.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, at, auto.TextPatternRangeEndpoint.Start, waitTime=0)
        a, b = sentence_span(raw, len(head.GetText()))
        text = raw[a:b].strip()
        rng = rng.FindText(text, False, False) or rng
    if not text:
        return None
    rects = [(r.left, r.top, r.right, r.bottom) for r in rng.GetBoundingRectangles()]
    rect = union(rects) if rects else None
    if rect and dist(rect, x, y) > 40:
        return None  # RangeFromPoint snaps to the nearest text even when the mouse is over an image or blank space
    return text, rect, UIA_TEXT


def uia_read(x, y, unit):
    control = auto.ControlFromPoint(x, y)
    if not control:
        return None
    # the element under the mouse is often a text leaf; the TextPattern usually lives on an ancestor (document/edit box)
    # (Discord's document sits ~20 levels above a message, so climb all the way to the window)
    el = control
    while el and not el.IsTopLevel():
        pattern = el.GetPattern(auto.PatternId.TextPattern)
        if pattern:
            got = text_pattern_read(pattern, x, y, unit)
            if got:
                return got
            break
        el = el.GetParentControl()
    if control.IsTopLevel() or control.GetPattern(auto.PatternId.TextPattern):
        return None  # a window or document title (e.g. a fullscreen game's, a web page's) is not what the user points at
    # Value before Name: in grid cells and text boxes the Name is only the label (Explorer's cells are all named "Name")
    getters = (lambda: control.GetPattern(auto.PatternId.ValuePattern).Value,
               lambda: control.Name,
               lambda: control.GetLegacyIAccessiblePattern().Value)
    for get in getters:
        try:
            text = (get() or "").strip()
        except Exception:  # pattern not supported
            continue
        if text and len(text) <= MAX_NAME_CHARS and text not in JUNK_NAMES:
            r = control.BoundingRectangle
            return text, (r.left, r.top, r.right, r.bottom), UIA_NAME
    return None


def is_chromium(x, y):
    hwnd = user32.GetAncestor(user32.WindowFromPoint(wt.POINT(x, y)), 2)  # GA_ROOT
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value.startswith("Chrome_WidgetWin")  # Chrome, Edge, Discord and other Electron apps


# ---------- method 2: Windows OCR ----------

_ocr_engine = None


def ocr_lines(img):
    """Run the built-in Windows OCR on a PIL image. Returns [[(word, (l, t, r, b)), ...] per line] in image pixels."""
    global _ocr_engine
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter
    if _ocr_engine is None:
        _ocr_engine = OcrEngine.try_create_from_user_profile_languages()
        if _ocr_engine is None:
            log.warning("Windows OCR has no language installed for your Windows display language")
            return []
    scale = 2  # Windows OCR misses small UI text; doubling the size helps a lot
    big = img.convert("RGBA").resize((img.width * scale, img.height * scale), Image.LANCZOS)
    writer = DataWriter()
    writer.write_bytes(big.tobytes("raw", "BGRA"))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, big.width, big.height)
    result = asyncio.run(_recognize(_ocr_engine, bitmap))
    return [[(w.text, (w.bounding_rect.x / scale, w.bounding_rect.y / scale,
                       (w.bounding_rect.x + w.bounding_rect.width) / scale,
                       (w.bounding_rect.y + w.bounding_rect.height) / scale)) for w in line.words]
            for line in result.lines]


async def _recognize(engine, bitmap):
    return await engine.recognize_async(bitmap)


def pick_ocr_text(lines, x, y, unit):
    """From OCR lines (screen pixels), pick the text at (x, y) for the unit. Returns (text, rect) or None."""
    lines = sorted((l for l in lines if l), key=lambda l: min(w[1][1] for w in l))
    if not lines:
        return None
    box = lambda words: union([w[1] for w in words])
    i = min(range(len(lines)), key=lambda k: dist(box(lines[k]), x, y))
    target = min(lines[i], key=lambda w: dist(w[1], x, y))
    if unit == "word":
        return target[0], target[1]
    if unit == "line":
        return " ".join(w[0] for w in lines[i]), box(lines[i])

    def same_block(p, q):
        a, b = box(p), box(q)
        gap = max(a[1], b[1]) - min(a[3], b[3])
        return gap < max(a[3] - a[1], b[3] - b[1]) and a[0] < b[2] and b[0] < a[2]  # close, and in the same column

    # grow the block from the line under the mouse; text from other columns or windows is never adjacent to it
    block, grew = [lines[i]], True
    while grew:
        grew = False
        for line in lines:
            if not any(line is b for b in block) and any(same_block(line, b) for b in block):
                block.append(line)
                grew = True
    block.sort(key=lambda l: box(l)[1])
    words = [w for line in block for w in line]
    if unit == "paragraph":
        return " ".join(w[0] for w in words), box(words)
    starts, text = [], ""
    for w in words:
        starts.append(len(text))
        text += w[0] + " "
    a, b = sentence_span(text, starts[next(k for k, w in enumerate(words) if w is target)])
    picked = [w for w, s in zip(words, starts) if a <= s < b]
    return text[a:b].strip(), box(picked)


def ocr_read(x, y, unit, region):
    vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))  # virtual screen, all monitors
    w, h = min(region[0], vw), min(region[1], vh)
    left = max(vx, min(x - w // 2, vx + vw - w))
    top = max(vy, min(y - h // 2, vy + vh - h))
    img = ImageGrab.grab((left, top, left + w, top + h), all_screens=True)
    lines = [[(t, (l + left, tp + top, r + left, b + top)) for t, (l, tp, r, b) in line] for line in ocr_lines(img)]
    got = pick_ocr_text(lines, x, y, unit)
    if not got or dist(got[1], x, y) > 60:
        return None  # nearest text is too far from the mouse to be what the user meant
    return got[0], tuple(int(v) for v in got[1]), OCR


# ---------- the two read modes ----------

def read_under_mouse(cfg):
    """Returns (text, rect, method) for the text under the mouse, or None. Never touches the clipboard."""
    x, y = auto.GetCursorPos()
    unit = cfg["hover_unit"]

    def attempt(fn, *args):
        try:
            return fn(*args)
        except Exception:
            log.exception("%s failed", fn.__name__)
            return None

    got = attempt(uia_read, x, y, unit)
    if (not got or got[2] != UIA_TEXT) and is_chromium(x, y):
        time.sleep(0.3)  # Chromium builds its accessibility tree only once a client asks, so the first query is often empty
        got = attempt(uia_read, x, y, unit) or got
    return got or attempt(ocr_read, x, y, unit, cfg["ocr_region"])


GDI_FORMATS = {2, 3, 9, 14}  # bitmap/metafile/palette handles, not plain memory; Windows rebuilds them from the formats we keep


def open_clipboard():
    for _ in range(25):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def save_clipboard():
    if not open_clipboard():
        return None
    saved = []
    try:
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            handle = user32.GetClipboardData(fmt) if fmt not in GDI_FORMATS else None
            ptr = kernel32.GlobalLock(handle) if handle else None
            if ptr:
                saved.append((fmt, ctypes.string_at(ptr, kernel32.GlobalSize(handle))))
                kernel32.GlobalUnlock(handle)
            fmt = user32.EnumClipboardFormats(fmt)
    finally:
        user32.CloseClipboard()
    return saved


def restore_clipboard(saved):
    if saved is None or not open_clipboard():
        return
    try:
        user32.EmptyClipboard()
        for fmt, data in saved:
            handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)
    finally:
        user32.CloseClipboard()


def read_selection():
    """Copy the current selection with Ctrl+C, return it, and put the previous clipboard back."""
    deadline = time.time() + 1
    while time.time() < deadline and any(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
        time.sleep(0.02)  # wait for the user to let go of the hotkey, or the app would see Ctrl+Alt+C
    saved = save_clipboard()
    seq = user32.GetClipboardSequenceNumber()
    for vk, flags in ((0x11, 0), (0x43, 0), (0x43, 2), (0x11, 2)):  # Ctrl down, C down, C up, Ctrl up
        user32.keybd_event(vk, 0, flags, 0)
    deadline = time.time() + 0.6
    while user32.GetClipboardSequenceNumber() == seq and time.time() < deadline:
        time.sleep(0.02)
    text = auto.GetClipboardText() if user32.GetClipboardSequenceNumber() != seq else ""
    restore_clipboard(saved)
    return text


def nothing_found():
    winsound.Beep(330, 120)


# ---------- hotkeys ----------

MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}


def parse_hotkey(combo):
    """'ctrl+alt+e' -> (modifier flags, virtual key code) for RegisterHotKey."""
    mods, vk = 0x4000, 0  # MOD_NOREPEAT: holding the keys down fires once
    for part in combo.lower().replace(" ", "").split("+"):
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", part):
            vk = 0x6F + int(part[1:])
        elif len(part) == 1:
            vk = user32.VkKeyScanW(ord(part)) & 0xFF
        else:
            raise ValueError(f"unknown key {part!r} in hotkey {combo!r}")
    if not vk:
        raise ValueError(f"hotkey {combo!r} has no key besides modifiers")
    return mods, vk


# ---------- app ----------

class App:
    def __init__(self):
        self.cfg = load_config()
        save_config(self.cfg)  # so the settings file always exists to open from the tray
        self.jobs = queue.Queue()
        self.ui = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = Overlay(self.root)
        self.icon = pystray.Icon("SUPERCURSE", tray_image(), self.tooltip(), self.menu())

    def tooltip(self):
        return (f"SUPERCURSE\n{self.cfg['selection_hotkey'].title()}: read selection\n"
                f"{self.cfg['hover_hotkey'].title()}: read under mouse ({self.cfg['hover_unit']})")

    def menu(self):
        def unit_item(unit):
            return pystray.MenuItem(unit.title(), lambda icon, item: self.set("hover_unit", unit),
                                    checked=lambda item: self.cfg["hover_unit"] == unit, radio=True)
        return pystray.Menu(
            pystray.MenuItem("Read under mouse", pystray.Menu(*map(unit_item, UNITS))),
            pystray.MenuItem("Highlight what is read", lambda icon, item: self.set("highlight", not self.cfg["highlight"]),
                             checked=lambda item: self.cfg["highlight"]),
            pystray.MenuItem("Stop speaking", lambda icon, item: self.jobs.put("stop")),
            pystray.MenuItem("Open settings file", lambda icon, item: os.startfile(CONFIG_PATH)),
            pystray.MenuItem("Quit", lambda icon, item: self.quit()),
        )

    def set(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        self.icon.title = self.tooltip()

    def quit(self):
        self.icon.stop()
        self.ui.put(("quit",))

    def hotkeys(self):
        bindings = {1: ("selection", self.cfg["selection_hotkey"]), 2: ("hover", self.cfg["hover_hotkey"])}
        for hotkey_id, (job, combo) in bindings.items():
            try:
                ok = user32.RegisterHotKey(None, hotkey_id, *parse_hotkey(combo))
            except ValueError as e:
                ok, combo = False, f"{combo} ({e})"
            if not ok:
                log.warning("could not register hotkey %s", combo)
                self.icon.notify(f"Could not use the hotkey {combo}. Another program may be using it. "
                                 f"Change it in the settings file and restart.", "SUPERCURSE")
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                # Windows swallows the hotkey's letter, so the app would see Alt pressed and released alone and open
                # its menu (Notepad moves focus to "File", breaking Ctrl+C). Tapping Ctrl while Alt is still held
                # prevents that; WinUI apps ignore the usual unassigned "mask" key, so it has to be a real key.
                if user32.GetAsyncKeyState(0x12) & 0x8000:
                    ctrl_held = user32.GetAsyncKeyState(0x11) & 0x8000
                    for flags in ((2, 0) if ctrl_held else (0, 2)):  # leave Ctrl as it was: up+down or down+up
                        user32.keybd_event(0x11, 0, flags, 0)
                self.jobs.put(bindings[msg.wParam][0])

    def worker(self):
        with auto.UIAutomationInitializerInThread():
            voice = comtypes.client.CreateObject("SAPI.SpVoice")
            while True:
                job = self.jobs.get()
                if job == "stop" or voice.Status.RunningState == 2:  # 2 = speaking: either hotkey stops speech
                    voice.Speak("", 3)  # async + purge = stop
                    log.info("stopped speaking")
                    continue
                try:
                    if job == "selection":
                        text = read_selection()
                    else:
                        got = read_under_mouse(self.cfg)
                        text = got and got[0]
                        if got:
                            log.info("hover via %s: %r", got[2], text[:100])
                            if self.cfg["highlight"] and got[1]:
                                self.ui.put(("highlight", got[1]))
                except Exception:
                    log.exception("%s read failed", job)
                    text = None
                text = clean_text(text or "")
                if text:
                    voice.Rate = max(-10, min(10, int(self.cfg["rate"])))
                    voice.Speak(text, 1 | 2 | 16)  # async, purge anything queued, plain text (not SAPI XML)
                    log.info("speaking (%s): %r", job, text[:100])
                else:
                    log.info("nothing found (%s)", job)
                    nothing_found()

    def poll_ui(self):
        try:
            while True:
                msg = self.ui.get_nowait()
                if msg[0] == "highlight":
                    self.overlay.show(msg[1])
                elif msg[0] == "quit":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self.poll_ui)

    def run(self):
        self.icon.run_detached()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.hotkeys, daemon=True).start()
        self.poll_ui()
        self.root.mainloop()


class Overlay:
    """A see-through yellow box shown briefly over the text being read. Click-through, never takes focus."""

    def __init__(self, root):
        self.root = root
        self.win = tk.Toplevel(root, bg="#ffd400")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True, "-alpha", 0.3)
        self.win.withdraw()
        root.update_idletasks()
        self.hwnd = int(self.win.wm_frame(), 16)
        ex_style = user32.GetWindowLongW(self.hwnd, -20)  # GWL_EXSTYLE
        # layered | transparent (clicks pass through) | tool window (no taskbar button) | no-activate (never takes focus)
        user32.SetWindowLongW(self.hwnd, -20, ex_style | 0x80000 | 0x20 | 0x80 | 0x8000000)
        self.hide_job = None

    def show(self, rect):
        pad = 3
        left, top, right, bottom = rect
        # shown with raw Win32 calls because Tk's own show/raise would activate the window and steal focus
        user32.SetWindowPos(self.hwnd, wt.HWND(-1), left - pad, top - pad, right - left + 2 * pad, bottom - top + 2 * pad,
                            0x0010 | 0x0040)  # HWND_TOPMOST, SWP_NOACTIVATE | SWP_SHOWWINDOW
        if self.hide_job:
            self.root.after_cancel(self.hide_job)
        self.hide_job = self.root.after(1000, lambda: user32.ShowWindow(self.hwnd, 0))


def tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=(40, 90, 200))
    draw.polygon([(18, 24), (28, 24), (40, 14), (40, 50), (28, 40), (18, 40)], fill="white")  # speaker
    return img


if __name__ == "__main__":
    App().run()

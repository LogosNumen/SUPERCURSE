# SUPERCURSE

A small Windows tray app that reads text aloud with the built-in Windows voice.

| Hotkey | What it reads |
|---|---|
| **Ctrl+Alt+R** | The text you have selected. |
| **Ctrl+Alt+E** | The text under the mouse pointer. Nothing needs to be selected, and the clipboard is never touched. |

Press either hotkey again while it is speaking to stop it. If it finds nothing to read, it plays a short low beep.

## Setup

You need Python 3.12 (64-bit). Install it from python.org and tick "Add to PATH". Then, in this folder:

```bat
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Start the app by double-clicking `SUPERCURSE.bat`. A blue speaker icon appears in the tray.
To start it with Windows, put a shortcut to `SUPERCURSE.bat` in the folder that opens when you type `shell:startup` into Win+R.

## Tray menu

Right-click the tray icon:

- **Read under mouse → Word / Line / Sentence / Paragraph**: how much Ctrl+Alt+E reads. The default is Sentence.
- **Highlight what is read**: briefly shows a see-through yellow box over the text it picked up. The box is click-through and never takes focus.
- **Stop speaking**
- **Open settings file**
- **Quit**

## Settings (`config.json`)

The file is created on first run. Restart the app after changing a hotkey.

| Key | Default | Meaning |
|---|---|---|
| `selection_hotkey` | `"ctrl+alt+r"` | Hotkey to read the selection. Modifiers are `ctrl`, `alt`, `shift` and `win`, plus one letter, digit or `f1`–`f24`. |
| `hover_hotkey` | `"ctrl+alt+e"` | Hotkey to read under the mouse. |
| `hover_unit` | `"sentence"` | `word`, `line`, `sentence` or `paragraph`. |
| `highlight` | `true` | Show the highlight box. |
| `ocr_region` | `[900, 200]` | Width and height in pixels of the screenshot taken around the mouse for OCR. |
| `rate` | `0` | Speech speed, from -10 (slow) to 10 (fast). |

Before speaking, all text is cleaned up the same way: markdown symbols are removed, web addresses are read as "link", and image placeholders are dropped.

## How "read under the mouse" finds the text

It tries each method in turn and stops at the first that finds something:

1. **Windows UI Automation, text pattern.** It finds the text control under the mouse, which may be several levels up from the element actually under the pointer. It then takes the exact spot under the pointer and widens it to a word, line or paragraph. For Sentence, it takes the paragraph and cuts out the sentence that contains the pointer.
2. **UI Automation, name or value.** For things like file names and buttons, it reads the element's Value, then its Name, then its legacy accessibility value. Text over 1500 characters is skipped, as are window and page titles.
3. **Chromium retry.** Chrome, Edge, Discord and other Electron apps only build their accessibility information once something asks for it. In those apps, if the text pattern didn't answer, it waits 0.3 s and tries again.
4. **OCR.** It takes a screenshot around the pointer and runs the OCR engine built into Windows (no extra install, no account or API key). It picks the text line nearest the pointer. For Sentence or Paragraph, it adds the lines above and below that belong to the same block of text.
5. If nothing is found, it beeps.

The app is per-monitor DPI aware, so the mouse position, screenshots and highlight box all use real screen pixels.

## Test results

These were tested on Windows 11 with two monitors at 100% scaling. All tests ran on the second monitor, which sits to the left of the main one and has negative screen coordinates.

| App | Method used | Result |
|---|---|---|
| Notepad (Windows 11) | UI Automation, text pattern | All four amounts correct, e.g. Sentence over "second" read "A second sentence sits here!". |
| File Explorer, a file name in Details view | UI Automation, value | Read "Quarterly report draft.txt". The Name of each cell there is only the column heading ("Name"), which is why Value is checked first. |
| Chrome, a web page | UI Automation, text pattern | All four amounts correct, including a sentence that contains a link. |
| Edge, opened fresh | UI Automation, text pattern after the retry | The first query came back empty and the 0.3 s retry read it. |
| Discord, a chat message | UI Automation, text pattern | Word, line and sentence of a message correct. The text control is about 20 levels above the message. |
| Text inside a picture | OCR | All four amounts correct. A separate block of text lower down was correctly kept apart. |
| Fullscreen game (VALORANT) | Goes to OCR | The window title is no longer read. Reading text inside the game was not tested. |

The selection hotkey, stopping speech, the "nothing found" beep and the highlight box were also tested with simulated key presses in Notepad. The highlight box showed up without taking focus. The clipboard was put back afterwards, including when it held an image.

## Known limits

- Over a picture or GIF in Discord, it reads Discord's hidden labels for that message (time, "GIF", "Add to Favorites") rather than text inside the picture.
- Sentence splitting breaks after every `.`, `!` or `?`, so "e.g. this" counts as two sentences.
- Display scaling above 100% has not been tested yet.
- The selection hotkey works by pressing Ctrl+C for you, so it only works in apps where Ctrl+C copies.

Problems are written to `supercurse.log` in this folder.

If it won't start and shows `cannot import name 'gen' from 'comtypes'`, comtypes couldn't create its cache folder. Create an empty file at `.venv\Lib\site-packages\comtypes\gen\__init__.py` and try again.

## Tests

```bat
.venv\Scripts\python test_supercurse.py
```

These check the text clean-up, sentence cutting, hotkey parsing and how OCR lines are grouped.

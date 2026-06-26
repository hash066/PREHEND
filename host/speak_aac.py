"""PREHEND-SPEAK — scanning AAC keyboard with multi-modal BCI input.

Receives gesture and EOG events from the PREHEND firmware over serial and
drives a scanning letter/word keyboard with TTS voice synthesis.

Modes of input (any action → SELECT):
  - Sustained eye blink >200 ms  (EOG_LONG, EXG Pill in EOG mode on A1)
  - Head nod                      (MPU6050 IMU)
  - EMG burst above commit thresh (flexor sEMG on A0 — fallback modality)

Navigation:
  - Short blink / sustained IMU tilt → NEXT item within row
  - Head shake                       → BACK (up one level)
  - Auto-scan: cursor advances every SCAN_INTERVAL_S if no input

On startup the script writes M1 to the device (locks claw open, safe).
On exit (ESC or window close) it writes M0 (restores GRASP mode).

Usage
-----
    pip install pyserial pygame pyttsx3
    python host/speak_aac.py                   # COM3, 2 s scan
    python host/speak_aac.py COM5              # explicit port
    python host/speak_aac.py COM5 --scan 1.5   # faster scan
    python host/speak_aac.py COM5 --rms-commit 0.4
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
from typing import List, Optional

import serial

# ---------------------------------------------------------------------------
# Grid content
# ---------------------------------------------------------------------------
ALPHABET_ROW: List[str] = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

WORD_ROWS: List[List[str]] = [
    ["I",    "YOU",   "HE",    "SHE",   "WE",    "THEY",  "IT",    "THIS",  "THAT",  "WHAT"],
    ["IS",   "ARE",   "WAS",   "HAVE",  "DO",    "CAN",   "WILL",  "WANT",  "NEED",  "GO"],
    ["THE",  "A",     "AND",   "OR",    "BUT",   "NOT",   "NO",    "YES",   "PLEASE","THANK"],
    ["HELP", "STOP",  "WAIT",  "COME",  "HERE",  "NOW",   "TODAY", "MORE",  "LESS",  "OKAY"],
    ["HELLO","GOOD",  "BAD",   "PAIN",  "TIRED", "HUNGRY","THIRSTY","HAPPY","SORRY", "LOVE"],
]

ACTION_ROW: List[str] = ["[SPEAK]", "[SPACE]", "[BACK]", "[CLEAR]"]

ALL_ROWS: List[List[str]] = [ALPHABET_ROW] + WORD_ROWS + [ACTION_ROW]

WORD_CORPUS: List[str] = [
    "I","YOU","HE","SHE","WE","THEY","IT","THIS","THAT","WHAT",
    "IS","ARE","WAS","HAVE","DO","CAN","WILL","WANT","NEED","GO",
    "THE","A","AND","OR","BUT","NOT","NO","YES","PLEASE","THANK",
    "HELP","STOP","WAIT","COME","HERE","NOW","TODAY","MORE","LESS","OKAY",
    "HELLO","GOOD","BAD","PAIN","TIRED","HUNGRY","THIRSTY","HAPPY","SORRY","LOVE",
    "WATER","FOOD","HOME","WORK","SLEEP","HEAR","SEE","FEEL","KNOW","THINK",
    "ABLE","BACK","CALL","CARE","DAY","EACH","FACE","GIVE","HAND","INTO",
    "JUST","KEEP","LAST","LONG","MAKE","MANY","MUST","NAME","OPEN","OVER",
    "PART","READ","REAL","SAME","SEEM","SIDE","SHOW","TELL","THAN","THEM",
    "THEN","TIME","TURN","USED","VERY","WELL","WHEN","WITH","WORD","YEAR",
]

# ---------------------------------------------------------------------------
# Event constants — must match firmware gestureByte encoding
# bits 3-0 = eogEvent, bits 5-4 = imuGesture
# ---------------------------------------------------------------------------
EOG_NONE   = 0; EOG_SHORT = 1; EOG_LONG = 2   # SELECT
EOG_SACC_R = 3; EOG_SACC_L = 4

IMU_NONE  = 0; IMU_NOD   = 1   # SELECT
IMU_SHAKE = 2                   # BACK
IMU_TILT  = 3                   # NEXT

ACTION_SELECT = "SELECT"
ACTION_NEXT   = "NEXT"
ACTION_BACK   = "BACK"

BAUD = 115200

# ---------------------------------------------------------------------------
# Colours (dark theme)
# ---------------------------------------------------------------------------
BG          = (17,  17,  17)
CELL_NORMAL = (40,  40,  40)
CELL_HL_ROW = (30,  80,  30)
CELL_HL_CEL = (25, 118, 210)
TXT_NORMAL  = (200, 200, 200)
TXT_HL      = (255, 255, 255)
TXT_PRED    = (100, 200, 100)
TXT_COMPOSED= (255, 240, 100)
BORDER      = (70,  70,  70)


# ---------------------------------------------------------------------------
# Word predictor
# ---------------------------------------------------------------------------
class WordPredictor:
    def __init__(self, corpus: List[str]):
        self._corpus = corpus

    def predict(self, prefix: str, n: int = 3) -> List[str]:
        if not prefix:
            return []
        p = prefix.upper()
        return [w for w in self._corpus if w.startswith(p)][:n]


# ---------------------------------------------------------------------------
# Serial reader (daemon thread)
# ---------------------------------------------------------------------------
class SerialReader:
    def __init__(self, port: str, baud: int):
        self._port = port
        self._baud = baud
        self._ser: Optional[serial.Serial] = None
        self._stop = threading.Event()
        self.event_q: queue.Queue = queue.Queue()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, cmd: str):
        if self._ser and self._ser.is_open:
            try:
                self._ser.write((cmd + '\n').encode())
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._ser:
            try:
                self.send("M0")
                time.sleep(0.3)
                self._ser.close()
            except Exception:
                pass

    def _run(self):
        try:
            self._ser = serial.Serial(self._port, self._baud, timeout=1)
        except serial.SerialException as e:
            print(f"[SERIAL] Cannot open {self._port}: {e}")
            return
        time.sleep(1.5)   # wait for Arduino reset on port open
        self.send("M1")
        print(f"[SERIAL] {self._port} open — device set to SPEAK mode")

        while not self._stop.is_set():
            try:
                raw = self._ser.readline()
            except serial.SerialException:
                time.sleep(0.1)
                continue
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 9:
                continue
            try:
                emg_norm     = float(parts[1])
                gesture_byte = int(parts[8])
                self.event_q.put({
                    "emgNorm":    emg_norm,
                    "eogEvent":   gesture_byte & 0x0F,
                    "imuGesture": (gesture_byte >> 4) & 0x03,
                })
            except (ValueError, IndexError):
                continue


# ---------------------------------------------------------------------------
# Scan state machine
# ---------------------------------------------------------------------------
class ScanGrid:
    def __init__(self, rows: List[List[str]]):
        self._rows = rows
        self._row  = 0
        self._cell = 0
        self._in_cells = False

    @property
    def rows(self): return self._rows
    @property
    def current_row(self): return self._row
    @property
    def current_cell(self): return self._cell
    @property
    def in_cells(self): return self._in_cells

    def next(self):
        if self._in_cells:
            self._cell = (self._cell + 1) % len(self._rows[self._row])
        else:
            self._row = (self._row + 1) % len(self._rows)

    def select(self) -> Optional[str]:
        if not self._in_cells:
            self._in_cells = True
            self._cell = 0
            return None
        chosen = self._rows[self._row][self._cell]
        self._in_cells = False
        self._cell = 0
        return chosen

    def back(self):
        if self._in_cells:
            self._in_cells = False
        else:
            self._row = max(0, self._row - 1)


# ---------------------------------------------------------------------------
# TTS engine (daemon thread, pyttsx3)
# ---------------------------------------------------------------------------
class TTSEngine:
    def __init__(self):
        self._pending: Optional[str] = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def speak(self, text: str):
        with self._lock:
            self._pending = text

    def _run(self):
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 120)    # slow, robotic — approximates DECtalk
            engine.setProperty("volume", 1.0)
            # prefer a low-pitched male voice for the Hawking aesthetic
            for v in engine.getProperty("voices"):
                if any(k in v.name.lower() for k in ("male", "david", "mark")):
                    engine.setProperty("voice", v.id)
                    break
        except Exception as e:
            print(f"[TTS] init failed: {e}")
            return

        while True:
            with self._lock:
                text = self._pending
                self._pending = None
            if text:
                try:
                    engine.say(text)
                    engine.runAndWait()
                except Exception:
                    pass
            else:
                time.sleep(0.04)


# ---------------------------------------------------------------------------
# AAC app (pygame)
# ---------------------------------------------------------------------------
class AACApp:
    SCAN_DEFAULT = 2.0

    def __init__(self, reader: SerialReader, scan_interval: float,
                 rms_commit: float):
        self._reader        = reader
        self._scan_interval = scan_interval
        self._rms_commit    = rms_commit
        self._grid          = ScanGrid(ALL_ROWS)
        self._composed      = ""
        self._predictor     = WordPredictor(WORD_CORPUS)
        self._predictions: List[str] = []
        self._tts           = TTSEngine()
        self._last_scan     = time.time()

    def run(self):
        import pygame
        pygame.init()

        W, H = 1280, 800
        screen = pygame.display.set_mode((W, H))
        pygame.display.set_caption("PREHEND-SPEAK  —  AAC Keyboard")

        fnt_lg = pygame.font.SysFont("monospace", 30, bold=True)
        fnt_md = pygame.font.SysFont("monospace", 19)
        fnt_sm = pygame.font.SysFont("monospace", 14)

        HEADER_H = 90
        PRED_H   = 36
        GRID_TOP = HEADER_H + PRED_H + 6
        ROW_H    = (H - GRID_TOP - 8) // len(ALL_ROWS)

        def row_rect(ri: int) -> pygame.Rect:
            return pygame.Rect(8, GRID_TOP + ri * ROW_H, W - 16, ROW_H - 2)

        def cell_rect(ri: int, ci: int) -> pygame.Rect:
            n = len(ALL_ROWS[ri])
            cw = (W - 16) // n
            return pygame.Rect(8 + ci * cw, GRID_TOP + ri * ROW_H, cw - 2, ROW_H - 2)

        clock = pygame.time.Clock()
        running = True

        while running:
            # -- pygame events --
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        running = False
                    elif ev.key in (pygame.K_SPACE, pygame.K_RETURN):
                        self._do(ACTION_SELECT)
                    elif ev.key in (pygame.K_RIGHT, pygame.K_DOWN, pygame.K_TAB):
                        self._do(ACTION_NEXT)
                    elif ev.key in (pygame.K_LEFT, pygame.K_UP, pygame.K_BACKSPACE):
                        self._do(ACTION_BACK)

            # -- drain serial events --
            while not self._reader.event_q.empty():
                try:
                    ev = self._reader.event_q.get_nowait()
                    action = self._route(ev)
                    if action:
                        self._do(action)
                        self._last_scan = time.time()
                except queue.Empty:
                    break

            # -- auto-scan --
            if time.time() - self._last_scan >= self._scan_interval:
                self._last_scan = time.time()
                self._grid.next()

            # -- draw --
            screen.fill(BG)

            # header
            pygame.draw.rect(screen, (28, 28, 28), (0, 0, W, HEADER_H))
            disp = (self._composed[-52:] if self._composed else "[ start composing… ]")
            screen.blit(fnt_lg.render(disp, True, TXT_COMPOSED), (12, 28))

            # prediction bar
            py = HEADER_H + 4
            for i, pw in enumerate(self._predictions[:3]):
                px = 12 + i * 420
                pygame.draw.rect(screen, (28, 55, 28),
                                 (px, py, 410, PRED_H - 2), border_radius=4)
                screen.blit(fnt_md.render(pw, True, TXT_PRED), (px + 6, py + 7))

            # grid
            for ri, row in enumerate(ALL_ROWS):
                is_hl_row  = (ri == self._grid.current_row)
                scan_cells = is_hl_row and self._grid.in_cells
                for ci, lbl in enumerate(row):
                    hl_cell = scan_cells and (ci == self._grid.current_cell)
                    if hl_cell:
                        bg = CELL_HL_CEL
                    elif is_hl_row and not self._grid.in_cells:
                        bg = CELL_HL_ROW
                    else:
                        bg = CELL_NORMAL
                    r = cell_rect(ri, ci)
                    pygame.draw.rect(screen, bg, r, border_radius=3)
                    pygame.draw.rect(screen, BORDER, r, 1, border_radius=3)
                    tc = TXT_HL if (hl_cell or is_hl_row) else TXT_NORMAL
                    ts = fnt_sm.render(lbl, True, tc)
                    screen.blit(ts, ts.get_rect(center=r.center))

            # status bar
            lvl = ("CELL " + str(self._grid.current_cell + 1)
                   if self._grid.in_cells
                   else "ROW " + str(self._grid.current_row + 1))
            st = fnt_sm.render(
                f"PREHEND-SPEAK  |  {lvl}  |  scan {self._scan_interval:.1f}s  "
                f"|  ESC = exit → restore GRASP",
                True, (100, 100, 100))
            screen.blit(st, (12, H - 18))

            pygame.display.flip()
            clock.tick(30)

        self._reader.stop()
        pygame.quit()

    # ---- action dispatcher -----------------------------------------------
    def _route(self, ev: dict) -> Optional[str]:
        eog = ev.get("eogEvent", 0)
        imu = ev.get("imuGesture", 0)
        emg = ev.get("emgNorm", 0.0)
        if eog == EOG_LONG or imu == IMU_NOD or emg > self._rms_commit:
            return ACTION_SELECT
        if eog == EOG_SHORT or imu == IMU_TILT:
            return ACTION_NEXT
        if imu == IMU_SHAKE:
            return ACTION_BACK
        return None

    def _do(self, action: str):
        if action == ACTION_NEXT:
            self._grid.next()

        elif action == ACTION_BACK:
            self._grid.back()

        elif action == ACTION_SELECT:
            chosen = self._grid.select()
            if chosen is None:
                return   # entered cell-scan level; nothing to append yet

            if chosen == "[SPEAK]":
                text = self._composed.strip()
                if text:
                    self._tts.speak(text)
                    print(f"[TTS] speaking: {text!r}")
            elif chosen == "[SPACE]":
                self._composed += " "
            elif chosen == "[BACK]":
                self._composed = self._composed[:-1]
            elif chosen == "[CLEAR]":
                self._composed = ""
            else:
                # single letter → append; word → append with trailing space
                self._composed += chosen + (" " if len(chosen) > 1 else "")

            # word prediction from last incomplete token
            tokens = self._composed.rstrip().split()
            last = tokens[-1] if tokens else ""
            self._predictions = self._predictor.predict(last)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="PREHEND-SPEAK scanning AAC keyboard")
    p.add_argument("port", nargs="?", default="COM3",
                   help="Serial port (default: COM3)")
    p.add_argument("--scan", type=float, default=AACApp.SCAN_DEFAULT,
                   help="Auto-scan interval in seconds (default: 2.0)")
    p.add_argument("--rms-commit", type=float, default=0.35,
                   help="EMG burst fraction for SELECT (match firmware rmsCommit)")
    return p.parse_args()


def main():
    args = parse_args()
    reader = SerialReader(args.port, BAUD)
    reader.start()
    app = AACApp(reader, scan_interval=args.scan, rms_commit=args.rms_commit)
    try:
        app.run()
    except KeyboardInterrupt:
        reader.stop()


if __name__ == "__main__":
    main()

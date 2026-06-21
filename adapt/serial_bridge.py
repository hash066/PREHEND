"""ADAPT — serial bridge to the SIGNAL firmware (fixed wire protocol).

Telemetry  Arduino -> host (firmware Section 6.12), one decimated CSV row:
    t_ms, emgRMS, tkeoEnv, state, lastCmd, falseNegCount
Remap      host -> Arduino (firmware Section 6.10 parseRemapCommand):
    REMAP,<LOGICAL>,<PATTERN1>[_<PATTERN2>]\n      e.g.  REMAP,HOME,SHORT_SHORT

CLAUDE.md rule 4: the .ino parser and this reader are changed TOGETHER, never
one alone. The casts and the REMAP grammar below mirror the firmware exactly.
"""
from __future__ import annotations

from typing import Iterator, Optional

from .commands import COMMANDS, LOGICALS

# Firmware enums (hardware/firmware ref Section 6.3 / 6.4), integer wire values.
STATE_NAMES = {0: "S_IDLE", 1: "S_BURST", 2: "S_CLASSIFY", 3: "S_ACTUATE", 4: "S_CONFIRM", 5: "S_LOCKOUT"}
CMD_NAMES = {0: "CMD_NONE", 1: "CMD_SHORT", 2: "CMD_LONG", 3: "CMD_DOUBLE"}

TELEMETRY_FIELDS = ("t_ms", "emg_rms", "tkeo_env", "state", "last_cmd", "false_neg_count")


def parse_telemetry(line: str) -> Optional[dict]:
    """Parse one telemetry CSV row. Returns a dict, or None if malformed.

    Robust by design: the firmware also emits human-readable lines (e.g.
    "SIGNAL+ADAPT ready.", "CAL done.", "REMAPPED HOME"), which must be skipped
    without raising.
    """
    parts = [p.strip() for p in line.strip().split(",")]
    if len(parts) != 6:
        return None
    try:
        row = {
            "t_ms": int(parts[0]),
            "emg_rms": float(parts[1]),
            "tkeo_env": float(parts[2]),
            "state": int(parts[3]),
            "last_cmd": int(parts[4]),
            "false_neg_count": int(parts[5]),
        }
    except ValueError:
        return None
    row["state_name"] = STATE_NAMES.get(row["state"], "UNKNOWN")
    row["last_cmd_name"] = CMD_NAMES.get(row["last_cmd"], "UNKNOWN")
    return row


def build_remap(logical: str, pattern: str) -> str:
    """Build a REMAP line for the firmware.

    Args:
        logical: target logical command — SELECT / HOME / BACK.
        pattern: burst pattern that should now trigger it — "SHORT", or a
            compound like "SHORT_SHORT" (primary_secondary).

    Returns the exact bytes-ready line including trailing newline.
    Raises ValueError on any token the firmware parser would reject.
    """
    logical = logical.upper()
    if logical not in LOGICALS:
        raise ValueError(f"logical must be one of {LOGICALS}, got {logical!r}")
    tokens = pattern.upper().split("_")
    if not (1 <= len(tokens) <= 2) or any(t not in COMMANDS for t in tokens):
        raise ValueError(f"pattern must be CMD or CMD_CMD from {COMMANDS}, got {pattern!r}")
    return f"REMAP,{logical},{'_'.join(tokens)}\n"


def parse_remap(line: str) -> tuple[str, list[str]]:
    """Inverse of build_remap (for round-trip tests / a firmware-side simulator).

    Mirrors firmware parseRemapCommand: split on the first two commas, then split
    the pattern on '_'. Returns (logical, [pattern_tokens]).
    """
    line = line.strip()
    if not line.startswith("REMAP,"):
        raise ValueError(f"not a REMAP line: {line!r}")
    c1 = line.index(",")
    c2 = line.index(",", c1 + 1)
    logical = line[c1 + 1 : c2]
    new_pat = line[c2 + 1 :]
    tokens = new_pat.split("_")
    return logical, tokens


class SerialBridge:
    """Thin pyserial wrapper for real hardware. Import-safe without pyserial.

    Usage (real device):
        bridge = SerialBridge("COM3")          # or "/dev/ttyACM0"
        for row in bridge.read_telemetry():    # yields parsed dict rows
            ...
        bridge.send_remap("HOME", "SHORT_SHORT")
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    def open(self):
        import serial  # imported lazily so the module loads without hardware deps

        self._ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        return self

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def read_telemetry(self, max_idle_reads: Optional[int] = None) -> Iterator[dict]:
        """Yield parsed telemetry rows; silently skips non-telemetry lines.

        max_idle_reads: if set, stop after this many consecutive empty reads (timeouts),
        so a disconnected/closed device doesn't hang the consumer forever. Default None
        keeps streaming indefinitely (the normal live-device case).
        """
        if self._ser is None:
            self.open()
        idle = 0
        while True:
            raw = self._ser.readline().decode(errors="ignore")
            if not raw:
                idle += 1
                if max_idle_reads is not None and idle >= max_idle_reads:
                    return
                continue
            idle = 0
            row = parse_telemetry(raw)
            if row is not None:
                yield row

    def send_remap(self, logical: str, pattern: str) -> str:
        """Send a REMAP command to the firmware. Returns the line sent."""
        if self._ser is None:
            self.open()
        line = build_remap(logical, pattern)
        self._ser.write(line.encode())
        return line

"""PREHEND live dashboard + CSV logger + automatic validation-plot capture.

Reads the decimated telemetry stream from the Arduino (115200 baud,
10 CSV fields: ms,emgNorm,tkeo,fsr,bpm,state,auxRaw,imuPacked,gestureByte,mode).
Also accepts the legacy 6-field format for backward compatibility.


  * Plots EMG + FSR + state live (three subplots).
  * Logs every line to a timestamped CSV.
  * Detects key events and auto-saves annotated validation plots.

Usage
-----
    pip install pyserial matplotlib
    python host/host_dashboard.py                        # COM3, default
    python host/host_dashboard.py COM5                   # explicit port
    python host/host_dashboard.py COM5 --no-plot         # log-only mode
    python host/host_dashboard.py COM5 --plot-dir out/   # custom plot dir
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime
from typing import List, Optional, Tuple

import serial

# ---------------------------------------------------------------------------
# State definitions
# ---------------------------------------------------------------------------
STATE_NAMES = {
    0: "IDLE",
    1: "ARMED",
    2: "PREPOS",
    3: "COMMIT",
    4: "HOLD",
    5: "RELEASE",
    6: "ABORT",
    7: "LOCKOUT",
}

STATE_COLORS = {
    0: "#3b3b3b",   # IDLE      – dark grey
    1: "#1976d2",   # ARMED     – blue
    2: "#ffa726",   # PREPOS    – orange
    3: "#66bb6a",   # COMMIT    – green
    4: "#29b6f6",   # HOLD      – sky-blue
    5: "#ab47bc",   # RELEASE   – purple
    6: "#ef5350",   # ABORT     – red
    7: "#78909c",   # LOCKOUT   – blue-grey
}

BAUD = 115200
RING_LEN = 500          # live-plot rolling-window length
SLIP_DFDT_THRESH = -50  # FSR derivative spike threshold (units / sample)

# ---------------------------------------------------------------------------
# Telemetry sample
# ---------------------------------------------------------------------------
GESTURE_NAMES = {
    # bits 3-0 = eogEvent
    0x01: "BLINK-SHORT", 0x02: "BLINK-LONG", 0x03: "SACC-R", 0x04: "SACC-L",
    # bits 5-4 = imuGesture (shifted)
    0x10: "NOD", 0x20: "SHAKE", 0x30: "TILT",
}

MODE_NAMES = {0: "GRASP", 1: "SPEAK", 2: "EEG_STREAM"}


def decode_gesture(gb: int) -> str:
    eog = gb & 0x0F
    imu = (gb >> 4) & 0x03
    parts = []
    if eog: parts.append(GESTURE_NAMES.get(eog, f"EOG:{eog}"))
    if imu: parts.append(GESTURE_NAMES.get(imu << 4, f"IMU:{imu}"))
    return "+".join(parts) if parts else ""


class Sample:
    """One telemetry row (supports 6-field legacy and 10-field extended formats)."""
    __slots__ = ("ms", "emg", "tkeo", "fsr", "bpm", "state",
                 "aux_raw", "imu_packed", "gesture_byte", "mode")

    def __init__(self, ms: float, emg: float, tkeo: float, fsr: float,
                 bpm: float, state: int,
                 aux_raw: int = 0, imu_packed: int = 0,
                 gesture_byte: int = 0, mode: int = 0):
        self.ms = ms
        self.emg = emg
        self.tkeo = tkeo
        self.fsr = fsr
        self.bpm = bpm
        self.state = state
        self.aux_raw = aux_raw
        self.imu_packed = imu_packed
        self.gesture_byte = gesture_byte
        self.mode = mode


# ---------------------------------------------------------------------------
# Event store
# ---------------------------------------------------------------------------
class EventStore:
    """Accumulates events and the raw sample history needed for plotting."""

    def __init__(self, plot_dir: str):
        self.plot_dir = plot_dir
        os.makedirs(plot_dir, exist_ok=True)

        # Full sample history (unbounded – fine for typical session lengths)
        self.history: List[Sample] = []

        # State-transition tracking
        self.prev_state: Optional[int] = None
        self.prepos_onset_ms: Optional[float] = None
        self.cycle_start_idx: Optional[int] = None  # index in history

        # Counters / stats
        self.prepos_leads_ms: List[float] = []
        self.abort_count: int = 0
        self.slip_save_count: int = 0
        self.grasp_cycle_count: int = 0

        # Slip detection helper – last two FSR values
        self._fsr_prev: Optional[float] = None

    # -- public API ----------------------------------------------------------

    def push(self, s: Sample) -> None:
        """Record sample and run event detectors."""
        self.history.append(s)
        cur = s.state
        prev = self.prev_state

        # --- state transitions ---
        if prev is not None and cur != prev:
            self._on_transition(prev, cur, s)

        # --- slip detection while in HOLD ---
        if cur == 4:
            self._check_slip(s)

        self._fsr_prev = s.fsr
        self.prev_state = cur

    def summary(self) -> str:
        """Return a human-readable event summary string."""
        lines = [
            "",
            "=" * 50,
            "  EVENT SUMMARY",
            "=" * 50,
        ]
        if self.prepos_leads_ms:
            avg = sum(self.prepos_leads_ms) / len(self.prepos_leads_ms)
            mn = min(self.prepos_leads_ms)
            mx = max(self.prepos_leads_ms)
            lines.append(
                f"  Pre-position leads: {len(self.prepos_leads_ms)} "
                f"(avg {avg:.0f}ms, min {mn:.0f}ms, max {mx:.0f}ms)"
            )
        else:
            lines.append("  Pre-position leads: 0")
        lines.append(f"  Aborts:             {self.abort_count}")
        lines.append(f"  Slip-saves:         {self.slip_save_count}")
        lines.append(f"  Full grasp cycles:  {self.grasp_cycle_count}")
        lines.append(f"  Plots saved to:     {self.plot_dir}/")
        lines.append("=" * 50)
        return "\n".join(lines)

    # -- internal event handlers ---------------------------------------------

    def _on_transition(self, prev: int, cur: int, s: Sample) -> None:
        # ARMED → PREPOS  ⇒ record onset
        if prev == 1 and cur == 2:
            self.prepos_onset_ms = s.ms

        # PREPOS → COMMIT  ⇒ compute lead, save plot
        if prev == 2 and cur == 3:
            if self.prepos_onset_ms is not None:
                lead = s.ms - self.prepos_onset_ms
                self.prepos_leads_ms.append(lead)
                self._save_prepos_lead_plot(lead, s)
            self.prepos_onset_ms = None

        # PREPOS → ABORT  ⇒ save abort plot
        if prev == 2 and cur == 6:
            self.abort_count += 1
            self._save_abort_plot(s)
            self.prepos_onset_ms = None

        # Track grasp-cycle start
        if prev == 0 and cur != 0:
            self.cycle_start_idx = len(self.history) - 1

        # Grasp-cycle end: RELEASE → IDLE
        if prev == 5 and cur == 0:
            if self.cycle_start_idx is not None:
                self.grasp_cycle_count += 1
                self._save_grasp_cycle_plot(self.cycle_start_idx)
            self.cycle_start_idx = None

    def _check_slip(self, s: Sample) -> None:
        if self._fsr_prev is None:
            return
        dfdt = s.fsr - self._fsr_prev
        if dfdt < SLIP_DFDT_THRESH:
            self.slip_save_count += 1
            self._save_slip_plot(s)

    # -- plot helpers --------------------------------------------------------

    def _ts_tag(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]

    def _window_around(self, ref_ms: float,
                       margin_ms: float = 500.0) -> List[Sample]:
        """Return samples within ±margin_ms of ref_ms."""
        lo = ref_ms - margin_ms
        hi = ref_ms + margin_ms
        return [s for s in self.history if lo <= s.ms <= hi]

    def _save_prepos_lead_plot(self, lead_ms: float, s: Sample) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        win = self._window_around(s.ms)
        if not win:
            return

        t = [x.ms for x in win]
        emg = [x.emg for x in win]
        states = [x.state for x in win]

        fig, ax1 = plt.subplots(figsize=(10, 4))
        ax1.set_xlabel("Time (ms)")
        ax1.set_ylabel("EMG norm", color="tab:blue")
        ax1.plot(t, emg, color="tab:blue", linewidth=0.8, label="EMG norm")
        ax1.tick_params(axis="y", labelcolor="tab:blue")

        ax2 = ax1.twinx()
        ax2.set_ylabel("State", color="tab:grey")
        ax2.step(t, states, color="tab:grey", alpha=0.5, where="post",
                 linewidth=1.2, label="State")
        ax2.tick_params(axis="y", labelcolor="tab:grey")

        ax1.axvline(self.prepos_onset_ms or (s.ms - lead_ms),
                    color="orange", ls="--", label="PREPOS onset")
        ax1.axvline(s.ms, color="green", ls="--", label="COMMIT")
        ax1.set_title(f"Pre-position Lead: {lead_ms:.1f} ms")
        ax1.legend(loc="upper left", fontsize=8)

        fname = os.path.join(self.plot_dir,
                             f"prepos_lead_{self._ts_tag()}.png")
        fig.tight_layout()
        fig.savefig(fname, dpi=150)
        plt.close(fig)
        print(f"[PLOT] prepos lead saved → {fname}")

    def _save_abort_plot(self, s: Sample) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        onset = self.prepos_onset_ms if self.prepos_onset_ms else s.ms - 500
        win = self._window_around((onset + s.ms) / 2,
                                  margin_ms=(s.ms - onset) / 2 + 300)
        if not win:
            return

        t = [x.ms for x in win]
        emg = [x.emg for x in win]

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(t, emg, color="tab:blue", linewidth=0.8)
        ax.axvline(onset, color="orange", ls="--", label="PREPOS onset")
        ax.axvline(s.ms, color="red", ls="--", label="ABORT")
        duration = s.ms - onset
        ax.set_title(f"Abort Event – PREPOS duration {duration:.0f} ms")
        ax.set_xlabel("Time (ms)")
        ax.set_ylabel("EMG norm")
        ax.legend(fontsize=8)

        fname = os.path.join(self.plot_dir, f"abort_{self._ts_tag()}.png")
        fig.tight_layout()
        fig.savefig(fname, dpi=150)
        plt.close(fig)
        print(f"[PLOT] abort saved → {fname}")

    def _save_slip_plot(self, s: Sample) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        win = self._window_around(s.ms, margin_ms=500)
        if not win:
            return

        t = [x.ms for x in win]
        fsr = [x.fsr for x in win]
        # Approximate servo angle from state (state 4 = HOLD → servo closed)
        # We don't have a separate servo field; plot FSR + state as proxy.
        states = [x.state for x in win]

        fig, ax1 = plt.subplots(figsize=(10, 4))
        ax1.set_xlabel("Time (ms)")
        ax1.set_ylabel("FSR", color="tab:red")
        ax1.plot(t, fsr, color="tab:red", linewidth=0.8, label="FSR")
        ax1.tick_params(axis="y", labelcolor="tab:red")

        ax2 = ax1.twinx()
        ax2.set_ylabel("State (servo proxy)", color="tab:grey")
        ax2.step(t, states, color="tab:grey", alpha=0.5, where="post",
                 linewidth=1.2)
        ax2.tick_params(axis="y", labelcolor="tab:grey")

        ax1.axvline(s.ms, color="magenta", ls="--", label="Slip detected")
        ax1.set_title("Slip-Save Event – FSR dip in HOLD")
        ax1.legend(fontsize=8)

        fname = os.path.join(self.plot_dir, f"slip_save_{self._ts_tag()}.png")
        fig.tight_layout()
        fig.savefig(fname, dpi=150)
        plt.close(fig)
        print(f"[PLOT] slip-save saved → {fname}")

    def _save_grasp_cycle_plot(self, start_idx: int) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch

        seg = self.history[start_idx:]
        if len(seg) < 4:
            return

        t   = [x.ms for x in seg]
        emg = [x.emg for x in seg]
        tkeo = [x.tkeo for x in seg]
        fsr = [x.fsr for x in seg]
        bpm = [x.bpm for x in seg]
        states = [x.state for x in seg]

        fig, (ax1, ax2, ax3, ax4) = plt.subplots(
            4, 1, figsize=(12, 10), sharex=True,
            gridspec_kw={"height_ratios": [3, 2, 1.2, 1.5]}
        )

        # Panel 1: EMG + TKEO
        ax1.set_ylabel("EMG norm", color="tab:blue")
        ax1.plot(t, emg, color="tab:blue", linewidth=0.7, label="EMG norm")
        ax1.tick_params(axis="y", labelcolor="tab:blue")
        ax1t = ax1.twinx()
        ax1t.set_ylabel("TKEO", color="tab:orange")
        ax1t.plot(t, tkeo, color="tab:orange", linewidth=0.7, alpha=0.8,
                  label="TKEO")
        ax1t.tick_params(axis="y", labelcolor="tab:orange")
        ax1.set_title("Full Grasp Cycle")
        ax1.legend(loc="upper left", fontsize=7)
        ax1t.legend(loc="upper right", fontsize=7)

        # Panel 2: FSR with state-coloured background
        ax2.set_ylabel("FSR")
        ax2.plot(t, fsr, color="tab:red", linewidth=0.7)
        self._add_state_bands(ax2, t, states)

        # Panel 3: State step
        ax3.set_ylabel("State")
        ax3.step(t, states, color="black", where="post", linewidth=1)
        ax3.set_yticks(list(STATE_NAMES.keys()))
        ax3.set_yticklabels(list(STATE_NAMES.values()), fontsize=6)

        # Panel 4: BPM
        ax4.set_ylabel("BPM")
        ax4.plot(t, bpm, color="tab:green", linewidth=0.7)
        ax4.set_xlabel("Time (ms)")

        # Legend for state colours
        patches = [Patch(facecolor=STATE_COLORS.get(k, "#ccc"), label=v,
                         alpha=0.3) for k, v in STATE_NAMES.items()]
        ax2.legend(handles=patches, loc="upper right", fontsize=6, ncol=4)

        fname = os.path.join(self.plot_dir,
                             f"grasp_cycle_{self._ts_tag()}.png")
        fig.tight_layout()
        fig.savefig(fname, dpi=150)
        plt.close(fig)
        print(f"[PLOT] grasp cycle saved → {fname}")

    @staticmethod
    def _add_state_bands(ax, t: list, states: list) -> None:
        """Draw coloured vertical bands for each contiguous state region."""
        if not t:
            return
        i = 0
        while i < len(t):
            j = i
            while j < len(t) and states[j] == states[i]:
                j += 1
            color = STATE_COLORS.get(states[i], "#ccc")
            ax.axvspan(t[i], t[j - 1], alpha=0.15, color=color)
            i = j


# ---------------------------------------------------------------------------
# Live display
# ---------------------------------------------------------------------------
class LiveDisplay:
    """Manages the interactive matplotlib figure with three subplots."""

    def __init__(self, ring_len: int = RING_LEN):
        import matplotlib
        matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        self.plt = plt

        self.ring_len = ring_len
        self.t_ring: deque = deque(maxlen=ring_len)
        self.emg_ring: deque = deque(maxlen=ring_len)
        self.tkeo_ring: deque = deque(maxlen=ring_len)
        self.fsr_ring: deque = deque(maxlen=ring_len)
        self.state_ring: deque = deque(maxlen=ring_len)
        self.bpm_ring: deque = deque(maxlen=ring_len)

        self.plt.ion()
        self.fig, (self.ax_emg, self.ax_fsr, self.ax_state) = \
            self.plt.subplots(3, 1, figsize=(11, 7), sharex=True)
        self.fig.canvas.manager.set_window_title("PREHEND Dashboard")
        self.ax_tkeo = self.ax_emg.twinx()

        self._update_counter = 0

    def push(self, s: Sample) -> None:
        self.t_ring.append(s.ms)
        self.emg_ring.append(s.emg)
        self.tkeo_ring.append(s.tkeo)
        self.fsr_ring.append(s.fsr)
        self.state_ring.append(s.state)
        self.bpm_ring.append(s.bpm)

        # Throttle redraw to every 4th sample for performance
        self._update_counter += 1
        if self._update_counter % 4 != 0:
            return
        self._redraw(s)

    def _redraw(self, s: Sample) -> None:
        t = list(self.t_ring)

        # -- Top: EMG + TKEO --
        self.ax_emg.cla()
        self.ax_tkeo.cla()
        self.ax_emg.plot(t, list(self.emg_ring), color="tab:blue",
                         linewidth=0.7, label="EMG norm")
        self.ax_emg.set_ylabel("EMG norm", color="tab:blue")
        self.ax_emg.tick_params(axis="y", labelcolor="tab:blue")
        self.ax_tkeo.plot(t, list(self.tkeo_ring), color="tab:orange",
                          linewidth=0.7, alpha=0.7, label="TKEO")
        self.ax_tkeo.set_ylabel("TKEO", color="tab:orange")
        self.ax_tkeo.tick_params(axis="y", labelcolor="tab:orange")
        self.ax_emg.legend(loc="upper left", fontsize=7)
        self.ax_tkeo.legend(loc="upper right", fontsize=7)

        state_name = STATE_NAMES.get(s.state, "?")
        elapsed = s.ms / 1000.0
        gesture_str = decode_gesture(s.gesture_byte)
        mode_str = MODE_NAMES.get(s.mode, f"M{s.mode}")
        extra = f"  |  Gesture: {gesture_str}" if gesture_str else ""
        self.ax_emg.set_title(
            f"PREHEND [{mode_str}]  |  State: {state_name} ({s.state})  |  "
            f"BPM: {s.bpm:.0f}  |  Elapsed: {elapsed:.1f}s{extra}",
            fontsize=10, fontweight="bold",
        )

        # -- Middle: FSR + state bands --
        self.ax_fsr.cla()
        self.ax_fsr.plot(t, list(self.fsr_ring), color="tab:red",
                         linewidth=0.7, label="FSR")
        self.ax_fsr.set_ylabel("FSR")
        EventStore._add_state_bands(self.ax_fsr, t, list(self.state_ring))

        # -- Bottom: State step + BPM --
        self.ax_state.cla()
        self.ax_state.step(t, list(self.state_ring), color="black",
                           where="post", linewidth=1, label="State")
        self.ax_state.set_ylabel("State")
        self.ax_state.set_yticks(list(STATE_NAMES.keys()))
        self.ax_state.set_yticklabels(list(STATE_NAMES.values()), fontsize=6)
        ax_bpm = self.ax_state.twinx()
        ax_bpm.cla()
        ax_bpm.plot(t, list(self.bpm_ring), color="tab:green",
                    linewidth=0.7, alpha=0.6, label="BPM")
        ax_bpm.set_ylabel("BPM", color="tab:green")
        ax_bpm.tick_params(axis="y", labelcolor="tab:green")
        self.ax_state.set_xlabel("Time (ms)")

        try:
            self.plt.pause(0.001)
        except Exception:
            pass  # ignore if window closed


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="host_dashboard",
        description="PREHEND live dashboard, CSV logger & auto-plot capture.",
    )
    p.add_argument("port", nargs="?", default="COM3",
                   help="Serial port (default: COM3)")
    p.add_argument("--no-plot", action="store_true",
                   help="Disable live matplotlib window (log-only mode)")
    p.add_argument("--plot-dir", default="plots",
                   help="Directory for auto-captured plots (default: plots/)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()

    # --- Serial ---
    ser = serial.Serial(args.port, BAUD, timeout=1)
    print(f"[INFO] Connected to {args.port} @ {BAUD} baud")

    # --- CSV log ---
    csv_path = f"prehend_{int(time.time())}.csv"
    csv_fh = open(csv_path, "w", newline="")
    csv_writer = csv.writer(csv_fh)
    csv_writer.writerow(["ms", "emgNorm", "tkeo", "fsr", "bpm", "state",
                         "auxRaw", "imuPacked", "gestureByte", "mode"])
    print(f"[INFO] Logging to {csv_path}")

    # --- Event store ---
    events = EventStore(args.plot_dir)
    print(f"[INFO] Auto-plots will be saved to {args.plot_dir}/")

    # --- Live display ---
    display: Optional[LiveDisplay] = None
    if not args.no_plot:
        display = LiveDisplay()

    # --- Graceful shutdown ---
    shutdown_requested = False

    def _handle_signal(sig, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)

    sample_count = 0

    print("[INFO] Dashboard running – press Ctrl+C to stop.\n")

    try:
        while not shutdown_requested:
            try:
                raw = ser.readline()
            except serial.SerialException:
                print("[WARN] Serial read error – retrying…")
                time.sleep(0.1)
                continue

            line = raw.decode(errors="ignore").strip()
            if not line:
                continue

            parts = line.split(",")
            if len(parts) < 6:
                continue

            try:
                ms    = float(parts[0])
                emg   = float(parts[1])
                tkeo  = float(parts[2])
                fsr   = float(parts[3])
                bpm   = float(parts[4])
                state = int(parts[5])
                # extended fields (firmware ≥ PREHEND-SPEAK)
                aux_raw      = int(parts[6])  if len(parts) > 6 else 0
                imu_packed   = int(parts[7])  if len(parts) > 7 else 0
                gesture_byte = int(parts[8])  if len(parts) > 8 else 0
                mode         = int(parts[9])  if len(parts) > 9 else 0
            except (ValueError, IndexError):
                continue

            # Pad legacy rows to 10 fields for consistent CSV
            while len(parts) < 10:
                parts.append("0")
            csv_writer.writerow(parts[:10])
            sample_count += 1

            if sample_count % 100 == 0:
                csv_fh.flush()

            s = Sample(ms, emg, tkeo, fsr, bpm, state,
                       aux_raw, imu_packed, gesture_byte, mode)

            # Event detection (runs even in no-plot mode)
            events.push(s)

            # Live display
            if display is not None:
                display.push(s)

    except KeyboardInterrupt:
        pass  # fallthrough to cleanup

    # --- Cleanup ---
    print("\n[INFO] Shutting down…")

    try:
        ser.close()
        print("[INFO] Serial port closed.")
    except Exception:
        pass

    csv_fh.flush()
    csv_fh.close()
    print(f"[INFO] CSV flushed and closed ({sample_count} samples).")

    print(events.summary())


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()

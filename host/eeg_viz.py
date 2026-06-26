"""PREHEND EEG Visualiser — live delta/theta/alpha/beta/gamma band power.

Reads the PREHEND 10-field telemetry stream (field 6 = auxRaw, the DC-blocked
A1 signal) and displays real-time brainwave band power using a matplotlib
FuncAnimation bar chart.

Requires the device to be in M2 (EEG_STREAM) mode with the EXG Pill placed
at Cz/Fz (10-20 system) for EEG, or any scalp location for research.

NOTE: Do NOT run this script and host_dashboard.py simultaneously on the same
serial port — they cannot share one UART connection. Use one at a time.

Alpha burst detection: if the alpha band fraction exceeds 30 % for ≥ 2 s, an
ALPHA BURST event is printed and optionally 'P' is sent to the device (which
triggers the existing Bereitschaftspotential early pre-position pathway).

Usage
-----
    pip install pyserial numpy matplotlib
    python host/eeg_viz.py                      # COM3, auto M2
    python host/eeg_viz.py COM5                 # explicit port
    python host/eeg_viz.py COM5 --bp-trigger    # send 'P' on alpha burst
    python host/eeg_viz.py COM5 --no-m2         # skip M2 switch (already set)
"""

from __future__ import annotations

import argparse
import threading
import time
from typing import Optional

import numpy as np
import serial

BAUD     = 115200
FS_HOST  = 100        # telemetry output rate (Hz)
BUF_LEN  = 2048       # ring buffer → 20.48 s history
FFT_WIN  = 512        # FFT window → 5.12 s, 0.195 Hz resolution
UPD_HZ   = 20         # plot refresh rate

# EEG frequency bands [lo, hi] Hz
BANDS = {
    "δ delta\n(0.5–4 Hz)":  (0.5,  4.0),
    "θ theta\n(4–8 Hz)":    (4.0,  8.0),
    "α alpha\n(8–13 Hz)":   (8.0, 13.0),
    "β beta\n(13–30 Hz)":  (13.0, 30.0),
    "γ gamma\n(30–45 Hz)": (30.0, 45.0),
}
BAND_COLORS = ["#5c6bc0", "#26a69a", "#66bb6a", "#ffa726", "#ef5350"]
ALPHA_IDX   = 2    # index of α in BANDS
ALPHA_THRESH = 0.30
ALPHA_BURST_SECS = 2.0


# ---------------------------------------------------------------------------
# Ring buffer (thread-safe)
# ---------------------------------------------------------------------------
class RingBuffer:
    def __init__(self, n: int):
        self._buf  = np.zeros(n, dtype=np.float32)
        self._idx  = 0
        self._n    = n
        self._lock = threading.Lock()

    def push(self, val: float):
        with self._lock:
            self._buf[self._idx % self._n] = val
            self._idx += 1

    def window(self, length: int) -> np.ndarray:
        with self._lock:
            end = self._idx
            if end == 0:
                return np.zeros(length, dtype=np.float32)
            if end < length:
                length = end
            idx = end % self._n
            if idx >= length:
                return self._buf[idx - length : idx].copy()
            tail = length - idx
            return np.concatenate([self._buf[self._n - tail :], self._buf[:idx]])

    @property
    def count(self): return self._idx


# ---------------------------------------------------------------------------
# Band power extractor
# ---------------------------------------------------------------------------
def band_power(sig: np.ndarray, fs: float) -> np.ndarray:
    if len(sig) < 8:
        return np.zeros(len(BANDS))
    win  = np.hanning(len(sig)).astype(np.float32)
    fft  = np.abs(np.fft.rfft(sig * win)) ** 2
    freq = np.fft.rfftfreq(len(sig), d=1.0 / fs)
    pwr  = np.zeros(len(BANDS))
    for i, (_, (lo, hi)) in enumerate(BANDS.items()):
        mask = (freq >= lo) & (freq <= hi)
        pwr[i] = fft[mask].mean() if mask.any() else 0.0
    total = pwr.sum()
    if total > 0:
        pwr /= total
    return pwr


# ---------------------------------------------------------------------------
# Serial reader (daemon thread)
# ---------------------------------------------------------------------------
class SerialReader:
    def __init__(self, port: str, buf: RingBuffer,
                 bp_trigger: bool, set_m2: bool):
        self._port = port
        self._buf  = buf
        self._bp   = bp_trigger
        self._m2   = set_m2
        self._ser: Optional[serial.Serial] = None
        self._stop = threading.Event()

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
                if self._m2:
                    self.send("M0")
                    time.sleep(0.3)
                self._ser.close()
            except Exception:
                pass

    def _run(self):
        try:
            self._ser = serial.Serial(self._port, BAUD, timeout=1)
        except serial.SerialException as e:
            print(f"[SERIAL] Cannot open {self._port}: {e}")
            return
        if self._m2:
            time.sleep(1.5)
            self.send("M2")
            print(f"[SERIAL] {self._port} open — device set to EEG_STREAM mode")

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
            if len(parts) < 7:
                continue
            try:
                self._buf.push(float(parts[6]))   # auxRaw field
            except (ValueError, IndexError):
                continue


# ---------------------------------------------------------------------------
# Visualiser
# ---------------------------------------------------------------------------
def run_viz(port: str, bp_trigger: bool, set_m2: bool):
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    import matplotlib.animation as animation

    buf    = RingBuffer(BUF_LEN)
    reader = SerialReader(port, buf, bp_trigger, set_m2)
    reader.start()

    alpha_since: Optional[float] = None
    smooth_pwr = np.zeros(len(BANDS))

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor("#111111")
    ax.set_facecolor("#1a1a1a")

    labels = list(BANDS.keys())
    bars   = ax.barh(labels, [0.2] * len(BANDS), color=BAND_COLORS, height=0.6)
    ax.set_xlim(0, 1.0)
    ax.set_xlabel("Relative Power", color="#cccccc", fontsize=10)
    ax.set_title("PREHEND — Live Brainwave Bands (EEG via EXG Pill)",
                 color="#ffffff", fontsize=12, fontweight="bold")
    ax.tick_params(colors="#cccccc", labelsize=9)
    for sp in ax.spines.values():
        sp.set_edgecolor("#444444")

    ax.axvline(ALPHA_THRESH, color="#66bb6a", linestyle="--",
               linewidth=1.2, alpha=0.7,
               label=f"α alert threshold ({ALPHA_THRESH:.0%})")
    ax.legend(facecolor="#222222", labelcolor="#cccccc",
              fontsize=8, loc="lower right")

    status = ax.text(0.99, -0.14, "", transform=ax.transAxes,
                     ha="right", va="top", color="#aaaaaa", fontsize=8)
    alert  = ax.text(0.5, 0.97, "", transform=ax.transAxes,
                     ha="center", va="top", color="#ffa726",
                     fontsize=11, fontweight="bold")

    def update(_frame):
        nonlocal alpha_since
        if buf.count < 32:
            return bars

        sig = buf.window(FFT_WIN)
        pwr = band_power(sig, float(FS_HOST))

        # EMA smoothing for display
        for i in range(len(BANDS)):
            smooth_pwr[i] = 0.3 * pwr[i] + 0.7 * smooth_pwr[i]

        for bar, val in zip(bars, smooth_pwr):
            bar.set_width(float(val))

        # Alpha burst detection
        alpha_val = float(smooth_pwr[ALPHA_IDX])
        now = time.time()
        if alpha_val > ALPHA_THRESH:
            if alpha_since is None:
                alpha_since = now
            elapsed = now - alpha_since
            if elapsed >= ALPHA_BURST_SECS:
                msg = f"ALPHA BURST  {elapsed:.1f}s  ({alpha_val:.0%})"
                alert.set_text(msg)
                if bp_trigger:
                    reader.send("P")
                    print(f"\n[ALPHA BURST] → sent 'P' to device  α={alpha_val:.1%}")
                alpha_since = now  # reset to avoid repeated triggers
            else:
                alert.set_text(f"α rising… {elapsed:.1f}s")
        else:
            alpha_since = None
            alert.set_text("")

        status.set_text(
            f"δ={smooth_pwr[0]:.1%}  θ={smooth_pwr[1]:.1%}  "
            f"α={smooth_pwr[2]:.1%}  β={smooth_pwr[3]:.1%}  "
            f"γ={smooth_pwr[4]:.1%}   n={buf.count}"
        )
        return bars

    ani = animation.FuncAnimation(
        fig, update,
        interval=int(1000 / UPD_HZ),
        blit=False,
        cache_frame_data=False,
    )

    try:
        plt.tight_layout()
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="PREHEND live EEG brainwave bands")
    p.add_argument("port", nargs="?", default="COM3",
                   help="Serial port (default: COM3)")
    p.add_argument("--bp-trigger", action="store_true",
                   help="Send 'P' to device on sustained alpha burst (BP pathway)")
    p.add_argument("--no-m2", action="store_true",
                   help="Skip automatic M2 mode switch (device already in EEG_STREAM)")
    return p.parse_args()


def main():
    args = parse_args()
    run_viz(args.port, args.bp_trigger, not args.no_m2)


if __name__ == "__main__":
    main()

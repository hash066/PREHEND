"""PREHEND research-tier EEG/BP early trigger (EXPERIMENTAL).

NOT in the control path. Detects a sustained negative slope in the
0.1-3 Hz EEG band (the Bereitschaftspotential) and sends 'P' to the
Arduino to force an early PRE-POSITION. EMG must still confirm within
tAbortMs or the cascade ABORTs, so a false BP never grasps on its own.

Requires AUX wired as EEG (set `auxMode = AUX_EEG` in the firmware) and
the EEG channel present in the telemetry's `tkeo` column slot — adjust
the parsed index to match what you stream. TAU_BP must be calibrated
per user.

    pip install pyserial numpy scipy
    python host/bp_trigger.py COM3
"""
import sys

import serial
import numpy as np
from scipy.signal import butter, sosfilt

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
fs = 500.0
TAU_BP = -0.8  # uV/s, calibrate per user

ser = serial.Serial(PORT, 115200)
sos = butter(4, [0.1 / (fs / 2), 3.0 / (fs / 2)], btype="band", output="sos")
buf = np.zeros(int(0.4 * fs))


def bp_slope(x):
    tt = np.linspace(0, len(x) / fs, len(x))
    return np.polyfit(tt, x, 1)[0]


while True:
    raw = ser.readline().decode(errors="ignore").strip().split(",")
    if len(raw) < 3:
        continue
    buf = np.roll(buf, -1)
    buf[-1] = float(raw[2])  # EEG channel slot in the telemetry line
    if bp_slope(sosfilt(sos, buf)) < TAU_BP:
        ser.write(b"P")  # early PRE-POSITION

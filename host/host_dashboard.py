"""PREHEND live dashboard + CSV logger.

Reads the decimated telemetry stream from the Arduino (115200 baud,
6 CSV fields: ms,emgNorm,tkeo,fsr,bpm,state) and plots EMG + FSR live
while logging every line to a timestamped CSV.

    pip install pyserial matplotlib
    python host/host_dashboard.py            # uses COM3 by default
    python host/host_dashboard.py COM5       # or pass your port
"""
import sys
import csv
import time
from collections import deque

import serial
import matplotlib.pyplot as plt

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
BAUD = 115200

ser = serial.Serial(PORT, BAUD)
N = 500
emg = deque(maxlen=N)
fsr = deque(maxlen=N)
st = deque(maxlen=N)

log = csv.writer(open(f"prehend_{int(time.time())}.csv", "w", newline=""))
log.writerow(["ms", "emgNorm", "tkeo", "fsr", "bpm", "state"])

plt.ion()
fig, (a1, a2) = plt.subplots(2, 1, sharex=True)

while True:
    line = ser.readline().decode(errors="ignore").strip().split(",")
    if len(line) != 6:
        continue
    log.writerow(line)
    ms, e, tk, f, bpm, s = line
    emg.append(float(e))
    fsr.append(float(f))
    st.append(int(s))
    a1.cla()
    a1.plot(emg, label="EMG norm")
    a1.legend(loc="upper right")
    a2.cla()
    a2.plot(fsr, color="tab:red")
    a2.set_title(f"state={st[-1]} bpm={bpm}")
    plt.pause(0.001)

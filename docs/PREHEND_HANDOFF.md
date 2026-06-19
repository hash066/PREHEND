# PREHEND — Project Handoff (self-contained context for Claude Code)

> **How to use this:** this file is the portable core of the 30-page build spec
> (`PREHEND_Predictive_Grasp_Build_Spec.pdf`). It is fully self-contained — it does
> not depend on the original chat. The complete firmware is in §8 and also lives at
> [`PREHEND/PREHEND.ino`](../PREHEND/PREHEND.ino).

---

## 0. TL;DR

**PREHEND** is a predictive, self-protecting servo-claw grasp controller built from an Upside Down
Labs BioAmp kit + Arduino UNO R4. It fuses two earlier concepts:

- **Anticipatory Grip** — autonomous slip-reflex (catch a slipping object faster than human reaction)
  + haptic grip-force feedback + EMG-open override.
- **A "Predictive Prosthetic" patent idea** — a staged `pre-position → confirm → commit → abort`
  cascade with graded torque and a safety lockout, originally triggered by the EEG
  Bereitschaftspotential (BP).

**The key engineering decision:** the patent's BP trigger (single-trial EEG on a consumer band) is
unreliable and unbuildable in 2 days. PREHEND keeps the patent's *cascade architecture* but triggers
on **EMG onset** instead — EMG leads mechanical force by 50–260 ms (electromechanical delay), so a
fast **TKEO onset** detector pre-positions the claw in that window, and an **RMS confirmation** commits
the grasp. This reproduces the "feels-like-thought" lead time on a reliable signal, **entirely on the
Arduino** (no Rust/Python host — that was dropped). EEG/BP is retained only as an optional research tier.

---

## 1. Builder + hard constraints

- Solo builder, RVCE CSE-Cybersecurity student, Bengaluru. Strong with Arduino/ESP32/IoT, TinyML,
  React/Next/TS, FastAPI. Prefers concise output, direct opinionated answers, honesty about
  novelty/limitations, premium/clean aesthetic.
- **Timeline: ~2 days (hard).** Build the reliable core first; everything else is layered on top.
- **Budget: ≤ ₹1,000 new spend, only if needed; can borrow parts.** Core needs ~₹190.
- **Reliability-first.** Threshold/rule logic is the core; ML is optional, never a dependency.
- **Use owned hardware** (see §2). No exotic parts.
- Wants the firmware to be genuinely buildable/flashable, not pseudocode.

---

## 2. Owned hardware (Upside Down Labs BioAmp kit + more)

- Muscle BioAmp Shield v0.3 (onboard sEMG AFE; stacks on Arduino; exposes A0–A2, a STEMMA port, a servo SRV header)
- Muscle BioAmp Band (sEMG electrode band)
- BioAmp EXG Pill (single-channel configurable AFE: ECG/EMG/EOG/EEG)
- Heart BioAmp Band (ECG), 2-Ch Brain BioAmp Band (EEG)
- Servo Claw (SG90-class servo + gripper)
- Arduino UNO **R4** (USB-C, 14-bit ADC, FPU) **and** Arduino Uno (classic) ; also an ESP32
- BioAmp + AUX cables, 6× STEMMA cables, 9V snap cable, jumpers
- Gel electrodes (24+24 + Boxy + repositionable), electrode/NuPrep gel + wipes

**New parts to buy for the core (~₹190):**

- FSR402 force-sensitive resistor + 10k resistor (divider) — ~₹150
- Coin vibration motor + NPN transistor (2N2222) + 1N4148 flyback diode — ~₹40
- (Optional power) 5V power bank, or use the 9V snap into VIN

---

## 3. Architecture (what runs where)

Everything runs **on the Arduino R4** in one ~1 kHz loop. Three concurrent processes:

1. **Predictive cascade (voluntary):** EMG onset → pre-position → RMS confirm → commit → abort if no confirm.
2. **Slip reflex (autonomous):** FSR force-derivative → tighten faster than human reaction; EMG-open overrides.
3. **Haptic feedback:** grip force (FSR) → coin-motor vibration intensity on the forearm.

Optional layers (do **not** put in the 2-day core):

- **ECG exertion gate** (Heart Band): HRV/heart-rate out of band → LOCKOUT (safety).
- **EEG/BP research trigger** (Brain Band): host-side BP slope detector → even-earlier pre-position.
  EMG still required to COMMIT, so a false BP never grasps on its own.

### The "swappable A1 slot" (important hardware idea)

- A0 = sEMG flexor (Shield onboard AFE).
- A2 = FSR (just a divider, no AFE).
- **A1 = the one EXG Pill**, configured for exactly ONE of: extensor EMG (recommended) / ECG / EEG.
  One Pill = one role at a time. A borrowed 2nd Pill enables two at once.

---

## 4. Pin map

| Pin | Signal | Source / note |
|-----|--------|----------------|
| A0  | sEMG flexor | Muscle Shield onboard AFE; 14-bit; 1 kHz |
| A1  | extensor EMG / ECG / EEG | EXG Pill via STEMMA — set `auxMode` in code |
| A2  | FSR contact force | FSR + 10k divider to 3.3/5V |
| D9  | Servo claw signal | PWM; Shield SRV header |
| D6  | Haptic PWM | Coin motor via NPN transistor + flyback diode |
| LED_BUILTIN | HOLD indicator | |
| VIN/5V/GND | Power | 9V snap into VIN or 5V power bank; common **star** ground |
| USB-C | telemetry only | 115200 baud; do not power from mains while worn |

**Two hard rules:** (1) coin motor only through a transistor + flyback diode, never a bare pin;
(2) battery/power-bank only while electrodes are on skin — never mains. Star-ground so servo current
spikes don't corrupt the µV EMG.

### Electrode placement

- sEMG flexor (A0): flexor digitorum belly, ~2 cm spacing; reference on a bony point.
- EMG extensor (A1, recommended mode): back-of-forearm extensor belly.
- ECG (A1 mode): Lead-I, LA + RA wrists, reference at ankle.
- EEG (A1 mode, research): Cz/Fz (10-20), reference at mastoid.

---

## 5. State machine (8 states)

`IDLE → ARMED → PRE-POSITION → COMMIT → HOLD → RELEASE`, with `ABORT` and `LOCKOUT` branches to IDLE.

| From → To | Condition |
|-----------|-----------|
| IDLE → ARMED | exertion nominal (or ECG layer off) |
| ARMED → PRE-POSITION | TKEO onset energy > `tkOnset` AND emgNorm > 0.05 |
| PRE-POSITION → COMMIT | emgNorm > `rmsCommit` within `tAbortMs` |
| PRE-POSITION → ABORT | `tAbortMs` elapses, no confirmation |
| COMMIT → HOLD | `tHoldMs` elapsed |
| HOLD → (tighten) | dF/dt < −`slipTh` OR force below floor (reflex; stays in HOLD) |
| HOLD → RELEASE | extensor RMS > `extOpen` (or flexor relaxed) |
| RELEASE / ABORT → IDLE | claw reached open |
| ANY → LOCKOUT | ECG out of [hrLo,hrHi] / arrhythmia / out-of-order |
| LOCKOUT → IDLE | 2 s cooldown + exertion nominal |

**Actuation math:**

- Pre-position: `θ_pre = C × CLAW_MAXPRE` (C = onset confidence 0–1)
- Commit: `θ_act = CLAW_MAXPRE + (RMS/MVC) × (CLAW_FULL − CLAW_MAXPRE)`
- Slip boost: `Δθ = clamp(−dF/dt × kSlip, 5°, 30°)`
- All motion rate-limited to `SERVO_RATE` deg/loop, clamped to [open, full]. CLAW_FULL never 180°.

---

## 6. Signal processing (all on-device)

- **EMG (A0):** DC-block (slow running mean) → square + EMA → RMS envelope (commit + grip grading).
  In parallel **TKEO** ψ[n] = x[n]² − x[n−1]·x[n+1] (3-sample) → onset energy (fast pre-position trigger).
- **FSR (A2):** low-pass → derivative dF/dt → slip flag; absolute floor catches slow slips.
- **ECG (A1, optional):** 1st-order high-pass → refractory R-peak → R-R → bpm/HRV → exertion gate.
- **EEG/BP (A1, research, host):** band-pass 0.1–3 Hz → sliding linear-regression slope; sustained
  negative slope = BP → early pre-position.

---

## 7. Parameter defaults

| Param | Default | Meaning |
|-------|---------|---------|
| FS_HZ | 1000 | loop/sample rate |
| CLAW_OPEN / MAXPRE / FULL | 0 / 70 / 160 | servo angles (deg) |
| SERVO_RATE | 6 | max deg/loop |
| emgBase / mvc | calibrated | resting & max-contraction RMS |
| tkOnset | calibrated | TKEO onset trigger |
| rmsCommit | 0.35 | confirm threshold (fraction of MVC) |
| extOpen | calibrated | extensor RMS for release |
| slipTh | 8.0 | |dF/dt| slip threshold |
| kSlip | 0.5 | slip→boost gain |
| fsrFloor | calibrated | contact baseline |
| tAbortMs | 400 | confirm window |
| tHoldMs | 150 | min hold |
| hrLo / hrHi | 45 / 140 | ECG exertion bounds (bpm) |

---

## 8. Complete firmware

The full sketch lives at [`PREHEND/PREHEND.ino`](../PREHEND/PREHEND.ino).

> Board: Arduino UNO R4 (Minima/WiFi). Library: **Servo** (bundled). 14-bit ADC + FPU make the 1 kHz
> float loop comfortable. The sketch auto-detects the board: on a classic Uno it falls back to a
> 10-bit ADC (`ADC_MID = 512`) and skips `analogReadResolution(14)`.

The `'P'` serial command (host BP early-trigger) and `'c'` calibration command are both wired in
`handleSerial()`.

---

## 9. Optional host scripts (NOT in the control path)

- [`host/host_dashboard.py`](../host/host_dashboard.py) — live dashboard + CSV logger
  (`pip install pyserial matplotlib`).
- [`host/bp_trigger.py`](../host/bp_trigger.py) — research-tier EEG/BP early trigger; experimental,
  EMG still gates COMMIT (`pip install pyserial numpy scipy`).

---

## 10. Build order (2 days)

1. **Day1 AM** — read EMG(A0)+FSR(A2); print RMS envelope + TKEO onset. (onset fires early on a flex)
2. **Day1 PM** — cascade FSM ARMED→PREPOS→COMMIT→ABORT; graded servo. (flex pre-positions, follow-through commits, twitch aborts)
3. **Day1 eve** — slip reflex in HOLD + extensor override. (tug tightens; relax/extensor releases)
4. **Day2 AM** — haptic (D6 via transistor) = grip force; calibration ('c'). (you feel grip; 15 s cal)
5. **Day2 PM** — tune thresholds; mount FSR + motor; safe clamps. (reliable pick–hold–release)
6. **Day2 eve** — demo polish + telemetry; (optional) ECG exertion gate.
7. **Beyond** — EEG/BP host trigger; ESP32 wireless dashboard.

Smallest working demo = end of Day 1.

---

## 11. Suggested next tasks for Claude Code

- [x] Create the Arduino sketch from §8 (`PREHEND/PREHEND.ino`); platformio.ini.
- [x] Add a serial command handler so `'P'` (early pre-position) works.
- [ ] Add live param tweaks over serial (e.g. `r0.4` to set rmsCommit).
- [ ] Build a tiny **bench simulator** (feed synthetic EMG/FSR via serial) to test the FSM without hardware.
- [ ] Wire the `host_dashboard.py` and capture validation plots (pre-position lead, abort, slip-save).
- [ ] (Optional) move params to EEPROM so calibration persists across power cycles.
- [ ] (Optional) port the on-device loop to ESP32 for wireless telemetry.

---

## 12. Safety + honesty notes (keep these true)

- **Not a medical device.** Non-certified boards; assistive/augmentation prototype only.
- **Battery/power-bank only** while electrodes are on skin; never mains. Coin motor via transistor+diode.
- Servo rate-limited + clamped to `CLAW_FULL` (never 180°); keep fingers clear; light loads.
- **Honest novelty:** EMG-onset prediction, graded myocontrol, slip-reflex+EMG-override (bebionic3),
  and vibrotactile feedback all exist in research. PREHEND's contribution is the *integration* —
  staged onset→confirm→commit→abort + concurrent slip-reflex + sensory feedback, on accessible owned
  hardware, fully on-device, in 2 days. Not a scientific/patent first. The BP/EEG trigger is the
  novel-but-hard part, kept optional/experimental.

## 13. Key references

- bebionic3 slip-prevention + EMG override (slip = grip-force derivative): PMC5074548
- TKEO EMG onset detection (3-sample; error 40±99 vs 229±356 ms): Solnik 2010 / Li 2007
- Electromechanical delay (EMG leads movement ~50–260 ms): PMC4274888
- Vibrotactile grip feedback: Tchimino 2022 (Front Neurosci 16:952288); Raveh 2018 (Hum Mov Sci 58:32)
- Bereitschaftspotential: Kornhuber & Deecke 1965; Libet 1983
- Upside Down Labs docs: docs.upsidedownlabs.tech ; github.com/upsidedownlabs

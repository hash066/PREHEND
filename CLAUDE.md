# CLAUDE.md — PREHEND project context

> Read this first. The full spec is in [`docs/PREHEND_HANDOFF.md`](docs/PREHEND_HANDOFF.md)
> (the portable core of the 30-page build-spec PDF). When in doubt, the handoff wins.

## What this is

PREHEND is a predictive, self-protecting servo-claw grasp controller on an **Arduino UNO R4** +
Upside Down Labs BioAmp kit. The entire control loop runs **on-device** at ~1 kHz — there is no
Rust/Python host in the control path (host scripts under `host/` are optional telemetry/research only).

It keeps a patent's `pre-position → confirm → commit → abort` cascade but **triggers on EMG onset, not
EEG/BP**. TKEO (3-sample ψ[n] = x[n]² − x[n−1]·x[n+1]) fires the pre-position inside the 50–260 ms
electromechanical-delay window; RMS-vs-MVC confirms and commits. EEG/BP is an optional host tier only,
and EMG still gates COMMIT so a false BP never grasps alone.

Three concurrent processes: predictive cascade (voluntary), slip reflex (autonomous FSR dF/dt with
EMG-open override), haptic feedback (FSR → coin motor). 8-state FSM:
`IDLE → ARMED → PREPOS → COMMIT → HOLD → RELEASE` + `ABORT`/`LOCKOUT`.

## Who the builder is

- Solo, RVCE CSE-Cybersecurity, Bengaluru. Strong with Arduino/ESP32/IoT, TinyML, React/Next/TS, FastAPI.
- Prefers concise, direct, opinionated answers; honesty about novelty/limitations; clean aesthetic.
- **~2-day hard timeline. ≤₹1,000 budget (core ~₹190).** Reliability-first: rules are the core, ML is
  never a dependency.

## How to work here

- **Firmware must be genuinely buildable/flashable, not pseudocode.** Test mentally against the UNO R4
  (14-bit ADC, FPU) and keep the classic-Uno fallback (`ADC_MID`, `analogReadResolution`) intact.
- **Zero blocking in the 1 kHz loop.** Telemetry is decimated (every 10th sample). `delay()` is only
  acceptable inside `runCalibration()`, which is intentionally blocking.
- **Don't add network/cloud to the control path.** The device is self-contained.
- **Safety is non-negotiable** (see handoff §4, §12): coin motor via NPN + flyback diode; battery only
  on skin, never mains; star ground; servo rate-limited and clamped to `CLAW_FULL` (never 180°).
- **Honest novelty.** The contribution is the *integration*, not a scientific/patent first. Don't
  overclaim.

## Pin map (quick ref — full table in handoff §4)

| Pin | Signal |
|-----|--------|
| A0  | flexor sEMG (Shield AFE) |
| A1  | EXG Pill — extensor EMG / ECG / EEG, set by `auxMode` |
| A2  | FSR force (divider) |
| D9  | servo claw |
| D6  | haptic coin motor (via NPN + flyback diode) |

## Layout

```
PREHEND/PREHEND.ino   firmware (the device)
platformio.ini        build targets: uno_r4_wifi (default) / uno_r4_minima / uno
host/                 optional desktop tools — NOT control path
docs/PREHEND_HANDOFF.md  full spec
```

## Open next-tasks (handoff §11)

- Live param tweaks over serial (e.g. set rmsCommit at runtime).
- Bench simulator: feed synthetic EMG/FSR over serial to test the FSM without hardware.
- Wire `host_dashboard.py`; capture validation plots (pre-position lead, abort, slip-save).
- Optional: params → EEPROM (persist calibration); ESP32 wireless port.

## GitHub

This repo is on the **hash066** account (not TheClazer). Push branches; the builder owns merges.

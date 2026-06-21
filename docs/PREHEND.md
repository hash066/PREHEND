<!-- Preserved verbatim from the repo's original root README (the PREHEND claw project),
     relocated here when the root README was rewritten to document the SIGNAL+ADAPT system
     that this repo's PDF specs describe and that the host-side code implements. -->

# PREHEND

**Predictive, self-protecting servo-claw grasp controller** — Arduino UNO R4 + Upside Down Labs
BioAmp kit. Everything runs on-device in one ~1 kHz loop. No host required.

PREHEND keeps the staged `pre-position → confirm → commit → abort` cascade from a "predictive
prosthetic" patent idea, but replaces its unreliable single-trial EEG/BP trigger with an **EMG-onset**
trigger. EMG leads mechanical force by 50–260 ms (electromechanical delay), so a fast **TKEO onset**
detector pre-positions the claw inside that window and an **RMS confirmation** commits the grasp —
reproducing the "feels-like-thought" lead time on a signal that actually works in real time.

Three concurrent processes:

1. **Predictive cascade** (voluntary): EMG onset → pre-position → RMS confirm → commit → abort.
2. **Slip reflex** (autonomous): FSR force-derivative tightens faster than human reaction; EMG-open overrides.
3. **Haptic feedback**: grip force → coin-motor vibration intensity on the forearm.

> Not a medical device. Assistive/augmentation prototype on non-certified hardware.

## Layout

```
PREHEND/PREHEND.ino     # complete firmware (the device)
platformio.ini          # build for uno_r4_wifi / uno_r4_minima / classic uno
host/                   # OPTIONAL desktop tools — not in the control path
├── host_dashboard.py   #   live plot + CSV logger
├── bp_trigger.py       #   research-tier EEG/BP early trigger (experimental)
└── requirements.txt
docs/PREHEND_HANDOFF.md  # full self-contained build spec (pin map, FSM, params, safety)
```

## Build & flash

**PlatformIO (recommended):**

```bash
pio run -t upload            # build + flash the default UNO R4 WiFi
pio device monitor -b 115200 # serial telemetry
```

For a UNO R4 Minima or classic Uno, target the matching env:
`pio run -e uno_r4_minima -t upload` / `pio run -e uno -t upload`.

**Arduino IDE:** open `PREHEND/PREHEND.ino`, select your board, upload. The bundled `Servo`
library is the only dependency.

## Use

1. Wire per the pin map in [`docs/PREHEND_HANDOFF.md`](PREHEND_HANDOFF.md) §4. Set `auxMode`
   in the sketch to match how the EXG Pill on A1 is wired (`AUX_EXTENSOR` recommended).
2. Open the serial monitor at 115200.
3. Send **`c`** to calibrate (relax → clench → open, ~15 s).
4. Flex to pre-position, follow through to commit, relax/extend to release. Tug a held object to see
   the slip reflex tighten.

Serial commands: `c` = calibrate, `P` = force early pre-position (used by the host BP tier; EMG must
still confirm or the cascade aborts).

## Safety (non-negotiable)

- Coin motor only through an NPN transistor + 1N4148 flyback diode — never a bare pin.
- Battery / power-bank only while electrodes are on skin — **never** mains.
- Common **star** ground so servo current spikes don't corrupt the µV EMG.
- Servo is rate-limited and clamped to `CLAW_FULL` (never 180°). Keep fingers clear; light loads.

See [`docs/PREHEND_HANDOFF.md`](PREHEND_HANDOFF.md) for the full spec, references, and build order.

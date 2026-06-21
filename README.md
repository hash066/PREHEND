# SIGNAL + ADAPT

**A single-channel EMG switch-access system (SIGNAL) with a survival-analytic command-channel
prognostics layer (ADAPT) that forecasts when a control gesture will become unreliable and
proactively migrates it — *before* the user loses the function.**

- **SIGNAL** — Arduino firmware (frozen, real-time). One EMG site → burst classifier
  (SHORT / LONG / DOUBLE) → servo switch-press actuator, 1 kHz loop.
  `signal_firmware/SIGNAL_ADAPT.ino`.
- **ADAPT** — host-side Python. Tracks per-command reliability over sessions, fits a Cox
  proportional-hazards survival model on five time-varying covariates, forecasts when a command
  class will fail, and migrates it to a compound pattern of healthier commands (or escalates to a
  caregiver when the channel itself is at its limit). `adapt/`.

Ground-truth specs live in the repo-root PDFs
(`ADAPT_command_channel_prognostics_patent_disclosure.pdf`,
`SIGNAL_ADAPT_hardware_map_and_firmware.pdf`). If code conflicts with them, the specs win.

> The PREHEND servo-claw firmware that previously headed this repo is preserved at
> [`docs/PREHEND.md`](docs/PREHEND.md) (and `PREHEND/PREHEND.ino`). This README documents the
> SIGNAL+ADAPT system the PDFs describe.

> **Not a medical device. Not clinically validated.** ADAPT is validated against *real* recorded
> multi-session sEMG datasets as the closest honest **proxy** for disease-driven decline — see the
> honesty notes below and [`docs/real_data_manifest.md`](docs/real_data_manifest.md).

---

## Architecture (patent §11–12)

```
[ Arduino — SIGNAL firmware, 1 kHz, real-time ]      <-- never blocked by ADAPT
   A0 EMG -> DC-block -> RMS + TKEO -> burst FSM -> servo (D9)
   telemetry out (decimated): t_ms,emgRMS,tkeoEnv,state,lastCmd,falseNegCount
        |  serial
[ Host (Python) — ADAPT, NON-real-time ]
   session_logger  -> 5 covariates per command/session (Accuracy, MeanRMS, DurVar,
                      InterAttemptGap, FalseNeg)
   hazard_model    -> lifelines CoxTimeVaryingFitter -> S(t) per command
   migration_engine-> if S(t) < 0.7: substitute (or escalate)  -> REMAP back to firmware
   report_generator-> plain-language caregiver note (Nebius Token Factory)
        |  REMAP,<LOGICAL>,<PATTERN1>[_<PATTERN2>]
[ Arduino updates its lookup table; guided-practice mode for N sessions ]
```

The Arduino only ever receives a small lookup table. No model fitting, no blocking, nothing
non-deterministic ever runs in the control loop.

## Repo layout

```
signal_firmware/SIGNAL_ADAPT.ino   frozen real-time firmware (transcribed from the PDF spec)
signal_firmware/esp32_bridge.ino   optional read-only telemetry relay
adapt/                             host-side ADAPT layer
  commands.py        vocabulary, priorities, thresholds (single source of truth)
  emg_clean.py       sEMG cleaning: DC-block, 50/60 Hz notch, 20–450 Hz band-pass, RMS, TKEO
  features.py        Hudgins time-domain features (MAV, RMS, IEMG, WL, ZC, SSC)
  datasets.py        loaders for Ninapro DB6 (.mat) and GRABMyo (WFDB)
  session_logger.py  Phase 2 — the 5 covariates per command/session + Cox frame builder
  hazard_model.py    Phase 3 — Cox PH fit, survival S(t), migration trigger
  migration_engine.py§10 policy — substitution priority SHORT>DOUBLE>LONG, escalation
  serial_bridge.py   wire protocol: telemetry parse + REMAP build/parse + pyserial bridge
  report_generator.py Phase 5 — Nebius Token Factory caregiver notification
  classifier_upgrade.py Phase 4 — optional flagged classifier upgrade + DB6 benchmark
  run_analysis.py    end-to-end driver (real data -> covariates -> Cox -> migration policy)
scripts/             dataset download + extract helpers
docs/                specs (PDFs), real_data_manifest.md, phase4_classifier_search.md, results.md
tests/               pytest: serial round-trip, migration priority, censoring/intervals, Cox, EMG
data/                downloaded real datasets (gitignored)
```

## Setup

```bash
pip install -r requirements.txt
```
Python 3.11. Key deps: `lifelines` (Cox PH — the only hazard model), `wfdb` (GRABMyo),
`scipy`/`numpy`/`pandas`, `scikit-learn` (covariate classifier), `pyserial` (hardware),
`openai` (Nebius client).

## Get the real data

See [`docs/real_data_manifest.md`](docs/real_data_manifest.md) for verified sources/licenses.

```bash
# Windows PowerShell helpers (resumable):
powershell -File scripts/download_db6.ps1        # Ninapro DB6 subset -> data/ninapro_db6/
powershell -File scripts/download_grabmyo.ps1    # GRABMyo subset     -> data/grabmyo/1.1.0/
python scripts/extract_db6.py data/ninapro_db6   # unzip DB6 .mat
# Cross-platform alternatives:
#   GRABMyo:  aws s3 sync --no-sign-request s3://physionet-open/grabmyo/1.1.0/ data/grabmyo/1.1.0/
#   DB6:      download DB6_s{N}_{a,b}.zip from https://ninapro.hevs.ch/instructions/DB6.html
```

## Run the pipeline (real data only)

```bash
# Build covariate + Cox tables from real recordings
python -m adapt.session_logger --dataset grabmyo --root data/grabmyo/1.1.0 --subjects 1-4 \
    --out adapt/derived/grabmyo_covariates.csv
python -m adapt.session_logger --dataset db6 --root data/ninapro_db6/extracted --subjects 1 \
    --out adapt/derived/db6_covariates.csv

# Fit the Cox PH model and see the migration policy applied per user
python -m adapt.run_analysis --db6-root data/ninapro_db6/extracted --db6-subjects 1 \
    --grabmyo-root data/grabmyo/1.1.0 --grabmyo-subjects 1-4

# Optional classifier upgrade benchmark on real DB6 (Phase 4)
python -m adapt.classifier_upgrade --root data/ninapro_db6/extracted --subjects 1
```

Real outputs and their caveats are recorded in [`docs/results.md`](docs/results.md).

## Plug in real serial hardware

1. **Flash the firmware.** Open `signal_firmware/SIGNAL_ADAPT.ino` in the Arduino IDE (only the
   bundled `Servo` library is needed), select your Uno-class board, and upload. Wire per
   `SIGNAL_ADAPT_hardware_map_and_firmware.pdf` §2–3 (A0 = EMG via Muscle BioAmp Shield, D9 =
   servo). Use **battery power** during real acquisition; common **star ground**.
2. **Calibrate.** Open the serial monitor at 115200 baud and send `c` (relax, then 3 twitches).
3. **Read telemetry + drive remaps from Python:**

   ```python
   from adapt.serial_bridge import SerialBridge

   bridge = SerialBridge("COM3")          # or "/dev/ttyACM0"
   for row in bridge.read_telemetry():    # parsed dict per decimated line
       print(row["t_ms"], row["emg_rms"], row["state_name"], row["last_cmd_name"])
       # ... log rows; aggregate into sessions for adapt.session_logger ...

   bridge.send_remap("HOME", "SHORT_SHORT")   # -> REMAP,HOME,SHORT_SHORT  (firmware §6.10)
   ```

   The wire protocol is fixed (CLAUDE.md rule 4): telemetry CSV
   `t_ms,emgRMS,tkeoEnv,state,lastCmd,falseNegCount` and host→device
   `REMAP,<LOGICAL>,<PATTERN1>[_<PATTERN2>]`. The `.ino` parser and `adapt/serial_bridge.py`
   are kept in lock-step; `tests/test_serial_protocol.py` pins the round-trip.

4. **Caregiver notifications (optional).** Set `NEBIUS_API_KEY` and call
   `adapt.report_generator.generate_caregiver_report(event)` to phrase a migration/escalation
   event in plain language via Nebius Token Factory. Without a key it returns a deterministic
   offline template (clearly marked). The API key is read from the environment, never hardcoded.

## Tests

```bash
pytest tests/ -v        # 37 tests: serial round-trip, migration priority, censoring/interval
                        # correctness, Cox fit/forecast/cold-start, EMG cleaning
```

## Honesty & limitations (non-negotiable)

- **Proxy, not disease data.** No public EMG-reliability-decline dataset for a progressive
  neuromuscular population exists. ADAPT is validated on real multi-session sEMG (Ninapro DB6,
  GRABMyo) whose inter-session accuracy decay is a documented **proxy**. No clinical validation
  is claimed (patent §16).
- **Few events ⇒ low confidence.** On this proxy, failure events are scarce (events-per-variable
  well below the ≥10 rule of thumb). The Cox coefficients are reported with an explicit
  low-confidence flag and lifelines' separation warnings surfaced — directions are sensible but
  magnitudes are **not** validated effect sizes.
- **Cox PH only** for the hazard model (`lifelines.CoxTimeVaryingFitter`) — no hand-rolled hazard,
  no swapped model. GitHub/HF models are used **only** for the optional classifier upgrade, never
  for the hazard model (CLAUDE.md rules 1, 6).
- **No fabricated numbers.** Every benchmark/accuracy figure in this repo is produced by running
  the code on real data; no paper/README number is reproduced as our own.
- The build goal says "clean the brainwaves" — this project is **myoelectric (sEMG)**, not EEG;
  the implemented cleaning is the correct sEMG chain, noted as such in `adapt/emg_clean.py`.

## Citations

- Ninapro DB6: Palermo et al., *IEEE ICORR* 2017.
- GRABMyo: Pradhan et al., *Scientific Data* 2022 (CC-BY 4.0).
- Cox PH: Cox 1972; time-varying extension Therneau & Grambsch 2000; `lifelines` (Davidson-Pilon 2019).

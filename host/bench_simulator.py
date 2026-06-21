#!/usr/bin/env python3
"""
bench_simulator.py  —  Software-in-the-loop bench simulator for PREHEND FSM
============================================================================

Faithfully re-implements every piece of the PREHEND.ino firmware in Python:
  • Signal processing  (DC-block, RMS EMA, TKEO, FSR low-pass, ECG R-peak)
  • 8-state FSM         (IDLE → ARMED → PREPOS → COMMIT → HOLD → RELEASE / ABORT / LOCKOUT)
  • Actuation math      (pre-position, commit angle, slip boost, rate-limiting, clamping)

Runs at a simulated 1 kHz cadence.  Deterministic synthetic waveforms exercise
specific firmware code-paths and report pass / fail.

Usage
-----
  python host/bench_simulator.py               # run all scenarios
  python host/bench_simulator.py --scenario normal_grasp
  python host/bench_simulator.py --interactive  # step-by-step injection
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np


# ===========================================================================
#  CONSTANTS  (exact copies from PREHEND.ino)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FS_HZ: int = 1000
DT: float = 1.0 / FS_HZ

# Servo geometry
CLAW_OPEN: int = 0
CLAW_MAXPRE: int = 70
CLAW_FULL: int = 160
SERVO_RATE: int = 6

# ADC (assume 14-bit UNO R4)
ADC_MID: int = 8192


class AuxMode(IntEnum):
    EXTENSOR = 0
    ECG = 1
    EEG = 2


class State(IntEnum):
    IDLE = 0
    ARMED = 1
    PREPOS = 2
    COMMIT = 3
    HOLD = 4
    RELEASE = 5
    ABORT = 6
    LOCKOUT = 7


STATE_NAMES: Dict[int, str] = {
    State.IDLE: "IDLE",
    State.ARMED: "ARMED",
    State.PREPOS: "PREPOS",
    State.COMMIT: "COMMIT",
    State.HOLD: "HOLD",
    State.RELEASE: "RELEASE",
    State.ABORT: "ABORT",
    State.LOCKOUT: "LOCKOUT",
}


# ===========================================================================
#  PARAMETERS  (mirrors firmware Params struct)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Params:
    emgBase: float = 0.02
    mvc: float = 1.0
    tkOnset: float = 8000.0
    rmsCommit: float = 0.35
    extOpen: float = 0.30
    slipTh: float = 8.0
    fsrFloor: float = 200.0
    kSlip: float = 0.5
    tAbortMs: int = 400
    tHoldMs: int = 150
    hrLo: int = 45
    hrHi: int = 140


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SNAPSHOT — recorded at every tick for analysis
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Snapshot:
    t_ms: int
    state: State
    curAngle: int
    tgtAngle: int
    haptic: int
    emgRMS: float
    tkeoEnv: float
    fsrLP: float
    dFdt: float
    bpm: float
    extRMS: float
    conf: float


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PREHEND FSM  (faithful translation)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PrehendFSM:
    """Complete software mirror of the PREHEND firmware control loop."""

    def __init__(self, params: Optional[Params] = None, aux_mode: AuxMode = AuxMode.EXTENSOR):
        self.P = params or Params()
        self.aux_mode = aux_mode

        # ---- servo ----
        self.curAngle: int = CLAW_OPEN
        self.tgtAngle: int = CLAW_OPEN

        # ---- signal state (exact names from firmware) ----
        self.emgDC: float = float(ADC_MID)
        self.emgMS: float = 0.0
        self.emgRMS: float = 0.0
        self.tb0: int = 0
        self.tb1: int = 0
        self.tb2: int = 0
        self.tkeoEnv: float = 0.0

        self.extDC: float = float(ADC_MID)
        self.extMS: float = 0.0
        self.extRMS: float = 0.0

        self.fsr: float = 0.0
        self.fsrLP: float = 0.0
        self.fsrPrev: float = 0.0
        self.dFdt: float = 0.0

        self.ecgHP: float = 0.0
        self.ecgPrev: float = 0.0
        self.lastBeatMs: int = 0
        self.bpm: float = 0.0
        self.rr: float = 0.0
        self.rrAvg: float = 800.0
        self.exertionOK: bool = True
        self._ecg_thr: float = 1500.0
        self._ecg_refr: bool = False
        self._ecg_tr: int = 0

        # ---- FSM ----
        self.state: State = State.IDLE
        self.tStateMs: int = 0
        self.tPreMs: int = 0
        self.tCommitMs: int = 0
        self.conf: float = 0.0

        # ---- simulation clock ----
        self._tick: int = 0  # current time in ms

        # ---- recording ----
        self.history: List[Snapshot] = []
        self.transitions: List[Tuple[int, State, State]] = []  # (t_ms, from, to)

    # ------------------------------------------------------------------ #
    #  Signal processing  (mirrors firmware helpers exactly)
    # ------------------------------------------------------------------ #

    def update_emg(self, raw: int) -> float:
        self.emgDC += 0.0008 * (raw - self.emgDC)
        x = raw - self.emgDC
        self.emgMS += 0.01 * (x * x - self.emgMS)
        self.emgRMS = math.sqrt(max(0.0, self.emgMS))
        self.tb2 = self.tb1
        self.tb1 = self.tb0
        self.tb0 = int(x)
        psi = float(self.tb1) * self.tb1 - float(self.tb0) * self.tb2
        if psi < 0:
            psi = 0.0
        self.tkeoEnv += 0.03 * (psi - self.tkeoEnv)
        return self.emgRMS

    def update_ext(self, raw: int) -> float:
        self.extDC += 0.0008 * (raw - self.extDC)
        x = raw - self.extDC
        self.extMS += 0.01 * (x * x - self.extMS)
        self.extRMS = math.sqrt(max(0.0, self.extMS))
        return self.extRMS

    def update_fsr(self, raw: int) -> None:
        self.fsr = float(raw)
        self.fsrLP += 0.15 * (self.fsr - self.fsrLP)
        self.dFdt = (self.fsrLP - self.fsrPrev) / DT
        self.fsrPrev = self.fsrLP

    def update_ecg(self, raw: int) -> None:
        hp = raw - self.ecgPrev + 0.97 * self.ecgHP
        self.ecgHP = hp
        self.ecgPrev = float(raw)
        now = self._tick
        if not self._ecg_refr and hp > self._ecg_thr:
            if self.lastBeatMs:
                self.rr = float(now - self.lastBeatMs)
                self.rrAvg += 0.2 * (self.rr - self.rrAvg)
                self.bpm = 60000.0 / self.rrAvg if self.rrAvg > 0 else 0.0
            self.lastBeatMs = now
            self._ecg_refr = True
            self._ecg_tr = now
        if self._ecg_refr and (now - self._ecg_tr) > 250:
            self._ecg_refr = False
        self.exertionOK = (self.bpm == 0) or (self.P.hrLo <= self.bpm <= self.P.hrHi)

    # ------------------------------------------------------------------ #
    #  Normalised feature helpers
    # ------------------------------------------------------------------ #

    def emg_norm(self) -> float:
        return (self.emgRMS - self.P.emgBase) / (self.P.mvc - self.P.emgBase + 1e-6)

    def onset_fired(self) -> bool:
        return self.tkeoEnv > self.P.tkOnset and self.emg_norm() > 0.05

    def rms_confirm(self) -> bool:
        return self.emg_norm() > self.P.rmsCommit

    def open_intent(self) -> bool:
        if self.aux_mode == AuxMode.EXTENSOR:
            return self.extRMS > self.P.extOpen
        return self.emg_norm() < 0.05

    # ------------------------------------------------------------------ #
    #  FSM step  (faithful translation of fsmStep())
    # ------------------------------------------------------------------ #

    def _fsm_step(self) -> None:
        now = self._tick
        prev_state = self.state

        # Global safety: ECG exertion gate → LOCKOUT
        if self.aux_mode == AuxMode.ECG and not self.exertionOK and self.state != State.LOCKOUT:
            self.state = State.LOCKOUT
            self.tStateMs = now
            self.tgtAngle = CLAW_OPEN
            if self.state != prev_state:
                self.transitions.append((now, prev_state, self.state))
            return

        if self.state == State.IDLE:
            self.tgtAngle = CLAW_OPEN
            if self.aux_mode != AuxMode.ECG or self.exertionOK:
                self.state = State.ARMED
                self.tStateMs = now

        elif self.state == State.ARMED:
            self.tgtAngle = CLAW_OPEN
            if self.onset_fired():
                self.conf = max(0.1, min(1.0, self.tkeoEnv / (self.P.tkOnset * 3.0)))
                self.tPreMs = now
                self.state = State.PREPOS
                self.tStateMs = now

        elif self.state == State.PREPOS:
            self.tgtAngle = int(self.conf * CLAW_MAXPRE)
            if self.rms_confirm():
                self.tCommitMs = now
                self.state = State.COMMIT
                self.tStateMs = now
            elif (now - self.tPreMs) > self.P.tAbortMs:
                self.state = State.ABORT
                self.tStateMs = now

        elif self.state == State.COMMIT:
            grip = max(0.0, min(1.0, self.emg_norm()))
            self.tgtAngle = int(CLAW_MAXPRE + grip * (CLAW_FULL - CLAW_MAXPRE))
            if (now - self.tCommitMs) > self.P.tHoldMs:
                self.state = State.HOLD
                self.tStateMs = now

        elif self.state == State.HOLD:
            # Slip reflex
            if self.dFdt < -self.P.slipTh or (self.fsrLP < self.P.fsrFloor and self.fsrLP > 1):
                boost = int(max(5.0, min(30.0, -self.dFdt * self.P.kSlip)))
                self.tgtAngle = min(self.curAngle + boost, CLAW_FULL)
            # Voluntary open
            if self.open_intent():
                self.state = State.RELEASE
                self.tStateMs = now

        elif self.state == State.RELEASE:
            self.tgtAngle = CLAW_OPEN
            if self.curAngle <= CLAW_OPEN + 2:
                self.state = State.IDLE
                self.tStateMs = now

        elif self.state == State.ABORT:
            self.tgtAngle = CLAW_OPEN
            if self.curAngle <= CLAW_OPEN + 2:
                self.state = State.IDLE
                self.tStateMs = now

        elif self.state == State.LOCKOUT:
            self.tgtAngle = CLAW_OPEN
            if (now - self.tStateMs) > 2000 and (self.aux_mode != AuxMode.ECG or self.exertionOK):
                self.state = State.IDLE
                self.tStateMs = now

        if self.state != prev_state:
            self.transitions.append((now, prev_state, self.state))

    # ------------------------------------------------------------------ #
    #  Outputs  (rate-limited servo + haptic buzz)
    # ------------------------------------------------------------------ #

    def _drive_outputs(self) -> int:
        """Returns haptic intensity (0-255)."""
        if self.tgtAngle > self.curAngle:
            self.curAngle = min(self.curAngle + SERVO_RATE, self.tgtAngle)
        elif self.tgtAngle < self.curAngle:
            self.curAngle = max(self.curAngle - SERVO_RATE, self.tgtAngle)
        self.curAngle = max(CLAW_OPEN, min(CLAW_FULL, self.curAngle))

        buzz = 0
        if self.state in (State.COMMIT, State.HOLD):
            # firmware:  map(fsrLP, fsrFloor, 12000, 40, 255)
            mapped = 40 + (self.fsrLP - self.P.fsrFloor) * (255 - 40) / (12000 - self.P.fsrFloor)
            buzz = int(max(0, min(255, mapped)))
        return buzz

    # ------------------------------------------------------------------ #
    #  Single tick (the public API)
    # ------------------------------------------------------------------ #

    def tick(self, raw_emg: int, raw_aux: int, raw_fsr: int, record: bool = True) -> Snapshot:
        """Advance the simulation by one 1 ms sample."""
        # Signal processing
        self.update_emg(raw_emg)
        if self.aux_mode == AuxMode.EXTENSOR:
            self.update_ext(raw_aux)
        elif self.aux_mode == AuxMode.ECG:
            self.update_ecg(raw_aux)
        self.update_fsr(raw_fsr)

        # FSM
        self._fsm_step()

        # Outputs
        haptic = self._drive_outputs()

        snap = Snapshot(
            t_ms=self._tick,
            state=self.state,
            curAngle=self.curAngle,
            tgtAngle=self.tgtAngle,
            haptic=haptic,
            emgRMS=self.emgRMS,
            tkeoEnv=self.tkeoEnv,
            fsrLP=self.fsrLP,
            dFdt=self.dFdt,
            bpm=self.bpm,
            extRMS=self.extRMS,
            conf=self.conf,
        )
        if record:
            self.history.append(snap)

        self._tick += 1
        return snap

    def reset(self) -> None:
        """Reset FSM and signal state to power-on defaults."""
        self.__init__(params=self.P, aux_mode=self.aux_mode)  # type: ignore[misc]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SYNTHETIC SIGNAL GENERATORS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _emg_burst(n: int, amp: float, *, noise_std: float = 50.0,
               rng: np.random.Generator | None = None) -> np.ndarray:
    """Simulate n samples of an EMG burst centred on ADC_MID."""
    if rng is None:
        rng = np.random.default_rng(42)
    signal = rng.normal(0, noise_std, n) * amp + ADC_MID
    return signal.astype(np.float64)


def _quiet(n: int, *, noise_std: float = 5.0,
           rng: np.random.Generator | None = None) -> np.ndarray:
    """Quiet EMG (just noise around mid-scale)."""
    if rng is None:
        rng = np.random.default_rng(42)
    return (rng.normal(0, noise_std, n) + ADC_MID).astype(np.float64)


SyntheticSample = Tuple[int, int, int]  # (raw_emg, raw_aux, raw_fsr)


# ── Scenario 1: Normal grasp ─────────────────────────────────────────────

def scenario_normal_grasp() -> Tuple[List[SyntheticSample], List[State], Params, AuxMode]:
    """
    EMG onset ramp → sustained contraction → extensor release.
    Expected: IDLE → ARMED → PREPOS → COMMIT → HOLD → RELEASE → IDLE
    """
    rng = np.random.default_rng(1)
    P = Params(emgBase=10.0, mvc=300.0, tkOnset=8000.0, rmsCommit=0.35,
               extOpen=50.0, tAbortMs=400, tHoldMs=150)
    samples: List[SyntheticSample] = []

    # Phase 1: 200 ms quiet  (settle filters)
    for _ in range(200):
        samples.append((int(rng.normal(ADC_MID, 5)), int(rng.normal(ADC_MID, 3)), 0))

    # Phase 2: 50 ms sharp EMG onset (high-amplitude, triggers TKEO)
    for _ in range(50):
        samples.append((int(rng.normal(ADC_MID, 200)), int(rng.normal(ADC_MID, 3)), 0))

    # Phase 3: 500 ms sustained strong EMG (confirm + commit + hold)
    for _ in range(500):
        samples.append((int(rng.normal(ADC_MID, 350)), int(rng.normal(ADC_MID, 3)), 600))

    # Phase 4: 50 ms extensor fires (open intent) while EMG drops
    for _ in range(50):
        samples.append((int(rng.normal(ADC_MID, 10)), int(rng.normal(ADC_MID, 200)), 400))

    # Phase 5: 500 ms quiet (release completes, servo returns to 0)
    for _ in range(500):
        samples.append((int(rng.normal(ADC_MID, 5)), int(rng.normal(ADC_MID, 3)), 0))

    expected = [State.IDLE, State.ARMED, State.PREPOS, State.COMMIT,
                State.HOLD, State.RELEASE, State.IDLE]
    return samples, expected, P, AuxMode.EXTENSOR


# ── Scenario 2: Abort (onset but no RMS confirm) ─────────────────────────

def scenario_abort() -> Tuple[List[SyntheticSample], List[State], Params, AuxMode]:
    """
    Brief EMG twitch fires TKEO but RMS never reaches rmsCommit.
    Expected: IDLE → ARMED → PREPOS → ABORT → IDLE
    """
    rng = np.random.default_rng(2)
    P = Params(emgBase=10.0, mvc=300.0, tkOnset=8000.0, rmsCommit=0.35,
               extOpen=50.0, tAbortMs=400, tHoldMs=150)
    samples: List[SyntheticSample] = []

    # Quiet settle
    for _ in range(200):
        samples.append((int(rng.normal(ADC_MID, 5)), int(rng.normal(ADC_MID, 3)), 0))

    # Very brief strong burst (10 ms) — enough for TKEO but too short for RMS
    for _ in range(10):
        samples.append((int(rng.normal(ADC_MID, 300)), int(rng.normal(ADC_MID, 3)), 0))

    # Back to quiet for longer than tAbortMs
    for _ in range(800):
        samples.append((int(rng.normal(ADC_MID, 5)), int(rng.normal(ADC_MID, 3)), 0))

    expected = [State.IDLE, State.ARMED, State.PREPOS, State.ABORT, State.IDLE]
    return samples, expected, P, AuxMode.EXTENSOR


# ── Scenario 3: Slip reflex ─────────────────────────────────────────────

def scenario_slip_reflex() -> Tuple[List[SyntheticSample], List[State], Params, AuxMode]:
    """
    Normal grasp into HOLD, then FSR drops sharply → slip boost.
    Expected states: IDLE → ARMED → PREPOS → COMMIT → HOLD
    In HOLD, tgtAngle should increase (slip boost).
    """
    rng = np.random.default_rng(3)
    P = Params(emgBase=10.0, mvc=300.0, tkOnset=8000.0, rmsCommit=0.35,
               extOpen=50.0, slipTh=8.0, kSlip=0.5, fsrFloor=200.0,
               tAbortMs=400, tHoldMs=150)
    samples: List[SyntheticSample] = []

    # Quiet settle
    for _ in range(200):
        samples.append((int(rng.normal(ADC_MID, 5)), int(rng.normal(ADC_MID, 3)), 0))

    # EMG onset
    for _ in range(50):
        samples.append((int(rng.normal(ADC_MID, 200)), int(rng.normal(ADC_MID, 3)), 0))

    # Sustained EMG + FSR contact (object grasped)
    for _ in range(500):
        samples.append((int(rng.normal(ADC_MID, 350)), int(rng.normal(ADC_MID, 3)), 800))

    # In HOLD: FSR drops sharply (slip event) over 50 ms
    for i in range(50):
        fsr_val = int(800 - i * 15)  # drops from 800 to 50
        samples.append((int(rng.normal(ADC_MID, 350)), int(rng.normal(ADC_MID, 3)), max(fsr_val, 10)))

    # Continue in HOLD with moderate FSR
    for _ in range(400):
        samples.append((int(rng.normal(ADC_MID, 350)), int(rng.normal(ADC_MID, 3)), 500))

    expected = [State.IDLE, State.ARMED, State.PREPOS, State.COMMIT, State.HOLD]
    return samples, expected, P, AuxMode.EXTENSOR


# ── Scenario 4: Extensor release ─────────────────────────────────────────

def scenario_extensor_release() -> Tuple[List[SyntheticSample], List[State], Params, AuxMode]:
    """
    Normal grasp into HOLD, then extensor EMG fires → RELEASE → IDLE.
    Expected: IDLE → ARMED → PREPOS → COMMIT → HOLD → RELEASE → IDLE
    """
    rng = np.random.default_rng(4)
    P = Params(emgBase=10.0, mvc=300.0, tkOnset=8000.0, rmsCommit=0.35,
               extOpen=50.0, tAbortMs=400, tHoldMs=150)
    samples: List[SyntheticSample] = []

    # Quiet settle
    for _ in range(200):
        samples.append((int(rng.normal(ADC_MID, 5)), int(rng.normal(ADC_MID, 3)), 0))

    # EMG onset
    for _ in range(50):
        samples.append((int(rng.normal(ADC_MID, 200)), int(rng.normal(ADC_MID, 3)), 0))

    # Sustained EMG + FSR
    for _ in range(500):
        samples.append((int(rng.normal(ADC_MID, 350)), int(rng.normal(ADC_MID, 3)), 600))

    # Extensor fires (AUX channel goes high-amplitude) while flexor drops
    for _ in range(100):
        samples.append((int(rng.normal(ADC_MID, 10)), int(rng.normal(ADC_MID, 200)), 400))

    # Quiet — release + return to IDLE
    for _ in range(500):
        samples.append((int(rng.normal(ADC_MID, 5)), int(rng.normal(ADC_MID, 3)), 0))

    expected = [State.IDLE, State.ARMED, State.PREPOS, State.COMMIT,
                State.HOLD, State.RELEASE, State.IDLE]
    return samples, expected, P, AuxMode.EXTENSOR


# ── Scenario 5: ECG lockout ──────────────────────────────────────────────

def scenario_ecg_lockout() -> Tuple[List[SyntheticSample], List[State], Params, AuxMode]:
    """
    ECG mode — heart rate spikes above hrHi, forcing LOCKOUT from any state.
    Expected: IDLE -> ARMED -> LOCKOUT -> IDLE
    """
    rng = np.random.default_rng(5)
    P = Params(emgBase=10.0, mvc=300.0, tkOnset=8000.0, rmsCommit=0.35,
               tAbortMs=400, tHoldMs=150, hrLo=45, hrHi=140)
    samples: List[SyntheticSample] = []

    # Define exact beat times (in ms)
    # Starts normal (800ms), goes fast (300ms) to trigger lockout, then recovers to normal (800ms)
    beat_times = [0, 800, 1100, 1400, 1700, 2000, 2300, 2600, 2900, 3200, 3500, 4300, 5100, 5900, 6700]
    beat_set = set(beat_times)

    total_duration = 7000
    for t in range(total_duration):
        emg = int(rng.normal(ADC_MID, 5))
        fsr = 0
        
        # Check if t is within a 3ms window of any beat time
        is_beat = False
        for b in beat_times:
            if 0 <= t - b < 3:
                is_beat = True
                break
                
        if is_beat:
            ecg = int(ADC_MID + 2500)  # large R-peak
        else:
            ecg = int(rng.normal(ADC_MID, 30))
            
        samples.append((emg, ecg, fsr))

    expected = [State.IDLE, State.ARMED, State.LOCKOUT, State.IDLE]
    return samples, expected, P, AuxMode.ECG


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SIMULATION RUNNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_scenario(
    name: str,
    samples: List[SyntheticSample],
    expected_states: List[State],
    params: Params,
    aux_mode: AuxMode,
    *,
    verbose: bool = True,
) -> bool:
    """Run a complete scenario and return True if it passes."""
    fsm = PrehendFSM(params=params, aux_mode=aux_mode)

    for raw_emg, raw_aux, raw_fsr in samples:
        fsm.tick(raw_emg, raw_aux, raw_fsr)

    # Build the observed state sequence from transitions
    observed: List[State] = [State.IDLE]  # always starts at IDLE
    for _, _, to_state in fsm.transitions:
        observed.append(to_state)

    # For slip_reflex: check that slip boost occurred
    slip_detected = False
    if name == "slip_reflex":
        # Look for tgtAngle increases during HOLD
        in_hold = False
        max_angle_in_hold = 0
        for snap in fsm.history:
            if snap.state == State.HOLD:
                in_hold = True
                max_angle_in_hold = max(max_angle_in_hold, snap.tgtAngle)
        if in_hold:
            # Find the angle when entering HOLD
            enter_angle = 0
            for snap in fsm.history:
                if snap.state == State.HOLD:
                    enter_angle = snap.curAngle
                    break
            if max_angle_in_hold > enter_angle:
                slip_detected = True

    # Check pass/fail
    # For expected_states, check that the observed sequence contains the
    # expected states in order (they may have duplicates between them)
    def subsequence_match(expected: List[State], observed: List[State]) -> bool:
        """Check if expected is a subsequence of observed."""
        it = iter(observed)
        return all(s in it for s in expected)

    passed = subsequence_match(expected_states, observed)

    # For slip_reflex, also require that slip was detected
    if name == "slip_reflex":
        passed = passed and slip_detected

    # ── Print results ──
    if verbose:
        status = "PASS" if passed else "FAIL"
        print(f"\n{'=' * 72}")
        print(f"  Scenario: {name}   {status}")
        print(f"{'=' * 72}")

        # Transition timeline
        print("  State transitions:")
        print(f"    t=0ms: {STATE_NAMES[State.IDLE]}", end="")
        for t_ms, from_st, to_st in fsm.transitions:
            print(f" -> t={t_ms}ms: {STATE_NAMES[to_st]}", end="")
        print()

        # Expected vs observed
        print(f"  Expected : {' -> '.join(STATE_NAMES[s] for s in expected_states)}")
        print(f"  Observed : {' -> '.join(STATE_NAMES[s] for s in observed)}")

        if name == "slip_reflex":
            print(f"  Slip boost detected: {'Yes' if slip_detected else 'No'}")

        # Key signals at transitions
        if fsm.transitions:
            print("  Key signals at transitions:")
            trans_times = {t for t, _, _ in fsm.transitions}
            for snap in fsm.history:
                if snap.t_ms in trans_times:
                    print(f"    t={snap.t_ms:5d}ms  emgRMS={snap.emgRMS:8.2f}  "
                          f"tkeo={snap.tkeoEnv:10.1f}  fsrLP={snap.fsrLP:7.1f}  "
                          f"dFdt={snap.dFdt:8.1f}  bpm={snap.bpm:5.1f}  "
                          f"angle={snap.curAngle:3d} deg  conf={snap.conf:.3f}")

        print(f"{'-' * 72}")

    return passed


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INTERACTIVE MODE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_interactive() -> None:
    """Step-by-step mode: user injects (emg, aux, fsr) at each tick."""
    print("PREHEND Interactive Bench Simulator")
    print("Enter samples as: emg,aux,fsr   (or 'q' to quit, Enter for ADC_MID,ADC_MID,0)")
    print(f"ADC_MID = {ADC_MID}")

    fsm = PrehendFSM()
    while True:
        try:
            line = input(f"[t={fsm._tick:5d}ms  state={STATE_NAMES[fsm.state]:8s}  "
                         f"angle={fsm.curAngle:3d}°] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if line.lower() == 'q':
            break
        if not line:
            raw_emg, raw_aux, raw_fsr = ADC_MID, ADC_MID, 0
        else:
            try:
                parts = line.split(',')
                raw_emg = int(parts[0])
                raw_aux = int(parts[1]) if len(parts) > 1 else ADC_MID
                raw_fsr = int(parts[2]) if len(parts) > 2 else 0
            except ValueError:
                print("  Invalid input. Use: emg,aux,fsr")
                continue

        snap = fsm.tick(raw_emg, raw_aux, raw_fsr)
        # Print transition if it happened
        if fsm.transitions and fsm.transitions[-1][0] == snap.t_ms:
            _, from_st, to_st = fsm.transitions[-1]
            print(f"  > TRANSITION: {STATE_NAMES[from_st]} -> {STATE_NAMES[to_st]}")
        print(f"  emgRMS={snap.emgRMS:.2f}  tkeo={snap.tkeoEnv:.1f}  "
              f"fsr={snap.fsrLP:.1f}  dFdt={snap.dFdt:.1f}  "
              f"tgt={snap.tgtAngle}°  cur={snap.curAngle}°  haptic={snap.haptic}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  SCENARIO REGISTRY
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SCENARIOS: Dict[str, Callable[[], Tuple[List[SyntheticSample], List[State], Params, AuxMode]]] = {
    "normal_grasp": scenario_normal_grasp,
    "abort": scenario_abort,
    "slip_reflex": scenario_slip_reflex,
    "extensor_release": scenario_extensor_release,
    "ecg_lockout": scenario_ecg_lockout,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CLI ENTRY POINT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main() -> int:
    parser = argparse.ArgumentParser(
        description="PREHEND bench simulator — run FSM scenarios without hardware",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
scenarios:
  normal_grasp        Full grasp cycle (IDLE → ARMED → PREPOS → COMMIT → HOLD → RELEASE → IDLE)
  abort               EMG twitch without confirmation (→ ABORT → IDLE)
  slip_reflex         Object slip detection and grip boost during HOLD
  extensor_release    Voluntary release via extensor EMG
  ecg_lockout         Heart-rate lockout via ECG exertion gate
""",
    )
    parser.add_argument("--scenario", "-s", choices=list(SCENARIOS.keys()),
                        help="Run a single scenario (default: all)")
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="Interactive step-by-step mode")
    args = parser.parse_args()

    if args.interactive:
        run_interactive()
        return 0

    print("\n  " + "=" * 58)
    print("    PREHEND Bench Simulator -- FSM Verification Suite")
    print("  " + "=" * 58)

    scenarios_to_run = [args.scenario] if args.scenario else list(SCENARIOS.keys())
    results: Dict[str, bool] = {}

    for name in scenarios_to_run:
        samples, expected, params, aux_mode = SCENARIOS[name]()
        results[name] = run_scenario(name, samples, expected, params, aux_mode)

    # Summary
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    failed = total - passed

    print(f"\n{'=' * 72}")
    print(f"  SUMMARY: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)", end="")
    print()
    for name, ok in results.items():
        icon = "[PASS]" if ok else "[FAIL]"
        print(f"    {icon} {name}")
    print(f"{'=' * 72}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

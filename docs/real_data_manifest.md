# ADAPT — Real Data Manifest

> **Honest-proxy statement (CLAUDE.md rule 2).** No public dataset of EMG-reliability
> decline in a progressive neuromuscular-disease population exists. ADAPT is validated
> against the closest **real recorded multi-session sEMG** datasets, which exhibit
> *measured inter-session* reliability decay (electrode shift / non-stationarity).
> This is a proxy for disease-driven decline, **not disease data**. No synthetic EMG is
> used anywhere in this project, and no clinical validation is claimed.

Every fact below was verified by fetching the source page / endpoint during the build
(not recalled from memory). Verification dates: **2026-06-21**.

---

## 1. Ninapro DB6 — *primary* dataset

| Field | Verified value |
|---|---|
| Source page | https://ninapro.hevs.ch/instructions/DB6.html |
| Reference (cite when using) | Palermo et al., *IEEE ICORR* 2017 |
| Purpose | Repeatability study of sEMG hand-grasp recognition (built specifically to study **inter-session** reliability) |
| Subjects | 10 intact subjects |
| Sessions | 5 days × 2 sessions/day (morning + afternoon) = **10 sessions** |
| Movements | 7 grasps on 14 objects, 12 repetitions each |
| Sampling rate | **2 kHz** |
| Electrodes | 14 Delsys Trigno double-differential sEMG (8 at radio-humeral joint height, 6 below) |
| Download URL pattern | `https://ninapro.hevs.ch/files/DB6_Preproc/DB6_s{N}_{a,b}.zip` |
| Verified file | `DB6_s1_a.zip` → HTTP 200, **1,279,928,226 bytes**, `application/zip` |
| Access | Retrievable via **direct HTTPS without login/registration** at download time (verified by HEAD + actual download) |
| License | The DB6 page does **not** display an explicit SPDX license; the project states the data are publicly available via Ninaweb and requests citation of Palermo et al. 2017. (Stated exactly as observed — not overclaimed.) |
| File format | MATLAB `.mat`, one file per session named `S{subj}_D{day}_T{trial}.mat` |
| `.mat` variables (verified on `S1_D1_T1.mat`) | `emg` (N×16 float32; 14 Trigno EMG + 2 aux), `restimulus`/`stimulus`, `rerepetition`/`repetition`, `acc`, `object`, `subj`, `time` |
| Movement labels present (verified) | `{0(rest),1,3,4,6,9,10,11}` — note **label 5 does not exist** |

**Downloaded subset** (see `scripts/download_db6.ps1`): subjects **1, 2, 3 — fully downloaded**,
both halves each (verified: 6 `.zip`, 30 `.mat` total after `scripts/extract_db6.py`). Per subject:
`_a` = days 1–3 (`S?_D1_T1 … S?_D3_T2`, 6 sessions), `_b` = days 4–5 (4 sessions) → **10 sessions ×
3 subjects**. The full DB6 is ~25 GB (10 subjects); this documented 3-subject subset is enough for a
real multi-session Cox fit and the DB6 classifier benchmark. To fetch all subjects, extend the
`$files` list in the script to subjects 1–10.

---

## 2. GRABMyo v1.1.0 — *secondary* multi-day proxy

| Field | Verified value |
|---|---|
| Source page | https://physionet.org/content/grabmyo/1.1.0/ |
| Reference (cite when using) | Pradhan et al., *Scientific Data* 2022 (s41597-022-01836-y) |
| Subjects | 43 participants (23M / 20F, age 24–35) |
| Sessions | 3 days with long separation: **Day 1, Day 8, Day 29** |
| Gestures | 16 gestures + rest = 17 classes, 7 trials each |
| Sampling rate | **2048 Hz** |
| Channels (verified on a record) | 32 signals: 16 forearm `F1–F16`, 12 wrist `W1–W12`, 4 reference `U1–U4`; units mV; 5.0 s / trial (10 240 samples) |
| License | **Creative Commons Attribution 4.0 International (CC-BY 4.0)** — verified in `LICENSE.txt` and on the page |
| Total size | 9.4 GB uncompressed (9.1 GB zip) |
| Access | **Openly accessible, no registration.** Public S3 mirror `s3://physionet-open/grabmyo/1.1.0/` |
| File format | WFDB `.dat`/`.hea`; layout `Session{1,2,3}/session{i}_participant{j}/session{i}_participant{j}_gesture{k}_trial{t}.{dat,hea}` |

**Downloaded subset** (see `scripts/download_grabmyo.ps1`): participants **1–4**, all 3
sessions, gestures **{1, 6, 11, 17}** (17 = rest), 7 trials — verified complete (28 `.dat`
per participant/session). To fetch the full dataset:
`aws s3 sync --no-sign-request s3://physionet-open/grabmyo/1.1.0/ <dest>` or
`wget -r -N -c -np https://physionet.org/files/grabmyo/1.1.0/`.

---

## 3. Command framing (SHORT / LONG / DOUBLE)

SIGNAL's vocabulary is three burst patterns (SHORT, LONG, DOUBLE). These benchmark
datasets contain hand **gestures/grasps**, not burst patterns, so a fixed subset of
gesture classes is **relabeled** as the three command classes (a documented framing —
the ADAPT claim tracks per-command-**class** reliability and is agnostic to which physical
gesture realises each class; it does **not** claim gesture *k* "is" a SHORT burst):

| Command | GRABMyo gesture | Ninapro DB6 movement label |
|---|---|---|
| SHORT  | 1  | 1 |
| LONG   | 6  | 3 |
| DOUBLE | 11 | 6 |
| (REST) | 17 | 0 |

A **single EMG channel** is used throughout (best forearm site selected per user on the
baseline session), to mirror SIGNAL's single-electrode hardware.

---

## 4. Covariate operationalisation — what is directly measured vs. proxied

The 5 covariates (patent Section 7.1) are computed from the **real** recordings. Because
these are fixed-protocol lab datasets (not a live SIGNAL deployment), some covariates are
operationalised via documented signal-derived proxies — stated here so nothing is implied
to be measured more directly than it is:

| Covariate | How computed on this data | Strength on benchmark data |
|---|---|---|
| `accuracy` | Per-session accuracy of a single-channel LDA trained on the user's **baseline** session, tested per session (reflects real inter-session signal change) | **Strong** |
| `mean_rms` | Mean RMS envelope amplitude of the active burst across class-c trials | **Strong** |
| `false_neg_count` | # class-c trials rejected as CMD_NONE (max posterior < baseline-calibrated threshold) | **Strong** |
| `dur_var` | CoV of strong-contraction hold time (≥50 % of envelope peak) across trials | Medium (trial windows are protocol-fixed) |
| `inter_attempt_gap` | **Proxy:** median within-trial time-to-peak. True wall-clock inter-attempt gaps are not recorded in these datasets; a live SIGNAL deployment would measure them directly. | Weak/proxy (documented) |

The Cox model drops any covariate with ~zero variance before fitting and reports which
were dropped — it never silently relies on a degenerate column.

---

## 5. Reproduce

```bash
pip install -r requirements.txt
# download (Windows PowerShell scripts; or use wget/aws as above)
powershell -File scripts/download_db6.ps1
powershell -File scripts/download_grabmyo.ps1
# build the real covariate + Cox tables
python -m adapt.session_logger --dataset grabmyo --root data/grabmyo/1.1.0 --subjects 1-4 \
    --out adapt/derived/grabmyo_covariates.csv
python -m adapt.session_logger --dataset db6 --root data/ninapro_db6/extracted --subjects 1-3 \
    --out adapt/derived/db6_covariates.csv
# fit the Cox PH model on the real table
python -m adapt.hazard_model --cox adapt/derived/grabmyo_covariates_cox.csv
```

# Phase 4 — EMG Classifier Search & Verification (GitHub / HuggingFace)

Goal: find an **actual working** EMG gesture-classification model/repo trained on or
compatible with Ninapro — with runnable code or loadable weights (not just a paper) —
to integrate as an *optional, flag-gated* host-side upgrade to SIGNAL's classifier, and
benchmark it on Ninapro DB6. The firmware real-time FSM is never replaced.

This search was run as a multi-agent workflow (`phase4-emg-classifier-search`): parallel
web scouts across GitHub + HuggingFace + papers-with-code → per-candidate adversarial
verification (fetching the live page, file tree, and source) → ranked synthesis.
**17 unique candidates** were verified. Findings below are from reading real pages, not memory.

## Headline (honest)

**No candidate is a one-command, weights-included DB6 gesture classifier.** Every genuinely
runnable, permissively-licensed option requires training from scratch. Loadable-weight
models either target a different task/montage or forbid redistributing derivatives.

## Verified candidates (selected)

| Candidate | Runnable | Weights for DB6 | DB6-usable | License | Note |
|---|---|---|---|---|---|
| [WaveFormer](https://github.com/ForeverBlue816/WaveFormer) | ✅ PyTorch | ❌ train from scratch | ✅ ships DB6 preprocessor | **MIT** | Best DB6-specific fit; README claims 81.93% inter-session (unverified — no weights ship) |
| [ocjorge/CNN-LSTM](https://github.com/ocjorge/CNN-LSTM) | ✅ Keras | ❌ | ✅ after edits | MIT | Light backup; built for DB1, needs window/label edits |
| [PulpBio/TinyMyo](https://huggingface.co/PulpBio/TinyMyo) | ✅ PyTorch | ⚠️ loadable, but DB6 = *pretraining* only (no classifier head) | partial | **CC BY-ND 4.0** (blocks shipping finetuned weights) | Exact DB6-matching preprocessor; strong but ND license is a blocker for an integrated upgrade |
| [sEMG-based-mvcnn](https://github.com/computer-animation-perception-group/sEMG-based-mvcnn) | ✅ | ❌ | ✅ full native DB6 wiring | GPL-3.0 | Built on **MXNet** (end-of-life, ~impossible to install in 2026); abandoned 2019 |
| [nina_funcs](https://pypi.org/project/nina-funcs/) | ✅ lib | ❌ | after edits | MIT | Preprocessing/feature library only; no model |
| NinaTools / NinaPro-Helper-Library / khushi2062 / tsagkas | ✅ | ❌ | mostly no | **None (all-rights-reserved)** | Hard-blocked for reuse/redistribution |
| [NeuroRVQ](https://huggingface.co/eugenehp/NeuroRVQ) | ✅ | ⚠️ tokenizer/foundation only, no head | ❌ | Apache-2.0 | Not a gesture classifier |
| [braindecode/emg2qwerty-generic](https://huggingface.co/braindecode/emg2qwerty-generic) | ✅ | ✅ | ❌ | CC BY-NC-SA | Wrong task (keystroke typing), 32-ch wrist montage |

## Decision

1. **External SOTA to plug in (documented, recommended): WaveFormer** — MIT, PyTorch, ships a
   correct DB6 preprocessor (16→14 ch dropping ch 8–9, fs=2000, 20–90 Hz band-pass + 50 Hz
   notch, per-channel z-score, 1024/512 windows, rep split train 1–8 / val 9–10 / test 11–12,
   7-class remap `{1,3,4,6,9,10,11}→[0..6]`). It ships **no weights**, so a from-scratch
   training run on a GPU is required; its README's 81.93% is therefore unverified here and is
   **not** reproduced as our own number. Integration notes are in
   `adapt/classifier_upgrade.py` (`WAVEFORMER_NOTES`).

2. **What we actually ship and benchmark (real numbers): `adapt/classifier_upgrade.py`** — a
   runnable, flag-gated host-side upgrade compatible with the same Ninapro/DB6 pipeline. It
   contrasts the SIGNAL-equivalent **baseline** (single best channel + LDA) against an
   **upgrade** (all 14 channels, Hudgins TD features + RandomForest), benchmarked on **real
   DB6** with the standard repetition split (train reps 1–8, test reps 9–12), intra- and
   inter-session. Numbers in this repo are produced by actually running that code on the
   downloaded DB6 data — never copied from a paper or README.

This keeps the deliverable honest: the search is real and documented, the integration is
real and runnable behind a flag, the benchmark numbers are real, and the absence of a
shippable-weights DB6 model is stated plainly rather than papered over.

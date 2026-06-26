import React, { useEffect, useRef, useState } from 'react';
import { useTelemetry, EEG_BANDS, MODE_NAMES } from '../context/TelemetryContext';

const NeuroLab = () => {
  const {
    eegBandsRef, headAccelRef, lastGesture, currentVitals,
    systemMode, setSystemMode, telemetryHistoryRef, isConnected
  } = useTelemetry();

  const barsRef = useRef({});
  const headDotRef = useRef(null);
  const canvasEegRef = useRef(null);
  const alphaSinceRef = useRef(null);
  const lastGestureRef = useRef(null);
  const flashShownRef = useRef(null);
  const [alphaBurst, setAlphaBurst] = useState(false);
  const [flash, setFlash] = useState(null);

  useEffect(() => { lastGestureRef.current = lastGesture; }, [lastGesture]);

  // rAF loop: animate band bars, head-accel dot, EEG waveform (ref-driven, no re-renders)
  useEffect(() => {
    let animId;
    const draw = () => {
      const bands = eegBandsRef.current;

      // band bars
      EEG_BANDS.forEach(b => {
        const el = barsRef.current[b.key];
        if (el) {
          const pct = Math.min(100, (bands[b.key] || 0) * 100);
          el.style.width = `${pct}%`;
          const valEl = el.parentElement.parentElement.querySelector('.eeg-band-val');
          if (valEl) valEl.textContent = `${pct.toFixed(0)}%`;
        }
      });

      // alpha-burst detection (sustained alpha > 30% for 2 s)
      const alphaPct = (bands.alpha || 0);
      const now = Date.now();
      if (alphaPct > 0.30) {
        if (alphaSinceRef.current == null) alphaSinceRef.current = now;
        if (now - alphaSinceRef.current > 2000) setAlphaBurst(true);
      } else {
        alphaSinceRef.current = null;
        setAlphaBurst(false);
      }

      // head-accel dot
      const acc = headAccelRef.current;
      if (headDotRef.current) {
        const cx = 50 + (acc.x / 127) * 38;
        const cy = 50 - (acc.y / 127) * 38;
        headDotRef.current.style.left = `${Math.max(4, Math.min(96, cx))}%`;
        headDotRef.current.style.top = `${Math.max(4, Math.min(96, cy))}%`;
      }

      // EEG waveform
      const canvas = canvasEegRef.current;
      if (canvas) {
        const ctx = canvas.getContext('2d');
        const w = canvas.width = canvas.parentElement.clientWidth;
        const h = canvas.height = canvas.parentElement.clientHeight;
        ctx.clearRect(0, 0, w, h);
        ctx.strokeStyle = 'rgba(99,102,241,0.12)';
        ctx.lineWidth = 1;
        for (let y = 0; y < h; y += 24) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
        const data = telemetryHistoryRef.current.aux;
        const len = data.length;
        ctx.beginPath();
        ctx.strokeStyle = '#6366f1';
        ctx.lineWidth = 1.6;
        for (let i = 0; i < len; i++) {
          const x = (i / (len - 1)) * w;
          const y = h / 2 - (data[i] / 4000) * (h / 2);
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }

      // gesture flash (edge-triggered; setState only on change → no per-frame renders)
      const g = lastGestureRef.current;
      const active = g && (now - g.time < 900) ? g : null;
      if ((active && active.time !== flashShownRef.current) || (!active && flashShownRef.current !== null)) {
        flashShownRef.current = active ? active.time : null;
        setFlash(active);
      }

      animId = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(animId);
  }, []);

  return (
    <div className="neuro-grid">

      {/* Live brainwave bands */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">
          <span>Live Brainwave Bands — EEG via EXG Pill (host-side FFT)</span>
          {alphaBurst && <span className="alpha-burst-tag">α BURST · relaxed/eyes-closed</span>}
        </div>
        <div className="eeg-bands">
          {EEG_BANDS.map(b => (
            <div className="eeg-band" key={b.key}>
              <div className="eeg-band-head">
                <span className="eeg-band-label" style={{ color: b.color }}>{b.label}</span>
                <span className="eeg-band-range">{b.range}</span>
                <span className="eeg-band-val">0%</span>
              </div>
              <div className="eeg-band-track">
                <div
                  className="eeg-band-fill"
                  ref={el => (barsRef.current[b.key] = el)}
                  style={{ background: b.color }}
                ></div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Raw EEG waveform */}
      <div className="card">
        <div className="card-title">Raw EEG Signal (A1 · DC-blocked)</div>
        <div className="canvas-container" style={{ minHeight: "150px", background: "#0f1226" }}>
          <canvas ref={canvasEegRef}></canvas>
        </div>
        <div className="neuro-hint">
          Switch device to <strong>EEG_STREAM</strong> for raw passthrough, or run
          <code> python host/eeg_viz.py</code> for the standalone band visualiser.
        </div>
      </div>

      {/* Head-gesture (MPU6050) tracker */}
      <div className="card">
        <div className="card-title">Head Gesture Tracker — MPU6050 (I²C)</div>
        <div className="head-pad">
          <div className="head-pad-cross-h"></div>
          <div className="head-pad-cross-v"></div>
          <div className="head-dot" ref={headDotRef}></div>
        </div>
        <div className="gesture-badges">
          {["NOD", "SHAKE", "TILT"].map(g => (
            <span
              key={g}
              className={`gesture-badge ${flash && flash.kind === "imu" && flash.label === g ? "fired" : ""}`}
            >{g}</span>
          ))}
        </div>
      </div>

      {/* Ocular / EOG + mode + AAC routing */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">Ocular Input (EOG) & Communication Routing</div>
        <div className="ocular-row">
          <div className={`eye-indicator ${flash && flash.kind === "eog" ? "blinking" : ""}`}>
            <div className="eye-shape"><div className="eye-pupil"></div></div>
            <span className="eye-label">
              {flash && flash.kind === "eog" ? flash.label : "Eyes Open"}
            </span>
          </div>

          <div className="ocular-info">
            <p>
              Electrodes beside the outer eye canthi capture the corneoretinal potential.
              A sustained blink (&gt;200 ms) registers as <strong>SELECT</strong> for the AAC
              speller; saccades scan left / right. The same channel can stream EEG for the
              bands above — one EXG Pill, two roles.
            </p>
            <div className="ocular-stats">
              <div><span>Aux (A1)</span><strong>{currentVitals.auxRaw}</strong></div>
              <div><span>Last gesture</span><strong>{lastGesture ? lastGesture.label : "—"}</strong></div>
              <div><span>Mode</span><strong>{MODE_NAMES[systemMode]}</strong></div>
              <div><span>Link</span><strong style={{ color: isConnected ? "var(--med-green)" : "var(--med-red)" }}>
                {isConnected ? "ONLINE" : "OFFLINE"}</strong></div>
            </div>
            <div className="mode-quick">
              {MODE_NAMES.map((name, idx) => (
                <button
                  key={idx}
                  className={`mode-btn ${systemMode === idx ? "active" : ""}`}
                  onClick={() => setSystemMode(idx)}
                >{name === "EEG_STREAM" ? "EEG" : name}</button>
              ))}
            </div>
          </div>
        </div>
      </div>

    </div>
  );
};

export default NeuroLab;

import React, { useEffect, useRef, useState } from 'react';
import { useTelemetry, STATE_NAMES } from '../context/TelemetryContext';

const Dashboard = () => {
  const { 
    currentVitals, 
    params, 
    setParams, 
    consoleLogs, 
    eventLogs, 
    sendParamChange,
    telemetryHistoryRef,
    isSimulatorActive
  } = useTelemetry();

  // Canvas refs
  const canvasEmgRef = useRef(null);
  const canvasFsrRef = useRef(null);
  const canvasServoRef = useRef(null);
  const canvasEcgRef = useRef(null);

  // Calibration modal state
  const [showCalModal, setShowCalModal] = useState(false);
  const [calInstruction, setCalInstruction] = useState("");
  const [calProgress, setCalProgress] = useState(0);

  // Setup live canvas drawing loops
  useEffect(() => {
    let animationId;
    const history = telemetryHistoryRef.current;

    const drawGrid = (ctx, width, height) => {
      ctx.strokeStyle = '#f1f5f9';
      ctx.lineWidth = 1;
      
      // vertical grid lines
      for (let x = 0; x < width; x += 40) {
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, height);
        ctx.stroke();
      }
      // horizontal grid lines
      for (let y = 0; y < height; y += 30) {
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(width, y);
        ctx.stroke();
      }
    };

    const drawPlot = (canvas, dataList, color, maxVal, minVal = 0, line2Data = null, color2 = null, isStateFill = false, stateList = null) => {
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      const width = canvas.width = canvas.parentElement.clientWidth;
      const height = canvas.height = canvas.parentElement.clientHeight;

      ctx.clearRect(0, 0, width, height);
      drawGrid(ctx, width, height);

      const len = dataList.length;
      if (len === 0) return;

      const getX = (index) => (index / (len - 1)) * width;
      const getY = (val) => {
        const range = maxVal - minVal;
        const norm = (val - minVal) / (range || 1);
        return height - (norm * (height - 20)) - 10; // 10px padding
      };

      // If FSR plot: Draw state background fills (ARMED, PREPOS, COMMIT, etc.)
      if (isStateFill && stateList) {
        for (let i = 0; i < len - 1; i++) {
          const state = stateList[i];
          if (state > 0) {
            let fillColor = 'rgba(241, 245, 249, 0.5)';
            if (state === 2) fillColor = 'rgba(224, 242, 254, 0.4)'; // PREPOS (blue)
            if (state === 3 || state === 4) fillColor = 'rgba(204, 251, 241, 0.4)'; // HOLD/COMMIT (teal)
            if (state === 6) fillColor = 'rgba(254, 226, 226, 0.4)'; // ABORT (red)
            if (state === 7) fillColor = 'rgba(254, 243, 199, 0.4)'; // LOCKOUT (amber)

            ctx.fillStyle = fillColor;
            ctx.fillRect(getX(i), 0, getX(i+1) - getX(i) + 1, height);
          }
        }
      }

      // Draw primary curve
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5;
      ctx.moveTo(getX(0), getY(dataList[0]));
      for (let i = 1; i < len; i++) {
        ctx.lineTo(getX(i), getY(dataList[i]));
      }
      ctx.stroke();

      // Draw secondary curve if present
      if (line2Data && color2) {
        ctx.beginPath();
        ctx.strokeStyle = color2;
        ctx.lineWidth = 1.5;
        ctx.moveTo(getX(0), getY(line2Data[0]));
        for (let i = 1; i < len; i++) {
          ctx.lineTo(getX(i), getY(line2Data[i]));
        }
        ctx.stroke();
      }
    };

    const render = () => {
      // 1. EMG & TKEO (Lead I)
      drawPlot(canvasEmgRef.current, history.emgNorm, '#0284c7', 1.0, 0, history.tkeo.map(v => v / 15000), '#0d9488');
      
      // 2. FSR & dF/dt (Lead II)
      drawPlot(canvasFsrRef.current, history.fsr, '#475569', 1024, 0, null, null, true, history.state);

      // 3. Servo Angle (Lead III)
      drawPlot(canvasServoRef.current, history.servo, '#0d9488', 180, 0);

      // 4. ECG (Lead IV)
      drawPlot(canvasEcgRef.current, history.ecg, '#dc2626', 1.5, -0.5);

      animationId = requestAnimationFrame(render);
    };

    render();

    return () => cancelAnimationFrame(animationId);
  }, []);

  // Run patient calibration trigger
  const runCalibration = () => {
    setShowCalModal(true);
    setCalInstruction("CALIBRATING: Keep muscles fully relaxed...");
    setCalProgress(0);
    
    sendParamChange("c", ""); // calibrate trigger

    let progress = 0;
    const interval = setInterval(() => {
      progress += 5;
      setCalProgress(progress);
      
      if (progress === 40) {
        setCalInstruction("CALIBRATING: Perform Maximum Voluntary Contraction (MVC) now!");
      }
      if (progress >= 100) {
        clearInterval(interval);
        setShowCalModal(false);
        setCalInstruction("");
      }
    }, 200);
  };

  // Simulate cardiac exertion lockout spike
  const triggerCardiacSpike = () => {
    sendParamChange("T", "1"); // Cardiac simulation spike
  };

  return (
    <div className="dashboard-grid">
      {/* Sidebar Vitals & Tuners */}
      <div className="sidebar">
        
        {/* Vitals Cards */}
        <div className="card">
          <div className="card-title">
            <span>Active Vitals</span>
          </div>
          <div className="vitals-grid">
            <div className="vital-gauge vital-emg">
              <div className="vital-info">
                <span className="vital-label">Flexor RMS</span>
                <span className="vital-value">{currentVitals.emgNorm}</span>
              </div>
              <span className="vital-unit">RMS</span>
            </div>
            
            <div className="vital-gauge vital-tkeo">
              <div className="vital-info">
                <span className="vital-label">TKEO Energy</span>
                <span className="vital-value">{currentVitals.tkeo}</span>
              </div>
              <span className="vital-unit">uV²</span>
            </div>

            <div className="vital-gauge vital-fsr">
              <div className="vital-info">
                <span className="vital-label">Contact Force</span>
                <span className="vital-value">{currentVitals.fsr}</span>
              </div>
              <span className="vital-unit">ADC</span>
            </div>

            <div className="vital-gauge vital-bpm">
              <div className="vital-info">
                <span className="vital-label">Heart Rate</span>
                <span className="vital-value">{currentVitals.bpm}</span>
              </div>
              <span className="vital-unit">BPM</span>
            </div>
          </div>
        </div>

        {/* FSM Diagnostics Stepper */}
        <div className="card">
          <div className="card-title">FSM State Machine</div>
          <div className="state-stepper">
            {STATE_NAMES.map((name, idx) => (
              <div key={idx} className={`state-step ${currentVitals.state === idx ? "active" : ""}`}>
                <span className="state-step-dot"></span>
                {idx}: {name}
              </div>
            ))}
          </div>
        </div>

        {/* Calibration Clinic Panel */}
        <div className="card">
          <div className="card-title">Calibration Clinic</div>
          <div className="param-tuner">
            <button className="btn-primary" style={{ width: "100%", marginBottom: "8px" }} onClick={runCalibration}>
              Run Calibration Cycle
            </button>
            
            <div className="slider-group">
              <div className="slider-header">
                <span>Commit Threshold</span>
                <span className="slider-value">{params.rmsCommit}</span>
              </div>
              <input 
                type="range" 
                min="0.05" 
                max="0.95" 
                step="0.01" 
                value={params.rmsCommit} 
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  setParams(p => ({ ...p, rmsCommit: val }));
                  sendParamChange("r", val);
                }}
              />
            </div>

            <div className="slider-group">
              <div className="slider-header">
                <span>Slip Threshold</span>
                <span className="slider-value">{params.slipTh}</span>
              </div>
              <input 
                type="range" 
                min="1.0" 
                max="25.0" 
                step="0.5" 
                value={params.slipTh} 
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  setParams(p => ({ ...p, slipTh: val }));
                  sendParamChange("s", val);
                }}
              />
            </div>

            <div className="slider-group">
              <div className="slider-header">
                <span>Slip Gain (kSlip)</span>
                <span className="slider-value">{params.kSlip.toFixed(2)}</span>
              </div>
              <input 
                type="range" 
                min="0.1" 
                max="2.0" 
                step="0.05" 
                value={params.kSlip} 
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  setParams(p => ({ ...p, kSlip: val }));
                  sendParamChange("k", val);
                }}
              />
            </div>

            <div className="slider-group">
              <div className="slider-header">
                <span>Abort Limit (ms)</span>
                <span className="slider-value">{params.tAbortMs}</span>
              </div>
              <input 
                type="range" 
                min="100" 
                max="1500" 
                step="50" 
                value={params.tAbortMs} 
                onChange={(e) => {
                  const val = parseInt(e.target.value);
                  setParams(p => ({ ...p, tAbortMs: val }));
                  sendParamChange("a", val);
                }}
              />
            </div>

            <div className="slider-group">
              <div className="slider-header">
                <span>Hold Window (ms)</span>
                <span className="slider-value">{params.tHoldMs}</span>
              </div>
              <input 
                type="range" 
                min="50" 
                max="1000" 
                step="25" 
                value={params.tHoldMs} 
                onChange={(e) => {
                  const val = parseInt(e.target.value);
                  setParams(p => ({ ...p, tHoldMs: val }));
                  sendParamChange("h", val);
                }}
              />
            </div>

            <div className="slider-group">
              <div className="slider-header">
                <span>Extensor Open</span>
                <span className="slider-value">{params.extOpen.toFixed(2)}</span>
              </div>
              <input 
                type="range" 
                min="0.05" 
                max="0.95" 
                step="0.01" 
                value={params.extOpen} 
                onChange={(e) => {
                  const val = parseFloat(e.target.value);
                  setParams(p => ({ ...p, extOpen: val }));
                  sendParamChange("e", val);
                }}
              />
            </div>

            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px", marginTop: "8px" }}>
              <button onClick={() => sendParamChange("P", "1")}>Trigger Prepos</button>
              <button className="btn-alert" onClick={() => sendParamChange("R", "1")}>Reset FSM</button>
            </div>
          </div>
        </div>

      </div>

      {/* Main Plot Curves & Logs */}
      <div className="workspace-main">
        <div className="oscilloscope-grid">
          <div className="card oscilloscope-card">
            <div className="oscilloscope-header">
              <span className="oscilloscope-title">
                <span className="oscilloscope-lead-dot" style={{ backgroundColor: "var(--med-blue)" }}></span>
                Lead I: Flexor EMG & TKEO Envelope
              </span>
            </div>
            <div className="canvas-container">
              <canvas ref={canvasEmgRef}></canvas>
            </div>
          </div>

          <div className="card oscilloscope-card">
            <div className="oscilloscope-header">
              <span className="oscilloscope-title">
                <span className="oscilloscope-lead-dot" style={{ backgroundColor: "var(--med-slate)" }}></span>
                Lead II: FSR Force & Auto-Trigger Highlights
              </span>
            </div>
            <div className="canvas-container">
              <canvas ref={canvasFsrRef}></canvas>
            </div>
          </div>

          <div className="card oscilloscope-card">
            <div className="oscilloscope-header">
              <span className="oscilloscope-title">
                <span className="oscilloscope-lead-dot" style={{ backgroundColor: "var(--med-teal)" }}></span>
                Lead III: Servo Command Actuation Angle
              </span>
            </div>
            <div className="canvas-container">
              <canvas ref={canvasServoRef}></canvas>
            </div>
          </div>

          <div className="card oscilloscope-card">
            <div className="oscilloscope-header">
              <span className="oscilloscope-title">
                <span className="oscilloscope-lead-dot" style={{ backgroundColor: "var(--med-red)" }}></span>
                Lead IV: Electrocardiogram (ECG) Exertion
              </span>
              <button 
                style={{ fontSize: "0.7rem", padding: "2px 8px", borderColor: "var(--med-red)", color: "var(--med-red)" }}
                onClick={triggerCardiacSpike}
              >
                Simulate Exertion Spike
              </button>
            </div>
            <div className="canvas-container">
              <canvas ref={canvasEcgRef}></canvas>
            </div>
          </div>
        </div>

        {/* Console Log Feed */}
        <div className="bottom-section">
          <div className="card" style={{ display: "flex", flexDirection: "column" }}>
            <div className="card-title">Diagnostics & Auto-Captured Event Log</div>
            <div className="event-feed">
              {eventLogs.map((log, idx) => (
                <div key={idx} className="event-item">
                  <span className="event-name">{log.name}</span>
                  <span className={`event-badge ${log.badgeClass}`}>{log.badgeText}</span>
                  <span className="event-time">{log.time}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="terminal-container">
            <div className="terminal-log">
              {consoleLogs.map((log, idx) => (
                <div key={idx} style={{ color: log.isError ? "var(--med-red)" : "#38bdf8" }}>
                  [{log.time}] {log.text}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* Calibration Modal */}
      {showCalModal && (
        <div className="calibration-modal">
          <div className="modal-content">
            <div className="modal-header">PREHEND Patient Calibration Cycle</div>
            <div className="modal-body">{calInstruction}</div>
            <div className="modal-progress">
              <div className="modal-progress-bar" style={{ width: `${calProgress}%` }}></div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default Dashboard;

import React, { useState, useEffect, useRef } from 'react';
import { useTelemetry } from '../context/TelemetryContext';

const Clinician = () => {
  const { currentVitals } = useTelemetry();

  // Clinician Diagnostics States
  const [calHistory, setCalHistory] = useState([]);
  const [estimatedDays, setEstimatedDays] = useState(0);
  const [sleepLogs, setSleepLogs] = useState([]);

  // PT Game State
  const canvasPtRef = useRef(null);
  const [isPtRunning, setIsPtRunning] = useState(false);
  const [ptScore, setPtScore] = useState(0);
  const ptScoreRef = useRef(0);
  const activeEmgRef = useRef(0.0);

  // Keep latest EMG value in ref for animation frame loop
  useEffect(() => {
    activeEmgRef.current = parseFloat(currentVitals.emgNorm || 0);
  }, [currentVitals.emgNorm]);

  // Load API Data
  useEffect(() => {
    fetch('/api/prognostics')
      .then(res => res.json())
      .then(data => {
        setCalHistory(data.calibrations || []);
        setEstimatedDays(data.estimated_days_left || 0);
      })
      .catch(e => console.error(e));

    fetch('/api/sleep')
      .then(res => res.json())
      .then(data => setSleepLogs(data || []))
      .catch(e => console.error(e));
  }, []);

  // PT Game Render Loop
  useEffect(() => {
    if (!isPtRunning) return;
    const canvas = canvasPtRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let animId;
    let time = 0;
    
    const renderGame = () => {
      time += 0.05;
      const width = canvas.width = canvas.parentElement.clientWidth;
      const height = canvas.height = canvas.parentElement.clientHeight;

      ctx.fillStyle = '#0f172a';
      ctx.fillRect(0, 0, width, height);

      // Target path (sinusoidal)
      const targetY = height / 2 + Math.sin(time) * (height / 3);
      
      // User path (based on current flexor muscle RMS)
      // Map emgNorm (0.0 to 1.0) to canvas height
      const userY = height - (activeEmgRef.current * height);

      // Draw Target circle (green)
      ctx.beginPath();
      ctx.arc(150, targetY, 12, 0, 2 * Math.PI);
      ctx.fillStyle = '#16a34a';
      ctx.fill();

      // Draw User indicator (blue)
      ctx.beginPath();
      ctx.arc(150, userY, 10, 0, 2 * Math.PI);
      ctx.fillStyle = '#0284c7';
      ctx.fill();

      // Draw alignment guide lines
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, height / 2);
      ctx.lineTo(width, height / 2);
      ctx.stroke();

      // Score Calculation
      const dist = Math.abs(targetY - userY);
      if (dist < 20) {
        ptScoreRef.current += 1;
        setPtScore(ptScoreRef.current);
      }

      ctx.fillStyle = 'white';
      ctx.font = '12px sans-serif';
      ctx.fillText(`Target Path`, 10, 20);
      ctx.fillText(`Align Blue Dot inside Green Target`, 10, 36);

      animId = requestAnimationFrame(renderGame);
    };

    renderGame();
    return () => cancelAnimationFrame(animId);
  }, [isPtRunning]);

  const handleStartPtSession = () => {
    if (isPtRunning) {
      setIsPtRunning(false);
    } else {
      ptScoreRef.current = 0;
      setPtScore(0);
      setIsPtRunning(true);
    }
  };

  const isLowSnr = parseFloat(currentVitals.snr) < 5.0 && currentVitals.snr !== "0.0";

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }}>
      
      {/* 1. Myoelectric Prognostics & SNR */}
      <div className="card">
        <div className="card-title">Myoelectric Prognostics & Signal-to-Noise (SNR)</div>
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: "0.85rem", color: "var(--text-secondary)" }}>Current SNR:</span>
            <span style={{ fontSize: "1.2rem", fontFamily: "monospace", fontWeight: "bold", color: isLowSnr ? "var(--med-red)" : "var(--med-green)" }}>
              {currentVitals.snr} dB
            </span>
          </div>

          {isLowSnr && (
            <div className="med-item overdue" style={{ borderLeft: "4px solid var(--med-red)", padding: "8px", fontSize: "0.8rem", fontWeight: "bold", animation: "pulse-bg 2s infinite" }}>
              ⚠️ ELECTRODE SIGNAL DEGRADED: Clean flexor sensor site immediately.
            </div>
          )}

          <div style={{ marginTop: "12px" }}>
            <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", textTransform: "uppercase", marginBottom: "6px" }}>
              MVC Calibration Decay (Last 7 Days)
            </div>
            <div className="bar-chart">
              {calHistory.map((cal, idx) => {
                const heightPct = Math.min(100, (cal.mvc_value / 1.5) * 100);
                return (
                  <div key={idx} className="chart-bar-container">
                    <div className="chart-bar" style={{ height: `${heightPct}%` }}></div>
                    <span className="chart-label">{cal.timestamp.split(' ')[0].substring(5)}</span>
                  </div>
                );
              })}
            </div>
            <div style={{ marginTop: "8px", fontSize: "0.85rem", textAlign: "center", color: "var(--text-secondary)" }}>
              Estimated remaining days before calibration decay limit: <strong style={{ color: "var(--med-amber)" }}>{estimatedDays} Days</strong>
            </div>
          </div>
        </div>
      </div>

      {/* 2. Sleep RESTLESSNESS Tracker */}
      <div className="card">
        <div className="card-title">Nocturnal Sleep Quality index (Restlessness)</div>
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", textTransform: "uppercase", marginBottom: "6px" }}>
            Spasm Activity & Sleep Restlessness
          </div>
          <div className="bar-chart">
            {sleepLogs.map((log, idx) => {
              const heightPct = log.restlessness_score;
              return (
                <div key={log.id} className="chart-bar-container">
                  <div className="chart-bar" style={{ height: `${heightPct}%`, backgroundColor: log.spasm_count > 15 ? "var(--med-red)" : "var(--med-blue)" }}></div>
                  <span className="chart-label">{log.date_str.substring(5)}</span>
                </div>
              );
            })}
          </div>
          <div style={{ fontSize: "0.8rem", color: "var(--text-secondary)", textAlign: "center" }}>
            Lower scores represent a restful sleep state. Red bars indicate nocturnal spasms detected.
          </div>
        </div>
      </div>

      {/* 3. Physiotherapy Game */}
      <div className="card">
        <div className="card-title">Myoelectric Physiotherapy Training</div>
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <div style={{ position: "relative", width: "100%", height: "120px", background: "#0f172a", borderRadius: "8px", overflow: "hidden", border: "1px solid #1e293b" }}>
            <canvas ref={canvasPtRef} style={{ width: "100%", height: "100%" }}></canvas>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <button 
              className={isPtRunning ? "btn-alert" : "btn-primary"} 
              onClick={handleStartPtSession}
            >
              {isPtRunning ? "Stop PT Session" : "Start PT Session"}
            </button>
            <span style={{ fontSize: "0.9rem", fontWeight: "bold" }}>
              Alignment Score: <span style={{ color: "var(--med-blue)", fontSize: "1.1rem" }}>{ptScore}</span>
            </span>
          </div>
        </div>
      </div>

      {/* 4. PDF Export Summary */}
      <div className="card" style={{ display: "flex", flexDirection: "column", justifyContent: "center", alignItems: "center" }}>
        <div className="card-title">Physician Records</div>
        <div style={{ display: "flex", flexDirection: "column", gap: "12px", alignItems: "center", textAlign: "center" }}>
          <p style={{ fontSize: "0.85rem", color: "var(--text-secondary)" }}>
            Generate a full clinical brief including demographics, calibration compliance, sleep reports, and safety logs.
          </p>
          <a 
            href="/api/report" 
            target="_blank" 
            rel="noopener noreferrer" 
            className="btn-primary" 
            style={{ display: "inline-block", padding: "8px 20px", textDecoration: "none", borderRadius: "6px", color: "white", fontWeight: "bold" }}
          >
            Open & Print Physician Report
          </a>
        </div>
      </div>

    </div>
  );
};

export default Clinician;

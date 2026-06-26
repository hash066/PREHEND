import React from 'react';
import { HashRouter as Router, Routes, Route, NavLink } from 'react-router-dom';
import { TelemetryProvider, useTelemetry, MODE_NAMES } from './context/TelemetryContext';
import Dashboard from './pages/Dashboard';
import Caregiver from './pages/Caregiver';
import Clinician from './pages/Clinician';
import NeuroLab from './pages/NeuroLab';

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true, desc: "Telemetry & Calibration" },
  { to: "/neuro", label: "Neuro Lab", desc: "Brainwaves · Gestures · AAC I/O" },
  { to: "/caregiver", label: "Caregiver Suite", desc: "AAC Speller · Meds · Geofence" },
  { to: "/clinician", label: "Clinician Diagnostics", desc: "Prognostics · Sleep · PT" },
];

// Mode switcher: GRASP / SPEAK / EEG_STREAM (sends M0/M1/M2 to device)
const ModeSwitcher = () => {
  const { systemMode, setSystemMode } = useTelemetry();
  return (
    <div className="mode-switcher">
      <div className="mode-switcher-label">Operating Mode</div>
      <div className="mode-switcher-btns">
        {MODE_NAMES.map((name, idx) => (
          <button
            key={idx}
            className={`mode-btn ${systemMode === idx ? "active" : ""}`}
            onClick={() => setSystemMode(idx)}
            title={
              idx === 0 ? "Normal predictive grasp FSM" :
              idx === 1 ? "Claw locked open; gestures drive AAC speller" :
              "Raw EEG passthrough for brainwave lab"
            }
          >
            {name === "EEG_STREAM" ? "EEG" : name}
          </button>
        ))}
      </div>
    </div>
  );
};

const Sidebar = () => {
  const { connStatus, isSimulatorActive, startSimulator, connectSerial, connectWiFi, isConnected } = useTelemetry();
  const [ipInput, setIpInput] = React.useState("192.168.4.1");

  return (
    <aside className="app-sidebar">
      <div className="sidebar-brand">
        <div className="hospital-logo">+</div>
        <div>
          <h1>PREHEND</h1>
          <div className="patient-label">Clinical Workstation</div>
        </div>
      </div>

      <nav className="sidebar-nav">
        {NAV_ITEMS.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) => `sidebar-nav-item ${isActive ? "active" : ""}`}
          >
            <span className="nav-text">
              <span className="nav-label">{item.label}</span>
              <span className="nav-desc">{item.desc}</span>
            </span>
          </NavLink>
        ))}
      </nav>

      <ModeSwitcher />

      <div className="sidebar-connection">
        <div className="connection-status">
          <span className={`status-dot ${isConnected ? "active" : ""}`}></span>
          <span>{connStatus}</span>
        </div>

        <button className="btn-primary" style={{ width: "100%" }} onClick={connectSerial}>
          {isConnected && connStatus.includes("USB") ? "Disconnect Serial" : "Connect USB (Serial)"}
        </button>

        <div className="wifi-row">
          <input
            type="text"
            value={ipInput}
            onChange={(e) => setIpInput(e.target.value)}
            placeholder="ESP32 IP"
          />
          <button onClick={() => connectWiFi(ipInput)}>
            {isConnected && connStatus.includes("WiFi") ? "Drop" : "WiFi"}
          </button>
        </div>

        <button
          style={{
            width: "100%",
            borderColor: isSimulatorActive ? "var(--med-red)" : "var(--med-teal)",
            color: isSimulatorActive ? "var(--med-red)" : "var(--med-teal)"
          }}
          onClick={startSimulator}
        >
          {isSimulatorActive ? "Stop Patient Sim" : "Start Patient Sim"}
        </button>
      </div>

      <div className="sidebar-footer">
        Predictive Grasp + AAC · On-device 1 kHz
      </div>
    </aside>
  );
};

function App() {
  return (
    <TelemetryProvider>
      <Router>
        <div className="app-shell">
          <Sidebar />
          <main className="app-main">
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/neuro" element={<NeuroLab />} />
              <Route path="/caregiver" element={<Caregiver />} />
              <Route path="/clinician" element={<Clinician />} />
            </Routes>
          </main>
        </div>
      </Router>
    </TelemetryProvider>
  );
}

export default App;

import React from 'react';
import { HashRouter as Router, Routes, Route, NavLink } from 'react-router-dom';
import { TelemetryProvider, useTelemetry } from './context/TelemetryContext';
import Dashboard from './pages/Dashboard';
import Caregiver from './pages/Caregiver';
import Clinician from './pages/Clinician';

// Header section layout
const NavigationHeader = () => {
  const { connStatus, isSimulatorActive, startSimulator, connectSerial, connectWiFi, isConnected } = useTelemetry();
  const [ipInput, setIpInput] = React.useState("192.168.4.1");

  return (
    <header>
      <div className="brand-section">
        <div className="hospital-logo">✚</div>
        <div>
          <h1>PREHEND Clinical Workstation</h1>
          <div className="patient-label">Diagnostic Telemetry & Calibration Terminal</div>
        </div>
      </div>

      <nav className="nav-links">
        <NavLink to="/" className={({ isActive }) => isActive ? "active" : ""}>Dashboard</NavLink>
        <NavLink to="/caregiver" className={({ isActive }) => isActive ? "active" : ""}>Caregiver Suite</NavLink>
        <NavLink to="/clinician" className={({ isActive }) => isActive ? "active" : ""}>Clinician Diagnostics</NavLink>
      </nav>
      
      <div className="connection-panel">
        <button id="btnSerial" className="btn-primary" onClick={connectSerial}>
          {isConnected && connStatus.includes("USB") ? "Disconnect Serial" : "Connect USB (Serial)"}
        </button>
        
        <input 
          type="text" 
          id="wsUrl" 
          value={ipInput} 
          onChange={(e) => setIpInput(e.target.value)} 
          style={{ width: "120px" }} 
          placeholder="ESP32 IP"
        />
        <button id="btnWS" onClick={() => connectWiFi(ipInput)}>
          {isConnected && connStatus.includes("WiFi") ? "Disconnect WiFi" : "Connect WiFi"}
        </button>
        
        <button 
          id="btnSim" 
          style={{ 
            borderColor: isSimulatorActive ? "var(--med-red)" : "var(--med-teal)", 
            color: isSimulatorActive ? "var(--med-red)" : "var(--med-teal)" 
          }}
          onClick={startSimulator}
        >
          {isSimulatorActive ? "Stop Sim" : "Start Patient Sim"}
        </button>

        <div className="connection-status">
          <span className={`status-dot ${isConnected ? "active" : ""}`} id="connDot"></span>
          <span id="connLabel">{connStatus}</span>
        </div>
      </div>
    </header>
  );
};

function App() {
  return (
    <TelemetryProvider>
      <Router>
        <NavigationHeader />
        <div className="page-container">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/caregiver" element={<Caregiver />} />
            <Route path="/clinician" element={<Clinician />} />
          </Routes>
        </div>
      </Router>
    </TelemetryProvider>
  );
}

export default App;

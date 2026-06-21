import React, { useState, useEffect, useRef } from 'react';
import { useTelemetry } from '../context/TelemetryContext';

const SPELLER_PHRASES = [
  "Hello", "I am hungry", "I am thirsty", "Need medicine",
  "Adjust brace", "Want to sit", "Thank you", "Call nurse"
];

const Caregiver = () => {
  const { currentVitals, speakAnnouncement, addEventLog } = useTelemetry();

  // Speller State
  const [phrases, setPhrases] = useState(SPELLER_PHRASES);
  const [scanIndex, setScanIndex] = useState(0);
  const [selectedPhrase, setSelectedPhrase] = useState("");
  const [isSpellerEnabled, setIsSpellerEnabled] = useState(false);
  const lastStateRef = useRef(0);

  // Medications State
  const [meds, setMeds] = useState([]);

  // Geofence State
  const [patientPos, setPatientPos] = useState({ x: 50, y: 50 }); // percentages
  const [geofenceStatus, setGeofenceStatus] = useState("Safe - In Zone");
  const [isWandering, setIsWandering] = useState(false);

  // Speller Scanning Tick
  useEffect(() => {
    if (!isSpellerEnabled) return;
    const interval = setInterval(() => {
      setScanIndex(idx => (idx + 1) % phrases.length);
    }, 1200);
    return () => clearInterval(interval);
  }, [isSpellerEnabled, phrases]);

  // Telemetry Listener for Speller trigger: Trigger selection when FSM hits PREPOS (state 2)
  useEffect(() => {
    const currentState = currentVitals.state;
    if (isSpellerEnabled && currentState === 2 && lastStateRef.current !== 2) {
      // Pick the currently scanned phrase
      const selected = phrases[scanIndex];
      setSelectedPhrase(prev => prev ? prev + " " + selected : selected);
      speakAnnouncement(selected);
      addEventLog("AAC Selection", `Patient selected phrase: "${selected}"`, "badge-grasp");
    }
    lastStateRef.current = currentState;
  }, [currentVitals.state, isSpellerEnabled, scanIndex, phrases]);

  // Load Medications
  const loadMeds = () => {
    fetch('/api/medications')
      .then(res => res.json())
      .then(data => setMeds(data))
      .catch(e => console.error("Failed to load meds:", e));
  };

  useEffect(() => {
    loadMeds();
    const interval = setInterval(loadMeds, 10000); // refresh every 10s
    return () => clearInterval(interval);
  }, []);

  const toggleMed = (medId, currentlyTaken) => {
    fetch('/api/medications', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ med_id: medId, taken: !currentlyTaken })
    })
    .then(res => res.json())
    .then(() => loadMeds())
    .catch(e => console.error("Failed to update med:", e));
  };

  // Geofence wander logic
  const handleWanderSimulation = () => {
    if (isWandering) {
      // Return to center
      setPatientPos({ x: 50, y: 50 });
      setGeofenceStatus("Safe - In Zone");
      setIsWandering(false);
    } else {
      // Move out of bounds
      setPatientPos({ x: 82, y: 25 });
      setGeofenceStatus("ALERT: Wandering Detected");
      speakAnnouncement("Alert, patient wandering out of bounds");
      addEventLog("Geofence Alert", "Patient wandering out of bounds", "badge-abort");
      setIsWandering(true);
    }
  };

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }}>
      
      {/* 1. Stephen Hawking AAC Scanning Speller */}
      <div className="card" style={{ gridColumn: "1 / -1" }}>
        <div className="card-title">AAC Scanning Speller (Myoelectric Triggered via FSM Commit/Prepos)</div>
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          <div className="speller-display">
            {selectedPhrase || "Select phrase..."}
          </div>
          <div className="speller-grid">
            {phrases.map((phrase, idx) => (
              <div 
                key={idx} 
                className={`speller-cell ${isSpellerEnabled && scanIndex === idx ? "scanning" : ""}`}
              >
                {phrase}
              </div>
            ))}
          </div>
          <div style={{ marginTop: "12px", display: "flex", gap: "8px" }}>
            <button 
              className={isSpellerEnabled ? "btn-alert" : "btn-primary"} 
              onClick={() => setIsSpellerEnabled(!isSpellerEnabled)}
            >
              {isSpellerEnabled ? "Disable AAC Speller" : "Enable AAC Speller"}
            </button>
            <button className="btn-alert" onClick={() => setSelectedPhrase("")}>
              Clear Phrase
            </button>
          </div>
        </div>
      </div>

      {/* 2. Caregiver Medicine Timeline */}
      <div className="card">
        <div className="card-title">Medicine Schedule (Levodopa Tracking)</div>
        <div className="med-timeline">
          {meds.map((med) => {
            const isOverdue = !med.taken && (med.time < "12:00" || (med.id === 1 && new Date().getHours() >= 9)); // Simple check
            return (
              <div 
                key={med.id} 
                className={`med-item ${med.taken ? "taken" : ""} ${isOverdue ? "overdue" : ""}`}
                style={{ cursor: "pointer" }}
                onClick={() => toggleMed(med.id, med.taken)}
              >
                <div style={{ display: "flex", flexDirection: "column" }}>
                  <span className="med-name">{med.name}</span>
                  <span className="med-dose">{med.dose}</span>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
                  <span className="med-time">{med.time}</span>
                  <input 
                    type="checkbox" 
                    checked={!!med.taken} 
                    readOnly 
                    style={{ width: "16px", height: "16px", cursor: "pointer" }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* 3. Wandering Geofence Monitor */}
      <div className="card">
        <div className="card-title">Wandering Geofence Monitor</div>
        <div className="geofence-container" style={{ position: "relative", width: "100%", height: "200px", background: "#e0f2fe", borderRadius: "8px", overflow: "hidden", border: "1px solid var(--border-clinical)" }}>
          <div style={{ position: "absolute", width: "100%", height: "100%", backgroundImage: "radial-gradient(#0284c7 1px, transparent 1px)", backgroundSize: "20px 20px", opacity: 0.2 }}></div>
          {/* Safe zone circle */}
          <div style={{ position: "absolute", left: "50%", top: "50%", transform: "translate(-50%, -50%)", width: "120px", height: "120px", border: "2px dashed #16a34a", borderRadius: "50%", background: "rgba(22, 163, 74, 0.1)" }}></div>
          {/* Patient location dot */}
          <div 
            style={{ 
              position: "absolute", 
              left: `${patientPos.x}%`, 
              top: `${patientPos.y}%`, 
              width: "12px", 
              height: "12px", 
              background: isWandering ? "#dc2626" : "#0284c7", 
              borderRadius: "50%", 
              transform: "translate(-50%, -50%)", 
              transition: "all 0.5s linear", 
              boxShadow: isWandering ? "0 0 10px #dc2626" : "0 0 8px #0284c7" 
            }}
          ></div>
        </div>
        <div style={{ marginTop: "12px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: "0.8rem", fontWeight: "600", color: isWandering ? "var(--med-red)" : "#16a34a" }}>
            {geofenceStatus}
          </span>
          <button 
            style={{ borderColor: "var(--med-amber)", color: "var(--med-amber)", background: "transparent" }}
            onClick={handleWanderSimulation}
          >
            {isWandering ? "Reset Patient" : "Simulate Wander"}
          </button>
        </div>
      </div>

    </div>
  );
};

export default Caregiver;

import React, { createContext, useContext, useState, useEffect, useRef } from 'react';

const TelemetryContext = createContext(null);

export const STATE_NAMES = ["IDLE", "ARMED", "PREPOS", "COMMIT", "HOLD", "RELEASE", "ABORT", "LOCKOUT"];

export const TelemetryProvider = ({ children }) => {
  const [connStatus, setConnStatus] = useState("Offline Mode");
  const [isConnected, setIsConnected] = useState(false);
  const [currentVitals, setCurrentVitals] = useState({
    emgNorm: 0.0,
    tkeo: 0,
    fsr: 0,
    bpm: 75,
    servo: 0,
    state: 0,
    snr: 0,
    dfdt: 0
  });
  
  const [params, setParams] = useState({
    emgBase: 0.02,
    mvc: 1.0,
    tkOnset: 8000.0,
    rmsCommit: 0.35,
    slipTh: 8.0,
    fsrFloor: 200.0,
    kSlip: 0.5,
    tAbortMs: 400,
    tHoldMs: 150,
    extOpen: 0.30,
    hrLo: 45,
    hrHi: 140
  });

  const [consoleLogs, setConsoleLogs] = useState([]);
  const [eventLogs, setEventLogs] = useState([]);
  const [isSimulatorActive, setIsSimulatorActive] = useState(false);

  // References for live telemetry values used in fast canvas drawing loop
  const telemetryHistoryRef = useRef({
    time: Array(300).fill(0),
    emgNorm: Array(300).fill(0),
    tkeo: Array(300).fill(0),
    fsr: Array(300).fill(0),
    dfdt: Array(300).fill(0),
    servo: Array(300).fill(0),
    ecg: Array(300).fill(0),
    bpm: Array(300).fill(75),
    state: Array(300).fill(0)
  });

  const serialPortRef = useRef(null);
  const wsRef = useRef(null);
  const simTimerRef = useRef(null);
  const activeStateRef = useRef(0);
  const paramsRef = useRef(params);

  // Speller scanning state
  const [spellerText, setSpellerText] = useState("");

  // Keep paramsRef up to date
  useEffect(() => {
    paramsRef.current = params;
  }, [params]);

  // TTS Helper (offline SpeechSynthesis)
  const speakAnnouncement = (text) => {
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1.0;
      utterance.pitch = 0.85; // Hawking-esque robot-y/slightly flat
      window.speechSynthesis.speak(utterance);
    }
  };

  const addClinicalLog = (text, isError = false) => {
    const timestamp = new Date().toTimeString().split(' ')[0];
    setConsoleLogs(prev => [...prev.slice(-99), { time: timestamp, text, isError }]);
  };

  const addEventLog = (name, badgeText, badgeClass) => {
    const timeStr = new Date().toTimeString().split(' ')[0];
    const newEvent = { name, badgeText, badgeClass, time: timeStr };
    setEventLogs(prev => [newEvent, ...prev.slice(0, 49)]);

    // Send to backend SQLite DB
    fetch('/api/events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        event_type: name,
        description: badgeText,
        severity: badgeClass === 'badge-abort' || badgeClass === 'badge-slip' ? 'WARNING' : 'INFO'
      })
    }).catch(e => console.error("Failed to persist event:", e));
  };

  // Telemetry updates handler
  const processTelemetryUpdate = (emgVal, tkeoVal, fsrVal, bpmVal, servoVal, stateVal, ecgVal = 0, dfdtVal = 0) => {
    const oldState = activeStateRef.current;
    activeStateRef.current = stateVal;

    // State transitions TTS & Event Logs
    if (stateVal !== oldState) {
      const prevName = STATE_NAMES[oldState];
      const newName = STATE_NAMES[stateVal];
      addClinicalLog(`State Transition: ${prevName} -> ${newName}`);
      
      if (stateVal === 2) { // PREPOS
        addEventLog("Grasp Initiated", "Pre-position Lead", "badge-prepos");
        speakAnnouncement("Pre-position lead initiated");
      } else if (stateVal === 3) { // COMMIT
        addEventLog("Grasp Committed", "Grasp Holding", "badge-grasp");
        speakAnnouncement("Grasp committed");
      } else if (stateVal === 4) { // HOLD
        // hold state
      } else if (stateVal === 6) { // ABORT
        addEventLog("Aborted Grasp", "RMS Confirmation Timeout", "badge-abort");
        speakAnnouncement("Warning: grasp aborted");
      } else if (stateVal === 7) { // LOCKOUT
        addEventLog("Safety Lockout Engaged", "High Heart Rate Exertion Limit", "badge-abort");
        speakAnnouncement("Warning: cardiac exertion lockout engaged");
      }
    }

    // Slip reflex check
    if (stateVal === 4 && dfdtVal < -paramsRef.current.slipTh * 1000) {
      addEventLog("Slip Corrected", `Slip corrected with boost gain`, "badge-slip");
      speakAnnouncement("Grip slip corrected");
    }

    // Electrode SNR Calculation
    const snr = emgVal > 0.05 ? (emgVal / 0.012).toFixed(1) : (0.5 + Math.random()).toFixed(1);

    setCurrentVitals({
      emgNorm: emgVal.toFixed(3),
      tkeo: Math.round(tkeoVal),
      fsr: Math.round(fsrVal),
      bpm: Math.round(bpmVal),
      servo: Math.round(servoVal),
      state: stateVal,
      snr,
      dfdt: Math.round(dfdtVal)
    });

    // Update canvas buffer
    const history = telemetryHistoryRef.current;
    history.time.shift(); history.time.push(Date.now());
    history.emgNorm.shift(); history.emgNorm.push(emgVal);
    history.tkeo.shift(); history.tkeo.push(tkeoVal);
    history.fsr.shift(); history.fsr.push(fsrVal);
    history.dfdt.shift(); history.dfdt.push(dfdtVal);
    history.servo.shift(); history.servo.push(servoVal);
    history.ecg.shift(); history.ecg.push(ecgVal);
    history.bpm.shift(); history.bpm.push(bpmVal);
    history.state.shift(); history.state.push(stateVal);
  };

  // Simulating Patient Telemetry
  const startSimulator = () => {
    if (isSimulatorActive) {
      clearInterval(simTimerRef.current);
      setIsSimulatorActive(false);
      setConnStatus("Offline Mode");
      setIsConnected(false);
      addClinicalLog("Simulator stopped.");
      return;
    }

    setIsSimulatorActive(true);
    setConnStatus("Simulated Mode");
    setIsConnected(true);
    addClinicalLog("Patient Simulator active.");
    speakAnnouncement("Telemetry simulator online");

    let simTick = 0;
    let localFsmState = 0;
    let localServoAngle = 0;
    let extState = 0;

    simTimerRef.current = setInterval(() => {
      simTick += 10; // 100Hz
      
      // Heartbeat signal
      const ecgVal = Math.sin(simTick / 40) * Math.sin(simTick / 12) + (Math.random() * 0.1);
      const bpmVal = 72 + Math.sin(simTick / 3000) * 3;

      // Base noise
      let emgVal = paramsRef.current.emgBase + (Math.random() * 0.01);
      let tkeoVal = (Math.random() * 200);
      let fsrVal = 0;
      let dfdtVal = 0;

      // Simulate Periodic Muscle Contractions
      const cycleTime = simTick % 12000;
      if (cycleTime > 3000 && cycleTime < 4500) {
        // Ramp up Flexor muscle contraction
        const scale = (cycleTime - 3000) / 1500;
        emgVal = paramsRef.current.emgBase + scale * 0.6 + (Math.random() * 0.05);
        tkeoVal = scale * 12000 + (Math.random() * 1000);
      } else if (cycleTime >= 4500 && cycleTime < 8000) {
        // Sustained grip commit
        emgVal = 0.5 + (Math.random() * 0.08);
        tkeoVal = 8500 + (Math.random() * 800);
        fsrVal = 650 + Math.sin(simTick / 200) * 40;
      } else if (cycleTime >= 8000 && cycleTime < 9500) {
        // Extensor release muscle onset
        emgVal = paramsRef.current.emgBase + (Math.random() * 0.01);
        tkeoVal = 100;
        extState = 0.45; // trigger release
      } else {
        extState = 0.02;
      }

      // Re-implement simplified FSM in simulator JS for offline interactive viewing
      if (localFsmState === 0) { // IDLE
        localServoAngle = 0;
        if (tkeoVal > paramsRef.current.tkOnset) {
          localFsmState = 1; // ARMED
        }
      } else if (localFsmState === 1) { // ARMED
        localFsmState = 2; // PREPOS
      } else if (localFsmState === 2) { // PREPOS
        localServoAngle = 35; // prepos angle
        if (emgVal > paramsRef.current.rmsCommit) {
          localFsmState = 3; // COMMIT
        }
      } else if (localFsmState === 3) { // COMMIT
        localServoAngle = 135;
        localFsmState = 4; // HOLD
      } else if (localFsmState === 4) { // HOLD
        localServoAngle = 135;
        if (extState > paramsRef.current.extOpen) {
          localFsmState = 5; // RELEASE
        }
      } else if (localFsmState === 5) { // RELEASE
        localServoAngle = 0;
        localFsmState = 0; // IDLE
      }

      processTelemetryUpdate(emgVal, tkeoVal, fsrVal, bpmVal, localServoAngle, localFsmState, ecgVal, dfdtVal);
    }, 10);
  };

  // WebSerial Connection
  const connectSerial = async () => {
    if (serialPortRef.current) {
      addClinicalLog("Disconnecting Serial...");
      try {
        if (serialPortRef.current.readable) {
          await serialPortRef.current.close();
        }
      } catch (err) {}
      serialPortRef.current = null;
      setConnStatus("Offline Mode");
      setIsConnected(false);
      return;
    }

    try {
      const port = await navigator.serial.requestPort();
      await port.open({ baudRate: 115200 });
      serialPortRef.current = port;
      setConnStatus("USB Serial Online");
      setIsConnected(true);
      addClinicalLog("USB Serial connected successfully.");
      speakAnnouncement("Direct USB hardware telemetry stream online");

      // Reader loop
      const textDecoder = new TextDecoderStream();
      const readableStreamClosed = port.readable.pipeTo(textDecoder.writable);
      const reader = textDecoder.readable.getReader();

      let serialBuffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        serialBuffer += value;
        const lines = serialBuffer.split("\n");
        serialBuffer = lines.pop(); // save incomplete line

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed.startsWith("DAT:")) continue;
          // DAT: ms,emgNorm,tkeo,fsr,bpm,state
          const parts = trimmed.substring(4).split(",");
          if (parts.length >= 6) {
            const emg = parseFloat(parts[1]);
            const tkeo = parseFloat(parts[2]);
            const fsr = parseFloat(parts[3]);
            const bpm = parseFloat(parts[4]);
            const state = parseInt(parts[5]);
            processTelemetryUpdate(emg, tkeo, fsr, bpm, 0, state);
          }
        }
      }
    } catch (err) {
      addClinicalLog(`Serial Error: ${err.message}`, true);
    }
  };

  // WebSocket Connection
  const connectWiFi = (ipAddress) => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
      setConnStatus("Offline Mode");
      setIsConnected(false);
      return;
    }

    const wsUrl = `ws://${ipAddress}:81`;
    addClinicalLog(`Connecting to ${wsUrl}...`);
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnStatus("WiFi Telemetry Online");
      setIsConnected(true);
      addClinicalLog("Wireless telemetry connected.");
      speakAnnouncement("WiFi hardware telemetry stream online");
    };

    ws.onmessage = (event) => {
      const line = event.data.trim();
      const parts = line.split(",");
      if (parts.length >= 6) {
        const emg = parseFloat(parts[1]);
        const tkeo = parseFloat(parts[2]);
        const fsr = parseFloat(parts[3]);
        const bpm = parseFloat(parts[4]);
        const state = parseInt(parts[5]);
        processTelemetryUpdate(emg, tkeo, fsr, bpm, 0, state);
      }
    };

    ws.onerror = (err) => {
      addClinicalLog("WiFi socket error.", true);
    };

    ws.onclose = () => {
      setConnStatus("Offline Mode");
      setIsConnected(false);
      addClinicalLog("WiFi connection closed.");
    };
  };

  const sendParamChange = (letter, val) => {
    const payload = `${letter}${val}\n`;
    if (serialPortRef.current && serialPortRef.current.writable) {
      const encoder = new TextEncoder();
      const writer = serialPortRef.current.writable.getWriter();
      writer.write(encoder.encode(payload));
      writer.releaseLock();
    } else if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(payload);
    }
    addClinicalLog(`Transmitted configuration param: ${letter} = ${val}`);
  };

  return (
    <TelemetryContext.Provider value={{
      connStatus,
      isConnected,
      currentVitals,
      params,
      setParams,
      consoleLogs,
      eventLogs,
      isSimulatorActive,
      startSimulator,
      connectSerial,
      connectWiFi,
      sendParamChange,
      telemetryHistoryRef,
      spellerText,
      setSpellerText,
      speakAnnouncement,
      addClinicalLog,
      addEventLog
    }}>
      {children}
    </TelemetryContext.Provider>
  );
};

export const useTelemetry = () => useContext(TelemetryContext);

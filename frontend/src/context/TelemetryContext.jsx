import React, { createContext, useContext, useState, useEffect, useRef } from 'react';

const TelemetryContext = createContext(null);

export const STATE_NAMES = ["IDLE", "ARMED", "PREPOS", "COMMIT", "HOLD", "RELEASE", "ABORT", "LOCKOUT"];
export const MODE_NAMES = ["GRASP", "SPEAK", "EEG_STREAM"];

// gestureByte encoding (must match firmware): bits 3-0 = eogEvent, bits 5-4 = imuGesture
export const EOG_NAMES = { 1: "BLINK", 2: "BLINK-LONG", 3: "SACCADE-R", 4: "SACCADE-L" };
export const IMU_NAMES = { 1: "NOD", 2: "SHAKE", 3: "TILT" };

export const EEG_BANDS = [
  { key: "delta", label: "δ Delta", range: "0.5–4 Hz", freq: 2.5, color: "#6366f1" },
  { key: "theta", label: "θ Theta", range: "4–8 Hz", freq: 6.0, color: "#0d9488" },
  { key: "alpha", label: "α Alpha", range: "8–13 Hz", freq: 10.0, color: "#16a34a" },
  { key: "beta", label: "β Beta", range: "13–30 Hz", freq: 20.0, color: "#d97706" },
  { key: "gamma", label: "γ Gamma", range: "30–45 Hz", freq: 38.0, color: "#dc2626" },
];

const AUX_WIN = 128; // sliding window for Goertzel band power
const FS_HOST = 100; // telemetry output rate (Hz)

// Goertzel single-bin power estimate
function goertzelPower(buf, freq, fs) {
  const k = (2 * Math.PI * freq) / fs;
  const coeff = 2 * Math.cos(k);
  let s0 = 0, s1 = 0, s2 = 0;
  for (let i = 0; i < buf.length; i++) {
    s0 = buf[i] + coeff * s1 - s2;
    s2 = s1;
    s1 = s0;
  }
  return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / buf.length;
}

export const TelemetryProvider = ({ children }) => {
  const [connStatus, setConnStatus] = useState("Offline Mode");
  const [isConnected, setIsConnected] = useState(false);
  const [systemMode, setSystemModeState] = useState(0); // 0 GRASP, 1 SPEAK, 2 EEG_STREAM
  const [lastGesture, setLastGesture] = useState(null);  // { label, kind, time }
  const [currentVitals, setCurrentVitals] = useState({
    emgNorm: 0.0,
    tkeo: 0,
    fsr: 0,
    bpm: 75,
    servo: 0,
    state: 0,
    snr: 0,
    dfdt: 0,
    auxRaw: 0
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
    state: Array(300).fill(0),
    aux: Array(300).fill(0)
  });

  // Refs polled by NeuroLab's rAF loop (avoids high-frequency re-renders)
  const eegBandsRef = useRef({ delta: 0, theta: 0, alpha: 0, beta: 0, gamma: 0 });
  const headAccelRef = useRef({ x: 0, y: 0 });
  const auxRingRef = useRef(new Float32Array(AUX_WIN));
  const auxIdxRef = useRef(0);
  const auxTickRef = useRef(0);

  const serialPortRef = useRef(null);
  const serialWriterRef = useRef(null);
  const wsRef = useRef(null);
  const simTimerRef = useRef(null);
  const activeStateRef = useRef(0);
  const paramsRef = useRef(params);
  const systemModeRef = useRef(0);

  // Speller scanning state
  const [spellerText, setSpellerText] = useState("");

  useEffect(() => { paramsRef.current = params; }, [params]);
  useEffect(() => { systemModeRef.current = systemMode; }, [systemMode]);

  // TTS Helper (offline SpeechSynthesis) — low pitch for Hawking-esque DECtalk feel
  const speakAnnouncement = (text) => {
    if ('speechSynthesis' in window) {
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.rate = 1.0;
      utterance.pitch = 0.85;
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

  // ---- low-level serial/ws writer ------------------------------------------
  const sendRaw = (payload) => {
    if (serialWriterRef.current) {
      try {
        serialWriterRef.current.write(new TextEncoder().encode(payload));
      } catch (e) { /* writer released */ }
    } else if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(payload);
    }
  };

  // Mode switching: M0 = GRASP, M1 = SPEAK (claw locks open), M2 = EEG_STREAM
  const setSystemMode = (m) => {
    setSystemModeState(m);
    systemModeRef.current = m;
    sendRaw(`M${m}\n`);
    addClinicalLog(`Mode switched → ${MODE_NAMES[m]} (M${m})`);
    speakAnnouncement(
      m === 1 ? "Speak mode. Claw locked open." :
      m === 2 ? "E E G streaming mode." : "Grasp mode active."
    );
  };

  // ---- gesture decode + EEG band update ------------------------------------
  const handleGestureByte = (gestureByte) => {
    if (!gestureByte) return;
    const eog = gestureByte & 0x0F;
    const imu = (gestureByte >> 4) & 0x03;
    if (imu && IMU_NAMES[imu]) {
      setLastGesture({ label: IMU_NAMES[imu], kind: "imu", time: Date.now() });
      addEventLog("Head Gesture", IMU_NAMES[imu], imu === 2 ? "badge-abort" : "badge-grasp");
    }
    if (eog && EOG_NAMES[eog]) {
      setLastGesture({ label: EOG_NAMES[eog], kind: "eog", time: Date.now() });
      if (eog === 2) addEventLog("Eye Blink (long)", "AAC Select", "badge-prepos");
    }
  };

  const pushAux = (auxRaw) => {
    const ring = auxRingRef.current;
    ring[auxIdxRef.current % AUX_WIN] = auxRaw;
    auxIdxRef.current++;

    // recompute band power every 5th sample (~20 Hz)
    if (++auxTickRef.current >= 5) {
      auxTickRef.current = 0;
      const buf = Array.from(ring);
      const raw = EEG_BANDS.map(b => goertzelPower(buf, b.freq, FS_HOST));
      const total = raw.reduce((a, b) => a + b, 0) || 1;
      const bands = {};
      EEG_BANDS.forEach((b, i) => { bands[b.key] = raw[i] / total; });
      eegBandsRef.current = bands;
    }
  };

  // Telemetry updates handler (extended: auxRaw, gestureByte, imuPacked, modeVal)
  const processTelemetryUpdate = (emgVal, tkeoVal, fsrVal, bpmVal, servoVal, stateVal,
                                  ecgVal = 0, dfdtVal = 0,
                                  auxRaw = 0, gestureByte = 0, imuPacked = 0, modeVal = null) => {
    const oldState = activeStateRef.current;
    activeStateRef.current = stateVal;

    if (modeVal !== null && modeVal !== systemModeRef.current) {
      systemModeRef.current = modeVal;
      setSystemModeState(modeVal);
    }

    // State transitions TTS & Event Logs
    if (stateVal !== oldState) {
      const prevName = STATE_NAMES[oldState];
      const newName = STATE_NAMES[stateVal];
      addClinicalLog(`State Transition: ${prevName} -> ${newName}`);

      if (stateVal === 2) {
        addEventLog("Grasp Initiated", "Pre-position Lead", "badge-prepos");
        speakAnnouncement("Pre-position lead initiated");
      } else if (stateVal === 3) {
        addEventLog("Grasp Committed", "Grasp Holding", "badge-grasp");
        speakAnnouncement("Grasp committed");
      } else if (stateVal === 6) {
        addEventLog("Aborted Grasp", "RMS Confirmation Timeout", "badge-abort");
        speakAnnouncement("Warning: grasp aborted");
      } else if (stateVal === 7) {
        addEventLog("Safety Lockout Engaged", "High Heart Rate Exertion Limit", "badge-abort");
        speakAnnouncement("Warning: cardiac exertion lockout engaged");
      }
    }

    if (stateVal === 4 && dfdtVal < -paramsRef.current.slipTh * 1000) {
      addEventLog("Slip Corrected", `Slip corrected with boost gain`, "badge-slip");
      speakAnnouncement("Grip slip corrected");
    }

    // gestures + EEG bands + head accel
    handleGestureByte(gestureByte);
    pushAux(auxRaw);
    if (imuPacked) {
      headAccelRef.current = {
        y: ((imuPacked >> 8) & 0xFF) - 128,
        x: (imuPacked & 0xFF) - 128
      };
    }

    const snr = emgVal > 0.05 ? (emgVal / 0.012).toFixed(1) : (0.5 + Math.random()).toFixed(1);

    setCurrentVitals({
      emgNorm: emgVal.toFixed(3),
      tkeo: Math.round(tkeoVal),
      fsr: Math.round(fsrVal),
      bpm: Math.round(bpmVal),
      servo: Math.round(servoVal),
      state: stateVal,
      snr,
      dfdt: Math.round(dfdtVal),
      auxRaw: Math.round(auxRaw)
    });

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
    history.aux.shift(); history.aux.push(auxRaw);
  };

  // Simulating Patient Telemetry (now also drives EEG, EOG blinks & head gestures)
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
    let nextGestureAt = 4000;
    let gestureToggle = 0;

    simTimerRef.current = setInterval(() => {
      simTick += 10; // 100Hz
      const speakMode = systemModeRef.current !== 0;

      const ecgVal = Math.sin(simTick / 40) * Math.sin(simTick / 12) + (Math.random() * 0.1);
      const bpmVal = 72 + Math.sin(simTick / 3000) * 3;

      // --- synthetic EEG aux signal (counts) ---
      // alpha amplitude swells slowly (mimics eyes-open/closed); blink injects a spike
      const alphaGain = 600 + 500 * (0.5 + 0.5 * Math.sin(simTick / 4000));
      let auxRaw =
        alphaGain * Math.sin(2 * Math.PI * 10 * simTick / 1000) +   // alpha 10 Hz
        250 * Math.sin(2 * Math.PI * 6 * simTick / 1000) +          // theta 6 Hz
        180 * Math.sin(2 * Math.PI * 20 * simTick / 1000) +         // beta 20 Hz
        120 * Math.sin(2 * Math.PI * 2.5 * simTick / 1000) +        // delta
        (Math.random() - 0.5) * 200;

      let emgVal = paramsRef.current.emgBase + (Math.random() * 0.01);
      let tkeoVal = (Math.random() * 200);
      let fsrVal = 0;
      let dfdtVal = 0;

      // --- periodic gestures (NOD / long-blink) for AAC demo ---
      let gestureByte = 0;
      let imuPacked = ((128) << 8) | 128;
      if (simTick >= nextGestureAt) {
        nextGestureAt = simTick + 4000 + Math.random() * 2000;
        gestureToggle = (gestureToggle + 1) % 2;
        if (gestureToggle === 0) {
          gestureByte = (1 << 4);            // IMU NOD
          imuPacked = ((128 + 70) << 8) | 128;
        } else {
          gestureByte = 2;                   // EOG long blink (select)
          auxRaw += 2500;                    // blink spike on the EEG trace
        }
      }

      // grasp cascade only runs in GRASP mode
      if (!speakMode) {
        const cycleTime = simTick % 12000;
        if (cycleTime > 3000 && cycleTime < 4500) {
          const scale = (cycleTime - 3000) / 1500;
          emgVal = paramsRef.current.emgBase + scale * 0.6 + (Math.random() * 0.05);
          tkeoVal = scale * 12000 + (Math.random() * 1000);
        } else if (cycleTime >= 4500 && cycleTime < 8000) {
          emgVal = 0.5 + (Math.random() * 0.08);
          tkeoVal = 8500 + (Math.random() * 800);
          fsrVal = 650 + Math.sin(simTick / 200) * 40;
        } else if (cycleTime >= 8000 && cycleTime < 9500) {
          emgVal = paramsRef.current.emgBase + (Math.random() * 0.01);
          tkeoVal = 100;
          extState = 0.45;
        } else {
          extState = 0.02;
        }

        if (localFsmState === 0) {
          localServoAngle = 0;
          if (tkeoVal > paramsRef.current.tkOnset) localFsmState = 1;
        } else if (localFsmState === 1) {
          localFsmState = 2;
        } else if (localFsmState === 2) {
          localServoAngle = 35;
          if (emgVal > paramsRef.current.rmsCommit) localFsmState = 3;
        } else if (localFsmState === 3) {
          localServoAngle = 135;
          localFsmState = 4;
        } else if (localFsmState === 4) {
          localServoAngle = 135;
          if (extState > paramsRef.current.extOpen) localFsmState = 5;
        } else if (localFsmState === 5) {
          localServoAngle = 0;
          localFsmState = 0;
        }
      } else {
        // SPEAK / EEG_STREAM: claw locked open, FSM frozen at IDLE
        localFsmState = 0;
        localServoAngle = 0;
      }

      processTelemetryUpdate(emgVal, tkeoVal, fsrVal, bpmVal, localServoAngle, localFsmState,
                             ecgVal, dfdtVal, auxRaw, gestureByte, imuPacked, systemModeRef.current);
    }, 10);
  };

  // ---- field parser shared by serial + websocket ---------------------------
  const parseTelemetryLine = (line) => {
    let payload = line.trim();
    if (payload.startsWith("DAT:")) payload = payload.substring(4);
    // ignore human-readable firmware messages
    if (!/^\d/.test(payload)) return;
    const parts = payload.split(",");
    if (parts.length < 6) return;
    const emg = parseFloat(parts[1]);
    const tkeo = parseFloat(parts[2]);
    const fsr = parseFloat(parts[3]);
    const bpm = parseFloat(parts[4]);
    const state = parseInt(parts[5]);
    if ([emg, tkeo, fsr, bpm, state].some(v => Number.isNaN(v))) return;
    const auxRaw = parts.length > 6 ? parseFloat(parts[6]) || 0 : 0;
    const imuPacked = parts.length > 7 ? parseInt(parts[7]) || 0 : 0;
    const gestureByte = parts.length > 8 ? parseInt(parts[8]) || 0 : 0;
    const modeVal = parts.length > 9 ? parseInt(parts[9]) : null;
    processTelemetryUpdate(emg, tkeo, fsr, bpm, 0, state, 0, 0,
                           auxRaw, gestureByte, imuPacked, modeVal);
  };

  // WebSerial Connection
  const connectSerial = async () => {
    if (serialPortRef.current) {
      addClinicalLog("Disconnecting Serial...");
      try {
        if (serialWriterRef.current) { serialWriterRef.current.releaseLock(); serialWriterRef.current = null; }
        if (serialPortRef.current.readable) await serialPortRef.current.close();
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
      try { serialWriterRef.current = port.writable.getWriter(); } catch (e) {}
      setConnStatus("USB Serial Online");
      setIsConnected(true);
      addClinicalLog("USB Serial connected successfully.");
      speakAnnouncement("Direct USB hardware telemetry stream online");

      const textDecoder = new TextDecoderStream();
      port.readable.pipeTo(textDecoder.writable).catch(() => {});
      const reader = textDecoder.readable.getReader();

      let serialBuffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        serialBuffer += value;
        const lines = serialBuffer.split("\n");
        serialBuffer = lines.pop();
        for (const line of lines) parseTelemetryLine(line);
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
    ws.onmessage = (event) => parseTelemetryLine(event.data);
    ws.onerror = () => addClinicalLog("WiFi socket error.", true);
    ws.onclose = () => {
      setConnStatus("Offline Mode");
      setIsConnected(false);
      addClinicalLog("WiFi connection closed.");
    };
  };

  const sendParamChange = (letter, val) => {
    sendRaw(`${letter}${val}\n`);
    addClinicalLog(`Transmitted configuration param: ${letter} = ${val}`);
  };

  return (
    <TelemetryContext.Provider value={{
      connStatus,
      isConnected,
      systemMode,
      setSystemMode,
      lastGesture,
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
      eegBandsRef,
      headAccelRef,
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

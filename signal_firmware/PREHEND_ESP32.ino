/* ============================================================
   PREHEND  —  predictive, self-protecting grasp controller
   Target : ESP32 DevKit (any variant)
   Stack  : Muscle Shield sEMG (GPIO36) | EXG Pill (GPIO39) |
            FSR (GPIO34) | Servo Claw (GPIO18) | coin motor
            via NPN (GPIO19)
   Note   : entire control loop runs ON DEVICE.
            WiFi + WebSocket telemetry added for wireless monitoring.

   ESP32 port of the original UNO R4 sketch.
   Key decision: the patent's EEG/BP trigger is replaced by an
   EMG-onset trigger (TKEO) so the predictive cascade is reliable
   and fully on-device. EEG/BP survives only as an optional host
   tier (see host/bp_trigger.py); EMG still gates COMMIT.
   ============================================================ */

#include <WiFi.h>
#include <WebServer.h>
#include <WebSocketsServer.h>
#include <ESP32Servo.h>
#include <EEPROM.h>

/* ---- WiFi credentials (edit these) ---- */
const char* WIFI_SSID     = "YOUR_SSID";
const char* WIFI_PASSWORD = "YOUR_PASSWORD";

/* ---- pins (ESP32-adapted) ---- */
const uint8_t PIN_EMG    = 36;   // GPIO36 / VP  – sEMG flexor
const uint8_t PIN_AUX    = 39;   // GPIO39 / VN  – extensor / ECG / EEG
const uint8_t PIN_FSR    = 34;   // GPIO34       – FSR
const uint8_t PIN_SERVO  = 18;   // GPIO18       – servo PWM
const uint8_t PIN_HAPTIC = 19;   // GPIO19       – haptic motor (LEDC PWM)
const uint8_t PIN_LED    = 2;    // GPIO2        – built-in LED

/* ---- A1 role: set to match how you wired the EXG Pill ---- */
enum AuxMode : uint8_t { AUX_EXTENSOR, AUX_ECG, AUX_EEG };
AuxMode auxMode = AUX_EXTENSOR;

/* ---- sampling ---- */
const uint16_t FS_HZ     = 1000;
const uint32_t SAMPLE_US = 1000000UL / FS_HZ;
const float    DT        = 1.0f / FS_HZ;
uint32_t lastSampleUs    = 0;

/* ---- ADC: ESP32 is 12-bit ---- */
const int ADC_MID = 2048;     // 12-bit mid-scale

/* ---- servo geometry ---- */
const int CLAW_OPEN = 0, CLAW_MAXPRE = 70, CLAW_FULL = 160, SERVO_RATE = 6;
Servo claw;
int curAngle = 0, tgtAngle = 0;

/* ---- signal state ---- */
float emgDC = ADC_MID, emgMS = 0, emgRMS = 0;
int   tb0 = 0, tb1 = 0, tb2 = 0;
float tkeoEnv = 0;
float extDC = ADC_MID, extMS = 0, extRMS = 0;
float fsr = 0, fsrLP = 0, fsrPrev = 0, dFdt = 0;
float ecgHP = 0, ecgPrev = 0;
uint32_t lastBeatMs = 0;
float bpm = 0, rr = 0, rrAvg = 800;
bool  exertionOK = true;

/* ---- calibrated parameters (filled by runCalibration) ---- */
struct Params {
  float    emgBase    = 0.02f;     // resting RMS
  float    mvc        = 1.0f;      // max voluntary contraction RMS
  float    tkOnset    = 8000.0f;   // TKEO onset trigger
  float    rmsCommit  = 0.35f;     // confirm: fraction of MVC (normalised)
  float    extOpen    = 0.30f;     // extensor 'open' threshold (RMS)
  float    slipTh     = 8.0f;      // |dF/dt| slip threshold
  float    fsrFloor   = 200.0f;    // 'contact' floor (ADC counts)
  float    kSlip      = 0.5f;      // slip -> boost gain
  uint16_t tAbortMs   = 400;       // confirmation window
  uint16_t tHoldMs    = 150;       // minimum hold
  uint8_t  hrLo       = 45;
  uint8_t  hrHi       = 140;       // exertion bounds (bpm)
} P;

/* ---- EEPROM persistence ---- */
const uint8_t  EEPROM_MAGIC   = 0xAB;
const uint16_t EEPROM_SIZE    = 128;
const uint16_t EEPROM_ADDR    = 0;

void saveParams() {
  EEPROM.write(EEPROM_ADDR, EEPROM_MAGIC);
  EEPROM.put(EEPROM_ADDR + 1, P);
  EEPROM.commit();
  Serial.println(F("EEPROM saved"));
}

void loadParams() {
  if (EEPROM.read(EEPROM_ADDR) == EEPROM_MAGIC) {
    EEPROM.get(EEPROM_ADDR + 1, P);
    Serial.println(F("EEPROM loaded"));
  } else {
    Serial.println(F("EEPROM empty, using defaults"));
  }
}

/* ---- finite state machine ---- */
enum State : uint8_t {
  S_IDLE, S_ARMED, S_PREPOS, S_COMMIT,
  S_HOLD, S_RELEASE, S_ABORT, S_LOCKOUT
};
State    state      = S_IDLE;
uint32_t tStateMs   = 0;
uint32_t tPreMs     = 0;
uint32_t tCommitMs  = 0;
float    conf       = 0.0f;    // onset confidence 0..1

/* ---- WiFi / WebSocket ---- */
WebServer        server(80);
WebSocketsServer webSocket(81);
bool wifiConnected = false;

/* ---- state name lookup (for telemetry) ---- */
const char* stateNames[] = {
  "IDLE", "ARMED", "PREPOS", "COMMIT",
  "HOLD", "RELEASE", "ABORT", "LOCKOUT"
};

/* ---- LEDC PWM channel for haptic motor ---- */
const uint8_t HAPTIC_LEDC_CH   = 0;
const uint32_t HAPTIC_LEDC_FREQ = 5000;
const uint8_t HAPTIC_LEDC_RES  = 8;   // 8-bit: 0-255

/* ============================================================
   EMBEDDED DASHBOARD HTML
   ============================================================ */
const char DASHBOARD_HTML[] PROGMEM = R"rawliteral(
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PREHEND Telemetry</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e6edf3;
  display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:20px}
h1{font-size:1.4em;margin-bottom:14px;color:#58a6ff;letter-spacing:1px}
.status{font-size:.85em;margin-bottom:16px;color:#8b949e}
.status .dot{display:inline-block;width:8px;height:8px;border-radius:50%;
  margin-right:5px;vertical-align:middle;background:#f85149}
.status .dot.on{background:#3fb950}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:12px;width:100%;max-width:600px;margin-bottom:18px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;
  padding:14px;text-align:center;transition:border-color .2s}
.card:hover{border-color:#58a6ff}
.card .label{font-size:.7em;color:#8b949e;text-transform:uppercase;
  letter-spacing:1px;margin-bottom:4px}
.card .value{font-size:1.6em;font-weight:700;color:#f0f6fc}
.card .value.state{font-size:1.3em;color:#d2a8ff}
.btns{display:flex;gap:10px;margin-top:4px}
button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;
  border-radius:8px;padding:8px 18px;cursor:pointer;font-size:.85em;
  transition:background .15s,border-color .15s}
button:hover{background:#30363d;border-color:#58a6ff}
.log{width:100%;max-width:600px;margin-top:14px;background:#161b22;
  border:1px solid #30363d;border-radius:10px;padding:10px;
  font-family:monospace;font-size:.75em;height:120px;overflow-y:auto;
  color:#8b949e}
</style>
</head>
<body>
<h1>&#x1F9BE; PREHEND Telemetry</h1>
<div class="status"><span class="dot" id="dot"></span><span id="connTxt">Disconnected</span></div>
<div class="grid">
  <div class="card"><div class="label">EMG Norm</div><div class="value" id="emg">--</div></div>
  <div class="card"><div class="label">TKEO</div><div class="value" id="tkeo">--</div></div>
  <div class="card"><div class="label">FSR</div><div class="value" id="fsr">--</div></div>
  <div class="card"><div class="label">BPM</div><div class="value" id="bpm">--</div></div>
  <div class="card" style="grid-column:span 2"><div class="label">State</div><div class="value state" id="state">--</div></div>
</div>
<div class="btns">
  <button onclick="ws.send('c')">Calibrate</button>
  <button onclick="ws.send('P')">Pre-position</button>
  <button onclick="ws.send('?')">Dump Params</button>
</div>
<div class="log" id="log"></div>
<script>
const SN=["IDLE","ARMED","PREPOS","COMMIT","HOLD","RELEASE","ABORT","LOCKOUT"];
let ws,retryT=1000;
function connect(){
  ws=new WebSocket("ws://"+location.hostname+":81");
  ws.onopen=()=>{
    document.getElementById("dot").classList.add("on");
    document.getElementById("connTxt").textContent="Connected";
    retryT=1000;
  };
  ws.onclose=()=>{
    document.getElementById("dot").classList.remove("on");
    document.getElementById("connTxt").textContent="Reconnecting…";
    setTimeout(connect,retryT);retryT=Math.min(retryT*2,8000);
  };
  ws.onmessage=(e)=>{
    const d=e.data;
    if(d.startsWith("{")){addLog(d);return;}
    const p=d.split(",");if(p.length<6)return;
    document.getElementById("emg").textContent=parseFloat(p[1]).toFixed(3);
    document.getElementById("tkeo").textContent=Math.round(parseFloat(p[2]));
    document.getElementById("fsr").textContent=Math.round(parseFloat(p[3]));
    document.getElementById("bpm").textContent=Math.round(parseFloat(p[4]));
    const si=parseInt(p[5]);
    document.getElementById("state").textContent=SN[si]||si;
  };
}
function addLog(t){const l=document.getElementById("log");
  l.textContent+=t+"\n";l.scrollTop=l.scrollHeight;}
connect();
</script>
</body>
</html>
)rawliteral";

/* ============================================================
   SIGNAL PROCESSING HELPERS
   (exact same algorithms as the UNO R4 version)
   ============================================================ */

/* flexor EMG: DC-block + RMS envelope + TKEO onset energy */
inline float updateEMG(int raw) {
  emgDC += 0.0008f * (raw - emgDC);
  float x = raw - emgDC;
  emgMS += 0.01f * (x * x - emgMS);
  emgRMS = sqrtf(emgMS);
  tb2 = tb1; tb1 = tb0; tb0 = (int)x;                 // 3-sample buffer
  float psi = (float)tb1 * tb1 - (float)tb0 * tb2;    // Teager-Kaiser
  if (psi < 0) psi = 0;
  tkeoEnv += 0.03f * (psi - tkeoEnv);
  return emgRMS;
}

/* extensor EMG (AUX in EXTENSOR mode): envelope for 'open' */
inline float updateEXT(int raw) {
  extDC += 0.0008f * (raw - extDC);
  float x = raw - extDC;
  extMS += 0.01f * (x * x - extMS);
  extRMS = sqrtf(extMS);
  return extRMS;
}

/* FSR: low-pass + derivative for slip */
inline void updateFSR(int raw) {
  fsr = raw;
  fsrLP += 0.15f * (fsr - fsrLP);
  dFdt = (fsrLP - fsrPrev) / DT;
  fsrPrev = fsrLP;
}

/* ECG (AUX in ECG mode): R-peak -> heart rate -> exertion gate */
inline void updateECG(int raw) {
  float hp = raw - ecgPrev + 0.97f * ecgHP;       // 1st-order high-pass
  ecgHP = hp; ecgPrev = raw;
  static float thr = 1500.0f;
  static bool  refr = false;
  static uint32_t tr = 0;
  uint32_t now = millis();
  if (!refr && hp > thr) {                         // detected beat
    if (lastBeatMs) {
      rr = now - lastBeatMs;
      rrAvg += 0.2f * (rr - rrAvg);
      bpm = 60000.0f / rrAvg;
    }
    lastBeatMs = now; refr = true; tr = now;
  }
  if (refr && now - tr > 250) refr = false;        // 250 ms refractory
  exertionOK = (bpm == 0) || (bpm >= P.hrLo && bpm <= P.hrHi);
}

/* normalised feature helpers */
inline float emgNorm()     { return (emgRMS - P.emgBase) / (P.mvc - P.emgBase + 1e-6f); }
inline bool  onsetFired()  { return tkeoEnv > P.tkOnset && emgNorm() > 0.05f; }
inline bool  rmsConfirm()  { return emgNorm() > P.rmsCommit; }
inline bool  openIntent()  {
  return (auxMode == AUX_EXTENSOR) ? (extRMS > P.extOpen) : (emgNorm() < 0.05f);
}

/* ============================================================
   CALIBRATION (call by sending 'c' over serial or WebSocket)
   ============================================================ */
float measureRMS(uint16_t ms) {
  uint32_t t0 = millis();
  float acc = 0;
  uint32_t n = 0;
  while (millis() - t0 < ms) {
    updateEMG(analogRead(PIN_EMG));
    acc += emgRMS;
    n++;
    delay(1);
  }
  return acc / max((uint32_t)1, n);
}

void broadcastMsg(const String& msg) {
  webSocket.broadcastTXT(msg);
  Serial.println(msg);
}

void runCalibration() {
  broadcastMsg("{\"cal\":\"relax 3s\"}");
  delay(800);
  P.emgBase = measureRMS(3000);

  broadcastMsg("{\"cal\":\"clench 3s\"}");
  delay(800);
  P.mvc = measureRMS(3000);

  P.tkOnset = tkeoEnv * 0.6f;    // onset = fraction of clench energy

  float f = 0;
  for (int i = 0; i < 200; i++) { f += analogRead(PIN_FSR); delay(2); }
  P.fsrFloor = f / 200 + 60;

  if (auxMode == AUX_EXTENSOR) {
    broadcastMsg("{\"cal\":\"open 3s\"}");
    delay(800);
    float e = 0;
    uint32_t t0 = millis(), n = 0;
    while (millis() - t0 < 3000) {
      updateEXT(analogRead(PIN_AUX));
      e += extRMS; n++;
      delay(1);
    }
    P.extOpen = (e / n) * 0.5f;
  }

  String result = "{\"cal\":\"done\",\"base\":" + String(P.emgBase, 4)
                + ",\"mvc\":" + String(P.mvc, 4)
                + ",\"tk\":" + String(P.tkOnset, 1)
                + ",\"floor\":" + String(P.fsrFloor, 1) + "}";
  broadcastMsg(result);

  saveParams();
}

/* ============================================================
   COMMAND HANDLER (shared by Serial and WebSocket)
   ============================================================ */
void handleCommand(const String& cmd) {
  if (cmd.length() == 0) return;

  char c0 = cmd.charAt(0);
  if (c0 == 'c') {
    runCalibration();
  } else if (c0 == 'P') {
    // host BP detector requests an early pre-position; EMG must still
    // confirm within tAbortMs or the cascade ABORTs (no grasp on BP alone)
    if (state == S_IDLE || state == S_ARMED) {
      conf = 0.3f;
      tPreMs = millis();
      state = S_PREPOS; tStateMs = tPreMs;
    }
  } else if (c0 == 'r' && cmd.length() > 1) {
    // 'r0.35' => set rmsCommit
    P.rmsCommit = cmd.substring(1).toFloat();
    broadcastMsg("{\"rmsCommit\":" + String(P.rmsCommit, 3) + "}");
    saveParams();
  } else if (c0 == 's' && cmd.length() > 1) {
    // 's8.0' => set slipTh
    P.slipTh = cmd.substring(1).toFloat();
    broadcastMsg("{\"slipTh\":" + String(P.slipTh, 2) + "}");
    saveParams();
  } else if (c0 == 'k' && cmd.length() > 1) {
    // 'k0.5' => set kSlip
    P.kSlip = cmd.substring(1).toFloat();
    broadcastMsg("{\"kSlip\":" + String(P.kSlip, 3) + "}");
    saveParams();
  } else if (c0 == '?') {
    // dump current params
    String info = "{\"params\":{\"emgBase\":" + String(P.emgBase, 4)
                + ",\"mvc\":" + String(P.mvc, 4)
                + ",\"tkOnset\":" + String(P.tkOnset, 1)
                + ",\"rmsCommit\":" + String(P.rmsCommit, 3)
                + ",\"extOpen\":" + String(P.extOpen, 3)
                + ",\"slipTh\":" + String(P.slipTh, 2)
                + ",\"fsrFloor\":" + String(P.fsrFloor, 1)
                + ",\"kSlip\":" + String(P.kSlip, 3)
                + ",\"tAbortMs\":" + String(P.tAbortMs)
                + ",\"tHoldMs\":" + String(P.tHoldMs)
                + ",\"hrLo\":" + String(P.hrLo)
                + ",\"hrHi\":" + String(P.hrHi) + "}}";
    broadcastMsg(info);
  }
}

/* ============================================================
   WEBSOCKET EVENT HANDLER
   ============================================================ */
void webSocketEvent(uint8_t num, WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      IPAddress ip = webSocket.remoteIP(num);
      Serial.printf("[WS] Client #%u connected from %s\n", num,
                    ip.toString().c_str());
      break;
    }
    case WStype_DISCONNECTED:
      Serial.printf("[WS] Client #%u disconnected\n", num);
      break;
    case WStype_TEXT: {
      String cmd = String((char*)payload).substring(0, length);
      cmd.trim();
      Serial.printf("[WS] Cmd from #%u: %s\n", num, cmd.c_str());
      handleCommand(cmd);
      break;
    }
    default:
      break;
  }
}

/* ============================================================
   WEB SERVER – serves the embedded dashboard
   ============================================================ */
void handleRoot() {
  server.send_P(200, "text/html", DASHBOARD_HTML);
}

void handleNotFound() {
  server.send(404, "text/plain", "Not Found");
}

/* ============================================================
   SETUP
   ============================================================ */
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 2000) { /* wait up to 2 s */ }
  Serial.println(F("\n=== PREHEND ESP32 ==="));

  /* ---- ADC ---- */
  analogReadResolution(12);

  /* ---- EEPROM ---- */
  EEPROM.begin(EEPROM_SIZE);
  loadParams();

  /* ---- haptic motor via LEDC PWM ---- */
  ledcSetup(HAPTIC_LEDC_CH, HAPTIC_LEDC_FREQ, HAPTIC_LEDC_RES);
  ledcAttachPin(PIN_HAPTIC, HAPTIC_LEDC_CH);
  ledcWrite(HAPTIC_LEDC_CH, 0);

  /* ---- LED ---- */
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);

  /* ---- servo ---- */
  claw.attach(PIN_SERVO);
  claw.write(CLAW_OPEN);
  curAngle = CLAW_OPEN;
  delay(300);

  /* ---- WiFi (non-blocking station mode) ---- */
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print(F("WiFi connecting"));
  // Non-blocking: we'll check status in loop()
  uint32_t wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - wifiStart < 10000) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    wifiConnected = true;
    Serial.print(F("WiFi connected.  IP: "));
    Serial.println(WiFi.localIP());
    Serial.print(F("Dashboard: http://"));
    Serial.print(WiFi.localIP());
    Serial.println('/');
  } else {
    Serial.println(F("WiFi NOT connected — running offline (serial only)"));
  }

  /* ---- HTTP server ---- */
  server.on("/", handleRoot);
  server.onNotFound(handleNotFound);
  server.begin();
  Serial.println(F("HTTP server started on port 80"));

  /* ---- WebSocket server ---- */
  webSocket.begin();
  webSocket.onEvent(webSocketEvent);
  Serial.println(F("WebSocket server started on port 81"));

  Serial.println(F("PREHEND ready. Send 'c' to calibrate, 'P' to force pre-position."));
  Serial.println(F("  'r0.35' set rmsCommit | 's8.0' set slipTh | 'k0.5' set kSlip | '?' dump params"));
}

/* ============================================================
   STATE MACHINE (identical logic to UNO R4 version)
   ============================================================ */
void fsmStep() {
  uint32_t now = millis();

  // global safety: ECG exertion gate forces LOCKOUT from any state
  if (auxMode == AUX_ECG && !exertionOK && state != S_LOCKOUT) {
    state = S_LOCKOUT; tStateMs = now; tgtAngle = CLAW_OPEN; return;
  }

  switch (state) {
    case S_IDLE:
      tgtAngle = CLAW_OPEN;
      if (auxMode != AUX_ECG || exertionOK) { state = S_ARMED; tStateMs = now; }
      break;

    case S_ARMED:
      tgtAngle = CLAW_OPEN;
      if (onsetFired()) {                           // fast TKEO onset
        conf = constrain(tkeoEnv / (P.tkOnset * 3.0f), 0.1f, 1.0f);
        tPreMs = now; state = S_PREPOS; tStateMs = now;
      }
      break;

    case S_PREPOS:                                   // pre-position in EMD window
      tgtAngle = (int)(conf * CLAW_MAXPRE);
      if (rmsConfirm()) { tCommitMs = now; state = S_COMMIT; tStateMs = now; }
      else if (now - tPreMs > P.tAbortMs) { state = S_ABORT; tStateMs = now; }
      break;

    case S_COMMIT: {                                // graded full grip
      float grip = constrain(emgNorm(), 0.0f, 1.0f);
      tgtAngle = (int)(CLAW_MAXPRE + grip * (CLAW_FULL - CLAW_MAXPRE));
      if (now - tCommitMs > P.tHoldMs) { state = S_HOLD; tStateMs = now; }
      break;
    }

    case S_HOLD: {
      // autonomous slip reflex (concurrent, fast)
      if (dFdt < -P.slipTh || (fsrLP < P.fsrFloor && fsrLP > 1)) {
        int boost = (int)constrain(-dFdt * P.kSlip, 5.0f, 30.0f);
        tgtAngle = min(curAngle + boost, CLAW_FULL);
      }
      // voluntary override: open always wins
      if (openIntent()) { state = S_RELEASE; tStateMs = now; }
      break;
    }

    case S_RELEASE:
      tgtAngle = CLAW_OPEN;
      if (curAngle <= CLAW_OPEN + 2) { state = S_IDLE; tStateMs = now; }
      break;

    case S_ABORT:
      tgtAngle = CLAW_OPEN;
      if (curAngle <= CLAW_OPEN + 2) { state = S_IDLE; tStateMs = now; }
      break;

    case S_LOCKOUT:
      tgtAngle = CLAW_OPEN;
      if (now - tStateMs > 2000 && (auxMode != AUX_ECG || exertionOK)) {
        state = S_IDLE; tStateMs = now;
      }
      break;
  }
}

/* ============================================================
   OUTPUTS (rate-limited servo + haptic via LEDC)
   ============================================================ */
void driveOutputs() {
  if (tgtAngle > curAngle)      curAngle = min(curAngle + SERVO_RATE, tgtAngle);
  else if (tgtAngle < curAngle) curAngle = max(curAngle - SERVO_RATE, tgtAngle);
  claw.write(constrain(curAngle, CLAW_OPEN, CLAW_FULL));

  int buzz = 0;
  if (state == S_COMMIT || state == S_HOLD)
    buzz = (int)constrain(map((long)fsrLP, (long)P.fsrFloor, 12000, 40, 255), 0, 255);
  ledcWrite(HAPTIC_LEDC_CH, buzz);

  digitalWrite(PIN_LED, state == S_HOLD);
}

/* ============================================================
   SERIAL COMMAND HANDLER
   ============================================================ */
void handleSerial() {
  if (!Serial.available()) return;
  String cmd = "";
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') break;
    cmd += ch;
  }
  cmd.trim();
  if (cmd.length() > 0) {
    Serial.printf("[Serial] Cmd: %s\n", cmd.c_str());
    handleCommand(cmd);
  }
}

/* ============================================================
   MAIN LOOP + TELEMETRY
   ============================================================ */
void loop() {
  /* non-blocking network handling */
  webSocket.loop();
  server.handleClient();

  /* serial commands */
  handleSerial();

  /* WiFi auto-reconnect (non-blocking check, very fast) */
  static uint32_t lastWifiCheck = 0;
  if (millis() - lastWifiCheck > 10000) {
    lastWifiCheck = millis();
    if (WiFi.status() != WL_CONNECTED) {
      wifiConnected = false;
      WiFi.reconnect();
    } else if (!wifiConnected) {
      wifiConnected = true;
      Serial.print(F("WiFi reconnected.  IP: "));
      Serial.println(WiFi.localIP());
    }
  }

  /* fixed 1 kHz cadence */
  uint32_t nowUs = micros();
  if (nowUs - lastSampleUs < SAMPLE_US) return;
  lastSampleUs = nowUs;

  /* read sensors */
  int rEMG = analogRead(PIN_EMG);
  int rAUX = analogRead(PIN_AUX);
  int rFSR = analogRead(PIN_FSR);

  updateEMG(rEMG);
  if (auxMode == AUX_EXTENSOR) updateEXT(rAUX);
  else if (auxMode == AUX_ECG) updateECG(rAUX);
  // AUX_EEG: BP handled by optional host (see host/bp_trigger.py)
  updateFSR(rFSR);

  fsmStep();
  driveOutputs();

  /* decimated telemetry (every 10th sample → 100 Hz) */
  static uint8_t dec = 0;
  if (++dec >= 10) {
    dec = 0;
    float en = emgNorm();
    // CSV: ms,emgNorm,tkeo,fsr,bpm,state
    char buf[80];
    snprintf(buf, sizeof(buf), "%lu,%.3f,%.0f,%.0f,%.0f,%d",
             (unsigned long)millis(), en, tkeoEnv, fsrLP, bpm, (int)state);
    Serial.println(buf);
    webSocket.broadcastTXT(buf);
  }
}

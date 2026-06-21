/* ================================================================
   SIGNAL + ADAPT -- single-channel EMG switch-access controller
   with remappable command vocabulary

   Hardware : Muscle BioAmp Shield v0.3 (A0 only) + Arduino
   Actuator : SG90-class servo (D9) -> mechanical switch press
   Optional : vibration motor (D6), relay (D2), ESP32 telemetry

   This is the SIGNAL firmware referenced by the ADAPT patent disclosure as the
   "frozen" real-time base layer. It is transcribed verbatim from
   SIGNAL_ADAPT_hardware_map_and_firmware.pdf Section 6 (the complete listing).
   Layers 1-2 (acquisition + burst decode) are unchanged SIGNAL; the ADAPT
   additions are only: a remappable command table (6.4), logical-command
   resolution (6.8), and a REMAP serial parser (6.10). No model fitting, no
   blocking, nothing non-deterministic runs in this 1 kHz loop.

   Wire protocol (must match adapt/serial_bridge.py):
     telemetry (decimated): t_ms,emgRMS,tkeoEnv,state,lastCmd,falseNegCount
     remap in:              REMAP,<LOGICAL>,<PATTERN1>[_<PATTERN2>]
   ================================================================ */
#include <Servo.h>

// ---- 6.1 Header, Pins, Constants -------------------------------------------
enum DriveMode { SERVO_MODE, RELAY_MODE };
DriveMode driveMode = SERVO_MODE;
const uint8_t PIN_EMG = A0, PIN_SERVO = 9, PIN_HAPTIC = 6, PIN_RELAY = 2;
const uint16_t FS_HZ = 1000;
const uint32_t SAMPLE_US = 1000000UL / FS_HZ;
uint32_t lastSampleUs = 0;

Servo lever; int LEVER_REST = 0, LEVER_PRESS = 45;

// ---- 6.2 Signal State ------------------------------------------------------
float emgDC = 512, emgMS = 0, emgRMS = 0;
int tb0 = 0, tb1 = 0, tb2 = 0; float tkeoEnv = 0;

struct Params {
  float emgBase = 0.02f;
  float tkOnset = 8000.0f;
  uint16_t debounceMs = 120;
  uint16_t shortMaxMs = 250;
  uint16_t longMinMs = 600;
  uint16_t doubleWinMs = 600;
  uint16_t pressMs = 300;
  uint8_t spasmCount = 5;
  uint16_t spasmWinMs = 2000;
} P;

// ---- 6.3 Burst FSM State ---------------------------------------------------
enum State : uint8_t { S_IDLE, S_BURST, S_CLASSIFY, S_ACTUATE, S_CONFIRM, S_LOCKOUT };
State state = S_IDLE;
uint32_t tBurstStart = 0, tBurstEnd = 0;
uint8_t burstCount = 0;
uint32_t burstTimes[4];
enum Cmd : uint8_t { CMD_NONE, CMD_SHORT, CMD_LONG, CMD_DOUBLE };
Cmd lastCmd = CMD_NONE;

// session counters (for telemetry / ADAPT covariate logging on host)
uint16_t attemptCount[3] = {0, 0, 0};  // indexed by Cmd-1
uint16_t successCount[3] = {0, 0, 0};
uint16_t falseNegCount = 0;

// ---- 6.4 ADAPT: Remappable Command Table -----------------------------------
enum LogicalCmd : uint8_t { LCMD_SELECT, LCMD_BACK, LCMD_HOME, LCMD_NONE };

// a 'pattern' can be a single Cmd, or a 2-step compound (for migrated commands)
struct PatternMap {
  Cmd primary;     // first required burst pattern
  Cmd secondary;   // CMD_NONE if not a compound pattern
  LogicalCmd logical;
} activeMap[3] = {
  { CMD_SHORT,  CMD_NONE, LCMD_SELECT },
  { CMD_LONG,   CMD_NONE, LCMD_HOME   },
  { CMD_DOUBLE, CMD_NONE, LCMD_BACK   },
};

bool practiceMode = false;
uint8_t practiceSessionsLeft = 0;

// compound-pattern matcher state (for migrated commands like SHORT_SHORT)
Cmd pendingPrimary = CMD_NONE;
uint32_t pendingPrimaryTime = 0;
const uint16_t COMPOUND_WIN_MS = 700;

// ---- forward declarations (so a plain C++ compiler is happy about order) ---
void fsmStep();
LogicalCmd resolveLogical(Cmd detected, uint32_t now);
LogicalCmd parseLogical(String t);
Cmd parseCmdName(String t);
void parseRemapCommand(String line);
void runCalibration();

// ---- 6.5 Core EMG Processing (unchanged from SIGNAL) -----------------------
inline void updateEMG(int raw) {
  emgDC += 0.0008f * (raw - emgDC);
  float x = raw - emgDC;
  emgMS += 0.01f * (x * x - emgMS);
  emgRMS = sqrtf(emgMS);
  tb2 = tb1; tb1 = tb0; tb0 = (int)x;
  float psi = (float)tb1 * tb1 - (float)tb0 * tb2;
  if (psi < 0) psi = 0;
  tkeoEnv += 0.03f * (psi - tkeoEnv);
}
inline bool onsetFired() { return tkeoEnv > P.tkOnset; }
inline bool belowFloor() { return emgRMS < P.emgBase * 1.5f; }

// ---- 6.6 setup() -----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  lever.attach(PIN_SERVO); lever.write(LEVER_REST);
  pinMode(PIN_HAPTIC, OUTPUT); analogWrite(PIN_HAPTIC, 0);
  pinMode(PIN_RELAY, OUTPUT); digitalWrite(PIN_RELAY, LOW);
  Serial.println(F("SIGNAL+ADAPT ready. 'c'=calibrate."));
}

// ---- 6.8 ADAPT: Logical-Command Resolution (defined before fsmStep uses it)-
// resolves a detected burst pattern into a logical command, handling both
// simple (single-pattern) and compound (migrated, 2-pattern) maps
LogicalCmd resolveLogical(Cmd detected, uint32_t now) {
  for (int i = 0; i < 3; i++) {
    PatternMap &m = activeMap[i];
    if (m.secondary == CMD_NONE) {
      // simple mapping
      if (m.primary == detected) return m.logical;
    } else {
      // compound mapping: needs primary THEN secondary within window
      if (pendingPrimary == CMD_NONE) {
        if (m.primary == detected) {
          pendingPrimary = detected; pendingPrimaryTime = now;
          return LCMD_NONE;  // waiting for second half
        }
      } else if (now - pendingPrimaryTime < COMPOUND_WIN_MS) {
        if (m.primary == pendingPrimary && m.secondary == detected) {
          pendingPrimary = CMD_NONE;
          return m.logical;
        }
      } else {
        pendingPrimary = CMD_NONE;  // window expired, reset
      }
    }
  }
  return LCMD_NONE;
}

// ---- 6.7 + 6.9 Burst FSM (IDLE..CLASSIFY then ACTUATE..LOCKOUT) -------------
void fsmStep() {
  uint32_t now = millis();
  switch (state) {
    case S_IDLE:
      if (onsetFired()) { tBurstStart = now; burstCount = 1; state = S_BURST; }
      break;
    case S_BURST:
      if (belowFloor() && (now - tBurstStart) > 20) {
        tBurstEnd = now; state = S_CLASSIFY;
      }
      break;
    case S_CLASSIFY: {
      uint16_t dur = tBurstEnd - tBurstStart;
      if (now - tBurstEnd < P.doubleWinMs && onsetFired()) {
        burstCount++; tBurstStart = now; state = S_BURST; break;
      }
      if (burstCount >= 2) { lastCmd = CMD_DOUBLE; }
      else if (dur >= P.longMinMs) { lastCmd = CMD_LONG; }
      else if (dur <= P.shortMaxMs) { lastCmd = CMD_SHORT; }
      else { lastCmd = CMD_NONE; falseNegCount++; }
      for (int i = 3; i > 0; i--) burstTimes[i] = burstTimes[i - 1];
      burstTimes[0] = now;
      if (now - burstTimes[3] < P.spasmWinMs && burstTimes[3] != 0) {
        state = S_LOCKOUT; tBurstStart = now; break;
      }
      state = (lastCmd != CMD_NONE) ? S_ACTUATE : S_IDLE;
      burstCount = 0;
      break; }
    case S_ACTUATE: {
      LogicalCmd lc = resolveLogical(lastCmd, now);
      if (lc != LCMD_NONE) {
        if (lc == LCMD_BACK && driveMode == RELAY_MODE)
        { digitalWrite(PIN_RELAY, HIGH); }
        lever.write(LEVER_PRESS);
        if (now - tBurstEnd > P.pressMs) {
          lever.write(LEVER_REST);
          digitalWrite(PIN_RELAY, LOW);
          state = S_CONFIRM; tBurstEnd = now;
        }
      } else {
        // pattern didn't complete a logical command yet (e.g. mid-compound)
        // or matched nothing -- return to idle without actuating
        state = S_IDLE;
      }
      break; }
    case S_CONFIRM:
      analogWrite(PIN_HAPTIC, 180);
      if (now - tBurstEnd > 80) { analogWrite(PIN_HAPTIC, 0); state = S_IDLE; }
      break;
    case S_LOCKOUT:
      if (now - tBurstStart > 2000) { state = S_IDLE; }
      break;
  }
}

// ---- 6.10 ADAPT: Serial Remap Parser ---------------------------------------
LogicalCmd parseLogical(String t) {
  if (t == "SELECT") return LCMD_SELECT;
  if (t == "HOME") return LCMD_HOME;
  if (t == "BACK") return LCMD_BACK;
  return LCMD_NONE;
}
Cmd parseCmdName(String t) {
  if (t == "SHORT") return CMD_SHORT;
  if (t == "LONG") return CMD_LONG;
  if (t == "DOUBLE") return CMD_DOUBLE;
  return CMD_NONE;
}
// format from host: REMAP,<flagged_logical>,<PATTERN1>[_<PATTERN2>]
// e.g. REMAP,HOME,SHORT_SHORT
void parseRemapCommand(String line) {
  int c1 = line.indexOf(',');
  int c2 = line.indexOf(',', c1 + 1);
  String flagged = line.substring(c1 + 1, c2);
  String newPat = line.substring(c2 + 1);
  LogicalCmd target = parseLogical(flagged);
  int us = newPat.indexOf('_');
  Cmd p1, p2;
  if (us == -1) { p1 = parseCmdName(newPat); p2 = CMD_NONE; }
  else {
    p1 = parseCmdName(newPat.substring(0, us));
    p2 = parseCmdName(newPat.substring(us + 1));
  }
  for (int i = 0; i < 3; i++) {
    if (activeMap[i].logical == target) {
      activeMap[i].primary = p1;
      activeMap[i].secondary = p2;
    }
  }
  practiceMode = true; practiceSessionsLeft = 5;
  Serial.print(F("REMAPPED ")); Serial.println(flagged);
}

// ---- 6.11 Calibration ------------------------------------------------------
float measureRMS(uint16_t ms) {
  uint32_t t0 = millis(); float acc = 0; uint32_t n = 0;
  while (millis() - t0 < ms) { updateEMG(analogRead(PIN_EMG)); acc += emgRMS; n++; delay(1); }
  return acc / max((uint32_t)1, n);
}
void runCalibration() {
  Serial.println(F("CAL relax 3s")); delay(800); P.emgBase = measureRMS(3000);
  Serial.println(F("CAL twitch x3")); delay(800);
  P.tkOnset = tkeoEnv * 0.6f + 3000.0f;
  Serial.println(F("CAL done."));
}

// ---- 6.12 Main Loop -- Sampling, Telemetry, Serial Commands ----------------
void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    if (line == "c") runCalibration();
    else if (line.startsWith("REMAP,")) parseRemapCommand(line);
  }
  uint32_t nowUs = micros();
  if (nowUs - lastSampleUs < SAMPLE_US) return;
  lastSampleUs = nowUs;
  updateEMG(analogRead(PIN_EMG));
  fsmStep();
  static uint8_t dec = 0;
  if (++dec >= 10) { dec = 0;
    // telemetry row consumed by host for ADAPT covariate logging:
    // t_ms, emgRMS, tkeoEnv, state, lastCmd, falseNegCount
    Serial.print(millis()); Serial.print(',');
    Serial.print(emgRMS, 1); Serial.print(',');
    Serial.print(tkeoEnv, 0); Serial.print(',');
    Serial.print(state); Serial.print(',');
    Serial.print(lastCmd); Serial.print(',');
    Serial.println(falseNegCount);
  }
}

/* ============================================================
   PREHEND  —  predictive, self-protecting grasp controller
   Target : Arduino UNO R4 (Minima / WiFi)   [classic Uno also OK]
   Stack  : Muscle Shield sEMG (A0) | EXG Pill (A1) | FSR (A2)
            Servo Claw (D9) | coin motor via NPN (D6)
   Note   : entire control loop runs ON DEVICE. Host optional.

   Key decision: the patent's EEG/BP trigger is replaced by an
   EMG-onset trigger (TKEO) so the predictive cascade is reliable
   and fully on-device. EEG/BP survives only as an optional host
   tier (see host/bp_trigger.py); EMG still gates COMMIT.
   ============================================================ */
#include <Servo.h>
#include <EEPROM.h>

/* ---- pins ---- */
const uint8_t PIN_EMG=A0, PIN_AUX=A1, PIN_FSR=A2;   // analog in
const uint8_t PIN_SERVO=9, PIN_HAPTIC=6;            // PWM out
const uint8_t PIN_LED=LED_BUILTIN;

/* ---- A1 role: set to match how you wired the EXG Pill ---- */
enum AuxMode : uint8_t { AUX_EXTENSOR, AUX_ECG, AUX_EEG };
AuxMode auxMode = AUX_EXTENSOR;

/* ---- sampling ---- */
const uint16_t FS_HZ = 1000;
const uint32_t SAMPLE_US = 1000000UL / FS_HZ;
const float    DT = 1.0f / FS_HZ;
uint32_t lastSampleUs = 0;

/* ---- EEPROM magic byte ---- */
const uint8_t EEPROM_MAGIC = 0xA7;     // version tag; change to invalidate old saves
const int     EEPROM_ADDR  = 0;        // start address

/* ---- non-blocking serial command buffer ---- */
char   serialBuf[24];
uint8_t serialIdx = 0;

/* ---- ADC resolution: UNO R4 is 14-bit; classic Uno is 10-bit ---- */
#if defined(ARDUINO_UNOR4_MINIMA) || defined(ARDUINO_UNOR4_WIFI)
  #define PREHEND_ADC_BITS 14
  const int ADC_MID = 8192;          // 14-bit mid-scale
#else
  #define PREHEND_ADC_BITS 10
  const int ADC_MID = 512;           // 10-bit mid-scale (classic Uno)
#endif

/* ---- servo geometry ---- */
const int CLAW_OPEN=0, CLAW_MAXPRE=70, CLAW_FULL=160, SERVO_RATE=6;
Servo claw; int curAngle=0, tgtAngle=0;

/* ---- signal state ---- */
float emgDC=ADC_MID, emgMS=0, emgRMS=0;  int tb0=0,tb1=0,tb2=0;
float tkeoEnv=0;
float extDC=ADC_MID, extMS=0, extRMS=0;
float fsr=0, fsrLP=0, fsrPrev=0, dFdt=0;
float ecgHP=0, ecgPrev=0; uint32_t lastBeatMs=0;
float bpm=0, rr=0, rrAvg=800; bool exertionOK=true;

/* ---- calibrated parameters (filled by runCalibration) ---- */
struct Params {
  float emgBase=0.02f;     // resting RMS
  float mvc=1.0f;          // max voluntary contraction RMS
  float tkOnset=8000.0f;   // TKEO onset trigger (fast, sensitive)
  float rmsCommit=0.35f;   // confirm: fraction of MVC (normalised)
  float extOpen=0.30f;     // extensor 'open' threshold (RMS)
  float slipTh=8.0f;       // |dF/dt| slip threshold
  float fsrFloor=200.0f;   // 'contact' floor (ADC counts)
  float kSlip=0.5f;        // slip -> boost gain
  uint16_t tAbortMs=400;   // confirmation window
  uint16_t tHoldMs=150;    // minimum hold
  uint8_t  hrLo=45, hrHi=140;   // exertion bounds (bpm)
} P;

/* ---- finite state machine ---- */
enum State : uint8_t { S_IDLE,S_ARMED,S_PREPOS,S_COMMIT,
                       S_HOLD,S_RELEASE,S_ABORT,S_LOCKOUT };
State state = S_IDLE;
uint32_t tStateMs=0, tPreMs=0, tCommitMs=0;
float conf = 0.0f;       // onset confidence 0..1

/* ============================================================
   SIGNAL PROCESSING HELPERS
   ============================================================ */

/* flexor EMG: DC-block + RMS envelope + TKEO onset energy */
inline float updateEMG(int raw){
  emgDC += 0.0008f*(raw-emgDC);
  float x = raw-emgDC;
  emgMS += 0.01f*(x*x-emgMS);
  emgRMS = sqrtf(emgMS);
  tb2=tb1; tb1=tb0; tb0=(int)x;                 // 3-sample buffer
  float psi = (float)tb1*tb1 - (float)tb0*tb2;  // Teager-Kaiser
  if(psi<0) psi=0;
  tkeoEnv += 0.03f*(psi-tkeoEnv);
  return emgRMS;
}

/* extensor EMG (A1 in EXTENSOR mode): envelope for 'open' */
inline float updateEXT(int raw){
  extDC += 0.0008f*(raw-extDC);
  float x = raw-extDC;
  extMS += 0.01f*(x*x-extMS);
  extRMS = sqrtf(extMS);
  return extRMS;
}

/* FSR: low-pass + derivative for slip */
inline void updateFSR(int raw){
  fsr = raw;
  fsrLP += 0.15f*(fsr-fsrLP);
  dFdt = (fsrLP-fsrPrev)/DT;
  fsrPrev = fsrLP;
}

/* ECG (A1 in ECG mode): R-peak -> heart rate -> exertion gate */
inline void updateECG(int raw){
  float hp = raw - ecgPrev + 0.97f*ecgHP;       // 1st-order high-pass
  ecgHP = hp; ecgPrev = raw;
  static float thr=1500.0f; static bool refr=false; static uint32_t tr=0;
  uint32_t now = millis();
  if(!refr && hp>thr){                          // detected beat
    if(lastBeatMs){ rr=now-lastBeatMs; rrAvg+=0.2f*(rr-rrAvg); bpm=60000.0f/rrAvg; }
    lastBeatMs=now; refr=true; tr=now;
  }
  if(refr && now-tr>250) refr=false;            // 250 ms refractory
  exertionOK = (bpm==0) || (bpm>=P.hrLo && bpm<=P.hrHi);
}

/* normalised feature helpers */
inline float emgNorm(){ return (emgRMS-P.emgBase)/(P.mvc-P.emgBase+1e-6f); }
inline bool  onsetFired(){ return tkeoEnv>P.tkOnset && emgNorm()>0.05f; }
inline bool  rmsConfirm(){ return emgNorm()>P.rmsCommit; }
inline bool  openIntent(){
  return (auxMode==AUX_EXTENSOR) ? (extRMS>P.extOpen) : (emgNorm()<0.05f);
}

/* ============================================================
   CALIBRATION (call by sending 'c' over serial)
   ============================================================ */
float measureRMS(uint16_t ms){
  uint32_t t0=millis(); float acc=0; uint32_t n=0;
  while(millis()-t0<ms){ updateEMG(analogRead(PIN_EMG)); acc+=emgRMS; n++; delay(1); }
  return acc/max((uint32_t)1,n);
}

void runCalibration(){
  Serial.println(F("CAL relax 3s"));  delay(800); P.emgBase=measureRMS(3000);
  Serial.println(F("CAL clench 3s")); delay(800); P.mvc=measureRMS(3000);
  P.tkOnset = tkeoEnv*0.6f;            // onset = fraction of clench energy
  float f=0; for(int i=0;i<200;i++){ f+=analogRead(PIN_FSR); delay(2);} P.fsrFloor=f/200+60;
  if(auxMode==AUX_EXTENSOR){
    Serial.println(F("CAL open 3s")); delay(800);
    float e=0; uint32_t t0=millis(),n=0;
    while(millis()-t0<3000){ updateEXT(analogRead(PIN_AUX)); e+=extRMS; n++; delay(1);}
    P.extOpen=(e/n)*0.5f;
  }
  Serial.print(F("CAL done base=")); Serial.print(P.emgBase);
  Serial.print(F(" mvc="));  Serial.print(P.mvc);
  Serial.print(F(" tk="));   Serial.print(P.tkOnset);
  Serial.print(F(" floor="));Serial.println(P.fsrFloor);
  saveParamsEEPROM();
}

/* ============================================================
   EEPROM SAVE / LOAD
   ============================================================ */
void saveParamsEEPROM(){
  EEPROM.put(EEPROM_ADDR, EEPROM_MAGIC);
  EEPROM.put(EEPROM_ADDR + (int)sizeof(EEPROM_MAGIC), P);
  Serial.println(F("EEPROM saved"));
}

void loadParamsEEPROM(){
  uint8_t tag;
  EEPROM.get(EEPROM_ADDR, tag);
  if(tag == EEPROM_MAGIC){
    EEPROM.get(EEPROM_ADDR + (int)sizeof(EEPROM_MAGIC), P);
    Serial.println(F("EEPROM loaded"));
  } else {
    Serial.println(F("EEPROM empty, using defaults"));
  }
}

/* ============================================================
   SETUP
   ============================================================ */
void setup(){
  Serial.begin(115200);
#if PREHEND_ADC_BITS == 14
  analogReadResolution(14);              // UNO R4 14-bit ADC
#endif
  pinMode(PIN_HAPTIC,OUTPUT); analogWrite(PIN_HAPTIC,0);
  pinMode(PIN_LED,OUTPUT);
  claw.attach(PIN_SERVO); claw.write(CLAW_OPEN); curAngle=CLAW_OPEN;
  delay(300);
  Serial.println(F("PREHEND ready. Send 'c' to calibrate, 'P' to force pre-position."));
  loadParamsEEPROM();
}

/* ============================================================
   STATE MACHINE
   ============================================================ */
void fsmStep(){
  uint32_t now = millis();

  // global safety: ECG exertion gate forces LOCKOUT from any state
  if(auxMode==AUX_ECG && !exertionOK && state!=S_LOCKOUT){
    state=S_LOCKOUT; tStateMs=now; tgtAngle=CLAW_OPEN; return;
  }

  switch(state){
    case S_IDLE:
      tgtAngle=CLAW_OPEN;
      if(auxMode!=AUX_ECG || exertionOK){ state=S_ARMED; tStateMs=now; }
      break;

    case S_ARMED:
      tgtAngle=CLAW_OPEN;
      if(onsetFired()){                          // fast TKEO onset
        conf = constrain(tkeoEnv/(P.tkOnset*3.0f), 0.1f, 1.0f);
        tPreMs=now; state=S_PREPOS; tStateMs=now;
      }
      break;

    case S_PREPOS:                               // pre-position in EMD window
      tgtAngle = (int)(conf*CLAW_MAXPRE);
      if(rmsConfirm()){ tCommitMs=now; state=S_COMMIT; tStateMs=now; }
      else if(now-tPreMs > P.tAbortMs){ state=S_ABORT; tStateMs=now; }
      break;

    case S_COMMIT: {                            // graded full grip
      float grip = constrain(emgNorm(), 0.0f, 1.0f);
      tgtAngle = (int)(CLAW_MAXPRE + grip*(CLAW_FULL-CLAW_MAXPRE));
      if(now-tCommitMs > P.tHoldMs){ state=S_HOLD; tStateMs=now; }
      break; }

    case S_HOLD: {
      // autonomous slip reflex (concurrent, fast)
      if(dFdt < -P.slipTh || (fsrLP < P.fsrFloor && fsrLP > 1)){
        int boost = (int)constrain(-dFdt*P.kSlip, 5.0f, 30.0f);
        tgtAngle = min(curAngle+boost, CLAW_FULL);
      }
      // voluntary override: open always wins
      if(openIntent()){ state=S_RELEASE; tStateMs=now; }
      break; }

    case S_RELEASE:
      tgtAngle=CLAW_OPEN;
      if(curAngle<=CLAW_OPEN+2){ state=S_IDLE; tStateMs=now; }
      break;

    case S_ABORT:
      tgtAngle=CLAW_OPEN;
      if(curAngle<=CLAW_OPEN+2){ state=S_IDLE; tStateMs=now; }
      break;

    case S_LOCKOUT:
      tgtAngle=CLAW_OPEN;
      if(now-tStateMs>2000 && (auxMode!=AUX_ECG || exertionOK)){
        state=S_IDLE; tStateMs=now;
      }
      break;
  }
}

/* ============================================================
   OUTPUTS (rate-limited servo + haptic)
   ============================================================ */
void driveOutputs(){
  if(tgtAngle>curAngle)      curAngle = min(curAngle+SERVO_RATE, tgtAngle);
  else if(tgtAngle<curAngle) curAngle = max(curAngle-SERVO_RATE, tgtAngle);
  claw.write(constrain(curAngle, CLAW_OPEN, CLAW_FULL));

  int buzz=0;
  if(state==S_COMMIT || state==S_HOLD)
    buzz = (int)constrain(map((long)fsrLP,(long)P.fsrFloor,12000,40,255),0,255);
  analogWrite(PIN_HAPTIC, buzz);

  digitalWrite(PIN_LED, state==S_HOLD);
}

/* ============================================================
   SERIAL COMMAND HANDLER
   'c' -> calibrate ;  'P' -> force early PRE-POSITION (host BP tier)
   ============================================================ */
void handleSerial(){
  while(Serial.available()){
    char ch = Serial.read();
    if(ch=='\n' || ch=='\r'){
      if(serialIdx==0) continue;              // ignore blank lines
      serialBuf[serialIdx] = '\0';            // null-terminate
      processSerialCmd(serialBuf);
      serialIdx = 0;
    } else {
      if(serialIdx < sizeof(serialBuf)-1)
        serialBuf[serialIdx++] = ch;
    }
  }
}

/* ---- process a complete serial command line ---- */
void processSerialCmd(const char* buf){
  char prefix = buf[0];
  const char* val = buf+1;

  if(prefix=='c' && buf[1]=='\0'){
    runCalibration(); return;
  }
  if(prefix=='P' && buf[1]=='\0'){
    // host BP detector requests an early pre-position; EMG must still
    // confirm within tAbortMs or the cascade ABORTs (no grasp on BP alone)
    if(state==S_IDLE || state==S_ARMED){
      conf = 0.3f;                    // default confidence for host trigger
      tPreMs = millis();
      state = S_PREPOS; tStateMs = tPreMs;
    }
    return;
  }
  if(prefix=='S' && buf[1]=='\0'){ saveParamsEEPROM(); return; }
  if(prefix=='L' && buf[1]=='\0'){ loadParamsEEPROM(); return; }
  if(prefix=='?' && buf[1]=='\0'){
    Serial.println(F("--- Params ---"));
    Serial.print(F("rmsCommit=")); Serial.println(P.rmsCommit,4);
    Serial.print(F("slipTh="));    Serial.println(P.slipTh,2);
    Serial.print(F("kSlip="));     Serial.println(P.kSlip,4);
    Serial.print(F("tAbortMs="));  Serial.println(P.tAbortMs);
    Serial.print(F("tHoldMs="));   Serial.println(P.tHoldMs);
    Serial.print(F("extOpen="));   Serial.println(P.extOpen,4);
    Serial.print(F("emgBase="));   Serial.println(P.emgBase,4);
    Serial.print(F("mvc="));       Serial.println(P.mvc,4);
    Serial.print(F("tkOnset="));   Serial.println(P.tkOnset,2);
    Serial.print(F("fsrFloor="));  Serial.println(P.fsrFloor,2);
    Serial.print(F("hrLo="));      Serial.println(P.hrLo);
    Serial.print(F("hrHi="));      Serial.println(P.hrHi);
    Serial.println(F("--------------"));
    return;
  }

  /* runtime parameter tweak: letter + numeric value */
  switch(prefix){
    case 'r': P.rmsCommit = atof(val);
              Serial.print(F("SET rmsCommit=")); Serial.println(P.rmsCommit,4); break;
    case 's': P.slipTh = atof(val);
              Serial.print(F("SET slipTh=")); Serial.println(P.slipTh,2); break;
    case 'k': P.kSlip = atof(val);
              Serial.print(F("SET kSlip=")); Serial.println(P.kSlip,4); break;
    case 'a': P.tAbortMs = (uint16_t)atoi(val);
              Serial.print(F("SET tAbortMs=")); Serial.println(P.tAbortMs); break;
    case 'h': P.tHoldMs = (uint16_t)atoi(val);
              Serial.print(F("SET tHoldMs=")); Serial.println(P.tHoldMs); break;
    case 'e': P.extOpen = atof(val);
              Serial.print(F("SET extOpen=")); Serial.println(P.extOpen,4); break;
    default:
              Serial.print(F("ERR unknown cmd: ")); Serial.println(buf); break;
  }
}

/* ============================================================
   MAIN LOOP + TELEMETRY
   ============================================================ */
void loop(){
  handleSerial();

  uint32_t nowUs = micros();
  if(nowUs-lastSampleUs < SAMPLE_US) return;   // fixed 1 kHz cadence
  lastSampleUs = nowUs;

  int rEMG=analogRead(PIN_EMG), rAUX=analogRead(PIN_AUX), rFSR=analogRead(PIN_FSR);
  updateEMG(rEMG);
  if(auxMode==AUX_EXTENSOR) updateEXT(rAUX);
  else if(auxMode==AUX_ECG) updateECG(rAUX);
  // AUX_EEG: BP handled by optional host (see host/bp_trigger.py)
  updateFSR(rFSR);

  fsmStep();
  driveOutputs();

  // decimated telemetry (every 10th sample) so serial never stalls the loop
  static uint8_t dec=0;
  if(++dec>=10){ dec=0;
    Serial.print(millis());     Serial.print(',');
    Serial.print(emgNorm(),3);  Serial.print(',');
    Serial.print(tkeoEnv,0);    Serial.print(',');
    Serial.print(fsrLP,0);      Serial.print(',');
    Serial.print(bpm,0);        Serial.print(',');
    Serial.println(state);
  }
}

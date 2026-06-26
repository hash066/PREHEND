/* ============================================================
   PREHEND  —  predictive, self-protecting grasp controller
   Target : Arduino UNO R4 (Minima / WiFi)   [classic Uno also OK]
   Stack  : Muscle Shield sEMG (A0) | EXG Pill (A1) | FSR (A2)
            Servo Claw (D9) | coin motor via NPN (D6) | MPU6050 (SDA/SCL)
   Note   : entire control loop runs ON DEVICE. Host optional.

   Key decision: the patent's EEG/BP trigger is replaced by an
   EMG-onset trigger (TKEO) so the predictive cascade is reliable
   and fully on-device. EEG/BP survives only as an optional host
   tier (see host/bp_trigger.py); EMG still gates COMMIT.

   NEW: MPU6050 IMU for head-gesture detection; EOG mode on A1 for
   eye-blink AAC input; mode switching (M0/M1/M2) for dual-mode
   grasp+communication operation (PREHEND-SPEAK).
   ============================================================ */
#include <Servo.h>
#include <EEPROM.h>
#include <Wire.h>

/* ---- pins ---- */
const uint8_t PIN_EMG=A0, PIN_AUX=A1, PIN_FSR=A2;
const uint8_t PIN_SERVO=9, PIN_HAPTIC=6;
const uint8_t PIN_LED=LED_BUILTIN;

/* ---- A1 role ---- */
enum AuxMode : uint8_t {
  AUX_EXTENSOR,     // extensor EMG for voluntary release
  AUX_ECG,          // ECG lead-I for exertion gate
  AUX_EEG,          // legacy alias → raw passthrough (host bp_trigger.py)
  AUX_EOG,          // eye-blink / saccade detection for AAC input
  AUX_EEG_STREAM    // raw EEG passthrough for host eeg_viz.py
};
AuxMode auxMode = AUX_EXTENSOR;

/* ---- system mode (host-controlled via M0/M1/M2) ----
   0 = GRASP      normal FSM runs
   1 = SPEAK      claw locked open; gestures route to AAC host
   2 = EEG_STREAM same as SPEAK, raw EEG passthrough          */
uint8_t systemMode = 0;

/* ---- sampling ---- */
const uint16_t FS_HZ = 1000;
const uint32_t SAMPLE_US = 1000000UL / FS_HZ;
const float    DT = 1.0f / FS_HZ;
uint32_t lastSampleUs = 0;

/* ---- EEPROM ---- */
const uint8_t EEPROM_MAGIC = 0xA7;
const int     EEPROM_ADDR  = 0;

/* ---- serial command buffer ---- */
char    serialBuf[24];
uint8_t serialIdx = 0;

/* ---- ADC resolution ---- */
#if defined(ARDUINO_UNOR4_MINIMA) || defined(ARDUINO_UNOR4_WIFI)
  #define PREHEND_ADC_BITS 14
  const int ADC_MID = 8192;
#else
  #define PREHEND_ADC_BITS 10
  const int ADC_MID = 512;
#endif

/* ---- servo geometry ---- */
const int CLAW_OPEN=0, CLAW_MAXPRE=70, CLAW_FULL=160, SERVO_RATE=6;
Servo claw; int curAngle=0, tgtAngle=0;

/* ---- EMG / FSR / ECG signal state ---- */
float emgDC=ADC_MID, emgMS=0, emgRMS=0;  int tb0=0,tb1=0,tb2=0;
float tkeoEnv=0;
float extDC=ADC_MID, extMS=0, extRMS=0;
float fsr=0, fsrLP=0, fsrPrev=0, dFdt=0;
float ecgHP=0, ecgPrev=0; uint32_t lastBeatMs=0;
float bpm=0, rr=0, rrAvg=800; bool exertionOK=true;

/* ---- EOG signal state ---- */
float   eogDC = ADC_MID;
int16_t eogFiltered = 0;   // DC-blocked A1 (used by EOG and EEG_STREAM modes)
uint8_t eogEvent = 0;      // 0=none 1=SHORT 2=LONG(=SELECT) 3=SACC_R 4=SACC_L

/* ---- IMU (MPU6050 @ I2C 0x68) ---- */
bool     imuPresent  = false;
int16_t  imuAccel[3] = {0, 0, 0};  // raw X Y Z (±2g, 16384 LSB/g)
uint8_t  imuGesture  = 0;          // 0=none 1=NOD 2=SHAKE 3=TILT
uint16_t imuPacked   = 0;          // high byte=aY/128+128, low byte=aX/128+128

/* ---- packed gesture byte for telemetry ----
   bits 3-0 = eogEvent, bits 5-4 = imuGesture          */
uint8_t gestureByte = 0;

/* ---- calibrated parameters ---- */
struct Params {
  float emgBase=0.02f;
  float mvc=1.0f;
  float tkOnset=8000.0f;
  float rmsCommit=0.35f;
  float extOpen=0.30f;
  float slipTh=8.0f;
  float fsrFloor=200.0f;
  float kSlip=0.5f;
  uint16_t tAbortMs=400;
  uint16_t tHoldMs=150;
  uint8_t  hrLo=45, hrHi=140;
} P;

/* ---- FSM ---- */
enum State : uint8_t { S_IDLE,S_ARMED,S_PREPOS,S_COMMIT,
                       S_HOLD,S_RELEASE,S_ABORT,S_LOCKOUT };
State state = S_IDLE;
uint32_t tStateMs=0, tPreMs=0, tCommitMs=0;
float conf = 0.0f;

/* ============================================================
   SIGNAL PROCESSING
   ============================================================ */

inline float updateEMG(int raw){
  emgDC += 0.0008f*(raw-emgDC);
  float x = raw-emgDC;
  emgMS += 0.01f*(x*x-emgMS);
  emgRMS = sqrtf(emgMS);
  tb2=tb1; tb1=tb0; tb0=(int)x;
  float psi = (float)tb1*tb1 - (float)tb0*tb2;
  if(psi<0) psi=0;
  tkeoEnv += 0.03f*(psi-tkeoEnv);
  return emgRMS;
}

inline float updateEXT(int raw){
  extDC += 0.0008f*(raw-extDC);
  float x = raw-extDC;
  extMS += 0.01f*(x*x-extMS);
  extRMS = sqrtf(extMS);
  return extRMS;
}

inline void updateFSR(int raw){
  fsr = raw;
  fsrLP += 0.15f*(fsr-fsrLP);
  dFdt = (fsrLP-fsrPrev)/DT;
  fsrPrev = fsrLP;
}

inline void updateECG(int raw){
  float hp = raw - ecgPrev + 0.97f*ecgHP;
  ecgHP = hp; ecgPrev = raw;
  static float thr=1500.0f; static bool refr=false; static uint32_t tr=0;
  uint32_t now = millis();
  if(!refr && hp>thr){
    if(lastBeatMs){ rr=now-lastBeatMs; rrAvg+=0.2f*(rr-rrAvg); bpm=60000.0f/rrAvg; }
    lastBeatMs=now; refr=true; tr=now;
  }
  if(refr && now-tr>250) refr=false;
  exertionOK = (bpm==0) || (bpm>=P.hrLo && bpm<=P.hrHi);
}

/* EOG: DC-block + blink-duration state machine */
inline void updateEOG(int raw){
  eogDC += 0.0008f*(raw-eogDC);
  int16_t x = (int16_t)(raw - eogDC);
  eogFiltered = x;

  const int16_t BLINK_THR  = 1500;   // ~90 mV corneoretinal swing (14-bit)
  const int16_t SACC_THR   = 400;    // slower horizontal saccade drift
  const uint32_t LONG_MIN  = 200;    // ms: minimum "sustained" blink
  const uint32_t LONG_MAX  = 800;    // ms: beyond this = eye closed, ignore

  static uint8_t  bState   = 0;
  static uint32_t bStartMs = 0;
  eogEvent = 0;

  uint32_t now = millis();
  switch(bState){
    case 0:   // idle
      if(x > BLINK_THR){ bState=1; bStartMs=now; }
      else if(x < -BLINK_THR){ eogEvent=4; }   // saccade left
      else if(x >  SACC_THR) { eogEvent=3; }   // saccade right
      break;
    case 1:   // above threshold
      if(x < BLINK_THR){
        uint32_t dur = now - bStartMs;
        if(dur < LONG_MIN)                         eogEvent=1;  // short blink → NEXT
        else if(dur < LONG_MAX)                    eogEvent=2;  // long blink  → SELECT
        bState=0;
      } else if(now-bStartMs >= LONG_MAX){
        bState=2;                                  // held too long → sustained close
      }
      break;
    case 2:   // sustained eye-close, wait for return
      if(x < BLINK_THR) bState=0;
      break;
  }
}

/* ---- normalised feature helpers ---- */
inline float emgNorm(){ return (emgRMS-P.emgBase)/(P.mvc-P.emgBase+1e-6f); }
inline bool  onsetFired(){ return tkeoEnv>P.tkOnset && emgNorm()>0.05f; }
inline bool  rmsConfirm(){ return emgNorm()>P.rmsCommit; }
inline bool  openIntent(){
  return (auxMode==AUX_EXTENSOR) ? (extRMS>P.extOpen) : (emgNorm()<0.05f);
}

/* ============================================================
   MPU6050 (I2C 0x68)
   ============================================================ */
static void imuWriteReg(uint8_t reg, uint8_t val){
  Wire.beginTransmission(0x68);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

void imuSetup(){
  Wire.begin();
  // Probe: WHO_AM_I (0x75) should return 0x68
  Wire.beginTransmission(0x68);
  Wire.write(0x75);
  if(Wire.endTransmission(false) != 0){ return; }
  Wire.requestFrom((uint8_t)0x68, (uint8_t)1);
  if(!Wire.available() || Wire.read() != 0x68){ return; }

  imuWriteReg(0x6B, 0x01);   // PWR_MGMT_1: wake, PLL x-gyro clock
  imuWriteReg(0x1A, 0x03);   // CONFIG: DLPF 44 Hz accel bandwidth
  imuWriteReg(0x1C, 0x00);   // ACCEL_CONFIG: ±2g, 16384 LSB/g
  imuWriteReg(0x19, 0x09);   // SMPLRT_DIV: output rate = 1kHz/10 = 100 Hz
  imuWriteReg(0x38, 0x01);   // INT_ENABLE: DATA_RDY_EN
  imuPresent = true;
  Serial.println(F("IMU OK"));
}

/* Non-blocking read: called every 10 ms inside the decimate gate (~700 µs) */
void updateIMU(){
  if(!imuPresent) return;
  Wire.beginTransmission(0x68);
  Wire.write(0x3B);           // ACCEL_XOUT_H
  Wire.endTransmission(false);
  Wire.requestFrom((uint8_t)0x68, (uint8_t)6);
  if(Wire.available() < 6) return;
  imuAccel[0] = (int16_t)((Wire.read()<<8) | Wire.read());
  imuAccel[1] = (int16_t)((Wire.read()<<8) | Wire.read());
  imuAccel[2] = (int16_t)((Wire.read()<<8) | Wire.read());

  // Pack for telemetry: ÷128 → ±128 range; offset by 128 for unsigned packing
  imuPacked = ((uint16_t)(uint8_t)((int8_t)constrain(imuAccel[1]/128,-127,127)+128) << 8)
            |  (uint8_t)((int8_t)constrain(imuAccel[0]/128,-127,127)+128);

  detectIMUGesture();
}

void detectIMUGesture(){
  const int16_t  NOD_THR  = 6000;   // ~0.37 g
  const int16_t  SHK_THR  = 6000;
  const int16_t  TLT_THR  = 4000;   // ~0.24 g
  const uint32_t GMAX_MS  = 600;
  const uint32_t TILT_MS  = 800;

  static uint8_t  nodSt=0; static uint32_t nodT=0;
  static uint8_t  shkSt=0; static uint32_t shkT=0; static int8_t shkDir=0;
  static uint32_t tiltT=0;

  uint32_t now = millis();
  imuGesture = 0;

  /* head nod (Y-axis peak → return) */
  switch(nodSt){
    case 0: if(imuAccel[1]> NOD_THR){ nodSt=1; nodT=now; } break;
    case 1:
      if(imuAccel[1] < NOD_THR/2){
        if(now-nodT < GMAX_MS) imuGesture=1;  // NOD
        nodSt=0;
      } else if(now-nodT > GMAX_MS) nodSt=0;
      break;
  }

  /* head shake (X-axis signed peak then opposite) */
  switch(shkSt){
    case 0:
      if(imuAccel[0]> SHK_THR){ shkSt=1; shkT=now; shkDir= 1; }
      else if(imuAccel[0]<-SHK_THR){ shkSt=1; shkT=now; shkDir=-1; }
      break;
    case 1:
      if((shkDir>0 && imuAccel[0]<-SHK_THR/2) ||
         (shkDir<0 && imuAccel[0]> SHK_THR/2)){
        if(now-shkT < GMAX_MS) imuGesture=2; // SHAKE
        shkSt=0;
      } else if(now-shkT > GMAX_MS) shkSt=0;
      break;
  }

  /* sustained tilt (X-axis > TLT_THR for > TILT_MS) */
  if(abs(imuAccel[0]) > TLT_THR){
    if(tiltT==0) tiltT=now;
    else if(now-tiltT > TILT_MS && imuGesture==0) imuGesture=3; // TILT
  } else { tiltT=0; }
}

/* ============================================================
   CALIBRATION
   ============================================================ */
float measureRMS(uint16_t ms){
  uint32_t t0=millis(); float acc=0; uint32_t n=0;
  while(millis()-t0<ms){ updateEMG(analogRead(PIN_EMG)); acc+=emgRMS; n++; delay(1); }
  return acc/max((uint32_t)1,n);
}

void runCalibration(){
  Serial.println(F("CAL relax 3s"));  delay(800); P.emgBase=measureRMS(3000);
  Serial.println(F("CAL clench 3s")); delay(800); P.mvc=measureRMS(3000);
  P.tkOnset = tkeoEnv*0.6f;
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
   EEPROM
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
  analogReadResolution(14);
#endif
  pinMode(PIN_HAPTIC,OUTPUT); analogWrite(PIN_HAPTIC,0);
  pinMode(PIN_LED,OUTPUT);
  claw.attach(PIN_SERVO); claw.write(CLAW_OPEN); curAngle=CLAW_OPEN;
  imuSetup();
  delay(300);
  Serial.println(F("PREHEND ready. c=cal P=prepos M0/M1/M2=mode ?=params"));
  loadParamsEEPROM();
}

/* ============================================================
   STATE MACHINE
   ============================================================ */
void fsmStep(){
  // SPEAK / EEG_STREAM: lock claw open, freeze FSM entirely
  if(systemMode != 0){
    tgtAngle = CLAW_OPEN;
    return;
  }

  uint32_t now = millis();

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
      if(onsetFired()){
        conf = constrain(tkeoEnv/(P.tkOnset*3.0f), 0.1f, 1.0f);
        tPreMs=now; state=S_PREPOS; tStateMs=now;
      }
      break;

    case S_PREPOS:
      tgtAngle = (int)(conf*CLAW_MAXPRE);
      if(rmsConfirm()){ tCommitMs=now; state=S_COMMIT; tStateMs=now; }
      else if(now-tPreMs > P.tAbortMs){ state=S_ABORT; tStateMs=now; }
      break;

    case S_COMMIT: {
      float grip = constrain(emgNorm(), 0.0f, 1.0f);
      tgtAngle = (int)(CLAW_MAXPRE + grip*(CLAW_FULL-CLAW_MAXPRE));
      if(now-tCommitMs > P.tHoldMs){ state=S_HOLD; tStateMs=now; }
      break; }

    case S_HOLD: {
      if(dFdt < -P.slipTh || (fsrLP < P.fsrFloor && fsrLP > 1)){
        int boost = (int)constrain(-dFdt*P.kSlip, 5.0f, 30.0f);
        tgtAngle = min(curAngle+boost, CLAW_FULL);
      }
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
   OUTPUTS
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
   ============================================================ */
void handleSerial(){
  while(Serial.available()){
    char ch = Serial.read();
    if(ch=='\n' || ch=='\r'){
      if(serialIdx==0) continue;
      serialBuf[serialIdx] = '\0';
      processSerialCmd(serialBuf);
      serialIdx = 0;
    } else {
      if(serialIdx < sizeof(serialBuf)-1)
        serialBuf[serialIdx++] = ch;
    }
  }
}

void processSerialCmd(const char* buf){
  char prefix = buf[0];
  const char* val = buf+1;

  if(prefix=='c' && buf[1]=='\0'){ runCalibration(); return; }
  if(prefix=='P' && buf[1]=='\0'){
    if(state==S_IDLE || state==S_ARMED){
      conf=0.3f; tPreMs=millis(); state=S_PREPOS; tStateMs=tPreMs;
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
    Serial.print(F("mode="));      Serial.println(systemMode);
    Serial.print(F("imu="));       Serial.println(imuPresent ? F("OK") : F("absent"));
    Serial.println(F("--------------"));
    return;
  }

  /* mode switching: M0 = GRASP, M1 = SPEAK, M2 = EEG_STREAM */
  if(prefix=='M'){
    uint8_t m = (uint8_t)atoi(val);
    if(m <= 2){ systemMode=m; Serial.print(F("MODE ")); Serial.println(systemMode); }
    return;
  }

  /* runtime parameter tweaks */
  switch(prefix){
    case 'r': P.rmsCommit=atof(val);
              Serial.print(F("SET rmsCommit=")); Serial.println(P.rmsCommit,4); break;
    case 's': P.slipTh=atof(val);
              Serial.print(F("SET slipTh=")); Serial.println(P.slipTh,2); break;
    case 'k': P.kSlip=atof(val);
              Serial.print(F("SET kSlip=")); Serial.println(P.kSlip,4); break;
    case 'a': P.tAbortMs=(uint16_t)atoi(val);
              Serial.print(F("SET tAbortMs=")); Serial.println(P.tAbortMs); break;
    case 'h': P.tHoldMs=(uint16_t)atoi(val);
              Serial.print(F("SET tHoldMs=")); Serial.println(P.tHoldMs); break;
    case 'e': P.extOpen=atof(val);
              Serial.print(F("SET extOpen=")); Serial.println(P.extOpen,4); break;
    default:  Serial.print(F("ERR unknown cmd: ")); Serial.println(buf); break;
  }
}

/* ============================================================
   MAIN LOOP + TELEMETRY (10 fields)
   ms,emgNorm,tkeo,fsr,bpm,state,auxRaw,imuPacked,gestureByte,mode
   ============================================================ */
void loop(){
  handleSerial();

  uint32_t nowUs = micros();
  if(nowUs-lastSampleUs < SAMPLE_US) return;
  lastSampleUs = nowUs;

  int rEMG=analogRead(PIN_EMG), rAUX=analogRead(PIN_AUX), rFSR=analogRead(PIN_FSR);
  updateEMG(rEMG);

  switch(auxMode){
    case AUX_EXTENSOR:
      updateEXT(rAUX);
      break;
    case AUX_ECG:
      updateECG(rAUX);
      break;
    case AUX_EOG:
      updateEOG(rAUX);
      break;
    case AUX_EEG:
    case AUX_EEG_STREAM:
      // raw passthrough — host handles all processing
      eogDC += 0.0008f*(rAUX-eogDC);
      eogFiltered = (int16_t)(rAUX - eogDC);
      break;
  }

  updateFSR(rFSR);
  fsmStep();
  driveOutputs();

  // decimated telemetry every 10th sample (~100 Hz)
  static uint8_t dec=0;
  if(++dec >= 10){ dec=0;
    // IMU read here: synchronous but only ~700 µs, every 10 ms
    updateIMU();
    // pack single-shot events; reset after packing
    gestureByte = (uint8_t)((eogEvent & 0x0F) | ((imuGesture & 0x03) << 4));
    eogEvent   = 0;
    imuGesture = 0;

    Serial.print(millis());       Serial.print(',');
    Serial.print(emgNorm(),3);    Serial.print(',');
    Serial.print(tkeoEnv,0);      Serial.print(',');
    Serial.print(fsrLP,0);        Serial.print(',');
    Serial.print(bpm,0);          Serial.print(',');
    Serial.print(state);          Serial.print(',');
    Serial.print(eogFiltered);    Serial.print(',');
    Serial.print(imuPacked);      Serial.print(',');
    Serial.print(gestureByte);    Serial.print(',');
    Serial.println(systemMode);
  }
}

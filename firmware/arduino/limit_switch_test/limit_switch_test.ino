// CNC Shield V3 + Arduino Uno
// Stepper X = base, Z = arm1, Y = arm2
// Limits on X-, Z-, Y-
//
// Commands (115200):
// HELP, LIMITS, POS, WATCH ON/OFF
// ENERGIZE / DEENERGIZE
// SPEED vx vy vz
// JOG X 200
// MOVE dx dy dz      (coordinated simultaneous move)
// HOMEX/HOMEY/HOMEZ/HOMEALL
// ESTOP
// CLEAR
//
// Auto-home on boot: YES (simultaneous)

#include <Arduino.h>

// ---------------- Pins (CNC Shield V3 defaults) ----------------
static const uint8_t PIN_EN = 8;

static const uint8_t X_STEP = 2, X_DIR = 5, X_LIM = 9;    // X-
static const uint8_t Y_STEP = 3, Y_DIR = 6, Y_LIM = 10;   // Y-
static const uint8_t Z_STEP = 4, Z_DIR = 7, Z_LIM = 11;   // Z-

// Your confirmed behavior: idle=0, pressed=1
static const uint8_t LIMIT_PRESSED_LEVEL = HIGH;

// Soft limits (pos=0 at home switch; away from home is negative)
static const long X_MIN = -10500;
static const long Z_MIN = -39000;
static const long Y_MIN = -49500;

// Auto-home behavior
static const bool AUTO_HOME_ON_BOOT = true;

struct Axis {
  char name;                 // 'X','Y','Z'
  uint8_t stepPin, dirPin, limPin;
  bool invertToward;         // flip what "positive" means physically
  long posSteps;             // 0 at home; negative away
  long speedSPS;             // steps/sec (cap)
  long minPos;               // negative soft limit
  bool homed;
};

Axis axX = { 'X', X_STEP, X_DIR, X_LIM, false, 0,  800, X_MIN, false };
Axis axY = { 'Y', Y_STEP, Y_DIR, Y_LIM, true,  0, 3000, Y_MIN, false }; // Y inverted (from your test)
Axis axZ = { 'Z', Z_STEP, Z_DIR, Z_LIM, false, 0, 3000, Z_MIN, false };

bool motorsEnabled = false;
bool watchEnabled  = false;
volatile bool estopLatched = false;

uint8_t lastRawX = 0, lastRawY = 0, lastRawZ = 0;
unsigned long lastPosPrintMs = 0;

// ---------------- Helpers ----------------
static inline Axis* axisByName(char c) {
  c = toupper(c);
  if (c == 'X') return &axX;
  if (c == 'Y') return &axY;
  if (c == 'Z') return &axZ;
  return nullptr;
}

static inline uint8_t limRaw(const Axis &a) { return (uint8_t)digitalRead(a.limPin); }
static inline bool limPressed(const Axis &a) { return limRaw(a) == LIMIT_PRESSED_LEVEL; }

void setEnable(bool on) {
  motorsEnabled = on;
  digitalWrite(PIN_EN, on ? LOW : HIGH); // active LOW
}

void estopNow(const char* why) {
  estopLatched = true;
  setEnable(false);
  Serial.print("ESTOP: ");
  Serial.println(why);
}

void printPos() {
  Serial.print("POS X="); Serial.print(axX.posSteps);
  Serial.print(" Y=");    Serial.print(axY.posSteps);
  Serial.print(" Z=");    Serial.print(axZ.posSteps);
  Serial.print(" EN=");   Serial.println(motorsEnabled ? 1 : 0);
}

void maybePrintPosDuringMotion() {
  unsigned long now = millis();
  if (now - lastPosPrintMs >= 50) {  // ~20 Hz updates
    lastPosPrintMs = now;
    printPos();
  }
}

void printLimits() {
  uint8_t rx = limRaw(axX), ry = limRaw(axY), rz = limRaw(axZ);
  Serial.print("LIMITS (raw) X="); Serial.print(rx);
  Serial.print(" Y="); Serial.print(ry);
  Serial.print(" Z="); Serial.println(rz);

  Serial.print("LIMITS (pressed=1) X="); Serial.print(rx == LIMIT_PRESSED_LEVEL ? 1 : 0);
  Serial.print(" Y="); Serial.print(ry == LIMIT_PRESSED_LEVEL ? 1 : 0);
  Serial.print(" Z="); Serial.println(rz == LIMIT_PRESSED_LEVEL ? 1 : 0);
}

void watchLimitsIfEnabled() {
  if (!watchEnabled) return;
  uint8_t rx = limRaw(axX), ry = limRaw(axY), rz = limRaw(axZ);
  if (rx != lastRawX || ry != lastRawY || rz != lastRawZ) {
    lastRawX = rx; lastRawY = ry; lastRawZ = rz;
    printLimits();
  }
}

// Clamp requested steps to stay within [minPos, 0] once homed.
// If NOT homed, allow moving toward home even if pos would go positive.
long clampToSoftLimits(Axis &a, long reqSteps) {
  if (reqSteps == 0) return 0;

  long desired = a.posSteps + reqSteps;

  // Upper clamp only AFTER homing
  if (a.homed && desired > 0) {
    reqSteps = 0 - a.posSteps;
    desired = a.posSteps + reqSteps;
  }

  // Lower clamp always (protects the arm no matter what)
  if (desired < a.minPos) {
    reqSteps = a.minPos - a.posSteps;
    desired = a.posSteps + reqSteps;
  }

  return reqSteps;
}

// ---------------- Serial parsing (ESTOP during motion) ----------------
String readLineNonBlocking() {
  static String line;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r' || c == '\n') {
      if (line.length() == 0) return "";
      String out = line;
      line = "";
      out.trim();
      return out;
    }
    line += c;
    if (line.length() > 120) { line = ""; return ""; }
  }
  return "";
}

void pollSerialDuringMotion() {
  String line = readLineNonBlocking();
  if (!line.length()) return;
  String up = line; up.toUpperCase();
  if (up == "ESTOP") estopNow("manual");
  if (up == "CLEAR") { estopLatched = false; Serial.println("OK ESTOP=0"); }
}

// ---------------- Low-level stepping ----------------
static inline void setDirForToward(Axis &a, bool toward) {
  bool dirLevel = toward;
  if (a.invertToward) dirLevel = !dirLevel;
  digitalWrite(a.dirPin, dirLevel ? HIGH : LOW);
}

static inline void pulseStep(uint8_t stepPin) {
  digitalWrite(stepPin, HIGH);
  delayMicroseconds(2);
  digitalWrite(stepPin, LOW);
}

// ---------------- Auto-home on boot (SIMULTANEOUS) ----------------
void autoHomeBootSimultaneous() {
  if (estopLatched) return;

  Serial.println("AUTO_HOME: begin");

  if (!motorsEnabled) setEnable(true);

  // If already pressed at boot, treat as homed immediately
  if (limPressed(axX)) { axX.posSteps = 0; axX.homed = true; Serial.println("BOOT_HOME X"); }
  if (limPressed(axY)) { axY.posSteps = 0; axY.homed = true; Serial.println("BOOT_HOME Y"); }
  if (limPressed(axZ)) { axZ.posSteps = 0; axZ.homed = true; Serial.println("BOOT_HOME Z"); }

  // Any axis not homed: move toward home until its switch hits
  bool needX = !axX.homed;
  bool needY = !axY.homed;
  bool needZ = !axZ.homed;

  if (!needX && !needY && !needZ) {
    Serial.println("AUTO_HOME: all already homed");
    printPos();
    return;
  }

  if (needX) setDirForToward(axX, true);
  if (needY) setDirForToward(axY, true);
  if (needZ) setDirForToward(axZ, true);

  // Coordinated ticking (1ms)
  const unsigned long tickUs = 1000;
  double accX = 0, accY = 0, accZ = 0;

  // Use each axis speed (sps) for homing approach
  const double vx = (double)axX.speedSPS;
  const double vy = (double)axY.speedSPS;
  const double vz = (double)axZ.speedSPS;

  unsigned long lastTick = micros();
  lastPosPrintMs = millis();

  // Travel cap: prevents endless run if a switch is broken
  const long maxStepsX = labs(axX.minPos) + 5000;
  const long maxStepsY = labs(axY.minPos) + 5000;
  const long maxStepsZ = labs(axZ.minPos) + 5000;
  long movedX = 0, movedY = 0, movedZ = 0;

  while (needX || needY || needZ) {
    if (estopLatched) return;

    while ((micros() - lastTick) < tickUs) {
      pollSerialDuringMotion();
      if (estopLatched) return;
    }
    lastTick = micros();

    // If a limit is hit, mark homed and snap to 0 immediately
    if (needX && limPressed(axX)) {
      needX = false;
      axX.posSteps = 0;
      axX.homed = true;
      Serial.println("HOME_HIT X");
      printPos();
    }
    if (needY && limPressed(axY)) {
      needY = false;
      axY.posSteps = 0;
      axY.homed = true;
      Serial.println("HOME_HIT Y");
      printPos();
    }
    if (needZ && limPressed(axZ)) {
      needZ = false;
      axZ.posSteps = 0;
      axZ.homed = true;
      Serial.println("HOME_HIT Z");
      printPos();
    }

    // Travel cap checks
    if (needX && movedX > maxStepsX) { Serial.println("AUTO_HOME FAIL: X travel cap"); estopNow("home cap"); return; }
    if (needY && movedY > maxStepsY) { Serial.println("AUTO_HOME FAIL: Y travel cap"); estopNow("home cap"); return; }
    if (needZ && movedZ > maxStepsZ) { Serial.println("AUTO_HOME FAIL: Z travel cap"); estopNow("home cap"); return; }

    // Accumulate step fractions
    if (needX) accX += vx * (tickUs / 1000000.0);
    if (needY) accY += vy * (tickUs / 1000000.0);
    if (needZ) accZ += vz * (tickUs / 1000000.0);

    // Step any axis that has >=1 step accumulated
    while (needX && accX >= 1.0) {
      pulseStep(axX.stepPin);
      axX.posSteps += 1; // temporarily positive until we hit switch; then snap to 0
      movedX++;
      accX -= 1.0;
    }
    while (needY && accY >= 1.0) {
      pulseStep(axY.stepPin);
      axY.posSteps += 1;
      movedY++;
      accY -= 1.0;
    }
    while (needZ && accZ >= 1.0) {
      pulseStep(axZ.stepPin);
      axZ.posSteps += 1;
      movedZ++;
      accZ -= 1.0;
    }

    maybePrintPosDuringMotion();
  }

  Serial.println("AUTO_HOME: done");
  printPos();
}

// ---------------- Motion ----------------
void moveAxis(Axis &a, long reqSteps) {
  if (reqSteps == 0 || estopLatched) return;

  // If not homed, block moving away from home (negative) for safety
  if (!a.homed && reqSteps < 0) {
    Serial.print("BLOCKED: "); Serial.print(a.name);
    Serial.println(" not homed; only HOME or positive moves allowed.");
    return;
  }

  reqSteps = clampToSoftLimits(a, reqSteps);
  if (reqSteps == 0) {
    Serial.print("SOFT_LIMIT: "); Serial.print(a.name); Serial.println(" no move.");
    return;
  }

  if (!motorsEnabled) setEnable(true);

  bool toward = (reqSteps > 0);
  long n = labs(reqSteps);

  setDirForToward(a, toward);

  unsigned long stepDelayUs = (unsigned long)(1000000UL / (unsigned long)max(1L, a.speedSPS));
  unsigned long lastStepUs = micros();

  for (long i = 0; i < n; i++) {
    if (estopLatched) return;

    if (toward && limPressed(a)) break;

    while ((micros() - lastStepUs) < stepDelayUs) {
      pollSerialDuringMotion();
      if (estopLatched) return;
    }
    lastStepUs = micros();

    pulseStep(a.stepPin);
    a.posSteps += toward ? +1 : -1;

    maybePrintPosDuringMotion();
  }

  // If we reached the switch during a normal move, snap to homed
  if (toward && limPressed(a)) {
    Serial.print("HOME_HIT ");
    Serial.println(a.name);
    a.posSteps = 0;
    a.homed = true;
  }

  printPos();
}

// Coordinated multi-axis MOVE
void moveXYZ(long dx, long dy, long dz) {
  if (estopLatched) return;

  // Safety: block negative moves on un-homed axes
  if (!axX.homed && dx < 0) { Serial.println("BLOCKED: X not homed for negative move"); dx = 0; }
  if (!axY.homed && dy < 0) { Serial.println("BLOCKED: Y not homed for negative move"); dy = 0; }
  if (!axZ.homed && dz < 0) { Serial.println("BLOCKED: Z not homed for negative move"); dz = 0; }

  dx = clampToSoftLimits(axX, dx);
  dy = clampToSoftLimits(axY, dy);
  dz = clampToSoftLimits(axZ, dz);

  long sx = labs(dx), sy = labs(dy), sz = labs(dz);
  if (sx == 0 && sy == 0 && sz == 0) { Serial.println("SOFT_LIMIT: no MOVE"); return; }

  if (!motorsEnabled) setEnable(true);

  bool tx = (dx > 0), ty = (dy > 0), tz = (dz > 0);
  setDirForToward(axX, tx);
  setDirForToward(axY, ty);
  setDirForToward(axZ, tz);

  double tX = (sx > 0) ? ((double)sx / (double)max(1L, axX.speedSPS)) : 0.0;
  double tY = (sy > 0) ? ((double)sy / (double)max(1L, axY.speedSPS)) : 0.0;
  double tZ = (sz > 0) ? ((double)sz / (double)max(1L, axZ.speedSPS)) : 0.0;
  double T  = max(tX, max(tY, tZ));
  if (T <= 0.0) T = 0.001;

  double vx = (sx > 0) ? (sx / T) : 0.0;
  double vy = (sy > 0) ? (sy / T) : 0.0;
  double vz = (sz > 0) ? (sz / T) : 0.0;

  const unsigned long tickUs = 1000;
  double accX = 0, accY = 0, accZ = 0;
  long remX = sx, remY = sy, remZ = sz;

  unsigned long lastTick = micros();
  lastPosPrintMs = millis();

  while ((remX > 0) || (remY > 0) || (remZ > 0)) {
    if (estopLatched) return;

    while ((micros() - lastTick) < tickUs) {
      pollSerialDuringMotion();
      if (estopLatched) return;
    }
    lastTick = micros();

    if (tx && limPressed(axX)) remX = 0;
    if (ty && limPressed(axY)) remY = 0;
    if (tz && limPressed(axZ)) remZ = 0;

    accX += vx * (tickUs / 1000000.0);
    accY += vy * (tickUs / 1000000.0);
    accZ += vz * (tickUs / 1000000.0);

    while (accX >= 1.0 && remX > 0) { pulseStep(axX.stepPin); axX.posSteps += tx ? +1 : -1; remX--; accX -= 1.0; }
    while (accY >= 1.0 && remY > 0) { pulseStep(axY.stepPin); axY.posSteps += ty ? +1 : -1; remY--; accY -= 1.0; }
    while (accZ >= 1.0 && remZ > 0) { pulseStep(axZ.stepPin); axZ.posSteps += tz ? +1 : -1; remZ--; accZ -= 1.0; }

    maybePrintPosDuringMotion();
  }

  printPos();
}

// Manual HOME commands still exist (sequential), but boot is simultaneous.
void homeAxis(Axis &a) {
  Serial.print("HOME "); Serial.print(a.name); Serial.println(" (manual)");
  a.homed = false; // force a re-home
  // simple manual: just move toward until pressed (uses moveAxis which will HOME_HIT)
  moveAxis(a, +60000);
  if (!limPressed(a)) Serial.println("FAIL: never hit limit.");
}

void homeAll() { homeAxis(axX); homeAxis(axY); homeAxis(axZ); }

void printHelp() {
  Serial.println("Commands:");
  Serial.println("  HELP, LIMITS, POS, WATCH ON/OFF");
  Serial.println("  ENERGIZE / DEENERGIZE");
  Serial.println("  SPEED vx vy vz");
  Serial.println("  JOG X 200");
  Serial.println("  MOVE dx dy dz");
  Serial.println("  HOMEX/HOMEY/HOMEZ/HOMEALL");
  Serial.println("  ESTOP");
  Serial.println("  CLEAR");
}

void handleCommand(const String &cmd) {
  if (!cmd.length()) return;
  String up = cmd; up.toUpperCase();

  if (up == "HELP") { printHelp(); return; }
  if (up == "LIMITS") { printLimits(); return; }
  if (up == "POS") { printPos(); return; }

  if (up == "WATCH ON") { watchEnabled = true; Serial.println("OK WATCH=1"); return; }
  if (up == "WATCH OFF") { watchEnabled = false; Serial.println("OK WATCH=0"); return; }

  if (up == "ENERGIZE") { if (!estopLatched) setEnable(true); Serial.println("OK EN=1"); return; }
  if (up == "DEENERGIZE") { setEnable(false); Serial.println("OK EN=0"); return; }

  if (up == "ESTOP") { estopNow("manual"); return; }
  if (up == "CLEAR") { estopLatched = false; Serial.println("OK ESTOP=0"); return; }

  if (up.startsWith("SPEED")) {
    long vx, vy, vz;
    if (sscanf(cmd.c_str(), "SPEED %ld %ld %ld", &vx, &vy, &vz) == 3) {
      axX.speedSPS = max(1L, vx);
      axY.speedSPS = max(1L, vy);
      axZ.speedSPS = max(1L, vz);
      Serial.println("OK SPEED set");
    } else Serial.println("ERR: SPEED vx vy vz");
    return;
  }

  if (up.startsWith("JOG ")) {
    char ax; long steps;
    if (sscanf(cmd.c_str(), "JOG %c %ld", &ax, &steps) == 2) {
      Axis *a = axisByName(ax);
      if (!a) { Serial.println("ERR: axis must be X/Y/Z"); return; }
      moveAxis(*a, steps);
    } else Serial.println("ERR: JOG X 200");
    return;
  }

  if (up.startsWith("MOVE")) {
    long dx, dy, dz;
    if (sscanf(cmd.c_str(), "MOVE %ld %ld %ld", &dx, &dy, &dz) == 3) {
      moveXYZ(dx, dy, dz);
    } else Serial.println("ERR: MOVE dx dy dz");
    return;
  }

  if (up == "HOMEX") { homeAxis(axX); return; }
  if (up == "HOMEY") { homeAxis(axY); return; }
  if (up == "HOMEZ") { homeAxis(axZ); return; }
  if (up == "HOMEALL") { homeAll(); return; }

  Serial.println("ERR: Unknown command");
}

void setup() {
  pinMode(PIN_EN, OUTPUT);
  setEnable(false);

  pinMode(axX.stepPin, OUTPUT); pinMode(axX.dirPin, OUTPUT);
  pinMode(axY.stepPin, OUTPUT); pinMode(axY.dirPin, OUTPUT);
  pinMode(axZ.stepPin, OUTPUT); pinMode(axZ.dirPin, OUTPUT);

  pinMode(axX.limPin, INPUT_PULLUP);
  pinMode(axY.limPin, INPUT_PULLUP);
  pinMode(axZ.limPin, INPUT_PULLUP);

  Serial.begin(115200);
  delay(200);

  lastRawX = limRaw(axX);
  lastRawY = limRaw(axY);
  lastRawZ = limRaw(axZ);

  Serial.println("READY");
  printHelp();
  printLimits();
  printPos();

  if (AUTO_HOME_ON_BOOT) {
    autoHomeBootSimultaneous();
  }
}

void loop() {
  String line = readLineNonBlocking();
  if (line.length()) handleCommand(line);
  watchLimitsIfEnabled();
}

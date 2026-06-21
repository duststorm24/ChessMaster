// 3-axis stepper controller for CNC Shield V3 + Arduino Uno
// Wiring (CNC shield):
//   X driver -> Stepper 1 (base rotation)
//   Y driver -> Stepper 3 (arm 2)
//   Z driver -> Stepper 2 (arm 1)
//
// Limit switches:
//   X- (D9)  -> base
//   Y- (D10) -> arm 2
//   Z- (D11) -> arm 1
//
// Observed hardware behavior (verified):
//   digitalRead(limitPin) == 0  -> NOT PRESSED
//   digitalRead(limitPin) == 1  -> PRESSED
//
// GOAL: Positive steps (MOVE ... >0 ...) should move TOWARD the limit switch
// for ALL three steppers.
//
// Commands over Serial (115200 baud):
//
//   MOVE dx dy dz
//     e.g. MOVE 500 -200 0
//
//   SPEED vx vy vz
//     e.g. SPEED 800 600 400   (steps/sec for X, Y, Z)
//
//   POS
//     prints current positions + limits + EN
//
//   LIMITS
//     prints only limit states (1=pressed,0=not pressed)
//
//   RAWLIMITS
//     prints raw digitalRead pin states
//
//   HOMEX / HOMEY / HOMEZ
//     homes that axis: moves toward its limit until triggered,
//     then sets its position to 0
//
//   HOMEALL
//     homes X then Y then Z
//
//   ENERGIZE
//   DEENERGIZE
//     enable/disable stepper drivers

const int EN_PIN = 8;   // Enable (LOW = enabled) on most CNC shields

// CNC Shield default pins
const int X_STEP_PIN = 2;
const int X_DIR_PIN  = 5;

const int Y_STEP_PIN = 3;
const int Y_DIR_PIN  = 6;

const int Z_STEP_PIN = 4;
const int Z_DIR_PIN  = 7;

// Endstop pins on CNC shield (X-, Y-, Z-)
const int X_LIMIT_PIN = 9;
const int Y_LIMIT_PIN = 10;
const int Z_LIMIT_PIN = 11;

struct Axis {
  int stepPin;
  int dirPin;
  int limitPin;
  long pos;          // relative steps from home (logical)
  long speed_sps;    // steps per second
  int homingDir;     // +1 or -1 (logical direction TOWARD the limit switch)
  int dirSign;       // +1 = normal, -1 = invert direction pin
};

// We want logical "+steps" to mean "toward the limit switch" on ALL axes.
// From your latest test:
//   Stepper 1 (base, X):  +200 -> toward limit  (keep normal)
//   Stepper 2 (arm 1, Z): +200 -> toward limit  (keep normal)
//   Stepper 3 (arm 2, Y): +200 -> AWAY from limit (invert this in code)
Axis axisX = { X_STEP_PIN, X_DIR_PIN, X_LIMIT_PIN, 0, 400, +1, +1 };  // base
Axis axisY = { Y_STEP_PIN, Y_DIR_PIN, Y_LIMIT_PIN, 0, 400, +1, -1 };  // arm 2 (inverted)
Axis axisZ = { Z_STEP_PIN, Z_DIR_PIN, Z_LIMIT_PIN, 0, 400, +1, +1 };  // arm 1

bool driversEnabled = true;

// ---------- LIMIT HELPERS ----------

// Raw reading from pin: 0 = not pressed, 1 = pressed (on your hardware)
int rawLimitValue(const Axis &ax) {
  return digitalRead(ax.limitPin);
}

// True if switch is physically pressed
bool limitPressed(const Axis &ax) {
  return (rawLimitValue(ax) == 1);
}

// ---------- STEPPER HELPERS ----------

void stepPulse(const Axis &ax) {
  digitalWrite(ax.stepPin, HIGH);
  delayMicroseconds(2);
  digitalWrite(ax.stepPin, LOW);
}

// Move axis by "steps" logical steps (can be negative). Blocking.
// Positive logical steps = toward the limit switch (by our convention).
void moveAxis(Axis &ax, long steps) {
  if (steps == 0) return;

  if (!driversEnabled) {
    digitalWrite(EN_PIN, LOW);
    driversEnabled = true;
    Serial.println("DRIVERS: ENERGIZED");
  }

  bool dirPositive = (steps > 0);
  long total = (steps > 0) ? steps : -steps;

  // Determine what the DIR pin should be physically:
  // Start from logical direction (positive = HIGH), then apply dirSign.
  bool pinHigh = dirPositive;
  if (ax.dirSign < 0) {
    pinHigh = !pinHigh;
  }
  digitalWrite(ax.dirPin, pinHigh ? HIGH : LOW);

  long sps = (ax.speed_sps > 0) ? ax.speed_sps : 200;
  unsigned long interval = 1000000UL / (unsigned long)sps;

  for (long i = 0; i < total; i++) {
    stepPulse(ax);
    ax.pos += dirPositive ? 1 : -1; // logical position
    delayMicroseconds(interval);
  }
}

// Home a single axis in the logical "homingDir" direction.
// homingDir = +1 means "positive steps move toward the limit switch".
void homeAxis(Axis &ax, const char *name) {
  if (!driversEnabled) {
    digitalWrite(EN_PIN, LOW);
    driversEnabled = true;
    Serial.println("DRIVERS: ENERGIZED (for homing)");
  }

  const int BACKOFF_STEPS = 200;
  const long MAX_HOME_STEPS = 25000;  // safety limit
  long count = 0;

  // If currently pressed, back off a bit first (away from switch)
  if (limitPressed(ax)) {
    for (int i = 0; i < BACKOFF_STEPS; i++) {
      if (!limitPressed(ax)) break;
      moveAxis(ax, -ax.homingDir);  // move away one step
    }
  }

  // Now move TOWARD the limit until it triggers (or we give up)
  while (!limitPressed(ax) && count < MAX_HOME_STEPS) {
    moveAxis(ax, ax.homingDir);   // logical homingDir
    count++;
  }

  ax.pos = 0;
  Serial.print("HOME ");
  Serial.print(name);
  Serial.print(" done. Steps used: ");
  Serial.println(count);
}

// ---------- STATUS PRINTING ----------

void printLimits() {
  int lx = limitPressed(axisX) ? 1 : 0;
  int ly = limitPressed(axisY) ? 1 : 0;
  int lz = limitPressed(axisZ) ? 1 : 0;

  Serial.print("LIMITS (1=pressed,0=not) X=");
  Serial.print(lx);
  Serial.print(" Y=");
  Serial.print(ly);
  Serial.print(" Z=");
  Serial.println(lz);
}

void printRawLimits() {
  int rx = rawLimitValue(axisX);
  int ry = rawLimitValue(axisY);
  int rz = rawLimitValue(axisZ);

  Serial.print("RAW X=");
  Serial.print(rx);
  Serial.print(" Y=");
  Serial.print(ry);
  Serial.print(" Z=");
  Serial.println(rz);
}

void printPos() {
  Serial.print("POS X=");
  Serial.print(axisX.pos);
  Serial.print(" Y=");
  Serial.print(axisY.pos);
  Serial.print(" Z=");
  Serial.print(axisZ.pos);
  Serial.print(" EN=");
  Serial.println(driversEnabled ? 1 : 0);

  printLimits();
}

// ---------- COMMAND HANDLERS ----------

void handleSpeedCommand(const String &line) {
  // Expect: SPEED vx vy vz
  long vx = axisX.speed_sps;
  long vy = axisY.speed_sps;
  long vz = axisZ.speed_sps;

  int n = sscanf(line.c_str(), "SPEED %ld %ld %ld", &vx, &vy, &vz);
  if (n == 3) {
    if (vx <= 0) vx = axisX.speed_sps;
    if (vy <= 0) vy = axisY.speed_sps;
    if (vz <= 0) vz = axisZ.speed_sps;

    axisX.speed_sps = vx;
    axisY.speed_sps = vy;
    axisZ.speed_sps = vz;

    Serial.print("SPEED set: X=");
    Serial.print(vx);
    Serial.print(" Y=");
    Serial.print(vy);
    Serial.print(" Z=");
    Serial.println(vz);
  } else {
    Serial.println("ERR: SPEED expects 3 numbers, e.g. SPEED 800 600 400");
  }
}

void handleMoveCommand(const String &line) {
  long dx = 0, dy = 0, dz = 0;
  int n = sscanf(line.c_str(), "MOVE %ld %ld %ld", &dx, &dy, &dz);
  if (n != 3) {
    Serial.println("ERR: MOVE expects 3 numbers, e.g. MOVE 500 -200 0");
    return;
  }

  Serial.print("MOVE dx=");
  Serial.print(dx);
  Serial.print(" dy=");
  Serial.print(dy);
  Serial.print(" dz=");
  Serial.println(dz);

  moveAxis(axisX, dx);
  moveAxis(axisY, dy);
  moveAxis(axisZ, dz);

  printPos();
}

void handleLine(String line) {
  line.trim();
  if (line.length() == 0) return;

  line.toUpperCase();

  if (line.startsWith("MOVE")) {
    handleMoveCommand(line);
  } else if (line.startsWith("SPEED")) {
    handleSpeedCommand(line);
  } else if (line == "POS") {
    printPos();
  } else if (line == "LIMITS") {
    printLimits();
  } else if (line == "RAWLIMITS") {
    printRawLimits();
  } else if (line == "HOMEX") {
    homeAxis(axisX, "X");
    printPos();
  } else if (line == "HOMEY") {
    homeAxis(axisY, "Y");
    printPos();
  } else if (line == "HOMEZ") {
    homeAxis(axisZ, "Z");
    printPos();
  } else if (line == "HOMEALL") {
    homeAxis(axisX, "X");
    homeAxis(axisY, "Y");
    homeAxis(axisZ, "Z");
    printPos();
  } else if (line == "DEENERGIZE") {
    digitalWrite(EN_PIN, HIGH);
    driversEnabled = false;
    Serial.println("DRIVERS: DE-ENERGIZED");
    printPos();
  } else if (line == "ENERGIZE") {
    digitalWrite(EN_PIN, LOW);
    driversEnabled = true;
    Serial.println("DRIVERS: ENERGIZED");
    printPos();
  } else {
    Serial.print("UNKNOWN CMD: ");
    Serial.println(line);
  }
}

// ---------- SETUP / LOOP ----------

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, LOW);
  driversEnabled = true;

  pinMode(axisX.stepPin, OUTPUT);
  pinMode(axisX.dirPin,  OUTPUT);
  pinMode(axisY.stepPin, OUTPUT);
  pinMode(axisY.dirPin,  OUTPUT);
  pinMode(axisZ.stepPin, OUTPUT);
  pinMode(axisZ.dirPin,  OUTPUT);

  // IMPORTANT: plain INPUT, because your CNC shield already provides the biasing.
  pinMode(axisX.limitPin, INPUT);
  pinMode(axisY.limitPin, INPUT);
  pinMode(axisZ.limitPin, INPUT);

  Serial.println("3-axis stepper controller READY.");
  Serial.println("Commands:");
  Serial.println("  MOVE dx dy dz");
  Serial.println("  SPEED vx vy vz");
  Serial.println("  POS");
  Serial.println("  LIMITS (1=pressed,0=not pressed)");
  Serial.println("  RAWLIMITS");
  Serial.println("  HOMEX / HOMEY / HOMEZ / HOMEALL");
  Serial.println("  ENERGIZE / DEENERGIZE");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    handleLine(line);
  }
}

// 3-axis stepper controller for CNC Shield V3 + A4988 + Arduino Uno
// X = Stepper 1 (base)
// Y = Stepper 3 (arm 2)
// Z = Stepper 2 (arm 1)
//
// Commands (over serial, 115200 baud):
//   MOVE dx dy dz
//   SPEED vx vy vz
//   POS
//   LIMITS
//   HOMEX / HOMEY / HOMEZ / HOMEALL
//   (also accepts: HOME1/HOME2/HOME3, HOME S1/S2/S3, HOMES1/S2/S3)
//   ENERGIZE / DEENERGIZE
//
// LIMITS output format (for the Pi UI):
//   LIMITS X=0 Y=0 Z=0   (1 = pressed, 0 = released)

const int EN_PIN        = 8;  // Enable (LOW = enabled) on CNC shield

// Step/dir pins for X/Y/Z on CNC shield
const int X_STEP_PIN    = 2;
const int X_DIR_PIN     = 5;
const int Y_STEP_PIN    = 3;
const int Y_DIR_PIN     = 6;
const int Z_STEP_PIN    = 4;
const int Z_DIR_PIN     = 7;

// Endstop (limit switch) pins: X-/Y-/Z-
const int X_LIMIT_PIN   = 9;
const int Y_LIMIT_PIN   = 10;
const int Z_LIMIT_PIN   = 11;

// Homing safety: max steps we'll search in the homing direction
const long HOME_MAX_STEPS = 15000;

// Min/max speeds
const long MIN_SPEED_SPS = 50;
const long MAX_SPEED_SPS = 4000;

struct Axis {
  int stepPin;
  int dirPin;
  int limitPin;
  long pos;        // position in steps (0 = home)
  long speed_sps;  // steps per second
  int homeDir;     // +1 or -1: direction that goes TOWARD the limit switch
};

// From your description:
//  - Base (X):   negative steps go toward the switch  -> homeDir = -1
//  - Arm 1 (Z):  negative steps go toward the switch  -> homeDir = -1
//  - Arm 2 (Y):  positive steps go toward the switch  -> homeDir = +1
Axis axisX = { X_STEP_PIN, X_DIR_PIN, X_LIMIT_PIN, 0, 400, -1 };
Axis axisY = { Y_STEP_PIN, Y_DIR_PIN, Y_LIMIT_PIN, 0, 400, +1 };
Axis axisZ = { Z_STEP_PIN, Z_DIR_PIN, Z_LIMIT_PIN, 0, 400, -1 };

bool driversEnabled = true;

// ---------- LOW-LEVEL HELPERS ----------

bool limitPressed(const Axis &ax) {
  // INPUT_PULLUP: pressed = LOW
  return (digitalRead(ax.limitPin) == LOW);
}

void clampSpeed(long &spd) {
  if (spd < MIN_SPEED_SPS) spd = MIN_SPEED_SPS;
  if (spd > MAX_SPEED_SPS) spd = MAX_SPEED_SPS;
}

void stepOnce(Axis &ax, int dirSign, unsigned long delayUs) {
  if (!driversEnabled) return;

  digitalWrite(ax.dirPin, (dirSign > 0) ? HIGH : LOW);

  digitalWrite(ax.stepPin, HIGH);
  delayMicroseconds(2);
  digitalWrite(ax.stepPin, LOW);

  delayMicroseconds(delayUs);

  ax.pos += (dirSign > 0) ? 1 : -1;
}

// Move a single axis by "steps" (positive or negative).
void moveAxisSteps(Axis &ax, long steps) {
  if (!driversEnabled) return;
  if (steps == 0) return;

  int dirSign = (steps > 0) ? +1 : -1;
  long count = (steps > 0) ? steps : -steps;

  long spd = ax.speed_sps;
  clampSpeed(spd);
  unsigned long delayUs = 1000000UL / (unsigned long)spd;

  for (long i = 0; i < count; i++) {
    stepOnce(ax, dirSign, delayUs);
  }
}

// ---------- HOMING LOGIC (simple, directional) ----------

// Home a single axis using its known homeDir:
//  - If we start already on the switch: just call that home, pos=0, done.
//  - Otherwise: step in homeDir until switch is pressed or HOME_MAX_STEPS reached.
void homeAxisDirectional(Axis &ax, const char *name) {
  if (!driversEnabled) {
    digitalWrite(EN_PIN, LOW);
    driversEnabled = true;
  }

  Serial.print("HOMING ");
  Serial.println(name);

  long spd = ax.speed_sps;
  clampSpeed(spd);
  unsigned long delayUs = 1000000UL / (unsigned long)spd;

  int homeDir = ax.homeDir;

  // Already on the switch? Call that home.
  if (limitPressed(ax)) {
    Serial.print("  ");
    Serial.print(name);
    Serial.println(" already at switch; pos=0.");
    ax.pos = 0;
    return;
  }

  bool found = false;
  long moved = 0;

  for (long i = 0; i < HOME_MAX_STEPS; i++) {
    if (limitPressed(ax)) {
      found = true;
      break;
    }
    stepOnce(ax, homeDir, delayUs);
    moved++;
  }

  Serial.print("  ");
  Serial.print(name);
  Serial.print(" moved steps: ");
  Serial.println(moved);

  if (found) {
    Serial.print("  ");
    Serial.print(name);
    Serial.println(" homed (switch hit).");
    ax.pos = 0;
  } else {
    Serial.print("  ");
    Serial.print(name);
    Serial.println(" homing FAILED (no switch).");
  }
}

void homeX() { homeAxisDirectional(axisX, "X"); }
void homeY() { homeAxisDirectional(axisY, "Y"); }
void homeZ() { homeAxisDirectional(axisZ, "Z"); }

void homeAll() {
  homeX();
  homeY();
  homeZ();
}

// ---------- STATUS OUTPUT ----------

void printPos() {
  Serial.print("POS X=");
  Serial.print(axisX.pos);
  Serial.print(" Y=");
  Serial.print(axisY.pos);
  Serial.print(" Z=");
  Serial.println(axisZ.pos);
}

void printLimits() {
  int lx = limitPressed(axisX) ? 1 : 0;
  int ly = limitPressed(axisY) ? 1 : 0;
  int lz = limitPressed(axisZ) ? 1 : 0;

  Serial.print("LIMITS X=");
  Serial.print(lx);
  Serial.print(" Y=");
  Serial.print(ly);
  Serial.print(" Z=");
  Serial.println(lz);
}

// ---------- COMMAND PARSING ----------

void handleCommand(String line) {
  line.trim();
  if (line.length() == 0) return;

  // Debug: show exactly what came from the Pi
  Serial.print("CMD: ");
  Serial.println(line);

  // Normalise to upper case for matching
  String upper = line;
  upper.toUpperCase();

  if (upper.startsWith("MOVE")) {
    long dx, dy, dz;
    int scanned = sscanf(line.c_str(), "MOVE %ld %ld %ld", &dx, &dy, &dz);
    if (scanned == 3) {
      Serial.print("MOVE dx=");
      Serial.print(dx);
      Serial.print(" dy=");
      Serial.print(dy);
      Serial.print(" dz=");
      Serial.println(dz);
      moveAxisSteps(axisX, dx);
      moveAxisSteps(axisY, dy);
      moveAxisSteps(axisZ, dz);
      printPos();
    } else {
      Serial.println("ERR bad MOVE");
    }
  }
  else if (upper.startsWith("SPEED")) {
    long vx, vy, vz;
    int scanned = sscanf(line.c_str(), "SPEED %ld %ld %ld", &vx, &vy, &vz);
    if (scanned == 3) {
      clampSpeed(vx);
      clampSpeed(vy);
      clampSpeed(vz);

      axisX.speed_sps = vx;
      axisY.speed_sps = vy;
      axisZ.speed_sps = vz;

      Serial.print("SPEED X=");
      Serial.print(axisX.speed_sps);
      Serial.print(" Y=");
      Serial.print(axisY.speed_sps);
      Serial.print(" Z=");
      Serial.println(axisZ.speed_sps);
    } else {
      Serial.println("ERR bad SPEED");
    }
  }
  else if (upper == "POS") {
    printPos();
  }
  else if (upper == "LIMITS") {
    printLimits();
  }
  // ----- HOMING: accept multiple spellings from the UI -----
  else if (upper == "HOMEX"  || upper == "HOME1"  || upper == "HOME S1" || upper == "HOMES1") {
    homeX();
  }
  else if (upper == "HOMEY"  || upper == "HOME3"  || upper == "HOME S3" || upper == "HOMES3") {
    // UI might call this S3 depending on mapping
    homeY();
  }
  else if (upper == "HOMEZ"  || upper == "HOME2"  || upper == "HOME S2" || upper == "HOMES2") {
    homeZ();
  }
  else if (upper == "HOMEALL" || upper == "HOME ALL" || upper == "ALLHOME") {
    homeAll();
  }
  // ----- POWER -----
  else if (upper == "ENERGIZE") {
    digitalWrite(EN_PIN, LOW);
    driversEnabled = true;
    Serial.println("DRIVERS ENERGIZED");
  }
  else if (upper == "DEENERGIZE") {
    digitalWrite(EN_PIN, HIGH);
    driversEnabled = false;
    Serial.println("DRIVERS DE-ENERGIZED");
  }
  else {
    Serial.print("UNKNOWN CMD (after normalisation): ");
    Serial.println(upper);
  }
}

// ---------- SETUP / LOOP ----------

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, LOW);  // enable drivers by default
  driversEnabled = true;

  pinMode(axisX.stepPin, OUTPUT);
  pinMode(axisX.dirPin,  OUTPUT);
  pinMode(axisY.stepPin, OUTPUT);
  pinMode(axisY.dirPin,  OUTPUT);
  pinMode(axisZ.stepPin, OUTPUT);
  pinMode(axisZ.dirPin,  OUTPUT);

  pinMode(axisX.limitPin, INPUT_PULLUP);
  pinMode(axisY.limitPin, INPUT_PULLUP);
  pinMode(axisZ.limitPin, INPUT_PULLUP);

  Serial.println("3-axis stepper controller READY.");
  Serial.println("Commands:");
  Serial.println("  MOVE dx dy dz");
  Serial.println("  SPEED vx vy vz");
  Serial.println("  POS");
  Serial.println("  LIMITS");
  Serial.println("  HOMEX / HOMEY / HOMEZ / HOMEALL");
  Serial.println("  (or HOME1/2/3, HOME S1/S2/S3)");
  Serial.println("  ENERGIZE / DEENERGIZE");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    handleCommand(line);
  }
}

// CNC Shield V3 + A4988 + Arduino Uno
// Stepper 1 = X slot (base rotation)
// Stepper 2 = Z slot (first arm)
// Stepper 3 = Y slot (second arm)
//
// Commands over Serial:
//
// 1) Set speeds (persist until changed):
//    1=200,2=1200,3=800   -> stepper 1 @ 200 sps, stepper 2 @ 1200 sps, stepper 3 @ 800 sps
//
// 2) Coordinated move (all 3 axes):
//    r4u12f6  ->
//      base RIGHT      4*100 = 400 steps  (stepper 1, X)
//      first arm  UP  12*100 = 1200 steps (stepper 2, Z)
//      second arm FWD  6*100 = 600 steps  (stepper 3, Y)
//
//    l2d5b3   ->
//      base LEFT       2*100 = 200 steps
//      first arm DOWN  5*100 = 500 steps
//      second arm BACK 3*100 = 300 steps
//
// 3) Homing:
//    h      -> all three axes move toward their limit switches until hit:
//              - base:  RIGHT (toward X-)
//              - arm1:  UP    (toward Z-)
//              - arm2:  BACK  (toward Y-)
//             When all are homed, drivers are DISABLED so joints are free.
//
// 4) Emergency stop:
//    s      -> stop current move immediately, DISABLE drivers
//
// Drivers are kept ENABLED during motion,
// except when an E-stop or homing finishes.

const int EN_PIN  = 8;   // Enable (LOW = enable, HIGH = disable) on most CNC shields

// CNC Shield default step/dir pins:
const int X_STEP = 2;
const int X_DIR  = 5;

const int Y_STEP = 3;
const int Y_DIR  = 6;

const int Z_STEP = 4;
const int Z_DIR  = 7;

// (We are not using the A slot in this sketch)

// ----- LIMIT SWITCH PINS (wired to X-, Y-, Z- on CNC shield) -----
// Base (stepper 1) -> X-  -> Arduino D9
// Arm2 (stepper 3) -> Y-  -> Arduino D11
// Arm1 (stepper 2) -> Z-  -> Arduino D13
const int LIMIT_BASE_PIN = 9;   // X-
const int LIMIT_ARM2_PIN = 11;  // Y-
const int LIMIT_ARM1_PIN = 13;  // Z-

// Generic axis struct
struct Axis {
  int stepPin;
  int dirPin;
};

// Stepper 1 = base (X)
// Stepper 2 = first arm (Z)
// Stepper 3 = second arm (Y)
Axis baseAxis   = { X_STEP, X_DIR }; // Stepper 1
Axis arm1Axis   = { Z_STEP, Z_DIR }; // Stepper 2
Axis arm2Axis   = { Y_STEP, Y_DIR }; // Stepper 3

// ---- SPEED SETTINGS ----
long speedBase_sps  = 200;  // default base speed (steps/sec)
long speedArm1_sps  = 200;  // default first arm speed
long speedArm2_sps  = 200;  // default second arm speed

const long MIN_SPEED_SPS = 50;
const long MAX_SPEED_SPS = 4000;

// Homing speed (you can tweak this)
const long HOMING_BASE_SPS = 400;
const long HOMING_ARM1_SPS = 400;
const long HOMING_ARM2_SPS = 400;

// Each unit in r4/u12/f6 command = this many steps
const long STEP_UNIT = 100;

// E-stop flag
bool estopTriggered = false;

// Move counter for logging
unsigned long moveCounter = 0;

// ------- LIMIT SWITCH HELPERS (INPUT_PULLUP, active LOW) -------
bool baseLimitHit()  { return digitalRead(LIMIT_BASE_PIN) == LOW; }
bool arm1LimitHit()  { return digitalRead(LIMIT_ARM1_PIN) == LOW; }
bool arm2LimitHit()  { return digitalRead(LIMIT_ARM2_PIN) == LOW; }

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);

  // Keep drivers ENABLED by default
  digitalWrite(EN_PIN, LOW);  // LOW = enabled on A4988 / most CNC shields

  pinMode(baseAxis.stepPin, OUTPUT);
  pinMode(baseAxis.dirPin,  OUTPUT);

  pinMode(arm1Axis.stepPin, OUTPUT);
  pinMode(arm1Axis.dirPin,  OUTPUT);

  pinMode(arm2Axis.stepPin, OUTPUT);
  pinMode(arm2Axis.dirPin,  OUTPUT);

  digitalWrite(baseAxis.stepPin, LOW);
  digitalWrite(arm1Axis.stepPin, LOW);
  digitalWrite(arm2Axis.stepPin, LOW);

  // Limit switches as INPUT_PULLUP (active LOW when pressed)
  pinMode(LIMIT_BASE_PIN, INPUT_PULLUP);
  pinMode(LIMIT_ARM1_PIN, INPUT_PULLUP);
  pinMode(LIMIT_ARM2_PIN, INPUT_PULLUP);

  Serial.println("Chess robot 3-axis controller READY.");
  Serial.println("Commands:");
  Serial.println("  Speeds: 1=200,2=1200,3=800   (steps/sec)");
  Serial.println("  Move:   r4u12f6  (r/l = base, u/d = arm1, f/b = arm2, units of 100 steps)");
  Serial.println("  Home:   h        (all 3 axes move toward their limit switches)");
  Serial.println("  E-stop: s");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) return;

    // E-stop command (from idle or mid-usage)
    if (line.equalsIgnoreCase("s")) {
      Serial.println("[E-STOP] Command received (idle). Drivers disabled.");
      digitalWrite(EN_PIN, HIGH); // disable all drivers
      estopTriggered = false;
      return;
    }

    // Homing command
    if (line.equalsIgnoreCase("h")) {
      homeAllAxes();
      return;
    }

    // Speed config vs move
    if (line.indexOf('=') != -1) {
      handleSpeedCommand(line);
    } else {
      handleMoveCommand(line);
    }
  }
}

// ---------------- SPEED COMMANDS ----------------
// Format:
// "1=200"             -> base  @ 200 sps
// "2=1200"            -> arm1  @ 1200 sps
// "3=800"             -> arm2  @ 800 sps
// "1=200,2=1200,3=800"-> all three
void handleSpeedCommand(String line) {
  line.replace(" ", "");  // strip spaces

  int start = 0;
  while (start < line.length()) {
    int commaIndex = line.indexOf(',', start);
    String part;
    if (commaIndex == -1) {
      part = line.substring(start);
      start = line.length();
    } else {
      part = line.substring(start, commaIndex);
      start = commaIndex + 1;
    }

    part.trim();
    if (part.length() == 0) continue;

    int eqIndex = part.indexOf('=');
    if (eqIndex == -1) continue;

    String motorStr = part.substring(0, eqIndex);
    String speedStr = part.substring(eqIndex + 1);

    motorStr.trim();
    speedStr.trim();

    int motorNum = motorStr.toInt();
    long spd = speedStr.toInt();
    if (spd <= 0) continue;

    if (spd < MIN_SPEED_SPS) spd = MIN_SPEED_SPS;
    if (spd > MAX_SPEED_SPS) spd = MAX_SPEED_SPS;

    if (motorNum == 1) {
      speedBase_sps = spd;
    } else if (motorNum == 2) {
      speedArm1_sps = spd;
    } else if (motorNum == 3) {
      speedArm2_sps = spd;
    }
  }

  Serial.print("[SPEED] 1=");
  Serial.print(speedBase_sps);
  Serial.print(" sps, 2=");
  Serial.print(speedArm1_sps);
  Serial.print(" sps, 3=");
  Serial.print(speedArm2_sps);
  Serial.println(" sps");
}

// ---------------- MOVE COMMANDS ----------------
// r4u12f6 -> base RIGHT, arm1 UP, arm2 FORWARD  (all in units of STEP_UNIT)
// l2d5b3  -> base LEFT,  arm1 DOWN, arm2 BACK
void handleMoveCommand(String line) {
  String original = line; // keep for log
  line.toLowerCase();

  long baseSteps = 0;
  long arm1Steps = 0;
  long arm2Steps = 0;

  int i = 0;
  while (i < line.length()) {
    char c = line.charAt(i);
    if (c == 'r' || c == 'l' || c == 'u' || c == 'd' || c == 'f' || c == 'b') {
      i++;

      // Optional sign
      bool neg = false;
      if (i < line.length() && line.charAt(i) == '-') {
        neg = true;
        i++;
      }

      // Collect digits
      long value = 0;
      bool hasDigit = false;
      while (i < line.length() && isDigit(line.charAt(i))) {
        hasDigit = true;
        value = value * 10 + (line.charAt(i) - '0');
        i++;
      }

      if (!hasDigit) {
        continue;
      }

      if (neg) value = -value;

      long steps = value * STEP_UNIT;

      switch (c) {
        case 'r': // base right (toward its X- limit in your wiring)
          baseSteps = steps;
          break;
        case 'l': // base left
          baseSteps = -steps;
          break;
        case 'u': // arm1 up (toward its Z- limit)
          arm1Steps = steps;
          break;
        case 'd': // arm1 down
          arm1Steps = -steps;
          break;
        case 'f': // arm2 forward (away from its Y- limit)
          arm2Steps = steps;
          break;
        case 'b': // arm2 back (toward its Y- limit)
          arm2Steps = -steps;
          break;
      }
    } else {
      i++; // ignore other characters
    }
  }

  if (baseSteps == 0 && arm1Steps == 0 && arm2Steps == 0) {
    Serial.println("[MOVE] No valid move parsed from command.");
    return;
  }

  moveCounter++;

  Serial.print("[MOVE #");
  Serial.print(moveCounter);
  Serial.print("] cmd='");
  Serial.print(original);
  Serial.print("' -> baseSteps=");
  Serial.print(baseSteps);
  Serial.print(", arm1Steps=");
  Serial.print(arm1Steps);
  Serial.print(", arm2Steps=");
  Serial.println(arm2Steps);

  coordinatedMove3(baseSteps, arm1Steps, arm2Steps);
}

// ---------------- 3-AXIS COORDINATED MOVE ----------------
void coordinatedMove3(long baseSteps, long arm1Steps, long arm2Steps) {
  long baseDir = 0;
  long arm1Dir = 0;
  long arm2Dir = 0;

  // Base
  if (baseSteps > 0) {
    baseDir = +1;
    digitalWrite(baseAxis.dirPin, HIGH);
  } else if (baseSteps < 0) {
    baseDir = -1;
    digitalWrite(baseAxis.dirPin, LOW);
    baseSteps = -baseSteps;
  }

  // Arm1
  if (arm1Steps > 0) {
    arm1Dir = +1;
    digitalWrite(arm1Axis.dirPin, HIGH);
  } else if (arm1Steps < 0) {
    arm1Dir = -1;
    digitalWrite(arm1Axis.dirPin, LOW);
    arm1Steps = -arm1Steps;
  }

  // Arm2
  if (arm2Steps > 0) {
    arm2Dir = +1;
    digitalWrite(arm2Axis.dirPin, HIGH);
  } else if (arm2Steps < 0) {
    arm2Dir = -1;
    digitalWrite(arm2Axis.dirPin, LOW);
    arm2Steps = -arm2Steps;
  }

  if (baseSteps == 0 && arm1Steps == 0 && arm2Steps == 0) {
    Serial.println("[MOVE] Nothing to move.");
    return;
  }

  // Clamp speeds
  if (speedBase_sps < MIN_SPEED_SPS) speedBase_sps = MIN_SPEED_SPS;
  if (speedBase_sps > MAX_SPEED_SPS) speedBase_sps = MAX_SPEED_SPS;
  if (speedArm1_sps < MIN_SPEED_SPS) speedArm1_sps = MIN_SPEED_SPS;
  if (speedArm1_sps > MAX_SPEED_SPS) speedArm1_sps = MAX_SPEED_SPS;
  if (speedArm2_sps < MIN_SPEED_SPS) speedArm2_sps = MIN_SPEED_SPS;
  if (speedArm2_sps > MAX_SPEED_SPS) speedArm2_sps = MAX_SPEED_SPS;

  unsigned long intervalBase = (baseSteps > 0 && speedBase_sps > 0)
                               ? (1000000UL / (unsigned long)speedBase_sps)
                               : 0;

  unsigned long intervalArm1 = (arm1Steps > 0 && speedArm1_sps > 0)
                               ? (1000000UL / (unsigned long)speedArm1_sps)
                               : 0;

  unsigned long intervalArm2 = (arm2Steps > 0 && speedArm2_sps > 0)
                               ? (1000000UL / (unsigned long)speedArm2_sps)
                               : 0;

  Serial.print("[RUN] Base=");
  Serial.print(baseSteps);
  Serial.print(" @ ");
  Serial.print(speedBase_sps);
  Serial.print(" sps, Arm1=");
  Serial.print(arm1Steps);
  Serial.print(" @ ");
  Serial.print(speedArm1_sps);
  Serial.print(" sps, Arm2=");
  Serial.print(arm2Steps);
  Serial.print(" @ ");
  Serial.print(speedArm2_sps);
  Serial.println(" sps");

  // Clear any old E-stop
  estopTriggered = false;

  // Make sure drivers are enabled before motion
  digitalWrite(EN_PIN, LOW);

  long doneBase = 0;
  long doneArm1 = 0;
  long doneArm2 = 0;
  unsigned long nextBase = micros();
  unsigned long nextArm1 = micros();
  unsigned long nextArm2 = micros();

  while ((doneBase < baseSteps || doneArm1 < arm1Steps || doneArm2 < arm2Steps) && !estopTriggered) {
    unsigned long now = micros();

    // --- Check for E-stop mid-move ---
    if (Serial.available()) {
      char c = Serial.read();
      if (c == 's' || c == 'S') {
        estopTriggered = true;
        Serial.println("[E-STOP] TRIGGERED! Halting motion.");
        // Flush rest of line
        while (Serial.available()) {
          char dump = Serial.read();
          if (dump == '\n' || dump == '\r') break;
        }
      }
    }

    if (estopTriggered) {
      break;
    }

    // Base stepping
    if (baseSteps > 0 && doneBase < baseSteps && intervalBase > 0 && now >= nextBase) {
      stepPulse(baseAxis.stepPin);
      doneBase++;
      nextBase += intervalBase;
    }

    // Arm1 stepping
    if (arm1Steps > 0 && doneArm1 < arm1Steps && intervalArm1 > 0 && now >= nextArm1) {
      stepPulse(arm1Axis.stepPin);
      doneArm1++;
      nextArm1 += intervalArm1;
    }

    // Arm2 stepping
    if (arm2Steps > 0 && doneArm2 < arm2Steps && intervalArm2 > 0 && now >= nextArm2) {
      stepPulse(arm2Axis.stepPin);
      doneArm2++;
      nextArm2 += intervalArm2;
    }
  }

  if (estopTriggered) {
    // Kill torque on E-stop
    digitalWrite(EN_PIN, HIGH);
    Serial.println("[DONE] Motion stopped by E-stop. Drivers DISABLED.");
  } else {
    // Normal end: keep drivers enabled
    Serial.println("[DONE] Move complete. Drivers remain ENABLED.");
  }
}

// ----------- HOMING: move all 3 axes toward their limit switches -----------
void homeAllAxes() {
  Serial.println("[HOME] Starting homing for all 3 axes.");

  // Directions:
  // - Base: RIGHT (same as 'r' -> dir HIGH) toward X- switch
  // - Arm1: UP    (same as 'u' -> dir HIGH) toward Z- switch
  // - Arm2: BACK  (same as 'b' -> negative -> dir LOW) toward Y- switch
  digitalWrite(baseAxis.dirPin, HIGH);   // base toward its limit
  digitalWrite(arm1Axis.dirPin, HIGH);   // arm1 toward its limit
  digitalWrite(arm2Axis.dirPin, LOW);    // arm2 toward its limit

  long spdBase = HOMING_BASE_SPS;
  long spdArm1 = HOMING_ARM1_SPS;
  long spdArm2 = HOMING_ARM2_SPS;

  if (spdBase < MIN_SPEED_SPS) spdBase = MIN_SPEED_SPS;
  if (spdArm1 < MIN_SPEED_SPS) spdArm1 = MIN_SPEED_SPS;
  if (spdArm2 < MIN_SPEED_SPS) spdArm2 = MIN_SPEED_SPS;

  unsigned long intervalBase = 1000000UL / (unsigned long)spdBase;
  unsigned long intervalArm1 = 1000000UL / (unsigned long)spdArm1;
  unsigned long intervalArm2 = 1000000UL / (unsigned long)spdArm2;

  bool baseDone = false;
  bool arm1Done = false;
  bool arm2Done = false;

  // Enable drivers for homing
  digitalWrite(EN_PIN, LOW);
  estopTriggered = false;

  unsigned long nextBase = micros();
  unsigned long nextArm1 = micros();
  unsigned long nextArm2 = micros();

  while (!(baseDone && arm1Done && arm2Done) && !estopTriggered) {
    unsigned long now = micros();

    // E-stop check
    if (Serial.available()) {
      char c = Serial.read();
      if (c == 's' || c == 'S') {
        estopTriggered = true;
        Serial.println("[E-STOP] TRIGGERED DURING HOMING! Halting motion.");
        while (Serial.available()) {
          char dump = Serial.read();
          if (dump == '\n' || dump == '\r') break;
        }
      }
    }
    if (estopTriggered) break;

    // Base homing
    if (!baseDone && now >= nextBase) {
      if (baseLimitHit()) {
        baseDone = true;
        Serial.println("[HOME] Base homed (X- limit hit).");
      } else {
        stepPulse(baseAxis.stepPin);
      }
      nextBase += intervalBase;
    }

    // Arm1 homing
    if (!arm1Done && now >= nextArm1) {
      if (arm1LimitHit()) {
        arm1Done = true;
        Serial.println("[HOME] Arm1 homed (Z- limit hit).");
      } else {
        stepPulse(arm1Axis.stepPin);
      }
      nextArm1 += intervalArm1;
    }

    // Arm2 homing
    if (!arm2Done && now >= nextArm2) {
      if (arm2LimitHit()) {
        arm2Done = true;
        Serial.println("[HOME] Arm2 homed (Y- limit hit).");
      } else {
        stepPulse(arm2Axis.stepPin);
      }
      nextArm2 += intervalArm2;
    }
  }

  // When homing finishes, kill torque so joints are free
  digitalWrite(EN_PIN, HIGH);

  if (estopTriggered) {
    Serial.println("[HOME] Homing aborted by E-stop. Drivers DISABLED.");
  } else {
    Serial.println("[HOME] All axes homed. Drivers DISABLED (free to move).");
  }
}

void stepPulse(int stepPin) {
  digitalWrite(stepPin, HIGH);
  delayMicroseconds(2);
  digitalWrite(stepPin, LOW);
}
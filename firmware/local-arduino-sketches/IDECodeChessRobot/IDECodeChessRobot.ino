// CNC Shield V3 + A4988 + Arduino Uno
// Command format over Serial:  motor,steps,speed
// Example:  1,+500,250   -> Stepper 1, +500 steps, 250 steps/second
// Now updated so steppers are DISABLED when not moving (no hissing).

// Enable pin (shared by all drivers, active LOW on A4988)
const int EN_PIN  = 8;

// CNC Shield default pin mapping for A4988 drivers:
const int X_STEP = 2;
const int X_DIR  = 5;

const int Y_STEP = 3;
const int Y_DIR  = 6;

const int Z_STEP = 4;
const int Z_DIR  = 7;

const int A_STEP = 12;
const int A_DIR  = 13;

// Mapping: "Stepper 1" = X, "Stepper 2" = Z, "Stepper 3" = Y, "Stepper 4" = A
struct Axis {
  int stepPin;
  int dirPin;
};

Axis steppers[4] = {
  {X_STEP, X_DIR},  // index 0 -> Stepper 1 (X slot, rotating base)
  {Z_STEP, Z_DIR},  // index 1 -> Stepper 2 (Z slot, first arm)
  {Y_STEP, Y_DIR},  // index 2 -> Stepper 3 (Y slot, second arm)
  {A_STEP, A_DIR}   // index 3 -> Stepper 4 (A slot, gripper)
};

// --------- CONFIG ----------
const long MIN_SPEED_SPS = 50;    // min 50 steps/sec
const long MAX_SPEED_SPS = 2000;  // max 2000 steps/sec
// ---------------------------

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  // Start with all drivers DISABLED (quiet)
  digitalWrite(EN_PIN, HIGH);  // HIGH = disabled for A4988 on most CNC shields

  for (int i = 0; i < 4; i++) {
    pinMode(steppers[i].stepPin, OUTPUT);
    pinMode(steppers[i].dirPin, OUTPUT);
    digitalWrite(steppers[i].stepPin, LOW);
  }

  Serial.println("CNC shield stepper controller ready.");
  Serial.println("Format: motor,steps,speed  (e.g. 1,+500,250)");
  Serial.println("motor: 1-4  | steps: +/- integer | speed: steps/sec (50-2000)");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) return;

    int motorIndex;
    long steps;
    long speedSPS;

    if (!parseCommand(line, motorIndex, steps, speedSPS)) {
      Serial.println("Invalid command. Use: motor,steps,speed  (e.g. 1,+500,250)");
      return;
    }

    // Move the requested motor
    moveStepper(motorIndex, steps, speedSPS);
  }
}

bool parseCommand(const String &line, int &motorIndex, long &steps, long &speedSPS) {
  int c1 = line.indexOf(',');
  int c2 = line.indexOf(',', c1 + 1);

  if (c1 == -1 || c2 == -1) {
    return false;
  }

  String sMotor = line.substring(0, c1);
  String sSteps = line.substring(c1 + 1, c2);
  String sSpeed = line.substring(c2 + 1);

  sMotor.trim();
  sSteps.trim();
  sSpeed.trim();

  int motorNum = sMotor.toInt();  // 1–4 expected
  long stepVal = sSteps.toInt();  // can be negative
  long speedVal = sSpeed.toInt(); // steps per second

  if (motorNum < 1 || motorNum > 4) {
    Serial.println("Motor number must be 1-4.");
    return false;
  }
  if (speedVal <= 0) {
    Serial.println("Speed must be > 0.");
    return false;
  }

  // Constrain speed to safe range
  if (speedVal < MIN_SPEED_SPS) speedVal = MIN_SPEED_SPS;
  if (speedVal > MAX_SPEED_SPS) speedVal = MAX_SPEED_SPS;

  motorIndex = motorNum - 1;  // convert to 0-based index
  steps = stepVal;
  speedSPS = speedVal;
  return true;
}

void moveStepper(int motorIndex, long steps, long speedSPS) {
  Axis axis = steppers[motorIndex];

  if (steps == 0) {
    Serial.println("No movement requested (0 steps).");
    return;
  }

  // Direction
  if (steps > 0) {
    digitalWrite(axis.dirPin, HIGH);  // define HIGH as "clockwise" for now
  } else {
    digitalWrite(axis.dirPin, LOW);   // and LOW as "counter-clockwise"
  }

  long stepCount = labs(steps);

  // Convert steps/sec to delay per half-step in microseconds
  unsigned long stepDelayMicros = 1000000UL / (unsigned long)speedSPS;

  Serial.print("Moving stepper ");
  Serial.print(motorIndex + 1);
  Serial.print(" by ");
  Serial.print(steps);
  Serial.print(" steps at ");
  Serial.print(speedSPS);
  Serial.println(" steps/sec");

  // --- ENABLE DRIVERS JUST FOR THIS MOVE ---
  digitalWrite(EN_PIN, LOW);   // LOW = enabled on A4988

  for (long i = 0; i < stepCount; i++) {
    digitalWrite(axis.stepPin, HIGH);
    delayMicroseconds(stepDelayMicros / 2);
    digitalWrite(axis.stepPin, LOW);
    delayMicroseconds(stepDelayMicros / 2);
  }

  // --- DISABLE DRIVERS AFTER MOVE (NO NOISE, NO HOLDING TORQUE) ---
  digitalWrite(EN_PIN, HIGH);  // disable all drivers

  Serial.println("Done. Drivers disabled.");
}
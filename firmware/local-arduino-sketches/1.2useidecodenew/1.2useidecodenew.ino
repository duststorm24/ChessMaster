// --- Single-axis homing + jog + soft limits ---
// CNC Shield V3 + A4988 + Arduino Uno
// Stepper on X slot (base rotation)
// Limit switch on X- (D9 assumed)
//
// Commands:
//   s=400   -> set speed to 400 steps/sec
//   +5      -> move +5 steps (clockwise, toward limit switch)
//   -100    -> move -100 steps (counter-clockwise, away from switch)
//   h       -> home: rotate CW until X- limit is pressed
//
// Soft limits:
//   position = 0 at home (at X- switch)
//   position decreases (negative) as you move away from home
//   position will never go below -10700
//   Positive moves stop early if the limit switch is hit and position is reset to 0.

const int EN_PIN      = 8;   // Enable (LOW = enable, HIGH = disable)
const int X_STEP      = 2;   // X STEP
const int X_DIR       = 5;   // X DIR
const int LIMIT_XMIN  = 9;   // X- limit switch (wired to X- on shield)

// Speed (steps per second)
long speed_sps = 400;              // default
const long MIN_SPEED_SPS = 10;
const long MAX_SPEED_SPS = 4000;

// Position tracking
// 0 = home at X- switch
// negative = away from home
long currentPos = 0;
const long NEG_LIMIT_STEPS = -10700;   // do not go below this

// Track limit state for logging
bool lastLimitPressed = false;

// ---------- Setup ----------

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, LOW);  // enable drivers

  pinMode(X_STEP, OUTPUT);
  pinMode(X_DIR, OUTPUT);
  digitalWrite(X_STEP, LOW);

  // Limit switch input.
  // Your wiring behaves inverted, so we treat HIGH = PRESSED.
  pinMode(LIMIT_XMIN, INPUT_PULLUP);

  lastLimitPressed = isLimitPressed();
  if (lastLimitPressed) {
    Serial.println("[LIMIT] X- PRESSED at startup");
  } else {
    Serial.println("[LIMIT] X- RELEASED at startup");
  }

  currentPos = 0;

  Serial.println("Single-axis base controller READY (with soft limits).");
  Serial.println("Commands:");
  Serial.println("  s=400   -> set speed to 400 steps/sec");
  Serial.println("  +5      -> move +5 steps (clockwise, toward switch)");
  Serial.println("  -100    -> move -100 steps (counter-clockwise, away)");
  Serial.println("  h       -> home: rotate CW until X- limit is pressed");
  Serial.println("Soft limits: 0 .. -10700 steps.");
}

// ---------- Helpers ----------

// Treat HIGH as "pressed"
bool isLimitPressed() {
  return (digitalRead(LIMIT_XMIN) == HIGH);
}

void stepOnce() {
  digitalWrite(X_STEP, HIGH);
  delayMicroseconds(2);
  digitalWrite(X_STEP, LOW);
}

// Move relative with:
// - soft limit at NEG_LIMIT_STEPS
// - auto-stop if moving + and switch is hit
void moveRelative(long steps) {
  if (steps == 0) return;

  // Clamp speed and compute delay
  if (speed_sps < MIN_SPEED_SPS) speed_sps = MIN_SPEED_SPS;
  if (speed_sps > MAX_SPEED_SPS) speed_sps = MAX_SPEED_SPS;
  unsigned long stepDelay = 1000000UL / (unsigned long)speed_sps; // us per step

  // Positive = toward switch (clockwise)
  if (steps > 0) {
    digitalWrite(X_DIR, HIGH);  // clockwise towards limit switch

    Serial.print("[MOVE] Request +");
    Serial.print(steps);
    Serial.print(" steps @ ");
    Serial.print(speed_sps);
    Serial.println(" sps (toward switch)");

    digitalWrite(EN_PIN, LOW); // enable

    long done = 0;
    while (done < steps) {
      // If we hit the switch, stop immediately and set position = 0
      if (isLimitPressed()) {
        currentPos = 0;
        Serial.print("[MOVE] X- LIMIT HIT after +");
        Serial.print(done);
        Serial.println(" steps. Position reset to 0.");
        return;
      }

      stepOnce();
      done++;
      currentPos++;   // moving toward switch = increasing position toward 0 (but it should never exceed 0)
      if (currentPos > 0) currentPos = 0; // just in case we overshoot due to any glitch
      delayMicroseconds(stepDelay);
    }

    Serial.print("[MOVE] Completed +");
    Serial.print(steps);
    Serial.print(" steps. Pos = ");
    Serial.println(currentPos);
  }
  // Negative = away from switch (counter-clockwise)
  else {
    long mag = -steps;  // magnitude of negative move

    // Compute where we would end up
    long targetPos = currentPos - mag;

    // Enforce soft limit: never go below NEG_LIMIT_STEPS
    if (targetPos < NEG_LIMIT_STEPS) {
      long maxAllowedSteps = currentPos - NEG_LIMIT_STEPS;
      if (maxAllowedSteps <= 0) {
        Serial.print("[SOFT LIMIT] Already at or past -");
        Serial.print(-NEG_LIMIT_STEPS);
        Serial.println(" limit. No negative motion allowed.");
        return;
      }
      Serial.print("[SOFT LIMIT] Requested -");
      Serial.print(mag);
      Serial.print(" steps would exceed limit. Clamping to -");
      Serial.print(maxAllowedSteps);
      Serial.println(" steps.");
      mag = maxAllowedSteps;
      targetPos = NEG_LIMIT_STEPS;
    }

    digitalWrite(X_DIR, LOW);   // counter-clockwise away from switch

    Serial.print("[MOVE] Request -");
    Serial.print(mag);
    Serial.print(" steps @ ");
    Serial.print(speed_sps);
    Serial.print(" sps (away from switch). Target pos = ");
    Serial.println(targetPos);

    digitalWrite(EN_PIN, LOW); // enable

    long done = 0;
    while (done < mag) {
      stepOnce();
      done++;
      currentPos--;   // going away from home
      if (currentPos < NEG_LIMIT_STEPS) {
        currentPos = NEG_LIMIT_STEPS;
        Serial.println("[SOFT LIMIT] Hit -10700 soft limit mid-move, stopping.");
        break;
      }
      delayMicroseconds(stepDelay);
    }

    Serial.print("[MOVE] Completed -");
    Serial.print(done);
    Serial.print(" steps. Pos = ");
    Serial.println(currentPos);
  }
}

// Home: rotate clockwise until limit switch is pressed
void homeAxis() {
  Serial.println("[HOME] Starting homing toward X- limit (clockwise).");

  // If already pressed, just reset pos
  if (isLimitPressed()) {
    currentPos = 0;
    Serial.println("[HOME] X- already pressed; position set to 0.");
    return;
  }

  digitalWrite(X_DIR, HIGH);  // clockwise toward switch

  if (speed_sps < MIN_SPEED_SPS) speed_sps = MIN_SPEED_SPS;
  if (speed_sps > MAX_SPEED_SPS) speed_sps = MAX_SPEED_SPS;
  unsigned long stepDelay = 1000000UL / (unsigned long)speed_sps;

  digitalWrite(EN_PIN, LOW); // enable

  const long MAX_STEPS = 200000; // safety cap
  long stepsTaken = 0;

  while (!isLimitPressed() && stepsTaken < MAX_STEPS) {
    stepOnce();
    stepsTaken++;
    // We don't really trust position during homing; we'll reset to 0 when we hit the switch.
    delayMicroseconds(stepDelay);
  }

  if (isLimitPressed()) {
    currentPos = 0;
    Serial.print("[HOME] X- reached after ");
    Serial.print(stepsTaken);
    Serial.println(" steps. Position set to 0.");
  } else {
    Serial.print("[HOME] FAILED: limit not reached after ");
    Serial.print(MAX_STEPS);
    Serial.println(" steps (check wiring or direction).");
  }
}

// ---------- Main loop ----------

void loop() {
  // Watch the limit and print on state changes
  bool nowPressed = isLimitPressed();
  if (nowPressed != lastLimitPressed) {
    lastLimitPressed = nowPressed;
    if (nowPressed) {
      Serial.println("[LIMIT] X- PRESSED");
    } else {
      Serial.println("[LIMIT] X- RELEASED");
    }
  }

  // Handle serial commands
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) return;

    // Homing
    if (line.equalsIgnoreCase("h")) {
      homeAxis();
      return;
    }

    // Speed: s=400
    if (line.charAt(0) == 's' || line.charAt(0) == 'S') {
      int eqIndex = line.indexOf('=');
      if (eqIndex != -1 && eqIndex < (int)line.length() - 1) {
        long newSpeed = line.substring(eqIndex + 1).toInt();
        if (newSpeed > 0) {
          if (newSpeed < MIN_SPEED_SPS) newSpeed = MIN_SPEED_SPS;
          if (newSpeed > MAX_SPEED_SPS) newSpeed = MAX_SPEED_SPS;
          speed_sps = newSpeed;
          Serial.print("[SPEED] speed_sps = ");
          Serial.print(speed_sps);
          Serial.println(" steps/sec");
        } else {
          Serial.println("[SPEED] Ignoring non-positive speed.");
        }
      } else {
        Serial.println("[SPEED] Use: s=400");
      }
      return;
    }

    // Otherwise: assume it’s a relative step command like +5 or -100
    long steps = line.toInt();  // handles + / - / digits
    if (steps == 0) {
      Serial.print("[CMD] Ignoring '");
      Serial.print(line);
      Serial.println("' (parsed as 0 steps)");
    } else {
      moveRelative(steps);
    }
  }
}
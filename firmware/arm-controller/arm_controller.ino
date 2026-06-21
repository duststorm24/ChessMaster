// === Chess Robot Arm – 3 Steppers + Limit Switches ===
// Control via Serial from the Raspberry Pi.
//
// Hardware mapping (CNC Shield V3 + Arduino Uno):
//   Stepper 1 (base rotation)  -> X driver  (STEP=2, DIR=5)
//   Stepper 2 (first arm)      -> Z driver  (STEP=4, DIR=7)
//   Stepper 3 (second arm)     -> Y driver  (STEP=3, DIR=6)
//
//   Limit switches wired to "X-", "Y-", "Z-" on the shield.
//   Typical CNC Shield v3 mapping (adjust if yours differs):
//     X- -> D9
//     Y- -> D11
//     Z- -> D13
//
//   Limit switches are assumed wired: pin --- switch --- GND
//   with internal pullups enabled (normal = HIGH, pressed = LOW).
//
// Serial protocol (from Pi or Serial Monitor):
//
//   H1        -> home stepper 1 (toward its limit switch)
//   H2        -> home stepper 2
//   H3        -> home stepper 3
//   HA        -> home ALL 3, one after another
//
//   M1 1000   -> move stepper 1 by +1000 steps
//   M2 -500   -> move stepper 2 by -500 steps
//   M3 200    -> move stepper 3 by +200 steps
//
//   V 800 600 400  -> set speed (steps/sec) for stepper1/2/3
//
//   Q         -> query status; prints one line:
//                POS 1:<p1> 2:<p2> 3:<p3> LIM X:<lx> Y:<ly> Z:<lz>
//
// Notes about homing directions (based on your description):
//   - Stepper 1: negative direction moves TOWARD its limit -> homeDir = -1
//   - Stepper 2: negative direction moves TOWARD its limit -> homeDir = -1
//   - Stepper 3: positive direction moves TOWARD its limit -> homeDir = +1
//
// You can tweak homeDir or pin numbers later if directions are flipped.

const int EN_PIN  = 8;   // Enable (LOW = enable, HIGH = disable) on most CNC shields

// STEP/DIR pins (CNC Shield defaults)
const int X_STEP = 2;
const int X_DIR  = 5;

const int Y_STEP = 3;
const int Y_DIR  = 6;

const int Z_STEP = 4;
const int Z_DIR  = 7;

// Limit switch pins – adjust if your shield is different
const int LIMIT_X_PIN = 9;   // X- endstop
const int LIMIT_Y_PIN = 11;  // Y- endstop
const int LIMIT_Z_PIN = 13;  // Z- endstop

// Homing safety cap: never step more than this while searching for a switch
const long MAX_HOMING_STEPS = 20000L;

// Default speeds (steps per second)
long speed1_sps = 800;
long speed2_sps = 800;
long speed3_sps = 800;

const long MIN_SPEED_SPS = 50;
const long MAX_SPEED_SPS = 4000;

bool estop = false;  // future use if you want an E-stop serial command

struct Axis {
  int stepPin;
  int dirPin;
  int limitPin;
  long position;   // steps from home (0 after homing)
  int homeDir;     // +1 or -1
  long speed_sps;  // current speed for this axis
};

// Mapping physical axes to drivers & limits:
//
// Axis 1 -> Stepper 1: base, X driver, X- limit
// Axis 2 -> Stepper 2: first arm, Z driver, Z- limit
// Axis 3 -> Stepper 3: second arm, Y driver, Y- limit
Axis axis1 = { X_STEP, X_DIR, LIMIT_X_PIN, 0L, -1, 800 };
Axis axis2 = { Z_STEP, Z_DIR, LIMIT_Z_PIN, 0L, -1, 800 };
Axis axis3 = { Y_STEP, Y_DIR, LIMIT_Y_PIN, 0L, +1, 800 };

void setupAxisPins(Axis &ax) {
  pinMode(ax.stepPin, OUTPUT);
  pinMode(ax.dirPin, OUTPUT);
  digitalWrite(ax.stepPin, LOW);
}

bool isLimitPressed(const Axis &ax) {
  // LOW == pressed if using INPUT_PULLUP
  return digitalRead(ax.limitPin) == LOW;
}

void stepOnce(Axis &ax, int dir) {
  digitalWrite(ax.dirPin, (dir > 0) ? HIGH : LOW);

  digitalWrite(ax.stepPin, HIGH);
  delayMicroseconds(3);
  digitalWrite(ax.stepPin, LOW);

  long speed_sps = ax.speed_sps;
  if (speed_sps < MIN_SPEED_SPS) speed_sps = MIN_SPEED_SPS;
  if (speed_sps > MAX_SPEED_SPS) speed_sps = MAX_SPEED_SPS;

  unsigned long interval_us = 1000000UL / (unsigned long)speed_sps;
  delayMicroseconds(interval_us);

  ax.position += (dir > 0) ? 1 : -1;
}

void homeAxis(Axis &ax, const char *label) {
  Serial.print("[HOME] Axis ");
  Serial.println(label);

  // If already pressed at startup, treat as home
  if (isLimitPressed(ax)) {
    ax.position = 0;
    Serial.print("[HOME] Axis ");
    Serial.print(label);
    Serial.println(" already at home (switch pressed).");
    return;
  }

  long steps = 0;
  while (!isLimitPressed(ax) && steps < MAX_HOMING_STEPS && !estop) {
    stepOnce(ax, ax.homeDir);
    steps++;
  }

  if (isLimitPressed(ax)) {
    ax.position = 0;
    Serial.print("[HOME] Axis ");
    Serial.print(label);
    Serial.print(" homed in ");
    Serial.print(steps);
    Serial.println(" steps.");
  } else {
    Serial.print("[HOME] Axis ");
    Serial.print(label);
    Serial.println(" failed (switch not hit).");
  }
}

void moveAxisSteps(Axis &ax, long steps, const char *label) {
  if (steps == 0) return;

  int dir = (steps > 0) ? +1 : -1;
  long remaining = (steps > 0) ? steps : -steps;

  Serial.print("[MOVE] Axis ");
  Serial.print(label);
  Serial.print(" steps=");
  Serial.println(steps);

  while (remaining > 0 && !estop) {
    // Optional: if moving toward the switch and it's already pressed, stop
    if ((dir == ax.homeDir) && isLimitPressed(ax)) {
      Serial.print("[MOVE] Axis ");
      Serial.print(label);
      Serial.println(" hit limit during move.");
      break;
    }

    stepOnce(ax, dir);
    remaining--;
  }
}

void printStatus() {
  int lx = isLimitPressed(axis1) ? 1 : 0;
  int ly = isLimitPressed(axis3) ? 1 : 0; // axis3 uses Y limit
  int lz = isLimitPressed(axis2) ? 1 : 0; // axis2 uses Z limit

  Serial.print("POS 1:");
  Serial.print(axis1.position);
  Serial.print(" 2:");
  Serial.print(axis2.position);
  Serial.print(" 3:");
  Serial.print(axis3.position);
  Serial.print(" LIM X:");
  Serial.print(lx);
  Serial.print(" Y:");
  Serial.print(ly);
  Serial.print(" Z:");
  Serial.println(lz);
}

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  digitalWrite(EN_PIN, LOW);    // enable all drivers

  // Limit pins with pullups
  pinMode(LIMIT_X_PIN, INPUT_PULLUP);
  pinMode(LIMIT_Y_PIN, INPUT_PULLUP);
  pinMode(LIMIT_Z_PIN, INPUT_PULLUP);

  setupAxisPins(axis1);
  setupAxisPins(axis2);
  setupAxisPins(axis3);

  Serial.println("=== Chess Robot 3-axis controller (Arduino) ===");
  Serial.println("Commands:");
  Serial.println("  H1 / H2 / H3 / HA");
  Serial.println("  M1 <steps> / M2 <steps> / M3 <steps>");
  Serial.println("  V <s1> <s2> <s3>   (steps/sec)");
  Serial.println("  Q   (query status)");
}

void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd.equalsIgnoreCase("Q")) {
    printStatus();
    return;
  }

  if (cmd.equalsIgnoreCase("HA")) {
    homeAxis(axis1, "1");
    homeAxis(axis2, "2");
    homeAxis(axis3, "3");
    printStatus();
    return;
  }

  if (cmd.equalsIgnoreCase("H1")) {
    homeAxis(axis1, "1");
    printStatus();
    return;
  }
  if (cmd.equalsIgnoreCase("H2")) {
    homeAxis(axis2, "2");
    printStatus();
    return;
  }
  if (cmd.equalsIgnoreCase("H3")) {
    homeAxis(axis3, "3");
    printStatus();
    return;
  }

  // Set speeds: V s1 s2 s3
  if (cmd.startsWith("V") || cmd.startsWith("v")) {
    cmd.remove(0, 1);
    cmd.trim();
    long s1 = axis1.speed_sps;
    long s2 = axis2.speed_sps;
    long s3 = axis3.speed_sps;

    int firstSpace = cmd.indexOf(' ');
    int secondSpace = cmd.indexOf(' ', firstSpace + 1);

    if (firstSpace == -1 || secondSpace == -1) {
      Serial.println("[V] Usage: V <s1> <s2> <s3>");
      return;
    }

    String s1str = cmd.substring(0, firstSpace);
    String s2str = cmd.substring(firstSpace + 1, secondSpace);
    String s3str = cmd.substring(secondSpace + 1);

    s1 = s1str.toInt();
    s2 = s2str.toInt();
    s3 = s3str.toInt();

    if (s1 < MIN_SPEED_SPS) s1 = MIN_SPEED_SPS;
    if (s1 > MAX_SPEED_SPS) s1 = MAX_SPEED_SPS;
    if (s2 < MIN_SPEED_SPS) s2 = MIN_SPEED_SPS;
    if (s2 > MAX_SPEED_SPS) s2 = MAX_SPEED_SPS;
    if (s3 < MIN_SPEED_SPS) s3 = MIN_SPEED_SPS;
    if (s3 > MAX_SPEED_SPS) s3 = MAX_SPEED_SPS;

    axis1.speed_sps = s1;
    axis2.speed_sps = s2;
    axis3.speed_sps = s3;

    Serial.print("[V] Speeds set: ");
    Serial.print("1=");
    Serial.print(s1);
    Serial.print(" 2=");
    Serial.print(s2);
    Serial.print(" 3=");
    Serial.println(s3);
    return;
  }

  // Move commands: M1 1000, M2 -500, M3 200
  if (cmd.startsWith("M") || cmd.startsWith("m")) {
    if (cmd.length() < 3) {
      Serial.println("[MOVE] Bad command");
      return;
    }
    char axisChar = cmd.charAt(1);
    int axisNum = axisChar - '0';
    if (axisNum < 1 || axisNum > 3) {
      Serial.println("[MOVE] Axis must be 1, 2 or 3");
      return;
    }

    String rest = cmd.substring(2);
    rest.trim();
    long steps = rest.toInt();

    if (axisNum == 1) {
      moveAxisSteps(axis1, steps, "1");
    } else if (axisNum == 2) {
      moveAxisSteps(axis2, steps, "2");
    } else if (axisNum == 3) {
      moveAxisSteps(axis3, steps, "3");
    }

    printStatus();
    return;
  }

  Serial.print("[WARN] Unknown command: ");
  Serial.println(cmd);
}

void loop() {
  // Read line-based commands from Serial
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    handleCommand(cmd);
  }
}

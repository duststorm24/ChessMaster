#include <AccelStepper.h>

// CNC Shield V3 standard pins for X/Y/Z
#define X_STEP_PIN 2
#define Y_STEP_PIN 3
#define Z_STEP_PIN 4
#define X_DIR_PIN  5
#define Y_DIR_PIN  6
#define Z_DIR_PIN  7
#define ENABLE_PIN 8
#define Z_LIMIT_PIN 11
#define Y_LIMIT_PIN 10
#define X_LIMIT_PIN 9
#define Z_MAX_DISTANCE_FROM_HOME 10000L
#define X_MAX_DISTANCE_FROM_HOME 9000L
#define Y_MAX_DISTANCE_FROM_HOME 9200L
#define X_ACCELERATION 2500.0
#define Y_ACCELERATION 2500.0
#define Z_ACCELERATION 2500.0
#define X_HOME_SPEED 1200
#define Y_HOME_SPEED 1200
#define Z_HOME_SPEED 400
#define MIN_COMMAND_SPEED 25
#define MAX_COMMAND_SPEED 3000
#define KEEP_DRIVERS_ENABLED 0

// A axis on D12/D13 for custom independent control
#define A_STEP_PIN 12
#define A_DIR_PIN  13

AccelStepper stepperX(AccelStepper::DRIVER, X_STEP_PIN, X_DIR_PIN);
AccelStepper stepperY(AccelStepper::DRIVER, Y_STEP_PIN, Y_DIR_PIN);
AccelStepper stepperZ(AccelStepper::DRIVER, Z_STEP_PIN, Z_DIR_PIN);
AccelStepper stepperA(AccelStepper::DRIVER, A_STEP_PIN, A_DIR_PIN);

bool activeX = false;
bool activeY = false;
bool activeZ = false;
bool activeA = false;
bool stepModeX = false;
bool stepModeY = false;
bool stepModeZ = false;
bool stepModeA = false;
bool accelModeX = false;
bool accelModeY = false;
bool accelModeZ = false;
bool homingZ = false;
bool zHomed = false;
bool yHomed = false;
bool xHomed = false;

unsigned long stopTimeX = 0;
unsigned long stopTimeY = 0;
unsigned long stopTimeZ = 0;
unsigned long stopTimeA = 0;

String inputLine = "";

void setDriversEnabled(bool enabled) {
  if (!enabled && KEEP_DRIVERS_ENABLED) {
    enabled = true;
  }
  digitalWrite(ENABLE_PIN, enabled ? LOW : HIGH);
}

bool isZLimitPressed();
bool isYLimitPressed();
bool isXLimitPressed();
long getXDistanceFromHome();
long getYDistanceFromHome();
long getZDistanceFromHome();
bool isYAtOrPastMaxDistance();
bool isZAtOrPastMaxDistance();
int resolveCommandSpeed(int requestedSpeed, int fallbackSpeed);
void startHoming(char axis, int speed);
void startHomingAll(int speedX, int speedY, int speedZ);
void processCommand(String cmd);
bool startAxis(char axis, int speed, int direction, int duration);
void stopAxis(String axis);
void readSerialCommands();
void runMotor(AccelStepper &stepper, bool &active, unsigned long &stopTime);
bool startAxisSteps(char axis, long steps, int direction, int speed);

void setup() {
  Serial.begin(115200);

  pinMode(ENABLE_PIN, OUTPUT);
  setDriversEnabled(KEEP_DRIVERS_ENABLED);
  pinMode(Z_LIMIT_PIN, INPUT_PULLUP);
  pinMode(Y_LIMIT_PIN, INPUT_PULLUP);
  pinMode(X_LIMIT_PIN, INPUT_PULLUP);

  if (isZLimitPressed()) {
    stepperZ.setCurrentPosition(0);
    zHomed = true;
  }
  if (isYLimitPressed()) {
    stepperY.setCurrentPosition(0);
    yHomed = true;
  }
  if (isXLimitPressed()) {
    stepperX.setCurrentPosition(0);
    xHomed = true;
  }

  stepperX.setMaxSpeed(3000);
  stepperY.setMaxSpeed(3000);
  stepperZ.setMaxSpeed(3000);
  stepperA.setMaxSpeed(3000);

  stepperX.setAcceleration(X_ACCELERATION);
  stepperY.setAcceleration(Y_ACCELERATION);
  stepperZ.setAcceleration(Z_ACCELERATION);
  stepperA.setAcceleration(100000);

  stepperX.setSpeed(0);
  stepperY.setSpeed(0);
  stepperZ.setSpeed(0);
  stepperA.setSpeed(0);
}

void loop() {
  readSerialCommands();
  runMotor(stepperX, activeX, stopTimeX);
  runMotor(stepperY, activeY, stopTimeY);
  runMotor(stepperZ, activeZ, stopTimeZ);
  runMotor(stepperA, activeA, stopTimeA);
}

void runMotor(AccelStepper &stepper, bool &active, unsigned long &stopTime) {
  if (!active) {
    return;
  }

  bool isXAxis = (&stepper == &stepperX);
  bool isYAxis = (&stepper == &stepperY);
  bool isZAxis = (&stepper == &stepperZ);

  bool inStepMode = (&stepper == &stepperX && stepModeX) ||
                    (&stepper == &stepperY && stepModeY) ||
                    (&stepper == &stepperZ && stepModeZ) ||
                    (&stepper == &stepperA && stepModeA);
  bool inAccelModeX = isXAxis && accelModeX;
  bool inAccelModeY = isYAxis && accelModeY;
  bool inAccelModeZ = isZAxis && accelModeZ;

  long commandedDelta = 0;
  if (inAccelModeX || inAccelModeY || inAccelModeZ || inStepMode) {
    commandedDelta = stepper.distanceToGo();
  }

  bool movingXTowardHome = isXAxis && ((inAccelModeX || stepModeX) ? (commandedDelta < 0) : (stepper.speed() < 0));
  bool movingXAwayFromHome = isXAxis && ((inAccelModeX || stepModeX) ? (commandedDelta > 0) : (stepper.speed() > 0));
  bool movingYTowardHome = isYAxis && ((inAccelModeY || stepModeY) ? (commandedDelta > 0) : (stepper.speed() > 0));
  bool movingYAwayFromHome = isYAxis && ((inAccelModeY || stepModeY) ? (commandedDelta < 0) : (stepper.speed() < 0));
  bool movingZForward = isZAxis && ((inAccelModeZ || stepModeZ) ? (commandedDelta > 0) : (stepper.speed() > 0));
  bool movingZAwayFromHome = isZAxis && ((inAccelModeZ || stepModeZ) ? (commandedDelta < 0) : (stepper.speed() < 0));

  bool hitXLimit = isXAxis && movingXTowardHome && isXLimitPressed();
  bool hitXMaxDistance = isXAxis && movingXAwayFromHome && xHomed && (stepperX.currentPosition() >= X_MAX_DISTANCE_FROM_HOME);
  bool hitYLimit = isYAxis && movingYTowardHome && isYLimitPressed();
  bool hitYMaxDistance = isYAxis && movingYAwayFromHome && yHomed && (stepperY.currentPosition() <= -Y_MAX_DISTANCE_FROM_HOME);
  bool hitZLimit = isZAxis && movingZForward && isZLimitPressed();
  bool hitZMaxDistance = isZAxis && movingZAwayFromHome && zHomed && (stepperZ.currentPosition() <= -Z_MAX_DISTANCE_FROM_HOME);

  bool timedOut = !(isZAxis && homingZ) && !inStepMode && !inAccelModeX && !inAccelModeY && !inAccelModeZ && (millis() >= stopTime);
  bool reachedTarget = (inStepMode || inAccelModeX || inAccelModeY || inAccelModeZ) && (stepper.distanceToGo() == 0);

  if (hitXLimit) {
    stepper.stop();
    stepper.setSpeed(0);
    active = false;
    stepModeX = false;
    accelModeX = false;
    stepperX.setCurrentPosition(0);
    xHomed = true;

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
  } else if (hitXMaxDistance) {
    stepper.stop();
    stepper.setSpeed(0);
    active = false;
    stepModeX = false;
    accelModeX = false;
    stepperX.setCurrentPosition(X_MAX_DISTANCE_FROM_HOME);

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
  } else if (hitYLimit) {
    stepper.stop();
    stepper.setSpeed(0);
    active = false;
    stepModeY = false;
    accelModeY = false;
    stepperY.setCurrentPosition(0);
    yHomed = true;

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
  } else if (hitYMaxDistance) {
    stepper.stop();
    stepper.setSpeed(0);
    active = false;
    stepModeY = false;
    accelModeY = false;
    stepperY.setCurrentPosition(-Y_MAX_DISTANCE_FROM_HOME);

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
  } else if (hitZLimit) {
    stepper.stop();
    stepper.setSpeed(0);
    active = false;
    stepperZ.setCurrentPosition(0);
    zHomed = true;
    stepModeZ = false;
    accelModeZ = false;
    homingZ = false;

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
  } else if (hitZMaxDistance) {
    stepper.stop();
    stepper.setSpeed(0);
    active = false;
    homingZ = false;
    stepModeZ = false;
    accelModeZ = false;
    stepperZ.setCurrentPosition(-Z_MAX_DISTANCE_FROM_HOME);

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
  } else if (timedOut || reachedTarget) {
    stepper.stop();
    stepper.setSpeed(0);
    active = false;

    if (&stepper == &stepperX) {
      stepModeX = false;
      accelModeX = false;
    }
    if (&stepper == &stepperY) {
      stepModeY = false;
      accelModeY = false;
    }
    if (&stepper == &stepperZ) {
      stepModeZ = false;
      accelModeZ = false;
      homingZ = false;
    }
    if (&stepper == &stepperA) {
      stepModeA = false;
    }

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
  } else {
    if (inAccelModeX || inAccelModeY || inAccelModeZ) {
      stepper.run();
    } else if (inStepMode) {
      stepper.runSpeedToPosition();
    } else {
      stepper.runSpeed();
    }
  }
}

void readSerialCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      processCommand(inputLine);
      inputLine = "";
    } else {
      inputLine += c;
    }
  }
}

bool isYLimitPressed() {
  return digitalRead(Y_LIMIT_PIN) == HIGH;
}

long getYDistanceFromHome() {
  long pos = stepperY.currentPosition();
  return pos >= 0 ? pos : -pos;
}

bool isXLimitPressed() {
  return digitalRead(X_LIMIT_PIN) == HIGH;
}

bool isZLimitPressed() {
  return digitalRead(Z_LIMIT_PIN) == HIGH;
}

long getXDistanceFromHome() {
  long pos = stepperX.currentPosition();
  return pos >= 0 ? pos : -pos;
}

long getZDistanceFromHome() {
  long pos = stepperZ.currentPosition();
  return pos >= 0 ? pos : -pos;
}

bool isZAtOrPastMaxDistance() {
  return zHomed && (getZDistanceFromHome() >= Z_MAX_DISTANCE_FROM_HOME);
}

bool isYAtOrPastMaxDistance() {
  return yHomed && (getYDistanceFromHome() >= Y_MAX_DISTANCE_FROM_HOME);
}

int resolveCommandSpeed(int requestedSpeed, int fallbackSpeed) {
  int safeSpeed = abs(requestedSpeed);
  if (safeSpeed <= 0) {
    safeSpeed = fallbackSpeed;
  }
  if (safeSpeed < MIN_COMMAND_SPEED) {
    safeSpeed = MIN_COMMAND_SPEED;
  }
  if (safeSpeed > MAX_COMMAND_SPEED) {
    safeSpeed = MAX_COMMAND_SPEED;
  }
  return safeSpeed;
}

void startHoming(char axis, int speed) {
  if (axis == 'X') {
    if (isXLimitPressed()) {
      stepperX.stop();
      stepperX.setSpeed(0);
      activeX = false;
      stepModeX = false;
      accelModeX = false;
      stepperX.setCurrentPosition(0);
      xHomed = true;

      if (!activeX && !activeY && !activeZ && !activeA) {
        setDriversEnabled(false);
      }
      Serial.println("OK");
      return;
    }

    setDriversEnabled(true);
    stepperX.setMaxSpeed(resolveCommandSpeed(speed, X_HOME_SPEED));
    stepperX.setAcceleration(X_ACCELERATION);
    stepperX.moveTo(stepperX.currentPosition() - 1000000L);
    activeX = true;
    stepModeX = false;
    accelModeX = true;
    stopTimeX = 4294967295UL;
    Serial.println("OK");
    return;
  }

  if (axis == 'Y') {
    if (isYLimitPressed()) {
      stepperY.stop();
      stepperY.setSpeed(0);
      activeY = false;
      stepModeY = false;
      accelModeY = false;
      stepperY.setCurrentPosition(0);
      yHomed = true;

      if (!activeX && !activeY && !activeZ && !activeA) {
        setDriversEnabled(false);
      }
      Serial.println("OK");
      return;
    }

    setDriversEnabled(true);
    stepperY.setMaxSpeed(resolveCommandSpeed(speed, Y_HOME_SPEED));
    stepperY.setAcceleration(Y_ACCELERATION);
    stepperY.moveTo(stepperY.currentPosition() + 1000000L);
    activeY = true;
    stepModeY = false;
    accelModeY = true;
    stopTimeY = 4294967295UL;
    Serial.println("OK");
    return;
  }

  if (axis != 'Z') {
    Serial.println("ERR");
    return;
  }

  if (isZLimitPressed()) {
    stepperZ.stop();
    stepperZ.setSpeed(0);
    activeZ = false;
    homingZ = false;
    stepModeZ = false;
    accelModeZ = false;
    stepperZ.setCurrentPosition(0);
    zHomed = true;

    if (!activeX && !activeY && !activeZ && !activeA) {
      setDriversEnabled(false);
    }
    Serial.println("OK");
    return;
  }

  setDriversEnabled(true);
  stepperZ.setMaxSpeed(resolveCommandSpeed(speed, Z_HOME_SPEED));
  stepperZ.setAcceleration(Z_ACCELERATION);
  stepperZ.moveTo(stepperZ.currentPosition() + 1000000L);
  activeZ = true;
  homingZ = true;
  stepModeZ = false;
  accelModeZ = true;
  stopTimeZ = 4294967295UL;
  Serial.println("OK");
}

void startHomingAll(int speedX, int speedY, int speedZ) {
  setDriversEnabled(true);

  if (isXLimitPressed()) {
    stepperX.stop();
    stepperX.setSpeed(0);
    activeX = false;
    stepModeX = false;
    accelModeX = false;
    stepperX.setCurrentPosition(0);
    xHomed = true;
  } else {
    stepperX.setMaxSpeed(resolveCommandSpeed(speedX, X_HOME_SPEED));
    stepperX.setAcceleration(X_ACCELERATION);
    stepperX.moveTo(stepperX.currentPosition() - 1000000L);
    activeX = true;
    stepModeX = false;
    accelModeX = true;
    stopTimeX = 4294967295UL;
  }

  if (isYLimitPressed()) {
    stepperY.stop();
    stepperY.setSpeed(0);
    activeY = false;
    stepModeY = false;
    accelModeY = false;
    stepperY.setCurrentPosition(0);
    yHomed = true;
  } else {
    stepperY.setMaxSpeed(resolveCommandSpeed(speedY, Y_HOME_SPEED));
    stepperY.setAcceleration(Y_ACCELERATION);
    stepperY.moveTo(stepperY.currentPosition() + 1000000L);
    activeY = true;
    stepModeY = false;
    accelModeY = true;
    stopTimeY = 4294967295UL;
  }

  if (isZLimitPressed()) {
    stepperZ.stop();
    stepperZ.setSpeed(0);
    activeZ = false;
    homingZ = false;
    stepModeZ = false;
    accelModeZ = false;
    stepperZ.setCurrentPosition(0);
    zHomed = true;
  } else {
    stepperZ.setMaxSpeed(resolveCommandSpeed(speedZ, Z_HOME_SPEED));
    stepperZ.setAcceleration(Z_ACCELERATION);
    stepperZ.moveTo(stepperZ.currentPosition() + 1000000L);
    activeZ = true;
    homingZ = true;
    stepModeZ = false;
    accelModeZ = true;
    stopTimeZ = 4294967295UL;
  }

  if (!activeX && !activeY && !activeZ && !activeA) {
    setDriversEnabled(false);
  }
}

bool startAxisSteps(char axis, long steps, int direction, int speed) {
  long requestedSteps = labs(steps);
  float signedSpeed = abs(speed) * (direction >= 0 ? 1 : -1);

  if (axis == 'X') {
    if (isXLimitPressed()) {
      stepperX.setCurrentPosition(0);
      xHomed = true;

      if (direction < 0) {
        stepperX.setSpeed(0);
        activeX = false;
        stepModeX = false;
        accelModeX = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    }

    if (!xHomed && direction > 0) {
      return false;
    }

    if (xHomed && direction > 0 && stepperX.currentPosition() >= X_MAX_DISTANCE_FROM_HOME) {
      stepperX.setCurrentPosition(X_MAX_DISTANCE_FROM_HOME);
      stepperX.setSpeed(0);
      activeX = false;
      stepModeX = false;
      accelModeX = false;

      if (!activeX && !activeY && !activeZ && !activeA) {
        setDriversEnabled(false);
      }
      return false;
    }

    setDriversEnabled(true);
    stepperX.setMaxSpeed(abs(speed));
    stepperX.setAcceleration(X_ACCELERATION);

    long targetPos = stepperX.currentPosition() + (direction >= 0 ? requestedSteps : -requestedSteps);
    if (targetPos > X_MAX_DISTANCE_FROM_HOME) {
      targetPos = X_MAX_DISTANCE_FROM_HOME;
    }

    stepperX.moveTo(targetPos);
    stopTimeX = 4294967295UL;
    activeX = true;
    stepModeX = false;
    accelModeX = true;
    return true;
  }

  if (axis == 'Y') {
    if (isYLimitPressed()) {
      stepperY.setCurrentPosition(0);
      yHomed = true;

      if (direction > 0) {
        stepperY.setSpeed(0);
        activeY = false;
        stepModeY = false;
        accelModeY = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    }

    if (!yHomed && direction < 0) {
      return false;
    }

    if (yHomed && direction < 0 && isYAtOrPastMaxDistance()) {
      stepperY.setCurrentPosition(-Y_MAX_DISTANCE_FROM_HOME);
      stepperY.setSpeed(0);
      activeY = false;
      stepModeY = false;
      accelModeY = false;

      if (!activeX && !activeY && !activeZ && !activeA) {
        setDriversEnabled(false);
      }
      return false;
    }

    setDriversEnabled(true);
    stepperY.setMaxSpeed(abs(speed));
    stepperY.setAcceleration(Y_ACCELERATION);

    long targetPos = stepperY.currentPosition() + (direction >= 0 ? requestedSteps : -requestedSteps);
    if (targetPos > 0) {
      targetPos = 0;
    }
    if (targetPos < -Y_MAX_DISTANCE_FROM_HOME) {
      targetPos = -Y_MAX_DISTANCE_FROM_HOME;
    }

    stepperY.moveTo(targetPos);
    stopTimeY = 4294967295UL;
    activeY = true;
    stepModeY = false;
    accelModeY = true;
    return true;
  }

  if (axis == 'Z') {
    if (isZLimitPressed()) {
      stepperZ.setCurrentPosition(0);
      zHomed = true;

      if (direction >= 0) {
        stepperZ.setSpeed(0);
        activeZ = false;
        homingZ = false;
        stepModeZ = false;
        accelModeZ = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    }

    if (!zHomed) {
      if (direction < 0) {
        stepperZ.setSpeed(0);
        activeZ = false;
        homingZ = false;
        stepModeZ = false;
        accelModeZ = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
      setDriversEnabled(true);
      stepperZ.setMaxSpeed(abs(speed));
      stepperZ.setAcceleration(Z_ACCELERATION);
      stepperZ.moveTo(stepperZ.currentPosition() + requestedSteps);
      stopTimeZ = 4294967295UL;
      activeZ = true;
      stepModeZ = false;
      accelModeZ = true;
      homingZ = false;
      return true;
    }

    if (direction >= 0) {
      long targetPos = stepperZ.currentPosition() + requestedSteps;
      if (targetPos > 0) {
        targetPos = 0;
      }

      setDriversEnabled(true);
      stepperZ.setMaxSpeed(abs(speed));
      stepperZ.setAcceleration(Z_ACCELERATION);
      stepperZ.moveTo(targetPos);
      stopTimeZ = 4294967295UL;
      activeZ = true;
      stepModeZ = false;
      accelModeZ = true;
      homingZ = false;
      return true;
    } else if (isZAtOrPastMaxDistance()) {
      stepperZ.setCurrentPosition(-Z_MAX_DISTANCE_FROM_HOME);
      stepperZ.setSpeed(0);
      activeZ = false;
      homingZ = false;
      stepModeZ = false;
      accelModeZ = false;

      if (!activeX && !activeY && !activeZ && !activeA) {
        setDriversEnabled(false);
      }
      return false;
    }

    long currentPos = stepperZ.currentPosition();
    long targetPos = currentPos - requestedSteps;
    if (targetPos < -Z_MAX_DISTANCE_FROM_HOME) {
      targetPos = -Z_MAX_DISTANCE_FROM_HOME;
    }

    setDriversEnabled(true);
    stepperZ.setMaxSpeed(abs(speed));
    stepperZ.setAcceleration(Z_ACCELERATION);
    stepperZ.moveTo(targetPos);
    stopTimeZ = 4294967295UL;
    activeZ = true;
    stepModeZ = false;
    accelModeZ = true;
    homingZ = false;
    return true;
  }

  long signedSteps = requestedSteps * (direction >= 0 ? 1L : -1L);

  setDriversEnabled(true);

  switch (axis) {
    case 'A':
      stepperA.setMaxSpeed(abs(speed));
      stepperA.move(signedSteps);
      stepperA.setSpeed(signedSpeed);
      activeA = true;
      stepModeA = true;
      return true;
  }

  if (!activeX && !activeY && !activeZ && !activeA) {
    setDriversEnabled(false);
  }

  return false;
}

void processCommand(String cmd) {
  cmd.trim();

  if (cmd.startsWith("MOVE_STEPS")) {
    char axis;
    long steps;
    int direction;
    int speed;

    int parsed = sscanf(cmd.c_str(), "MOVE_STEPS %c %ld %d %d", &axis, &steps, &direction, &speed);
    if (parsed == 4) {
      Serial.println(startAxisSteps(axis, steps, direction, speed) ? "OK" : "ERR");
    } else {
      Serial.println("ERR");
    }
  } else if (cmd.startsWith("MOVE")) {
    char axis;
    int speed;
    int direction;
    int duration;

    int parsed = sscanf(cmd.c_str(), "MOVE %c %d %d %d", &axis, &speed, &direction, &duration);
    if (parsed == 4) {
      Serial.println(startAxis(axis, speed, direction, duration) ? "OK" : "ERR");
    } else {
      Serial.println("ERR");
    }
  } else if (cmd.startsWith("STOP")) {
    char axis[8];
    int parsed = sscanf(cmd.c_str(), "STOP %7s", axis);
    if (parsed == 1) {
      stopAxis(String(axis));
      Serial.println("OK");
    } else {
      Serial.println("ERR");
    }
  } else if (cmd == "HOME ALL") {
    startHomingAll(0, 0, 0);
    Serial.println("OK");
  } else if (cmd.startsWith("HOME ALL")) {
    int speedX;
    int speedY;
    int speedZ;
    int parsed = sscanf(cmd.c_str(), "HOME ALL %d %d %d", &speedX, &speedY, &speedZ);
    if (parsed == 3) {
      startHomingAll(speedX, speedY, speedZ);
      Serial.println("OK");
    } else {
      Serial.println("ERR");
    }
  } else if (cmd.startsWith("HOME")) {
    char axis;
    int speed;
    int parsed = sscanf(cmd.c_str(), "HOME %c %d", &axis, &speed);
    if (parsed == 2) {
      startHoming(axis, speed);
    } else if (sscanf(cmd.c_str(), "HOME %c", &axis) == 1) {
      startHoming(axis, 0);
    } else {
      Serial.println("ERR");
    }
  } else if (cmd == "STATUS") {
    Serial.print("LIMIT_Z=");
    Serial.println(isZLimitPressed() ? 1 : 0);
  } else if (cmd == "LIMITS") {
    Serial.print("LIMIT_X=");
    Serial.print(isXLimitPressed() ? 1 : 0);
    Serial.print(";");
    Serial.print("LIMIT_Y=");
    Serial.print(isYLimitPressed() ? 1 : 0);
    Serial.print(";");
    Serial.print("LIMIT_Z=");
    Serial.print(isZLimitPressed() ? 1 : 0);
    Serial.print(";");
    Serial.println("LIMIT_A=INACTIVE");
  } else if (cmd == "DIST X") {
    if (xHomed) {
      Serial.print("DIST_X=");
      Serial.println(getXDistanceFromHome());
    } else {
      Serial.println("DIST_X=UNKNOWN");
    }
  } else if (cmd == "DIST Y") {
    if (yHomed) {
      Serial.print("DIST_Y=");
      Serial.println(getYDistanceFromHome());
    } else {
      Serial.println("DIST_Y=UNKNOWN");
    }
  } else if (cmd == "DIST Z") {
    if (zHomed) {
      Serial.print("DIST_Z=");
      Serial.println(getZDistanceFromHome());
    } else {
      Serial.println("DIST_Z=UNKNOWN");
    }
  } else {
    Serial.println("ERR");
  }
}

bool startAxis(char axis, int speed, int direction, int duration) {
  if (axis == 'X') {
    if (isXLimitPressed()) {
      stepperX.setCurrentPosition(0);
      xHomed = true;

      if (direction < 0) {
        stepperX.stop();
        stepperX.setSpeed(0);
        activeX = false;
        accelModeX = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    }

    if (!xHomed && direction > 0) {
      return false;
    }

    if (xHomed && direction > 0 && stepperX.currentPosition() >= X_MAX_DISTANCE_FROM_HOME) {
      stepperX.setCurrentPosition(X_MAX_DISTANCE_FROM_HOME);
      stepperX.stop();
      stepperX.setSpeed(0);
      activeX = false;
      accelModeX = false;

      if (!activeX && !activeY && !activeZ && !activeA) {
        setDriversEnabled(false);
      }
      return false;
    }
  }

  if (axis == 'Y') {
    if (isYLimitPressed()) {
      stepperY.setCurrentPosition(0);
      yHomed = true;

      if (direction > 0) {
        stepperY.stop();
        stepperY.setSpeed(0);
        activeY = false;
        accelModeY = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    }

    if (!yHomed && direction < 0) {
      return false;
    }

    if (yHomed && direction < 0 && isYAtOrPastMaxDistance()) {
      stepperY.setCurrentPosition(-Y_MAX_DISTANCE_FROM_HOME);
      stepperY.stop();
      stepperY.setSpeed(0);
      activeY = false;
      accelModeY = false;

      if (!activeX && !activeY && !activeZ && !activeA) {
        setDriversEnabled(false);
      }
      return false;
    }
  }

  if (axis == 'Z') {
    if (isZLimitPressed()) {
      stepperZ.setCurrentPosition(0);
      zHomed = true;

      if (direction >= 0) {
        stepperZ.stop();
        stepperZ.setSpeed(0);
        activeZ = false;
        homingZ = false;
        accelModeZ = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    }

    if (!zHomed) {
      if (direction < 0) {
        stepperZ.stop();
        stepperZ.setSpeed(0);
        activeZ = false;
        homingZ = false;
        accelModeZ = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    } else {
      if (isZAtOrPastMaxDistance() && direction < 0) {
        stepperZ.setCurrentPosition(-Z_MAX_DISTANCE_FROM_HOME);
        stepperZ.stop();
        stepperZ.setSpeed(0);
        activeZ = false;
        homingZ = false;
        accelModeZ = false;

        if (!activeX && !activeY && !activeZ && !activeA) {
          setDriversEnabled(false);
        }
        return false;
      }
    }
  }

  setDriversEnabled(true);
  unsigned long stopAt = millis() + duration;

  switch (axis) {
    case 'X':
      stepperX.setMaxSpeed(abs(speed));
      stepperX.setAcceleration(X_ACCELERATION);
      stepperX.moveTo(stepperX.currentPosition() + (direction >= 0 ? 1000000L : -1000000L));
      stopTimeX = stopAt;
      activeX = true;
      stepModeX = false;
      accelModeX = true;
      return true;
    case 'Y':
      stepperY.setMaxSpeed(abs(speed));
      stepperY.setAcceleration(Y_ACCELERATION);
      if (yHomed && direction >= 0) {
        stepperY.moveTo(0);
      } else {
        stepperY.moveTo(stepperY.currentPosition() + (direction >= 0 ? 1000000L : -1000000L));
      }
      stopTimeY = stopAt;
      activeY = true;
      stepModeY = false;
      accelModeY = true;
      return true;
    case 'Z':
      stepperZ.setMaxSpeed(abs(speed));
      stepperZ.setAcceleration(Z_ACCELERATION);
      if (zHomed && direction >= 0) {
        stepperZ.moveTo(0);
      } else {
        stepperZ.moveTo(stepperZ.currentPosition() + (direction >= 0 ? 1000000L : -1000000L));
      }
      stopTimeZ = stopAt;
      activeZ = true;
      stepModeZ = false;
      accelModeZ = true;
      homingZ = false;
      return true;
    case 'A':
      stepperA.setSpeed(abs(speed) * (direction >= 0 ? 1 : -1));
      stopTimeA = stopAt;
      activeA = true;
      stepModeA = false;
      return true;
  }

  if (!activeX && !activeY && !activeZ && !activeA) {
    setDriversEnabled(false);
  }

  return false;
}

void stopAxis(String axis) {
  axis.trim();

  if (axis == "X" || axis == "ALL") {
    stepperX.stop();
    stepperX.setSpeed(0);
    activeX = false;
    stepModeX = false;
    accelModeX = false;
  }
  if (axis == "Y" || axis == "ALL") {
    stepperY.stop();
    stepperY.setSpeed(0);
    activeY = false;
    stepModeY = false;
    accelModeY = false;
  }
  if (axis == "Z" || axis == "ALL") {
    stepperZ.stop();
    stepperZ.setSpeed(0);
    activeZ = false;
    stepModeZ = false;
    accelModeZ = false;
    homingZ = false;
  }
  if (axis == "A" || axis == "ALL") {
    stepperA.setSpeed(0);
    activeA = false;
    stepModeA = false;
  }

  if (!activeX && !activeY && !activeZ && !activeA) {
    setDriversEnabled(false);
  }
}

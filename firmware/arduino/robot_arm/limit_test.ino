// Minimal limit switch tester for CNC shield
// X- (D9), Y- (D10), Z- (D11)

const int X_LIMIT_PIN = 9;
const int Y_LIMIT_PIN = 10;
const int Z_LIMIT_PIN = 11;

void setup() {
  Serial.begin(115200);

  // IMPORTANT: plain INPUT – the CNC shield already has pull-down or conditioning.
  pinMode(X_LIMIT_PIN, INPUT);
  pinMode(Y_LIMIT_PIN, INPUT);
  pinMode(Z_LIMIT_PIN, INPUT);

  Serial.println("Limit test READY. Raw pin reads every 200 ms.");
  Serial.println("0 = not pressed, 1 = pressed (based on what we observed earlier).");
}

void loop() {
  int rx = digitalRead(X_LIMIT_PIN);
  int ry = digitalRead(Y_LIMIT_PIN);
  int rz = digitalRead(Z_LIMIT_PIN);

  Serial.print("RAW X=");
  Serial.print(rx);
  Serial.print(" Y=");
  Serial.print(ry);
  Serial.print(" Z=");
  Serial.println(rz);

  delay(200);
}

// Weapon firmware (ESP32-C3): bow_raw.ino hardware + the game network.
//
//   IR sensor sees the opponent  -> "on target": motor bursts, helmet is told ("aim")
//   NFC tag read                 -> a shot. Hit if on target at that moment, otherwise miss.
//
// >>> Set PLAYER before uploading: 1 = bow, 2 = crossbow. <<<
#define PLAYER 1

#include <WiFi.h>
#include <WiFiUdp.h>
#include <Wire.h>
#include <Adafruit_PN532.h>

// ---------------- settings ----------------
const char* WIFI_NAME = "Triet1";
const char* WIFI_PASS = "trietbill";

const IPAddress EVERYONE(255, 255, 255, 255);
const uint16_t GAME_PORT = 4210;           // same as GAME_PORT in helmet_game.py

// PN532 I2C pins
#define PN532_SDA  8
#define PN532_SCL  9
#define PN532_IRQ  2

// IR and motor pins
#define IR_PIN     4
#define MOTOR_PIN  3

// Set false if your IR output goes HIGH when triggered
const bool IR_ACTIVE_LOW = true;

// Motor burst settings (played once each time the weapon comes on target)
const int BURST_COUNT = 3;
const unsigned long BURST_ON_MS = 300;
const unsigned long BURST_OFF_MS = 100;

// How the game reads the hardware. Change these if the behaviour is not what you want.
const bool SHOT_WHEN_TAG_LEAVES = false;   // false = shot when the NFC tag is read, true = shot when the tag goes away
const bool USE_CAMERA_LOCK = false;        // true = the helmet camera's "on target" also counts for a hit
const unsigned long IR_HOLD_MS = 300;      // stay "on target" this long after the last IR pulse
const unsigned long TAG_GONE_MS = 400;     // tag must be unread this long before it counts as gone
const unsigned long SHOT_COOLDOWN_MS = 500;  // shortest time between two shots
const unsigned long AIM_SEND_MS = 150;     // tell the helmet "on target" this often

// ---------------- state ----------------
Adafruit_PN532 nfc(PN532_IRQ, -1, &Wire);
WiFiUDP udp;

bool nfcReady = false;
bool tagPresent = false;
unsigned long lastTagSeen = 0;
unsigned long lastShot = 0;
uint32_t shotNumber = 0;

volatile unsigned long lastIRSeen = 0;
volatile bool irEverSeen = false;
bool wasAimed = false;
unsigned long lastAimSent = 0;

unsigned long lockedUntil = 0;             // helmet camera lock, from "cue" messages
unsigned long lastHello = 0;

int burstsLeft = 0;
bool motorOn = false;
unsigned long motorChanged = 0;

// ---------------- network ----------------
void sendLine(const char* text) {
  udp.beginPacket(EVERYONE, GAME_PORT);
  udp.print(text);
  udp.endPacket();
}

// "shot <player> <1/0> <n>", sent three times; helmets and the dashboard ignore repeats.
void reportShot(bool hit) {
  shotNumber++;
  char msg[32];
  snprintf(msg, sizeof(msg), "shot %d %d %lu", PLAYER, hit ? 1 : 0, (unsigned long)shotNumber);
  for (int i = 0; i < 3; i++) {
    sendLine(msg);
    delay(15);
  }
  Serial.println(hit ? "SHOT: HIT" : "SHOT: miss");
}

// Reads this player's helmet: "cue <player> U|D|C|N <on target 0/1>".
void readHelmet() {
  while (udp.parsePacket() > 0) {
    char buf[64];
    int n = udp.read(buf, sizeof(buf) - 1);
    if (n <= 0) continue;
    buf[n] = 0;
    int who, locked;
    char tilt;
    if (sscanf(buf, "cue %d %c %d", &who, &tilt, &locked) == 3 && who == PLAYER) {
      lockedUntil = locked ? millis() + 400 : 0;
    }
  }
}

// ---------------- aiming ----------------
void IRAM_ATTR onIRPulse() {
  lastIRSeen = millis();
  irEverSeen = true;
}

bool isIRTriggered() {
  int value = digitalRead(IR_PIN);
  return IR_ACTIVE_LOW ? (value == LOW) : (value == HIGH);
}

bool irOnTarget() {
  return irEverSeen && (millis() - lastIRSeen < IR_HOLD_MS);
}

bool cameraOnTarget() {
  return millis() < lockedUntil;
}

bool aimedAtOpponent() {
  return irOnTarget() || (USE_CAMERA_LOCK && cameraOnTarget());
}

// ---------------- motor (does not block the rest of the program) ----------------
void startBursts() {
  if (burstsLeft > 0) return;              // already playing
  burstsLeft = BURST_COUNT;
  motorOn = true;
  motorChanged = millis();
  digitalWrite(MOTOR_PIN, HIGH);
}

void updateMotor() {
  if (burstsLeft == 0) return;
  unsigned long now = millis();
  if (motorOn && now - motorChanged >= BURST_ON_MS) {
    digitalWrite(MOTOR_PIN, LOW);
    motorOn = false;
    motorChanged = now;
    burstsLeft--;
  } else if (!motorOn && now - motorChanged >= BURST_OFF_MS) {
    digitalWrite(MOTOR_PIN, HIGH);
    motorOn = true;
    motorChanged = now;
  }
}

// ---------------- shots ----------------
void fireShot() {
  unsigned long now = millis();
  if (lastShot != 0 && now - lastShot < SHOT_COOLDOWN_MS) return;
  lastShot = now;
  reportShot(aimedAtOpponent());
}

void checkNFC() {
  if (!nfcReady) return;

  uint8_t uid[7];
  uint8_t uidLength = 0;
  bool found = nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLength, 20);
  unsigned long now = millis();

  if (found) {
    lastTagSeen = now;
    if (!tagPresent) {
      tagPresent = true;
      Serial.print("NFC UID: ");
      for (uint8_t i = 0; i < uidLength; i++) {
        if (uid[i] < 0x10) Serial.print('0');
        Serial.print(uid[i], HEX);
        if (i < uidLength - 1) Serial.print(':');
      }
      Serial.println();
      if (!SHOT_WHEN_TAG_LEAVES) fireShot();
    }
  } else if (tagPresent && now - lastTagSeen > TAG_GONE_MS) {
    tagPresent = false;
    Serial.println("NFC tag gone");
    if (SHOT_WHEN_TAG_LEAVES) fireShot();
  }
}

// ---------------- setup ----------------
void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(IR_PIN, INPUT);
  pinMode(MOTOR_PIN, OUTPUT);
  digitalWrite(MOTOR_PIN, LOW);
  attachInterrupt(digitalPinToInterrupt(IR_PIN), onIRPulse, IR_ACTIVE_LOW ? FALLING : RISING);

  Wire.begin(PN532_SDA, PN532_SCL);
  Serial.printf("Player %d weapon: PN532 + IR + motor + game network\n", PLAYER);

  nfc.begin();
  if (nfc.getFirmwareVersion()) {
    nfc.SAMConfig();
    nfcReady = true;
    Serial.println("PN532 detected.");
  } else {
    Serial.println("ERROR: PN532 not detected. Check wiring and I2C mode.");
    Serial.println("Carrying on without it: shots only from the Serial Monitor (h, m, t).");
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // lower latency
  WiFi.begin(WIFI_NAME, WIFI_PASS);
  Serial.print("Joining Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  udp.begin(GAME_PORT);
  Serial.printf("\nPlayer %d weapon online, IP %s\nReady.\n", PLAYER, WiFi.localIP().toString().c_str());
}

// ---------------- loop ----------------
void loop() {
  unsigned long now = millis();

  readHelmet();

  // IR: the interrupt catches short pulses; this catches a steady signal.
  if (isIRTriggered()) {
    lastIRSeen = now;
    irEverSeen = true;
  }

  // Coming on target: three motor bursts, once. While on target, keep telling the helmet.
  bool aimed = irOnTarget();
  if (aimed && !wasAimed) {
    Serial.println("IR: on target");
    startBursts();
  }
  if (!aimed && wasAimed) Serial.println("IR: lost target");
  if (aimed != wasAimed || (aimed && now - lastAimSent >= AIM_SEND_MS)) {
    char msg[16];
    snprintf(msg, sizeof(msg), "aim %d %d", PLAYER, aimed ? 1 : 0);
    sendLine(msg);
    lastAimSent = now;
  }
  wasAimed = aimed;

  updateMotor();
  checkNFC();

  // Test without hardware: h = hit, m = miss, t = a real shot using the current aim.
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 'h') reportShot(true);
    if (c == 'm') reportShot(false);
    if (c == 't') fireShot();
  }

  // Heartbeat so the dashboard can show this weapon as online.
  if (now - lastHello > 2000) {
    lastHello = now;
    char msg[24];
    snprintf(msg, sizeof(msg), "hello %d weapon", PLAYER);
    sendLine(msg);
  }

  delay(5);
}

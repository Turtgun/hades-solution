#include <Wire.h>
#include <Adafruit_PN532.h>

// PN532 I2C pins
#define PN532_SDA  8
#define PN532_SCL  9
#define PN532_IRQ  2

// IR and motor pins
#define IR_PIN     4
#define MOTOR_PIN  3

Adafruit_PN532 nfc(PN532_IRQ, -1, &Wire);

// Set false if your IR output goes HIGH when triggered
const bool IR_ACTIVE_LOW = true;

// Motor burst settings
const int BURST_COUNT = 3;
const unsigned long BURST_ON_MS = 300;
const unsigned long BURST_OFF_MS = 100;

bool lastIRTriggered = false;

void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(IR_PIN, INPUT);
  pinMode(MOTOR_PIN, OUTPUT);
  digitalWrite(MOTOR_PIN, LOW);

  Wire.begin(PN532_SDA, PN532_SCL);

  Serial.println("ESP32-C3: PN532 + IR + Motor bursts");

  nfc.begin();

  uint32_t version = nfc.getFirmwareVersion();

  if (!version) {
    Serial.println("ERROR: PN532 not detected. Check wiring and I2C mode.");
    while (true) {
      digitalWrite(MOTOR_PIN, LOW);
      delay(100);
    }
  }

  Serial.println("PN532 detected.");
  nfc.SAMConfig();

  Serial.println("Ready.");
}

bool isIRTriggered() {
  int value = digitalRead(IR_PIN);
  return IR_ACTIVE_LOW ? (value == LOW) : (value == HIGH);
}

void runMotorBursts() {
  Serial.println("IR triggered: 3 motor bursts");

  for (int i = 0; i < BURST_COUNT; i++) {
    digitalWrite(MOTOR_PIN, HIGH);
    delay(BURST_ON_MS);

    digitalWrite(MOTOR_PIN, LOW);
    delay(BURST_OFF_MS);
  }

  Serial.println("Bursts complete.");
}

void loop() {
  // IR sensor triggers three bursts once per detection
  bool irTriggered = isIRTriggered();

  if (irTriggered && !lastIRTriggered) {
    runMotorBursts();
  }

  lastIRTriggered = irTriggered;

  // NFC reads and prints the UID only
  uint8_t uid[7];
  uint8_t uidLength = 0;

  if (nfc.readPassiveTargetID(
        PN532_MIFARE_ISO14443A,
        uid,
        &uidLength,
        20)) {

    Serial.print("NFC UID: ");

    for (uint8_t i = 0; i < uidLength; i++) {
      if (uid[i] < 0x10) Serial.print('0');
      Serial.print(uid[i], HEX);
      if (i < uidLength - 1) Serial.print(':');
    }

    Serial.println();
    delay(300);
  }

  delay(10);
}
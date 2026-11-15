// Weapon firmware (ESP32), speaking the same messages as helmet_game.py.
//
// >>> Set PLAYER before uploading: 1 = bow, 2 = crossbow. <<<
#define PLAYER 1

#include <WiFi.h>
#include <WiFiUdp.h>

const char* WIFI_NAME = "Triet1";
const char* WIFI_PASS = "trietbill";

const IPAddress EVERYONE(255, 255, 255, 255);
const uint16_t GAME_PORT = 4210;           // same as GAME_PORT in helmet_game.py

WiFiUDP udp;
uint32_t shotNumber = 0;
unsigned long lastHello = 0;
unsigned long lockedUntil = 0;

void sendLine(const char* text) {
  udp.beginPacket(EVERYONE, GAME_PORT);
  udp.print(text);
  udp.endPacket();
}

// True while this player's helmet reports the opponent's tag is centred
// (the last number of its "cue <player> U|D|C|N <0/1>" message).
bool onTarget() {
  return millis() < lockedUntil;
}

// Call this once per shot: true = hit, false = miss.
// Sends "shot <player> <1/0> <n>" three times; helmets and the UNO Q ignore repeats.
// The opponent's helmet buzzes on a hit, and the UNO Q updates the score.
void reportShot(bool hit) {
  shotNumber++;
  char msg[32];
  snprintf(msg, sizeof(msg), "shot %d %d %lu", PLAYER, hit ? 1 : 0, (unsigned long)shotNumber);
  for (int i = 0; i < 3; i++) {
    sendLine(msg);
    delay(15);
  }
  Serial.println(msg);
}

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

void setup() {
  Serial.begin(115200);

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // lower latency
  WiFi.begin(WIFI_NAME, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print(".");
  }
  udp.begin(GAME_PORT);
  Serial.printf("\nPlayer %d weapon online, IP %s\n", PLAYER, WiFi.localIP().toString().c_str());
}

void loop() {
  readHelmet();

  // ===== YOUR HIT/MISS DETECTION GOES HERE =====
  // When the trigger fires, call reportShot(true) or reportShot(false).
  // If "hit" means the helmet camera was locked on when you fired:
  //     reportShot(onTarget());

  // Test without sensors: type h (hit), m (miss) or t (use the helmet's lock) in the Serial Monitor.
  if (Serial.available()) {
    char c = Serial.read();
    if (c == 'h') reportShot(true);
    if (c == 'm') reportShot(false);
    if (c == 't') reportShot(onTarget());
  }

  // Heartbeat so the dashboard can show this weapon as online.
  if (millis() - lastHello > 2000) {
    lastHello = millis();
    char msg[24];
    snprintf(msg, sizeof(msg), "hello %d weapon", PLAYER);
    sendLine(msg);
  }
}

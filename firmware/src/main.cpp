// Serial IR modem: sends NEC frames on request and reports frames it hears.
// It knows nothing about the amp; the host decides what to send.
//
// Line protocol, 115200 baud, '\n'-terminated (a trailing '\r' is ignored):
//   host -> ping                 device -> pong amp-ir-serial <version>
//   host -> nec <addr> <cmd>     device -> ok | err <reason>
//   device -> ready amp-ir-serial <version>   (once, at boot)
//   device -> rx nec <addr> <cmd>             (whenever a frame is received)
// <addr> and <cmd> are 4 hex digits in ESPHome's NEC convention: the low byte
// goes on the air first, so the Onkyo power toggle is "nec 6CD2 34CB".
// Nothing is transmitted except in reply to a "nec" command.

#include <Arduino.h>
#include <IRrecv.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRutils.h>

static const char *VERSION = "1";
static const uint16_t PIN_IR_RX = 32;  // IR Unit receiver (tested 2026-10-29)
static const uint16_t PIN_IR_TX = 26;  // IR Unit emitter, not inverted
static const size_t MAX_LINE = 64;

IRsend irsend(PIN_IR_TX);
IRrecv irrecv(PIN_IR_RX);
decode_results results;

static char line[MAX_LINE + 1];
static size_t lineLen = 0;
static bool lineOverflow = false;

static uint8_t reverseBits(uint8_t b) {
  b = (b & 0xF0) >> 4 | (b & 0x0F) << 4;
  b = (b & 0xCC) >> 2 | (b & 0x33) << 2;
  b = (b & 0xAA) >> 1 | (b & 0x55) << 1;
  return b;
}

// IRremoteESP8266 holds NEC frames MSB-first as received; ESPHome reports the
// four bytes LSB-first. Convert between the two one byte at a time.
static uint32_t toRaw(uint16_t addr, uint16_t cmd) {
  return (uint32_t)reverseBits(addr & 0xFF) << 24 |
         (uint32_t)reverseBits(addr >> 8) << 16 |
         (uint32_t)reverseBits(cmd & 0xFF) << 8 |
         (uint32_t)reverseBits(cmd >> 8);
}

static void fromRaw(uint32_t raw, uint16_t *addr, uint16_t *cmd) {
  *addr = reverseBits(raw >> 24) | reverseBits(raw >> 16) << 8;
  *cmd = reverseBits(raw >> 8) | reverseBits(raw) << 8;
}

static bool parseHex16(const char *s, uint16_t *out) {
  if (strlen(s) != 4) return false;
  uint16_t v = 0;
  for (int i = 0; i < 4; i++) {
    char c = s[i];
    v <<= 4;
    if (c >= '0' && c <= '9') v |= c - '0';
    else if (c >= 'a' && c <= 'f') v |= c - 'a' + 10;
    else if (c >= 'A' && c <= 'F') v |= c - 'A' + 10;
    else return false;
  }
  *out = v;
  return true;
}

static void handleLine(char *s) {
  char *verb = strtok(s, " ");
  if (verb == nullptr) return;  // blank line
  if (strcmp(verb, "ping") == 0) {
    if (strtok(nullptr, " ") != nullptr) {
      Serial.println("err ping takes no arguments");
      return;
    }
    Serial.printf("pong amp-ir-serial %s\n", VERSION);
  } else if (strcmp(verb, "nec") == 0) {
    char *a = strtok(nullptr, " ");
    char *c = strtok(nullptr, " ");
    uint16_t addr, cmd;
    if (a == nullptr || c == nullptr || strtok(nullptr, " ") != nullptr) {
      Serial.println("err usage: nec <addr> <cmd>");
      return;
    }
    if (!parseHex16(a, &addr) || !parseHex16(c, &cmd)) {
      Serial.println("err addr and cmd must be 4 hex digits");
      return;
    }
    // Stop listening while sending so the emitter's own frame isn't reported.
    irrecv.disableIRIn();
    irsend.sendNEC(toRaw(addr, cmd), kNECBits, 0);
    irrecv.enableIRIn();
    Serial.println("ok");
  } else {
    Serial.printf("err unknown command %s\n", verb);
  }
}

void setup() {
  Serial.begin(115200);
  irsend.begin();
  irrecv.enableIRIn();
  Serial.printf("\nready amp-ir-serial %s\n", VERSION);
}

void loop() {
  while (Serial.available() > 0) {
    char ch = Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') {
      if (lineOverflow) {
        Serial.printf("err line longer than %u characters\n", (unsigned)MAX_LINE);
      } else {
        line[lineLen] = '\0';
        handleLine(line);
      }
      lineLen = 0;
      lineOverflow = false;
    } else if (lineLen < MAX_LINE) {
      line[lineLen++] = ch;
    } else {
      lineOverflow = true;
    }
  }

  if (irrecv.decode(&results)) {
    if (results.decode_type == NEC && !results.repeat) {
      uint16_t addr, cmd;
      fromRaw(results.value, &addr, &cmd);
      Serial.printf("rx nec %04X %04X\n", addr, cmd);
    }
    irrecv.resume();
  }
}

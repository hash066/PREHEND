// esp32_bridge.ino -- read-only telemetry relay + optional local logging
// Transcribed from SIGNAL_ADAPT_hardware_map_and_firmware.pdf Section 8.
// Cannot send anything into the Arduino's control loop: it only forwards what it
// receives over serial to a simple web page, and appends rows to local flash for
// the ADAPT covariate pipeline (adapt/session_logger.py) to consume later.
#include <WiFi.h>
#include <WebServer.h>
#include <SPIFFS.h>

WebServer server(80);
String lastLine = "";
File logFile;

void handleRoot() { server.send(200, "text/plain", lastLine); }

void setup() {
  Serial.begin(115200);  // from Arduino TX
  WiFi.begin("yourSSID", "yourPASS");
  while (WiFi.status() != WL_CONNECTED) delay(300);
  SPIFFS.begin(true);
  logFile = SPIFFS.open("/session_log.csv", FILE_APPEND);
  server.on("/", handleRoot);
  server.begin();
}

void loop() {
  if (Serial.available()) {
    lastLine = Serial.readStringUntil('\n');
    if (logFile) logFile.println(lastLine);  // append for ADAPT host pickup
  }
  server.handleClient();
}

// Example only: GPIO 34 and 35 are ADC1 pins on a classic ESP32.
// Change these pins for your board and EMG circuitry before uploading.
// Make sure the sensor output is within your board's ADC input limits.
#include <Arduino.h>

constexpr int EMG_PINS[] = {34, 35};
constexpr size_t CHANNELS = sizeof(EMG_PINS) / sizeof(EMG_PINS[0]);
constexpr uint32_t SAMPLE_PERIOD_US = 4000; // Requested example rate: 250 Hz.
constexpr uint32_t BAUD_RATE = 921600;

uint32_t next_sample_us;
uint32_t sample_sequence = 0;

void setup() {
  Serial.begin(BAUD_RATE);
  for (size_t i = 0; i < CHANNELS; ++i) pinMode(EMG_PINS[i], INPUT);
  next_sample_us = micros();
}

void loop() {
  const uint32_t now = micros();
  const int32_t late = static_cast<int32_t>(now - next_sample_us);
  if (late < 0) return;

  // Count missed acquisition slots so the backend can detect the gap.
  const uint32_t skipped = static_cast<uint32_t>(late) / SAMPLE_PERIOD_US;
  sample_sequence += skipped;
  next_sample_us += skipped * SAMPLE_PERIOD_US;

  const uint32_t sample_time = micros();
  int values[CHANNELS];
  for (size_t i = 0; i < CHANNELS; ++i) values[i] = analogRead(EMG_PINS[i]);

  Serial.print("{\"type\":\"emg\",\"version\":1,\"first_sequence\":");
  Serial.print(static_cast<unsigned long>(sample_sequence));
  Serial.print(",\"device_timestamp_us\":");
  Serial.print(static_cast<unsigned long>(sample_time));
  Serial.print(",\"samples\":[[");
  for (size_t i = 0; i < CHANNELS; ++i) {
    if (i) Serial.print(',');
    Serial.print(values[i]);
  }
  Serial.println("]]}");

  ++sample_sequence;
  next_sample_us += SAMPLE_PERIOD_US;
}

#pragma once
#include <stdbool.h>

// BOOT button: short press = emergency stop, hold >= 6 s = wipe config and reboot.
void button_init(void);
bool button_take_emergency(void);      // true once per short press

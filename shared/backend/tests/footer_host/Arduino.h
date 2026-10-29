#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#define PROGMEM
#define pgm_read_dword(p) (*(const uint32_t*)(p))
#define pgm_read_word(p) (*(const uint16_t*)(p))
#define pgm_read_byte(p) (*(const uint8_t*)(p))
struct QuietSerial {template<typename...Args> void printf(const char*,Args...) {}};
extern QuietSerial Serial;

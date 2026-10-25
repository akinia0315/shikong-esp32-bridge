#pragma once
#include <stdbool.h>
#include <stddef.h>

#define TOY_SSID_MAX 33
#define TOY_PASS_MAX 65
#define TOY_URL_MAX 128
#define TOY_TOKEN_MAX 128

typedef struct {
    char ssid[TOY_SSID_MAX];
    char pass[TOY_PASS_MAX];
    char url[TOY_URL_MAX];
    char token[TOY_TOKEN_MAX];
} toy_config_t;

void config_store_init(void);
bool config_store_load(toy_config_t *out);
bool config_store_save(const toy_config_t *cfg);
void config_store_clear(void);
bool config_store_is_complete(const toy_config_t *cfg);

/*
 * Copyright 2026
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#define _GNU_SOURCE

#include "../xenoid-gralloc/android_compat.h"

#include <dlfcn.h>
#include <ctype.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#if defined(__ANDROID__) && !defined(XENOID_HWC_PRESENT_TEST)
#include <sys/system_properties.h>
#endif

#define HWC_HARDWARE_MODULE_ID "hwcomposer"
#define HWC_HARDWARE_COMPOSER "composer"
#define HWC_DEVICE_API_VERSION_1_4 UINT32_C(0x01040001)
#define HWC_MODULE_API_VERSION_0_1 HARDWARE_MODULE_API_VERSION(0, 1)
#define HWC_DISPLAY_PRIMARY 0
#define HWC_EVENT_VSYNC 0
#define HWC_DISPLAY_NO_ATTRIBUTE 0
#define HWC_DISPLAY_VSYNC_PERIOD 1
#define HWC_DISPLAY_WIDTH 2
#define HWC_DISPLAY_HEIGHT 3
#define HWC_DISPLAY_DPI_X 4
#define HWC_DISPLAY_DPI_Y 5
#define HWC_POWER_MODE_OFF 0
#define HWC_POWER_MODE_NORMAL 2
#define RAVEN_CONFIG_60HZ 0
#define RAVEN_CONFIG_120HZ 1
#define RAVEN_CONFIG_COUNT 2
#define RAVEN_DEFAULT_CONFIG RAVEN_CONFIG_120HZ
#define RAVEN_PERIOD_60HZ_NS INT64_C(16666667)
#define RAVEN_PERIOD_120HZ_NS INT64_C(8333333)
#define BASE_HWC_PATH "/vendor/lib64/hw/hwcomposer.redroid.so"
#ifndef XENOID_QA_REFRESH_FILE
#define XENOID_QA_REFRESH_FILE "/data/local/tmp/xenoid-qa-refresh-rate"
#endif
#ifndef XENOID_QA_DISPLAY_MODE_FILE
#define XENOID_QA_DISPLAY_MODE_FILE "/data/local/tmp/xenoid-qa-display-mode"
#endif

struct hwc_display_contents_1;
typedef struct hwc_display_contents_1 hwc_display_contents_1_t;

struct hwc_procs {
    void (*invalidate)(const struct hwc_procs* procs);
    void (*vsync)(const struct hwc_procs* procs, int display, int64_t timestamp);
    void (*hotplug)(const struct hwc_procs* procs, int display, int connected);
};
typedef struct hwc_procs hwc_procs_t;

struct hwc_composer_device_1;
typedef struct hwc_composer_device_1 hwc_composer_device_1_t;

struct hwc_composer_device_1 {
    hw_device_t common;
    int (*prepare)(hwc_composer_device_1_t* device, size_t display_count,
                   hwc_display_contents_1_t** displays);
    int (*set)(hwc_composer_device_1_t* device, size_t display_count,
               hwc_display_contents_1_t** displays);
    int (*eventControl)(hwc_composer_device_1_t* device, int display, int event,
                        int enabled);
    int (*setPowerMode)(hwc_composer_device_1_t* device, int display, int mode);
    int (*query)(hwc_composer_device_1_t* device, int what, int* value);
    void (*registerProcs)(hwc_composer_device_1_t* device,
                          const hwc_procs_t* procs);
    void (*dump)(hwc_composer_device_1_t* device, char* buffer, int length);
    int (*getDisplayConfigs)(hwc_composer_device_1_t* device, int display,
                             uint32_t* configs, size_t* config_count);
    int (*getDisplayAttributes)(hwc_composer_device_1_t* device, int display,
                                uint32_t config, const uint32_t* attributes,
                                int32_t* values);
    int (*getActiveConfig)(hwc_composer_device_1_t* device, int display);
    int (*setActiveConfig)(hwc_composer_device_1_t* device, int display,
                           int config);
    int (*setCursorPositionAsync)(hwc_composer_device_1_t* device, int display,
                                  int x, int y);
    void* reserved_proc[1];
};

struct raven_hwc;

struct raven_hwc_procs {
    hwc_procs_t public_procs;
    struct raven_hwc* owner;
};

struct display_mode {
    int32_t width;
    int32_t height;
    int32_t dpi_milli;
};

struct raven_hwc {
    hwc_composer_device_1_t public_device;
    hwc_composer_device_1_t* base;
    void* base_dso;
    uint32_t base_config;
    pthread_mutex_t lock;
    pthread_mutex_t present_lock;
    int64_t present_last_start;
    unsigned present_rate;
    pthread_cond_t wake;
    pthread_t vsync_thread;
    int thread_started;
    int stop;
    int vsync_enabled;
    int active_config;
    size_t config_count;
    int64_t periods_ns[RAVEN_CONFIG_COUNT];
    struct display_mode display_mode;
    const hwc_procs_t* client_procs;
    struct raven_hwc_procs base_procs;
};

/* Optional physical attributes of this virtual display, read once at open.
 * An absent or malformed record preserves the base composer's attributes.
 * Writers must replace the three-integer file atomically before HWC restart.
 */
static int requested_display_mode(struct display_mode* mode) {
    FILE* file = fopen(XENOID_QA_DISPLAY_MODE_FILE, "re");
    if (file == NULL) return 0;
    char buffer[128];
    size_t length = fread(buffer, 1, sizeof(buffer), file);
    int read_failed = ferror(file);
    fclose(file);
    if (read_failed || length == 0 || length == sizeof(buffer) ||
        memchr(buffer, '\0', length) != NULL) return 0;
    buffer[length] = '\0';

    int32_t values[3];
    char* cursor = buffer;
    for (size_t index = 0; index < 3; ++index) {
        while (isspace((unsigned char)*cursor)) ++cursor;
        if (!isdigit((unsigned char)*cursor)) return 0;
        char* end;
        errno = 0;
        long value = strtol(cursor, &end, 10);
        const int32_t maximum = index == 2 ? INT32_MAX / 1000 : INT32_MAX;
        if (errno != 0 || value <= 0 || value > maximum ||
            (*end != '\0' && !isspace((unsigned char)*end))) return 0;
        values[index] = (int32_t)value;
        cursor = end;
    }
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor != '\0') return 0;
    *mode = (struct display_mode){values[0], values[1], values[2] * 1000};
    return 1;
}

static int64_t monotonic_ns(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (int64_t)now.tv_sec * INT64_C(1000000000) + now.tv_nsec;
}

static void sleep_until_ns(int64_t deadline_ns) {
    struct timespec deadline = {
        .tv_sec = (time_t)(deadline_ns / INT64_C(1000000000)),
        .tv_nsec = (long)(deadline_ns % INT64_C(1000000000)),
    };
    while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &deadline, NULL) == EINTR) {
    }
}

static int requested_refresh_hz(void) {
    FILE* file = fopen(XENOID_QA_REFRESH_FILE, "re");
    if (file == NULL) return 0;
    char buffer[64];
    size_t length = fread(buffer, 1, sizeof(buffer), file);
    fclose(file);
    if (length == 0 || length == sizeof(buffer) || memchr(buffer, '\0', length) != NULL) return 0;
    buffer[length] = '\0';
    char* end;
    errno = 0;
    long value = strtol(buffer, &end, 10);
    if (end == buffer || errno != 0) return 0;
    while (isspace((unsigned char)*end)) ++end;
    /* Positive integral Hz whose nanosecond period is representable. */
    return *end == '\0' && value > 0 && value <= INT64_C(1000000000)
                   ? (int)value : 0;
}

static void configure_refresh_rates(struct raven_hwc* wrapper) {
    wrapper->active_config = RAVEN_DEFAULT_CONFIG;
    wrapper->config_count = RAVEN_CONFIG_COUNT;
    wrapper->periods_ns[RAVEN_CONFIG_60HZ] = RAVEN_PERIOD_60HZ_NS;
    wrapper->periods_ns[RAVEN_CONFIG_120HZ] = RAVEN_PERIOD_120HZ_NS;
    const int refresh_hz = requested_refresh_hz();
    if (refresh_hz > 0) {
        wrapper->config_count = 1;
        wrapper->active_config = 0;
        wrapper->periods_ns[0] = INT64_C(1000000000) / refresh_hz;
    }
}

static int64_t period_for_config(const struct raven_hwc* wrapper, int config) {
    return wrapper->periods_ns[config];
}

static struct raven_hwc* raven_from_device(hwc_composer_device_1_t* device) {
    return (struct raven_hwc*)device;
}

static struct raven_hwc* raven_from_common(hw_device_t* device) {
    return (struct raven_hwc*)device;
}

static void* raven_vsync_main(void* opaque) {
    struct raven_hwc* wrapper = opaque;
    int64_t next_ns = 0;
    int64_t previous_period = 0;

    pthread_mutex_lock(&wrapper->lock);
    while (!wrapper->stop) {
        while (!wrapper->stop && !wrapper->vsync_enabled) {
            next_ns = 0;
            pthread_cond_wait(&wrapper->wake, &wrapper->lock);
        }
        if (wrapper->stop) break;

        int64_t period = period_for_config(wrapper, wrapper->active_config);
        int64_t now_ns = monotonic_ns();
        if (next_ns == 0 || period != previous_period || next_ns <= now_ns) {
            next_ns = now_ns + period;
        }
        previous_period = period;
        pthread_mutex_unlock(&wrapper->lock);

        sleep_until_ns(next_ns);
        now_ns = monotonic_ns();

        pthread_mutex_lock(&wrapper->lock);
        const hwc_procs_t* procs = NULL;
        if (!wrapper->stop && wrapper->vsync_enabled) {
            procs = wrapper->client_procs;
            period = period_for_config(wrapper, wrapper->active_config);
            if (period != previous_period || next_ns <= now_ns) {
                next_ns = now_ns + period;
            } else {
                next_ns += period;
            }
            previous_period = period;
        }
        pthread_mutex_unlock(&wrapper->lock);

        if (procs != NULL && procs->vsync != NULL) {
            procs->vsync(procs, HWC_DISPLAY_PRIMARY, now_ns);
        }
        pthread_mutex_lock(&wrapper->lock);
    }
    pthread_mutex_unlock(&wrapper->lock);
    return NULL;
}

static const hwc_procs_t* raven_client_procs(struct raven_hwc* wrapper) {
    const hwc_procs_t* procs;
    pthread_mutex_lock(&wrapper->lock);
    procs = wrapper->client_procs;
    pthread_mutex_unlock(&wrapper->lock);
    return procs;
}

static void base_invalidate(const hwc_procs_t* procs) {
    struct raven_hwc_procs* bridge = (struct raven_hwc_procs*)procs;
    const hwc_procs_t* client = raven_client_procs(bridge->owner);
    if (client != NULL && client->invalidate != NULL) client->invalidate(client);
}

static void base_vsync(const hwc_procs_t* procs, int display, int64_t timestamp) {
    (void)procs;
    (void)display;
    (void)timestamp;
}

static void base_hotplug(const hwc_procs_t* procs, int display, int connected) {
    struct raven_hwc_procs* bridge = (struct raven_hwc_procs*)procs;
    const hwc_procs_t* client = raven_client_procs(bridge->owner);
    if (client != NULL && client->hotplug != NULL) {
        client->hotplug(client, display, connected);
    }
}

static int raven_prepare(hwc_composer_device_1_t* device, size_t display_count,
                         hwc_display_contents_1_t** displays) {
    struct raven_hwc* wrapper = raven_from_device(device);
    return wrapper->base->prepare(wrapper->base, display_count, displays);
}

static unsigned requested_present_fps(void) {
#ifdef XENOID_HWC_PRESENT_TEST
    const char* value = getenv("QA_HWC_PRESENT_FPS");
#elif defined(__ANDROID__)
    char property[PROP_VALUE_MAX] = {0};
    __system_property_get("debug.hwc.present_fps", property);
    const char* value = property;
#else
    const char* value = NULL;
#endif
    if (value == NULL || *value == '\0') return 0;
    uint64_t result = 0;
    for (const char* p = value; *p; ++p) {
        if (*p < '0' || *p > '9') return 0;
        result = result * 10 + (unsigned)(*p - '0');
        if (result > UINT64_C(1000000000)) return 0;
    }
    return (unsigned)result;
}

static void pace_present(struct raven_hwc* wrapper) {
    const unsigned rate = requested_present_fps();
    // SurfaceFlinger serializes the primary HWC set path. Never enqueue extra
    // waiters if an unexpected concurrent caller appears; preserve forwarding.
    if (pthread_mutex_trylock(&wrapper->present_lock) != 0) return;
    if (rate != wrapper->present_rate) {
        wrapper->present_last_start = 0;
        wrapper->present_rate = rate;
    }
    if (rate > 0) {
        const int64_t period = (INT64_C(1000000000) + rate - 1) / rate;
        const int64_t now = monotonic_ns();
        if (now > 0) {
            int64_t deadline = wrapper->present_last_start + period;
            // No catch-up or accumulated debt: at most one period per caller.
            if (deadline > now + period) deadline = now + period;
            if (deadline > now) sleep_until_ns(deadline);
            const int64_t woke = monotonic_ns();
            wrapper->present_last_start = woke > 0 ? woke : now;
        } else {
            wrapper->present_last_start = 0;
        }
    }
    pthread_mutex_unlock(&wrapper->present_lock);
}

static int raven_set(hwc_composer_device_1_t* device, size_t display_count,
                     hwc_display_contents_1_t** displays) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (display_count > 0 && displays != NULL && displays[HWC_DISPLAY_PRIMARY] != NULL) {
        pace_present(wrapper);
    }
    // No pacing lock is held across the driver call or its callbacks. Buffer,
    // acquire/release/retire fences and the exact base error result are untouched.
    return wrapper->base->set(wrapper->base, display_count, displays);
}

static int raven_event_control(hwc_composer_device_1_t* device, int display,
                               int event, int enabled) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (display != HWC_DISPLAY_PRIMARY || event != HWC_EVENT_VSYNC ||
        (enabled != 0 && enabled != 1)) {
        return -EINVAL;
    }
    pthread_mutex_lock(&wrapper->lock);
    wrapper->vsync_enabled = enabled;
    pthread_cond_broadcast(&wrapper->wake);
    pthread_mutex_unlock(&wrapper->lock);
    if (wrapper->base->eventControl != NULL) {
        wrapper->base->eventControl(wrapper->base, display, event, 0);
    }
    return 0;
}

static int raven_set_power_mode(hwc_composer_device_1_t* device, int display,
                                int mode) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (display != HWC_DISPLAY_PRIMARY) return -EINVAL;
    if (mode == HWC_POWER_MODE_OFF) {
        return wrapper->base->setPowerMode(wrapper->base, display, 1);
    }
    if (mode == HWC_POWER_MODE_NORMAL) {
        return wrapper->base->setPowerMode(wrapper->base, display, 0);
    }
    return -EINVAL;
}

static int raven_query(hwc_composer_device_1_t* device, int what, int* value) {
    struct raven_hwc* wrapper = raven_from_device(device);
    return wrapper->base->query(wrapper->base, what, value);
}

static void raven_register_procs(hwc_composer_device_1_t* device,
                                 const hwc_procs_t* procs) {
    struct raven_hwc* wrapper = raven_from_device(device);
    pthread_mutex_lock(&wrapper->lock);
    wrapper->client_procs = procs;
    pthread_mutex_unlock(&wrapper->lock);
    wrapper->base->registerProcs(wrapper->base, &wrapper->base_procs.public_procs);
    if (wrapper->base->eventControl != NULL) {
        wrapper->base->eventControl(
            wrapper->base, HWC_DISPLAY_PRIMARY, HWC_EVENT_VSYNC, 0);
    }
}

static void raven_dump(hwc_composer_device_1_t* device, char* buffer, int length) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (buffer == NULL || length <= 0) return;
    buffer[0] = '\0';
    if (wrapper->base->dump != NULL) wrapper->base->dump(wrapper->base, buffer, length);
    size_t used = strnlen(buffer, (size_t)length);
    if (used >= (size_t)length) return;
    int active;
    pthread_mutex_lock(&wrapper->lock);
    active = wrapper->active_config;
    pthread_mutex_unlock(&wrapper->lock);
    snprintf(buffer + used, (size_t)length - used,
             "\nXenoid display modes: count=%zu; active=%.3fHz; "
             "configured geometry=%dx%d; configured dpi=%.3f\n",
             wrapper->config_count,
             1000000000.0 / period_for_config(wrapper, active),
             wrapper->display_mode.width, wrapper->display_mode.height,
             wrapper->display_mode.dpi_milli / 1000.0);
}

static int raven_get_display_configs(hwc_composer_device_1_t* device, int display,
                                     uint32_t* configs, size_t* config_count) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (config_count == NULL) return -EINVAL;
    if (display != HWC_DISPLAY_PRIMARY) {
        return wrapper->base->getDisplayConfigs(
            wrapper->base, display, configs, config_count);
    }
    size_t capacity = *config_count;
    if (configs != NULL) {
        for (size_t i = 0; i < capacity && i < wrapper->config_count; ++i) {
            configs[i] = (uint32_t)i;
        }
    }
    *config_count = wrapper->config_count;
    return 0;
}

static int raven_get_display_attributes(hwc_composer_device_1_t* device,
                                        int display, uint32_t config,
                                        const uint32_t* attributes,
                                        int32_t* values) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (display != HWC_DISPLAY_PRIMARY) {
        return wrapper->base->getDisplayAttributes(
            wrapper->base, display, config, attributes, values);
    }
    if (config >= wrapper->config_count || attributes == NULL || values == NULL) {
        return -EINVAL;
    }
    int result = wrapper->base->getDisplayAttributes(
        wrapper->base, display, wrapper->base_config, attributes, values);
    if (result != 0) return result;
    for (size_t index = 0; attributes[index] != HWC_DISPLAY_NO_ATTRIBUTE; ++index) {
        if (attributes[index] == HWC_DISPLAY_VSYNC_PERIOD) {
            values[index] = (int32_t)period_for_config(wrapper, (int)config);
        } else if (wrapper->display_mode.width > 0) {
            switch (attributes[index]) {
                case HWC_DISPLAY_WIDTH:
                    values[index] = wrapper->display_mode.width;
                    break;
                case HWC_DISPLAY_HEIGHT:
                    values[index] = wrapper->display_mode.height;
                    break;
                case HWC_DISPLAY_DPI_X:
                case HWC_DISPLAY_DPI_Y:
                    values[index] = wrapper->display_mode.dpi_milli;
                    break;
            }
        }
    }
    return 0;
}

static int raven_get_active_config(hwc_composer_device_1_t* device, int display) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (display != HWC_DISPLAY_PRIMARY) return -EINVAL;
    pthread_mutex_lock(&wrapper->lock);
    int active = wrapper->active_config;
    pthread_mutex_unlock(&wrapper->lock);
    return active;
}

static int raven_set_active_config(hwc_composer_device_1_t* device, int display,
                                   int config) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (display != HWC_DISPLAY_PRIMARY || config < 0 ||
        (size_t)config >= wrapper->config_count) {
        return -EINVAL;
    }
    const hwc_procs_t* procs;
    pthread_mutex_lock(&wrapper->lock);
    wrapper->active_config = config;
    pthread_cond_broadcast(&wrapper->wake);
    procs = wrapper->client_procs;
    pthread_mutex_unlock(&wrapper->lock);
    if (procs != NULL && procs->invalidate != NULL) procs->invalidate(procs);
    return 0;
}

static int raven_set_cursor_position(hwc_composer_device_1_t* device, int display,
                                     int x, int y) {
    struct raven_hwc* wrapper = raven_from_device(device);
    if (wrapper->base->setCursorPositionAsync == NULL) return -EINVAL;
    return wrapper->base->setCursorPositionAsync(wrapper->base, display, x, y);
}

static int raven_close(hw_device_t* device) {
    struct raven_hwc* wrapper = raven_from_common(device);
    pthread_mutex_lock(&wrapper->lock);
    wrapper->stop = 1;
    wrapper->vsync_enabled = 0;
    wrapper->client_procs = NULL;
    pthread_cond_broadcast(&wrapper->wake);
    pthread_mutex_unlock(&wrapper->lock);
    if (wrapper->thread_started) pthread_join(wrapper->vsync_thread, NULL);
    if (wrapper->base != NULL && wrapper->base->common.close != NULL) {
        wrapper->base->common.close(&wrapper->base->common);
    }
    if (wrapper->base_dso != NULL) dlclose(wrapper->base_dso);
    pthread_cond_destroy(&wrapper->wake);
    pthread_mutex_destroy(&wrapper->present_lock);
    pthread_mutex_destroy(&wrapper->lock);
    free(wrapper);
    return 0;
}

static int raven_open(const hw_module_t* module, const char* id,
                      hw_device_t** out_device) {
    if (module == NULL || id == NULL || out_device == NULL ||
        strcmp(id, HWC_HARDWARE_COMPOSER) != 0) {
        return -EINVAL;
    }

    void* base_dso = dlopen(BASE_HWC_PATH, RTLD_NOW | RTLD_LOCAL);
    if (base_dso == NULL) return -ENOENT;
    hw_module_t* base_module = dlsym(base_dso, HAL_MODULE_INFO_SYM_AS_STR);
    if (base_module == NULL || base_module->methods == NULL ||
        base_module->methods->open == NULL) {
        dlclose(base_dso);
        return -EINVAL;
    }

    hw_device_t* base_common = NULL;
    int result = base_module->methods->open(base_module, id, &base_common);
    if (result != 0 || base_common == NULL) {
        dlclose(base_dso);
        return result != 0 ? result : -ENODEV;
    }
    hwc_composer_device_1_t* base = (hwc_composer_device_1_t*)base_common;
    if (base->prepare == NULL || base->set == NULL || base->eventControl == NULL ||
        base->setPowerMode == NULL || base->query == NULL ||
        base->registerProcs == NULL || base->getDisplayConfigs == NULL ||
        base->getDisplayAttributes == NULL) {
        base_common->close(base_common);
        dlclose(base_dso);
        return -ENOSYS;
    }

    uint32_t base_config = 0;
    size_t base_config_count = 1;
    result = base->getDisplayConfigs(
        base, HWC_DISPLAY_PRIMARY, &base_config, &base_config_count);
    if (result != 0 || base_config_count == 0) {
        base_common->close(base_common);
        dlclose(base_dso);
        return result != 0 ? result : -ENODEV;
    }

    struct raven_hwc* wrapper = calloc(1, sizeof(*wrapper));
    if (wrapper == NULL) {
        base_common->close(base_common);
        dlclose(base_dso);
        return -ENOMEM;
    }
    wrapper->base = base;
    wrapper->base_dso = base_dso;
    wrapper->base_config = base_config;
    configure_refresh_rates(wrapper);
    requested_display_mode(&wrapper->display_mode);
    wrapper->base_procs.owner = wrapper;
    wrapper->base_procs.public_procs.invalidate = base_invalidate;
    wrapper->base_procs.public_procs.vsync = base_vsync;
    wrapper->base_procs.public_procs.hotplug = base_hotplug;

    result = pthread_mutex_init(&wrapper->lock, NULL);
    if (result != 0) {
        base_common->close(base_common);
        dlclose(base_dso);
        free(wrapper);
        return -result;
    }
    result = pthread_cond_init(&wrapper->wake, NULL);
    if (result != 0) {
        pthread_mutex_destroy(&wrapper->lock);
        base_common->close(base_common);
        dlclose(base_dso);
        free(wrapper);
        return -result;
    }
    result = pthread_mutex_init(&wrapper->present_lock, NULL);
    if (result != 0) {
        pthread_cond_destroy(&wrapper->wake);
        pthread_mutex_destroy(&wrapper->lock);
        base_common->close(base_common);
        dlclose(base_dso);
        free(wrapper);
        return -result;
    }

    wrapper->public_device.common.tag = HARDWARE_DEVICE_TAG;
    wrapper->public_device.common.version = HWC_DEVICE_API_VERSION_1_4;
    wrapper->public_device.common.module = (hw_module_t*)module;
    wrapper->public_device.common.close = raven_close;
    wrapper->public_device.prepare = raven_prepare;
    wrapper->public_device.set = raven_set;
    wrapper->public_device.eventControl = raven_event_control;
    wrapper->public_device.setPowerMode = raven_set_power_mode;
    wrapper->public_device.query = raven_query;
    wrapper->public_device.registerProcs = raven_register_procs;
    wrapper->public_device.dump = raven_dump;
    wrapper->public_device.getDisplayConfigs = raven_get_display_configs;
    wrapper->public_device.getDisplayAttributes = raven_get_display_attributes;
    wrapper->public_device.getActiveConfig = raven_get_active_config;
    wrapper->public_device.setActiveConfig = raven_set_active_config;
    wrapper->public_device.setCursorPositionAsync = raven_set_cursor_position;

    result = pthread_create(
        &wrapper->vsync_thread, NULL, raven_vsync_main, wrapper);
    if (result != 0) {
        pthread_mutex_destroy(&wrapper->present_lock);
        pthread_cond_destroy(&wrapper->wake);
        pthread_mutex_destroy(&wrapper->lock);
        base_common->close(base_common);
        dlclose(base_dso);
        free(wrapper);
        return -result;
    }
    wrapper->thread_started = 1;
    *out_device = &wrapper->public_device.common;
    return 0;
}

static hw_module_methods_t raven_module_methods = {
    .open = raven_open,
};

__attribute__((visibility("default"))) hw_module_t HAL_MODULE_INFO_SYM = {
    .tag = HARDWARE_MODULE_TAG,
    .module_api_version = HWC_MODULE_API_VERSION_0_1,
    .hal_api_version = HARDWARE_HAL_API_VERSION,
    .id = HWC_HARDWARE_MODULE_ID,
    .name = "Xenoid Raven dynamic display composer",
    .author = "Xenoid",
    .methods = &raven_module_methods,
};

#if defined(__LP64__)
_Static_assert(sizeof(hw_device_t) == 120, "Android 13 LP64 hw_device_t ABI changed");
_Static_assert(sizeof(hwc_composer_device_1_t) == 224,
               "Android 13 LP64 hwc_composer_device_1_t ABI changed");
#endif

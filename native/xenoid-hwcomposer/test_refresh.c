#define XENOID_QA_REFRESH_FILE "/data/local/tmp/xenoid-qa-refresh-unit-test"
#include "hwcomposer_wrapper.c"
#include <assert.h>
#include <unistd.h>

static void write_setting(const char* text) {
    FILE* file = fopen(XENOID_QA_REFRESH_FILE, "we");
    assert(file != NULL);
    assert(fputs(text, file) >= 0);
    assert(fclose(file) == 0);
}

int main(void) {
    struct raven_hwc wrapper = {0};
    unlink(XENOID_QA_REFRESH_FILE);
    configure_refresh_rates(&wrapper);
    assert(wrapper.config_count == 2 && wrapper.active_config == 1);
    assert(period_for_config(&wrapper, 0) == RAVEN_PERIOD_60HZ_NS);
    assert(period_for_config(&wrapper, 1) == RAVEN_PERIOD_120HZ_NS);

    write_setting("20\n");
    configure_refresh_rates(&wrapper);
    assert(wrapper.config_count == 1 && wrapper.active_config == 0);
    assert(period_for_config(&wrapper, 0) == INT64_C(50000000));
    size_t count = 2;
    uint32_t configs[2] = {99, 99};
    assert(raven_get_display_configs(&wrapper.public_device, 0, configs, &count) == 0);
    assert(count == 1 && configs[0] == 0 && configs[1] == 99);

    write_setting("30\n");
    configure_refresh_rates(&wrapper);
    assert(wrapper.config_count == 1 && period_for_config(&wrapper, 0) == INT64_C(33333333));
    const char* invalid[] = {"0", "-20", "abc", "20foo", "20 30", "9999999999999999999999999999999999999", ""};
    for (size_t i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        write_setting(invalid[i]);
        configure_refresh_rates(&wrapper);
        assert(wrapper.config_count == 2 && wrapper.active_config == 1);
        assert(period_for_config(&wrapper, 0) == RAVEN_PERIOD_60HZ_NS);
        assert(period_for_config(&wrapper, 1) == RAVEN_PERIOD_120HZ_NS);
    }
    unlink(XENOID_QA_REFRESH_FILE);
    puts("HWC refresh configuration tests passed");
    return 0;
}

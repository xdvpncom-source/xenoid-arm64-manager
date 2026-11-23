// Optional, process-wide BufferQueue admission pacing. Display APIs are untouched.
#include <atomic>
#include <cerrno>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <memory>
#include <mutex>
#include <new>
#include <pthread.h>
#include <time.h>
#include <unordered_map>
#include <unistd.h>
#ifdef __ANDROID__
#include <android/log.h>
#include <sys/syscall.h>
#include <sys/system_properties.h>
#endif

#define EXPORT __attribute__((visibility("default")))
#define QUEUE_NAME "_ZN7android19BufferQueueProducer11queueBufferEiRKNS_22IGraphicBufferProducer16QueueBufferInputEPNS1_17QueueBufferOutputE"
#define DTOR1_NAME "_ZN7android19BufferQueueProducerD1Ev"
#define DTOR2_NAME "_ZN7android19BufferQueueProducerD2Ev"
#ifdef __ANDROID__
#define SYMBOL(name) __asm__(name)
#else
#define SYMBOL(name)
#endif
extern "C" EXPORT int gui_queue(void*, int, const void*, void*) SYMBOL(QUEUE_NAME);
extern "C" EXPORT void gui_destroy1(void*) SYMBOL(DTOR1_NAME);
extern "C" EXPORT void gui_destroy2(void*, void*) SYMBOL(DTOR2_NAME);

namespace {
using QueueFunction = int (*)(void*, int, const void*, void*);
using DestroyFunction = void (*)(void*);
using BaseDestroyFunction = void (*)(void*, void*);
std::atomic<uint64_t> calls{0}, waits{0}, failures{0}, retired{0};
std::atomic<uint64_t> destructorCalls{0}, baseDestructorCalls{0};
thread_local unsigned nesting = 0;

void logEvent(const char* message) {
#ifdef __ANDROID__
    __android_log_print(ANDROID_LOG_INFO, "GuiBufferPacer", "%s", message);
#else
    std::fprintf(stderr, "GuiBufferPacer: %s\n", message);
#endif
}

uint32_t rate() {
#ifdef GUI_PACER_TEST
    const char* text = std::getenv("GUI_PACER_FPS");
#elif defined(__ANDROID__)
    char value[PROP_VALUE_MAX]{};
    __system_property_get("debug.gui.frame_pacer_fps", value);
    const char* text = value;
#else
    const char* text = std::getenv("GUI_PACER_FPS");
#endif
    if (!text || !*text) return 0;
    uint64_t result = 0;
    for (const char* p = text; *p; ++p) {
        if (*p < '0' || *p > '9') return 0;
        result = result * 10 + static_cast<unsigned>(*p - '0');
        if (result > 1000000000ULL) return 0;
    }
    return static_cast<uint32_t>(result);
}

int64_t nowNs() {
    timespec t{};
#ifdef __ANDROID__
    if (syscall(__NR_clock_gettime, CLOCK_MONOTONIC, &t)) return 0;
#else
    if (clock_gettime(CLOCK_MONOTONIC, &t)) return 0;
#endif
    return static_cast<int64_t>(t.tv_sec) * 1000000000LL + t.tv_nsec;
}

bool sleepUntil(int64_t deadline) {
    for (;;) {
        const auto now = nowNs();
        if (!now) return false;
        const auto left = deadline - now;
        if (left <= 0) return true;
        timespec remaining{static_cast<time_t>(left / 1000000000LL),
                           static_cast<long>(left % 1000000000LL)};
#ifdef __ANDROID__
        if (!syscall(__NR_clock_nanosleep, CLOCK_MONOTONIC, 0, &remaining, nullptr)) return true;
#else
        if (!nanosleep(&remaining, nullptr)) return true;
#endif
        if (errno != EINTR) return false;
    }
}

struct Gate {
    std::mutex mutex;
    uint32_t rate = 0;
    int64_t previous = 0;
    uint64_t calls = 0;
};
struct Registry {
    std::mutex mutex;
    std::unordered_map<void*, std::shared_ptr<Gate>> gates;
    int64_t report = 0;
};
std::atomic<Registry*> registry{nullptr};

Registry* getRegistry() {
    Registry* value = registry.load(std::memory_order_acquire);
    if (!value) {
        auto* fresh = new Registry;
        if (!registry.compare_exchange_strong(value, fresh,
                                              std::memory_order_release,
                                              std::memory_order_acquire)) delete fresh;
        else value = fresh;
    }
    return value;
}

// Never use inherited mutexes after a zygote fork. The child's old registry
// is intentionally abandoned (a bounded copy-on-write object) and not freed.
void childForked() {
    registry.store(nullptr, std::memory_order_release); nesting = 0;
    calls = 0; waits = 0; failures = 0; retired = 0;
    destructorCalls = 0; baseDestructorCalls = 0;
}
__attribute__((constructor)) void installForkHandler() {
    if (pthread_atfork(nullptr, nullptr, childForked)) logEvent("atfork registration failed");
}

void forget(void* producer) {
    Registry* value = registry.load(std::memory_order_acquire);
    if (!value) return;
    std::lock_guard<std::mutex> lock(value->mutex);
    retired += value->gates.erase(producer);
}

void pace(void* producer, uint32_t target) {
    try {
        Registry* value = getRegistry();
        std::shared_ptr<Gate> gate;
        {
            std::lock_guard<std::mutex> lock(value->mutex);
            auto& slot = value->gates[producer];
            if (!slot) slot = std::make_shared<Gate>();
            gate = slot;
        }
        {
            // Only this producer's admission is locked. No libgui method is
            // called, and no registry or GUI lock is held while sleeping.
            std::lock_guard<std::mutex> lock(gate->mutex);
            if (gate->rate != target) { gate->rate = target; gate->previous = 0; }
            const int64_t now = nowNs();
            const int64_t period = (1000000000LL + target - 1) / target;
            if (now && gate->previous && gate->previous + period > now) {
                ++waits;
                if (!sleepUntil(gate->previous + period)) ++failures;
            }
            gate->previous = nowNs();
            if (!gate->previous) ++failures;
            ++gate->calls;
        }
        const auto count = ++calls;
        const int64_t now = nowNs();
        std::lock_guard<std::mutex> lock(value->mutex);
        if (count == 1 || (now && now - value->report >= 5000000000LL)) {
            value->report = now;
            char message[192];
            std::snprintf(message, sizeof(message), "fps=%u calls=%" PRIu64 " producers=%zu waits=%" PRIu64 " failures=%" PRIu64,
                          target, count, value->gates.size(), waits.load(), failures.load());
            logEvent(message);
        }
    } catch (const std::bad_alloc&) {
        ++failures;
        logEvent("allocation failed; forwarding unpaced");
    }
}

#ifdef GUI_PACER_TEST
extern "C" int test_original_queue(void*, int, const void*, void*);
extern "C" void test_original_destroy(void*);
extern "C" void test_original_base_destroy(void*, void*);
QueueFunction originalQueue() { return test_original_queue; }
DestroyFunction originalDestroy(const char*) { return test_original_destroy; }
BaseDestroyFunction originalBaseDestroy() { return test_original_base_destroy; }
#else
#ifndef GUI_ORIGINAL_LIB
#define GUI_ORIGINAL_LIB "libgui.so"
#endif
void* originalLibrary() {
    // A GLOBAL child dependency may occur after libgui in the loaded-object
    // list. RTLD_NEXT is therefore insufficient. An explicit handle starts
    // lookup at the original library's own definition, not the global group.
    static void* handle = dlopen(GUI_ORIGINAL_LIB, RTLD_NOW | RTLD_NOLOAD);
    return handle;
}
QueueFunction originalQueue() {
    static auto next = originalLibrary()
        ? reinterpret_cast<QueueFunction>(dlsym(originalLibrary(), QUEUE_NAME)) : nullptr;
    return next == gui_queue ? nullptr : next;
}
DestroyFunction originalDestroy(const char* name) {
    return originalLibrary()
        ? reinterpret_cast<DestroyFunction>(dlsym(originalLibrary(), name)) : nullptr;
}
BaseDestroyFunction originalBaseDestroy() {
    return originalLibrary()
        ? reinterpret_cast<BaseDestroyFunction>(dlsym(originalLibrary(), DTOR2_NAME)) : nullptr;
}
#endif
struct Depth { Depth() { ++nesting; } ~Depth() { --nesting; } };
} // namespace

extern "C" EXPORT int gui_queue(void* producer, int slot, const void* input, void* output) {
    const auto next = originalQueue();
    if (!next) { ++failures; logEvent("original queueBuffer unavailable"); return -ENOSYS; }
    const uint32_t target = rate();
    if (!target) { forget(producer); return next(producer, slot, input, output); }
    if (!producer || nesting) return next(producer, slot, input, output);
    Depth depth;
    pace(producer, target);
    return next(producer, slot, input, output);
}

extern "C" EXPORT void gui_destroy1(void* producer) {
    ++destructorCalls;
    forget(producer);
    const auto next = originalDestroy(DTOR1_NAME);
    if (next && next != gui_destroy1) next(producer);
    else { ++failures; logEvent("original D1 destructor unavailable"); }
}
extern "C" EXPORT void gui_destroy2(void* producer, void* vtt) {
    // Itanium ABI: a base destructor for a virtual-base class receives the
    // construction VTT in x1. It must survive all lookups and registry work.
    ++baseDestructorCalls;
    forget(producer);
    const auto next = originalBaseDestroy();
    if (next && next != gui_destroy2) next(producer, vtt);
    else { ++failures; logEvent("original D2 destructor unavailable"); }
}

extern "C" EXPORT int lab_gui_pacer_stats(char* output, size_t capacity) {
    size_t count = 0;
    Registry* value = registry.load(std::memory_order_acquire);
    if (value) { std::lock_guard<std::mutex> lock(value->mutex); count = value->gates.size(); }
    return std::snprintf(output, capacity, "{\"calls\":%" PRIu64 ",\"waits\":%" PRIu64 ",\"failures\":%" PRIu64 ",\"retired\":%" PRIu64 ",\"producers\":%zu,\"complete_destructors\":%" PRIu64 ",\"base_destructors\":%" PRIu64 "}",
                         calls.load(), waits.load(), failures.load(), retired.load(), count,
                         destructorCalls.load(), baseDestructorCalls.load());
}

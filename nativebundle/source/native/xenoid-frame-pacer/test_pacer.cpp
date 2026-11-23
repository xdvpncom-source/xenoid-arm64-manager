#define GUI_PACER_TEST 1
#include "pacer.cpp"
#include <cassert>
#include <sys/wait.h>
#include <thread>
#include <vector>

std::mutex testLock;
std::unordered_map<void*, std::vector<int64_t>> recorded;
int resultCode = 0, destructions = 0;
extern "C" int test_original_queue(void* object, int slot, const void* input, void* output) {
    assert(slot == 3 && input == reinterpret_cast<void*>(4) && output == reinterpret_cast<void*>(5));
    std::lock_guard<std::mutex> lock(testLock);
    recorded[object].push_back(nowNs()); return resultCode;
}
extern "C" void test_original_destroy(void*) { ++destructions; }
extern "C" void test_original_base_destroy(void*, void* vtt) { assert(vtt == reinterpret_cast<void*>(6)); ++destructions; }
int invoke(void* object) { return gui_queue(object, 3, reinterpret_cast<void*>(4), reinterpret_cast<void*>(5)); }

int main() {
    void* a = reinterpret_cast<void*>(1); void* b = reinterpret_cast<void*>(2);
    setenv("GUI_PACER_FPS", "0", 1);
    auto start = nowNs(); for (int i=0;i<10;++i) assert(invoke(a)==0);
    assert(nowNs()-start < 40000000);
    recorded.clear(); setenv("GUI_PACER_FPS", "15", 1);
    start = nowNs();
    for (int i=0;i<4;++i) { assert(invoke(a)==0); assert(invoke(b)==0); }
    const auto elapsed=nowNs()-start;
    assert(elapsed>=195000000 && elapsed<450000000);
    for (auto& values:recorded) for(size_t i=1;i<values.second.size();++i)
        assert(values.second[i]-values.second[i-1]>=63000000);
    gui_destroy2(a, reinterpret_cast<void*>(6)); assert(destructions==1);
    start=nowNs();assert(invoke(a)==0);assert(nowNs()-start<40000000);
    setenv("GUI_PACER_FPS","0",1);invoke(a);
    setenv("GUI_PACER_FPS","15",1);start=nowNs();invoke(a);assert(nowNs()-start<40000000);
    recorded.clear();
    std::thread first([&]{for(int i=0;i<3;++i)invoke(a);});
    std::thread second([&]{for(int i=0;i<3;++i)invoke(b);});first.join();second.join();
    for (auto& values:recorded) for(size_t i=1;i<values.second.size();++i)
        assert(values.second[i]-values.second[i-1]>=63000000);
    resultCode=-22;assert(invoke(a)==-22);resultCode=0;
    // Fork while a parent registry lock is held. Child must use fresh state.
    Registry* before=registry.load();before->mutex.lock();
    pid_t child=fork();assert(child>=0);
    if(!child){assert(registry.load()==nullptr);invoke(a);_exit(0);}
    before->mutex.unlock();int status=0;waitpid(child,&status,0);assert(WIFEXITED(status)&&WEXITSTATUS(status)==0);
    gui_destroy1(a);gui_destroy2(b, reinterpret_cast<void*>(6));
    assert(registry.load()->gates.empty());
    char stats[300];lab_gui_pacer_stats(stats,sizeof(stats));std::puts(stats);
    std::puts("PASS: unchanged arguments/results, independent producers15, concurrent producers, disabled, lifecycle/reuse, fork-safe registry");
}

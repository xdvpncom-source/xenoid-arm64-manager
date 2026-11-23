#include <atomic>
#include <cstdlib>
#include <typeinfo>
namespace android {
class IGraphicBufferProducer { public: struct QueueBufferInput { int value; }; struct QueueBufferOutput { int value; }; };
class MockRefBase { public: virtual ~MockRefBase(); };
MockRefBase::~MockRefBase() = default;
class BufferQueueProducer;
__attribute__((noinline)) void observeDestruction(BufferQueueProducer*);
class BufferQueueProducer : public virtual MockRefBase {
public:
    virtual ~BufferQueueProducer();
    virtual int queueBuffer(int, const IGraphicBufferProducer::QueueBufferInput&, IGraphicBufferProducer::QueueBufferOutput*);
};
BufferQueueProducer::~BufferQueueProducer() { observeDestruction(this); }
__attribute__((noinline)) void observeDestruction(BufferQueueProducer* object) {
    if (typeid(*object) != typeid(BufferQueueProducer)) std::abort();
}
int BufferQueueProducer::queueBuffer(int slot, const IGraphicBufferProducer::QueueBufferInput& input,
                                    IGraphicBufferProducer::QueueBufferOutput* output) {
    output->value = input.value + slot; return -23;
}
}
extern "C" __attribute__((visibility("default"))) void* mock_new() { return new android::BufferQueueProducer; }
extern "C" __attribute__((visibility("default"))) int mock_invoke(void* p) {
    auto* object = static_cast<android::BufferQueueProducer*>(p);
    android::IGraphicBufferProducer::QueueBufferInput input{40};
    android::IGraphicBufferProducer::QueueBufferOutput output{};
    using Fn=int(*)(void*,int,const void*,void*);
    auto vtable=*reinterpret_cast<void***>(object);
    int result=reinterpret_cast<Fn>(vtable[2])(object,2,&input,&output);
    return result==-23 && output.value==42 ? 0 : 1;
}
extern "C" __attribute__((visibility("default"))) void mock_delete(void* p) { delete static_cast<android::BufferQueueProducer*>(p); }

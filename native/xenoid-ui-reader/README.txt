Xenoid stock UI reader

StockUiServer is a private stdin/stdout bridge to the Android image's own
com.android.commands.uiautomator.DumpCommand.run(String[]). It adds no gesture
implementation, UI backend, APK or listening socket. Each request uses the
stock connect, idle wait, dump and disconnect code; only the app_process JVM
is reused. Normal D and all-interactive-windows W remain distinct results.
A separate rare readonly navigation request uses a new connection in this SAME
JVM to reacquire only SystemUI Back. Normal dumps never send that extra request.

Build used for the embedded artifact:
  JDK17.0.21.0.3 javac --release8; Android SDK Build Tools36.0.0 d8 --min-api26.
  python3 native/xenoid-ui-reader/build.py --javac /path/to/jdk17/bin/javac \
    --d8 /path/to/android-sdk/build-tools/36.0.0/d8 --output /tmp/stock-ui.jar
  SHA256:6290d1500fd1012019556a63c8993c048bccd76e42f0f4c40928fa548f9d12d0
  JAR size:9974bytes. Only classes.dex is packaged. Desktop test fixtures are
  not included. qa/qa_ui_reader.py embeds deterministic gzip/base64 bytes and
  checks the JAR SHA before use; update the pin only with a reviewed rebuild.

Protocol (big endian): request is magic0x58554931, uint32sequence, uint8flags.
Flags0=stocknormal,1=compressed,2=--windows,3=both,127=quit. Flag4 is followed
by an int32 nonnegative displayId and returns bounded navigation JSON. Response is magic,
sequence,statusbyte,uint32length,bytes. Maximum XML response8MiB; errors128bytes.
Sequence0 is READY plus actual guest PID/UID/kernel starttime. Other sequences
increase strictly. Informational stock output is redirected to bounded stderr.
Empty/failed stock dumps are errors, never cached successful XML.

Navigation freshness: a new UiAutomationShellWrapper connection calls
UiAutomation.setServiceInfo (Android13 clears that connection's node cache),
then gets the navigation root with getRoot(0), which bypasses the cache. Child
getChild(index,0) disables prefetch; EACH acquired node is explicitly refresh()ed
from the provider and the selected Back is refreshed again at the end. No app
root is read. At most128 nodes/depth16,128 window records/16displays and16KiB
reply are accepted. Every acquired node/window is recycled and the wrapper
disconnects; disconnect failure retires the JVM/pipe for fenced host cleanup.
The host never starts a reader for this request. It requires identical offered
Back bounds/layer/frame, fresh WM/InputDispatcher routing to SystemUI,
and an unexpired five-second lease INCLUDING mapping/read/verification time.
The usual official Xenoid native tap remains the sole input path. Freshness is
bounded, not atomic with UI changes occurring after the final provider query.
These navigation changes require a real-device canary before qualification.

Accessibility IDs are scoped to the currently registered interaction connection.
The fresh proof reports window_id_scope=current_ui_automation_connection and
node_window_id, which must equal its nonnegative fresh window_id. It does not
compare that ID with one from the earlier disconnected stock dump. Android13
ViewRootImpl removes/registers accessibility connections when their state
changes, and AccessibilityWindowManager allocates a new ID on registration.
An actual readonly canary observed3956 ->3964 with all semantic/geometry/layer
fields identical; the old cross-connection comparison rejected it. That failed
canary injected no input. This is evidence of that rejection, not proof of the
earlier historical advertisement failure or real-touch qualification.
Primary source:
https://android.googlesource.com/platform/frameworks/base/+/android-13.0.0_r74/core/java/android/view/ViewRootImpl.java
https://android.googlesource.com/platform/frameworks/base/+/android-13.0.0_r74/services/accessibility/java/com/android/server/accessibility/AccessibilityWindowManager.java

Host lifecycle: one lazy JVM per Broker round, actual ADB shell UID/environment,
private0700directory and0600files. Every read checks owned container/image/PID,
userspace epoch and helper PID identity; the Native lock serializes lifecycle.
Stop, timeout, generation change and cleanup close the pipe and reap only the
verified helper and captured compiler descendants. A cancellation marker plus
atomic directory retirement prevents a queued late launch after cleanup.
Cleanup failure retains its owner reference and can be retried. Explicit
inspect_window(reader='legacy') selects the old per-call reader; read failures
never silently switch modes.

Historical measurements of the PREVIOUS helper on one paused Android13 QA device: five normal reads kept the same
PID and matched the old reader byte-for-byte. One initial compiler was observed;
none appeared after JVM warmup. Persistent reads took about3s versus13.8s for
one cold stock launch in that sample. PSS was about50.4MiB after startup, so JVM
reuse trades sustained memory for lower repeated compiler CPU. These timings
are canary observations, not a universal performance guarantee.

Navigation diagnostics add no read, wait or retry. Each navigation request emits
one fixed-prefix ASCII JSON row on stderr, bound to its protocol sequence and
display. It reports already-fetched display/window metadata only: at most12
window records, bounded escaped system-window titles and no application titles
or roots. Its total size is bounded by4096bytes, with explicit truncation and
metadata-error flags. Missing display, malformed display values and missing
NavigationBar return distinct safe error codes. Diagnostic failure never turns
an unavailable proof into success. Normal stock reply frames are unchanged.

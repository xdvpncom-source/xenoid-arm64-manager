Xenoid stock UI reader

StockUiServer is a private stdin/stdout bridge to the Android image's own
com.android.commands.uiautomator.DumpCommand.run(String[]). It adds no gesture
implementation, UI backend, APK or listening socket. Each request uses the
stock connect, idle wait, dump and disconnect code; only the app_process JVM
is reused. Normal D and all-interactive-windows W remain distinct results.

Build used for the embedded artifact:
  JDK17 javac --release8; Android SDK Build Tools35.0.0 d8 --min-api26.
  python3 native/xenoid-ui-reader/build.py --javac /path/to/jdk17/bin/javac \
    --d8 /path/to/android-sdk/build-tools/35.0.0/d8 --output /tmp/stock-ui.jar
  SHA256:3305ef8e5e18320bcb82826794c8a56dacbdb4826e971fb7d283df4ff39f7bb5
  JAR size:3955bytes. Only classes.dex is packaged. Desktop test fixtures are
  not included. qa/qa_ui_reader.py embeds deterministic gzip/base64 bytes and
  checks the JAR SHA before use; update the pin only with a reviewed rebuild.

Protocol (big endian): request is magic0x58554931, uint32sequence, uint8flags.
Flags0=stocknormal,1=compressed,2=--windows,3=both,127=quit. Response is magic,
sequence,statusbyte,uint32length,bytes. Maximum XML response8MiB; errors128bytes.
Sequence0 is READY plus actual guest PID/UID/kernel starttime. Other sequences
increase strictly. Informational stock output is redirected to bounded stderr.
Empty/failed stock dumps are errors, never cached successful XML.

Host lifecycle: one lazy JVM per Broker round, actual ADB shell UID/environment,
private0700directory and0600files. Every read checks owned container/image/PID,
userspace epoch and helper PID identity; the Native lock serializes lifecycle.
Stop, timeout, generation change and cleanup close the pipe and reap only the
verified helper and captured compiler descendants. A cancellation marker plus
atomic directory retirement prevents a queued late launch after cleanup.
Cleanup failure retains its owner reference and can be retried. Explicit
inspect_window(reader='legacy') selects the old per-call reader; read failures
never silently switch modes.

Measurements on one paused Android13 QA device: five normal reads kept the same
PID and matched the old reader byte-for-byte. One initial compiler was observed;
none appeared after JVM warmup. Persistent reads took about3s versus13.8s for
one cold stock launch in that sample. PSS was about50.4MiB after startup, so JVM
reuse trades sustained memory for lower repeated compiler CPU. These timings
are canary observations, not a universal performance guarantee.

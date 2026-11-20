This ARM64 Android 13 HWC1 wrapper delegates composition to Xenoid's original
hwcomposer.redroid.so. It changes the advertised mode and the actual VSYNC
callback period together.

Without /data/local/tmp/xenoid-qa-refresh-rate, the original 60/120 Hz modes
and 120 Hz default remain. A valid positive integer in that file selects one
mode at that rate. The file is read when the composer service starts.

Build on macOS or Linux with an Android NDK:
  bash native/xenoid-hwcomposer/build.sh /path/to/android-ndk /tmp/hwc-build

The test_refresh executable runs on an ARM64 Android device and tests the
actual parser/configuration functions. It uses a separate temporary file,
/data/local/tmp/xenoid-qa-refresh-unit-test, which it removes on success.

Device.set_frame_rate is responsible for validating the matching Xenoid
runtime, staging the library, mounting it over the read-only vendor path,
writing the requested rate, restarting graphics only when needed, and
verifying the resulting VSYNC period. Composer restart also restarts
SurfaceFlinger and Zygote on this image, so call it before launching a test
app. The /data files persist across container restart; the mount must be
reapplied by the SDK after a restart. The mount never modifies the original
vendor library. Unmounting it and restarting graphics restores the original
implementation.

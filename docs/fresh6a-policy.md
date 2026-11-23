# Fresh Pixel 6a policy and scenario

This release targets newly created native Bluejay devices. It does not silently migrate an existing Raven policy or reset an existing device's data. The explicit fresh-reset rollout must archive the old global policy, stop old workers, then select the generated Bluejay policy before creating new devices.

The native image owns the coherent fixed Android 13 release tuple, including fingerprint, partition properties and Java Build metadata. Build fingerprint stays constant for this release; it is not a per-device identifier. The default policy keeps Build, CPU/RAM declarations, display/input bounds, storage, USB hardware, camera metadata and sensor metadata fixed. It retains every field in the full JSON catalogue, with native-generated IDs controlled by official Regenerate. Native SIM/location, DRM and Google identity remain native responsibilities; this policy creates no replacement values for them.

The logical display is 1080×2400, density 420, physical PPI 429, advertised refresh 60 Hz. The source-owned native render configuration is separate: 15 FPS, one SwiftShader thread. The real container limits are one CPU, 2 GiB RAM and 512 MiB swap. Reported six-GiB/Tensor metadata does not increase those limits or turn the guest into physical hardware.

`qa-resources.json` uses `fps:60`, `render_fps:15`, `scale:null`, `dpi:420`. In this mode the QA runtime verifies the baked render manifest, asset hashes, properties, live 191-key Build mapping, native display, real cgroup limits and official input mapping. It does not install warm libraries, set WM overrides, call the old HWC rate replacement or assert that property readback measures actual presented FPS. The release still requires real image/Java/graphics and workload qualification before deployment.

Battery design/full capacity is 4410 mAh, minimum capacity 4306 mAh. The default selection changes percentage, voltage and temperature; charge counter follows the selected percentage. Plugged stays 0 and status stays 3 (discharging), matching the observed power-source getters. Charging-source randomization remains disabled because changing its JSON field was not reflected by the native battery service. Only the supported zone0 thermal temperature has a default random pool. Other thermal fields stay visible without speculative changes. Saved custom policies remain untouched and incompatible Build/display pools are rejected before native Regenerate.

Generate the explicit new policy from the public native template:

```sh
python3 qa/qa_fresh6a.py emit-policy \
  --template nativebundle/source/examples/fingerprints/pixel-bluejay-android13.json \
  --revision 1
```

This command prints JSON only. Installation or a controller API caller owns its atomic save and revision check. Existing deployments without `render_fps` retain their old resource semantics. A newly initialized panel starts with the fresh defaults; existing stored settings are not silently rewritten.

The existing `qa/vpn-cycle.py` and `examples/current-vpn-cycle.py` remain byte-identical. Each assigned APK is opened, allowed 90–120 seconds, and an observed close control is used for an ad. After five seconds it handles the applicable terms/notification/VPN dialogs using official Xenoid input, keeps the app foreground for the configured 100 seconds, closes it and clears that assigned app's data. At the successful cycle end it draws 1–10 once; 5 invokes official Regenerate. After native completion it clears GMS/GSF cache, waits two seconds between the configured phases, clears their data and performs microG registration. The next cycle uses the APK currently assigned by the panel. The runner repeats until stopped. An app error requests a recorded retry rather than being counted as a successful cycle.

The scenario never clicks an ad install/purchase/learn-more control as a substitute for Close. It does not modify the uploaded APK. Existing tests cover the order, delays, assignment resolution, official touch route, durable Regenerate retry key and Google cleanup sequence.

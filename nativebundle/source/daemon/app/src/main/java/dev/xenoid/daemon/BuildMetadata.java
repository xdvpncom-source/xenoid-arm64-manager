package dev.xenoid.daemon;

import java.time.Instant;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

/** Pure adapter for optional metadata from the selected, verified profile. */
final class BuildMetadata {
    private BuildMetadata() {}

    static Map<String, String> properties(Map<String, Object> build) {
        Map<String, String> result = new LinkedHashMap<>();
        for (String[] pair : new String[][] {
                {"host", "ro.build.host"}, {"user", "ro.build.user"},
                {"baseband", "ro.build.expect.baseband"}}) {
            if (!build.containsKey(pair[0])) continue;
            Object raw = build.get(pair[0]);
            if (!(raw instanceof String)
                    || !((String) raw).matches("[A-Za-z0-9._-]{1,91}")) {
                throw new IllegalArgumentException("build_metadata_invalid_" + pair[0]);
            }
            result.put(pair[1], (String) raw);
            if ("baseband".equals(pair[0])) result.put("gsm.version.baseband", (String) raw);
        }
        if (build.containsKey("date_utc")) {
            Object raw = build.get("date_utc");
            if (!(raw instanceof String) || !((String) raw).matches("[0-9]{1,11}")) {
                throw new IllegalArgumentException("build_metadata_invalid_date_utc");
            }
            long seconds = Long.parseLong((String) raw);
            ZonedDateTime date = Instant.ofEpochSecond(seconds).atZone(ZoneOffset.UTC);
            if (date.getYear() < 1970 || date.getYear() > 9999) {
                throw new IllegalArgumentException("build_metadata_invalid_date_utc");
            }
            String[] days = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"};
            String[] months = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
            String text = String.format(Locale.US, "%s %s %2d %02d:%02d:%02d UTC %04d",
                    days[date.getDayOfWeek().getValue() - 1], months[date.getMonthValue() - 1],
                    date.getDayOfMonth(), date.getHour(), date.getMinute(), date.getSecond(), date.getYear());
            for (String scope : new String[] {"", "product", "system", "system_ext", "vendor", "odm",
                    "system_dlkm", "vendor_dlkm", "odm_dlkm", "bootimage"}) {
                String prefix = scope.isEmpty() ? "ro.build" : "ro." + scope + ".build";
                result.put(prefix + ".date", text);
                result.put(prefix + ".date.utc", Long.toString(seconds));
            }
        }
        return result;
    }
}

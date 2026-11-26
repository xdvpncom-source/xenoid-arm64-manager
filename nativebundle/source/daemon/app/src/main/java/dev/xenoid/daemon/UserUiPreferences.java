package dev.xenoid.daemon;

import android.app.AlarmManager;
import android.app.UiModeManager;
import android.content.Context;
import android.content.res.Configuration;
import android.os.LocaleList;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.TimeZone;
import java.util.concurrent.TimeUnit;

/** Real user preferences, separate from the immutable SIM/location identity. */
public final class UserUiPreferences {
    static final String MARKER = "xenoid_qa_ui_preferences_v1";
    static final String TARGET_SCHEMA = "dev.xenoid.ui-preferences/v1";
    static final String SNAPSHOT_SCHEMA = "dev.xenoid.ui-preferences-snapshot/v1";
    private static final String[] SYSTEM_KEYS = {"system_locales", "font_scale", "time_12_24", "date_format"};
    private static final String[] GLOBAL_KEYS = {"auto_time_zone", MARKER};
    private static final int MAX_JSON_BYTES = 16384;
    private static final int MAX_SETTINGS_OUTPUT = 256 * 1024;
    private static final java.io.PrintStream OUTPUT = new java.io.PrintStream(
            new java.io.FileOutputStream(java.io.FileDescriptor.out));

    private UserUiPreferences() { }

    static final class RegionalSettings {
        final List<String> locales;
        final String timezone;
        final boolean overridden;
        RegionalSettings(List<String> locales, String timezone, boolean overridden) {
            this.locales = Collections.unmodifiableList(new ArrayList<>(locales));
            this.timezone = timezone;
            this.overridden = overridden;
        }
    }

    /** Only an absent marker returns authority to the original Location profile. */
    static RegionalSettings regionalSettings(Context context, List<?> fallback, String timezone) {
        return regionalSettings(Settings.Global.getString(context.getContentResolver(), MARKER), fallback, timezone);
    }

    static RegionalSettings regionalSettings(String raw, List<?> fallback, String timezone) {
        List<String> locales = new ArrayList<>();
        for (Object item : fallback) locales.add((String) item);
        if (raw != null) {
            try {
                JSONObject target = validateTarget(parse(raw));
                return new RegionalSettings(localeStrings(target.getJSONArray("locales")),
                        target.getString("timezone"), true);
            } catch (Exception invalid) {
                throw new IllegalStateException("invalid_user_preferences", invalid);
            }
        }
        return new RegionalSettings(locales, timezone, false);
    }

    static JSONObject validateTarget(JSONObject input) throws Exception {
        exactKeys(input, "schema", "locales", "timezone", "font_scale", "night_mode", "time_12_24", "date_format", "operation_key");
        if (!TARGET_SCHEMA.equals(text(input, "schema"))) throw invalid();
        localeStrings(input.getJSONArray("locales"));
        timezone(text(input, "timezone"));
        double scale = number(input, "font_scale");
        if (scale <= 0 || scale > 2.0) throw invalid();
        int night = integer(input, "night_mode");
        if (night != UiModeManager.MODE_NIGHT_NO && night != UiModeManager.MODE_NIGHT_YES) throw invalid();
        if (!Arrays.asList("12", "24").contains(text(input, "time_12_24"))) throw invalid();
        String date = text(input, "date_format");
        if (date.isEmpty() || date.length() > 64 || date.indexOf('\0') >= 0) throw invalid();
        new SimpleDateFormat(date, Locale.ROOT);
        if (!text(input, "operation_key").matches("[A-Za-z0-9._:-]{1,200}")) throw invalid();
        return new JSONObject(input.toString());
    }

    private static JSONObject validateSnapshot(JSONObject input) throws Exception {
        exactKeys(input, "schema", "user_id", "values", "settings", "properties");
        if (!SNAPSHOT_SCHEMA.equals(text(input, "schema")) || integer(input, "user_id") != 0) throw invalid();
        JSONObject values = input.getJSONObject("values");
        exactKeys(values, "locales", "timezone", "font_scale", "night_mode");
        localeStrings(values.getJSONArray("locales"));
        timezone(text(values, "timezone"));
        double scale = number(values, "font_scale");
        if (scale <= 0 || scale > 10 || integer(values, "night_mode") < 0 || integer(values, "night_mode") > 3) throw invalid();
        JSONObject settings = input.getJSONObject("settings");
        exactKeys(settings, "system", "global");
        nullableStrings(settings.getJSONObject("system"), SYSTEM_KEYS);
        nullableStrings(settings.getJSONObject("global"), GLOBAL_KEYS);
        JSONObject properties = input.getJSONObject("properties");
        exactKeys(properties, "locale", "timezone");
        for (String key : new String[]{"locale", "timezone"}) {
            Object value = properties.get(key);
            if (!(value instanceof String) || ((String) value).indexOf('\0') >= 0 || ((String) value).length() > 128) throw invalid();
        }
        return new JSONObject(input.toString());
    }

    static List<String> localeStrings(JSONArray values) throws Exception {
        if (values.length() < 1 || values.length() > 8) throw invalid();
        List<String> result = new ArrayList<>();
        for (int i = 0; i < values.length(); i++) {
            Object value = values.get(i);
            if (!(value instanceof String)) throw invalid();
            String text = (String) value;
            if (text.length() > 64 || !text.matches("[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*")) throw invalid();
            Locale locale = new Locale.Builder().setLanguageTag(text).build();
            if (locale.getLanguage().isEmpty() || "und".equals(locale.getLanguage()) || !locale.toLanguageTag().equals(text) || result.contains(text)) throw invalid();
            result.add(text);
        }
        return result;
    }

    private static String timezone(String value) {
        if (value.isEmpty() || value.length() > 128 || !Arrays.asList(TimeZone.getAvailableIDs()).contains(value)) throw invalid();
        return value;
    }

    private static void exactKeys(JSONObject object, String... keys) throws Exception {
        Set<String> expected = new HashSet<>(Arrays.asList(keys));
        if (object.length() != expected.size()) throw invalid();
        for (String key : keys) if (!object.has(key)) throw invalid();
    }

    private static void nullableStrings(JSONObject object, String... keys) throws Exception {
        exactKeys(object, keys);
        for (String key : keys) {
            Object value = object.get(key);
            if (value != JSONObject.NULL && (!(value instanceof String) || ((String) value).length() > MAX_JSON_BYTES || ((String) value).indexOf('\0') >= 0)) throw invalid();
        }
    }

    private static double number(JSONObject object, String key) throws Exception {
        Object value = object.get(key);
        if (!(value instanceof Number) || !Double.isFinite(((Number) value).doubleValue())) throw invalid();
        return ((Number) value).doubleValue();
    }

    private static String text(JSONObject object, String key) throws Exception {
        Object value = object.get(key);
        if (!(value instanceof String)) throw invalid();
        return (String) value;
    }

    private static int integer(JSONObject object, String key) throws Exception {
        double value = number(object, key);
        if (value != (int) value) throw invalid();
        return (int) value;
    }

    private static IllegalArgumentException invalid() { return new IllegalArgumentException("ui_preferences_invalid"); }

    private static JSONObject parse(String raw) throws Exception {
        if (raw.getBytes(StandardCharsets.UTF_8).length > MAX_JSON_BYTES) throw invalid();
        JSONTokener parser = new JSONTokener(raw);
        Object value = parser.nextValue();
        if (!(value instanceof JSONObject) || parser.nextClean() != 0) throw invalid();
        return (JSONObject) value;
    }

    private static Object activityManager() throws Exception {
        return Class.forName("android.app.ActivityManager").getDeclaredMethod("getService").invoke(null);
    }

    private static Configuration configuration() throws Exception {
        return (Configuration) Class.forName("android.app.IActivityManager").getMethod("getConfiguration").invoke(activityManager());
    }

    private static void setConfiguration(JSONArray locales, double scale) throws Exception {
        Configuration delta = new Configuration();
        delta.setLocales(LocaleList.forLanguageTags(join(localeStrings(locales))));
        delta.fontScale = (float) scale;
        Configuration.class.getField("userSetLocale").setBoolean(delta, true);
        Class<?> type = Class.forName("android.app.IActivityManager");
        Object service = activityManager();
        try {
            type.getMethod("updatePersistentConfiguration", Configuration.class).invoke(service, delta);
        } catch (NoSuchMethodException absent) {
            type.getMethod("updatePersistentConfigurationWithAttribution", Configuration.class, String.class, String.class)
                    .invoke(service, delta, "android", null);
        }
    }

    private static String join(List<String> values) {
        StringBuilder out = new StringBuilder();
        for (String value : values) { if (out.length() != 0) out.append(','); out.append(value); }
        return out.toString();
    }

    private static String property(String name) throws Exception {
        return (String) Class.forName("android.os.SystemProperties").getMethod("get", String.class, String.class).invoke(null, name, "");
    }

    private static void property(String name, String value) throws Exception {
        Class.forName("android.os.SystemProperties").getMethod("set", String.class, String.class).invoke(null, name, value);
    }

    /** A standalone app_process has no registered ApplicationThread provider identity. */
    static JSONObject parseSettingsOutput(String output, String... keys) throws Exception {
        JSONObject out = new JSONObject();
        for (String key : keys) out.put(key, JSONObject.NULL);
        Set<String> seen = new HashSet<>();
        String[] lines = output.split("\n", -1);
        for (int i = 0; i < lines.length; i++) {
            String line = lines[i];
            if (line.isEmpty() && i == lines.length - 1) continue;
            int separator = line.indexOf('=');
            if (separator <= 0 || line.indexOf('\r') >= 0 || line.indexOf('\0') >= 0) {
                throw new IllegalStateException("ui_preferences_settings_output_ambiguous");
            }
            String key = line.substring(0, separator);
            if (!key.matches("[A-Za-z0-9_.:-]+") || !seen.add(key)) {
                throw new IllegalStateException("ui_preferences_settings_output_ambiguous");
            }
            if (out.has(key)) out.put(key, line.substring(separator + 1));
        }
        return out;
    }

    private static final class OutputReader implements Runnable {
        final java.io.InputStream input;
        final java.io.ByteArrayOutputStream bytes = new java.io.ByteArrayOutputStream();
        volatile Exception failure;
        OutputReader(java.io.InputStream input) { this.input = input; }
        @Override public void run() {
            try {
                byte[] buffer = new byte[4096];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    if (bytes.size() + count > MAX_SETTINGS_OUTPUT) throw new IllegalStateException("ui_preferences_settings_output_limit");
                    bytes.write(buffer, 0, count);
                }
            } catch (Exception error) { failure = error; }
        }
    }

    static List<String> settingsCommand(boolean global, String key, String value) {
        if (!Arrays.asList(global ? GLOBAL_KEYS : SYSTEM_KEYS).contains(key)
                || value != null && (value.indexOf('\0') >= 0 || value.indexOf('\n') >= 0 || value.indexOf('\r') >= 0)) throw invalid();
        List<String> args = new ArrayList<>(Arrays.asList("/system/bin/cmd", "settings", "--user", "0",
                value == null ? "delete" : "put", global ? "global" : "system", key));
        if (value != null) args.add(value);
        return args;
    }

    private static String settingsCommand(List<String> args) throws Exception {
        java.lang.Process child = new ProcessBuilder(args).start();
        OutputReader stdout = new OutputReader(child.getInputStream()), stderr = new OutputReader(child.getErrorStream());
        Thread outThread = new Thread(stdout, "ui-preferences-settings-out"), errThread = new Thread(stderr, "ui-preferences-settings-err");
        outThread.setDaemon(true); errThread.setDaemon(true);
        outThread.start(); errThread.start();
        try {
            if (!child.waitFor(8, TimeUnit.SECONDS)) throw new IllegalStateException("ui_preferences_settings_timeout");
            outThread.join(1000); errThread.join(1000);
            if (outThread.isAlive() || errThread.isAlive()) throw new IllegalStateException("ui_preferences_settings_timeout");
            if (stdout.failure != null) throw stdout.failure;
            if (stderr.failure != null) throw stderr.failure;
            if (child.exitValue() != 0 || stderr.bytes.size() != 0) throw new IllegalStateException("ui_preferences_settings_command_failed");
            return new String(stdout.bytes.toByteArray(), StandardCharsets.UTF_8);
        } finally {
            finishSettingsCommand(child, outThread, errThread);
        }
    }

    static void finishSettingsCommand(java.lang.Process child, Thread stdout, Thread stderr) throws Exception {
        boolean interrupted = Thread.interrupted();
        try {
            if (child.isAlive()) child.destroyForcibly();
            // The outer managed operation owns the overall deadline. Do not
            // start rollback while our killed settings client is still alive.
            while (true) {
                try { child.waitFor(); break; }
                catch (InterruptedException stopped) { interrupted = true; }
            }
            for (java.io.Closeable stream : new java.io.Closeable[]{child.getInputStream(), child.getErrorStream(), child.getOutputStream()}) {
                try { stream.close(); } catch (java.io.IOException ignored) { }
            }
            try { stdout.join(1000); stderr.join(1000); }
            catch (InterruptedException stopped) { interrupted = true; }
        } finally {
            if (interrupted) Thread.currentThread().interrupt();
        }
    }

    private static JSONObject settings(boolean global, String[] keys) throws Exception {
        return parseSettingsOutput(settingsCommand(Arrays.asList("/system/bin/cmd", "settings", "--user", "0", "list",
                global ? "global" : "system")), keys);
    }

    private static void put(boolean global, String key, String value) throws Exception {
        settingsCommand(settingsCommand(global, key, value));
    }

    private static JSONObject snapshot(Context context) throws Exception {
        Configuration config = configuration();
        JSONArray locales = new JSONArray();
        for (int i = 0; i < config.getLocales().size(); i++) locales.put(config.getLocales().get(i).toLanguageTag());
        // The helper has no application message loop. Invalidate only its own
        // java.util cache so this getter observes AlarmManager's current zone.
        TimeZone.setDefault(null);
        JSONObject values = new JSONObject().put("locales", locales).put("timezone", TimeZone.getDefault().getID())
                .put("font_scale", config.fontScale).put("night_mode", ((UiModeManager) context.getSystemService(Context.UI_MODE_SERVICE)).getNightMode());
        JSONObject settings = new JSONObject().put("system", settings(false, SYSTEM_KEYS))
                .put("global", settings(true, GLOBAL_KEYS));
        JSONObject properties = new JSONObject().put("locale", property("persist.sys.locale")).put("timezone", property("persist.sys.timezone"));
        return validateSnapshot(new JSONObject().put("schema", SNAPSHOT_SCHEMA).put("user_id", 0)
                .put("values", values).put("settings", settings).put("properties", properties));
    }

    private static void apply(Context context, JSONObject target) throws Exception {
        put(true, MARKER, target.toString()); // Publish exact validated expectations before any framework mutation.
        put(true, "auto_time_zone", "0");
        setConfiguration(target.getJSONArray("locales"), target.getDouble("font_scale"));
        ((AlarmManager) context.getSystemService(Context.ALARM_SERVICE)).setTimeZone(target.getString("timezone"));
        ((UiModeManager) context.getSystemService(Context.UI_MODE_SERVICE)).setNightMode(target.getInt("night_mode"));
        put(false, "font_scale", Double.toString(target.getDouble("font_scale")));
        put(false, "time_12_24", target.getString("time_12_24"));
        put(false, "date_format", target.getString("date_format"));
    }

    private static void restore(Context context, JSONObject desired) throws Exception {
        JSONObject values = desired.getJSONObject("values");
        JSONObject settings = desired.getJSONObject("settings");
        // Restore ownership first; an absent marker returns authority to the
        // original Location profile. No radio/SIM fields are written here.
        put(true, MARKER, nullable(settings.getJSONObject("global"), MARKER));
        put(true, "auto_time_zone", "0");
        setConfiguration(values.getJSONArray("locales"), values.getDouble("font_scale"));
        ((AlarmManager) context.getSystemService(Context.ALARM_SERVICE)).setTimeZone(values.getString("timezone"));
        ((UiModeManager) context.getSystemService(Context.UI_MODE_SERVICE)).setNightMode(values.getInt("night_mode"));
        for (String key : SYSTEM_KEYS) put(false, key, nullable(settings.getJSONObject("system"), key));
        JSONObject properties = desired.getJSONObject("properties");
        property("persist.sys.locale", properties.getString("locale"));
        property("persist.sys.timezone", properties.getString("timezone"));
        put(true, "auto_time_zone", nullable(settings.getJSONObject("global"), "auto_time_zone"));
    }

    private static String nullable(JSONObject object, String key) throws Exception {
        return object.isNull(key) ? null : object.getString(key);
    }

    private static boolean sameJson(Object a, Object b) throws Exception {
        if (a == JSONObject.NULL || b == JSONObject.NULL) return a == b;
        if (a instanceof JSONObject && b instanceof JSONObject) {
            JSONObject first = (JSONObject) a, second = (JSONObject) b;
            if (first.length() != second.length()) return false;
            java.util.Iterator<String> keys = first.keys();
            while (keys.hasNext()) { String key = keys.next(); if (!second.has(key) || !sameJson(first.get(key), second.get(key))) return false; }
            return true;
        }
        if (a instanceof JSONArray && b instanceof JSONArray) {
            JSONArray first = (JSONArray) a, second = (JSONArray) b;
            if (first.length() != second.length()) return false;
            for (int i = 0; i < first.length(); i++) if (!sameJson(first.get(i), second.get(i))) return false;
            return true;
        }
        if (a instanceof Number && b instanceof Number) return Math.abs(((Number) a).doubleValue() - ((Number) b).doubleValue()) < 0.00001;
        return a != null && a.equals(b);
    }

    private static boolean matchesTarget(JSONObject current, JSONObject target) throws Exception {
        JSONObject values = current.getJSONObject("values"), system = current.getJSONObject("settings").getJSONObject("system");
        JSONObject global = current.getJSONObject("settings").getJSONObject("global"), props = current.getJSONObject("properties");
        return sameJson(values.get("locales"), target.get("locales")) && sameJson(values.get("font_scale"), target.get("font_scale"))
                && sameJson(values.get("night_mode"), target.get("night_mode")) && sameJson(values.get("timezone"), target.get("timezone"))
                && target.getJSONArray("locales").getString(0).equals(props.getString("locale"))
                && target.getString("timezone").equals(props.getString("timezone"))
                && target.getString("time_12_24").equals(system.getString("time_12_24"))
                && target.getString("date_format").equals(system.getString("date_format"))
                && "0".equals(global.getString("auto_time_zone")) && sameJson(parse(global.getString(MARKER)), target);
    }

    private static JSONObject await(Context context, JSONObject desired, boolean restoring) throws Exception {
        long deadline = SystemClock.elapsedRealtime() + 5000;
        do {
            JSONObject current = snapshot(context);
            if (restoring ? sameJson(current, desired) : matchesTarget(current, desired)) return current;
            Thread.sleep(100);
        } while (SystemClock.elapsedRealtime() < deadline);
        throw new IllegalStateException("ui_preferences_readback_mismatch");
    }

    private static Context systemContext() throws Exception {
        if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        Class<?> type = Class.forName("android.app.ActivityThread");
        Object thread = type.getDeclaredMethod("systemMain").invoke(null);
        return (Context) type.getDeclaredMethod("getSystemContext").invoke(thread);
    }

    private static String errorCode(Throwable failure) {
        while (failure instanceof java.lang.reflect.InvocationTargetException && failure.getCause() != null) failure = failure.getCause();
        String value = failure.getMessage();
        return Arrays.asList("ui_preferences_invalid", "ui_preferences_root_required", "ui_preferences_setting_failed",
                "ui_preferences_readback_mismatch", "invalid_user_preferences", "ui_preferences_settings_timeout",
                "ui_preferences_settings_output_limit", "ui_preferences_settings_command_failed",
                "ui_preferences_settings_output_ambiguous").contains(value) ? value : "ui_preferences_operation_failed";
    }

    public static void main(String[] args) {
        JSONObject before = null;
        Context context = null;
        boolean mutation = false;
        try {
            if (args.length < 1 || args.length > 2 || !("read".equals(args[0]) || "apply".equals(args[0]) || "restore".equals(args[0]))) throw invalid();
            mutation = !"read".equals(args[0]);
            if ((mutation && args.length != 2) || (!mutation && args.length != 1)) throw invalid();
            if (mutation && Process.myUid() != 0) throw new SecurityException("ui_preferences_root_required");
            JSONObject desired = mutation ? parse(new String(Base64.decode(args[1], Base64.DEFAULT), StandardCharsets.UTF_8)) : null;
            if (mutation) desired = "apply".equals(args[0]) ? validateTarget(desired) : validateSnapshot(desired);
            context = systemContext();
            before = snapshot(context);
            JSONObject result = before;
            if (mutation) {
                if ("apply".equals(args[0])) apply(context, desired); else restore(context, desired);
                result = await(context, desired, "restore".equals(args[0]));
            }
            OUTPUT.println(new JSONObject().put("ok", true).put("snapshot", result).toString());
            OUTPUT.flush();
            System.exit(0);
        } catch (Throwable failure) {
            boolean rolledBack = false;
            boolean canRollback = mutation && before != null && context != null;
            if (canRollback) {
                try { restore(context, before); await(context, before, true); rolledBack = true; } catch (Throwable ignored) { }
            }
            try {
                OUTPUT.println(new JSONObject().put("ok", false).put("error", errorCode(failure))
                        .put("rollback_attempted", canRollback).put("rollback_succeeded", rolledBack).toString());
            } catch (Exception ignored) { OUTPUT.println("{\"ok\":false,\"error\":\"ui_preferences_operation_failed\"}"); }
            OUTPUT.flush();
            System.exit(1);
        }
    }
}

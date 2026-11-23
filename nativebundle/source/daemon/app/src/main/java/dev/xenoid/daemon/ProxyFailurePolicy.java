package dev.xenoid.daemon;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Optional policy metadata inside the existing encrypted proxy desired state. */
final class ProxyFailurePolicy {
    static final String SCHEMA = "dev.xenoid.proxy-failure-policy/v1";
    private ProxyFailurePolicy() {}
    static Map<String, Object> normalize(Object value) {
        if (value == null) return null;
        if (!(value instanceof Map)) throw new IllegalArgumentException("invalid_failure_policy");
        Map<?, ?> raw = (Map<?, ?>) value;
        if (!raw.keySet().equals(Set.of("schema", "mode", "retry_delays_seconds"))
                || !SCHEMA.equals(raw.get("schema")) || !"retry_proxy".equals(raw.get("mode"))
                || !(raw.get("retry_delays_seconds") instanceof List)) {
            throw new IllegalArgumentException("invalid_failure_policy");
        }
        List<?> delays = (List<?>) raw.get("retry_delays_seconds");
        if (delays.isEmpty() || delays.size() > 8) throw new IllegalArgumentException("invalid_failure_policy");
        List<Long> copy = new ArrayList<>();
        for (Object item : delays) {
            if (!(item instanceof Integer) && !(item instanceof Long)) throw new IllegalArgumentException("invalid_failure_policy");
            long delay = ((Number) item).longValue();
            if (delay < 1 || delay > 2147483647) throw new IllegalArgumentException("invalid_failure_policy");
            copy.add(delay);
        }
        Map<String, Object> clean = new LinkedHashMap<>();
        clean.put("schema", SCHEMA); clean.put("mode", "retry_proxy");
        clean.put("retry_delays_seconds", Collections.unmodifiableList(copy));
        return Collections.unmodifiableMap(clean);
    }
    static boolean routingReady(Map<String, Object> policy, long generation, Map<String, Object> report) {
        if (policy == null || report == null || !"retry_proxy".equals(report.get("failurePolicy"))
                || !digest(policy).equals(report.get("failurePolicyDigest"))
                || !"active".equals(report.get("phase"))
                || !Boolean.TRUE.equals(report.get("structuralApplied"))
                || !(report.get("dataPlaneVerified") instanceof Boolean)
                || !(report.get("generation") instanceof Long)
                || ((Long) report.get("generation")) != generation) return false;
        Object upstream = report.get("upstreamState");
        boolean verified = Boolean.TRUE.equals(report.get("dataPlaneVerified"));
        String error = report.get("errorCode") instanceof String ? (String) report.get("errorCode") : "";
        return ("unknown".equals(upstream) && !verified && (error.isEmpty()
                    || "source_fetch_denied".equals(error) || "provider_empty".equals(error)
                    || "worker_unavailable".equals(error) || "compiler_unavailable".equals(error)))
                || ("retrying".equals(upstream) && !verified
                    && ("probe_timeout".equals(error) || "probe_failed".equals(error)
                        || "proxy_outbound_unavailable".equals(error)))
                || ("online".equals(upstream) && verified && error.isEmpty());
    }
    static String digest(Map<String, Object> policy) {
        if (policy == null) return "";
        Map<String, Object> clean = normalize(policy);
        StringBuilder body = new StringBuilder("{\"mode\":\"retry_proxy\",\"retry_delays_seconds\":[");
        boolean first = true;
        for (Object delay : (List<?>) clean.get("retry_delays_seconds")) {
            if (!first) body.append(','); first = false; body.append(delay);
        }
        body.append("],\"schema\":\"").append(SCHEMA).append("\"}");
        try {
            byte[] hash = MessageDigest.getInstance("SHA-256").digest(body.toString().getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder();
            for (byte item : hash) { hex.append(Character.forDigit((item >>> 4) & 15, 16)); hex.append(Character.forDigit(item & 15, 16)); }
            return hex.toString();
        } catch (java.security.NoSuchAlgorithmException impossible) { throw new IllegalStateException(impossible); }
    }
}

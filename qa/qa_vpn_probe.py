"""One uncached HTTPS request on a caller-verified Android VPN network handle.

To verify the embedded DEX offline, use JDK 17 and run this module as
    python qa_vpn_probe.py ANDROID_SDK/platforms/android-33/android.jar ANDROID_SDK/build-tools/35.0.1/d8
The Android instance needs no compiler, extra package or configuration.
"""

import base64
import hashlib
import io
import re
import zipfile

JAVA_SOURCE = """package dev.xenoid.qa;

import android.net.Network;
import java.io.IOException;
import java.net.Proxy;
import java.net.URL;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLException;

public final class BoundHttpsCheck {
    public static void main(String[] args) {
        String result = "INTERNAL_ERROR";
        HttpsURLConnection connection = null;
        try {
            Network network = Network.fromNetworkHandle(Long.parseLong(args[0]));
            URL url = new URL("https://connectivitycheck.gstatic.com/generate_204");
            connection = (HttpsURLConnection) network.openConnection(url, Proxy.NO_PROXY);
            connection.setUseCaches(false);
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(5000);
            connection.setRequestProperty("Cache-Control", "no-cache");
            connection.setRequestProperty("Connection", "close");
            result = "HTTP_" + connection.getResponseCode();
        } catch (SSLException exc) {
            result = "TLS_ERROR";
        } catch (IOException exc) {
            result = "IO_ERROR";
        } catch (IllegalArgumentException exc) {
            result = "INVALID_HANDLE";
        } catch (SecurityException exc) {
            result = "BIND_DENIED";
        } finally {
            if (connection != null) connection.disconnect();
        }
        System.out.println("XENOID_BOUND_HTTPS " + result);
        System.exit(0);
    }
}
"""

DEX_SHA256 = "06a41f7048901ee8c785f4dd1ded7af975d06500bbdc2256f47aedf39c47e242"
DEX_BASE64 = (
    "ZGV4CjAzOQAawlKR+5IpeD5+gD0PCqIbEWASxGQVKXOUCQAAcAAAAHhWNBIAAAAAAAAAAAAJAAA9AAAAcAAAABUA"
    "AABkAQAADQAAALgBAAACAAAAVAIAABQAAABkAgAAAQAAAAQDAABwBgAAJAMAANoEAADiBAAA7wQAAP4EAAAKBQAA"
    "EQUAABQFAAAkBQAALgUAADEFAAA1BQAAOAUAADwFAABABQAARAUAAEkFAABgBQAAgQUAAJgFAACvBQAA1QUAAOcF"
    "AAD7BQAAGgYAAC4GAABJBgAAXQYAAG8GAAB/BgAAmQYAAL0GAADbBgAA5QYAAPAGAADzBgAA9wYAAPsGAAAABwAA"
    "BAcAABkHAAAcBwAAMQcAADkHAABABwAATAcAAFIHAABlBwAAdgcAAKoHAACwBwAAugcAAMoHAADPBwAA2gcAAOMH"
    "AAD2BwAAEggAACIIAAA2CAAARAgAAE4IAAAFAAAACAAAAA8AAAAQAAAAEQAAABIAAAATAAAAFAAAABUAAAAWAAAA"
    "FwAAABgAAAAZAAAAGgAAABsAAAAcAAAAHQAAAB4AAAAhAAAAJwAAACgAAAAFAAAAAAAAAAAAAAAJAAAAAQAAAKQE"
    "AAAMAAAAAgAAAKwEAAAKAAAACgAAAAAAAAALAAAACwAAALQEAAANAAAACwAAAKQEAAAOAAAADwAAALwEAAAhAAAA"
    "EgAAAAAAAAAiAAAAEgAAALQEAAAjAAAAEgAAAKQEAAAkAAAAEgAAAMQEAAAlAAAAEgAAAMwEAAAjAAAAEgAAANQE"
    "AAAMAAUAMwAAAA0ADQAfAAAAAgACAC0AAAACAAYAMgAAAAMABwAAAAAAAwAMADAAAAAFAAkANQAAAAcAAQA0AAAA"
    "CAAHAAAAAAALAAkAAAAAAAsABAApAAAACwAFACkAAAALAAMAOwAAAAwACAAsAAAADgAJAAAAAAAQAAcAKwAAABAA"
    "AAAuAAAAEAAIADYAAAAQAAsANwAAABAACAA4AAAAEAAKADkAAAAQAAsAOgAAAAMAAAARAAAACAAAAAAAAAD/////"
    "AAAAAO8IAAAAAAAAAQABAAEAAAAAAAAABAAAAHAQBgAAAA4ABgABAAMABAAAAAAAkAAAABoABAASARICRgUFAXEQ"
    "BQAFAAsDcSAAAEMADAUiAw4AGgQvAHAgDABDAGIEAQBuMAEANQQMBR8FEABuIBMAFQBuIBAAFQATAogTbiAPACUA"
    "biARACUAGgICABoDMQBuMBIAJQMaAgMAGgMqAG4wEgAlAyICCwBwIAcAAgBuEA4ABQAKAG4gCAACAG4QCgACAAwA"
    "OAUqAG4QDQAFACglDQAHUig5B1IoCQdSKA8HUigSB1IoFQ0AKC8aAAEAOAIVAG4QDQACACgQGgAGADgCDQAo+BoA"
    "BwA4AggAKPMaACAAOAIDACjuYgUAACICCwAaAyYAcCAHADIAbiAJAAIAbhAKAAIADABuIAQABQBxEAsAAQAOADgC"
    "BQBuEA0AAgAnAAQAAAAZAAEAHQAAACwACwBcAAAAAgAVAGQAAAAMABUAA3wRbgRpBmQJXFp8EVgEVgZUCVJPAFoA"
    "AQAAAAoAAAABAAAAAQAAAAEAAAAAAAAAAgAAAA4ADQACAAAACgAKAAEAAAATAAAAAQAAABQABjxpbml0PgALQklO"
    "RF9ERU5JRUQADUNhY2hlLUNvbnRyb2wACkNvbm5lY3Rpb24ABUhUVFBfAAFJAA5JTlZBTElEX0hBTkRMRQAISU9f"
    "RVJST1IAAUoAAkpMAAFMAAJMSQACTEoAAkxMAANMTEwAFUxhbmRyb2lkL25ldC9OZXR3b3JrOwAfTGRldi94ZW5v"
    "aWQvcWEvQm91bmRIdHRwc0NoZWNrOwAVTGphdmEvaW8vSU9FeGNlcHRpb247ABVMamF2YS9pby9QcmludFN0cmVh"
    "bTsAJExqYXZhL2xhbmcvSWxsZWdhbEFyZ3VtZW50RXhjZXB0aW9uOwAQTGphdmEvbGFuZy9Mb25nOwASTGphdmEv"
    "bGFuZy9PYmplY3Q7AB1MamF2YS9sYW5nL1NlY3VyaXR5RXhjZXB0aW9uOwASTGphdmEvbGFuZy9TdHJpbmc7ABlM"
    "amF2YS9sYW5nL1N0cmluZ0J1aWxkZXI7ABJMamF2YS9sYW5nL1N5c3RlbTsAEExqYXZhL25ldC9Qcm94eTsADkxq"
    "YXZhL25ldC9VUkw7ABhMamF2YS9uZXQvVVJMQ29ubmVjdGlvbjsAIkxqYXZheC9uZXQvc3NsL0h0dHBzVVJMQ29u"
    "bmVjdGlvbjsAHExqYXZheC9uZXQvc3NsL1NTTEV4Y2VwdGlvbjsACE5PX1BST1hZAAlUTFNfRVJST1IAAVYAAlZJ"
    "AAJWTAADVkxMAAJWWgATWEVOT0lEX0JPVU5EX0hUVFBTIAABWgATW0xqYXZhL2xhbmcvU3RyaW5nOwAGYXBwZW5k"
    "AAVjbG9zZQAKZGlzY29ubmVjdAAEZXhpdAARZnJvbU5ldHdvcmtIYW5kbGUAD2dldFJlc3BvbnNlQ29kZQAyaHR0"
    "cHM6Ly9jb25uZWN0aXZpdHljaGVjay5nc3RhdGljLmNvbS9nZW5lcmF0ZV8yMDQABG1haW4ACG5vLWNhY2hlAA5v"
    "cGVuQ29ubmVjdGlvbgADb3V0AAlwYXJzZUxvbmcAB3ByaW50bG4AEXNldENvbm5lY3RUaW1lb3V0ABpzZXRJbnN0"
    "YW5jZUZvbGxvd1JlZGlyZWN0cwAOc2V0UmVhZFRpbWVvdXQAEnNldFJlcXVlc3RQcm9wZXJ0eQAMc2V0VXNlQ2Fj"
    "aGVzAAh0b1N0cmluZwCeAX5+RDh7ImJhY2tlbmQiOiJkZXgiLCJjb21waWxhdGlvbi1tb2RlIjoicmVsZWFzZSIs"
    "Imhhcy1jaGVja3N1bXMiOmZhbHNlLCJtaW4tYXBpIjozMywic2hhLTEiOiJhYmFhYjQ2OWI1ZWJkNGRkMmJiOTFi"
    "YTBlZDZmNDUyNzdmYWFlNGNhIiwidmVyc2lvbiI6IjguNi4yLWRldiJ9AAAAAgACgYAEpAYBCbwGAAAADAAAAAAA"
    "AAABAAAAAAAAAAEAAAA9AAAAcAAAAAIAAAAVAAAAZAEAAAMAAAANAAAAuAEAAAQAAAACAAAAVAIAAAUAAAAUAAAA"
    "ZAIAAAYAAAABAAAABAMAAAEgAAACAAAAJAMAAAEQAAAHAAAApAQAAAIgAAA9AAAA2gQAAAAgAAABAAAA7wgAAAAQ"
    "AAABAAAAAAkAAA=="
)
TMP_TEMPLATE = "/data/local/tmp/xenoid-vpn-qa.XXXXXXXX"


def _jar():
    dex = base64.b64decode(DEX_BASE64, validate=True)
    if hashlib.sha256(dex).hexdigest() != DEX_SHA256:
        raise RuntimeError("Embedded VPN connectivity probe failed its integrity check")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr(zipfile.ZipInfo("classes.dex"), dex)
    return content.getvalue()


def https_204(shell, handle):
    """Return True only for an HTTP 204 over the specified network with default TLS trust.

    The caller must verify network transport, routes, ownership and handle. No
    environmental default network is used here. A broken probe is an error,
    distinct from an HTTP, network or TLS failure.
    """
    handle = str(handle)
    if not re.fullmatch(r"[1-9][0-9]{0,18}", handle) or int(handle) > 2**63 - 1:
        raise ValueError("Invalid Android network handle")
    jar = _jar()
    directory = shell("mktemp", "-d", TMP_TEMPLATE).strip()
    if not re.fullmatch(r"/data/local/tmp/xenoid-vpn-qa\.[a-zA-Z0-9]{8}", directory):
        raise RuntimeError("Android did not create the expected private QA directory")
    path = directory + "/probe.jar"
    try:
        # Positional arguments avoid interpolating guest paths and data in shell code.
        shell("sh", "-c",
              'umask 077 && printf %s "$1" | base64 -d > "$2" && chmod 0400 "$2"',
              "qa-stage", base64.b64encode(jar).decode("ascii"), path)
        if shell("sha256sum", path).split() != [hashlib.sha256(jar).hexdigest(), path]:
            raise RuntimeError("Staged Android VPN probe failed checksum verification")
        reply = shell("env", "CLASSPATH=" + path, "timeout", "-s", "KILL", "18",
                      "/system/bin/app_process", "/system/bin", "dev.xenoid.qa.BoundHttpsCheck",
                      handle, timeout=23)
        matches = re.findall(r"(?m)^XENOID_BOUND_HTTPS (HTTP_[1-5][0-9][0-9]|TLS_ERROR|IO_ERROR|BIND_DENIED|INVALID_HANDLE|INTERNAL_ERROR)\r?$", reply)
        if len(matches) != 1 or matches[0] in {"INVALID_HANDLE", "INTERNAL_ERROR"}:
            raise RuntimeError("Android VPN probe did not return a valid result")
        return matches[0] == "HTTP_204"
    finally:
        shell("rm", "-f", path)
        shell("rmdir", directory)


def _verify_build(android_jar, d8):
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as where:
        root = Path(where)
        source = root / "BoundHttpsCheck.java"
        source.write_text(JAVA_SOURCE, encoding="utf-8")
        subprocess.run(["javac", "--release", "8", "-g:none", "-encoding", "UTF-8",
                        "-classpath", android_jar, "-d", where, str(source)], check=True)
        cls = root / "dev/xenoid/qa/BoundHttpsCheck.class"
        subprocess.run([d8, "--min-api", "33", "--release", "--lib", android_jar,
                        "--output", where, str(cls)], check=True)
        return (root / "classes.dex").read_bytes()


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        raise SystemExit("Usage: python qa_vpn_probe.py <android-33 android.jar> <d8>")
    if _verify_build(*sys.argv[1:]) != base64.b64decode(DEX_BASE64):
        raise SystemExit("Rebuilt DEX differs from embedded DEX")
    print("Verified embedded Android VPN probe")

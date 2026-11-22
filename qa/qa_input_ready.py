"""Bind and reannounce an existing official Xenoid uinput event inode.

These commands never inject events, create a driver, reload a profile or change
runtime artifacts. Caller MUST hold the instance's native operation lock and
must verify strict InputReader/dispatcher mapping after reannouncement.
"""
from dataclasses import asdict, dataclass
import base64
import hashlib
import json
import re
import shlex
import stat


class NativeInputBindingError(RuntimeError):
    code = 'native_input_binding_invalid'

    def __init__(self, detail='Native input binding could not be verified'):
        super().__init__(detail)


def _require(ok, detail):
    if not ok:
        raise NativeInputBindingError(detail)


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _command(script):
    _require(len(script.encode()) <= 4096, 'Native input command exceeds rootd limit')
    return script


@dataclass(frozen=True)
class InputBinding:
    pid: int
    start_ticks: int
    node: str
    alias_dir: str
    uinput_fd: int
    socket_fd: int
    socket_inode: int
    metadata: str
    profile_sha: str
    status_sha: str
    server_sha: str
    kernel_sha: str

    @property
    def digest(self):
        return _sha(json.dumps(asdict(self), sort_keys=True, separators=(',', ':')))


def probe_command():
    """Read status, service/socket ownership and kernel axes without events.

    A deterministic private alias can supply the SAME node after interrupted
    public unlink/link. No file is written by this probe.
    """
    return _command(r'''set -eu
f(){ printf '{"ok":false,"code":"native_input_binding_invalid"}\n'; exit 2; }
h(){ sha256sum "$1"|cut -d' ' -f1; }
[ "$(getprop init.svc.xenoid-input)" = running ]||f
p=$(getprop init.svc_debug_pid.xenoid-input); case "$p" in ''|*[!0-9]*) f;; esac
[ "$(cat /proc/$p/comm)" = xenoid-input ]||f
[ "$(awk '{print $4}' /proc/$p/stat)" = 1 ]||f
t=$(awk '{print $22}' /proc/$p/stat)
[ "$(readlink /proc/$p/exe)" = /system/bin/xenoid-input ]||f
[ "$(awk '/^Uid:/{print $2":"$3":"$4":"$5}' /proc/$p/status)" = 0:0:0:0 ]||f
[ "$(tr '\000' ' ' </proc/$p/cmdline)" = '/system/bin/xenoid-input serve ' ]||f
s=$(/data/local/tmp/xenoid-input status)||f
n=$(printf '%s' "$s"|sed -n 's/.*"eventNode":"\(\/dev\/input\/event[0-9][0-9]*\)".*/\1/p'); [ -n "$n" ]||f
a=/dev/input/.xenoid-reannounce-$p-$t-${n##*/}
u=''; k=''; si=$(awk '$8=="/dev/socket/.inputd"{print $7}' /proc/net/unix)
case "$si" in ''|*[!0-9]*) f;; esac
for fd in /proc/$p/fd/*; do z=$(readlink "$fd"||true); case "$z" in /dev/uinput) [ -z "$u" ]||f; u=${fd##*/};; "socket:[$si]") [ -z "$k" ]||f; k=${fd##*/};; esac; done
[ -n "$u" ]&&[ -n "$k" ]||f
v=$n
if [ ! -e "$n" ]; then [ ! -L "$a" ]&&[ "$(stat -c '%u:%g:%a' "$a")" = 0:0:700 ]||f; v=$a/event; fi
[ -c "$v" ]&&[ ! -L "$v" ]||f
m=$(stat -c '%d:%i:%f:%t:%T:%u:%g:%a' "$v")
x=/sys/class/input/${n##*/}/device
printf 'STATUS\t%s\nPID\t%s\nSTART\t%s\nNODE\t%s\nALIAS\t%s\nUINPUT_FD\t%s\nSOCKET_FD\t%s\nSOCKET_INODE\t%s\nMETA\t%s\nPROFILE_SHA\t%s\nSERVER_SHA\t%s\nCLIENT_SHA\t%s\nSYSFS_NAME\t' "$s" "$p" "$t" "$n" "$a" "$u" "$k" "$si" "$m" "$(h /data/local/tmp/xenoid-profile/effective.json)" "$(h /proc/$p/exe)" "$(h /data/local/tmp/xenoid-input)"
base64 "$x/name"|tr -d '\n'; printf '\nSYSFS_DEV\t'; cat "/sys/class/input/${n##*/}/dev"; printf 'PROPERTIES\t'; cat "$x/properties"; printf 'GETEVENT\t'; getevent -lp "$v"|sed '/^add device [0-9][0-9]*: /d;s/value [-0-9][0-9]*/value 0/g'|base64|tr -d '\n'; printf '\n'
''')


def _records(text):
    _require(isinstance(text, str) and len(text) <= 32768, 'Invalid input probe output')
    records = {}
    for line in text.splitlines():
        key, sep, value = line.partition('\t')
        _require(sep and key not in records, 'Invalid or duplicate input probe record')
        records[key] = value
    return records


def validate_display_geometry(profile):
    """Validate a generated display and its official raw axes as one preset.

    This is intentionally separate from legacy parse_probe(), which also accepts
    old physical-axis profiles under an independent WM scaling override.
    """
    try:
        display, inputs = profile["display"], profile["input"]
        width, height, density = (display[key] for key in ("width", "height", "densityDpi"))
        axes = {axis: inputs[axis] for axis in ("x", "y")}
    except (KeyError, TypeError) as error:
        raise NativeInputBindingError("Generated display/input geometry is missing") from error
    _require(all(type(n) is int and n >= 2 for n in (width, height)),
             "Native touch display dimensions must be at least two pixels")
    _require(type(density) is int and density > 0, "Native touch display density is invalid")
    for axis, size in (("x", width), ("y", height)):
        expected = {"minimum": 0, "maximum": size - 1}
        actual = axes[axis]
        _require(isinstance(actual, dict) and set(actual) == set(expected)
                 and all(type(actual[key]) is int and actual[key] == value for key, value in expected.items()),
                 "Generated display and native " + axis + " axes disagree")
    return {"width": width, "height": height, "density_dpi": density,
            "raw_bounds": {"x": [0, width - 1], "y": [0, height - 1]}}


def verify_reader_event_node(dump, name, reader_device_id, event_node):
    """Tie the selected InputReader device to the proven official event path."""
    reader = re.search(r"(?s)Input Reader State[^\n]*:\n(.*?)(?:\nUnwantedInteractionBlocker:|\nInput Dispatcher State:)", dump)
    _require(reader is not None, "InputReader state is missing")
    bodies = re.findall(r"^  Device " + re.escape(str(reader_device_id)) + r": " + re.escape(name)
                        + r"\n(.*?)(?=^  Device |^  Configuration:|\Z)", reader[1], re.M | re.S)
    _require(len(bodies) == 1, "The selected InputReader device is ambiguous")
    ids = re.findall(r"^    EventHub Devices:\s*\[\s*(-?\d+)\s*\]\s*$", bodies[0], re.M)
    _require(len(ids) == 1, "The native touchscreen must have one EventHub source")
    hub = re.search(r"(?s)Event Hub State:\n(.*?)\nInput Reader State", dump)
    _require(hub is not None, "EventHub state is missing")
    entries = re.findall(r"^    " + re.escape(ids[0]) + r": " + re.escape(name)
                         + r"\n(.*?)(?=^    -?\d+:|^  Unattached video devices:|\Z)", hub[1], re.M | re.S)
    _require(len(entries) == 1, "The selected EventHub source is ambiguous")
    paths = re.findall(r"^      Path: (\S+)\s*$", entries[0], re.M)
    enabled = re.findall(r"^      Enabled: (\S+)\s*$", entries[0], re.M)
    _require(paths == [event_node] and enabled == ["true"], "InputReader is not using the verified official event node")
    return int(ids[0])


def parse_probe(text, profile):
    """Validate an actual official status + kernel descriptor against effective.json."""
    values = _records(text)
    expected_keys = {'STATUS','PID','START','NODE','ALIAS','UINPUT_FD','SOCKET_FD','SOCKET_INODE','META','PROFILE_SHA','SERVER_SHA','CLIENT_SHA','SYSFS_NAME','SYSFS_DEV','PROPERTIES','GETEVENT'}
    _require(set(values) == expected_keys, 'Incomplete input probe')
    try:
        native = json.loads(values['STATUS'])
        settings = profile['input']
        name = settings['name']
        axes = {a: (int(settings[a]['minimum']), int(settings[a]['maximum'])) for a in ('x', 'y')}
        pid, start, ufd, sfd, socket_inode = [int(values[k]) for k in ('PID','START','UINPUT_FD','SOCKET_FD','SOCKET_INODE')]
        device, inode, mode_hex, major_hex, minor_hex, uid, gid, permission = values['META'].split(':')
        mode, major, minor = int(mode_hex, 16), int(major_hex, 16), int(minor_hex, 16)
        kernel = base64.b64decode(values['GETEVENT'], validate=True).decode()
        kernel_name = base64.b64decode(values['SYSFS_NAME'], validate=True).decode().rstrip('\n')
        direct = int(values['PROPERTIES'].strip(), 16) & 2
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise NativeInputBindingError('Malformed native input evidence') from error
    _require(pid > 1 and start > 0 and min(ufd, sfd) >= 0 and ufd != sfd and socket_inode > 0, 'Invalid native service binding')
    node = values['NODE']
    _require(re.fullmatch(r'/dev/input/event[0-9]+', node) is not None, 'Invalid native event path')
    alias_dir = f'/dev/input/.xenoid-reannounce-{pid}-{start}-{node.rsplit("/", 1)[1]}'
    _require(values['ALIAS'] == alias_dir, 'Invalid native alias binding')
    _require(all(re.fullmatch('[0-9a-f]{64}', values[k]) for k in ('PROFILE_SHA','SERVER_SHA','CLIENT_SHA')), 'Invalid native binary/profile digest')
    _require(values['SERVER_SHA'] == values['CLIENT_SHA'], 'Native client/server binary mismatch')
    _require(values['SYSFS_DEV'] == f'{major}:{minor}', 'Native sysfs device number mismatch')
    _require(stat.S_ISCHR(mode) and major == 13 and minor >= 64 and uid == '0' and gid == '1004' and permission == '660' and int(device) > 0 and int(inode) > 0, 'Untrusted native event inode')
    _require((mode & 0o7777) == 0o660, 'Unexpected native event permissions')
    _require(native.get('ok') is True and native.get('schema') == 'dev.input/v2' and native.get('driverLayer') is True and native.get('directTouch') is True and native.get('persistentDevice') is True, 'Native input service is unavailable')
    _require(native.get('eventNode') == node and native.get('name') == name == kernel_name and direct == 2, 'Native touchscreen identity mismatch')
    _require('INPUT_PROP_DIRECT' in kernel, 'Kernel touchscreen is not DIRECT')
    match = re.search(r'^\s*name:\s*"([^"\n]+)"\s*$', kernel, re.M)
    _require(match is not None and match[1] == name, 'Kernel input name mismatch')
    for axis in ('x', 'y'):
        low, high = axes[axis]
        _require(high > low and native.get(axis+'Min') == low and native.get(axis+'Max') == high, 'Native raw axes mismatch')
        for field in ('ABS_'+axis.upper(), 'ABS_MT_POSITION_'+axis.upper()):
            matches = re.findall(r'\b'+field+r'\s*:\s*value -?\d+, min (-?\d+), max (-?\d+),', kernel)
            _require(len(matches) == 1 and tuple(map(int, matches[0])) == (low, high), 'Kernel raw axes mismatch')
    return InputBinding(pid, start, node, alias_dir, ufd, sfd, socket_inode, values['META'], values['PROFILE_SHA'], _sha(values['STATUS']), values['SERVER_SHA'], _sha(kernel))


def repair_command(binding):
    """Reannounce the SAME validated official inode; preserve alias on failure.

    Returning ok means publication succeeded. It does NOT mean InputReader is
    ready: the SDK must separately require its original strict mapping guard.
    """
    _require(isinstance(binding, InputBinding), 'Expected verified native input binding')
    # Frozen dataclasses may be deserialized externally: validate every value
    # used in the shell even though shlex.quote also prevents interpretation.
    _require(re.fullmatch(r'/dev/input/event[0-9]+', binding.node) is not None and binding.pid > 1 and binding.start_ticks > 0, 'Invalid input binding')
    expected_alias = f'/dev/input/.xenoid-reannounce-{binding.pid}-{binding.start_ticks}-{binding.node.rsplit("/",1)[1]}'
    _require(binding.alias_dir == expected_alias, 'Invalid recovery alias')
    for digest in (binding.profile_sha, binding.status_sha, binding.server_sha, binding.kernel_sha):
        _require(re.fullmatch('[0-9a-f]{64}', digest) is not None, 'Invalid binding digest')
    _require(re.fullmatch(r'[0-9]+:[0-9]+:[0-9a-f]+:[0-9a-f]+:[0-9a-f]+:0:1004:660', binding.metadata) is not None, 'Invalid inode binding')
    _require(min(binding.uinput_fd, binding.socket_fd) >= 0 and binding.socket_inode > 0, 'Invalid fd binding')
    variables = '\n'.join(f'{k}={shlex.quote(str(v))}' for k, v in {
        'p': binding.pid, 't': binding.start_ticks, 'n': binding.node, 'a': binding.alias_dir,
        'u': binding.uinput_fd, 'k': binding.socket_fd, 'si': binding.socket_inode,
        'm': binding.metadata, 'ph': binding.profile_sha, 'sh': binding.status_sha,
        'bh': binding.server_sha, 'kh': binding.kernel_sha, 'bd': binding.digest,
    }.items())
    return _command(variables + r'''
set -eu
f(){ printf '{"ok":false,"code":"native_input_reannouncement_failed","binding_digest":"%s"}\n' "$bd"; exit 2; }
h(){ sha256sum "$1"|cut -d' ' -f1; }
st(){ stat -c '%d:%i:%f:%t:%T:%u:%g:%a' "$1"; }
ck(){
[ "$(getprop init.svc.xenoid-input)" = running ]&&[ "$(getprop init.svc_debug_pid.xenoid-input)" = "$p" ]||f
[ "$(cat /proc/$p/comm)" = xenoid-input ]&&[ "$(awk '{print $4":"$22}' /proc/$p/stat)" = "1:$t" ]||f
[ "$(readlink /proc/$p/exe)" = /system/bin/xenoid-input ]&&[ "$(h /proc/$p/exe)" = "$bh" ]&&[ "$(h /data/local/tmp/xenoid-input)" = "$bh" ]||f
[ "$(readlink /proc/$p/fd/$u)" = /dev/uinput ]&&[ "$(readlink /proc/$p/fd/$k)" = "socket:[$si]" ]||f
[ "$(awk '$8=="/dev/socket/.inputd"{print $7}' /proc/net/unix)" = "$si" ]||f
[ "$(h /data/local/tmp/xenoid-profile/effective.json)" = "$ph" ]||f
s=$(/data/local/tmp/xenoid-input status)||f; [ "$(printf '%s' "$s"|sha256sum|cut -d' ' -f1)" = "$sh" ]||f
}
ck
if [ ! -e "$a" ]; then mkdir -m 700 "$a"||f; fi
[ -d "$a" ]&&[ ! -L "$a" ]&&[ "$(stat -c '%u:%g:%a' "$a")" = 0:0:700 ]||f
if [ -e "$a/event" ]; then [ -c "$a/event" ]&&[ ! -L "$a/event" ]&&[ "$(st "$a/event")" = "$m" ]||f
else [ -c "$n" ]&&[ ! -L "$n" ]&&[ "$(st "$n")" = "$m" ]||f; ln "$n" "$a/event"||f; fi
[ "$(st "$a/event")" = "$m" ]||f
[ "$(getevent -lp "$a/event"|sed '/^add device [0-9][0-9]*: /d;s/value [-0-9][0-9]*/value 0/g'|sha256sum|cut -d' ' -f1)" = "$kh" ]||f
ck
if [ -e "$n" ]; then [ -c "$n" ]&&[ ! -L "$n" ]&&[ "$(st "$n")" = "$m" ]||f; rm -- "$n"||f; fi
if ! ln "$a/event" "$n"; then [ -e "$n" ]||ln "$a/event" "$n"||f; f; fi
[ "$(st "$n")" = "$m" ]||f
ck
rm -- "$a/event"||f; rmdir "$a"||f
printf '{"ok":true,"schema":"xenoid.native-input-reannouncement/v1","reannounced":true,"same_inode":true,"driver_recreated":false,"events_injected":false,"binding_digest":"%s"}\n' "$bd"
''')


def parse_repair(text, binding):
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as error:
        raise NativeInputBindingError('Invalid native reannouncement result') from error
    _require(value == {'ok': True, 'schema': 'xenoid.native-input-reannouncement/v1', 'reannounced': True, 'same_inode': True, 'driver_recreated': False, 'events_injected': False, 'binding_digest': binding.digest}, 'Native reannouncement did not verify its binding')
    return value

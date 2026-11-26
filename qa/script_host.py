#!/usr/bin/python3
"""Root-owned remote service. User scripts only run in separate, unprivileged units."""

import argparse
import base64
from contextlib import ExitStack, closing, contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import http.client
import importlib.util
import json
import math
import os
import queue
from pathlib import Path
import re
import selectors
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree

from qa_lifecycle import QA_ACTIONS, vpn_record
from shared_policy import ANDROID_PACKAGE, APP_ACTIONS, shared_policy


HERE = Path(__file__).resolve().parent
UPSTREAM = Path("/opt/xenoid-fleet/upstream")
STATE = Path("/var/lib/xenoid-panel-scripts")
SERVICE = "xenoid-panel-script"
UNIT = Path("/etc/systemd/system") / (SERVICE + "@.service")
NATIVE_ENV_FILE = Path("/opt/xenoid-fleet/toolchain/env.sh")
TERMINAL = {"stopped", "completed", "failed"}
CHROME = "com.android.chrome"
MAX_MESSAGE = 2048
MAX_EVENTS = 1536
RUN_ID = re.compile(r"[0-9a-zA-Z_-]{1,80}")
TAG = re.compile(r"[0-9a-f]{12}")
# Reproducible source/build instructions: native/xenoid-hwcomposer/README.txt.
# This ARM64 HWC reads the rate from the script-controlled configuration file.
DISPLAY_LIBRARY_SHA256 = 'f415e0aebe91a1de9a11ecddb29ef5ff7b139b2efa60622510a94e77f4cf754c'
DISPLAY_LIBRARY_GZIP = (
    'H4sIAAAAAAAC/+Vae3BUVZo/995O6IQIEcJDXt1kxApRkohJTFT05gE2MQpiRhnLtdPp7qRbOt1NdwfDY4tG2Jmw7EzRwvoAZSK4'
    'Y7pLqtiaOJKq2RHB2mV23VpwkAFX3fBYN4I1g7xMozH7++49Nzndk7bW2vlr96Y63/1+53yPc853vnPuvWfj4sYlsiQx41LY22yU'
    'Y0zldGCeiFWxXPyfxqaybPAmoV46LZNTqZnjEpfLdBXdnEoNhSSXJZY/mkoHJ6RSUY58tVp13tqdSrdz//onji13oFDnD5xNpSOd'
    'JaXKyVyuitur6k6lQSmVGv2y4j8jLtP38NMwu5zL2fJ0Pp1+zFKp0fePQi6bff/LxH9B7k86fYmlUsMetXML97vG7woFvC42G/eh'
    'hdXsf3PdWb6wrLKqsoL9mS/yc9IYeH4GfHYGvDQDXpkBX5QBr8uAN2TA/yID7s6AP5sB/3EGfHcG/O8z4L/KgP9TBvxfM+C/y4B/'
    'lAH/rwz4HzPg1zPgOdLY+JwMeEUGvDYD3pABfyoD/kwGfF0G/K+B38wK2EGevwZHSmT+p+BPp/L/+E/S/n4G3VY+N4h/hfN0OW/K'
    '0laXFyWRl0d4IhZOZ/B8YeaY848/Gc/sdmenw97q9Tt83nVuzjoi7k5vBEzI3eYNR9whIK2B0CoWjoSc7UHm8gWCbj9IeG07czp8'
    'voCTBSOekNvhsrd3QNju9UPe5XP6AmH3SJEz4HfpJSMI/kfSKrjcsBJYm6bQQFtDbvIyHHE4V9mdnlX2VofXx1o1f1qpPmvVjdrt'
    '7e52pydElcC4QyF/gPyPBHxpquH9qjSow58Can61hAIOl9MRHvX+mYDXTyr9PhhPqfusA410kg57mzsS8ba7Oed3+ANhn9sdhEtr'
    'wv5gyOuPtGou2h5eynzeFpevJBxgjUtr64hzEuN51hloD6JNoZKQY43bT5ixbo1jqevmIs4feo1Hn4ml7DuOCrgs4McEXBHwUwIu'
    '7iv6BTxLwAcEXFz/Lgn4OAEfFHCzOKH2juI5AmwW8Fxx3RDw8QI+XcDzBNwq4DeJ+yMBnyDgZQI+UcCrBDxf3N8J+M0CbhNwMY8s'
    'F/DJAr5SwAsEvFnApwi4R8CnCnhQwKcJeKeATxfwqIDfIuBdAj5DwLcL+EwBf0nAZwl4t4DPFvAeAZ8j4E31T9gkZpJGs6p+3b78'
    '25Q9hcz0+A25hWmjza2Vbj82RdYVNIOsrrV+R7vXaXV5w0GfY611pGYpSl2BUCnmXmV5qefZUnHyubV9FU2/Upcj4ijFjHb4SiPt'
    'wdJOTfmC1Y4FITeyUNizIER5LZcbNcy0B5DG7oG1Dn9k0bx1HfdaHc6Id4170bySu1pt63LZiB+6INo0U7n3Y567++sZu0pzCdSM'
    'xg6CzqJGL0Z8ghaA1oOWga4EVRfr++BOG2Nvgp5ayth50FuxqZmFjipq0J8ftjfo+8tbH2LsFdAy0AOg94G+B3oA9BLoeVATOvcS'
    '6HTQlY0YF9BbH4Yc6CnQA6BbHoGfoO+BWjGOVcsYawRdDtoMegeeKXpMo/s9bezWrWDShjnSzDyLhlEsnq8bLSd6B/VB3WieyOeP'
    'BSfSsCbKc3Wj84UwD8eMGKSyDZT36lLnbRV+O3ndKgqk+gnBprzZ6g55p+lvs18wv5j7Ut6eCa/m5yovSXmWYNME9rsTH578/alP'
    'P6kfX0768HtvCdopabJRaXne7DJDeE/uq3m58ut5FsC6IIkxkqPx/OJB9BPT5Ky2vNlWEtuT/ao515NnsdpIgKqP3mvtJzu20dx8'
    'H7Uf2KUH9TZDV1ljugudeZayxlEHRK6I2g/56Ta9H0Z80TyRN3Drn37C+/UQ6kZto7mVsBM85gyMdA4ACwKLaJM5tX1yY2qbChBr'
    'B1G3XNBZBOyUoJPG6T5gx2wj7UztM7l+7E6jqpT3IPsNZN/XZdXl4hjrIyxvz7OofJx+rysRWKP90BPFvOoVHlNPALNiTr2Zqf9X'
    'pva/7BJ5vb9ofnU18MfetLYJ4aDVrUfdVxp43zan9a2a2rdB1D3fMJrrqS+2ABsA9rERs0/lzW4WA/7lCbvydxfsmfrq9NwNFLxP'
    'GVF/+qN/1yIYOQO/96FnAwL4dd3n7RhXcuQVPiDRPAsgiH7MZeiy36p8aLEqH0aflfPrFFZA/CHFfpLo0PDwNKLN6N5LCksw2XLS'
    'KDsDnuTY8HA+0ec65Py7uTzxCueHj0jHh9Yr8UG7KX79iaz41WXZ8aEjLPbDY/d+OAAd3XJ0Unyqmoxvtgz2jGe9m/Brwe8rxZw4'
    'Ojx8yfxJWeyqIiX68TsD7LoiJ+YpbCd8KifZ5QjXQ6jH+ktiNSy6ohBlag7rfRfY3KqS2BXYmAvMama95nw1OTAOVFKTPaB769jx'
    'M0pB4h3FMsikB04iZxb357CKbsU+2A0d5vvUpI3kVDVpJlqvJo+SvE1NdhFtRDnR5SgnuhLl2aw35yk1uRV0PvzZ+xg7fq6Oxc4o'
    'luJ3jsw5OAh/Ph/PkoY9bNIqBsZDLov1WlFfqoiuSMpoH+R7jjx9EHUS18ax5PTGCXHzJda3DmmB+tc8g2n9Pn8Wy49+oJD+RNcv'
    'pscJ65nJ8rs+uCV+EXqwOSy/Bjt4Xi7oVyYnLgCjfsMUKYC9ST1a39sHLVLOxZo81nR12RKM02KMV318aH1dHOMXozE3V6maz6Tj'
    'NOkwRSd9ifigvhLLPkDZz2W22gYb0UldMwbGqPNbap8UnXQGZdGQSfP9qKyPKfmKWC6gsae2bJ2OaR5fPET3R9HWrYXj4sY90c85'
    'rV2ENt9m1soGLDq2dC5hORpmm8uxW9FftVmxrttyNbyoWMe3lrJ8rQ9vG6/hnnLev5Us/xxipOu2PF1PNct/MZslozIb6nqGJfdQ'
    'H8u639QW4mk8uqmP0tq9DVg/dL2q0SkPXRunJrvRB0UYd+SsZNeTUt/X4F9G+bAiY95kYSyyMWfGxYfqpNjjmDO/Rb8Uo/5RjJv1'
    '9OFFcfmvVnw+sTtEca7FPCuJ9WAumbuzYrkuqS/n1yypzY8NpTEmRRf9mPpZZr3wN9H/AxabC12yObrinDIlsRllFG/wLbGFxvHG'
    '2svY1DVRPYrfs/BdQd1r4NH+nVj7eo+K8ZNF8fMgfF6C2FkcRxu02HnnmfKqTq4bz719uj4Wq5WiK9YAH1DUpEWacrF7aLhpq6z2'
    'eYeGKyg+zVksliOp1cXz2KP2KWz15uHhpgc2sdUq6NJvhity1JdXmE3s+NLwGyGyv/fr4SaLia1eBWpGGc2/ja1Snw3l5pxDM7x7'
    'p8ZPoH9rYHNo/W/Qr1J8CH4U5KlJGrMngO84rN+vxP0V5D2HrCbxiNRL43d1WQ3adbjHyiwnjXtqH/2G1v8D2i1Bp6zpnAqdVxS5'
    'GLmr+BD0qdD3IXTTPdn/IfpuX66aVCTSPQW5rQD9O7n46rJa6CHdR3qaNTujvG7nN/EryB9kg8acHVNi86HPE8qKUXubEYM5Ektu'
    'PMySt9NYw948Kn+ypq8L86s5oSYpzouoLIS2om0UozZaQhGnzWvUpErtlajcFLsg6Tq+kChmJyf+oFFL8RXkLKP9LIh2IFeSv9q9'
    'qt9Tfl/KpGLKN/MVViH2+RVFKd6IsYIfTey4ghxvTnwF3TSHdrRIfd9I+jy6rmHIr8i9PfBLj0NL8Tn89HllKRbHwsHXBbrHo0y5'
    '3cxWb0K8kAzFVCH6iHwkPYpN8LlRvxd9vMr79wrWnmPcnx3o1+Pcp02F2THK/eQH8Sb4eD7NR8O3aFrMDK3/dfwrjOP1J2TYUzR7'
    'c7EG2NEnF02s6QHGqsk+2Sb/zE1G+ycnriGuyOZZZUox3dO4nEEMvQ2/3pLZxc/Rp9chS/7uBfZi66Y+ut+H+64J07Zt+RXaKk/d'
    'JrHo86xPHep58/QHPdUDl9kpx9OS1LbHOr15DnQj3uvgowofa+H3ez3YIxekY9SWffDrXdouwk/TSoopy0ljDGpNrNfD4D9oTq+a'
    'zM1ix+2Yqw13bt3VgPltn8e+bMGO9AJiEs/pBQ/lqJfzkBeiJrZMymLLtknbdr2P/NUgjf+X5z45vOghlcW+UHITdVu6H/mbuZD9'
    '6QP7a7e89siqTSzmoPwg5V7shr6GzuKDpN8CnnCyTbHiZciHY8xXmqsUr5KEdqxWk1lYw9PnYzdvF57Bes8hh+a8pSYvoA7Z9FIc'
    'Q07+Uu2j8b+OeL6qKBgrU4L2A1aMF/JlxdHDWTF6lZBzOjwYl1nfW5OQS5eoBylvNcPP6PBwxVhz3ohJiu9fwodx0GHE53yM647W'
    'Gm2Mi/V5mjInjH1WDeZgoTJ4N/n9QE3hRazDTaJuYx5SXxQYcxH58xZpNC9Owv0vMd4e5EUPz4tjzUMtFjxqEvvbXjEe9HVNjn89'
    'kyVvtJmwzxDWuM1MW+OMvEZrC61lX2LP43m6ps+MdZZyEfHiujMX+xbYT9Da8/VMis0l0Ls4fqON9jC1I+vQ2hee/2Aa1oWrqHv6'
    'o9cv3wB9AmvDLX/57uVOjMvpeWcve1pq+oowTpT7HLD1Gbdtw3pDZbbh4XKS82BOncE6Rnu2Kes9g/2oN/CGGsf6tpp8NM+bttss'
    '/XyXXTFfNEtTtnUfbrpRP1698eTkrl2YKzttG3+0O2foyR1FQ7ZtOZ9P2z3wBouf5fpoTUbuSPwb7fXy9fXOsPWPY9gpgJ0e7qeW'
    '44ETPwUxQj53wWfy164UXKSymfDXdmTnro3KlIsdGFeztHPXDGnmtt2QuYbx6snWc42+LunrPo3xNWVmguR/ym1dRXnX9eHy3VhT'
    'fcCG60wY24nxb5blY4+vxF6fVh2PY/9/AbH1C8s98QZJjp2jvfw4tv89zOkt08tiZydK+/+uTsI+XE6cnybvp7z5mUXZ/xjiwMPH'
    'vwhzO47YiO9bFB9Qos/H97Hj5kv/cTmx7/64NUvd3/2TRfu7yCchJrBmN32zbDnGf1kcfmnjX8cfqbX3oevfiWOfeeDmT9Vk/tns'
    'mBXPNrS/M35a2VmUyeO0Mo3/DHyhwF8AXyvwfwDfIvCXwT8n8NfB7xX4G+APC/y35IvAyzWwbx7ls8EXCnwu+FqBnwC+ReAngX9O'
    '4KeC3yvwM8AfFvg54M8KfCHZzxnlbyP7Al9M9gW+hOwL/EKyL/CVZF/g7yH7An8/2Rf4WrKfO8ovIfsC30D2Bf4Rsj/Cb795BdkX'
    '+MfJvs4b7+/VtO/sxgvOn0mp/Cv8xngn/Trn57DU79zYvwdEOdav8828gsR54/uRwnlD3ng3HDWnvmc9JhvnGPh72tdG36OJ3yGM'
    'd/5mXtF4zjfeNxjfCnp4ReMd/kHu8LVvdX+6eLnxjrmI99cslvre8BJvr5nX/5bzNs4Pc17+nt+nrU///6JdPP6aHqb37EzazAfu'
    'eU7VvLTzGf/Xrkavf5U7dI+1sbHeuvDOkrKSMmtR6eMBX0e7O1zKT1SUtnR4fa6WQKQ0HHKWtgUCbT530OfuXODg5T7fmvYFoYrK'
    'u6qqyhaE3D63I+wuDXRE9IJgKPCM26kz1gp3dWVlZXVrWWXZ3XeXVVc7HOV3Lqwuc7payp0V7spWR1lrRfVd5dXzjeMc1qI776qu'
    'qq6qqrrDenuwLXCHdUFLwBcB44sQ0+4jrAUWXdaA38q9cM63On0Of5t1jTsU9gI32uaJRILhe0pLueslemvCgY6Q013iDLSXRgIB'
    'n9Pj8PpTnP8efrOSVq/fa3eEQo61rCTi7owwUtzu9uPGH4i4SwzbXpeGtQUiJUEfbtBzDv3OtdYfjoRYidtjbw052t12jwtcm7+j'
    'hLfHDpa+xZBMSSigyYZIjGvR7oT6usq17TrocYQ9ukjAHnS4XF5/26gtrSp9JWIlYQ99OHa0oG6ArOk2/xxhN4vnTiM/ZTrvw9LO'
    '+xjXCzzHyml516BGws7ma4I5LR9v5TlyRD47lapp39vS6Y94zjV4I+/a0uSzuUy6/3voTIggb+R9g1rT7JnHOLejCOXGumLQg9Kf'
    'njsSr43cJ0PeWCebzWOPV3r/deiLTCCTvDWDvEGruf3atHXXoMa6K/NzM+n2T/A2LTS+I49PpV+Ncb5KvB4Svpt+1/m2TPH3dpp8'
    '0U2pNF0gvVtvEs4Cppw75B/NL+V9t/370+RH1rU5aYGTof0eLm9Ml5Fzgdax/TWPcY5toiBv7FOKrGPbS5d/l/tuyI+cZ7SmnmPM'
    'TpM39Jal+W/sy3rm8vrSd9vv4Zghf4B/fzfOZS6/6bvnz8dp9kfOcRaOLZBufzKvUpaGH/iBTh/LMH8N+s8s9bzKyHlbfq72LPtu'
    '//8bTk5ryqgrAAA='
)
DISPLAY_LIBRARY = "/vendor/lib64/hw/hwcomposer.raven.so"
DISPLAY_BASE_LIBRARY = "/vendor/lib64/hw/hwcomposer.redroid.so"
DISPLAY_RATE_FILE = "/data/local/tmp/xenoid-qa-refresh-rate"
DISPLAY_STAGE_DIR = "/data/local/tmp/xenoid-qa-display"

ANDROID_FOCUS = re.compile(r"^Window\{([0-9a-fA-F]+) u([0-9]{1,6}) ([^/\s]+)/([.\w$]+)\}$", re.ASCII)
NATIVE_NAV_PREFIX = "xenoid-native-navigation/v1:"
NATIVE_NAV_MAX_AGE_SECONDS = 5.0
UNIT_CONTENT = """[Unit]
Description=Xenoid device script supervisor for phone-%i
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=/opt/xenoid-fleet/upstream
Environment=XENOID_PROJECT=/opt/xenoid-fleet/upstream
ExecStart=/usr/bin/python3 /opt/xenoid-fleet/panel-scripts/script_host.py worker %i
Environment=HOME=/root
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=control-group
UMask=0077
[Install]
WantedBy=multi-user.target
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def adopt_native_environment():
    """Load the official toolchain exports before native modules cache paths."""
    try:
        info = NATIVE_ENV_FILE.lstat()
    except FileNotFoundError:
        return False  # Older installations can still run their existing ADB scripts.
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError("The official toolchain environment must be private from other writers")
    # Match Connection.root + official Upstream.call, including sudo's HOME
    # and secure_path. Native artifact identities hash these environment inputs;
    # inheriting systemd's shorter PATH makes installed artifacts appear stale.
    result = subprocess.run(["sudo", "-n", "-H", "--", "/bin/bash", "-e", "-c", 'source "$1" >/dev/null; exec /usr/bin/env -0',
                             "xenoid-panel-native-env", str(NATIVE_ENV_FILE)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("The official native toolchain environment could not be loaded")
    values = dict(item.decode().split("=", 1) for item in result.stdout.split(b"\0") if b"=" in item)
    # Pinned native artifacts._BASE_ENV and per-target environment declarations,
    # plus the toolchain/ADB compiler variables already supported by the SDK.
    allowed = {"HOME", "PATH", "TMPDIR", "JAVA_HOME", "RUSTUP_HOME", "CARGO_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT",
               "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "XENOID_NDK_VERSION", "ANDROID_API", "ANDROID_BUILD_TOOLS",
               "XENOID_ANDROID_API", "DEVELOPER_DIR", "SDKROOT", "DOCKER_HOST", "DOCKER_CONTEXT", "COLIMA_PROFILE",
               "XENOID_DOCKER_CONTEXT", "XENOID_STANDALONE_APK", "XENOID_PROXY_CC", "NDK_HOME", "TEMP", "TMP",
               "CC", "CXX", "AR", "RANLIB"}
    for key in allowed:
        if key in values:
            os.environ[key] = values[key]
        else:
            os.environ.pop(key, None)
    return True


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"Expected integer between {low} and {high}")
    return value


def device_number(value):
    if not re.fullmatch(r"[1-9][0-9]{0,15}", str(value)):
        raise ValueError("Invalid device ID")
    return integer(int(value), 1, (1 << 53) - 1)


def read_json(limit):
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Request too large")
    return json.loads(data)


def validate(config):
    try:
        shared_policy("source", source=config.get("source"))
    except OverflowError as error:
        raise ValueError("Invalid script source") from error
    for key in ("script_id", "version"):
        integer(config.get(key), 1, (1 << 53) - 1)
    if not RUN_ID.fullmatch(str(config.get("run_id", ""))) or not TAG.fullmatch(str(config.get("resource_tag", ""))):
        raise ValueError("Invalid run identifier or device identity")
    if config.get("instance_id") is not None and not re.fullmatch(
            r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", str(config["instance_id"])):
        raise ValueError("Invalid instance UUID")
    for key in ("repeat_count", "initial_delay", "interval", "timeout", "command_timeout"):
        value = config.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a non-negative integer (0 means unlimited for counts/timeouts)")
    if config.get("language") not in {"python", "bash"}:
        raise ValueError("Unsupported script language")
    checked = {k: config.get(k) for k in ("script_id", "version", "language", "source", "run_id", "resource_tag",
                                       "instance_id", "repeat_count", "initial_delay", "interval", "timeout")}
    checked["command_timeout"] = config.get("command_timeout", 0)
    checked["verify_apk"] = config.get("verify_apk", False)
    if type(checked["verify_apk"]) is not bool:
        raise ValueError("verify_apk must be a boolean")
    for key in ("package", "android_user"):
        value = config.get(key)
        if value is not None:
            if key == "package" and (not isinstance(value, str) or not ANDROID_PACKAGE.fullmatch(value)):
                raise ValueError("Invalid Android package")
            if key == "android_user" and not (str(value).isdigit() or value == "current"):
                raise ValueError("Invalid Android user")
            checked[key] = value
    assigned = config.get("assigned_apk")
    if assigned is not None:
        if not isinstance(assigned, dict) or set(assigned) != {"id", "sha256"}:
            raise ValueError("Invalid APK assignment")
        integer(assigned["id"], 1, (1 << 53) - 1)
        if not isinstance(assigned["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", assigned["sha256"]):
            raise ValueError("Invalid assigned APK digest")
        checked["assigned_apk"] = assigned.copy()
    for key in ("qa_canary", "window_actions", "read_only_window", "shared_app", "identity_reservations"):
        if key in config:
            if type(config[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
            checked[key] = config[key]
    if "app_test" in config:
        if not isinstance(config["app_test"], dict):
            raise ValueError("Invalid app test settings")
        checked["app_test"] = config["app_test"].copy()
    if "expected_container_id" in config:
        container = config["expected_container_id"]
        if not isinstance(container, str) or not re.fullmatch(r"[0-9a-f]{64}", container):
            raise ValueError("Invalid expected container identity")
        checked["expected_container_id"] = container
    if "assigned_apk_size" in config:
        checked["assigned_apk_size"] = config["assigned_apk_size"]
    if checked.get("shared_app"):
        checked.update(shared_policy("launch", config=config))
    return checked


@contextmanager
def db(folder):
    with closing(sqlite3.connect(folder / "runs.db", timeout=15)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            yield conn


def prepare(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with db(folder) as conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, created TEXT NOT NULL, config TEXT NOT NULL, state TEXT NOT NULL,
          iteration INTEGER NOT NULL DEFAULT 0, current INTEGER NOT NULL DEFAULT 0,
          updated TEXT NOT NULL, resume_at REAL NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS current_run ON runs(current) WHERE current=1;
        CREATE TABLE IF NOT EXISTS retired (id TEXT PRIMARY KEY, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, time TEXT NOT NULL,
          level TEXT NOT NULL, message TEXT NOT NULL, iteration INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS run_events ON events(run_id,seq);
        CREATE TABLE IF NOT EXISTS regeneration_receipts (
          run_id TEXT NOT NULL, iteration INTEGER NOT NULL, operation_key TEXT NOT NULL,
          document TEXT NOT NULL, PRIMARY KEY(run_id,iteration,operation_key)
        );
        """)
        if "pruned_through" not in {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}:
            conn.execute("ALTER TABLE runs ADD COLUMN pruned_through INTEGER NOT NULL DEFAULT 0")


def record(conn, run, iteration, level, message):
    message = str(message).replace("\x00", "")
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE-13] + "… [truncated]"
    conn.execute("INSERT INTO events(run_id,time,level,message,iteration) VALUES (?,?,?,?,?)",
                 (run, now(), level, message, iteration))
    cutoff = conn.execute("SELECT seq FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1 OFFSET ?",
                          (run, MAX_EVENTS)).fetchone()
    if cutoff is not None:
        conn.execute("UPDATE runs SET pruned_through=? WHERE id=?", (cutoff[0], run))
        conn.execute("DELETE FROM events WHERE run_id=? AND seq<=?", (run, cutoff[0]))


class RegenerationReceipts:
    """Private, durable operation decisions and native checkpoints, outside Android."""

    def __init__(self, folder, run, iteration):
        self.folder, self.run, self.iteration = Path(folder), run, iteration

    def load(self, key):
        with db(self.folder) as conn:
            row = conn.execute("SELECT document FROM regeneration_receipts WHERE run_id=? AND iteration=? AND operation_key=?",
                               (self.run, self.iteration, key)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, key, document):
        with db(self.folder) as conn:
            conn.execute("INSERT OR REPLACE INTO regeneration_receipts VALUES(?,?,?,?)",
                         (self.run, self.iteration, key, json.dumps(document, sort_keys=True)))

    def pending(self):
        with db(self.folder) as conn:
            rows = conn.execute("SELECT * FROM regeneration_receipts ORDER BY rowid").fetchall()
        return [(row["run_id"], row["iteration"], row["operation_key"], json.loads(row["document"]))
                for row in rows if json.loads(row["document"]).get("status") != "committed"]


def update(folder, run, state, iteration, message, level="info", resume_at=0):
    with db(folder) as conn:
        row = conn.execute("SELECT state FROM runs WHERE current=1 AND id=?", (run,)).fetchone()
        if row is None or row["state"] in TERMINAL:
            return
        conn.execute("UPDATE runs SET state=?,iteration=?,updated=?,resume_at=? WHERE id=?",
                     (state, iteration, now(), resume_at, run))
        record(conn, run, iteration, level, message)


def snapshot(folder, number, run=None, after=None, limit=32, *, include_identity=True):
    missing = dict(state="missing", run_id=run, device_id=number, events=[], cursor=after or 0, has_more=False)
    if not (folder / "runs.db").exists():
        return missing
    with db(folder) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            return missing
        row = conn.execute("SELECT * FROM runs WHERE id=?" if run else "SELECT * FROM runs WHERE current=1",
                           (run,) if run else ()).fetchone()
        if row is None:
            if run and conn.execute("SELECT 1 FROM sqlite_master WHERE name='retired' AND type='table'").fetchone():
                retired = conn.execute("SELECT state FROM retired WHERE id=?", (run,)).fetchone()
                if retired:
                    missing["state"] = retired["state"]
            return missing
        config = json.loads(row["config"])
        result = {k: config[k] for k in ("script_id", "version", "run_id", "resource_tag", "instance_id")}
        result.update(state=row["state"], device_id=number, iteration=row["iteration"], updated=row["updated"])
        if include_identity and config.get("identity_reservations"):
            from identity_reservations import poll as identity_poll
            result.update(identity_poll(folder, row["id"]))
        try:
            from qa_profile_runtime import status_for_binding
            profile_status = status_for_binding(config.get("instance_id"), config.get("resource_tag"),
                                                config.get("expected_container_id"))
            if profile_status is not None:
                result["profile_status"] = profile_status
        except (OSError, ValueError):
            pass
        if after is not None:
            result["pruned_through"] = row["pruned_through"] if "pruned_through" in row.keys() else 0
            rows = conn.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                                (row["id"], after, limit + 1)).fetchall()
            result.update(events=[dict(item) for item in rows[:limit]], has_more=len(rows) > limit,
                          cursor=rows[min(len(rows), limit)-1]["seq"] if rows else after)
        return result


def ctl(*args, timeout=36):
    process = subprocess.run(["systemctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                             timeout=timeout, check=False)
    if process.returncode:
        raise RuntimeError(process.stderr.decode(errors="replace").strip()[:400] or "systemctl failed")


def install_unit():
    with (STATE / "unit.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if UNIT.is_file() and UNIT.read_text() == UNIT_CONTENT:
            return
        with tempfile.NamedTemporaryFile("w", dir=UNIT.parent, delete=False) as temp:
            path = Path(temp.name)
            temp.write(UNIT_CONTENT)
            os.fchmod(temp.fileno(), 0o644)
        try:
            os.replace(path, UNIT)
            ctl("daemon-reload")
        finally:
            path.unlink(missing_ok=True)


def unit(number):
    return f"{SERVICE}@{number}.service"


def trusted_device(number, config):
    path = str(UPSTREAM / "src")
    if path not in sys.path:
        sys.path.insert(0, path)
    from xenoid.backend import RuntimeManager
    from xenoid.config import resolve_instance

    context, settings, lease = resolve_instance(f"phone-{number}", project_root=UPSTREAM, migrate_legacy=False)
    if (context.resource_tag != config["resource_tag"] or
            config.get("instance_id") is not None and context.instance_id != config["instance_id"]):
        raise ValueError("The registered device identity changed; assign this script to the new device explicitly")
    return RuntimeManager(context, settings, lease)


def _native_reserve_read_guard(target, manager, lock_info, remaining):
    """Prove a current worker holds EX while awaiting an unacknowledged reserve.

    This grants only a fresh read alongside that paused owner. It never treats
    the retained request as a new observation or changes reservation state.
    Called on both sides of the read; any changed proof discards the result.
    """
    from identity_reservations import PROC, active_request, digest, directory, read, run_folder

    def require(value):
        if not value:
            raise ValueError("identity_observation_not_waiting_reserve")

    def private_file(path):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        return info

    folder = directory(STATE / str(target["device_id"]))
    database = folder / "runs.db"
    db_info = private_file(database)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=remaining(.25))) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rows = conn.execute("SELECT id,config,state,iteration,current FROM runs WHERE current=1").fetchall()
        require(len(rows) == 1)
        row = dict(rows[0])
        config = json.loads(row["config"])
        require(row["state"] in {"queued", "running", "waiting"} and config.get("run_id") == row["id"]
                and config.get("identity_reservations") is True
                and config.get("instance_id") == target["instance_id"]
                and config.get("resource_tag") == target["resource_tag"]
                and config.get("expected_container_id") == target["container_id"])
        root = run_folder(folder, row["id"])
        recovery = None
        request_run, request_iteration, request_config = row["id"], row["iteration"], config
        if root is None or not os.path.lexists(root / "active.json"):
            from identity_reservations import recovery_envelope, validate_recovery_envelope
            recovery = recovery_envelope(folder, row["id"], remaining=remaining)
            require(recovery is not None)
            recovered = validate_recovery_envelope(folder, row["id"], recovery, remaining=remaining)
            require(recovered["binding"] == target and recovered["current_config"] == config)
            request_run = recovery["original_run"]
            request_iteration = recovery["original_iteration"]
            request_config = recovered["original_config"]
            root = run_folder(folder, request_run)
            require(root is not None)
        else:
            require(row["state"] in {"running", "waiting"})
        active, request = active_request(root, request_run)
        if recovery is not None:
            require(request == recovered["request"] and request["request_sha256"] == recovery["request_sha256"])
        phase = request.get("phase")
        require(request.get("schema") == "dev.xenoid.native-identity-request/v1"
                and request.get("binding") == target
                and (phase == "reserve" or recovery is not None and phase == "finalize")
                and isinstance(request.get("before_values"), dict) and len(request["before_values"]) == 13)
        request_path = root / active["path"]
        operation = directory(request_path.parent)
        require(operation.name == hashlib.sha256(request["operation_key"].encode()).hexdigest())
        owner = read(operation / "owner.json")
        require(all(owner.get(key) == request.get(key) for key in
                    ("binding", "run_id", "operation_key", "native_transaction", "context")))
        receipt_row = conn.execute("SELECT document FROM regeneration_receipts WHERE run_id=? AND iteration=? AND operation_key=?",
                                   (request_run, request_iteration, request["operation_key"])).fetchone()
        require(receipt_row is not None)
        receipt = json.loads(receipt_row[0])
        receipt_binding = {key: target[key] for key in ("instance_id", "resource_tag", "container_id")}
        require(receipt.get("binding") == receipt_binding and receipt.get("status") == "pending"
                and receipt.get("identity_reservation_context") == request.get("context")
                and request.get("context", {}).get("scenario_sha256") == hashlib.sha256(request_config["source"].encode()).hexdigest()
                and receipt.get("before_digest") == request.get("native_before", {}).get("stableDigest"))
        if recovery is not None:
            require(receipt == recovered["receipt"])
        if phase == "reserve":
            require(request.get("values") is None and request.get("native_state") is None
                    and "values" in request and "native_state" in request
                    and not any(receipt.get(key) is not None for key in
                                ("native_journal", "native_completion", "result", "completed_at", "profile_apply")))
            expected_values = request["before_values"]
        else:
            from identity_reservations import values_from_target
            native = request.get("native_state")
            require(isinstance(native, dict) and native.get("phase") == "committed"
                    and native == receipt.get("native_journal")
                    and native.get("transactionId") == request["native_transaction"]
                    and native.get("before") == request["native_before"] and native.get("target") == request["target"]
                    and not any(receipt.get(key) is not None for key in
                                ("native_completion", "result", "completed_at", "profile_apply")))
            expected_values = values_from_target(request["target"], request["location_sidecar"])
            require(request.get("values") == expected_values and len(expected_values) == 13)
    after_db = private_file(database)
    require((db_info.st_dev, db_info.st_ino) == (after_db.st_dev, after_db.st_ino))

    # MainPID and its real proc identity must own this exact native lock inode.
    shown = subprocess.run(["systemctl", "show", unit(target["device_id"]),
                            "--property=ActiveState,SubState,MainPID,ControlGroup"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=remaining(2), check=False)
    require(shown.returncode == 0)
    props = dict(line.split("=", 1) for line in shown.stdout.decode().splitlines() if "=" in line)
    require(props.get("ActiveState") == "active" and props.get("SubState") == "running"
            and props.get("MainPID", "").isdigit() and int(props["MainPID"]) > 1)
    pid = int(props["MainPID"])
    process = PROC / str(pid)
    require(process.stat().st_uid == 0)
    status = (process / "status").read_text()
    require(re.search(r"^Uid:\s+0\s+0\s+0\s+0\s*$", status, re.M))
    argv = (process / "cmdline").read_bytes().split(b"\0")
    require(argv == [b"/usr/bin/python3", str(HERE / "script_host.py").encode(),
                     b"worker", str(target["device_id"]).encode(), b""])
    fields = (process / "stat").read_text().rpartition(")")[2].split()
    require(len(fields) > 19 and fields[19].isdecimal())
    if recovery is not None:
        require(recovery["worker"] == {"pid": pid, "start_ticks": int(fields[19]),
                                       "unit": unit(target["device_id"])})
    groups = (process / "cgroup").read_text()
    cgroups = [line.split(":", 2)[2] for line in groups.splitlines() if line.count(":") >= 2]
    group = props.get("ControlGroup", "")
    require(group.startswith("/system.slice/") and group.endswith("/" + unit(target["device_id"])))
    require(group in cgroups)

    def waiting_lock():
        holders = []
        for line in (PROC / "locks").read_text().splitlines():
            parts = line.split()
            if len(parts) != 8 or parts[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
                continue  # In particular, -> records are waiters, not owners.
            major, minor, inode = parts[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == (
                    os.major(lock_info.st_dev), os.minor(lock_info.st_dev), lock_info.st_ino):
                require(parts[6:] == ["0", "EOF"])
                holders.append(int(parts[4]))
        require(holders == [pid])
        descriptors = list((process / "fd").iterdir())
        require(len(descriptors) <= 4096)
        require(any((entry.stat().st_dev, entry.stat().st_ino) == (lock_info.st_dev, lock_info.st_ino)
                    for entry in descriptors))

    waiting_lock()
    require(not os.path.lexists(request_path.with_suffix(".ack.json"))
            and not any(os.path.lexists(manager.context.state_root / name) for name in (
                "device-regenerate-v3.json", "device-regenerate-v2.json", "device-regenerate.json", "convergence-v1.json")))
    if phase == "reserve":
        require(not os.path.lexists(operation / "prepared.json"))
    else:
        require(read(operation / "prepared.json") == {"native_transaction": request["native_transaction"],
                "target_sha256": request["target_sha256"], "attempt": request["attempt"]})
    remaining(1)
    return {"proof": digest({"row": row, "owner": owner, "active": active, "request": request,
                              "receipt": receipt, "worker": [pid, fields[19], groups, argv[1].decode()],
                              "database": [db_info.st_dev, db_info.st_ino], "recovery": recovery}),
            "expected_values": expected_values}


def native_identity_observation(target, *, request_id, manager=None):
    """One bounded read-only observation, with no run/APK or lifecycle action."""
    from types import SimpleNamespace
    if (not isinstance(target, dict) or set(target) != {"device_id", "instance_id", "resource_tag", "container_id"}
            or type(target["device_id"]) is not int
            or not isinstance(request_id, str) or not re.fullmatch("[0-9a-f]{32}", request_id)
            or not isinstance(target["instance_id"], str) or not re.fullmatch("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", target["instance_id"])
            or not isinstance(target["resource_tag"], str) or not re.fullmatch("[0-9a-f]{12}", target["resource_tag"])
            or not isinstance(target["container_id"], str) or not re.fullmatch("[0-9a-f]{64}", target["container_id"])):
        raise ValueError("Invalid identity observation binding")
    device_number(target["device_id"])
    skipped = {"binding": dict(target), "request_id": request_id}
    fd = None
    try:
        if manager is None:
            os.chdir(UPSTREAM)
            adopt_native_environment()
            manager = trusted_device(target["device_id"], target)
        if manager.context.instance_id != target["instance_id"] or manager.context.resource_tag != target["resource_tag"]:
            raise ValueError("identity_observation_owner_changed")
        path = manager.context.state_root / "operation.lock"
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        def lock_owned():
            now = path.lstat()
            return (stat.S_ISREG(now.st_mode) and now.st_uid == 0 and stat.S_IMODE(now.st_mode) == 0o600 and now.st_nlink == 1
                    and (now.st_dev, now.st_ino) == (before.st_dev, before.st_ino))
        if not lock_owned():
            raise ValueError("identity_observation_lock_changed")
        deadline = time.monotonic() + 8
        def remaining(wanted):
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("identity_observation_deadline")
            return min(float(wanted or 5), left)
        reserve_guard = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                reserve_guard = _native_reserve_read_guard(target, manager, before, remaining)
            except Exception:
                return {**skipped, "status": "skipped", "reason": "native_busy"}
        if not lock_owned():
            raise ValueError("identity_observation_lock_changed")
        if any(os.path.lexists(manager.context.state_root / name) for name in (
                "device-regenerate-v3.json", "device-regenerate-v2.json", "device-regenerate.json", "convergence-v1.json")):
            return {**skipped, "status": "skipped", "reason": "native_pending"}
        def owned(timeout=5):
            return manager._owned_container_record(timeout=remaining(timeout))
        def docker(args, timeout=5):
            return manager.docker_exec(args, timeout=remaining(timeout))
        def daemon(timeout=5):
            client = manager.daemon_client(timeout=remaining(timeout))
            return SimpleNamespace(location_status=lambda timeout=5: client.location_status(timeout=remaining(timeout)))
        reader = SimpleNamespace(context=manager.context, cfg=manager.cfg,
                                 _owned_container_record=owned, docker_exec=docker, daemon_client=daemon)
        from identity_reservations import observe, signed, process_binding
        first, _ = owned()
        if not first or first.get("Id") != target["container_id"] or first.get("State", {}).get("Running") is not True:
            raise ValueError("identity_observation_runtime_changed")
        runtime = process_binding(first)
        values, build = observe(reader)
        last, _ = owned()
        if not last or last.get("Id") != target["container_id"] or process_binding(last) != runtime or not lock_owned():
            raise ValueError("identity_observation_runtime_changed")
        if reserve_guard is not None:
            if (_native_reserve_read_guard(target, manager, before, remaining) != reserve_guard
                    or values != reserve_guard["expected_values"]):
                raise ValueError("identity_observation_reserve_changed")
        remaining(1)
        observation = signed({"schema": "dev.xenoid.native-identity-inventory-observation/v1",
                              "binding": dict(target), "request_id": request_id, "runtime": runtime,
                              "values": values, "build": build, "captured_at": time.time()}, "proof_sha256")
        return {**skipped, "status": "observed", "observation": observation}
    except Exception as error:
        return {**skipped, "status": "unavailable", "reason": type(error).__name__}
    finally:
        if fd is not None:
            os.close(fd)


def poll_items(value):
    modern = isinstance(value, dict)
    if modern:
        if set(value) != {"runs", "identity_targets"} or not isinstance(value["identity_targets"], list) or len(value["identity_targets"]) > 1:
            raise ValueError("Invalid identity poll request")
        items, targets = value["runs"], value["identity_targets"]
    else:
        items, targets = value, []
    if not isinstance(items, list) or len(items) > 150:
        raise ValueError("Invalid poll request")
    results = []
    for item in items:
        number = device_number(item["device_id"])
        run = item["run_id"]
        if not RUN_ID.fullmatch(run):
            raise ValueError("Invalid run ID")
        results.append(snapshot(STATE / str(number), number, run, integer(item.get("after", 0), 0, 2**63-1),
                                limit=max(1, min(32, 110 // max(1, len(items))))))
    answer = {"items": results}
    if modern:
        answer["identity_observations"] = []
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"binding", "request_id"}:
                raise ValueError("Invalid identity target")
            answer["identity_observations"].append(native_identity_observation(target["binding"], request_id=target["request_id"]))
    return answer


def manage(action, folder, number, run):
    if action == "start":
        config = validate(json.load(sys.stdin))
        run = config["run_id"]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (folder / "operation.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        prepare(folder)
        if action == "start":
            with db(folder) as conn:
                retired = conn.execute("SELECT 1 FROM retired WHERE id=?", (run,)).fetchone()
            if retired:
                return snapshot(folder, number, run)
            install_unit()
            # Admission needs the old run's state, not its optional identity
            # transport proof, which can legitimately wait on native EX.
            current = snapshot(folder, number, include_identity=False)
            with db(folder) as conn:
                old = conn.execute("SELECT config,current,state FROM runs WHERE id=?", (run,)).fetchone()
            if old is not None:
                if json.loads(old["config"]) != config:
                    raise ValueError("Run id already exists for different settings")
                if old["current"] and old["state"] not in TERMINAL:
                    ctl("enable", "--now", unit(number))
                return snapshot(folder, number, run)
            trusted_device(number, config)
            if current["state"] != "missing":
                ctl("stop", unit(number))
            with db(folder) as conn:
                if current["state"] not in TERMINAL | {"missing"}:
                    conn.execute("UPDATE runs SET state='stopped',updated=? WHERE current=1", (now(),))
                    record(conn, current["run_id"], current["iteration"], "info", "Stopped for a new assignment")
                conn.execute("UPDATE runs SET current=0 WHERE current=1")
                timestamp = now()
                conn.execute("""INSERT INTO runs(id,created,config,state,current,updated,resume_at)
                                VALUES(?,?,?,'queued',1,?,?)""",
                             (run, timestamp, json.dumps(config), timestamp, time.time() + config["initial_delay"]))
                record(conn, run, 0, "info", f"Queued {config['language']} version {config['version']}")
                conn.execute("INSERT OR IGNORE INTO retired SELECT id,state FROM runs ORDER BY rowid DESC LIMIT -1 OFFSET 10")
                conn.execute("DELETE FROM runs WHERE id IN (SELECT id FROM retired)")
                conn.execute("DELETE FROM events WHERE run_id NOT IN (SELECT id FROM runs)")
            try:
                ctl("enable", "--now", unit(number))
            except Exception as error:
                update(folder, run, "failed", 0, str(error), "error")
                raise
        else:
            current = snapshot(folder, number)
            if current["state"] == "missing" or run and current["run_id"] != run:
                if run and snapshot(folder, number, run)["state"] == "missing":
                    # A delayed start with this request ID can no longer revive a cancelled device script.
                    with db(folder) as conn:
                        conn.execute("INSERT OR IGNORE INTO retired(id,state) VALUES(?,'stopped')", (run,))
                return snapshot(folder, number, run)
            if current["state"] not in TERMINAL:
                # Mark the desired state before stopping. Reboot/retry cannot resurrect an acknowledged stop.
                update(folder, current["run_id"], "stopped", current["iteration"], "Stop requested")
            ctl("disable", "--now", unit(number))
        return snapshot(folder, number, run)


def check_url(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("Expected a browser URL without NUL characters")
    return value


def ws_page(port, path):
    if not re.fullmatch(r"/devtools/page/[A-Fa-f0-9]+", path):
        raise ValueError("Invalid Chrome page target")
    with socket.create_connection(("127.0.0.1", port), timeout=3) as conn, conn.makefile("rb") as src:
        key = base64.b64encode(os.urandom(16)).decode()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        if b" 101 " not in src.readline(1024):
            raise ValueError("Chrome refused the page inspection")
        headers = {}
        for _ in range(30):
            line = src.readline(4096)
            if line == b"\r\n":
                break
            field, value = line.decode("ascii").split(":", 1)
            headers[field.lower()] = value.strip()
        if headers.get("sec-websocket-accept") != expected:
            raise ValueError("Invalid Chrome WebSocket response")
        expr = "({url:location.href,visible:document.visibilityState,ready:document.readyState,text:(document.querySelector('pre')?.innerText || document.body?.innerText || '').slice(0,32768)})"
        payload = json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expr, "returnByValue": True}}).encode()
        mask = os.urandom(4)
        conn.sendall(b"\x81\xfe" + struct.pack("!H", len(payload)) + mask
                     + bytes(ch ^ mask[i % 4] for i, ch in enumerate(payload)))
        for _ in range(15):
            h = src.read(2)
            if len(h) != 2 or h[0] != 0x81 or h[1] & 128:
                raise ValueError("Invalid page inspection reply")
            size = h[1] & 127
            if size in {126, 127}:
                size = int.from_bytes(src.read(2 if size == 126 else 8), "big")
            if size > 131072:
                raise ValueError("Page inspection exceeded output limit")
            msg = json.loads(src.read(size))
            if msg.get("id") == 1:
                return msg["result"]["result"]["value"]
        raise ValueError("No Chrome page response")


def browser_json(port, url):
    # No redirect handler, proxy environment, or socket address supplied by web content is used here.
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
        conn.request("GET", "/json/list")
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("Chrome has no tab list yet")
        tabs = json.loads(response.read(1024 * 1024))
    expected = url.rstrip("/")
    for tab in tabs:
        if tab.get("type") != "page" or tab.get("url", "").rstrip("/") != expected:
            continue
        path = urlsplit(tab.get("webSocketDebuggerUrl", "")).path
        page = ws_page(port, path)
        if page["url"].rstrip("/") == expected and page.get("visible") == "visible" and page.get("ready") == "complete":
            return json.loads(page["text"])
    raise ValueError("Chrome has not rendered JSON for the requested URL")


def safe_adb(args):
    """Validate transport shape; command choice belongs to the administrator."""
    if not isinstance(args, list) or not args or not all(isinstance(x, str) and "\0" not in x for x in args):
        raise ValueError("Expected a non-empty ADB argument list without NUL characters")
    return args


class TransportUnavailable(RuntimeError):
    """Temporary device/transport failure; callers may wait before retrying."""


class NativeObservationRejected(TransportUnavailable):
    """A consumed navigation observation was rejected before pointer injection."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class RuntimeIdentityError(RuntimeError):
    """The assigned container identity changed; never retry against a new target."""


class RegenerationPending(RuntimeError):
    """Only a sanitized native code may cross the unprivileged SDK boundary."""

    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code) else "device_regeneration_failed"
        super().__init__("Official regeneration pending: " + self.code)


def current_display_dump(dump, display_id=0):
    """Select one current display; archived/ambiguous output is not actionable."""
    if type(display_id) is not int or display_id < 0:
        return None
    if (re.search(r"(?im)^\s*WINDOW MANAGER LAST ANR\b", dump)
            or len(re.findall(r"(?m)^WINDOW MANAGER DISPLAY CONTENTS\b", dump)) > 1):
        return None
    displays = list(re.finditer(r"(?m)^[ \t]*Display: mDisplayId=(-?\d+)\b[^\n]*$", dump))
    if displays:
        selected = [(match.end(), displays[index + 1].start() if index + 1 < len(displays) else len(dump))
                    for index, match in enumerate(displays) if int(match[1]) == display_id]
        if len(selected) != 1:
            return None
        start, end = selected[0]
        dump = dump[start:end]
    elif display_id != 0:
        return None
    return dump


def foreground_window(dump, display_id=0):
    """Parse current display output; archived ANR windows are never actionable."""
    dump = current_display_dump(dump, display_id)
    if dump is None:
        return None
    focused = re.findall(r"(?m)^[ \t]*mCurrentFocus=(\S.*)$", dump)
    if len(focused) != 1:
        return None
    match = ANDROID_FOCUS.fullmatch(focused[0].strip())
    if match and ANDROID_PACKAGE.fullmatch(match[3]):
        return match.groups()
    # Framework crash/ANR windows have a token and title but no component.
    generic = re.fullmatch(r"Window\{([0-9a-fA-F]+) u([0-9]+) (.+)\}", focused[0].strip())
    if generic:
        token, user, title = generic.groups()
        return token, user, "", title
    return None


def current_display_geometry(dump, display_id=0):
    dump = current_display_dump(dump, display_id)
    if dump is None:
        return None
    frames = re.findall(r"(?m)^[ \t]*DisplayFrames w=(\d+) h=(\d+) r=([0-3])\s*$", dump)
    if len(frames) != 1:
        return None
    width, height, rotation = map(int, frames[0])
    return (width, height, rotation) if width > 0 and height > 0 else None


def native_navigation_back(snapshot):
    """Return one exact, observed SystemUI Back; never infer screen coordinates."""
    if snapshot.get("stable") is not True or type(snapshot.get("display_id")) is not int:
        return None
    found = []
    for control in snapshot.get("navigation_controls", ()):
        box, viewport = control.get("bounds"), control.get("window_bounds")
        if (control.get("resource_id") != "com.android.systemui:id/back"
                or control.get("package") != "com.android.systemui"
                or control.get("enabled") is not True or control.get("clickable") is not True
                or control.get("window_type") != "TYPE_SYSTEM"
                or control.get("window_title") != "Navigation bar"
                or type(control.get("window_id")) is not int or type(control.get("window_layer")) is not int
                or control.get("display_id") != snapshot["display_id"]
                or not isinstance(box, (list, tuple)) or len(box) != 4
                or not isinstance(viewport, (list, tuple)) or len(viewport) != 4
                or any(type(value) is not int for value in (*box, *viewport))
                or not (viewport[0] <= box[0] < box[2] <= viewport[2]
                        and viewport[1] <= box[1] < box[3] <= viewport[3])):
            continue
        found.append(control)
    return found[0] if len(found) == 1 else None


def native_navigation_node_matches(proof, offer):
    """Match Back semantics/geometry and IDs within the fresh connection.

    Android reassigns accessibility window IDs when its interaction connection
    is registered again. They cannot identify a window across DumpCommand and
    the fresh UiAutomation connection. Current WM/dispatcher routing still
    proves which real window owns the point immediately before official input.
    """
    expected = {"schema": "xenoid.nav-node-proof/v1", "display_id": offer["display_id"],
                "window_layer": offer["window_layer"],
                "window_id_scope": "current_ui_automation_connection",
                "window_type": "TYPE_SYSTEM", "window_title": "Navigation bar",
                "window_bounds": offer["window_bounds"], "bounds": offer["bounds"],
                "package": "com.android.systemui", "resource_id": "com.android.systemui:id/back",
                "visible": True, "enabled": True, "clickable": True,
                "cache_bypass": "new_connection_setServiceInfo_root_target_refresh"}
    return (isinstance(proof, dict) and all(type(proof.get(k)) is type(v) and proof[k] == v
            for k, v in expected.items()) and type(proof.get("window_id")) is int
            and proof["window_id"] >= 0 and type(proof.get("node_window_id")) is int
            and proof["node_window_id"] == proof["window_id"]
            and type(proof.get("nodes_visited")) is int
            and 1 <= proof["nodes_visited"] <= 128)


def native_navigation_routing(display_dump, window_dump, input_dump, offer):
    """Fail closed unless current WM/dispatcher route this point to SystemUI.

    This proves current touch ownership, not Back semantics. The independent
    cache-bypassing navigation-node proof supplies those semantics. Text formats
    beyond the qualified Android13 rectangle format are deliberately unproven.
    """
    def need(value):
        if not value:
            raise ValueError("navigation_routing_changed_or_unproven")

    rect_pattern = r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"
    def rect(value):
        found = re.fullmatch(rect_pattern, value)
        need(found is not None)
        return tuple(map(int, found.groups()))

    def one(pattern, value, flags=0):
        found = list(re.finditer(pattern, value, flags))
        need(len(found) == 1)
        return found[0]

    need(all(isinstance(value, str) and len(value) <= 1024 * 1024
             for value in (display_dump, window_dump, input_dump)))
    display = current_display_dump(display_dump, offer["display_id"])
    need(display is not None)
    nav = one(r"(?m)^\s*mNavigationBar=Window\{([0-9a-fA-F]+) u(\d+) NavigationBar\d*\}\s*$", display)
    token = nav[1]
    windows = list(re.finditer(r"(?m)^  Window #\d+ Window\{([0-9a-fA-F]+) u(\d+) (NavigationBar\d*)\}:\n", window_dump))
    blocks = []
    for entry in windows:
        end = re.search(r"(?m)^  Window #\d+ ", window_dump[entry.end():])
        block = window_dump[entry.end():entry.end() + end.start()] if end else window_dump[entry.end():]
        if re.search(r"\bmDisplayId=" + str(offer["display_id"]) + r"\b", block):
            blocks.append((entry, block))
    need(len(blocks) == 1)
    entry, block = blocks[0]
    need(entry[1] == token and entry[2] == nav[2])
    owner = one(r"\bmOwnerUid=(\d+)\b[^\n]*\bpackage=com\.android\.systemui\b", block)
    session = one(r"\bmSession=Session\{\S+ (\d+):[^}\n]+\}", block)
    need("ty=NAVIGATION_BAR" in block and "mViewVisibility=0x0 mHaveFrame=true" in block
         and "mHasSurface=true isReadyForDisplay()=true" in block
         and re.search(r"\bSurface: shown=true\b[^\n]*\balpha=1\.0(?:\s|$)", block)
         and "mDrawState=HAS_DRAWN" in block and "mLastHidden=false" in block
         and re.search(r"\bisOnScreen=true\b", block) and re.search(r"(?m)^\s*isVisible=true\s*$", block))
    frame = one(r"(?m)^\s*Frames:[^\n]*\bframe=(" + rect_pattern + r")", block)
    need(rect(frame[1]) == tuple(offer["window_bounds"]))
    dispatcher = one(r"(?s)(?:^|\n)Input Dispatcher State:\n(.*?)(?:\n[A-Za-z][^\n]*:\n|\Z)", input_dump)[1]
    need("DispatchEnabled: true" in dispatcher and "DispatchFrozen: false" in dispatcher
         and "InputFilterEnabled: false" in dispatcher and "Pointer Capture Requested: false" in dispatcher)
    rows = list(re.finditer(r"(?m)^      (\d+): name='([^'\n]+)', id=(\d+), displayId=(\d+), inputConfig=([^\n]+)$", dispatcher))
    need(bool(rows) and len(rows) <= 256)
    # Reject silently unparsed window records, including malformed visibility.
    need(len(rows) == len(re.findall(r"(?m)^      \d+: name=", dispatcher)))
    display_rows = [row for row in rows if int(row[4]) == offer["display_id"]]
    need([int(row[1]) for row in display_rows] == list(range(len(display_rows))))
    parsed = []
    known = {"NOT_FOCUSABLE", "TRUSTED_OVERLAY", "WATCH_OUTSIDE_TOUCH", "SLIPPERY", "NO_INPUT_CHANNEL",
             "NOT_VISIBLE", "NOT_TOUCHABLE", "PREVENT_SPLITTING", "IS_WALLPAPER", "SPY", "DROP_INPUT",
             "DROP_INPUT_IF_OBSCURED", "DUPLICATE_TOUCH_TO_WALLPAPER", "DISABLE_USER_ACTIVITY",
             "PAUSE_DISPATCHING", "INTERCEPTS_STYLUS", "REPLACE_TOUCHABLE_REGION_WITH_CROP",
             "NOT_TOUCH_MODAL", "0x0"}
    for row in rows:
        if int(row[4]) != offer["display_id"]:
            continue
        fields = one(r"^(.+?), alpha=([0-9.]+), frame=(" + rect_pattern + r"), globalScale=([0-9.]+), "
                     r"applicationInfo\.name=.*?, applicationInfo\.token=.*?, touchableRegion=(.+?), "
                     r"ownerPid=(\d+), ownerUid=(\d+), dispatchingTimeout=\d+ms, hasToken=(true|false), "
                     r"touchOcclusionMode=([A-Z_]+)$", row[5])
        config = set(fields[1].split(" | "))
        need(config <= known)
        region = None if fields[9] == "<empty>" else rect(fields[9])
        parsed.append({"name": row[2], "config": config, "alpha": fields[2], "frame": rect(fields[3]),
                       "scale": fields[8], "region": region, "pid": int(fields[10]), "uid": int(fields[11]),
                       "has_token": fields[12] == "true"})
    wanted = token + " " + entry[3]
    selected = [i for i, row in enumerate(parsed) if row["name"] == wanted]
    need(len(selected) == 1)
    index = selected[0]
    row = parsed[index]
    need(row["config"] <= {"NOT_FOCUSABLE", "TRUSTED_OVERLAY", "WATCH_OUTSIDE_TOUCH", "SLIPPERY"}
         and row["alpha"] in {"1.0", "1.00"} and row["scale"] == "1.000000" and row["has_token"]
         and row["frame"] == tuple(offer["window_bounds"]) and row["region"] == row["frame"]
         and row["pid"] == int(session[1]) and row["uid"] == int(owner[1]))
    x = (offer["bounds"][0] + offer["bounds"][2]) // 2
    y = (offer["bounds"][1] + offer["bounds"][3]) // 2
    def contains(box):
        return box is not None and box[0] <= x < box[2] and box[1] <= y < box[3]
    need(contains(row["region"]))
    for above in parsed[:index]:
        if above["config"] & {"NOT_VISIBLE", "NOT_TOUCHABLE", "NO_INPUT_CHANNEL"}:
            continue
        need(not contains(above["region"]))
    return {"systemui_pid": row["pid"], "systemui_uid": row["uid"], "navigation_token": token}


def native_touch_point(dump, profile, x, y):
    """Invert Android's live dispatcher, reader orientation and calibration.

    Android 13 TouchInputMapper::rotateAndScale uses display size / inclusive
    raw-axis size. Its debug scale fields are rounded, so recompute that ratio.
    The dispatcher's display transform includes the current rotation and crop.
    """
    def missing(detail):
        raise TransportUnavailable("Native touch mapping unavailable: " + detail)

    def match(pattern, text, detail):
        found = re.search(pattern, text, re.M)
        if found is None:
            missing(detail)
        return found

    def invert(a, b, tx, c, d, ty, px, py):
        determinant = a * d - b * c
        if not math.isfinite(determinant) or abs(determinant) < 1e-12:
            missing("singular coordinate transform")
        px, py = px - tx, py - ty
        return (d * px - b * py) / determinant, (a * py - c * px) / determinant

    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    reader = match(r"(?s)Input Reader State[^\n]*:\n(.*?)(?:\nUnwantedInteractionBlocker:|\nInput Dispatcher State:)",
                   dump, "InputReader state is missing")[1]
    devices = list(re.finditer(r"^  Device (-?\d+): ([^\n]+)\n(.*?)(?=^  Device |^  Configuration:|\Z)",
                               reader, re.M | re.S))
    settings = profile.get("input", {}) if isinstance(profile, dict) else {}
    if not isinstance(settings, dict) or not isinstance(settings.get("name"), str):
        missing("the native touchscreen profile is missing")
    candidates = []
    for device in devices:
        body = device[3]
        if device[2].strip() != settings["name"] or "Touch Input Mapper (mode - DIRECT):" not in body:
            continue
        axes = re.search(r"Raw Touch Axes:\n(.*?)^      Calibration:", body, re.M | re.S)
        if not axes:
            continue
        raw = []
        for axis in ("X", "Y"):
            found = re.search(r"^        " + axis + r": min=(-?\d+), max=(-?\d+),", axes[1], re.M)
            configured = settings.get(axis.lower(), {})
            if not found or not isinstance(configured, dict):
                break
            values = tuple(map(int, found.groups()))
            if values != (configured.get("minimum"), configured.get("maximum")) or values[1] <= values[0]:
                break
            raw.append(values)
        if len(raw) == 2:
            candidates.append((device[1], body, raw))
    if len(candidates) != 1:
        missing("expected one direct touchscreen matching the current native profile")
    device_id, body, ((xmin, xmax), (ymin, ymax)) = candidates[0]
    viewport = match(r"^      Viewport [^\n]*\bdisplayId=(-?\d+),", body, "touchscreen viewport is missing")
    display_id = viewport[1]
    width = int(match(r"^      DisplayWidth: (\d+)px", body, "reader display width is missing")[1])
    height = int(match(r"^      DisplayHeight: (\d+)px", body, "reader display height is missing")[1])
    orientation = int(match(r"^      InputDeviceOrientation: (\d+)", body, "reader orientation is missing")[1])
    if width <= 0 or height <= 0 or orientation not in (0, 1, 2, 3):
        missing("invalid reader display geometry")
    dispatcher = match(r"(?s)Input Dispatcher State:\n(.*)", dump, "InputDispatcher state is missing")[1]
    # Isolate the display header before per-window transforms, which map into
    # individual window coordinates rather than the screen used by UI bounds.
    section = match(r"(?s)^  Display: " + re.escape(display_id) + r"\n(.*?)(?:\n    Windows:|\Z)",
                    dispatcher, "dispatcher display transform is missing")[1]
    logical = match(r"^    logicalSize=(\d+)x(\d+)", section, "logical display size is missing")
    logical_width, logical_height = map(int, logical.groups())
    # A readiness observation validates the same mapping at its centre without
    # injecting an event. official_tap still requires explicit integer inputs.
    if x is None and y is None:
        x, y = (logical_width - 1) // 2, (logical_height - 1) // 2
    if not 0 <= x < logical_width or not 0 <= y < logical_height:
        raise ValueError("Touch coordinates are outside the current logical display")
    transforms = list(re.finditer(r"^        transform[^\n]*$", section, re.M))
    if len(transforms) != 1:
        missing("dispatcher transform is missing or ambiguous")
    transform = transforms[0]
    header, tail = transform[0], section[transform.end():]
    if header == "        transform (ROT_0) (IDENTITY)" and not tail.strip():
        # Android omits numeric rows only for the identity transform. This is
        # the same coordinate mapping, not an alternative input transport.
        a, b, tx, c, d, ty, u, v, w = (1, 0, 0, 0, 1, 0, 0, 0, 1)
    else:
        if re.fullmatch(r"        transform \(ROT_(?:0|90|180|270)\) \((?:IDENTITY|(?:SCALE|TRANSLATE|ROTATE)(?: (?:SCALE|TRANSLATE|ROTATE))* ?)\)", header) is None:
            missing("dispatcher transform format is unsupported")
        matrix = re.fullmatch(r"\s*\n\s*(" + number + r")\s+(" + number + r")\s+(" + number +
                              r")\n\s*(" + number + r")\s+(" + number + r")\s+(" + number + r")\n\s*(" + number +
                              r")\s+(" + number + r")\s+(" + number + r")\s*", tail)
        if matrix is None:
            missing("dispatcher matrix is missing or ambiguous")
        a, b, tx, c, d, ty, u, v, w = map(float, matrix.groups())
    if not all(math.isfinite(value) for value in (a, b, tx, c, d, ty, u, v, w)) or (u, v, w) != (0, 0, 1):
        missing("unsupported dispatcher projection")
    px, py = invert(a, b, tx, c, d, ty, x, y)
    sx, sy = width / (xmax - xmin + 1), height / (ymax - ymin + 1)
    if orientation == 0:
        ax, ay = xmin + px / sx, ymin + py / sy
    elif orientation == 1:
        ax, ay = xmax - py / sx, ymin + px / sy
    elif orientation == 2:
        ax, ay = xmax - px / sx, ymax - py / sy
    else:
        ax, ay = xmin + py / sx, ymax - px / sy
    calibration = []
    for label in ("X scale", "X ymix", "X offset", "Y xmix", "Y scale", "Y offset"):
        calibration.append(float(match(r"^        " + label + r": (" + number + r")", body,
                                       "device affine calibration is missing")[1]))
    rx, ry = invert(*calibration, ax, ay)
    if not math.isfinite(rx) or not math.isfinite(ry):
        missing("non-finite raw coordinates")
    physical = {}
    for label in ("PhysicalLeft", "PhysicalTop", "PhysicalWidth", "PhysicalHeight"):
        physical[label] = int(match(r"^      " + label + r": (-?\d+)(?:px)?$", body,
                                    "reader physical frame is missing")[1])
    # A letterboxed viewport can start between two raw integer coordinates.
    # Quantize only to the surrounding grid points and prove the selected one
    # remains in Android's crop and projects within one logical pixel. Merely
    # rounding can choose an event Android would discard at the cropped edge.
    candidates = []
    for qx in sorted({math.floor(rx), math.ceil(rx)}):
        for qy in sorted({math.floor(ry), math.ceil(ry)}):
            if not xmin <= qx <= xmax or not ymin <= qy <= ymax:
                continue
            # Android checks the uncalibrated raw point against this crop.
            if not (physical["PhysicalLeft"] <= (qx - xmin) * sx <= physical["PhysicalLeft"] + physical["PhysicalWidth"]
                    and physical["PhysicalTop"] <= (qy - ymin) * sy <= physical["PhysicalTop"] + physical["PhysicalHeight"]):
                continue
            ca, cb, ctx, cc, cd, cty = calibration
            fx, fy = ca * qx + cb * qy + ctx, cc * qx + cd * qy + cty
            if orientation == 0:
                dx, dy = (fx - xmin) * sx, (fy - ymin) * sy
            elif orientation == 1:
                dx, dy = (fy - ymin) * sy, (xmax - fx) * sx
            elif orientation == 2:
                dx, dy = (xmax - fx) * sx, (ymax - fy) * sy
            else:
                dx, dy = (ymax - fy) * sy, (fx - xmin) * sx
            lx, ly = a * dx + b * dy + tx, c * dx + d * dy + ty
            ex, ey = abs(lx - x), abs(ly - y)
            if not all(math.isfinite(value) for value in (lx, ly, ex, ey)) or ex > 1 or ey > 1:
                continue
            candidates.append((ex * ex + ey * ey, (qx - rx) ** 2 + (qy - ry) ** 2, qx, qy))
    if not candidates:
        missing("requested screen point has no representable native pixel within the active physical frame")
    _, _, rx, ry = min(candidates)
    return rx, ry, {"device_id": int(device_id), "display_id": int(display_id),
                    "logical_size": [logical_width, logical_height], "orientation": orientation}


class Broker:
    def __init__(self, manager, log, verify=None, *, config=None, app_cache=None):
        self.manager = manager
        self.verify = verify
        options = config if config is not None else {}
        self.config = options
        self.configure_callback = None
        self.window_offer = None
        self._native_navigation_lock = threading.Lock()
        self._native_navigation_generation = 0
        self._native_navigation_offer = None
        self._native_display_id = 0
        self.container_id = options.get("expected_container_id")
        self.forward_lock = threading.Lock()
        self.log = log
        self.url = None
        self.stopping = threading.Event()
        self.port = None
        self.read_only_window = options.get("read_only_window") is True
        self.shared_app = options.get("shared_app") is True
        self.assigned_apk_size = options.get("assigned_apk_size")
        self.resolved_app = None
        self.app_test = options.get("app_test")
        self.assigned_apk = options.get("assigned_apk")
        self.app_cache = app_cache if app_cache is not None else {}
        self.regeneration_receipts = None
        self._ui_reader = None

    @staticmethod
    def regeneration_cli():
        name = "xenoid._panel_regeneration_" + secrets.token_hex(8)
        spec = importlib.util.spec_from_file_location(name, UPSTREAM / "src/xenoid/cli.py")
        if spec is None or spec.loader is None:
            raise RegenerationPending("native_regeneration_unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module

    def regeneration_binding(self, manager, *, running=True):
        context = manager.context
        if (context.resource_tag != self.config["resource_tag"] or
                self.config.get("instance_id") is not None and context.instance_id != self.config["instance_id"]):
            raise RuntimeIdentityError("The registered device changed during regeneration")
        owned, _reason = manager._owned_container_record(timeout=8)
        if owned is None or not re.fullmatch(r"[0-9a-f]{64}", str(owned.get("Id"))):
            raise RegenerationPending("native_runtime_unavailable")
        if self.container_id is not None and owned["Id"] != self.container_id:
            raise RuntimeIdentityError("The assigned Android runtime changed during regeneration")
        if running and (owned.get("State") or {}).get("Running") is not True:
            raise RegenerationPending("native_runtime_stopped")
        self.container_id = owned["Id"]
        return {"instance_id": context.instance_id, "resource_tag": context.resource_tag, "container_id": owned["Id"]}

    def invalidate_regeneration_caches(self):
        self.close_ui_reader(force=True)
        self.app_cache.clear()
        self.resolved_app = self.window_offer = self.url = None
        self._hierarchy_focus_observed = False
        self._hierarchy_focus = None
        self._hierarchy_geometry = None
        with self._native_navigation_lock:
            self._native_navigation_generation += 1
            self._native_navigation_offer = None

    def regeneration_data_fence(self, manager):
        owned, _reason = manager._owned_container_record(timeout=8)
        if owned is None or owned.get("Id") != self.container_id:
            raise RuntimeIdentityError("The regeneration preparation runtime changed")
        mounts = sorted(({key: row.get(key) for key in ("Type", "Name", "Source", "Destination", "RW")}
                         for row in owned.get("Mounts", [])), key=lambda row: str(row.get("Destination")))
        return hashlib.sha256(json.dumps({"image": owned.get("Image"), "mounts": mounts}, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def daemon_only_boot_compatible(current, desired):
        """Prove the native daemon live-update case, without accepting boot drift."""
        if not isinstance(current, dict) or not isinstance(desired, dict):
            return False
        for key in ("baseImageId", "bootInputSha256", "contextInputs", "googleInputs", "toolInputs", "builderInputs"):
            if key not in current or current[key] != desired.get(key):
                return False
        if current.get("inputSha256") == desired.get("inputSha256"):
            return False
        old_seed, new_seed = current.get("daemonSeedContract"), desired.get("daemonSeedContract")
        if not isinstance(old_seed, dict) or not isinstance(new_seed, dict) or set(old_seed) != set(new_seed):
            return False
        if any(old_seed[key] != new_seed[key] for key in old_seed if key != "apkSha256"):
            return False
        try:
            old = {row["target"]: row for row in current["artifactTuples"]}
            new = {row["target"]: row for row in desired["artifactTuples"]}
            if set(old) != set(new) or len(old) != len(current["artifactTuples"]) or len(new) != len(desired["artifactTuples"]):
                return False
            if any(old[name] != new[name] for name in old if name != "daemon"):
                return False
            before, after = old["daemon"], new["daemon"]
            if any(before.get(key) != after.get(key) for key in ("target", "toolSha256")):
                return False
            # A daemon source fix changes its real input digest. The native
            # boot digest excludes the replaceable daemon APK; every other
            # artifact and the complete security seed contract stay identical.
            if any(not isinstance(row.get("inputSha256"), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", row["inputSha256"])
                   for row in (before, after)):
                return False
            if len(before["outputs"]) != 1 or len(after["outputs"]) != 1:
                return False
            a, b = before["outputs"][0], after["outputs"][0]
            return (a["path"] == b["path"] and a["path"].endswith(".apk") and a["mode"] == b["mode"]
                    and a["sha256"] == old_seed["apkSha256"] and b["sha256"] == new_seed["apkSha256"]
                    and a["sha256"] != b["sha256"])
        except (KeyError, TypeError):
            return False

    def regeneration_daemon_compatibility(self, manager, plan, observation, binding):
        """Use native content records and installed APK proof for one safe UP."""
        runtime = observation.get("runtime") or {}
        if (plan.resolution != "complete" or plan.boot_seed_action != "none"
                or runtime.get("imageInputSha256") == plan.desired_image_input_sha256):
            return None
        selected = observation.get("selectedImageRecord") or {}
        desired = manager.runtime_image_input_record()
        if (selected.get("imageId") != binding["image_id"]
                or selected.get("inputSha256") != runtime.get("imageInputSha256")
                or desired.get("inputSha256") != plan.desired_image_input_sha256
                or desired.get("bootInputSha256") != plan.desired_image_boot_input_sha256
                or not self.daemon_only_boot_compatible(selected, desired)
                or manager._runtime_seed_incompatibility(selected, desired) is not None):
            raise RegenerationPending("regeneration_preparation_image_change_required")
        # Either the pinned stock daemon is still installed, or the same
        # official live update already finished before an interrupted reply.
        installed = manager._daemon_live_update_matches(desired)
        if installed.get("ok") is not True:
            installed = manager._daemon_live_update_matches(selected)
        if (installed.get("ok") is not True or installed.get("signingLineageMatches") is not True
                or installed.get("sharedUserIdMatches") is not True):
            raise RegenerationPending("regeneration_preparation_daemon_unverified")
        return {"image_id": binding["image_id"], "current_input": selected["inputSha256"],
                "desired_input": desired["inputSha256"], "boot_input": desired["bootInputSha256"],
                "storage": {key: runtime.get(key) for key in ("dataUuid", "rootfsUuid")}}

    @staticmethod
    def regeneration_preparation_plan(plan, retained, observation, binding):
        runtime = observation.get("runtime") or {}
        if (observation.get("instanceId") != binding["instance_id"] or runtime.get("containerId") != binding["container_id"]
                or any(runtime.get(key) is not True for key in ("ownershipValid", "integrationIdentityValid", "storageValid"))):
            raise RegenerationPending("regeneration_preparation_binding_changed")
        if any(not isinstance(runtime.get(key), str) or not re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", runtime[key])
               for key in ("dataUuid", "rootfsUuid")):
            raise RegenerationPending("regeneration_preparation_storage_unverified")
        if plan.resolution != "complete" or plan.boot_seed_action != "none":
            raise RegenerationPending("regeneration_preparation_requires_maintenance")
        selected = observation.get("selectedImageRecord") or {}
        if not binding.get("image_id") or selected.get("imageId") != binding["image_id"]:
            raise RegenerationPending("regeneration_preparation_image_change_required")
        compatible = binding.get("daemon_compatibility")
        compatible_retained = retained is None or bool(
            retained.get("regenerationTransactionId") is None and retained.get("bootSeedTarget") is None
            and retained.get("instanceId") == binding["instance_id"]
            and retained.get("oldContainerId") == binding["container_id"]
            and retained.get("newContainerId") in {None, binding["container_id"]}
            and retained.get("selectedImageInputSha256") == runtime.get("imageInputSha256")
            and retained.get("selectedImageBootInputSha256") == runtime.get("imageBootInputSha256")
            and not set(retained.get("completed", ())) & {"container_removed", "container_created", "storage_converged", "seed_runtime_started", "seed_initialized"})
        daemon_update = bool(isinstance(compatible, dict)
            and compatible.get("image_id") == binding.get("image_id")
            and compatible.get("current_input") == runtime.get("imageInputSha256") == selected.get("inputSha256")
            and compatible.get("desired_input") == plan.desired_image_input_sha256
            and compatible.get("boot_input") == plan.desired_image_boot_input_sha256 == runtime.get("imageBootInputSha256") == selected.get("bootInputSha256")
            and compatible.get("storage") == {key: runtime.get(key) for key in ("dataUuid", "rootfsUuid")}
            and compatible_retained and plan.runtime_action == "reuse" and plan.image_action == "reuse-selected"
            and all(getattr(plan, name, None) == "reuse" for name in ("identity_action", "location_action", "keybox_action", "camera_action", "google_action", "protection_action"))
            and getattr(plan, "daemon_action", None) in {"reuse", "install", "reconcile"}
            and getattr(plan, "proxy_action", None) in {"reuse", "reconcile"}
            and not getattr(plan, "deploy_components", None))
        for field, desired, journal_key in (("imageInputSha256", plan.desired_image_input_sha256, "selectedImageInputSha256"),
                                             ("imageBootInputSha256", plan.desired_image_boot_input_sha256, "selectedImageBootInputSha256")):
            selected_key = "inputSha256" if field == "imageInputSha256" else "bootInputSha256"
            if field == "imageInputSha256" and daemon_update:
                continue
            if (not isinstance(desired, str) or not re.fullmatch(r"[0-9a-f]{64}", desired)
                    or runtime.get(field) != desired or selected.get(selected_key) != desired
                    or retained is not None and retained.get(journal_key) != desired):
                raise RegenerationPending("regeneration_preparation_image_change_required")
        if retained is None:
            if plan.image_action != "reuse-selected" or plan.runtime_action not in {"reuse", "start", "restart"}:
                raise RegenerationPending("regeneration_preparation_requires_recreation")
        else:
            if (retained.get("regenerationTransactionId") is not None or retained.get("bootSeedTarget") is not None
                    or retained.get("instanceId") != binding["instance_id"]):
                raise RegenerationPending("regeneration_preparation_foreign_transaction")
            completed = set(retained.get("completed", []))
            if plan.image_action == "ensure-desired" and "image_ensured" not in completed:
                raise RegenerationPending("regeneration_preparation_image_change_required")
            if plan.runtime_action in {"create", "recreate"}:
                required = {"storage_converged", "container_created", "runtime_started"}
                if plan.runtime_action == "recreate":
                    required.add("container_removed")
                if not required <= completed or retained.get("newContainerId") != binding["container_id"]:
                    raise RegenerationPending("regeneration_preparation_requires_recreation")
            for key, observed in (("observedDataUuid", "dataUuid"), ("observedRootfsUuid", "rootfsUuid")):
                if retained.get(key) != runtime.get(observed):
                    raise RegenerationPending("regeneration_preparation_storage_changed")

    def prepare_regeneration(self, cli, manager, args, binding, receipt, store, operation_key, check_budget, deadline=None):
        preflight = cli._regeneration_runtime_preflight(manager)
        if preflight.get("ok") is True:
            return
        if preflight.get("error") != "device_regeneration_runtime_not_converged":
            raise RegenerationPending(preflight.get("error"))
        native_executor = cli.ConvergenceExecutor
        inspected = native_executor(manager)
        observation = inspected._observe(skip_build=True)
        retained = inspected.journal.load()
        raw_plan = retained["plan"] if retained is not None else (preflight.get("convergence") or {}).get("plan")
        plan = cli.ConvergencePlan.from_dict(raw_plan)
        owned, _reason = manager._owned_container_record(timeout=8)
        plan_binding = {**binding, "image_id": (owned or {}).get("Image")}
        plan_binding["daemon_compatibility"] = self.regeneration_daemon_compatibility(manager, plan, observation, plan_binding)
        self.regeneration_preparation_plan(plan, retained, observation, plan_binding)
        fence = self.regeneration_data_fence(manager)
        data = {key: (observation.get("runtime") or {}).get(key) for key in ("dataUuid", "rootfsUuid")}
        prior = receipt.get("preparation")
        if prior and (prior.get("data_fence") != fence or prior.get("storage") != data):
            raise RegenerationPending("regeneration_preparation_storage_changed")
        receipt["preparation"] = {"status": "pending", "data_fence": fence, "storage": data,
                                  "plan_digest": plan.plan_digest}
        store.save(operation_key, receipt)
        broker = self
        error_type = native_executor._execute_plan.__globals__["ConvergenceError"]

        class PreparedExecutor(native_executor):
            def _execute_plan(inner, candidate, **kwargs):
                try:
                    check_budget()
                    broker.regeneration_preparation_plan(candidate, kwargs.get("retained"), kwargs["initial_observation"], plan_binding)
                    if broker.regeneration_data_fence(manager) != fence:
                        raise RegenerationPending("regeneration_preparation_storage_changed")
                except RegenerationPending as error:
                    raise error_type(error.code, phase="inspecting") from None
                if deadline is not None:
                    outer = kwargs.get("deadline")
                    kwargs["deadline"] = deadline if outer is None else min(deadline, outer)
                return super()._execute_plan(candidate, **kwargs)

            def _phase_call(inner, phase, method_name, reporter, outer_deadline, **kwargs):
                # Guard the actual phase too: a native input replan must never
                # turn preparation into an implicit image/container replacement.
                if method_name in {"remove_owned_container_for_recreate", "converge_storage", "create_owned_container",
                                   "start_seed_runtime", "initialize_seed_runtime", "ensure_runtime_image"}:
                    raise error_type("regeneration_preparation_requires_maintenance", phase=phase)
                try:
                    check_budget()
                    broker.regeneration_binding(manager, running=False)
                    if broker.regeneration_data_fence(manager) != fence:
                        raise RegenerationPending("regeneration_preparation_storage_changed")
                except RegenerationPending as error:
                    raise error_type(error.code, phase=phase) from None
                if deadline is not None:
                    outer_deadline = deadline if outer_deadline is None else min(deadline, outer_deadline)
                return super()._phase_call(phase, method_name, reporter, outer_deadline, **kwargs)

        captured = []
        previous_executor, previous_print, previous_progress = cli.ConvergenceExecutor, cli.print_json, cli._write_up_progress
        cli.ConvergenceExecutor = PreparedExecutor
        cli.print_json = lambda value: captured.append(value)
        cli._write_up_progress = lambda _event: None
        args.skip_build = True
        try:
            code = cli.cmd_up(args)
            answer = captured[-1] if captured else {}
            if code or answer.get("ok") is not True:
                raise RegenerationPending(answer.get("error") or answer.get("errorCode") or "native_preparation_failed")
        finally:
            cli.ConvergenceExecutor, cli.print_json, cli._write_up_progress = previous_executor, previous_print, previous_progress
        after = native_executor(manager)._observe(skip_build=True)
        if (self.regeneration_binding(manager) != binding or self.regeneration_data_fence(manager) != fence
                or any((after.get("runtime") or {}).get(key) != value for key, value in data.items())):
            raise RegenerationPending("regeneration_preparation_binding_changed")
        strict = cli._regeneration_runtime_preflight(manager)
        if strict.get("ok") is not True:
            raise RegenerationPending(strict.get("error"))
        receipt["preparation"]["status"] = "committed"
        identity = cli.DeviceIdentityStore(manager.context).load()
        receipt["before_digest"] = cli.stable_identity_digest(identity["stable"])
        store.save(operation_key, receipt)

    @staticmethod
    def regeneration_post_cache(plan):
        if plan is None:
            return None
        if not isinstance(plan, dict) or set(plan) - {"packages", "user", "timeout"}:
            raise ValueError("Post-cache plan must contain packages, user and optional timeout")
        packages = plan.get("packages")
        if not isinstance(packages, list) or any(not isinstance(value, str) or not ANDROID_PACKAGE.fullmatch(value) for value in packages):
            raise ValueError("Post-cache packages must be Android package names")
        user, timeout = plan.get("user", "current"), plan.get("timeout", None)
        if user != "current" and (type(user) is not int or user < 0):
            raise ValueError("Post-cache user must be current or a nonnegative user id")
        if timeout is not None and (type(timeout) is not int or timeout < 0):
            raise ValueError("Post-cache timeout must be nonnegative; zero is unlimited")
        packages = list(dict.fromkeys(packages))
        return {"packages": packages, "user": user, "timeout": timeout} if packages else None

    def complete_regeneration_post_cache(self, manager, receipt, store, key, deadline):
        plan = receipt.get("post_cache")
        if plan is None:
            return None
        try:
            def budget():
                if self.stopping.is_set():
                    raise TransportUnavailable("Device script is stopping")
                if deadline is not None and time.monotonic() >= deadline:
                    raise TransportUnavailable("Regeneration post-cache exceeded its configured timeout")
            budget()
            user = receipt.get("post_cache_resolved_user")
            if user is None:
                user = plan["user"]
                if user == "current":
                    result = self.native_display_exec(manager, ["am", "get-current-user"], deadline=deadline, check=False)
                    text = result.stdout.decode(errors="replace").strip()
                    if result.returncode or not re.fullmatch(r"[0-9]+", text):
                        raise TransportUnavailable("Post-cache current user could not be verified")
                    user = int(text)
                receipt["post_cache_resolved_user"] = user
                store.save(key, receipt)  # Fix the user before the first cache mutation.
            if type(user) is not int or user < 0:
                raise TransportUnavailable("Recorded post-cache user is invalid")
            completed = receipt.setdefault("post_cache_completed", {})
            if not isinstance(completed, dict):
                raise TransportUnavailable("Recorded post-cache progress is invalid")
            for package in plan["packages"]:
                budget()
                if package not in completed:
                    cache_timeout = plan["timeout"]
                    if cache_timeout is None:
                        cache_timeout = receipt.get("post_cache_command_timeout", self.config.get("command_timeout", 0))
                    result = self.clear_cache(package, user=user, timeout=cache_timeout, _deadline=deadline)
                else:
                    result = completed[package]
                if (not isinstance(result, dict) or result.get("ok") is not True or result.get("cache_only") is not True
                        or result.get("data_cleared") is not False or result.get("uid_verified") is not True
                        or result.get("package") != package or result.get("user") != user):
                    raise TransportUnavailable("Post-cache operation did not verify its package/user")
                if package not in completed:
                    completed[package] = result
                    store.save(key, receipt)  # Confirm each package before advancing.
            budget()
            return {"ok": True, "cache_only": True, "data_cleared": False,
                    "packages": list(plan["packages"]), "user": plan["user"],
                    "requested_user": plan["user"], "resolved_user": user}
        except RuntimeIdentityError:
            raise
        except Exception as error:
            receipt["last_cache_error"] = type(error).__name__
            store.save(key, receipt)
            raise RegenerationPending("regeneration_post_cache_pending") from None

    def restore_recorded_google_baseline(self, manager, cli, store, binding, deadline):
        """Restore only the prior SDK-authorized post-reset native Google seed.

        A fresh official GEN requires the strict offline baseline. The previous
        explicit post-Google action may have enabled provider-managed check-in.
        No unrecorded device or changed core identity is repaired here.
        """
        candidate = None
        with db(store.folder) as connection:
            rows = connection.execute("SELECT run_id,iteration,operation_key,document FROM regeneration_receipts ORDER BY rowid DESC").fetchall()
        for row in rows:
            previous = json.loads(row["document"])
            reset = (previous.get("result") or {}).get("google_reset")
            if (previous.get("binding") == binding and previous.get("status") == "committed"
                    and previous.get("post_google") is not None and isinstance(reset, dict)
                    and reset.get("ok") is True and reset.get("registration_enabled") is True
                    and reset.get("checkin_requested") is True and previous.get("native_completion") is not None):
                candidate = row, previous
                break
        if candidate is None:
            return False
        prior_row, previous = candidate
        try:
            self.verify_regeneration_native_completion(manager, cli, previous, deadline)
        except RegenerationPending:
            return False  # A later core/boot change never authorizes old reseeding.
        state = previous["native_journal"]
        if self.stopping.is_set():
            raise RegenerationPending("script_stopping")
        timeout = 60 if deadline is None else max(.1, min(60, deadline-time.monotonic()))
        client = manager.daemon_client(timeout=timeout)
        current = client.google_identity_inspect(timeout=timeout)
        from qa_google_reset import registration_state_command
        switch = self.native_display_exec(manager, registration_state_command(
            previous["post_google_progress"]["resolved_user"]), deadline=deadline, check=False)
        if switch.returncode:
            raise RegenerationPending("previous_google_registration_state_unverified")
        enabled = switch.stdout.decode(errors="replace").strip()
        if enabled not in ("enabled", "disabled"):
            raise RegenerationPending("previous_google_registration_state_unverified")
        if (isinstance(current, dict) and current.get("ok") is True
                and current.get("offlineSeeded") is True and enabled == "disabled"):
            return False
        # Use the same official seed derivation and activation API as Native.
        # Plaintext identifiers remain in memory and are never persisted/logged.
        target = cli.GoogleBindingStore(manager.context, manager.lease).regeneration_gsf_android_id(state["transactionId"])
        expected_digest = hashlib.sha256(target.encode("ascii")).hexdigest()
        answer = client.google_identity_activate(target, timeout=timeout)
        if (not isinstance(answer, dict) or answer.get("ok") is not True
                or answer.get("offlineSeeded") is not True
                or answer.get("gsfAndroidIdSha256") != expected_digest):
            raise RegenerationPending("previous_google_baseline_restore_pending")
        switch = self.native_display_exec(manager, registration_state_command(
            previous["post_google_progress"]["resolved_user"]), deadline=deadline, check=False)
        if switch.returncode or switch.stdout.decode(errors="replace").strip() != "disabled":
            raise RegenerationPending("previous_google_baseline_restore_pending")
        previous["google_baseline_restore"] = {"ok": True, "restored_at": now(), "before_new_native_regeneration": True}
        # Update the prior receipt, retaining its original run/iteration key.
        with db(store.folder) as connection:
            changed = connection.execute("UPDATE regeneration_receipts SET document=? WHERE run_id=? AND iteration=? AND operation_key=? AND document=?",
                               (json.dumps(previous, sort_keys=True), prior_row["run_id"], prior_row["iteration"],
                                prior_row["operation_key"], prior_row["document"]))
            if changed.rowcount != 1:
                raise RegenerationPending("previous_google_receipt_changed")
        if self.log:
            self.log("action", "Prior recorded Google baseline restored through official Xenoid before the next regeneration")
        return True

    def retire_recorded_google_offline_marker(self, manager, receipt, store, key, deadline):
        """Hand this explicit Google reset from native offline to provider mode."""
        progress = receipt.get("post_google_progress")
        plan = receipt.get("post_google")
        state = receipt.get("native_journal")
        if (not isinstance(plan, dict) or "com.google.android.gms" not in plan["clear_packages"]
                or receipt.get("native_completion") is None or not isinstance(progress, dict)
                or set(progress.get("cleared_packages", {})) != set(plan["clear_packages"])):
            raise RegenerationPending("google_mode_handoff_unverified")
        cli = self.regeneration_cli()
        self.verify_regeneration_native_completion(manager, cli, receipt, deadline)
        target = cli.GoogleBindingStore(manager.context, manager.lease).regeneration_gsf_android_id(state["transactionId"])
        expected = hashlib.sha256(target.encode("ascii")).hexdigest()
        intent = progress.get("mode_handoff")
        script = r'''
set -eu
operation=$1
expected=$2
allow_missing=$3
profile=/data/local/tmp/xenoid-profile
marker=$profile/gsf_android_id
[ -d "$profile" ] && [ ! -L "$profile" ]
[ "$(readlink -f "$profile")" = "$profile" ]
[ "$(stat -c '%u:%g:%a' "$profile")" = '0:0:700' ]
if [ ! -e "$marker" ]; then
    [ ! -L "$marker" ]
    if [ "$operation" = inspect ]; then echo absent; exit 0; fi
    [ "$allow_missing" = 1 ] || exit 71
    sync -f "$profile"
    [ ! -e "$marker" ] && [ ! -L "$marker" ]
    echo absent
    exit 0
fi
[ "$operation" != verify_absent ] || exit 72
[ -f "$marker" ] && [ ! -L "$marker" ]
[ "$(readlink -f "$marker")" = "$marker" ]
[ "$(stat -c '%u:%g:%a:%h' "$marker")" = '0:0:600:1' ]
size=$(stat -c %s "$marker")
[ "$size" -ge 2 ] && [ "$size" -le 20 ]
value=$(cat "$marker")
case "$value" in ''|0*|*[!0-9]*) exit 73;; esac
[ "$size" -eq "$((${#value}+1))" ]
actual=$(printf '%s' "$value" | sha256sum)
[ "${actual%% *}" = "$expected" ] || exit 74
if [ "$operation" = inspect ]; then echo present; exit 0; fi
[ "$operation" = retire ] || exit 75
rm "$marker"
sync -f "$profile"
[ ! -e "$marker" ] && [ ! -L "$marker" ]
echo retired
'''
        def command(operation, allow_missing=False):
            answer = self.native_display_exec(manager, ["sh", "-c", script, "xenoid-google-mode-handoff",
                                                       operation, expected, "1" if allow_missing else "0"],
                                              deadline=deadline, check=False)
            output = answer.stdout.decode(errors="replace").strip()
            if answer.returncode or output not in ("present", "absent", "retired"):
                raise RegenerationPending("google_mode_handoff_marker_invalid")
            return output
        if intent is None:
            if progress.get("registration_started"):
                raise RegenerationPending("google_mode_handoff_order_invalid")
            if command("inspect") != "present":
                raise RegenerationPending("google_mode_handoff_marker_missing_before_intent")
            intent = {"intent": True, "transaction_id": state["transactionId"],
                      "expected_marker_sha256": expected, "completed": False}
            progress["mode_handoff"] = intent
            store.save(key, receipt)  # Fence a lost response after unlink.
        if (not isinstance(intent, dict) or intent.get("intent") is not True
                or intent.get("transaction_id") != state["transactionId"]
                or intent.get("expected_marker_sha256") != expected
                or type(intent.get("completed")) is not bool):
            raise RegenerationPending("google_mode_handoff_intent_invalid")
        if intent["completed"]:
            command("verify_absent", True)
            return {"identity_mode": "provider-managed", "offline_marker_retired": True}
        command("retire", True)
        intent["completed"] = True
        progress["offline_marker_retired"] = True
        store.save(key, receipt)
        return {"identity_mode": "provider-managed", "offline_marker_retired": True}

    def complete_regeneration_post_google(self, manager, receipt, store, key, deadline):
        plan = receipt.get("post_google")
        if plan is None:
            return None
        from qa_google_reset import complete
        try:
            cache_plan = receipt.get("post_cache")
            if (plan["user"] == "current" and cache_plan is not None and cache_plan["user"] == "current"
                    and receipt.get("post_cache_resolved_user") is not None):
                progress = receipt.setdefault("post_google_progress", {})
                if "resolved_user" not in progress:
                    progress["resolved_user"] = receipt["post_cache_resolved_user"]
                    store.save(key, receipt)
            progress = receipt.get("post_google_progress") or {}
            if progress.get("registration_started"):
                cache = receipt.get("post_cache")
                if cache is not None and set(receipt.get("post_cache_completed", {})) != set(cache["packages"]):
                    raise RuntimeError("post_google_cache_progress_inconsistent")
            def execute(args):
                budget = deadline
                not_after = (receipt.get("post_google_progress") or {}).get("not_after")
                if not_after is not None:
                    remaining = not_after - time.time()
                    if remaining <= 0:
                        raise TimeoutError("post_google_timeout")
                    stage_deadline = time.monotonic() + remaining
                    budget = stage_deadline if budget is None else min(budget, stage_deadline)
                return self.native_display_exec(manager, args, deadline=budget, check=False)
            def retire_marker():
                handoff_deadline = deadline
                progress = receipt.get("post_google_progress") or {}
                if progress.get("completed") is not True and progress.get("not_after") is not None:
                    remaining = progress["not_after"] - time.time()
                    if remaining <= 0:
                        raise TimeoutError("post_google_timeout")
                    stage_deadline = time.monotonic() + remaining
                    handoff_deadline = stage_deadline if deadline is None else min(deadline, stage_deadline)
                return self.retire_recorded_google_offline_marker(manager, receipt, store, key, handoff_deadline)
            result = complete(plan, receipt, lambda: store.save(key, receipt), execute,
                              self.stopping, deadline=deadline, retire_offline_marker=retire_marker)
            if self.log:
                self.log("action", "Google data reset completed; microG Device registration enabled and check-in requested")
            return result
        except RuntimeIdentityError:
            raise
        except Exception as error:
            receipt["last_google_error"] = type(error).__name__
            store.save(key, receipt)
            raise RegenerationPending("regeneration_post_google_pending") from None

    def regeneration_native_completion(self, manager, cli, state, result, deadline):
        """Checkpoint the native proof before script-authorized Google data reset."""
        identity = cli.DeviceIdentityStore(manager.context).load()
        if (state.get("phase") != "committed" or identity is None
                or cli.stable_identity_digest(identity["stable"]) != state["target"]["stableDigest"]):
            raise RegenerationPending("regeneration_native_checkpoint_invalid")
        boot = self.native_display_exec(manager, ["cat", "/proc/sys/kernel/random/boot_id"], deadline=deadline)
        if boot.stdout.decode(errors="replace").strip() != state["target"]["bootId"]:
            raise RegenerationPending("regeneration_native_boot_changed")
        return {"schema": "dev.xenoid.native-post-google-checkpoint/v1", "result": dict(result),
                "data_fence": self.regeneration_data_fence(manager),
                "stable_digest": state["target"]["stableDigest"], "boot_id": state["target"]["bootId"]}

    def verify_regeneration_native_completion(self, manager, cli, receipt, deadline):
        checkpoint = receipt.get("native_completion")
        state = receipt.get("native_journal")
        if (not isinstance(checkpoint, dict) or checkpoint.get("schema") != "dev.xenoid.native-post-google-checkpoint/v1"
                or not isinstance(state, dict) or state.get("phase") != "committed"
                or checkpoint.get("stable_digest") != state["target"]["stableDigest"]
                or checkpoint.get("boot_id") != state["target"]["bootId"]
                or checkpoint.get("data_fence") != self.regeneration_data_fence(manager)):
            raise RegenerationPending("regeneration_native_checkpoint_invalid")
        self.regeneration_binding(manager)
        identity = cli.DeviceIdentityStore(manager.context).load()
        if identity is None or cli.stable_identity_digest(identity["stable"]) != checkpoint["stable_digest"]:
            raise RegenerationPending("regeneration_native_identity_changed")
        boot = self.native_display_exec(manager, ["cat", "/proc/sys/kernel/random/boot_id"], deadline=deadline)
        if boot.stdout.decode(errors="replace").strip() != checkpoint["boot_id"]:
            raise RegenerationPending("regeneration_native_boot_changed")
        result = checkpoint.get("result")
        if (not isinstance(result, dict) or result.get("ok") is not True or result.get("phase") != "committed"
                or result.get("runtimeOnly") is not True or result.get("containerRecreated") is not False):
            raise RegenerationPending("regeneration_native_checkpoint_invalid")
        return dict(result)

    def capture_regeneration_profile(self, manager, binding, deadline):
        from qa_profile_runtime import capture_plan
        return capture_plan(self, manager, binding, deadline)

    def native_location_baseline(self, manager):
        """Remember the active native country before the first requested move."""
        from xenoid.location import LocationStateStore
        state = LocationStateStore(manager.context.state_root, manager.cfg.network_dns_servers).load()
        active = state.get("active") if isinstance(state, dict) else None
        if (not isinstance(active, dict) or state.get("pending") is not None
                or active.get("country") != state.get("desiredCountry")):
            raise RegenerationPending("native_location_baseline_not_active")
        return active["country"]

    def complete_regeneration_profile(self, manager, cli, receipt, store, key, deadline):
        from qa_profile_runtime import complete
        try:
            result = complete(self, manager, cli, receipt, store, key, deadline)
            if result is not None and self.log:
                self.log("action", "Pinned JSON profile applied through official Xenoid; native IDs and input verified")
            return result
        except RuntimeIdentityError:
            raise
        except Exception as error:
            code = getattr(error, "code", "profile_apply_pending")
            receipt["last_profile_error"] = code
            store.save(key, receipt)
            raise RegenerationPending(code) from None

    def complete_regeneration_preferences(self, manager, receipt, store, key, deadline):
        from qa_ui_preferences import PreferencesPending, complete
        try:
            result = complete(self, manager, receipt, store, key, deadline)
        except PreferencesPending as error:
            receipt["last_preferences_error"] = error.code
            store.save(key, receipt)
            raise RegenerationPending(error.code) from None
        if result and self.log:
            self.log("action", "Android user preferences verified: " + json.dumps(
                result["target"], ensure_ascii=False, sort_keys=True))
        return result

    def regenerate(self, timeout=0, operation_key=None, post_cache=None, post_google=None,
                   post_preferences=None, *, _deadline=None):
        if type(timeout) is not int or timeout < 0:
            raise ValueError("Regeneration timeout must be a nonnegative integer; zero is unlimited")
        if not isinstance(operation_key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", operation_key):
            raise ValueError("Supply a nonempty regeneration operation key")
        post_cache = self.regeneration_post_cache(post_cache)
        from qa_google_reset import normalize_plan
        post_google = normalize_plan(post_google)
        from qa_ui_preferences import normalize_plan as normalize_preferences
        post_preferences = normalize_preferences(post_preferences)
        try:
            return self._regenerate(timeout, operation_key, post_cache, post_google,
                                    post_preferences, _deadline=_deadline)
        except (RuntimeIdentityError, RegenerationPending):
            raise
        except Exception as error:
            raise RegenerationPending(getattr(error, "code", None)) from None

    def managed_identity_gate(self, manager, store, key, receipt, deadline):
        if not self.config.get("identity_reservations"):
            return None
        from identity_reservations import NativeGate, digest
        context = {"scenario_sha256": hashlib.sha256(self.config["source"].encode()).hexdigest(),
                   "profile_plan_sha256": digest(receipt["profile_plan"]) if receipt.get("profile_plan") else None}
        prior = receipt.get("identity_reservation_context")
        if prior is not None and prior != context:
            raise RegenerationPending("identity_reservation_context_changed")
        if prior is None:
            receipt["identity_reservation_context"] = context
            store.save(key, receipt)
        return NativeGate(store.folder, self.config, manager, key, stopping=self.stopping,
                          deadline=deadline, context=context)

    def _regenerate(self, timeout, operation_key, post_cache=None, post_google=None,
                    post_preferences=None, *, _deadline=None):
        store = self.regeneration_receipts
        if store is None:
            raise RuntimeError("Regeneration receipts are unavailable outside a managed script iteration")
        if self.stopping.is_set():
            raise RegenerationPending("script_stopping")
        deadline = time.monotonic() + timeout if timeout else None
        if _deadline is not None:
            deadline = _deadline if deadline is None else min(deadline, _deadline)
        remaining = timeout or None
        if _deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RegenerationPending("regeneration_timeout")
        manager = self.verify() if self.verify else self.manager
        # The official in-process command shares this native ContextVar. Never
        # launch a child CLI while holding the native lock.
        with manager.instance_operation_lock(timeout_seconds=remaining):
            binding = self.regeneration_binding(manager, running=False)
            receipt = store.load(operation_key)
            if receipt is not None and receipt.get("binding") != binding:
                raise RuntimeIdentityError("The regeneration receipt belongs to a different runtime")
            if receipt is not None and receipt.get("post_cache") != post_cache:
                raise RegenerationPending("regeneration_post_cache_plan_changed")
            if receipt is not None and receipt.get("post_google") != post_google:
                raise RegenerationPending("regeneration_post_google_plan_changed")
            if receipt is not None and receipt.get("post_preferences") != post_preferences:
                raise RegenerationPending("regeneration_post_preferences_plan_changed")
            if (receipt is not None and post_preferences is not None
                    and receipt.get("post_preferences_plan") is None
                    and (receipt.get("native_journal") is not None
                         or receipt.get("native_completion") is not None)):
                raise RegenerationPending("finish_existing_regeneration_before_preferences")
            if receipt is not None and receipt.get("status") == "committed":
                gate = self.managed_identity_gate(manager, store, operation_key, receipt, deadline)
                if gate is not None:
                    gate.finalize(receipt["native_journal"])
                self.regeneration_binding(manager)
                self.invalidate_regeneration_caches()
                with self.display_raw_docker(manager, deadline):
                    execute = lambda values, **options: self.native_display_exec(manager, values, deadline=deadline, **options)
                    self.display_bootstrap(manager, execute, deadline)
                    self.ensure_native_interaction(manager, deadline, repair=True, foreground=True)
                return {**receipt["result"], "input_ready": True, "foreground_ready": True,
                        "touch_backend": "xenoid_native", "replayed": True}
            if receipt is not None and receipt.get("native_completion") is not None and (post_google is not None or receipt.get("profile_plan") is not None or receipt.get("post_preferences_plan") is not None):
                # Google data now belongs to the explicit post stage. Its retry
                # must not demand the pre-clear offline Google target or re-GEN.
                cli = self.regeneration_cli()
                self.cleanup()
                self.invalidate_regeneration_caches()
                with self.display_raw_docker(manager, deadline):
                    result = self.verify_regeneration_native_completion(manager, cli, receipt, deadline)
                    gate = self.managed_identity_gate(manager, store, operation_key, receipt, deadline)
                    if gate is not None:
                        gate.finalize(receipt["native_journal"])
                    result["profile_apply"] = self.complete_regeneration_profile(manager, cli, receipt, store, operation_key, deadline)
                    result["ui_preferences"] = self.complete_regeneration_preferences(manager, receipt, store, operation_key, deadline)
                    progress = receipt.get("post_google_progress") or {}
                    if progress.get("registration_started") and post_cache is not None:
                        if set(receipt.get("post_cache_completed", {})) != set(post_cache["packages"]):
                            raise RegenerationPending("post_google_cache_progress_inconsistent")
                    result["cache_cleanup"] = self.complete_regeneration_post_cache(manager, receipt, store, operation_key, deadline)
                    result["google_reset"] = self.complete_regeneration_post_google(manager, receipt, store, operation_key, deadline)
                    self.ensure_native_interaction(manager, deadline, repair=True, foreground=True)
                receipt.update(status="committed", result=result, completed_at=now())
                store.save(operation_key, receipt)
                if self.log:
                    self.log("action", "Official regeneration committed; container binding preserved and native profile ready")
                return result
            for run, iteration, key, pending in store.pending():
                if (run, iteration, key) != (store.run, store.iteration, operation_key) and pending.get("binding") == binding:
                    raise RegenerationPending("another_regeneration_is_pending")
            cli = self.regeneration_cli()
            native_journal = cli.RegenerationJournal
            live_state = native_journal(manager.context).load()
            identity = cli.DeviceIdentityStore(manager.context).load()
            if identity is None:
                raise RegenerationPending("device_identity_required")
            digest = cli.stable_identity_digest(identity["stable"])
            google_prepared = False
            if receipt is None and live_state is None:
                with self.display_raw_docker(manager, deadline):
                    google_prepared = self.restore_recorded_google_baseline(manager, cli, store, binding, deadline)
            if receipt is None:
                self.regeneration_binding(manager)
                receipt = {"status": "pending", "binding": binding, "timeout": timeout,
                           "before_digest": digest, "created_at": now(), "post_cache": post_cache, "post_google": post_google,
                           "post_preferences": post_preferences,
                           "post_cache_command_timeout": self.config.get("command_timeout", 0)}
                # Existing native transactions retain their original contract;
                # only a new GEN pins the current administrator profile policy.
                if live_state is None:
                    with self.display_raw_docker(manager, deadline):
                        profile_plan = self.capture_regeneration_profile(manager, binding, deadline)
                    if profile_plan is not None:
                        receipt["profile_plan"] = profile_plan
                elif post_preferences is not None:
                    raise RegenerationPending("finish_existing_regeneration_before_preferences")
                if google_prepared:
                    receipt["google_reset_preparation"] = {"completed": True, "before_native_journal": True}
                store.save(operation_key, receipt)
            saved_state = receipt.get("native_journal")
            if saved_state is not None:
                saved_state = native_journal(manager.context).validate(saved_state)
                if live_state is not None and live_state["transactionId"] != saved_state["transactionId"]:
                    raise RegenerationPending("regeneration_transaction_changed")
                if live_state is None and saved_state["phase"] != "committed":
                    raise RegenerationPending("regeneration_checkpoint_without_journal")
            elif (live_state is None and receipt["before_digest"] != digest
                  and (receipt.get("preparation") or {}).get("status") != "pending"):
                raise RegenerationPending("regeneration_outcome_ambiguous")

            def checkpoint(state):
                if state is not None:
                    if state.get("instanceId") != binding["instance_id"]:
                        raise RuntimeIdentityError("The native regeneration journal changed device")
                    previous = receipt.get("native_journal")
                    if previous is not None and previous["transactionId"] != state["transactionId"]:
                        raise RegenerationPending("regeneration_transaction_changed")
                    receipt["native_journal"] = state
                    store.save(operation_key, receipt)

            def check_budget():
                if self.stopping.is_set():
                    raise RegenerationPending("script_stopping")
                if deadline is not None and time.monotonic() >= deadline:
                    raise RegenerationPending("regeneration_timeout")

            class ObservedJournal(native_journal):
                def load(inner):
                    state = super().load()
                    checkpoint(state)
                    return state

                def _write(inner, state):
                    state = super()._write(state)
                    checkpoint(state)
                    check_budget()
                    return state

                def clear(inner):
                    checkpoint(super().load())
                    check_budget()
                    return super().clear()

            captured = []
            cli.RegenerationJournal = ObservedJournal
            cli.runtime = lambda _args: manager
            cli.print_json = lambda value: captured.append(value)
            args = argparse.Namespace(context=manager.context, config=manager.cfg, lease=manager.lease,
                                      dry_run=False, _operation_lock_held=True)
            identity_gate = self.managed_identity_gate(manager, store, operation_key, receipt, deadline)
            if identity_gate is not None:
                args._identity_reservation_hook = identity_gate
            self.cleanup()
            self.invalidate_regeneration_caches()
            previous_operation_lock = getattr(manager, "operation_lock_held", False)
            manager.operation_lock_held = True
            try:
                # Native subprocess and daemon socket timeouts share the same
                # recovery deadline; retry must not reset their budget.
                budget = cli.command_timeout(deadline - time.monotonic()) if _deadline is not None else nullcontext()
                with self.display_raw_docker(manager, deadline), budget:
                    check_budget()
                    if live_state is None and saved_state is None:
                        self.prepare_regeneration(cli, manager, args, binding, receipt, store, operation_key, check_budget, deadline=deadline)
                        if post_preferences is not None and receipt.get("post_preferences_plan") is None:
                            # Preparation may officially upgrade an older daemon
                            # that does not yet contain the preferences helper.
                            # Pin the original preferences after that upgrade,
                            # before the first native identity mutation.
                            from qa_ui_preferences import capture
                            receipt["post_preferences_plan"] = capture(
                                self, manager, binding, post_preferences, deadline, operation_key)
                            store.save(operation_key, receipt)
                    if post_preferences is not None and "native_location" in post_preferences:
                        from qa_ui_preferences import native_location_args
                        for name, value in native_location_args(receipt.get("post_preferences_plan")).items():
                            setattr(args, name, value)
                    if live_state is None and saved_state is not None:
                        if identity_gate is not None:
                            identity_gate.resume(saved_state)
                        verified = cli._verify_regeneration_target_state(manager, cli.DeviceIdentityStore(manager.context), saved_state)
                        google = cli._regeneration_google_runtime_postcondition(manager)
                        if google.get("ok") is not True:
                            raise RegenerationPending(google.get("error"))
                        answer = cli._regeneration_success_result(saved_state, resumed=True, cleaned=True,
                                                                 google_identity=verified["googleIdentity"])
                    else:
                        code = cli.cmd_device_regenerate(args)
                        answer = captured[-1] if captured else {}
                        if code or answer.get("ok") is not True:
                            raise RegenerationPending(answer.get("error"))
                    if (answer.get("schema") != "dev.xenoid.device-regenerate/v3" or answer.get("phase") != "committed"
                            or answer.get("runtimeOnly") is not True or answer.get("containerRecreated") is not False):
                        raise RegenerationPending("native_regeneration_result_unverified")
                    state = receipt.get("native_journal")
                    if state is None or state.get("phase") != "committed":
                        raise RegenerationPending("native_regeneration_checkpoint_missing")
                    execute = lambda values, **options: self.native_display_exec(manager, values, deadline=deadline, **options)
                    self.display_bootstrap(manager, execute, deadline)
                    self.ensure_native_interaction(manager, deadline, repair=True, foreground=True)
                    refreshed = self.verify() if self.verify else manager
                    if self.regeneration_binding(refreshed) != binding:
                        raise RuntimeIdentityError("The runtime binding changed during official regeneration")
                    self.manager = refreshed
                    result = {"ok": True, "phase": "committed", "runtimeOnly": True, "containerRecreated": False,
                              "native_ready": True, "profile_ready": True, "container_id_preserved": True,
                              "input_ready": True, "foreground_ready": True, "touch_backend": "xenoid_native",
                              "instance_id_preserved": True, "resource_tag_preserved": True,
                              "identities_changed": state["before"]["stableDigest"] != state["target"]["stableDigest"],
                              "stable_digest_before": state["before"]["stableDigest"],
                              "stable_digest_after": state["target"]["stableDigest"],
                              "boot_id_changed": state["before"]["bootId"] != state["target"]["bootId"],
                              "runtime_epoch_changed": True, "operation_key": operation_key, "replayed": False}
                    if not result["identities_changed"] or not result["boot_id_changed"]:
                        raise RegenerationPending("native_regeneration_identity_unchanged")
                    if identity_gate is not None:
                        identity_gate.finalize(state)
                    if post_google is not None or receipt.get("profile_plan") is not None or receipt.get("post_preferences_plan") is not None:
                        receipt["native_completion"] = self.regeneration_native_completion(refreshed, cli, state, result, deadline)
                        store.save(operation_key, receipt)
                    result["profile_apply"] = self.complete_regeneration_profile(refreshed, cli, receipt, store, operation_key, deadline)
                    result["ui_preferences"] = self.complete_regeneration_preferences(refreshed, receipt, store, operation_key, deadline)
                    result["cache_cleanup"] = self.complete_regeneration_post_cache(
                        refreshed, receipt, store, operation_key, deadline)
                    if post_google is not None:
                        result["google_reset"] = self.complete_regeneration_post_google(refreshed, receipt, store, operation_key, deadline)
                        self.ensure_native_interaction(refreshed, deadline, repair=True, foreground=True)
                    receipt.update(status="committed", result=result, completed_at=now())
                    store.save(operation_key, receipt)
                    if self.log:
                        self.log("action", "Official regeneration committed; container binding preserved and native profile ready")
                    return result
            except RuntimeIdentityError:
                raise
            except Exception as error:
                pending = error if isinstance(error, RegenerationPending) else RegenerationPending(getattr(error, "code", None))
                receipt["last_error"] = pending.code
                store.save(operation_key, receipt)
                raise pending from None
            finally:
                manager.operation_lock_held = previous_operation_lock
                self.invalidate_regeneration_caches()


    @contextmanager
    def owned_manager(self, *, deadline=..., allow_stopping=False):
        # Wait only before entering the operation; never replay its body/input.
        # The panel budget is authoritative (0 = unlimited). Explicit operation
        # deadlines include lock acquisition; native EX ownership is unchanged.
        from xenoid.config import InstanceError
        manager = self.verify() if self.verify else self.manager
        if deadline is Ellipsis:
            seconds = self.config.get("command_timeout", 0)
            deadline = time.monotonic() + seconds if seconds else None
        with ExitStack() as stack:
            while True:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TransportUnavailable("Native ownership wait exceeded its configured timeout")
                try:
                    stack.enter_context(manager.instance_operation_lock(
                        timeout_seconds=0.25 if remaining is None else min(0.25, remaining)))
                    break
                except InstanceError as error:
                    if error.code != "instance_busy" or str(error) != "instance operation in progress":
                        raise
                    if self.stopping.is_set() and not allow_stopping:
                        raise RuntimeError("Device script is stopping") from error
            if self.stopping.is_set() and not allow_stopping:
                raise RuntimeError("Device script is stopping")
            # Recheck the registered assignment after a potentially long wait.
            if self.verify:
                current = self.verify()
                if any(getattr(current.context, key) != getattr(manager.context, key)
                       for key in ("instance_id", "resource_tag", "state_root")):
                    raise RuntimeIdentityError("The registered instance changed while waiting for ownership")
                manager = current
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TransportUnavailable("Native ownership wait exceeded its configured timeout")
            owned, reason = manager._owned_container_record(timeout=5 if remaining is None else min(5, remaining))
            if owned is None:
                raise RuntimeError(reason or "The native runtime ownership check failed")
            if (owned.get("State") or {}).get("Running") is not True:
                raise TransportUnavailable("The registered Android container is stopped")
            container = owned.get("Id")
            if not re.fullmatch(r"[0-9a-f]{64}", str(container)) or self.container_id is not None and container != self.container_id:
                raise RuntimeIdentityError("The assigned Android runtime changed during this iteration")
            if deadline is not None and time.monotonic() >= deadline:
                raise TransportUnavailable("Native ownership verification exceeded its configured timeout")
            self.container_id = container
            if self.stopping.is_set() and not allow_stopping:
                raise RuntimeError("Device script is stopping")
            yield manager

    def owned_call(self, args, timeout):
        # The upstream convenience wrapper applies command/timeout policy. Run
        # ADB directly after validating the live container identity instead.
        deadline = time.monotonic() + timeout if timeout else None
        cleanup = args[:2] == ["forward", "--remove"]
        with self.owned_manager(deadline=deadline, allow_stopping=cleanup) as manager:
            executable = shutil.which("adb")
            if not executable:
                raise RuntimeError("ADB executable is unavailable on the device host")
            if args == ["connect"]:
                command = [executable, "connect", manager.adb_target]
            elif args[:1] in (["version"], ["--version"], ["start-server"], ["kill-server"], ["devices"]):
                command = [executable, *args]
            else:
                command = [executable, "-s", manager.adb_target, *args]
            if self.stopping.is_set() and not cleanup:
                raise RuntimeError("Device script is stopping")
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TransportUnavailable("ADB command deadline expired before sending")
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=remaining, check=False)
            return {"ok": result.returncode == 0, "code": result.returncode,
                    "stdout": result.stdout.decode(errors="replace"),
                    "stderr": result.stderr.decode(errors="replace")}

    def official_tap(self, x, y, *, input_guard=None):
        if type(x) is not int or type(y) is not int:
            raise ValueError("Touch coordinates must be integers")
        if self.stopping.is_set():
            raise RuntimeError("Device script is stopping")
        seconds = self.config.get("command_timeout", 0)
        deadline = time.monotonic() + seconds if seconds else None
        with self.owned_manager(deadline=deadline) as manager:
            state = self.ensure_native_interaction(manager, deadline, repair=True)
            raw_x, raw_y, _ = native_touch_point(state["dump"], state["profile"], x, y)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TransportUnavailable("Native touch mapping timed out before injection")
            # A navigation guard runs after native mapping, under this same
            # instance owner. It never acquires the instance lock recursively.
            with input_guard(manager, state, deadline) if input_guard else nullcontext():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TransportUnavailable("Native touch deadline expired before injection")
                options = {} if remaining is None else {"timeout": remaining}
                result = manager.daemon_client(**options).tap(raw_x, raw_y)
        shared_policy("native_tap", result=result)
        self.log("action", f"Xenoid native tap logical ({x}, {y}) → raw ({raw_x}, {raw_y}) via /dev/uinput")
        return result

    def native_interaction_snapshot(self, manager, deadline, *, foreground=False):
        """Read the actual InputReader mapping; never send a pointer event."""
        command = ("cat /data/local/tmp/xenoid-profile/effective.json; "
                   "printf '\\nXENOID_INPUT_MAPPING\\n'; dumpsys input")
        if foreground:
            command += "; printf '\\nXENOID_FOREGROUND_WINDOW\\n'; dumpsys window displays"
        response = self.native_display_exec(manager, ["sh", "-c", command], deadline=deadline)
        try:
            profile_bytes, raw = response.stdout.split(b"\nXENOID_INPUT_MAPPING\n", 1)
            profile = json.loads(profile_bytes)
            focus_dump = None
            if foreground:
                raw, focus_dump = raw.split(b"\nXENOID_FOREGROUND_WINDOW\n", 1)
            dump = raw.decode()
        except (ValueError, UnicodeError) as error:
            raise TransportUnavailable("Native touch mapping could not be read") from error
        mapping_error = None
        try:
            _x, _y, mapping = native_touch_point(dump, profile, None, None)
        except TransportUnavailable as error:
            mapping = None
            mapping_error = str(error)[:512]
        if mapping is not None:
            self._native_display_id = mapping["display_id"]
        focus = foreground_window(focus_dump.decode(), self._native_display_id) if focus_dump is not None else None
        window = {"ready": focus is not None, "package": focus[2] if focus else "",
                  "component": (focus[2] + "/" + focus[3]) if focus and focus[2] else ""}
        result = {"ready": mapping is not None, "mapping": mapping, "profile": profile,
                "profile_sha256": hashlib.sha256(profile_bytes).hexdigest(), "dump": dump,
                "foreground": window}
        if mapping_error is not None:
            result["mapping_error"] = mapping_error
        return result

    def native_input_rootd(self, manager, command, deadline):
        """Use the same token-gated official rootd transport as soft_reboot."""
        def verify():
            owned, _reason = manager._owned_container_record(timeout=8)
            if owned is None or owned.get("Id") != self.container_id:
                raise RuntimeIdentityError("The assigned runtime changed during input readiness")
        verify()
        options = {} if deadline is None else {"timeout": max(0.0, deadline - time.monotonic())}
        if options and options["timeout"] <= 0:
            raise TransportUnavailable("Native input readiness exceeded the configured timeout")
        with self.display_raw_docker(manager, deadline):
            client = manager.daemon_client(**options)
            token = client.read_private_token(force=True, **options)
            if token is None:
                raise TransportUnavailable("Native input control token is unavailable")
            reply = manager._rootd_exec(command, token, deadline=deadline)
        verify()
        if not isinstance(reply, dict) or reply.get("ok") is not True or reply.get("exitCode") != 0:
            raise TransportUnavailable("Official native input readiness command did not complete")
        return reply.get("stdout", "")

    def ensure_native_interaction(self, manager, deadline, *, repair=False, foreground=False):
        """Require the real strict mapping; repair only its owned event inode.

        A rootd repair is not a tap, a new driver, a fingerprint application or
        proof of readiness. Only a fresh InputReader/dispatcher mapping passes.
        The caller's configured deadline is the entire wait budget (0 = none).
        """
        repaired = False
        while True:
            if self.stopping.is_set():
                raise TransportUnavailable("Device script is stopping")
            if deadline is not None and time.monotonic() >= deadline:
                raise TransportUnavailable("Native interaction readiness exceeded the configured timeout")
            state = self.native_interaction_snapshot(manager, deadline, foreground=foreground)
            if state["ready"] and (not foreground or state["foreground"]["ready"]):
                return state
            if not repair:
                return state
            if not state["ready"] and not repaired:
                from qa_input_ready import (NativeInputBindingError, parse_probe,
                                            parse_repair, probe_command, repair_command)
                evidence = self.native_input_rootd(manager, probe_command(), deadline)
                binding = parse_probe(evidence, state["profile"])
                if binding.profile_sha != state["profile_sha256"]:
                    raise NativeInputBindingError("The native profile changed during input readiness")
                answer = self.native_input_rootd(manager, repair_command(binding), deadline)
                parse_repair(answer, binding)
                repaired = True
                if self.log:
                    self.log("action", "Reannounced the same official Xenoid input inode; no pointer event was sent")
            delay = 0.5 if deadline is None else min(0.5, max(0.0, deadline - time.monotonic()))
            if self.stopping.wait(delay):
                raise TransportUnavailable("Device script is stopping")

    def verify_native_display_geometry(self, manager, target, deadline, state=None):
        """Prove a generated JSON preset reaches WM, kernel and InputReader.

        No pointer event is sent. Existing official same-inode reannouncement is
        allowed when InputReader is not ready; native driver implementation and
        the calibrated per-tap mapping remain unchanged. Caller holds the native
        instance lock, as the profile transaction already does.
        """
        from qa_input_ready import (NativeInputBindingError, parse_probe,
                                    probe_command, validate_display_geometry, verify_reader_event_node)
        from qa_device_profile import profile_sha256

        desired = validate_display_geometry(target)
        expected_sha = profile_sha256(target)

        def verify_owned():
            owned, _reason = manager._owned_container_record(timeout=8)
            if (not owned or owned.get("Id") != self.container_id
                    or not owned.get("State", {}).get("Running")):
                raise RuntimeIdentityError("The assigned runtime changed during display/input verification")
            if self.stopping.is_set():
                raise TransportUnavailable("Device script is stopping")
            if deadline is not None and time.monotonic() >= deadline:
                raise TransportUnavailable("Display/input verification exceeded the configured timeout")

        def verify_profile(observed):
            if (not isinstance(observed, dict) or not isinstance(observed.get("profile"), dict)
                    or profile_sha256(observed["profile"]) != expected_sha):
                raise NativeInputBindingError("The generated profile changed during display/input verification")
            validate_display_geometry(observed["profile"])

        def read_wm():
            output = self.native_display_exec(manager, ["sh", "-c", "wm size; wm density"], deadline=deadline)
            text = output.stdout.decode(errors="strict")
            def value(kind, count):
                label = "size" if kind == "size" else "density"
                pattern = r"(\d+)x(\d+)" if count == 2 else r"(\d+)"
                physical = re.findall(r"(?m)^Physical " + label + r":\s*" + pattern + r"\s*$", text)
                override = re.findall(r"(?m)^Override " + label + r":\s*" + pattern + r"\s*$", text)
                if len(physical) != 1 or len(override) > 1:
                    raise NativeInputBindingError("WindowManager display metrics are ambiguous")
                selected = (override or physical)[0]
                return tuple(map(int, selected)) if count == 2 else int(selected)
            size, density = value("size", 2), value("density", 1)
            if size != (desired["width"], desired["height"]) or density != desired["density_dpi"]:
                raise NativeInputBindingError("WindowManager metrics do not match the generated JSON preset")
            return {"size": list(size), "density_dpi": density}

        verify_owned()
        epoch = manager._userspace_runtime_epoch(deadline=deadline)
        if not epoch:
            raise NativeInputBindingError("Android userspace epoch is unavailable")
        observed = state if state is not None else self.native_interaction_snapshot(manager, deadline)
        verify_profile(observed)  # Never repair a different/newer profile.
        if not observed.get("ready"):
            observed = self.ensure_native_interaction(manager, deadline, repair=True)
            verify_profile(observed)
        wm = read_wm()
        evidence = self.native_input_rootd(manager, probe_command(), deadline)
        binding = parse_probe(evidence, observed["profile"])
        if binding.profile_sha != observed.get("profile_sha256"):
            raise NativeInputBindingError("Kernel input probe and generated profile changed during verification")
        # A fresh reader snapshot must still refer to the same exact profile
        # bytes after probing kernel ownership, raw ABS and MT axes.
        final = self.native_interaction_snapshot(manager, deadline)
        verify_profile(final)
        if not final.get("ready") or final.get("profile_sha256") != binding.profile_sha:
            raise NativeInputBindingError("InputReader is not ready for the verified native profile")
        mapping = final.get("mapping") or {}
        orientation = mapping.get("orientation")
        if type(orientation) is not int or orientation not in (0, 1, 2, 3):
            raise NativeInputBindingError("InputReader orientation is invalid")
        logical = [desired["width"], desired["height"]]
        if orientation % 2:
            logical.reverse()
        if mapping.get("logical_size") != logical:
            raise NativeInputBindingError("InputReader logical viewport does not match the generated display")
        eventhub_id = verify_reader_event_node(final["dump"], final["profile"]["input"]["name"],
                                                mapping.get("device_id"), binding.node)
        points = []
        for y in (0, (logical[1] - 1) // 2, logical[1] - 1):
            for x in (0, (logical[0] - 1) // 2, logical[0] - 1):
                try:
                    raw_x, raw_y, actual_mapping = native_touch_point(final["dump"], final["profile"], x, y)
                except (ValueError, TransportUnavailable) as error:
                    raise NativeInputBindingError("Generated display point cannot be represented by official touch") from error
                if actual_mapping != mapping:
                    raise NativeInputBindingError("InputReader mapping changed between verified display points")
                points.append({"logical": [x, y], "raw": [raw_x, raw_y]})
        if read_wm() != wm or manager._userspace_runtime_epoch(deadline=deadline) != epoch:
            raise NativeInputBindingError("Android display changed during native touch verification")
        verify_owned()
        return {"ok": True, "events_injected": False, "official_backend": "xenoid_native",
                "native_profile_sha256": expected_sha, "raw_profile_sha256": binding.profile_sha,
                "event_node": binding.node, "eventhub_id": eventhub_id, "native_binding_digest": binding.digest,
                "userspace_epoch": epoch, "raw_bounds": desired["raw_bounds"],
                "display": {"width": desired["width"], "height": desired["height"],
                            "density_dpi": desired["density_dpi"], "logical_size": logical,
                            "orientation": orientation, "display_id": mapping.get("display_id")},
                "mapped_points": points}

    def input_status(self, timeout=None):
        """Read mapping/focus only; a script chooses what window to act on."""
        seconds = self.config.get("command_timeout", 0) if timeout is None else timeout
        if type(seconds) is not int or seconds < 0:
            raise ValueError("Input inspection timeout must be nonnegative; zero is unlimited")
        deadline = time.monotonic() + seconds if seconds else None
        manager = self.verify() if self.verify else self.manager
        with manager.instance_operation_lock(timeout_seconds=seconds or None):
            with self.owned_manager(deadline=deadline) as manager:
                state = self.native_interaction_snapshot(manager, deadline, foreground=True)
        return {"ready": state["ready"], "backend": "xenoid_native", "mapping": state["mapping"],
                "foreground": state["foreground"], "events_injected": False}

    def clear_cache(self, package, user="current", timeout=None, *, _deadline=None):
        """Use PackageManager's cache-only API; never fall through to app-data clear."""
        if not isinstance(package, str) or not ANDROID_PACKAGE.fullmatch(package):
            raise ValueError("Supply an Android package name")
        requested_user = self.config.get("android_user") if user is None else user
        if requested_user is None:
            requested_user = "current"
        if requested_user != "current" and (type(requested_user) is not int or requested_user < 0):
            raise ValueError("Use current or a nonnegative Android user id")
        seconds = self.config.get("command_timeout", 0) if timeout is None else timeout
        if type(seconds) is not int or seconds < 0:
            raise ValueError("Cache timeout must be nonnegative; zero is unlimited")
        deadline = time.monotonic() + seconds if seconds else None
        if _deadline is not None:
            deadline = _deadline if deadline is None else min(deadline, _deadline)
        if deadline is not None and deadline <= time.monotonic():
            raise TransportUnavailable("Package cache-only operation exceeded its configured timeout")
        manager = self.verify() if self.verify else self.manager
        with manager.instance_operation_lock(timeout_seconds=None if deadline is None else max(0.0, deadline - time.monotonic())):
            with self.owned_manager(deadline=deadline) as manager:
                def execute(args):
                    return self.native_display_exec(manager, args, deadline=deadline, check=False)

                if requested_user == "current":
                    current = execute(["am", "get-current-user"])
                    text = current.stdout.decode(errors="replace").strip()
                    if current.returncode or not re.fullmatch(r"[0-9]+", text):
                        raise TransportUnavailable("Android current user could not be verified")
                    selected_user = int(text)
                else:
                    selected_user = requested_user

                def package_uid():
                    listed = execute(["cmd", "package", "list", "packages", "-U", "--user", str(selected_user), package])
                    values = re.findall(r"(?m)^package:" + re.escape(package) + r" uid:([0-9]+)\s*$",
                                        listed.stdout.decode(errors="replace"))
                    if listed.returncode or len(values) != 1:
                        raise TransportUnavailable("The selected package/user UID could not be verified")
                    return int(values[0])

                before_uid = package_uid()
                # Android 13 returns 255 for valid help. Inspect its declared
                # syntax, but require exit 0 + Success for the actual operation.
                help_result = execute(["cmd", "package", "help"])
                help_text = help_result.stdout.decode(errors="replace")
                if not re.search(r"(?m)^\s*clear\s+[^\n]*\[--cache-only\][^\n]*PACKAGE", help_text):
                    raise TransportUnavailable("Android PackageManager cache-only clear is unsupported")
                cleared = execute(["cmd", "package", "clear", "--cache-only", "--user", str(selected_user), package])
                if cleared.returncode or cleared.stdout.decode(errors="replace").strip() != "Success":
                    raise TransportUnavailable("Android did not confirm the PackageManager cache-only clear")
                if package_uid() != before_uid:
                    raise RuntimeIdentityError("The package/user UID changed during cache-only clear")
                owned, _reason = manager._owned_container_record(timeout=8)
                if owned is None or owned.get("Id") != self.container_id:
                    raise RuntimeIdentityError("The assigned runtime changed during cache-only clear")
        if self.log:
            self.log("action", f"Cleared only {package} cache for Android user {selected_user}; application data preserved")
        return {"ok": True, "package": package, "user": selected_user, "uid_verified": True,
                "cache_only": True, "data_cleared": False}

    def native_display_exec(self, manager, args, *, data=None, deadline=None, check=True, stopping=True):
        """Use the pinned native Docker context; ADB runs as shell, not Android root."""
        if stopping and self.stopping.is_set():
            raise RuntimeError("Device script is stopping")
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TransportUnavailable("Display configuration timed out")
        command = [*manager.docker_base_cmd(), "exec", *(["-i"] if data is not None else []),
                   self.container_id, *args]
        try:
            result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=remaining, env=manager.docker_env(), check=False)
        except subprocess.TimeoutExpired as error:
            raise TransportUnavailable("Display configuration transport timed out") from error
        if check and result.returncode:
            detail = result.stderr.decode(errors="replace").strip()[:320]
            raise RuntimeError(detail or "Native display command failed")
        return result

    @staticmethod
    def display_arm64(data):
        return (len(data) >= 20 and data[:4] == b"\x7fELF" and data[4:6] == b"\x02\x01"
                and struct.unpack_from("<H", data, 18)[0] == 183)

    def display_state(self, execute):
        script = (
            f"printf 'sha='; sha256sum {DISPLAY_LIBRARY} | cut -d' ' -f1; "
            f"printf 'config='; cat {DISPLAY_RATE_FILE} 2>/dev/null; printf '\\n'; "
            "printf 'mounts='; "
            f"awk '$5 == \"{DISPLAY_LIBRARY}\" {{ n++ }} END {{ print n+0 }}' /proc/self/mountinfo; "
            "printf 'period='; dumpsys SurfaceFlinger --latency 2>/dev/null | head -n 1; "
            "printf 'sf_pid='; pidof surfaceflinger; "
            "printf 'boot='; getprop sys.boot_completed; "
            "printf 'composer='; getprop init.svc.vendor.hwcomposer-2-1; "
            "printf 'activity='; service check activity 2>/dev/null"
        )
        output = execute(["sh", "-c", script]).stdout.decode(errors="replace")
        state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        for key in ("mounts", "period"):
            value = state.get(key, "").strip()
            state[key] = int(value) if value.isdigit() else 0
        state["ready"] = (state.get("boot") == "1" and state.get("composer") == "running"
                          and re.search(r":\s*found\b", state.get("activity", "")) is not None
                          and state["period"] > 0)
        return state

    @staticmethod
    def display_matches(state, fps, *, period=None):
        if not state.get("ready"):
            return False
        if period is not None:
            return abs(state["period"] - period) <= 1
        if fps:
            return (state.get("sha") == DISPLAY_LIBRARY_SHA256 and state.get("mounts", 0) > 0
                    and state.get("config") == str(fps)
                    and abs(state["period"] - 1_000_000_000 // fps) <= 1)
        # These are the existing Raven hardware modes, not an imposed FPS limit.
        return not state.get("config") and any(abs(state["period"] - value) <= 1
                                              for value in (8_333_333, 16_666_667))

    def display_wait(self, execute, fps, deadline, *, period=None, previous_pid=None, stopping=True):
        last_error = None
        while deadline is None or time.monotonic() < deadline:
            try:
                state = self.display_state(execute)
                restarted = not previous_pid or bool(state.get("sf_pid") and state["sf_pid"] != previous_pid)
                if restarted and self.display_matches(state, fps, period=period):
                    return state
            except RuntimeIdentityError:
                raise
            except (RuntimeError, OSError) as error:
                last_error = error
            delay = 0.5 if deadline is None else min(0.5, max(0, deadline - time.monotonic()))
            if stopping and self.stopping.wait(delay):
                raise RuntimeError("Device script is stopping")
            if not stopping:
                time.sleep(delay)
        raise TransportUnavailable("Android display did not recover before the configured timeout"
                                   + (f": {last_error}" if last_error else ""))

    def display_profile_ready(self, execute):
        # The daemon replays wm size/density asynchronously on every service
        # start. Wait for that thread to finish before the script sets its own
        # display metrics. Linux comm truncates this thread name to 15 bytes.
        command = (
            "pids=$(pidof dev.xenoid.daemon) || exit 0; [ -n \"$pids\" ] || exit 0; "
            "for pid in $pids; do for task in /proc/$pid/task/*/comm; do "
            "[ -r \"$task\" ] || continue; IFS= read -r name <\"$task\"; "
            "case \"$name\" in xenoid-runtime-*) exit 0;; esac; done; done; printf ready"
        )
        return execute(["sh", "-c", command]).stdout.strip() == b"ready"

    @contextmanager
    def display_raw_docker(self, manager, deadline, *, stopping=True):
        # Native util.run redacts /data/local/tmp in stdout meant for people.
        # Bootstrap parses that stdout as paths/process identity, so preserve
        # exact bytes here while retaining all native ownership checks.
        absent = object()
        previous = vars(manager).get("docker_exec", absent)

        def raw(args, timeout=15):
            local_deadline = None if timeout is None else time.monotonic() + max(0, timeout)
            if deadline is not None:
                local_deadline = deadline if local_deadline is None else min(deadline, local_deadline)
            remaining = None if local_deadline is None else local_deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return {"ok": False, "error": "docker_exec_timeout"}
            owned, reason = manager._owned_container_record(timeout=5 if remaining is None else min(5, remaining))
            if owned is None or owned.get("Id") != self.container_id:
                raise RuntimeIdentityError(reason or "The assigned Android runtime changed during display setup")
            try:
                result = self.native_display_exec(manager, args, deadline=local_deadline,
                                                  check=False, stopping=stopping)
            except (TransportUnavailable, OSError) as error:
                return {"ok": False, "error": str(error)}
            return {"ok": result.returncode == 0, "returncode": result.returncode,
                    "stdout": result.stdout.decode(errors="replace"),
                    "stderr": result.stderr.decode(errors="replace"),
                    "command": [*manager.docker_base_cmd(), "exec", self.container_id, *args]}

        manager.docker_exec = raw
        try:
            yield
        finally:
            if previous is absent:
                del manager.docker_exec
            else:
                manager.docker_exec = previous

    def display_bootstrap(self, manager, execute, deadline, *, stopping=True):
        """Restore native control services and finish profile replay after graphics restart."""
        with self.display_raw_docker(manager, deadline, stopping=stopping):
            return self.display_bootstrap_ready(manager, execute, deadline, stopping=stopping)

    def display_bootstrap_ready(self, manager, execute, deadline, *, stopping=True):
        ready = False
        last_error = None
        while deadline is None or time.monotonic() < deadline:
            if stopping and self.stopping.is_set():
                raise RuntimeError("Device script is stopping")
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            try:
                if not ready:
                    status = {}
                    try:
                        options = {} if remaining is None else {"timeout": remaining}
                        client = manager.daemon_client(**options)
                        status = client.bootstrap_status(**options)
                    except RuntimeIdentityError:
                        raise
                    except (RuntimeError, OSError, ValueError) as error:
                        last_error = str(error)
                    ready = status.get("ok") is True and status.get("state") == "ready"
                    if not ready:
                        # The native method has a bounded attempt of its own.
                        # For an unlimited script, retry attempts until stopped;
                        # never turn that native attempt bound into a run limit.
                        remaining = None if deadline is None else max(0, deadline - time.monotonic())
                        if remaining is not None and remaining <= 0:
                            break
                        options = {} if remaining is None else {"timeout": remaining}
                        result = manager.reconcile_bootstrap(**options)
                        ready = result.get("ok") is True and result.get("controlReady") is True
                        if not ready:
                            last_error = result.get("error") or result.get("code") or "native bootstrap pending"
                if ready and self.display_profile_ready(execute):
                    return
            except RuntimeIdentityError:
                raise
            except (RuntimeError, OSError, ValueError) as error:
                last_error = str(error)
            delay = 0.5 if deadline is None else min(0.5, max(0, deadline - time.monotonic()))
            if stopping and self.stopping.wait(delay):
                raise RuntimeError("Device script is stopping")
            if not stopping:
                time.sleep(delay)
        raise TransportUnavailable("Native services/profile replay did not recover before the configured timeout"
                                   + (f": {last_error}" if last_error else ""))

    def display_proxy_reconcile(self, manager, deadline, *, force=False, stopping=True):
        """Reapply native proxy desired state, retaining source/node/enabled state.

        A pre-existing invalid proxy is reported independently of a valid frame
        rate change. Never turn a proxy failure into a direct-network fallback.
        """
        reconciled = False
        summary = {"ok": False, "ready": False, "reconciled": False}

        def remaining():
            value = None if deadline is None else deadline - time.monotonic()
            if value is not None and value <= 0:
                raise TransportUnavailable("display_proxy_timeout")
            return value

        def verify():
            value = remaining()
            owned, reason = manager._owned_container_record(timeout=5 if value is None else min(5, value))
            if owned is None or owned.get("Id") != self.container_id:
                raise RuntimeIdentityError(reason or "The assigned runtime changed during proxy recovery")

        def needs_reconcile(status):
            if not isinstance(status, dict) or status.get("ok") is not True or status.get("stateReadable") is False:
                return True
            if status.get("quarantined") is True:
                return True
            continuous = status.get("failurePolicy") is not None
            if continuous:
                from xenoid.daemon_client import proxy_routing_ready
                if not proxy_routing_ready(status):
                    return True
            report = status.get("report")
            if isinstance(report, dict) and report.get("phase") in ("quarantine", "quarantined", "failed", "stopped"):
                return True
            active = (isinstance(report, dict) and report.get("phase") == "active"
                      and report.get("structuralApplied") is True
                      and (continuous or report.get("dataPlaneVerified") is True))
            # mustBlock is the enabled fail-closed latch and can remain true
            # while a verified proxy is active; it alone is not quarantine.
            if status.get("mustBlock") is True and not active:
                return True
            agent = status.get("agent")
            if status.get("enabled") is True and isinstance(agent, dict) and (
                    agent.get("ready") is False or agent.get("phase") in ("quarantine", "quarantined", "failed", "stopped")):
                return True
            epoch = status.get("runtimeEpoch")
            for key in ("ownerRuntimeEpoch", "agentRuntimeEpoch"):
                if epoch and status.get(key) and status[key] != epoch:
                    return True
            for key in ("report", "owner", "agent"):
                part = status.get(key)
                if isinstance(part, dict) and epoch and part.get("runtimeEpoch") and part["runtimeEpoch"] != epoch:
                    return True
            return False

        def host_needs_reconcile(status):
            # Android can retain a successful report after its host agent dies.
            # Read the actual native process/engine state before reusing it.
            if not isinstance(status, dict) or status.get("enabled") is not True:
                return False
            if (status.get("stateReadable") is not True or status.get("configured") is not True
                    or type(status.get("generation")) is not int or status["generation"] < 0
                    or not isinstance(status.get("runtimeEpoch"), str) or not status["runtimeEpoch"]):
                summary.update(live_host_checked=False, live_host_ready=False)
                return True
            verify()
            value = remaining()
            if value is None:
                budget = nullcontext()
            else:
                from xenoid.util import command_timeout
                budget = command_timeout(value)
            with self.display_raw_docker(manager, deadline, stopping=stopping), budget:
                live = manager.proxy_engine_status()
            verify()
            ready = (isinstance(live, dict) and live.get("ok") is True
                     and live.get("phase") == "active" and live.get("structuralApplied") is True
                     and live.get("quarantined") is not True)
            if status.get("failurePolicy") is not None:
                from xenoid.daemon_client import proxy_routing_ready
                ready = proxy_routing_ready(status, live)
            if ready:
                ready = (type(live.get("generation")) is int
                         and all(live.get(key) == status[key] for key in ("generation", "runtimeEpoch")))
            summary.update(live_host_checked=True, live_host_ready=ready)
            return not ready

        try:
            if stopping and self.stopping.is_set():
                raise RuntimeError("device_stopping")
            verify()
            value = remaining()
            options = {} if value is None else {"timeout": value}
            client = manager.daemon_client(**options)
            try:
                status = client.proxy_status(**options)
            except RuntimeIdentityError:
                raise
            except Exception:
                status = {}
            required = force or needs_reconcile(status) or host_needs_reconcile(status)
            if required:
                value = remaining()
                # The official API has no timeout argument. Its native context
                # bounds subprocess/socket operations by this setup deadline.
                if value is None:
                    budget = nullcontext()
                else:
                    from xenoid.util import command_timeout
                    budget = command_timeout(value)
                with self.display_raw_docker(manager, deadline, stopping=stopping), budget:
                    result = manager.reconcile_proxy_desired()
                reconciled = True
                verify()
                value = remaining()
                options = {} if value is None else {"timeout": value}
                status = client.proxy_status(**options)
                success = isinstance(result, dict) and result.get("ok") is True
                if not success:
                    summary["code"] = (result.get("code") or result.get("error")) if isinstance(result, dict) else None
            else:
                success = True
            ready = (success and isinstance(status, dict) and status.get("ok") is True
                     and not needs_reconcile(status) and not host_needs_reconcile(status))
            summary.update(ok=ready, ready=ready, reconciled=reconciled)
            if isinstance(status, dict):
                for key in ("enabled", "configured", "quarantined", "mustBlock"):
                    if type(status.get(key)) is bool:
                        summary[key] = status[key]
            if not ready and not summary.get("code"):
                summary["code"] = "proxy_recovery_pending"
        except RuntimeIdentityError:
            raise
        except Exception:
            # Native exceptions can contain credentials. Return stable codes,
            # never arbitrary exception text, source values or endpoint URLs.
            expired = deadline is not None and time.monotonic() >= deadline
            summary.update(code="proxy_recovery_timeout" if expired else "proxy_recovery_failed", reconciled=reconciled)
        if summary.get("code") and not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", str(summary["code"])):
            summary["code"] = "proxy_recovery_failed"
        if not summary["ready"] and self.log:
            self.log("warning", "Display is configured; desired proxy recovery remains pending ("
                     + str(summary.get("code") or "proxy_recovery_failed") + ")")
        return summary

    def set_frame_rate(self, fps, timeout=None):
        """Apply a script-selected rate; None preserves it and 0 restores native modes."""
        if timeout is not None and (type(timeout) is not int or timeout < 0):
            raise ValueError("Display timeout must be a non-negative integer or null")
        if fps is None:
            return {"fps": None, "changed": False, "restarted": False}
        if type(fps) is not int or fps < 0 or fps > 1_000_000_000:
            raise ValueError("Frame rate must be a non-negative integer with a representable nanosecond period")
        binary = gzip.decompress(base64.b64decode(DISPLAY_LIBRARY_GZIP))
        if not self.display_arm64(binary) or hashlib.sha256(binary).hexdigest() != DISPLAY_LIBRARY_SHA256:
            raise RuntimeError("Bundled display library failed its integrity check")
        seconds = self.config.get("command_timeout", 0) if timeout is None else timeout
        deadline = time.monotonic() + seconds if seconds else None
        with self.owned_manager(deadline=deadline) as manager:
            def execute(args, **options):
                return self.native_display_exec(manager, args, deadline=deadline, **options)

            for path in (DISPLAY_LIBRARY, DISPLAY_BASE_LIBRARY):
                if not self.display_arm64(execute(["cat", path]).stdout):
                    raise ValueError("This display helper requires the ARM64 Raven/Redroid composer")
            before = self.display_state(execute)
            if self.display_matches(before, fps):
                self.display_bootstrap(manager, execute, deadline)
                proxy = self.display_proxy_reconcile(manager, deadline)
                return {"fps": fps, "changed": False, "restarted": False,
                        "period_ns": before["period"], "container_id": self.container_id, "proxy": proxy}
            saved = execute(["sh", "-c", f"if [ -f {DISPLAY_RATE_FILE} ]; then cat {DISPLAY_RATE_FILE}; else exit 42; fi"], check=False)
            if saved.returncode not in (0, 42):
                raise RuntimeError("Could not preserve the current display configuration")
            old_config = saved.stdout if saved.returncode == 0 else None
            original_mounts = before["mounts"]
            mutation_started = False
            mount_attempted = False

            def write_config(run, value):
                if value is None:
                    run(["rm", "-f", DISPLAY_RATE_FILE])
                else:
                    run(["sh", "-c", f"set -e; f=$(mktemp {DISPLAY_RATE_FILE}.XXXXXX); "
                         "trap 'rm -f \"$f\"' EXIT; cat >\"$f\"; chmod 0644 \"$f\"; "
                         f"mv -f \"$f\" {DISPLAY_RATE_FILE}"], data=value)

            try:
                mutation_started = True
                write_config(execute, f"{fps}\n".encode() if fps else None)
                if fps and (before.get("sha") != DISPLAY_LIBRARY_SHA256 or not original_mounts):
                    stage = f"{DISPLAY_STAGE_DIR}/hwcomposer-{DISPLAY_LIBRARY_SHA256}.so"
                    execute(["mkdir", "-p", DISPLAY_STAGE_DIR])
                    execute(["sh", "-c", f"set -e; f=$(mktemp {DISPLAY_STAGE_DIR}/library.XXXXXX); "
                             "trap 'rm -f \"$f\"' EXIT; cat >\"$f\"; chmod 0644 \"$f\"; "
                             f"mv -f \"$f\" {stage}"], data=binary)
                    mount_attempted = True
                    execute(["mount", "--bind", stage, DISPLAY_LIBRARY])
                execute(["setprop", "ctl.restart", "vendor.hwcomposer-2-1"])
                after = self.display_wait(execute, fps, deadline, previous_pid=before.get("sf_pid"))
                self.display_bootstrap(manager, execute, deadline)
                proxy = self.display_proxy_reconcile(manager, deadline, force=True)
            except RuntimeIdentityError:
                # Never issue rollback commands after the container binding is lost.
                raise
            except BaseException as error:
                if not mutation_started:
                    raise
                # Cleanup has its own configured transport budget so an expired
                # apply deadline cannot prevent restoration of the prior display.
                rollback_deadline = time.monotonic() + seconds if seconds else None
                def restore(args, **options):
                    return self.native_display_exec(manager, args, deadline=rollback_deadline,
                                                    stopping=False, **options)
                try:
                    write_config(restore, old_config)
                    if mount_attempted:
                        restore(["sh", "-c", "set -e; "
                                 f"while [ \"$(awk '$5 == \"{DISPLAY_LIBRARY}\" {{ n++ }} END {{ print n+0 }}' /proc/self/mountinfo)\" -gt {original_mounts} ]; do "
                                 f"umount {DISPLAY_LIBRARY}; done"])
                    previous_pid = restore(["pidof", "surfaceflinger"], check=False).stdout.decode().strip()
                    restore(["setprop", "ctl.restart", "vendor.hwcomposer-2-1"])
                    rollback_period = before["period"] or None
                    if rollback_period is None and before.get("sha") == DISPLAY_LIBRARY_SHA256 and old_config:
                        saved_rate = old_config.strip()
                        if saved_rate.isdigit() and 0 < int(saved_rate) <= 1_000_000_000:
                            rollback_period = 1_000_000_000 // int(saved_rate)
                    self.display_wait(restore, None, rollback_deadline,
                                      period=rollback_period, previous_pid=previous_pid, stopping=False)
                    self.display_bootstrap(manager, restore, rollback_deadline, stopping=False)
                    proxy = self.display_proxy_reconcile(manager, rollback_deadline, force=True, stopping=False)
                except BaseException as rollback_error:
                    raise TransportUnavailable(f"Display change failed ({error}); rollback could not be verified ({rollback_error})") from error
                network = "" if proxy.get("ready") else "; desired proxy recovery remains pending"
                raise TransportUnavailable(f"Display change failed; previous configuration restored{network}: {error}") from error
        self.window_offer = None
        if self.log:
            self.log("action", f"Display rate {'native' if fps == 0 else str(fps) + ' FPS'} verified; Android graphics restarted")
        return {"fps": fps, "changed": True, "restarted": True,
                "period_ns": after["period"], "container_id": self.container_id, "proxy": proxy}

    def adb(self, args, timeout=None):
        if self.stopping.is_set():
            raise RuntimeError("Device script is stopping")
        seconds = self.config.get("command_timeout", 0) if timeout is None else timeout
        if self.package_metadata_may_change(args):
            # A package-manager request can partially change state even if it
            # later errors or times out, so discard cached proof before sending.
            self.app_cache.clear()
            self.resolved_app = None
        try:
            result = self.owned_call(args, seconds or None)
        except subprocess.TimeoutExpired as error:
            # A timeout says nothing about whether a mutating command completed.
            # Classify it for the caller, but never resend the command here.
            detail = f"ADB {args[0] if args else 'command'} timed out after {error.timeout}s"
            raise TransportUnavailable(detail) from error
        if not result.get("ok"):
            detail = result.get("stderr") or result.get("message") or result.get("error") or "ADB command failed"
            detail = str(detail).strip()[:320]
            if "offline" in detail.lower() or re.search(r"device .+ not found|no devices/emulators found", detail, re.I):
                raise TransportUnavailable(detail)
            raise RuntimeError(detail)
        output = (result.get("stdout") or "").strip()
        return output

    @staticmethod
    def package_metadata_may_change(args):
        if args[:1] in (["install"], ["install-multiple"], ["install-multi-package"], ["uninstall"], ["root"], ["unroot"], ["reboot"]):
            return True
        if args[:1] != ["shell"]:
            return False
        text = " ".join(args[1:])
        # Only cache invalidation, not a command allowlist. Data-only pm clear
        # deliberately keeps the APK cache: it does not replace package code.
        return bool(re.search(r"\b(?:pm|cmd\s+package)\s+(?:install\S*|uninstall|remove-user|create-user)\b|\bam\s+switch-user\b", text))

    def app_assignment_key(self):
        assigned = self.assigned_apk or {}
        return (self.container_id, self.config.get("instance_id"), self.config.get("resource_tag"),
                self.android_user(), assigned.get("id"), assigned.get("sha256"), self.assigned_apk_size)

    def cleanup(self, *, drain=False):
        reader_error = None
        try:
            self.close_ui_reader(drain=drain)
        except Exception as error:
            reader_error = error
        with self.forward_lock:
            port, self.port = self.port, None
        if port is not None:
            # A disconnected ADB transport releases its forwards. Never remove a port through a new runtime.
            self.owned_call(["forward", "--remove", f"tcp:{port}"], 4)
        if reader_error:
            raise reader_error

    def close_ui_reader(self, *, force=False, drain=False):
        reader = getattr(self, "_ui_reader", None)
        if reader is not None:
            # A final round teardown retains this reader until its original
            # native owner becomes available. Only lock entry is retried: the
            # fenced close body, other lock errors and explicit Shutdown are not.
            seconds = self.config.get("command_timeout", 0)
            deadline = None if drain or not seconds else time.monotonic() + seconds
            logged_at = None
            with ExitStack() as stack:
                while True:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TransportUnavailable("UI reader cleanup ownership wait exceeded its configured timeout")
                    try:
                        stack.enter_context(reader.manager.instance_operation_lock(
                            timeout_seconds=.25 if remaining is None else min(.25, remaining)))
                        break
                    except Exception as error:
                        if getattr(error, "code", None) != "instance_busy" or str(error) != "instance operation in progress":
                            raise
                        from xenoid.config import InstanceError
                        if not isinstance(error, InstanceError):
                            raise
                        now = time.monotonic()
                        if self.log and (logged_at is None or now - logged_at >= 5):
                            self.log("warning", "Waiting for native ownership before UI reader cleanup")
                            logged_at = now
                reader.close(force=force)
            self._ui_reader = None

    @staticmethod
    def inspection_priority(value):
        if value is not None and (type(value) is not int or not -20 <= value <= 19):
            raise ValueError("UI inspection priority must be an integer from -20 to 19 or null")
        return value

    def focus_command(self, priority=None):
        priority = self.inspection_priority(priority)
        return ["shell", *(["nice", "-n", str(priority)] if priority is not None else []), "dumpsys", "window", "displays"]

    def ui_hierarchy(self, timeout, priority=None, windows=False, reader=None):
        if reader not in (None, "persistent", "legacy"):
            raise ValueError("reader must be persistent or legacy")
        priority = self.inspection_priority(priority)
        seconds = self.config.get("command_timeout", 0) if timeout is None else timeout
        if reader == "legacy":
            self.close_ui_reader()
            return self.legacy_ui_hierarchy(timeout, priority, windows)
        from qa_ui_reader import PersistentUiReader, UiReaderError
        deadline = time.monotonic() + seconds if seconds else None
        self._hierarchy_focus_observed = False
        self._hierarchy_focus = None
        self._hierarchy_geometry = None
        try:
            with self.owned_manager(deadline=deadline) as manager:
                current = self._ui_reader
                if (current is None or current.pipe is None or current.pipe.closed or
                        current.pipe.process.poll() is not None or current.priority != priority):
                    self.close_ui_reader()
                    current = PersistentUiReader(manager, self.container_id, priority, deadline, self.stopping,
                                                 defer_start=True)
                    self._ui_reader = current
                    current.start()
                xml = current.read(manager, windows, deadline)
        except RuntimeIdentityError:
            raise
        except (UiReaderError, OSError, TimeoutError) as error:
            raise TransportUnavailable("Android UI inspection failed: " + str(error)) from error
        root_tag = "displays" if windows else "hierarchy"
        start, end = xml.find("<" + root_tag), xml.rfind("</" + root_tag + ">")
        if start < 0 or end < start:
            raise RuntimeError("Invalid Android UI hierarchy")
        return xml[start:end + len(root_tag) + 3]

    def legacy_ui_hierarchy(self, timeout, priority=None, windows=False):
        priority = self.inspection_priority(priority)
        # A host adb timeout does not kill its guest Java process. Bound the
        # actual uiautomator executable (its wrapper execs app_process), not a
        # surrounding shell: toybox timeout does not reliably kill grandchildren.
        seconds = self.config.get("command_timeout", 0) if timeout is None else timeout
        marker = "XENOID_UI_FOCUS_AFTER"
        ui_args = ["uiautomator", "dump"]
        if windows:
            ui_args.append("--windows")
        if seconds:
            guest_budget = max(1, seconds - 2)
            ui_args = ["timeout", "-s", "KILL", str(guest_budget), *ui_args]
            post_focus = (f"remaining=$(({guest_budget} - $(date +%s) + started))\n"
                          "[ \"$remaining\" -gt 0 ] || exit 124\n"
                          "timeout -s KILL \"$remaining\" dumpsys window displays")
        else:
            post_focus = "dumpsys window displays"
        # uiautomator's file writer does not reliably emit XML through /dev/tty
        # or /proc/self/fd/1 under adb shell. Use a private, unique guest file and
        # stream it only after a successful dump; the shell always removes it.
        body = ("umask 077\n"
                "dump=$(mktemp /data/local/tmp/xenoid-ui.XXXXXX) || exit 1\n"
                "trap 'rm -f \"$dump\"' 0\n"
                "trap 'exit 130' 1 2 15\n"
                "started=$(date +%s)\n" + shlex.join(ui_args) + " \"$dump\" >/dev/null\n"
                "status=$?\n"
                "[ \"$status\" -eq 0 ] || exit \"$status\"\n"
                "[ -s \"$dump\" ] || exit 65\n"
                "cat \"$dump\"\n"
                f"printf '\\n{marker}\\n'\n" + post_focus)
        if priority is not None:
            # Per-call scheduling only: renderer/app priorities are not changed.
            body = shlex.join(["nice", "-n", str(priority), "sh", "-c", body])
        self._hierarchy_focus_observed = False
        self._hierarchy_focus = None
        self._hierarchy_geometry = None
        try:
            xml = self.adb(["shell", body], timeout=seconds)
        except RuntimeIdentityError:
            raise
        except RuntimeError as error:
            raise TransportUnavailable("Android UI inspection failed: " + str(error)) from error
        if marker in xml:
            xml, focus_dump = xml.rsplit(marker, 1)
            self._hierarchy_focus = foreground_window(focus_dump, self._native_display_id)
            self._hierarchy_geometry = current_display_geometry(focus_dump, self._native_display_id)
            self._hierarchy_focus_observed = True
        root_tag = "displays" if windows else "hierarchy"
        start, end = xml.find("<" + root_tag), xml.rfind("</" + root_tag + ">")
        if start < 0 or end < start:
            raise RuntimeError("Invalid Android UI hierarchy")
        return xml[start:end + len(root_tag) + 3]

    def inspect_window(self, package=None, priority=None, windows=False, reader=None):
        priority = self.inspection_priority(priority)
        if type(windows) is not bool:
            raise ValueError("windows must be Boolean")
        if reader not in (None, "persistent", "legacy"):
            raise ValueError("reader must be persistent or legacy")
        if package is not None and (
            not isinstance(package, str) or len(package) > 255 or not ANDROID_PACKAGE.fullmatch(package)
        ):
            raise ValueError("Supply an Android package name")
        # Starting any newer hierarchy read retires older navigation offers.
        # No instance/operation lock is acquired while holding this short lock.
        with self._native_navigation_lock:
            self._native_navigation_generation += 1
            generation = self._native_navigation_generation
            self._native_navigation_offer = None
        focus_dump = self.adb(self.focus_command(priority))
        focus = foreground_window(focus_dump, self._native_display_id)
        geometry_before = current_display_geometry(focus_dump, self._native_display_id)
        focus_before_complete = time.monotonic()
        snapshot = {"package": "", "component": "", "stable": False, "controls": [], "truncated": False}
        if focus is not None:
            _, _, current_package, activity = focus
            component = f"{current_package}/{activity}" if current_package else activity
            snapshot.update(package=current_package, component=component, stable=True)
        self._hierarchy_focus_observed = False
        hierarchy_started = time.monotonic()
        try:
            if windows:
                kwargs = {"priority": priority, "windows": True}
                if reader is not None:
                    kwargs["reader"] = reader
                hierarchy = self.ui_hierarchy(None, **kwargs)
                hierarchy_received = time.monotonic()
                window_data = shared_policy("window_snapshot", hierarchy=hierarchy,
                                            display_id=self._native_display_id if self._native_display_id is not None else 0)
                controls, truncated = window_data["controls"], window_data["truncated"]
            else:
                if reader is not None:
                    hierarchy = self.ui_hierarchy(None, priority=priority, reader=reader)
                else:
                    hierarchy = self.ui_hierarchy(None) if priority is None else self.ui_hierarchy(None, priority=priority)
                hierarchy_received = time.monotonic()
                controls, truncated = shared_policy("visible_controls", hierarchy=hierarchy, package=package)
                window_data = {}
        except TransportUnavailable:
            raise
        except (RuntimeError, ValueError, ElementTree.ParseError):
            snapshot["stable"] = False
            return snapshot
        hierarchy_complete = time.monotonic()
        # Count only the gap AFTER the first verified focus response and BEFORE
        # the final independent focus query. Setup/transport before that first
        # response, and the final query's own duration, cannot become ad grace.
        focus_after_started = time.monotonic()
        compound_focus = self._hierarchy_focus_observed
        if compound_focus:
            current = self._hierarchy_focus
            geometry_after = getattr(self, "_hierarchy_geometry", None)
        else:
            current_dump = self.adb(self.focus_command(priority))
            current = foreground_window(current_dump, self._native_display_id)
            geometry_after = current_display_geometry(current_dump, self._native_display_id)
        snapshot["stable"] = focus is not None and current == focus
        if not snapshot["package"]:
            snapshot["package"] = next((control.get("package", "") for control in controls
                                        if control.get("package")), "")
        snapshot.update(window_data)
        snapshot.update(controls=controls, truncated=truncated)
        display_id = self._native_display_id if self._native_display_id is not None else 0
        app_windows = [item for item in window_data.get("windows", ())
                       if item.get("window_type") == "TYPE_APPLICATION"]
        active_apps = [item for item in app_windows
                       if item.get("window_active") is True or item.get("window_focused") is True]
        owned_roots = [item for item in active_apps if item.get("root_node_present_in_XML") is True
                       and item.get("root_package") == snapshot["package"]]
        hierarchy_state = ("not_requested" if not windows else
                           "unknown_legacy_root" if not hierarchy.startswith("<displays") else
                           "partial_application_window_missing" if not active_apps else
                           "partial_application_root_missing" if not any(item.get("root_node_present_in_XML") for item in active_apps) else
                           "available" if owned_roots else "partial_application_owner_mismatch")
        snapshot["focus_observation"] = {
            "schema": "xenoid.wm-focus-observation/v1", "exact_match": snapshot["stable"],
            "basis": "legacy_compound_uncredited" if compound_focus else "independent_wm_focus_pair",
            "display_id": display_id, "token": focus[0] if focus else None,
            "user": int(focus[1]) if focus else None,
            "package": snapshot["package"], "component": snapshot["component"],
            "geometry_match": geometry_before is not None and geometry_before == geometry_after,
            "geometry": list(geometry_before) if geometry_before is not None else None,
            "elapsed_seconds": max(0.0, focus_after_started - focus_before_complete)
                               if snapshot["stable"] and not compound_focus else 0.0,
            "hierarchy_read_seconds": max(0.0, hierarchy_received - hierarchy_started),
            "hierarchy_inspection_seconds": max(0.0, hierarchy_complete - hierarchy_started),
            "hierarchy_state": hierarchy_state,
            "application_windows": len(app_windows), "active_application_windows": len(active_apps),
            # Stock collection has no per-node timestamp. This is NOT a claim
            # that every node was collected at RPC completion.
            "node_age_basis": "within_stock_collection_interval",
        }
        back = native_navigation_back(snapshot)
        # System windows can have a raw title without a package in WM focus.
        # Preserve that tuple; input separately verifies the real SystemUI Back.
        if (back is not None and focus is not None and snapshot["stable"] and
                geometry_before is not None and geometry_before == geometry_after):
            token = NATIVE_NAV_PREFIX + secrets.token_urlsafe(24)
            offer = {"token": token, "generation": generation, "focus": focus, "display_id": display_id,
                     "geometry": geometry_before,
                     "created_at": time.monotonic(), "bounds": list(back["bounds"]),
                     "window_bounds": list(back["window_bounds"]), "window_id": back["window_id"],
                     "window_layer": back["window_layer"]}
            with self._native_navigation_lock:
                if self._native_navigation_generation == generation:
                    self._native_navigation_offer = offer
                    snapshot["native_navigation_observation"] = token
        return snapshot

    @contextmanager
    def native_navigation_guard(self, offer, manager, state, deadline):
        # Lock ordering is instance/operation -> navigation. All offer creation
        # and consumption releases navigation before acquiring instance locks.
        # Holding this through injection serializes competing observations.
        with self._native_navigation_lock:
            def reject(reason):
                raise NativeObservationRejected(reason)

            def fresh():
                age = time.monotonic() - offer["created_at"]
                if (self._native_navigation_generation != offer["generation"] or
                        not 0 <= age <= NATIVE_NAV_MAX_AGE_SECONDS):
                    reject("navigation_observation_stale")
                if self.stopping.is_set() or deadline is not None and time.monotonic() >= deadline:
                    reject("navigation_operation_expired")

            fresh()
            mapping = state.get("mapping") or {}
            size = mapping.get("logical_size")
            box, viewport = offer["bounds"], offer["window_bounds"]
            if (type(mapping.get("display_id")) is not int or mapping["display_id"] != offer["display_id"] or
                    not isinstance(size, (list, tuple)) or len(size) != 2 or
                    any(type(value) is not int or value <= 0 for value in size) or
                    tuple(size) != offer["geometry"][:2] or type(mapping.get("orientation")) is not int or
                    mapping["orientation"] != offer["geometry"][2] or
                    not (0 <= viewport[0] <= box[0] < box[2] <= viewport[2] <= size[0] and
                         0 <= viewport[1] <= box[1] < box[3] <= viewport[3] <= size[1])):
                reject("navigation_display_geometry_changed_or_unproven")
            proof_deadline = offer["created_at"] + NATIVE_NAV_MAX_AGE_SECONDS
            if deadline is not None:
                proof_deadline = min(proof_deadline, deadline)
            # Reacquire only the real navigation window in the already-running
            # serialized reader. Never start it, dump the app, or recursively
            # acquire the instance owner from this guard.
            from qa_ui_reader import DumpFailed, UiReaderError
            reader = self._ui_reader
            if (reader is None or reader.closed or not reader.started or reader.pipe is None
                    or reader.pipe.closed or reader.pipe.process.poll() is not None):
                reject("navigation_reader_unavailable")
            offer["verification_stages"] = {}
            nav_started = time.monotonic()
            try:
                # A status-1 reply completed its Java disconnect and leaves the
                # same pipe synchronized. Reacquire at most once for a narrowly
                # unavailable provider result, within this offer's original lease.
                # Never retry a changed/ambiguous node, transport, or input call.
                for attempt in range(2):
                    fresh()
                    if (self._ui_reader is not reader or reader.closed or not reader.started or
                            reader.pipe is None or reader.pipe.closed or reader.pipe.process.poll() is not None):
                        reject("navigation_reader_unavailable")
                    offer["verification_stages"]["fresh_node_attempts"] = attempt + 1
                    try:
                        proof = reader.navigation(manager, offer["display_id"], proof_deadline)
                        break
                    except DumpFailed as error:
                        code = str(error)
                        offer["verification_stages"]["fresh_node_error_code"] = (
                            code if re.fullmatch(r"nav_[a-z_]{1,72}", code)
                            else "unrecognized_navigation_dump_failure")
                        if attempt or code not in {"nav_display_unavailable", "nav_bar_unavailable",
                                                   "nav_root_unavailable", "nav_back_unavailable"}:
                            reject("navigation_fresh_back_unavailable")
                        # The next navigation() repeats native ownership, epoch,
                        # helper and frame checks before its fresh provider read.
                        fresh()
            except (UiReaderError, OSError, TimeoutError) as error:
                # An unreadable epoch proves no runtime transition. Reject this
                # consumed offer only after fenced cleanup is positively complete;
                # the next ordinary observation must acquire a fresh reader/token.
                if (isinstance(error, UiReaderError) and str(error) == "ui_reader_epoch_unreadable" and
                        getattr(error, "cleanup_error", None) is None and reader.closed is True and
                        reader.private_identity is None and reader.pipe.closed is True and
                        reader.pipe.cleanup_pending is False and reader.pipe.process.poll() is not None):
                    reject("navigation_reader_epoch_unreadable")
                # Preserve real runtime changes, unknown cleanup and transport
                # failures. Never reinterpret a lost input acknowledgement.
                raise TransportUnavailable("Navigation reader verification failed: " + str(error)) from error
            finally:
                offer["verification_stages"]["fresh_node_read_seconds"] = max(0.0, time.monotonic() - nav_started)
            if not native_navigation_node_matches(proof, offer):
                reject("navigation_back_node_changed_or_unproven")
            fresh()
            routing_started = time.monotonic()
            windows = self.native_display_exec(manager, ["dumpsys", "window", "windows"], deadline=proof_deadline)
            routing = self.native_display_exec(manager, ["dumpsys", "input"], deadline=proof_deadline)
            current = self.native_display_exec(manager, ["dumpsys", "window", "displays"], deadline=proof_deadline)
            current_dump = current.stdout.decode(errors="strict")
            if foreground_window(current_dump, offer["display_id"]) != offer["focus"]:
                reject("navigation_focus_changed")
            if current_display_geometry(current_dump, offer["display_id"]) != offer["geometry"]:
                reject("navigation_display_geometry_changed_or_unproven")
            latest_input = routing.stdout.decode(errors="strict")
            try:
                native_navigation_routing(current_dump, windows.stdout.decode(errors="strict"), latest_input, offer)
            except ValueError:
                reject("navigation_routing_changed_or_unproven")
            center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            if (native_touch_point(latest_input, state["profile"], *center) !=
                    native_touch_point(state["dump"], state["profile"], *center)):
                reject("navigation_native_mapping_changed")
            fresh()
            offer["verification_stages"].update(
                routing_read_seconds=max(0.0, time.monotonic() - routing_started),
                maximum_node_age_before_input_seconds=max(0.0, time.monotonic() - nav_started))
            yield

    def handle_native_navigation(self, observation, action):
        started = time.monotonic()
        with self._native_navigation_lock:
            offer = self._native_navigation_offer
            if offer is None or observation != offer["token"]:
                return {"handled": False, "action": action, "reason": "navigation_observation_not_current",
                        "official_backend": "xenoid_native", "input_sent": False}
            # Consume before mapping, any failure, or a lost input acknowledgement.
            self._native_navigation_offer = None
        if action != "back":
            return {"handled": False, "action": action, "reason": "navigation_action_not_observed",
                    "official_backend": "xenoid_native", "input_sent": False}
        left, top, right, bottom = offer["bounds"]
        try:
            result = self.official_tap((left + right) // 2, (top + bottom) // 2,
                input_guard=lambda manager, state, deadline: self.native_navigation_guard(offer, manager, state, deadline))
        except NativeObservationRejected as error:
            return {"handled": False, "action": action, "reason": error.reason,
                    "official_backend": "xenoid_native", "input_sent": False,
                    "verification_seconds": max(0.0, time.monotonic() - started),
                    "verification_stages": offer.get("verification_stages", {}),
                    "observation_age_seconds": max(0.0, time.monotonic() - offer["created_at"])}
        return {"handled": True, "action": action, "reason": "observed_native_navigation_sent",
                "official_backend": "xenoid_native", "input_sent": True, "result": result,
                "verification_seconds": max(0.0, time.monotonic() - started),
                "verification_stages": offer.get("verification_stages", {}),
                "observation_age_seconds": max(0.0, time.monotonic() - offer["created_at"])}

    def discover_shared_app(self):
        listing = self.adb(["shell", "pm", "list", "packages", "--user", self.android_user(), "-f"])
        paths = shared_policy("apk_candidates", listing=listing)
        # The panel binds a specific APK archive to this device. Filter by byte size first.
        # Package names and paths come from Android, and only verified data-app base APKs are eligible.
        find_digest = r"""
size=$1
digest=$2
shift 2
for apk do
    actual=$(stat -c %s "$apk" 2>/dev/null) || continue
    [ "$actual" = "$size" ] || continue
    sha=$(sha256sum "$apk" 2>/dev/null) || continue
    if [ "${sha%% *}" = "$digest" ]; then
        printf '%s\n' "$apk"
    fi
done
"""
        command = shlex.join(["sh", "-c", find_digest, "fleet-app-scan", str(self.assigned_apk_size),
                              self.assigned_apk["sha256"], *paths])
        matching = self.adb(["shell", command]).splitlines()
        package = shared_policy("apk_match", paths=paths, matching=matching)
        # Discovery has already checked exact file size and SHA-256. Repeating
        # pm path + sha256sum here and before every action added substantial I/O.
        key = self.app_assignment_key()
        self.app_cache["resolution"] = (key, package)
        self.app_cache["verified"] = (key, package)
        return package

    def android_user(self):
        user = self.config.get("android_user")
        return "current" if user is None else str(user)

    def verify_test_app(self):
        strict = self.config.get("verify_apk", False)
        package = self.config.get("package") or (self.app_test or {}).get("package") or self.resolved_app
        if package is None and self.assigned_apk:
            resolved = self.app_cache.get("resolution")
            if resolved and resolved[0] == self.app_assignment_key():
                package = resolved[1]
        if package is None and self.assigned_apk:
            try:
                package = self.discover_shared_app()
            except (RuntimeError, ValueError):
                if strict:
                    raise
        if package is None and not strict:
            focus = foreground_window(self.adb(self.focus_command()), self._native_display_id)
            package = focus[2] if focus else None
        if not package:
            raise RuntimeError("Set package in panel/script settings, or bring an app to the foreground")
        if not ANDROID_PACKAGE.fullmatch(package):
            raise ValueError("Invalid Android package")
        self.resolved_app = package
        if strict:
            if not self.assigned_apk:
                raise RuntimeError("APK verification requires an assigned APK")
            key = self.app_assignment_key()
            if self.app_cache.get("verified") != (key, package):
                path = shared_policy("apk_path", output=self.adb(
                    ["shell", "pm", "path", "--user", self.android_user(), package]))
                checksum = self.adb(["shell", "sha256sum", path])
                shared_policy("apk_checksum", path=path, checksum=checksum, expected=self.assigned_apk["sha256"])
                self.app_cache["verified"] = (self.app_assignment_key(), package)
        return package

    def tap_test_control(self, package, resource_id, *, dialog=False):
        selector = shared_policy("selector", package=package, resource_id=resource_id, dialog=dialog,
                                 debug_only=False)
        root = ElementTree.fromstring(self.ui_hierarchy(None))
        bounds = shared_policy("unique_control", root=root, package=None, selector=selector)
        if bounds is None:
            return False
        left, top, right, bottom = bounds
        self.official_tap((left + right) // 2, (top + bottom) // 2)
        return True

    def app_test_action(self, action, args):
        # UI selectors and positive system buttons do not require APK ownership.
        if action == "app-confirm":
            return self.tap_test_control("", "", dialog=True)
        if action == "app-tap" and ":id/" in args[0]:
            return self.tap_test_control("", args[0])
        package = self.verify_test_app()
        user = self.android_user()
        if action == "app-package":
            return package
        if action == "app-tap":
            return self.tap_test_control(package, args[0])
        if action == "app-open":
            resolved = self.adb(["shell", "cmd", "package", "resolve-activity", "--brief", "--user", user,
                                 "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                                 "-p", package])
            components = re.findall(r"(?m)^" + re.escape(package) + r"/[.\w$]+$", resolved, re.ASCII)
            if not components:
                raise RuntimeError("The selected app has no launcher activity")
            self.adb(["shell", "am", "start", "--user", user, "-W", "-n", components[0]])
        elif action == "app-close":
            self.adb(["shell", "am", "force-stop", "--user", user, package])
        elif action == "app-reset":
            if self.adb(["shell", "pm", "clear", "--user", user, package]) != "Success":
                raise RuntimeError("Android did not confirm clearing the selected app")
        elif action == "app-grant-permission":
            permission = args[0] if "." in args[0] else "android.permission." + args[0]
            self.adb(["shell", "pm", "grant", "--user", user, package, permission])
        return True

    def detect_window(self):
        snapshot = self.inspect_window()
        choices = {"back": ("Back", None)}
        # inspect_window already acquired the hierarchy. Reuse its controls
        # instead of starting a second uiautomator process for every observation.
        for node in snapshot.get("controls", []):
            bounds = node.get("bounds")
            if node.get("enabled") is False or not bounds or len(bounds) != 4:
                continue
            words = {node.get("text", "").strip().casefold(), node.get("content_desc", "").strip().casefold()}
            rid = node.get("resource_id", "")
            action = None
            if rid == "android:id/button1" or words & {"allow", "ok", "continue", "اجازه دادن", "تأیید", "موافقم"}:
                action = "allow"
            elif rid == "android:id/button2" or words & {"cancel", "لغو"}:
                action = "cancel"
            elif words & {"close", "close ad", "dismiss", "×", "✕", "بستن", "بستن تبلیغ"}:
                action = "close"
            elif words & {"connect", "اتصال"}:
                action = "connect"
            if action and action not in choices:
                left, top, right, bottom = bounds
                choices[action] = (node.get("text") or node.get("content_desc") or action,
                                   ((left + right) // 2, (top + bottom) // 2))
        token = secrets.token_urlsafe(24)
        self.window_offer = token, choices
        package = snapshot["package"]
        kind = "vpn" if package == "com.android.vpndialogs" else "window"
        return {**snapshot, "kind": kind, "observation": token,
                "actions": [{"name": name, "label": label} for name, (label, _) in choices.items()]}

    def handle_window(self, observation, action):
        if isinstance(observation, str) and observation.startswith(NATIVE_NAV_PREFIX):
            return self.handle_native_navigation(observation, action)
        if not self.window_offer or observation != self.window_offer[0]:
            return {"handled": False, "action": action, "reason": "Observe the window before choosing an action"}
        choices = self.window_offer[1]
        if action not in choices:
            return {"handled": False, "action": action, "reason": "The selected control was not found"}
        point = choices[action][1]
        if point is None:
            self.adb(["shell", "input", "keyevent", "KEYCODE_BACK"])
        else:
            self.official_tap(*point)
        return {"handled": True, "action": action, "reason": "Input sent"}

    def qa_cycle(self, options):
        """Legacy one-call workflow with administrator-provided timing and cleanup."""
        if not isinstance(options, dict):
            raise ValueError("Cycle options must be an object")
        settings = {"timeout": 0, "poll_interval": 1, "initial_wait": 0, "final_wait": 0,
                    "close_app": True, "clear_data": True, "tap_center": True, **options}
        for key in ("timeout", "poll_interval", "initial_wait", "final_wait"):
            value = settings[key]
            if type(value) not in (int, float) or value < 0:
                raise ValueError(f"{key} must be a non-negative number")
        for key in ("close_app", "clear_data", "tap_center"):
            if type(settings[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        self.app_test_action("app-open", [])
        if self.stopping.wait(settings["initial_wait"]):
            raise RuntimeError("Device script is stopping")
        deadline = time.monotonic() + settings["timeout"] if settings["timeout"] else None
        delivered = set()
        result = {"connected": False, "retried": False, "detail": "No connected Android VPN route found"}
        while not self.stopping.is_set():
            result = self.legacy_qa_action("qa-connect-check-v3", [])
            if result["connected"]:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            window = self.detect_window()
            names = {item["name"] for item in window["actions"]}
            action = ("allow" if window["kind"] == "vpn" and "allow" in names else
                      "close" if "close" in names else "connect" if "connect" in names else None)
            key = (window.get("component"), action, json.dumps(window.get("controls", []), sort_keys=True))
            if action and key not in delivered:
                self.handle_window(window["observation"], action)
                if action == "allow":
                    delivered = {item for item in delivered if item[1] not in {"connect", "center"}}
                delivered.add(key)
            elif not action and settings["tap_center"]:
                # Older Flutter builds expose status text without a named power button.
                words = {control.get("text", "").strip().casefold() for control in window.get("controls", [])}
                center_key = (window.get("component"), "center")
                if words & {"disconnected", "not connected", "قطع اتصال", "متصل نیست"} and center_key not in delivered:
                    self.legacy_qa_action("qa-center", [])
                    delivered.add(center_key)
            if self.stopping.wait(settings["poll_interval"]):
                raise RuntimeError("Device script is stopping")
        if self.stopping.is_set():
            raise RuntimeError("Device script is stopping")
        if self.stopping.wait(settings["final_wait"]):
            raise RuntimeError("Device script is stopping")
        if settings["close_app"]:
            self.app_test_action("app-close", [])
        if settings["clear_data"]:
            self.app_test_action("app-reset", [])
        return result

    def legacy_qa_action(self, action, args):
        """Compatibility aliases; scripts control sequence, timing and target selection."""
        if action == "qa-cycle-v4":
            if len(args) > 1:
                raise ValueError("Supply one optional cycle settings object")
            return self.qa_cycle(args[0] if args else {})
        if args:
            raise ValueError("This compatibility method accepts no arguments")
        if action == "qa-open":
            self.app_test_action("app-open", [])
            return "opened"
        if action in {"qa-dismiss", "qa-dismiss-v2"}:
            window = self.detect_window()
            names = {item["name"] for item in window["actions"]}
            if "close" in names:
                self.handle_window(window["observation"], "close")
            return "dismissed-v2" if action == "qa-dismiss-v2" else "dismissed"
        if action in {"qa-center", "qa-center-v2"}:
            window = self.detect_window()
            if any(item["name"] == "connect" for item in window["actions"]):
                result = self.handle_window(window["observation"], "connect")
            else:
                sizes = re.findall(r"(?m)^(?:Physical|Override) size: (\d+)x(\d+)\s*$",
                                   self.adb(["shell", "wm", "size"]))
                if not sizes:
                    raise RuntimeError("Android did not report its screen dimensions")
                width, height = map(int, sizes[-1])
                self.official_tap(width // 2, height // 2)
                result = {"handled": True}
            if action == "qa-center-v2":
                # Approval is a script-invoked operation, without caller-package gating.
                confirm = self.detect_window()
                if confirm["kind"] == "vpn":
                    self.handle_window(confirm["observation"], "allow")
            return "tapped"
        if action == "qa-connect-check-v3":
            package = self.verify_test_app()
            packages = self.adb(["shell", "pm", "list", "packages", "-U", "--user", self.android_user(), package])
            ids = re.findall(r"(?m)^package:" + re.escape(package) + r" uid:(\d+)$", packages)
            output = self.adb(["shell", "dumpsys", "connectivity"])
            network = vpn_record(output, ids[0]) if len(ids) == 1 else None
            return {"connected": network is not None, "retried": False,
                    "detail": "Connected Android VPN route for " + package if network else
                              "No connected Android VPN route found for " + package}
        if action in {"qa-finish", "qa-finish-v3"}:
            self.app_test_action("app-close", [])
            self.app_test_action("app-reset", [])
            return "finished"
        raise ValueError("Unknown compatibility action")

    def dispatch(self, action, args):
        if not isinstance(args, list):
            raise ValueError("Expected an argument list")
        if action == "regenerate" and 2 <= len(args) <= 5:
            return self.regenerate(*args)
        if action in {"ui-preferences-read", "ui-preferences-restore", "ui-preferences-baseline"} and not args:
            from qa_ui_preferences import _helper, read_baseline, restore
            seconds = self.config.get("command_timeout", 0)
            deadline = time.monotonic() + seconds if seconds else None
            with self.owned_manager(deadline=deadline) as manager:
                with self.display_raw_docker(manager, deadline):
                    if action == "ui-preferences-read":
                        return {"ok": True, "snapshot": _helper(self, manager, "read", None, deadline)}
                    if action == "ui-preferences-baseline":
                        return {"ok": True, **read_baseline(self, manager, deadline=deadline)}
                    return restore(self, manager, deadline=deadline)
        if action == "input-status" and len(args) <= 1:
            return self.input_status(*args)
        if action == "clear-cache" and 1 <= len(args) <= 3:
            return self.clear_cache(*args)
        if action == "set-frame-rate" and 1 <= len(args) <= 2:
            return self.set_frame_rate(*args)
        if action in {"apply-resource-defaults", "apply_resource_defaults"} and len(args) <= 1:
            from qa_resource_runtime import apply_for_broker
            return apply_for_broker(self, *args)
        if action == "settings" and not args:
            return {key: self.config.get(key) for key in (
                "repeat_count", "initial_delay", "interval", "timeout", "command_timeout", "verify_apk", "package", "android_user")}
        if action == "configure-run" and len(args) == 1:
            if not self.configure_callback:
                raise RuntimeError("Run configuration is unavailable")
            previous_user = self.android_user()
            previous_verify = self.config.get("verify_apk", False)
            result = self.configure_callback(args[0])
            self.resolved_app = None
            if self.android_user() != previous_user or self.config.get("verify_apk", False) != previous_verify:
                self.app_cache.clear()
            return result
        if action == "inspect-window" and len(args) <= 4:
            return self.inspect_window(*args)
        if action in APP_ACTIONS:
            shared_policy("app_action", action=action, args=args)
            return self.app_test_action(action, args)
        if action == "detect-window" and not args:
            return self.detect_window()
        if action == "handle-window" and len(args) == 2:
            return self.handle_window(*args)
        if action in QA_ACTIONS:
            return self.legacy_qa_action(action, args)
        if action == "tap" and len(args) == 2:
            return self.official_tap(int(args[0]), int(args[1]))
        if action == "adb-command" and len(args) == 2:
            seconds = args[1]
            if seconds is not None and (type(seconds) is not int or seconds < 0):
                raise ValueError("Command timeout must be a non-negative integer")
            return self.adb(safe_adb(args[0]), timeout=seconds)
        if action == "open-url" and len(args) == 1:
            url = check_url(args[0])
            cmd = ["am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", url, "-p", CHROME]
            result = self.adb(["shell", shlex.join(cmd)])
            if "Error:" in result or "Exception" in result or "com.android.chrome" not in result:
                raise RuntimeError("Chrome did not accept the Android VIEW intent")
            self.url = url
            self.log("action", f"Opened Chrome: {urlsplit(url).hostname}{urlsplit(url).path}")
            return result
        if action == "page-json" and len(args) <= 1:
            if not self.url:
                raise ValueError("Open a URL before reading its page")
            seconds = args[0] if args else 35
            if type(seconds) is not int or seconds < 0:
                raise ValueError("Page timeout must be a non-negative integer")
            with self.forward_lock:
                port = integer(int(self.adb(["forward", "tcp:0", "localabstract:chrome_devtools_remote"])), 1, 65535)
                self.port = port
            try:
                deadline = time.monotonic() + seconds if seconds else None
                while deadline is None or time.monotonic() < deadline:
                    try:
                        result = browser_json(port, self.url)
                        self.adb(["get-state"])
                        self.log("action", "Read JSON from the rendered Chrome page")
                        return result
                    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                        if self.stopping.wait(0.5):
                            raise RuntimeError("Device script is stopping")
                raise RuntimeError("Timed out waiting for rendered JSON at the opened URL")
            finally:
                self.cleanup()
        if action == "close-chrome" and not args:
            self.adb(["shell", "am", "force-stop", CHROME])
            # A successful ADB shell is required even when pidof reports no process.
            pid = self.adb(["shell", "pidof com.android.chrome || echo stopped"])
            if pid != "stopped":
                raise RuntimeError("Chrome was still running after force-stop")
            self.log("action", "Closed Chrome; confirmed its browser process stopped")
            return "stopped"
        if action == "adb" and args in (["exec-out", "uiautomator", "dump", "/dev/tty"],
                                        ["shell", "uiautomator", "dump", "/dev/tty"]):
            action, args = "ui-dump", []
        if action == "ui-dump" and not args:
            xml = self.ui_hierarchy(None)
            self.log("action", "Read the Android UI hierarchy")
            return xml
        if action == "adb":
            result = self.adb(safe_adb(args))
            if args[:2] == ["shell", "input"] and len(args) > 2:
                detail = " ".join(args[3:]) if len(args) > 2 and args[2] in {"tap", "swipe"} else ""
                self.log("action", f"Android input {args[2]} {detail}".rstrip())
            return result
        raise ValueError("Unknown device action")


class DeviceServer(socketserver.UnixStreamServer):
    """One device command at a time without blocking supervisor shutdown."""

    def __init__(self, address, handler):
        self.pending = queue.Queue(maxsize=2)
        super().__init__(address, handler)
        self.active = threading.Thread(target=self.handle_queue, daemon=True)

    def handle_queue(self):
        while not self.broker.stopping.is_set():
            try:
                connection, address = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                super().process_request(connection, address)
            except OSError:
                self.shutdown_request(connection)

    def process_request(self, connection, address):
        try:
            self.pending.put_nowait((connection, address))
        except queue.Full:
            try:
                connection.sendall(b'{"ok":false,"error":"Device command queue is full"}\n')
            except OSError:
                pass
            self.shutdown_request(connection)

    def server_close(self):
        super().server_close()
        while not self.pending.empty():
            self.shutdown_request(self.pending.get_nowait()[0])


class Requests(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        with self.request.makefile("rb") as stream:
            try:
                packet = stream.readline()
                if not packet.endswith(b"\n"):
                    raise ValueError("Incomplete device request")
                message = json.loads(packet)
                value = self.server.broker.dispatch(message["action"], message["args"])
                response = {"ok": True, "value": value}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)[:400]}
            try:
                self.request.sendall(json.dumps(response).encode() + b"\n")
            except OSError:
                pass


def jail_layout(root, config):
    root.chmod(0o755)
    for item in ("usr", "usr/bin", "usr/lib", "dev", "proc", "sys", "tmp", "run", "etc", "work", "work/bin"):
        (root / item).mkdir(mode=0o755, exist_ok=True)
        (root / item).chmod(0o755)
    for name in ("bin", "lib"):
        (root / name).symlink_to("usr/" + name)
    if Path("/lib64").exists():
        (root / "lib64").mkdir()
    sdk = root / "work" / "xenoid_device.py"
    shutil.copyfile(HERE / "script_runtime.py", sdk)
    sdk.chmod(0o555)
    file = root / "work" / ("script.py" if config["language"] == "python" else "script.sh")
    file.write_text(config["source"])
    file.chmod(0o444)
    return file.name


def sandbox_command(number, manager, config, root, filename, name, iteration=None):
    properties = [
        f"RootDirectory={root}", "WorkingDirectory=/tmp", "DynamicUser=yes", "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_UNIX", "PrivateDevices=yes", "NoNewPrivileges=yes",
        "CapabilityBoundingSet=", "ProtectProc=invisible", "ProcSubset=pid", "RestrictNamespaces=yes",
        "RestrictSUIDSGID=yes", "ProtectSystem=strict", "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes", "ProtectControlGroups=yes", "LockPersonality=yes",
        "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM",
        "MemoryMax=128M", "TasksMax=24", "CPUQuota=50%", "LimitNOFILE=128",
        "LimitCORE=0", "LimitFSIZE=16777216", "UMask=0077",
        "TemporaryFileSystem=/tmp:rw,nosuid,nodev,size=16M /work/bin:rw,nosuid,nodev,exec,size=1M,mode=1777", "BindReadOnlyPaths=/usr/bin /usr/lib",
        "RuntimeMaxSec=infinity", "TimeoutStopSec=2s", "KillMode=control-group",
        f"BindsTo={unit(number)}", f"After={unit(number)}",
    ]
    if Path("/lib64").exists():
        properties.append("BindReadOnlyPaths=/lib64")
    command = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--unit", name]
    for prop in properties:
        command.extend(["--property", prop])
    env = {"PATH": "/work/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8",
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "ANDROID_SERIAL": manager.adb_target,
           "XENOID_INSTANCE": f"phone-{number}", "XENOID_DEVICE_ID": str(number), "XENOID_RUN_ID": config["run_id"]}
    if iteration is not None:
        env["XENOID_ITERATION"] = str(iteration)
    for key, value in env.items():
        command.extend(["--setenv", f"{key}={value}"])
    command.extend(["/usr/bin/python3", "-s", "/work/xenoid_device.py", "--launch", config["language"], "/work/" + filename])
    return command


def drain(process, log, timeout):
    # Limit both retained and incoming lines. A script can never fill a privileged temp file.
    pending = b""
    quota = 65536
    line_count = 0
    started = time.monotonic()

    def emit(line):
        nonlocal line_count
        if line_count < 250:
            log("output", line.decode(errors="replace"))
        elif line_count == 250:
            log("warning", "Script output truncated for this iteration")
        line_count += 1

    with selectors.DefaultSelector() as mux:
        mux.register(process.stdout, selectors.EVENT_READ)
        while True:
            # Read the live setting each pass: scripts may extend, shorten or
            # remove this round's deadline while they are already running.
            seconds = timeout() if callable(timeout) else timeout
            if seconds and time.monotonic() - started >= seconds:
                raise TimeoutError("Script execution timed out")
            if not mux.select(timeout=1):
                continue
            chunk = os.read(process.stdout.fileno(), 16384)
            if not chunk:
                if pending:
                    emit(pending)
                return process.wait(timeout=5)
            if quota <= 0:
                continue
            pending += chunk[:quota]
            quota -= len(chunk)
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                emit(line)
            if quota <= 0:
                emit(pending)
                pending = b""
                log("warning", "Maximum script output bytes reached for this iteration")
    raise TimeoutError("Script execution timed out")


def finish_device_requests(server, thread, broker):
    """Wait for the stop-aware active request before releasing owned resources."""
    broker.stopping.set()
    server.shutdown()
    thread.join(timeout=1)
    if server.active is not None:
        server.active.join(timeout=20)
        if server.active.is_alive():
            raise RuntimeError("Device request cleanup did not finish before shutdown")
    broker.cleanup(drain=True)


def run_iteration(folder, number, config, manager, iteration, app_cache=None):
    run = config["run_id"]

    def log(level, message):
        with db(folder) as conn:
            record(conn, run, iteration, level, message)

    broker = Broker(manager, log, verify=lambda: trusted_device(number, config), config=config, app_cache=app_cache)
    broker.regeneration_receipts = RegenerationReceipts(folder, run, iteration)
    if config.get("identity_reservations"):
        from identity_reservations import publish_observation
        with manager.instance_operation_lock(timeout_seconds=config.get("command_timeout") or None):
            if any((manager.context.state_root / name).exists() for name in (
                    "device-regenerate-v3.json", "device-regenerate-v2.json", "device-regenerate.json", "convergence-v1.json")):
                raise RegenerationPending("identity_observation_native_pending")
            publish_observation(folder, config, manager)
    with tempfile.TemporaryDirectory(prefix=f"xp-{number}-", dir="/run") as temp:
        root = Path(temp) / "root"
        root.mkdir()
        filename = jail_layout(root, config)
        name = f"xenoid-script-sandbox-{number}-{os.urandom(5).hex()}.service"
        def configure(changes):
            if not isinstance(changes, dict):
                raise ValueError("Run settings must be an object")
            allowed = {"repeat_count", "initial_delay", "interval", "timeout", "command_timeout", "verify_apk", "package", "android_user"}
            if not set(changes) <= allowed:
                raise ValueError("Unknown run settings: " + ", ".join(sorted(set(changes) - allowed)))
            candidate = validate({**config, **changes})
            config.update({key: candidate.get(key) for key in changes})
            with db(folder) as conn:
                conn.execute("UPDATE runs SET config=?,updated=? WHERE id=?",
                             (json.dumps(config, sort_keys=True), now(), run))
            return {key: config.get(key) for key in allowed}

        broker.configure_callback = configure
        address = str(root / "work/device.sock")
        with DeviceServer(address, Requests) as server:
            server.broker = broker
            server.active.start()
            os.chmod(address, 0o666)
            # Only root can traverse the containing /run path from the host. The sandbox sees /work.
            # Host directory itself can be 0700; the DynamicUser needs search access to its chroot root.
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            try:
                command = sandbox_command(number, manager, config, root, filename, name, iteration=iteration)
                with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT) as process:
                    try:
                        return drain(process, log, lambda: config["timeout"])
                    finally:
                        broker.stopping.set()
                        if process.poll() is None:
                            subprocess.run(["systemctl", "stop", name], stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, timeout=8)
                            process.kill()
            finally:
                finish_device_requests(server, thread, broker)


class Shutdown(BaseException):
    pass


def recovery_current(folder, config):
    """An acknowledged stop/replacement must never resume an old decision."""
    with db(folder) as conn:
        row = conn.execute("SELECT id,state,config FROM runs WHERE current=1").fetchone()
    if (row is None or row["id"] != config["run_id"] or row["state"] in TERMINAL
            or json.loads(row["config"]) != config):
        raise Shutdown


def regeneration_replay_config(folder, run, binding, receipt):
    """Recover only the recorded run's metadata; its user source is never executed."""
    with db(folder) as conn:
        row = conn.execute("SELECT id,config FROM runs WHERE id=?", (run,)).fetchone()
    if row is None:
        raise RegenerationPending("regeneration_original_run_missing")
    try:
        original = json.loads(row["config"])
        if (not isinstance(original, dict) or row["id"] != run or original.get("run_id") != run
                or original.get("instance_id") != binding.get("instance_id")
                or original.get("resource_tag") != binding.get("resource_tag")
                or original.get("expected_container_id") != binding.get("container_id")):
            raise ValueError("Original run binding differs from the receipt")
        source = shared_policy("source", source=original.get("source"))
        reserved = original.get("identity_reservations", False)
        if type(reserved) is not bool:
            raise ValueError("Invalid original reservation setting")
        prior = receipt.get("identity_reservation_context")
        if prior is not None:
            from identity_reservations import digest
            expected = {"scenario_sha256": hashlib.sha256(source).hexdigest(),
                        "profile_plan_sha256": digest(receipt["profile_plan"]) if receipt.get("profile_plan") else None}
            if not reserved or prior != expected:
                raise ValueError("Original run context differs from the receipt")
    except (KeyError, TypeError, ValueError, OverflowError, UnicodeError):
        raise RegenerationPending("regeneration_original_config_invalid") from None
    return {"run_id": run, "instance_id": binding["instance_id"], "resource_tag": binding["resource_tag"],
            "expected_container_id": binding["container_id"], "source": original["source"],
            "identity_reservations": reserved}


def resume_regeneration_receipts(folder, number, config, iteration, app_cache):
    """Finish recorded native transactions before any new user app actions."""
    store = RegenerationReceipts(folder, config["run_id"], iteration)
    recovered_current = False
    recovery_current(folder, config)
    manager = trusted_device(number, config)
    for run, index, key, receipt in store.pending():
        binding = receipt.get("binding") or {}
        if (binding.get("instance_id") != manager.context.instance_id
                or binding.get("resource_tag") != manager.context.resource_tag):
            continue  # Never adopt a deleted/replaced immutable device.
        pinned = regeneration_replay_config(folder, run, binding, receipt)
        def log(level, message):
            with db(folder) as conn:
                record(conn, config["run_id"], iteration, level, message)
        log("warning", "Resuming a recorded official regeneration before the next app iteration")
        broker = Broker(manager, log, verify=lambda: trusted_device(number, pinned), config=pinned, app_cache=app_cache)
        broker.regeneration_receipts = RegenerationReceipts(folder, run, index)
        timeout = receipt.get("timeout", 0)
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            recovery_current(folder, config)
            if deadline is not None and time.monotonic() >= deadline:
                raise RegenerationPending("regeneration_timeout")
            try:
                options = {} if deadline is None else {"_deadline": deadline}
                if receipt.get("post_google") is not None:
                    options["post_google"] = receipt["post_google"]
                if receipt.get("post_preferences") is not None:
                    options["post_preferences"] = receipt["post_preferences"]
                broker.regenerate(timeout, key, receipt.get("post_cache"), **options)
                break
            except RegenerationPending as error:
                recovery_current(folder, config)
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise RegenerationPending("regeneration_timeout") from None
                delay = config["interval"] if remaining is None else min(config["interval"], remaining)
                update(folder, config["run_id"], "waiting", iteration, str(error), "warning",
                       resume_at=time.time() + delay)
                # SIGTERM/SIGINT interrupt this wait through Shutdown. Zero is
                # the administrator's explicit immediate-retry setting.
                time.sleep(delay)
        recovered_current |= run == config["run_id"]
    return recovered_current


def supervisor(folder, number, graphics_gate=None):
    def on_signal(_signo, _frame):
        raise Shutdown
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    prepare(folder)
    with db(folder) as conn:
        row = conn.execute("SELECT * FROM runs WHERE current=1").fetchone()
    if row is None or row["state"] in TERMINAL:
        return
    config = json.loads(row["config"])
    run, index = config["run_id"], row["iteration"]
    app_cache = {}
    try:
        recovered = resume_regeneration_receipts(folder, number, config, index, app_cache)
        if row["state"] == "running":
            # A power loss leaves it unknown whether a UI action finished. Never silently execute it twice.
            if not recovered or config["repeat_count"] and index >= config["repeat_count"]:
                raise RuntimeError("Previous iteration was interrupted; start a new run to retry")
            due = time.time() + config["interval"]
            update(folder, run, "waiting", index, "Recorded regeneration recovered; interrupted script attempt will retry",
                   "error", resume_at=due)
            row = {"state": "waiting", "resume_at": due}
        due = row["resume_at"]
        last_wait = None
        reconnect_after = 0
        while True:
            if due > time.time():
                time.sleep(max(0, due - time.time()))
            resume_regeneration_receipts(folder, number, config, index, app_cache)
            manager = trusted_device(number, config)
            if graphics_gate is not None:
                from qa_graphics import worker_boundary
                graphics = worker_boundary(number, config, manager, graphics_gate)
                if graphics.get("ok") is not True or graphics.get("deferred"):
                    reason = graphics.get("reason") or graphics.get("code") or "graphics_boundary_pending"
                    if last_wait != reason:
                        update(folder, run, "waiting", index, "Waiting for reported renderer: " + reason, "warning")
                    last_wait = reason
                    due = time.time() + 8
                    continue
            try:
                probe = Broker(manager, None, config=config, app_cache=app_cache)
                boot = probe.adb(["shell", "getprop", "sys.boot_completed"], timeout=6)
                wait_reason = None if boot == "1" else f"Android sys.boot_completed={boot!r}"
            except TransportUnavailable as error:
                wait_reason = str(error)
                if time.monotonic() >= reconnect_after:
                    reconnect_after = time.monotonic() + 30
                    try:
                        # The native connect verb targets this lease only, after Docker ownership validation.
                        probe.adb(["connect"], timeout=5)
                    except RuntimeIdentityError:
                        raise
                    except RuntimeError as reconnect_error:
                        wait_reason = str(reconnect_error)
            except RuntimeIdentityError:
                raise
            except RuntimeError as error:
                wait_reason = str(error)
            if wait_reason:
                if last_wait != wait_reason:
                    update(folder, run, "waiting", index, "Waiting for device: " + wait_reason, "warning")
                last_wait = wait_reason
                due = time.time() + 8
                continue
            last_wait = None
            index += 1
            update(folder, run, "running", index, f"Iteration {index} started")
            rc = run_iteration(folder, number, config, manager, index, app_cache=app_cache)
            if rc == 75:
                # EX_TEMPFAIL is an explicit request from the editable workflow.
                # Keep failed attempts visible instead of reporting them as success.
                if config["repeat_count"] and index >= config["repeat_count"]:
                    raise RuntimeError(f"Iteration {index} failed; configured attempt count exhausted")
                due = time.time() + config["interval"]
                update(folder, run, "waiting", index,
                       f"Iteration {index} failed (retry requested); next attempt in {config['interval']}s",
                       "error", resume_at=due)
                continue
            if rc:
                raise RuntimeError(f"Iteration {index} exited with status {rc} (see script output)")
            if config["repeat_count"] and index >= config["repeat_count"]:
                update(folder, run, "completed", index, f"Iteration {index} finished; run complete")
                return
            due = time.time() + config["interval"]
            update(folder, run, "waiting", index, f"Iteration {index} finished; next in {config['interval']}s", resume_at=due)
            row = {"state": "waiting", "resume_at": due}
    except Shutdown:
        # Controller stop persists desired state before SIGTERM. A host reboot resumes queued/interval waits.
        return
    except Exception as error:
        update(folder, run, "failed", index, str(error)[:800] or type(error).__name__, "error")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status", "logs", "poll", "worker", "identity-ack"))
    parser.add_argument("device", nargs="?")
    parser.add_argument("--run-id")
    parser.add_argument("--after", type=int, default=0)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("The managed helper requires the host controller account")
    if args.run_id and not RUN_ID.fullmatch(args.run_id):
        raise ValueError("Invalid run ID")
    if args.action == "poll":
        return poll_items(read_json(65536))
    number = device_number(args.device)
    folder = STATE / str(number)
    if args.action == "identity-ack":
        from identity_reservations import acknowledge
        with db(folder) as connection:
            row = connection.execute("SELECT id,config,state FROM runs WHERE current=1").fetchone()
        if row is None or row["id"] != args.run_id or row["state"] in TERMINAL:
            raise ValueError("identity_ack_current_run_changed")
        return acknowledge(folder, args.run_id, json.loads(row["config"]), read_json(65536))
    if args.action in {"start", "worker"}:
        # The pinned native CLI derives canonical relative build inputs from its checkout cwd.
        os.chdir(UPSTREAM)
        os.environ["XENOID_PROJECT"] = str(UPSTREAM)
        adopt_native_environment()
    if args.action == "worker":
        # Graphics reload must finish before a new QA worker can use the device.
        # The worker retains the shared gate; scheduled reloads never stop it.
        from qa_graphics import lifecycle_gate
        with lifecycle_gate(number, worker=True) as graphics_gate:
            supervisor(folder, number, graphics_gate=graphics_gate)
    elif args.action in {"start", "stop"}:
        STATE.mkdir(mode=0o700, exist_ok=True)
        return manage(args.action, folder, number, args.run_id)
    else:
        return snapshot(folder, number, args.run_id, args.after if args.action == "logs" else None)


if __name__ == "__main__":
    try:
        response = main()
        if response is not None:
            print(json.dumps(response, ensure_ascii=False))
    except Exception as error:
        print(str(error)[:800] or type(error).__name__, file=sys.stderr)
        sys.exit(1)

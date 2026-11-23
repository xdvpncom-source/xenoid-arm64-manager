#!/bin/bash
# Private compiler prerequisites, using actual Soong targets and generated
# Kati installation records. This never contacts an Android runtime.
set -eo pipefail
cd /work/source
export OUT_DIR=/work/source/out-bluetooth-r74
export DIST_DIR=/work/source/dist-bluetooth-r74
export ALLOW_MISSING_DEPENDENCIES=true USE_RBE=false USE_GOMA=false GOMAXPROCS=4
unset SOONG_GEN_COMPDB SOONG_LINK_COMPDB_TO SANITIZE_HOST SANITIZE_TARGET SANITIZE_TARGET_DIAG
unset LLVM_PREBUILTS_BASE LLVM_PREBUILTS_VERSION RUST_PREBUILTS_BASE RUST_PREBUILTS_VERSION
source build/envsetup.sh
lunch redroid_arm64-userdebug
TARGETS=(
  "$OUT_DIR/soong/.intermediates/external/protobuf/aprotoc/linux_glibc_x86_64/aprotoc"
  "$OUT_DIR/soong/.intermediates/external/libcxx/libc++/linux_glibc_x86_64_shared/libc++.so"
  "$OUT_DIR/soong/.intermediates/system/tools/hidl/hidl-gen/linux_glibc_x86_64/hidl-gen"
  "$OUT_DIR/soong/.intermediates/build/soong/cc/ndkstubgen/ndkstubgen/linux_glibc_x86_64_PY3/ndkstubgen"
)
prebuilts/build-tools/linux-x86/bin/ninja -f "$OUT_DIR/combined.ninja" \
  -w dupbuild=err -n -v "${TARGETS[@]}" > /work/artifacts/host-prerequisites-dryrun.txt
python3 - <<'PY'
from pathlib import Path
assert 'missing dependencies:' not in Path('/work/artifacts/host-prerequisites-dryrun.txt').read_text()
PY
build/soong/soong_ui.bash --make-mode --skip-kati --skip-soong-tests -j1 showcommands "${TARGETS[@]}"
python3 - <<'PY'
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess

root = Path('/work/source/out-bluetooth-r74')
mapping = [
    ('host/linux-x86/bin/aprotoc', 'soong/.intermediates/external/protobuf/aprotoc/linux_glibc_x86_64/aprotoc', []),
    ('host/linux-x86/lib64/libc++.so', 'soong/.intermediates/external/libcxx/libc++/linux_glibc_x86_64_shared/libc++.so', []),
    ('host/linux-x86/bin/hidl-gen', 'soong/.intermediates/system/tools/hidl/hidl-gen/linux_glibc_x86_64/hidl-gen', ['host/linux-x86/lib64/libc++.so']),
    ('host/linux-x86/bin/ndkstubgen', 'soong/.intermediates/build/soong/cc/ndkstubgen/ndkstubgen/linux_glibc_x86_64_PY3/ndkstubgen', []),
]
mk = root / 'soong/installs-redroid_arm64.mk'
text = mk.read_text()
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
receipts = []
for target, source, extra in mapping:
    src, dst = root / source, root / target
    rule = str(dst) + ': ' + ' '.join(str(root / p) for p in [source, *extra])
    stanza = rule + '\n\t@echo "Install $@"\n\trm -f $@ && cp -f -d $< $@\n'
    assert stanza in text, 'generated install rule changed: ' + target
    assert src.is_file() and not src.is_symlink() and src.stat().st_size > 0
    assert src.stat().st_mode & 0o111
    if dst.exists() or dst.is_symlink():
        assert dst.is_file() and not dst.is_symlink() and digest(dst) == digest(src)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        temporary = dst.with_name(dst.name + '.source-build-install')
        with temporary.open('xb') as output, src.open('rb') as original:
            shutil.copyfileobj(original, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(src.stat().st_mode & 0o777)
        os.rename(temporary, dst)
    assert digest(dst) == digest(src)
    receipts.append({'source': str(src), 'installed': str(dst), 'sha256': digest(dst),
                     'mode': oct(dst.stat().st_mode & 0o777)})
probe = subprocess.run([str(root / 'host/linux-x86/bin/aprotoc'), '--version'],
                       capture_output=True, text=True, timeout=15)
assert probe.returncode == 0, probe.stderr
result = {'ok': True, 'generated_install_rules_sha256': digest(mk),
          'installed': receipts, 'aprotoc_version': probe.stdout.strip()}
path = Path('/work/artifacts/host-prerequisites-installed.json')
assert not path.exists()
path.write_text(json.dumps(result, indent=2))
print(json.dumps(result))
PY

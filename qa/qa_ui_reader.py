"""Private per-Broker UI reader using Android's exact stock DumpCommand.

Only the JVM is persistent. Each request still creates the stock command,
connects/dumps/disconnects as Android implements it. No input injection or socket.
"""
import base64
import gzip
import hashlib
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import threading
import time

MAGIC = 0x58554931
MAX_XML = 8 * 1024 * 1024
MAIN_CLASS = "org.xenoid.qa.StockUiServer"
JAR_SHA256 = "3305ef8e5e18320bcb82826794c8a56dacbdb4826e971fb7d283df4ff39f7bb5"
JAR_GZIP_BASE64 = "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"

class UiReaderError(RuntimeError):
    pass


class DumpFailed(UiReaderError):
    """A bounded stock failure, with the framing still synchronized."""


def jar_bytes():
    value = gzip.decompress(base64.b64decode(JAR_GZIP_BASE64))
    if hashlib.sha256(value).hexdigest() != JAR_SHA256:
        raise UiReaderError('ui_reader_artifact_invalid')
    return value


def remaining(deadline):
    if deadline is None:
        return None
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('ui_reader_timeout')
    return value


def proc_record(pid):
    root = Path('/proc') / str(pid)
    text = (root / 'stat').read_text()
    fields = text.rsplit(')', 1)[1].split()
    status = (root / 'status').read_text()
    uid = int(re.search(r'^Uid:\s+(\d+)', status, re.M)[1])
    cmdline = (root / 'cmdline').read_bytes().split(b'\0')
    final = (root / 'stat').read_text().rsplit(')',1)[1].split()
    if final[19] != fields[19]:
        raise ProcessLookupError('process_replaced_during_observation')
    return {'pid': pid, 'state': final[0], 'ppid': int(final[1]), 'starttime': int(final[19]), 'uid': uid,
            'cmdline': cmdline,
            'pidns': os.readlink(str(root / 'ns/pid')),
            'nspid': int(re.search(r'^NSpid:\s+(.+)$', status, re.M)[1].split()[-1])}


class PipeProcess:
    """Framing/timeout/serialization; the owner supplies a fenced remote cleanup."""
    def __init__(self, argv, deadline, stopping, cleanup, prepare_cleanup=None):
        if stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        self.stopping, self.cleanup = stopping, cleanup
        self.prepare_cleanup = prepare_cleanup
        self.lock = threading.Lock()
        self.closed, self.sequence, self.identity = False, 0, None
        self.cleanup_pending = False
        self.stderr_tail = bytearray()
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
        self.pump = threading.Thread(target=self._stderr, daemon=True)
        self.pump.start()
        try:
            sequence, status, data = self._reply(deadline)
            parts = data.decode('ascii').split('\t')
            if sequence != 0 or status != 0 or len(parts) != 4 or parts[0] != 'READY':
                raise UiReaderError('ui_reader_startup_protocol')
            self.identity = dict(zip(('pid', 'uid', 'starttime'), map(int, parts[1:])))
        except BaseException as error:
            try:
                self.close(force=True)
            except Exception as cleanup_error:
                error.cleanup_error = cleanup_error
            raise

    def _stderr(self):
        try:
            while True:
                data = os.read(self.process.stderr.fileno(), 4096)
                if not data:
                    return
                self.stderr_tail.extend(data)
                del self.stderr_tail[:-8192]
        except (OSError, ValueError):
            return

    def _read(self, size, deadline, *, shutdown=False):
        value = bytearray()
        with selectors.DefaultSelector() as mux:
            mux.register(self.process.stdout, selectors.EVENT_READ)
            while len(value) < size:
                if not shutdown and self.stopping.is_set():
                    raise UiReaderError('ui_reader_stopping')
                budget = remaining(deadline)
                delay = .2 if budget is None else min(.2, budget)
                if not mux.select(delay):
                    continue
                chunk = os.read(self.process.stdout.fileno(), size-len(value))
                if not chunk:
                    raise UiReaderError('ui_reader_closed')
                value.extend(chunk)
        return bytes(value)

    def _reply(self, deadline, *, shutdown=False):
        magic, sequence, status, size = struct.unpack('>IIBI', self._read(13, deadline, shutdown=shutdown))
        if magic != MAGIC or status not in (0, 1, 2) or size > (MAX_XML if status == 0 else 128):
            raise UiReaderError('ui_reader_invalid_frame')
        return sequence, status, self._read(size, deadline, shutdown=shutdown)

    def request(self, windows, deadline):
        with self.lock:
            if self.closed:
                raise UiReaderError('ui_reader_closed')
            self.sequence += 1
            try:
                self.process.stdin.write(struct.pack('>IIB', MAGIC, self.sequence, 2 if windows else 0))
                self.process.stdin.flush()
                sequence, status, data = self._reply(deadline)
                if sequence != self.sequence:
                    raise UiReaderError('ui_reader_sequence_mismatch')
                if status == 1:
                    raise DumpFailed(data.decode('ascii'))
                if status != 0:
                    raise UiReaderError('ui_reader_protocol_rejected')
                return data
            except DumpFailed:
                raise
            except BaseException:
                self.close(force=True)
                raise

    def close(self, *, force=False):
        if self.closed:
            if self.cleanup_pending:
                self.cleanup(self.identity)
                self.cleanup_pending = False
            return
        self.closed = True
        cleanup_error = None
        try:
            if not force and self.process.poll() is None:
                try:
                    self.sequence += 1
                    self.process.stdin.write(struct.pack('>IIB', MAGIC, self.sequence, 127))
                    self.process.stdin.flush()
                    seq, status, data = self._reply(time.monotonic()+1, shutdown=True)
                    if (seq, status, data) != (self.sequence, 0, b'BYE'):
                        raise UiReaderError('ui_reader_shutdown_protocol')
                    self.process.wait(timeout=1)
                except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                    force = True
            if self.prepare_cleanup is not None:
                try:
                    self.prepare_cleanup()
                except BaseException as error:
                    cleanup_error = error
            # Close the submitting transport before sweeping its guest process:
            # a delayed adbd launch must not be submitted after that sweep.
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
            except BaseException as error:
                cleanup_error = cleanup_error or error
            try:
                self.cleanup(self.identity)
            except BaseException as error:
                self.cleanup_pending = True
                cleanup_error = error
        finally:
            self.pump.join(timeout=.5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
        if cleanup_error:
            raise UiReaderError('ui_reader_cleanup_unconfirmed') from cleanup_error


class PersistentUiReader:
    """Must be created/read while the caller owns the Native instance lock."""
    def __init__(self, manager, container_id, priority, deadline, stopping, *, defer_start=False):
        self.manager, self.container_id = manager, container_id
        self.stopping, self.priority = stopping, priority
        self.deadline = deadline
        self.pipe, self.private_identity = None, None
        self.cleanup_targets = {}
        self._cleanup_binding_verified = False
        self.private = '/data/local/tmp/xenoid-ui-reader-' + os.urandom(16).hex()
        self.claim = os.urandom(16).hex()
        self.directory = self.binding = self.root = self.pidns = None
        self.launch_body = self.host_helper = None
        self.closed = self.started = False
        if not defer_start:
            self.start()

    def start(self):
        if self.closed or self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        if self.started:
            return
        deadline, priority, stopping = self.deadline, self.priority, self.stopping
        self.binding = self._binding()
        if not self.binding['epoch']:
            raise UiReaderError('ui_reader_epoch_unreadable')
        self.root = Path('/proc')/str(self.binding['pid'])/'root'
        self.directory = self.root/self.private.lstrip('/')
        self.pidns = os.readlink('/proc/'+str(self.binding['pid'])+'/ns/pid')
        try:
            result = self._adb('id -u; id -g',deadline)
            values = result.decode('ascii').split()
            if len(values) != 2:
                raise UiReaderError('ui_reader_uid_unreadable')
            self.guest_uid = int(values[0])
            guest_gid = int(values[1])
            if stopping.is_set():
                raise UiReaderError('ui_reader_stopping')
            # Establish the owned directory before any queued shell writes.
            # Cleanup can atomically retire it even if ADB setup starts late.
            self.directory.mkdir(mode=0o700)
            info = self.directory.lstat()
            self.private_identity = (info.st_dev,info.st_ino,info.st_uid)
            os.chown(self.directory,self._host_id(self.guest_uid,'uid_map'),self._host_id(guest_gid,'gid_map'))
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                raise UiReaderError('ui_reader_private_directory_invalid')
            self.private_identity = (info.st_dev, info.st_ino, info.st_uid)
            self._verify()
            self._adb('set -eu; umask 077; [ ! -e '+shlex.quote(self.private+'/cancelled')+
                ' ]; cat > '+shlex.quote(self.private+'/server.jar')+
                '; chmod 600 '+shlex.quote(self.private+'/server.jar'),deadline,data=jar_bytes())
            self.launch_body = ('set -eu; umask 077; printf "%s %s\\n" "$$" "$(awk \'{print $22}\' /proc/$$/stat)" > '+
                shlex.quote(self.private+'/process.tmp')+'; mv '+shlex.quote(self.private+'/process.tmp')+' '+
                shlex.quote(self.private+'/process')+'; [ ! -e '+shlex.quote(self.private+'/cancelled')+' ]; exec env '+
                shlex.quote('CLASSPATH=/system/framework/uiautomator.jar:'+self.private+'/server.jar')+' '+
                (shlex.join(['nice', '-n', str(priority)])+' ' if priority is not None else '')+
                shlex.join(['app_process', '/system/bin', MAIN_CLASS, self.private]))
            self.pipe = PipeProcess(self._argv(shlex.join(['sh','-c',self.launch_body])),
                                    deadline, stopping, self._terminate, self._capture_cleanup)
            self._helper(self.pipe.identity, required=True)
            self._verify()
            self.started = True
        except BaseException:
            self.close(force=True)
            raise

    def _argv(self, command):
        executable = shutil.which('adb')
        if not executable:
            raise UiReaderError('ui_reader_adb_unavailable')
        return [executable, '-s', self.manager.adb_target, 'shell', '-T', command]

    def _adb(self, command, deadline, data=None):
        if self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        process = subprocess.Popen(self._argv(command),stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        first = True
        try:
            while True:
                if self.stopping.is_set():
                    raise UiReaderError('ui_reader_stopping')
                budget = remaining(deadline)
                try:
                    out,err = process.communicate(input=data if first else None,
                                                   timeout=.2 if budget is None else min(.2,budget))
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if process.returncode or len(out)>4096 or len(err)>8192:
                raise UiReaderError('ui_reader_setup_failed')
            return out
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            for stream in (process.stdin,process.stdout,process.stderr):stream.close()

    def _host_id(self, value, kind):
        for line in (Path('/proc')/str(self.binding['pid'])/kind).read_text().splitlines():
            inside,outside,count = map(int,line.split())
            if inside <= value < inside+count:
                return outside+value-inside
        raise UiReaderError('ui_reader_user_namespace_mapping_missing')

    def _binding(self, *, include_epoch=True):
        budget = remaining(self.deadline) if include_epoch else None
        owned, _ = self.manager._owned_container_record(timeout=5 if budget is None else min(5,budget))
        if not owned or owned.get('Id') != self.container_id or not owned.get('State', {}).get('Running'):
            raise UiReaderError('ui_reader_runtime_changed')
        pid = owned['State']['Pid']
        if include_epoch and self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        probe_deadline = time.monotonic()+2
        if self.deadline is not None:
            probe_deadline = min(probe_deadline,self.deadline)
        return {'pid':pid, 'pid_start':proc_record(pid)['starttime'], 'image':owned.get('Image'),
                'epoch':self.manager._userspace_runtime_epoch(deadline=probe_deadline) if include_epoch else None,
                'instance':self.manager.context.instance_id, 'tag':self.manager.context.resource_tag}

    def _verify(self, *, cleanup=False):
        if not cleanup and self.stopping.is_set():
            raise UiReaderError('ui_reader_stopping')
        current = self._binding(include_epoch=not cleanup)
        keys = ('pid','pid_start','image','instance','tag') if cleanup else tuple(self.binding)
        if any(current[key] != self.binding[key] for key in keys):
            raise UiReaderError('ui_reader_runtime_changed')
        if not cleanup and not current['epoch']:
            raise UiReaderError('ui_reader_epoch_unreadable')

    def _private_ok(self):
        if self.directory is None or self.private_identity is None:
            return False
        info = self.directory.lstat()
        return (stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode)==0o700 and
                (info.st_dev,info.st_ino,info.st_uid)==self.private_identity)

    def _matches(self, record, identity=None):
        if record['pidns'] != self.pidns or record['uid'] != self.private_identity[2]:
            return False
        args = record['cmdline']
        command_matches = (MAIN_CLASS.encode() in args and self.private.encode() in args or
                           self.launch_body is not None and self.launch_body.encode() in args)
        if not command_matches:
            return False
        return identity is None or (record['nspid']==identity['pid'] and
                    record['starttime']==identity['starttime'] and identity['uid']==self.guest_uid)

    def _helper(self, identity=None, *, required=False):
        if identity is None and self.directory is not None:
            record_path = self.directory/'process'
            if record_path.exists():
                info = record_path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    info.st_size > 128 or info.st_uid != self.private_identity[2]):
                    raise UiReaderError('ui_reader_pid_record_invalid')
                values = record_path.read_text().split()
                if len(values) != 2 or not all(v.isdigit() and int(v)>0 for v in values):
                    raise UiReaderError('ui_reader_pid_record_invalid')
                identity = {'pid':int(values[0]), 'starttime':int(values[1]), 'uid':self.guest_uid}
        candidates = [self.host_helper] if self.host_helper else []
        if not candidates:
            for path in Path('/proc').iterdir():
                if path.name.isdigit():candidates.append(int(path.name))
        found = []
        for pid in candidates:
            try:
                record = proc_record(pid)
                if self._matches(record, identity):found.append(record)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        if len(found) > 1:
            raise UiReaderError('ui_reader_process_ambiguous')
        if found:
            self.host_helper = found[0]['pid']
            return found[0]
        if required:
            raise UiReaderError('ui_reader_process_unverified')
        return None

    def _mark_cancelled(self):
        cancel = self.directory/'cancelled'
        try:
            fd = os.open(cancel, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            info = cancel.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.private_identity[2]:
                raise UiReaderError('ui_reader_cancel_marker_invalid')
        else:
            try:
                os.fchown(fd,self.private_identity[2],self.directory.stat().st_gid)
            finally:
                os.close(fd)

    def _capture_cleanup(self, identity=None):
        if not getattr(self, '_cleanup_binding_verified', False):
            self._verify(cleanup=True)
            self._cleanup_binding_verified = True
        if not self._private_ok():
            raise UiReaderError('ui_reader_private_directory_changed')
        self._mark_cancelled()
        helper = self._helper(identity)
        if helper is None:
            return
        self.cleanup_targets[helper['pid']] = dict(helper, kind='helper')
        # Freeze only the authenticated helper before enumerating descendants.
        # It cannot fork another compiler between this snapshot and pipe close.
        current = self._live_cleanup_target(self.cleanup_targets[helper['pid']])
        if current is not None:
            os.kill(current['pid'],signal.SIGSTOP)
            deadline = time.monotonic()+1
            while True:
                current = self._live_cleanup_target(self.cleanup_targets[helper['pid']])
                if current is None or current.get('state') in ('T','t'):
                    break
                if time.monotonic() >= deadline:
                    os.kill(current['pid'],signal.SIGCONT)
                    raise UiReaderError('ui_reader_freeze_unconfirmed')
                time.sleep(.01)
        processes = {}
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            try:
                record = proc_record(int(path.name))
                if record['pidns'] == self.pidns:
                    processes[record['pid']] = record
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        for pid, record in processes.items():
            if not record['cmdline'] or not Path(os.fsdecode(record['cmdline'][0])).name.startswith('dex2oat'):
                continue
            parent, visited = record['ppid'], set()
            while parent in processes and parent not in visited and parent != helper['pid']:
                visited.add(parent)
                parent = processes[parent]['ppid']
            if parent == helper['pid'] and record['uid'] == self.private_identity[2]:
                self.cleanup_targets[pid] = dict(record, kind='compiler')

    def _live_cleanup_target(self, expected):
        try:
            current = proc_record(expected['pid'])
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        if current['starttime'] != expected['starttime']:
            return None  # The original process is gone; never signal its reused PID.
        if current.get('state') in ('Z', 'X'):
            return None  # A reaping zombie has no running JVM/compiler to signal.
        if current['uid'] != expected['uid'] or current['pidns'] != expected['pidns']:
            raise UiReaderError('ui_reader_cleanup_identity_changed')
        if expected['kind'] == 'helper':
            if not self._matches(current):
                raise UiReaderError('ui_reader_helper_exec_changed')
        elif current['cmdline'] != expected['cmdline']:
            raise UiReaderError('ui_reader_compiler_exec_changed')
        return current

    def _terminate(self, identity):
        # Capture descendants before the ADB transport closes, and again here.
        # A captured compiler remains owned by PID/starttime even if reparented.
        success = False
        try:
            self._capture_cleanup(identity)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for expected in sorted(self.cleanup_targets.values(),key=lambda r:r['kind']=='helper'):
                    current = self._live_cleanup_target(expected)
                    if current is not None:
                        os.kill(current['pid'],signal.SIGKILL if current.get('state') in ('T','t') else sig)
                end = time.monotonic()+1
                while time.monotonic()<end:
                    if not any(self._live_cleanup_target(r) is not None for r in self.cleanup_targets.values()):
                        success = True
                        return
                    time.sleep(.05)
            raise UiReaderError('ui_reader_process_survived')
        finally:
            if not success:
                # A failed cleanup must not leave an owned process suspended.
                for expected in self.cleanup_targets.values():
                    current = self._live_cleanup_target(expected)
                    if current is not None and current.get('state') in ('T','t'):
                        os.kill(current['pid'],signal.SIGCONT)

    def read(self, manager, windows, deadline):
        if self.closed:
            raise UiReaderError('ui_reader_closed')
        self.manager = manager
        self.deadline = deadline
        try:
            self._verify()
            self._helper(self.pipe.identity, required=True)
            result = self.pipe.request(windows, deadline)
            self._verify()
            self._helper(self.pipe.identity, required=True)
            return result.decode('utf-8')
        except DumpFailed:
            # Stock DumpCommand completed/disconnected but produced no valid XML.
            # Keep this synchronized JVM for the caller's next explicit read.
            raise
        except BaseException:
            self.close(force=True)
            raise

    def close(self, *, force=False):
        self.closed = True
        if self.private_identity is None and (self.pipe is None or
                self.pipe.closed and not self.pipe.cleanup_pending):
            return
        self._cleanup_binding_verified = False
        error = None
        try:
            if self.pipe is not None:
                self.pipe.close(force=force)
            elif self.private_identity is not None:
                self._terminate(None)
        except BaseException as failure:
            error = failure
        try:
            if self.private_identity is not None:
                # PipeProcess can already be closed after an earlier failed
                # cleanup. Always retry the fenced guest sweep, not just files.
                self._terminate(self.pipe.identity if self.pipe is not None else None)
                if not self._cleanup_binding_verified:
                    self._verify(cleanup=True)
                if not self._private_ok() or self._helper() is not None:
                    raise UiReaderError('ui_reader_cleanup_unconfirmed')
                entries = list(self.directory.iterdir())
                for path in entries:
                    info = path.lstat()
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or
                        info.st_uid!=self.private_identity[2] or
                        path.name not in ('server.jar','owner','process','process.tmp','cancelled') and not re.fullmatch(r'ui-[A-Za-z0-9_-]+\.xml',path.name)):
                        raise UiReaderError('ui_reader_unknown_cleanup_entry')
                # Remove the launch path atomically before removing the cancel
                # marker. A delayed shell then fails its PID-record redirection
                # under set -e and cannot exec a new compiler/helper afterward.
                retired = self.directory.with_name(self.directory.name+'.retired-'+os.urandom(8).hex())
                if retired.exists():
                    raise UiReaderError('ui_reader_retired_path_exists')
                names = [path.name for path in entries]
                self.directory.rename(retired)
                self.directory = retired
                for name in names:
                    (self.directory/name).unlink()
                self.directory.rmdir()
                self.private_identity = None
                error = None  # A retry within this close proved cleanup complete.
        except BaseException as failure:
            error = error or failure
        if error:
            raise UiReaderError('ui_reader_cleanup_unconfirmed') from error

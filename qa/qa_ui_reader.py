"""Private per-Broker UI reader using Android's exact stock DumpCommand.

Only the JVM is persistent. Each request still creates the stock command,
connects/dumps/disconnects as Android implements it. No input injection or socket.
The separate rare navigation-proof request reads only SystemUI Back through a
fresh cache-bypassing connection in that same serialized JVM.
"""
import base64
import gzip
import hashlib
import json
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
MAX_NAVIGATION = 16 * 1024
MAIN_CLASS = "org.xenoid.qa.StockUiServer"
JAR_SHA256 = 'a16ec9acfc003aad1f5fa4d5103dc7932aec5cc48ef3231c5337444328bac2ca'
JAR_GZIP_BASE64 = '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'

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

    def _reply(self, deadline, *, shutdown=False, maximum=MAX_XML):
        magic, sequence, status, size = struct.unpack('>IIBI', self._read(13, deadline, shutdown=shutdown))
        if magic != MAGIC or status not in (0, 1, 2) or size > (maximum if status == 0 else 128):
            raise UiReaderError('ui_reader_invalid_frame')
        return sequence, status, self._read(size, deadline, shutdown=shutdown)

    def request(self, windows, deadline):
        return self._request(2 if windows else 0, b'', deadline, MAX_XML)

    def navigation(self, display_id, deadline):
        if type(display_id) is not int or not 0 <= display_id <= 2147483647:
            raise ValueError('ui_reader_navigation_display_invalid')
        return self._request(4, struct.pack('>i', display_id), deadline, MAX_NAVIGATION)

    def _request(self, flags, payload, deadline, maximum):
        with self.lock:
            if self.closed:
                raise UiReaderError('ui_reader_closed')
            self.sequence += 1
            try:
                remaining(deadline)
                self.process.stdin.write(struct.pack('>IIB', MAGIC, self.sequence, flags) + payload)
                self.process.stdin.flush()
                sequence, status, data = self._reply(deadline, maximum=maximum)
                if sequence != self.sequence:
                    raise UiReaderError('ui_reader_sequence_mismatch')
                if status == 1:
                    raise DumpFailed(data.decode('ascii'))
                if status != 0:
                    raise UiReaderError('ui_reader_protocol_rejected')
                return data
            except DumpFailed:
                raise
            except BaseException as error:
                try:
                    self.close(force=True)
                except BaseException as cleanup_error:
                    error.cleanup_error = cleanup_error
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
        keys = ('pid','pid_start','image','instance','tag')
        if any(current[key] != self.binding[key] for key in keys):
            raise UiReaderError('ui_reader_runtime_changed')
        if not cleanup:
            if not current['epoch']:
                raise UiReaderError('ui_reader_epoch_unreadable')
            if current['epoch'] != self.binding['epoch']:
                raise UiReaderError('ui_reader_runtime_changed')

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

    def navigation(self, manager, display_id, deadline):
        """Rare fresh Back proof; caller already holds the instance lock.

        Never starts a helper or acquires another instance/operation lock. A
        framing/timeout failure uses the same fenced close while that owner is
        held; no Broker.close_ui_reader recursion is involved.
        """
        if self.closed or not self.started or self.pipe is None or self.pipe.closed:
            raise UiReaderError('ui_reader_navigation_unavailable')
        self.manager, self.deadline = manager, deadline
        try:
            self._verify()
            self._helper(self.pipe.identity, required=True)
            data = self.pipe.navigation(display_id, deadline)
            result = json.loads(data.decode('ascii'))
            if not isinstance(result, dict) or result.get('schema') != 'xenoid.nav-node-proof/v1':
                raise UiReaderError('ui_reader_navigation_invalid')
            self._verify()
            self._helper(self.pipe.identity, required=True)
            remaining(deadline)
            return result
        except DumpFailed:
            raise
        except BaseException as error:
            try:
                self.close(force=True)
            except BaseException as cleanup_error:
                error.cleanup_error = cleanup_error
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
                if self.pipe is not None:
                    # The outer fenced retry can complete a sweep that failed
                    # in PipeProcess.close. Retire its pending flag only after
                    # both the owned guest processes and directory are gone.
                    self.pipe.cleanup_pending = False
                error = None  # A retry within this close proved cleanup complete.
        except BaseException as failure:
            error = error or failure
        if error:
            raise UiReaderError('ui_reader_cleanup_unconfirmed') from error

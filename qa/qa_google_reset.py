"""Durable, script-requested Google data reset and microG registration switch.

No input, preference editing, identity generation or network-success claim.
The caller owns the native instance lock and supplies the fenced Android exec.
"""
import math
import re
import time

PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
REGISTRATION_COMPONENT = 'com.google.android.gms/org.microg.gms.provision.ProvisionService'


def normalize_plan(plan):
    if plan is None:
        return None
    fields = {'clear_packages', 'user', 'delay_seconds', 'timeout', 'poll_seconds'}
    if not isinstance(plan, dict) or set(plan) - fields:
        raise ValueError('Post-Google plan has unknown fields')
    packages = plan.get('clear_packages')
    if not isinstance(packages, list) or not packages or any(
            not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in packages):
        raise ValueError('Post-Google clear_packages must contain Android packages')
    user = plan.get('user', 'current')
    if user != 'current' and (type(user) is not int or user < 0):
        raise ValueError('Post-Google user must be current or a nonnegative user id')
    delay, poll, timeout = plan.get('delay_seconds', 2), plan.get('poll_seconds', 1), plan.get('timeout', 0)
    for value, name, allow_zero in ((delay, 'delay_seconds', True), (poll, 'poll_seconds', False)):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or value < 0 or not allow_zero and value == 0):
            raise ValueError('Post-Google ' + name + ' is invalid')
    if type(timeout) is not int or timeout < 0:
        raise ValueError('Post-Google timeout must be nonnegative; zero is unlimited')
    return {'clear_packages': list(dict.fromkeys(packages)), 'user': user,
            'delay_seconds': delay, 'timeout': timeout, 'poll_seconds': poll}


def registration_state_command(user):
    # Read only the switch; never emit check-in IDs, account state or tokens.
    return ['sh', '-c', r'''
set -eu
app_data=/data/user/$1/com.google.android.gms
preferences=$app_data/shared_prefs/com.google.android.gms_preferences.xml
[ -d "$app_data" ] && [ ! -L "$app_data" ] && [ ! -L "$app_data/shared_prefs" ] && [ ! -L "$preferences" ]
if [ ! -e "$preferences" ]; then echo disabled; exit 0; fi
[ -f "$preferences" ]
owner=$(stat -c %u "$app_data")
[ "$owner" = "$(stat -c %u "$preferences")" ]
awk '
/name="checkin_enable_service"/ {
    count++
    if ($0 ~ /^[[:space:]]*<boolean name="checkin_enable_service" value="(true|false)"[[:space:]]*\/>[[:space:]]*$/)
        enabled = ($0 ~ /value="true"/)
    else malformed = 1
}
END {
    if (malformed || count > 1) exit 1
    print enabled ? "enabled" : "disabled"
}' "$preferences"
''', 'xenoid-registration-state', str(user)]


def complete(plan, receipt, save, execute, stopping, *, deadline=None):
    """Resume the recorded reset. execute(args) returns CompletedProcess bytes."""
    progress = receipt.setdefault('post_google_progress', {})
    if not isinstance(progress, dict):
        raise RuntimeError('post_google_progress_invalid')
    if 'started_at' not in progress:
        progress['started_at'] = time.time()
        progress['not_after'] = progress['started_at'] + plan['timeout'] if plan['timeout'] else None
        save()
    limit = progress.get('not_after')
    if (type(progress.get('started_at')) not in (int, float) or not math.isfinite(progress['started_at'])
            or limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit))
            or bool(plan['timeout']) != (limit is not None)):
        raise RuntimeError('post_google_deadline_progress_invalid')
    if limit is not None:
        stage_deadline = time.monotonic() + (limit-time.time())
        deadline = stage_deadline if deadline is None else min(deadline, stage_deadline)

    def budget():
        if stopping.is_set():
            raise RuntimeError('post_google_stopping')
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('post_google_timeout')

    def command(args):
        budget()
        answer = execute(args)
        if answer.returncode:
            raise RuntimeError('post_google_android_command_failed')
        return answer.stdout.decode(errors='replace').strip()

    def persist():
        save()

    def result_for(user):
        return {'ok': True, 'data_cleared': True, 'packages': list(plan['clear_packages']),
                'user': plan['user'], 'resolved_user': user, 'delay_seconds': plan['delay_seconds'],
                'registration_enabled': True, 'checkin_requested': True, 'network_registered': None}

    if progress.get('completed') is True:
        # A later native readiness retry must not expire or repeat a completed
        # post stage. Its immutable proof survives independently of its budget.
        user = progress.get('resolved_user')
        done = progress.get('cleared_packages')
        if (type(user) is not int or user < 0 or not isinstance(done, dict)
                or set(done) != set(plan['clear_packages'])
                or not all(progress.get(k) is True for k in
                           ('registration_started', 'registration_enabled', 'checkin_requested'))
                or progress.get('result') != result_for(user)):
            raise RuntimeError('post_google_completed_progress_invalid')
        for package, entry in done.items():
            if (not isinstance(entry, dict) or entry.get('package') != package or entry.get('user') != user
                    or entry.get('ok') is not True or entry.get('data_cleared') is not True
                    or entry.get('uid_verified') is not True):
                raise RuntimeError('post_google_completed_progress_invalid')
        return dict(progress['result'])

    user = progress.get('resolved_user')
    if user is None:
        user = plan['user']
        if user == 'current':
            value = command(['am', 'get-current-user'])
            if not re.fullmatch(r'[0-9]+', value):
                raise RuntimeError('post_google_user_unverified')
            user = int(value)
        progress['resolved_user'] = user
        persist()  # Resolve once before all package mutations.
    if type(user) is not int or user < 0:
        raise RuntimeError('post_google_recorded_user_invalid')
    done = progress.setdefault('cleared_packages', {})
    waits = progress.setdefault('waits', {})
    if not isinstance(done, dict) or not isinstance(waits, dict):
        raise RuntimeError('post_google_progress_invalid')
    if set(done) - set(plan['clear_packages']):
        raise RuntimeError('post_google_unknown_completed_package')
    if progress.get('registration_started') and set(done) != set(plan['clear_packages']):
        raise RuntimeError('post_google_registration_progress_inconsistent')

    def pause(name):
        state = waits.get(name)
        if state is None:
            state = waits[name] = {'not_before': time.time() + plan['delay_seconds'], 'completed': False}
            persist()
        if (not isinstance(state, dict) or type(state.get('not_before')) not in (int, float)
                or not math.isfinite(state['not_before']) or type(state.get('completed')) is not bool):
            raise RuntimeError('post_google_delay_progress_invalid')
        while not state['completed']:
            budget()
            remaining = state['not_before'] - time.time()
            if remaining <= 0:
                state['completed'] = True
                persist()
                break
            limit = remaining if deadline is None else min(remaining, deadline - time.monotonic())
            stopping.wait(min(.2, max(0, limit)))

    def package_uid(package):
        value = command(['cmd', 'package', 'list', 'packages', '-U', '--user', str(user), package])
        found = re.findall(r'(?m)^package:' + re.escape(package) + r' uid:([0-9]+)\s*$', value)
        if len(found) != 1:
            raise RuntimeError('post_google_package_uid_unverified')
        return int(found[0])

    pause('after_cache')
    for index, package in enumerate(plan['clear_packages']):
        budget()
        if package in done:
            entry = done[package]
            if (not isinstance(entry, dict) or entry.get('package') != package or entry.get('user') != user
                    or entry.get('ok') is not True or entry.get('data_cleared') is not True
                    or entry.get('uid_verified') is not True):
                raise RuntimeError('post_google_clear_progress_invalid')
            continue
        if progress.get('registration_started'):
            raise RuntimeError('post_google_clear_after_registration_forbidden')
        if index:
            pause('before_clear_' + str(index))
        before_uid = package_uid(package)
        if command(['pm', 'clear', '--user', str(user), package]) != 'Success':
            raise RuntimeError('post_google_clear_unconfirmed')
        if package_uid(package) != before_uid:
            raise RuntimeError('post_google_package_uid_changed')
        done[package] = {'ok': True, 'package': package, 'user': user,
                         'data_cleared': True, 'uid_verified': True}
        persist()

    pause('before_registration')
    if not progress.get('registration_started'):
        progress['registration_started'] = True
        persist()  # No future recovery may clear data beyond this boundary.
    def registration_state():
        state = command(registration_state_command(user))
        if state not in ('enabled', 'disabled'):
            raise RuntimeError('post_google_registration_state_invalid')
        return state

    state = registration_state() if progress.get('checkin_requested') else 'disabled'
    if not progress.get('checkin_requested') or state != 'enabled':
        # Reissuing ProvisionService is idempotent. If its prior request was
        # interrupted before the preference write, resume registration only.
        output = command(['cmd', 'activity', 'start-service', '--user', str(user), '-n',
                          REGISTRATION_COMPONENT, '--ez', 'checkin_enabled', 'true'])
        if re.search(r'(?im)^\s*(?:Error|Exception|SecurityException)\b', output):
            raise RuntimeError('post_google_registration_request_failed')
        progress['checkin_requested'] = True
        persist()
        state = registration_state()
    if state != 'enabled':
        budget()
        pause_seconds = plan['poll_seconds']
        if deadline is not None:
            pause_seconds = min(pause_seconds, max(0, deadline-time.monotonic()))
        stopping.wait(pause_seconds)
        state = registration_state()
        if state != 'enabled':
            # The outer script's configured retry pause schedules another
            # attempt; completed clears and their delays are never repeated.
            raise RuntimeError('post_google_registration_switch_pending')
    progress['registration_enabled'] = True
    progress['result'] = result_for(user)
    progress['completed'] = True
    persist()
    return dict(progress['result'])

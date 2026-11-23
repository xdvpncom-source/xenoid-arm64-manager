"""Credential-free initial-proxy intent for the existing convergence journal."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping
import uuid

from .proxy_source import normalize_proxy_failure_policy

SCHEMA = 'dev.xenoid.proxy-bootstrap/v1'
MAX_SOURCE_BYTES = 1024 * 1024
REQUEST_KEYS = frozenset({'schema', 'instanceId', 'sourcePath', 'sourceSha256', 'kind', 'enabled', 'selectedNode',
                         'udpAllowed', 'ipv6Allowed', 'allowInsecureHttp', 'failurePolicy'})
INTENT_KEYS = REQUEST_KEYS | {'dataUuid', 'phase', 'appliedGeneration'}


class ProxyBootstrapError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _need(ok: bool, code: str = 'proxy_bootstrap_intent_invalid') -> None:
    if not ok: raise ProxyBootstrapError(code)


def _uuid(value: Any) -> str:
    try:
        parsed = uuid.UUID(value)
        _need(str(parsed) == value)
        return value
    except (TypeError, ValueError, AttributeError) as error:
        raise ProxyBootstrapError('proxy_bootstrap_intent_invalid') from error


def validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(value, Mapping) and set(value) == REQUEST_KEYS)
    result = dict(value)
    _need(result['schema'] == SCHEMA)
    _uuid(result['instanceId'])
    path = result['sourcePath']
    _need(isinstance(path, str) and '\0' not in path and len(path) <= 4096
          and Path(path).is_absolute() and '..' not in Path(path).parts)
    try: _need(0 < len(path.encode('utf-8')) <= 4096)
    except UnicodeError as error: raise ProxyBootstrapError('proxy_bootstrap_intent_invalid') from error
    _need(isinstance(result['sourceSha256'], str) and re.fullmatch('[0-9a-f]{64}', result['sourceSha256']) is not None)
    _need(isinstance(result['kind'], str) and result['kind'] in {'endpoint', 'uri_list', 'clash', 'subscription'}
          and type(result['enabled']) is bool)
    _need(isinstance(result['selectedNode'], str) and '\0' not in result['selectedNode'] and len(result['selectedNode']) <= 128)
    try:
        result['selectedNode'].encode('utf-8', 'strict')
        _need(len(result['selectedNode'].encode('utf-16-le')) // 2 <= 128)
    except UnicodeError as error:
        raise ProxyBootstrapError('proxy_bootstrap_intent_invalid') from error
    _need(all(type(result[key]) is bool for key in ('udpAllowed', 'ipv6Allowed', 'allowInsecureHttp')))
    try:
        policy = normalize_proxy_failure_policy(result['failurePolicy'])
    except (TypeError, ValueError) as error:
        raise ProxyBootstrapError('proxy_bootstrap_policy_invalid') from error
    _need(isinstance(policy, dict) and policy.get('mode') == 'retry_proxy', 'proxy_bootstrap_policy_required')
    result['failurePolicy'] = policy
    return result


def validate_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    _need(isinstance(value, Mapping) and set(value) == INTENT_KEYS)
    result = {**validate_request({key: value[key] for key in REQUEST_KEYS}),
              'dataUuid': _uuid(value['dataUuid']), 'phase': value['phase'], 'appliedGeneration': value['appliedGeneration']}
    _need(isinstance(result['phase'], str) and result['phase'] in {'pending', 'applied'})
    if result['phase'] == 'pending':
        _need(result['appliedGeneration'] is None)
    else:
        _need(type(result['appliedGeneration']) is int and 0 <= result['appliedGeneration'] < 2**63)
    return result


def bind_request(value: Mapping[str, Any], data_uuid: str) -> dict[str, Any]:
    return validate_intent({**validate_request(value), 'dataUuid': data_uuid,
                            'phase': 'pending', 'appliedGeneration': None})


def request_from_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    result = validate_intent(value)
    return {key: result[key] for key in REQUEST_KEYS}


def _source_bytes(path: str) -> bytes:
    selected = Path(path)
    try:
        current = Path('/')
        for part in selected.parts[1:-1]:
            current /= part
            info = current.lstat()
            _need(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, os.geteuid())
                  and not info.st_mode & 0o022, 'proxy_bootstrap_source_unsafe')
        fd = os.open(selected, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            _need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1
                  and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= MAX_SOURCE_BYTES,
                  'proxy_bootstrap_source_unsafe')
            with os.fdopen(fd, 'rb', closefd=False) as stream: raw = stream.read(MAX_SOURCE_BYTES + 1)
            after = os.fstat(fd)
            _need((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                  == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                  and len(raw) == before.st_size, 'proxy_bootstrap_source_changed')
            return raw
        finally: os.close(fd)
    except OSError as error:
        raise ProxyBootstrapError('proxy_bootstrap_source_unavailable') from error


def read_source(value: Mapping[str, Any]) -> str:
    request = request_from_intent(value) if set(value) == INTENT_KEYS else validate_request(value)
    raw = _source_bytes(request['sourcePath'])
    _need(hashlib.sha256(raw).hexdigest() == request['sourceSha256'], 'proxy_bootstrap_source_changed')
    try: text = raw.decode('utf-8', 'strict')
    except UnicodeError as error:
        raise ProxyBootstrapError('proxy_bootstrap_source_invalid') from error
    _need(bool(text.strip()) and '\0' not in text, 'proxy_bootstrap_source_invalid')
    return text


def make_request(instance_id: str, path: Path, *, kind: str, udp_allowed: bool,
                 ipv6_allowed: bool, allow_insecure_http: bool, failure_policy: Mapping[str, Any],
                 selected_node: str = '', enabled: bool = True) -> dict[str, Any]:
    selected = path.expanduser().absolute()
    request = validate_request({'schema': SCHEMA, 'instanceId': instance_id, 'sourcePath': str(selected),
        'sourceSha256': hashlib.sha256(_source_bytes(str(selected))).hexdigest(), 'kind': kind,
        'enabled': enabled, 'selectedNode': selected_node,
        'udpAllowed': udp_allowed, 'ipv6Allowed': ipv6_allowed, 'allowInsecureHttp': allow_insecure_http,
        'failurePolicy': failure_policy})
    read_source(request)
    return request


def public_intent(value: Mapping[str, Any]) -> dict[str, Any]:
    """Do not expose the source path or source contents in command results."""
    intent = validate_intent(value)
    result = {key: intent[key] for key in ('schema', 'instanceId', 'dataUuid', 'sourceSha256', 'kind', 'enabled',
        'udpAllowed', 'ipv6Allowed', 'allowInsecureHttp', 'failurePolicy', 'phase', 'appliedGeneration')}
    result['selectedNodeSha256'] = hashlib.sha256(intent['selectedNode'].encode('utf-8')).hexdigest()
    return result

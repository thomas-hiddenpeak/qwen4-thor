"""Check actual durable catalog ownership, without disclosing private state.

Wire completion binds a catalog to the native sealed record. The native CPU
restart/expiry cases verify its private session contract; this offline check
does not decrypt state or claim that a remote server shares q4t's expiry clock.
"""
import hashlib
import json
import re
from tools.serve_service.package import require, sha


def retained(events, remote, directory, scope):
    ids = [e['item_id'] for e in events if e['type'] == 'mcp_list_tools.completed']
    require(len(ids) == len(set(ids)) <= 4 and
            remote['methods'].count('initialize') == len(ids) and
            0 <= remote['live_sessions'] <= len(ids) and
            remote['live_sessions'] + remote['deleted_sessions'] == len(ids),
            'remote sessions do not match completed owned imports')
    owner = hashlib.sha256(scope.encode()).hexdigest()
    records = []
    for identity in ids:
        require(re.fullmatch(r'mcpl_[A-Za-z0-9_-]+', identity), 'catalog identity')
        path = directory / (owner + '.tool_run.' + identity + '.json')
        require(not path.is_symlink() and path.stat().st_size <= 8 << 20,
                'bounded regular catalog record required')
        wrapper = json.loads(path.read_text())
        record = wrapper['record']
        encoded = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode()
        require(hashlib.sha256(encoded).hexdigest() == wrapper['sha256'] and
                record['owner'] == owner and record['id'] == identity and
                record['kind'] == 'tool_run' and record['revision'] >= 1 and
                0 < record['expires_at'] - record['created_at'] <= 3600 and
                record['data']['format'] == 'q4t.mcp.catalog.v1' and
                re.fullmatch(r'q4t1:[0-9a-f]{32}:[0-9a-f]{24}:[0-9a-f]{32}:[0-9a-f]+',
                             record['data']['sealed']),
                'owned finite sealed catalog missing or corrupt')
        records.append(dict(id=identity, file_sha256=sha(path),
            created_at=record['created_at'], expires_at=record['expires_at']))
    return dict(remote_sessions_retained=remote['live_sessions'], catalogs=records,
                local_process_cleanup_checked_separately=True,
                remote_server_expiration_not_claimed=True)

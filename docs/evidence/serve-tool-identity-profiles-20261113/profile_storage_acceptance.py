"""Exact primary/light qualification with real stored state across shutdown.

Reuse the accepted bounded driver: two starts, eight model requests, 900s.
Four additional control requests create/read/delete a real native conversation.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.serve_recovery import profile_acceptance as base
from tools.serve_service.package import read, require, save, sha

SOURCE = Path(__file__).resolve()
METADATA = dict(checkpoint='controller-stop-20261113')
original_prepare, original_finalize = base.prepare, base.finalize


def prepare(args):
    original_prepare(args)
    path = args.output.resolve()/'plan.json'
    plan = read(path)
    plan['sources'][str(SOURCE)] = sha(SOURCE)
    plan['targeted_storage_controls'] = dict(limit=4, metadata=METADATA,
        create_and_read_primary=True, read_and_delete_after_light_restart=True)
    save(path, plan)


class Stage(base.Stage):
    def profile_business(self):
        super().profile_business()
        require(self.plan['targeted_storage_controls']['metadata'] == METADATA,
                'storage fixture changed')
        if self.profile_name == 'primary':
            value = self.sdk('primary-storage-create', 'conversation_create',
                             dict(metadata=METADATA))
            require(value['object'] == 'conversation' and
                    value['metadata'] == METADATA, 'stored contract differs')
            identifier = value['id']
            require(type(identifier) is str and identifier.startswith('conv_')
                and identifier.isascii() and all(c.isalnum() or c == '_'
                                                for c in identifier),
                'invalid actual conversation identity')
            save(self.out/'storage-conversation.json', value)
        else:
            value = read(self.out/'storage-conversation.json')
            identifier = value['id']
        actual = self.sdk(self.profile_name+'-storage-read',
                          'conversation_retrieve', response_id=identifier)
        require(actual == value,
                'actual stored state changed or unavailable')
        if self.profile_name == 'light':
            deleted = self.sdk('light-storage-delete', 'conversation_delete',
                               response_id=identifier)
            require(deleted['id'] == identifier and
                    deleted['deleted'] is True, 'actual state deletion failed')
            save(self.out/'storage-business.json', dict(
                status='PASS_ACTUAL_STORAGE_ACROSS_MODEL_SHUTDOWN',
                conversation_id=identifier, metadata=METADATA,
                control_requests=4, persisted_across_restart=True,
                explicit_delete=True))


def finalize(output):
    out = Path(output)
    storage = read(out/'storage-business.json')
    require(storage['status'] == 'PASS_ACTUAL_STORAGE_ACROSS_MODEL_SHUTDOWN'
        and storage['persisted_across_restart'] and storage['explicit_delete']
        and storage['control_requests'] == 4, 'storage business incomplete')
    original_finalize(out)
    path = out/'acceptance.json'
    result = read(path)
    result['targeted_storage'] = dict(record=str(out/'storage-business.json'),
        sha256=sha(out/'storage-business.json'), **storage)
    save(path, result)


if __name__ == '__main__':
    base.prepare, base.Stage, base.finalize = prepare, Stage, finalize
    base.main()

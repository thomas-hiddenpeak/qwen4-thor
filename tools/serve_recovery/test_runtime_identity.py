"""Actual CPU file DAC reproducer for lost Jetson device group membership."""
import ctypes
import grp
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.serve_recovery import runtime_identity as identity


class SelectionTests(unittest.TestCase):
    def test_only_target_accounts_existing_device_groups_survive(self):
        account=SimpleNamespace(pw_uid=1234,pw_gid=1234,pw_name='cpu-service')
        devices=lambda name:SimpleNamespace(gr_gid={'video':44,'render':993}[name])
        with mock.patch.object(identity.pwd,'getpwuid',return_value=account),\
             mock.patch.object(os,'getgrouplist',return_value=[1234,27,44]) as membership,\
             mock.patch.object(identity.grp,'getgrnam',side_effect=devices):
            self.assertEqual(identity.groups(1234,1234),[44])
            self.assertEqual(identity.groups(1234,44),[])
        self.assertEqual(membership.call_args.args,('cpu-service',1234))

    def test_missing_account_and_root_alias_never_grant_fallback_membership(self):
        account=SimpleNamespace(pw_uid=1234,pw_gid=1234,pw_name='cpu-service')
        with mock.patch.object(identity.pwd,'getpwuid',side_effect=KeyError('unavailable')):
            with self.assertRaises(KeyError):identity.groups(1234,1234)
        with mock.patch.object(identity.pwd,'getpwuid',return_value=account),\
             mock.patch.object(os,'getgrouplist',return_value=[0,27,1234]),\
             mock.patch.object(identity.grp,'getgrnam',return_value=SimpleNamespace(gr_gid=0)):
            with self.assertRaises(ValueError):identity.groups(1234,1234)


def probe(path,uid,gid,mode):
    assert ctypes.CDLL(None).prctl(38,1,0,0,0)==0
    if mode=='clear':
        os.setgroups([]);os.setresgid(gid,gid,gid);os.setresuid(uid,uid,uid)
    else:identity.drop(uid,gid)
    result=dict(uid=list(os.getresuid()),gid=list(os.getresgid()),groups=os.getgroups())
    for name in ('device-buffer','administration'):
        try:result[name]=(path/name).read_text()
        except PermissionError:result[name]='DENIED'
    # access() checks DAC without opening/querying a GPU device.
    result['nvmap_dac_readable']=os.access('/dev/nvmap',os.R_OK,effective_ids=True)
    try:os.setresuid(0,0,0);result['regain_root']=True
    except PermissionError:result['regain_root']=False
    print(json.dumps(result),flush=True)


@unittest.skipUnless(os.geteuid()==0,'explicit root CPU identity checks')
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.uid,self.gid=int(os.environ['SUDO_UID']),int(os.environ['SUDO_GID'])
        output=ROOT/'.q4t-work/serve-runtime-identity-20261112';output.mkdir(exist_ok=True)
        output.chmod(0o755)
        self.temp=tempfile.TemporaryDirectory(dir=output);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);os.chown(self.path,self.uid,self.gid)
        for name,group in (('device-buffer','video'),('administration','sudo')):
            p=self.path/name;p.write_text('owned CPU fixture')
            os.chown(p,0,grp.getgrnam(group).gr_gid);p.chmod(0o440)

    def test_actual_old_dac_failure_fixed_without_retaining_admin_or_root(self):
        values={}
        for mode in ('clear','device'):
            result=subprocess.run([sys.executable,'-I','-B',__file__,'--probe',str(self.path),
                str(self.uid),str(self.gid),mode],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr);values[mode]=json.loads(result.stdout)
        self.assertEqual(values['clear']['device-buffer'],'DENIED')
        self.assertEqual(values['device']['device-buffer'],'owned CPU fixture')
        self.assertFalse(values['clear']['nvmap_dac_readable'])
        self.assertTrue(values['device']['nvmap_dac_readable'])
        for value in values.values():
            self.assertEqual(value['administration'],'DENIED');self.assertFalse(value['regain_root'])
            self.assertEqual(value['uid'],[self.uid]*3);self.assertEqual(value['gid'],[self.gid]*3)
        self.assertEqual(values['device']['groups'],identity.groups(self.uid,self.gid))

    def test_no_new_privileges_required_before_any_credential_change(self):
        self.assertEqual(ctypes.CDLL(None).prctl(39,0,0,0,0),0)
        with mock.patch.object(os,'setgroups') as change,self.assertRaisesRegex(ValueError,'no_new_privs'):
            identity.drop(self.uid,self.gid)
        change.assert_not_called();self.assertEqual(os.geteuid(),0)


if __name__=='__main__':
    if sys.argv[1:2]==['--probe']:probe(Path(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]),sys.argv[5])
    else:unittest.main(module=None)

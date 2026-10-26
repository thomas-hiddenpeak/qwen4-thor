"""Four CPU-only regressions for the observed missing Thor driver identity."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import prepare_gpu_pairs as preparation
from test_gpu_pair_stage import stage

DRIVER='/opt/nvidia/l4t-gpu-libs/openrm/libcuda.so.1.1'


class DriverFreezeContracts(unittest.TestCase):
    def parent(self,files):
        return dict(artifact='/owned/artifact',model='/owned/model',files=files)

    def test_opt_driver_inherited_with_original_digest_and_narrow_roots(self):
        source={DRIVER:'a'*64,'/usr/lib/known.so':'b'*64,
            '/opt/unrelated/libforeign.so':'c'*64,'/owned/model/weights':'d'*64}
        bound={}
        preparation.inherit_live_dependencies(self.parent(source),lambda p,h:bound.update({p:h}))
        self.assertEqual(bound,{k:v for k,v in source.items() if 'foreign' not in k})

    def test_missing_driver_rejected_before_any_binding_or_launch(self):
        bound={}
        with self.assertRaises(RuntimeError):
            preparation.inherit_live_dependencies(self.parent({'/usr/lib/libcudart.so':'a'*64}),
                lambda p,h:bound.update({p:h}))
        self.assertFalse(bound)

    def test_inherited_hash_failure_is_not_ignored(self):
        def changed(path,digest):
            self.assertEqual((path,digest),(DRIVER,'a'*64))
            raise RuntimeError('frozen driver digest differs')
        with self.assertRaisesRegex(RuntimeError,'digest differs'):
            preparation.inherit_live_dependencies(self.parent({DRIVER:'a'*64}),changed)

    def test_actual_mappings_require_exact_hash_and_env_abort_on_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest=root/'manifest.json';manifest.write_text('{}')
            libs=[root/n for n in ('libcuda.so.1.1','libcudart.so.13','libcublasLt.so.13')]
            for path in libs:path.write_text(path.name)
            files={str(p):stage.sha(p) for p in libs}
            plan=dict(files=files,tool_dependency=dict(package_root=str(root/'nsys'),manifest=str(manifest)))
            maps=''.join('000-fff r-xp 0 00:00 1 '+str(p)+'\n' for p in libs)
            original=Path.read_text
            def read(path,*a,**kw):return maps if str(path)=='/proc/7/maps' else original(path,*a,**kw)
            with patch.object(Path,'read_text',read):
                self.assertEqual(stage.loaded_libraries(plan,7),files)
                for mode in ('missing','changed'):
                    broken=dict(plan,files=dict(files))
                    if mode=='missing':broken['files'].pop(str(libs[0]))
                    else:broken['files'][str(libs[0])]='0'*64
                    with self.subTest(mode=mode),self.assertRaises(stage.legacy.EnvironmentAbort):
                        stage.loaded_libraries(broken,7)


if __name__=='__main__':unittest.main()

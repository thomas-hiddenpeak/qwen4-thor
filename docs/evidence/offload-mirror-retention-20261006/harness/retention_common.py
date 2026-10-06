"""Use the existing bounded process owner; do not import old frozen stages."""
import importlib.util
from pathlib import Path
import sys
sys.dont_write_bytecode = True
R = Path(__file__).resolve().parent
W = R/'source'
spec = importlib.util.spec_from_file_location('retention_owner', R.parent/'offload-mechanism-20261006/phase_common.py')
owned = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owned)
owned.W = W
run, sha, read, save = owned.run, owned.sha, owned.read, owned.save

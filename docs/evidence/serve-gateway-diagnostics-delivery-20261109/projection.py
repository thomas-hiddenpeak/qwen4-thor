import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from tools.serve_host import native
from tools.serve_recovery import control
assert Path(native.__file__).is_relative_to(Path(sys.argv[1]))
v=json.loads(Path(sys.argv[2]).read_text())
v.update(state="WAIT_DEPENDENCY",business_ready=False,gateway_bound_model=0,
 gateway_binding_reason="gateway_capacity_mismatch",
 gateway_binding_model_generation=v["model_generation"],gateway_binding_age_ms=5)
r=native.empty();r.update(native.project_status(v))
assert r["gateway_binding"]["reason"]=="gateway_capacity_mismatch"
assert 'reason="gateway_capacity_mismatch"} 1' in native.metrics(r)
print(json.dumps(dict(status="PASS_SEALED_DIAGNOSTIC_PROJECTION",reasons=len(control.GATEWAY_REASONS))))

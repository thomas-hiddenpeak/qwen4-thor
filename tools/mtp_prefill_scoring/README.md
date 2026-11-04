# Mainline pooled draft prefill scoring

The runtime and independent scoring/state fixtures were selectively extracted
from research commit c9d4475d. No snapshot preallocation, write pipeline,
prefill event timer, or client changes are included.

Build `q4t` and `q4t_prefill_scoring_contract` before freezing. The latter is an
explicit GPU fixture, never a default test. `prepare.py` binds clean committed
sources, the accepted parent, fixed historical inputs, and one 198-request
acceptance plan. It uses the original checkout only for frozen artifacts and
the existing evalscope environment; imports use this isolated checkout.

Run `driver.py --plan PLAN` beneath `tools/evalscope/gpu_exclusive.py` for the
entire phase. The first tests are real evalscope HTTP requests. Independent
state fixtures follow the HTTP target/output gates. No automatic resampling or
deployment occurs in this driver; results still require protection review.

The detailed scope, stopping rules, budgets and evidence reuse are frozen in
[the acceptance plan](../../docs/MTP_PREFILL_ADOPTION_PLAN_2026-11-04.md).

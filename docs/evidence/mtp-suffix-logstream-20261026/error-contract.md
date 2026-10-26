# Six target error records: classification only

This read-only review uses already extracted evidence. It does not accept the
aborted capture, export its report, or promise whole-trace completeness. The new
stream parser retains all six errors as UNRESOLVED and qualification-blocking.

The local CUDA 13.3 header identifies activity kinds 33/34/35 as OpenACC
data/launch/other and 36 as CUDA_EVENT, at lines 458–484 of
`/usr/local/cuda-13.3/include/cupti_activity.h`.
Header SHA256: `306ed811227ae7a0a154fd27f977c72e0e5ea0082f63d5395624f6abfd44596a`.
The same header documents `cuptiActivityDisableContext` at lines 11588–11604:
unsupported kinds may produce CUPTI_ERROR_INVALID_KIND. It does not enumerate a
complete context-support matrix or guarantee the surrounding collector remains
unaffected when a disable operation fails. No actual API return code is in the
six observed log records, so that return code is not reconstructed.

The [official CUPTI 13.4.0 API documentation](https://docs.nvidia.com/cupti/13.4.0/api/group__CUPTI__ACTIVITY__API.html#group__cupti__activity__api_1ga90d628b01bcc8044b86060e8a1ff7457)
has the same kind definitions and unsupported-kind contract (rendered source
lines 3844–3869 and 7048–7071 in the read-only web inspection). This is public
13.4 documentation, not the source of the exact Nsight-shipped binary. The
capture loaded the packaged `libcupti-sbsa.so.13.4`; the local 13.3 header and
public 13.4 API are semantic references, not binary identity substitutions.

Local HTML API reference SHA256:
`e048086d4b11f97343e79ff73272a8ea0a3cce8cb52b53b2a6e7052964fac9a8`, at
`/usr/local/cuda-13.3/extras/CUPTI/doc/html/api/group__CUPTI__ACTIVITY__API.html`,
lines 7926–7950. Neither the installed Nsight UserGuide/ReleaseNotes searches nor
the exact official-site searches found a public MemoryStorage counter contract.
This absence is not a claim that no internal documentation exists.

The already saved `suffix-gpu-pairs-20261026/nsys8-off-nvlog-volume.json`
(SHA256 `ef7b5cd6c8d841d2f17201a5534c37af74eeb5cbfde01a199c18ba4c0b7984b4`)
records the four context-disable errors at original lines
4999/5003/5007/5011, all 04:34:07.758. They follow the target CUDAFinish/DONE
records (4984/4985) and precede its NVTXFinish (5029). This supports the forensic
label AUXILIARY_ACTIVITY_CONTEXT_DISABLE_REJECTED, not a no-loss exception.
CUDA_EVENT metadata also participates in CUDA event synchronization association;
it must not be described as universally irrelevant to every GPU analysis.

The two MemoryStorage errors are at original lines 2065964/2065965,
04:34:22.936, after an observed SaveReport marker at 2065787, 04:34:21.202.
The objects print matching allocated/returned values 35/35 and 1020242/1020242.
The maximum supported statement is that these fields were equal at the logged
destructor diagnostic. A report-save line is not, by itself, authenticated
report/session identity; ordering after it is not proof the report contains
every event. The parser therefore records POST_REPORT_DESTRUCTOR_BALANCED_COUNTERS
only when one preceding SaveReport marker is observed, explicitly leaves report
identity unproved, and retains UNRESOLVED eligibility. Missing/multiple report
markers or unequal counts remain a more general unresolved destructor error.

`cuptiActivityGetNumDroppedRecords` has a separate public contract at header
lines 11606–11631 for buffer-space-related dropped records; reading it resets its
queue counter. No MemoryStorage field is documented as an alias for that API.
`cuptiActivityFlushAll` at lines 11975–12017 is not implicit CUDA synchronization,
and forced flush may deliver incomplete activity records. Thus equal pool
counters, DONE, a saved report, and flush completion cannot replace per-capture
identity, activity-field/correlation checks, diagnostics and loss evidence.

Any future policy review must preserve these six raw records and explain its
restricted recorded-activity scope explicitly. This phase changes neither the
old strict gate nor the performance NO_GO, and authorizes no new GPU capture.

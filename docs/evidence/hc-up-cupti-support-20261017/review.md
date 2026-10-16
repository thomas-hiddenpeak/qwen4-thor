# CUPTI submitted field support review

Actual CUPTI API version 130300, CUDA driver API version 13020, driver595.78.
ldd resolves the probe to the local CUDA13.3 libcupti.so.13. These checks do not
initialize a CUDA context or launch GPU work. Different version numbers alone
do not establish incompatibility: the version-matched local usage manual
supports backward, forward and enhanced compatibility subject to driver rules.

Official versioned API documentation:
https://docs.nvidia.com/cupti/13.3.0/api/group__CUPTI__ACTIVITY__API.html
Official versioned known issues:
https://docs.nvidia.com/cupti/13.3.0/release-notes/release-notes.html
Current release notes checked for a matching fix:
https://docs.nvidia.com/cupti/release-notes/release-notes.html

The explicit latency-timestamp restriction is HES tracing; the probe never
requested HES. The local header allows submitted=UNKNOWN when uncollected.
No checked source identifies this specific Thor software-trace submitted=0
case as a confirmed defect with a known fix. Latest notes discuss HES zero
values and latency-collection deadlock, different symptoms from this successful
three-kernel execution with only submitted missing. Do not infer a matching
fix or change the production driver/toolkit based on those notes.

No new capture, no upgraded library/driver, no inference. Current source
review cannot make missing submitted timestamps usable for model attribution.
Support absent in this probe is distinct from proven platform-wide exclusion.

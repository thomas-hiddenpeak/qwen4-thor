# Standalone media planner CPU evidence

80 contract cases, 36 real PNG/JPEG processor geometry comparisons, zero
warnings and no ASan/UBSan findings. The planner attempted no allocation
under persistent C++/C allocator denial. Exact logs, small generated images,
commands, tested source hashes and build configuration are archived.

The two host executables stay local with SHA-256 identities. No model, GPU
test or HTTP service was started. This core is not yet integrated in serve;
the default binary and frozen SRV-02h candidate stayed unchanged. Header/RGB
budgets do not bound decoder internal memory or reserve physical RAM.

# Original probe initialization-order check

One startup of the unchanged v2 probe under GDB. Keep ASLR enabled, install a
pending breakpoint on exported cuInit, stop at main before the first CUPTI API,
record loaded libcuda mappings, then kill this diagnostic child. Never continue
past main or launch kernels. Common GPU guard wraps debugger and child.

This checks pre-main initialization only. If cuInit is observed or libcuda is
mapped, inspect the stack before inferring ordering. If neither is observed,
pre-main CUDA driver initialization is not supported by this startup evidence;
this does not validate every internal CUPTI prerequisite or explain submitted=0.
No model request or repeated timestamp collection.

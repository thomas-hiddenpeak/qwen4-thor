# Production q4t and its native contract probe use the same link boundary.
# This does not intercept calls internal to external shared libraries.
function(q4t_link_cuda_cleanup target)
  foreach(api cudaMalloc cudaHostAlloc cudaMallocHost cudaMallocAsync
              cudaFree cudaFreeAsync cudaFreeHost cudaEventDestroy
              cudaStreamDestroy cudaStreamSynchronize cudaDeviceSynchronize
              cudaEventSynchronize)
    target_link_options(${target} PRIVATE "LINKER:--wrap=${api}")
  endforeach()
  if(Q4T_SERVE_FAULT_INJECTION)
    target_compile_definitions(${target} PRIVATE Q4T_SERVE_FAULT_INJECTION=1)
  endif()
endfunction()

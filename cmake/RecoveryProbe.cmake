# A separate disposable CUDA executable. Never linked into the CPU Guardian.
add_executable(q4t_cuda_probe
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/cuda_probe.cu)
set_target_properties(q4t_cuda_probe PROPERTIES OUTPUT_NAME q4t-cuda-probe)
target_link_libraries(q4t_cuda_probe PRIVATE CUDA::cudart)
target_compile_options(q4t_cuda_probe PRIVATE
  $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=-Wall,-Wextra,-Werror>)

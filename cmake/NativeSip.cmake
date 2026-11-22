# Only the opt-in telephone worker links the SIP transaction stack.
include_guard(GLOBAL)
find_path(Q4T_SOFIA_INCLUDE_DIR sofia-sip/nta.h PATH_SUFFIXES sofia-sip-1.12)
find_library(Q4T_SOFIA_LIBRARY NAMES sofia-sip-ua)
if(Q4T_SOFIA_INCLUDE_DIR AND Q4T_SOFIA_LIBRARY)
  add_executable(q4t_live_sip
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/live_sip_main.cpp)
  set_target_properties(q4t_live_sip PROPERTIES OUTPUT_NAME q4t-live-sip)
  target_compile_features(q4t_live_sip PRIVATE cxx_std_23)
  target_include_directories(q4t_live_sip SYSTEM PRIVATE ${Q4T_SOFIA_INCLUDE_DIR}
    ${Q4T_OPENSSL_INCLUDE_DIR} ${Q4T_OPENSSL_ARCH_INCLUDE_DIR})
  target_compile_options(q4t_live_sip PRIVATE -Wall -Wextra -Werror)
  target_link_libraries(q4t_live_sip PRIVATE ${Q4T_SOFIA_LIBRARY}
    ${Q4T_SYSTEM_FETCH_SSL_LIBRARY} ${Q4T_SYSTEM_CRYPTO_LIBRARY})
endif()

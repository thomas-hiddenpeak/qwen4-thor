# Standalone service transport; reuses host HTTP/JSON without model libraries.
include_guard(GLOBAL)
find_package(Threads REQUIRED)
add_library(q4t_ingress_access STATIC
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_access.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/trace/sha256.cpp)
target_link_libraries(q4t_ingress_access PUBLIC q4t_guardian_journal
  Threads::Threads)
target_compile_features(q4t_ingress_access PUBLIC cxx_std_23)
target_compile_options(q4t_ingress_access PRIVATE -Wall -Wextra -Werror)
option(Q4T_ENABLE_NATIVE_TLS "Build optional native TLS (system OpenSSL 3)" ON)
add_executable(q4t_ingress
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_main.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_transport.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_proxy.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_tls.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_credentials.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_cors.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_admin.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_access_admin.cpp
  ${CMAKE_CURRENT_LIST_DIR}/../src/server/http_request.cpp)
set_target_properties(q4t_ingress PROPERTIES OUTPUT_NAME q4t-ingress)
target_link_libraries(q4t_ingress PRIVATE q4t_ingress_access Threads::Threads)
target_compile_definitions(q4t_ingress PRIVATE
  Q4T_ENABLE_NATIVE_TLS=$<BOOL:${Q4T_ENABLE_NATIVE_TLS}>)
if(Q4T_ENABLE_NATIVE_TLS)
  find_path(Q4T_OPENSSL_INCLUDE_DIR openssl/ssl.h REQUIRED)
  find_path(Q4T_OPENSSL_ARCH_INCLUDE_DIR openssl/opensslconf.h
    HINTS ${Q4T_OPENSSL_INCLUDE_DIR}/${CMAKE_LIBRARY_ARCHITECTURE}
    PATH_SUFFIXES ${CMAKE_LIBRARY_ARCHITECTURE} REQUIRED)
  find_file(Q4T_SYSTEM_SSL_LIBRARY libssl.so.3
    PATHS /usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}
          /lib/${CMAKE_LIBRARY_ARCHITECTURE} /usr/lib64 /usr/lib /lib64 /lib
    NO_DEFAULT_PATH REQUIRED)
  target_include_directories(q4t_ingress SYSTEM PRIVATE
    ${Q4T_OPENSSL_INCLUDE_DIR} ${Q4T_OPENSSL_ARCH_INCLUDE_DIR})
  target_compile_definitions(q4t_ingress PRIVATE
    Q4T_SYSTEM_SSL_LIBRARY="${Q4T_SYSTEM_SSL_LIBRARY}")
  target_link_libraries(q4t_ingress PRIVATE ${CMAKE_DL_LIBS})
endif()
target_compile_features(q4t_ingress PRIVATE cxx_std_23)
target_compile_options(q4t_ingress PRIVATE -Wall -Wextra -Werror)
if(NOT DEFINED Q4T_BUILD_TESTS OR Q4T_BUILD_TESTS)
  add_executable(q4t_ingress_proxy_tests
    ${CMAKE_CURRENT_LIST_DIR}/../tests/ingress_proxy_test.cpp
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/ingress_proxy.cpp
    ${CMAKE_CURRENT_LIST_DIR}/../src/server/http_request.cpp)
  target_link_libraries(q4t_ingress_proxy_tests PRIVATE q4t_ingress_access)
  target_compile_options(q4t_ingress_proxy_tests PRIVATE -Wall -Wextra -Werror)
  add_executable(q4t_ingress_access_tests
    ${CMAKE_CURRENT_LIST_DIR}/../tests/ingress_access_test.cpp)
  target_link_libraries(q4t_ingress_access_tests PRIVATE q4t_ingress_access)
  target_compile_options(q4t_ingress_access_tests PRIVATE
    -Wall -Wextra -Werror)
  add_executable(q4t_ingress_no_tls $<TARGET_PROPERTY:q4t_ingress,SOURCES>)
  set_target_properties(q4t_ingress_no_tls PROPERTIES OUTPUT_NAME q4t-ingress-no-tls)
  target_link_libraries(q4t_ingress_no_tls PRIVATE
    q4t_ingress_access Threads::Threads)
  target_compile_features(q4t_ingress_no_tls PRIVATE cxx_std_23)
  target_compile_definitions(q4t_ingress_no_tls PRIVATE Q4T_ENABLE_NATIVE_TLS=0)
  target_compile_options(q4t_ingress_no_tls PRIVATE -Wall -Wextra -Werror)
endif()

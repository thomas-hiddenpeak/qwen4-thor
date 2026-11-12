# Runtime loading keeps an absent crypto library out of ordinary Chat startup.
include_guard(GLOBAL)
find_path(Q4T_OPENSSL_INCLUDE_DIR openssl/crypto.h REQUIRED)
find_path(Q4T_OPENSSL_ARCH_INCLUDE_DIR openssl/opensslconf.h
  HINTS ${Q4T_OPENSSL_INCLUDE_DIR}/${CMAKE_LIBRARY_ARCHITECTURE}
  PATH_SUFFIXES ${CMAKE_LIBRARY_ARCHITECTURE} REQUIRED)
find_file(Q4T_SYSTEM_CRYPTO_LIBRARY libcrypto.so.3
  PATHS /usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}
        /lib/${CMAKE_LIBRARY_ARCHITECTURE} /usr/lib64 /usr/lib /lib64 /lib
  NO_DEFAULT_PATH REQUIRED)
# The file-fetch child reuses the already required system OpenSSL headers.
# Optional runtime TLS is loaded there only, never during model startup.
find_file(Q4T_SYSTEM_FETCH_SSL_LIBRARY libssl.so.3
  PATHS /usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}
        /lib/${CMAKE_LIBRARY_ARCHITECTURE} /usr/lib64 /usr/lib /lib64 /lib
  NO_DEFAULT_PATH REQUIRED)
function(q4t_link_native_crypto target)
  target_include_directories(${target} SYSTEM PRIVATE
    ${Q4T_OPENSSL_INCLUDE_DIR} ${Q4T_OPENSSL_ARCH_INCLUDE_DIR})
  target_compile_definitions(${target} PRIVATE
    Q4T_SYSTEM_CRYPTO_LIBRARY="${Q4T_SYSTEM_CRYPTO_LIBRARY}"
    Q4T_SYSTEM_FETCH_SSL_LIBRARY="${Q4T_SYSTEM_FETCH_SSL_LIBRARY}")
  target_link_libraries(${target} PRIVATE ${CMAKE_DL_LIBS})
endfunction()

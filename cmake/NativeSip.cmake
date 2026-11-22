# Only the opt-in telephone worker links the SIP transaction stack.
include_guard(GLOBAL)
find_path(Q4T_SOFIA_INCLUDE_DIR sofia-sip/nta.h PATH_SUFFIXES sofia-sip-1.12)
find_library(Q4T_SOFIA_LIBRARY NAMES sofia-sip-ua)
if(Q4T_SOFIA_INCLUDE_DIR AND Q4T_SOFIA_LIBRARY)
  add_executable(q4t_live_sip
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/live_sip_main.cpp
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/live_sip_invite.cpp)
  set_target_properties(q4t_live_sip PROPERTIES OUTPUT_NAME q4t-live-sip)
  target_compile_features(q4t_live_sip PRIVATE cxx_std_23)
  target_include_directories(q4t_live_sip SYSTEM PRIVATE ${Q4T_SOFIA_INCLUDE_DIR}
    ${Q4T_OPENSSL_INCLUDE_DIR} ${Q4T_OPENSSL_ARCH_INCLUDE_DIR})
  target_compile_options(q4t_live_sip PRIVATE -Wall -Wextra -Werror)
  target_link_libraries(q4t_live_sip PRIVATE ${Q4T_SOFIA_LIBRARY}
    ${Q4T_SYSTEM_FETCH_SSL_LIBRARY} ${Q4T_SYSTEM_CRYPTO_LIBRARY})
endif()

# The SIP audio process shares the WebRTC worker's existing GStreamer base.
# libSRTP is used only by this on-demand process.
find_path(Q4T_SRTP_INCLUDE_DIR srtp2/srtp.h)
find_library(Q4T_SRTP_LIBRARY NAMES srtp2 libsrtp2.so.1)
if(Q4T_RTC_BASE_FOUND AND Q4T_SRTP_INCLUDE_DIR AND Q4T_SRTP_LIBRARY)
  add_executable(q4t_sip_media
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/sip_media_main.cpp
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/sip_media_audio.cpp
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/sip_media_offer.cpp
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/live_rtp_codec.cpp)
  set_target_properties(q4t_sip_media PROPERTIES OUTPUT_NAME q4t-sip-media)
  target_compile_features(q4t_sip_media PRIVATE cxx_std_23)
  target_compile_options(q4t_sip_media PRIVATE -Wall -Wextra -Werror)
  target_include_directories(q4t_sip_media SYSTEM PRIVATE ${Q4T_SRTP_INCLUDE_DIR})
  target_link_libraries(q4t_sip_media PRIVATE PkgConfig::Q4T_RTC_BASE ${Q4T_SRTP_LIBRARY})
endif()

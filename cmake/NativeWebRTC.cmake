# WebRTC and its plugins belong only to the on-demand media worker.
include_guard(GLOBAL)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(Q4T_RTC_BASE QUIET IMPORTED_TARGET gstreamer-app-1.0>=1.22
                    gstreamer-sdp-1.0>=1.22)
endif()
find_path(Q4T_WEBRTC_INCLUDE_DIR gst/webrtc/webrtc.h
          HINTS ${Q4T_RTC_BASE_INCLUDE_DIRS} PATH_SUFFIXES gstreamer-1.0)
find_library(Q4T_WEBRTC_LIBRARY NAMES gstwebrtc-1.0 libgstwebrtc-1.0.so.0)
if(Q4T_RTC_BASE_FOUND AND Q4T_WEBRTC_INCLUDE_DIR AND Q4T_WEBRTC_LIBRARY)
  add_executable(q4t_live_webrtc
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/live_webrtc_main.cpp
    ${CMAKE_CURRENT_LIST_DIR}/../src/service/live_webrtc_offer.cpp)
  set_target_properties(q4t_live_webrtc PROPERTIES OUTPUT_NAME q4t-live-webrtc)
  target_compile_features(q4t_live_webrtc PRIVATE cxx_std_23)
  target_compile_definitions(q4t_live_webrtc PRIVATE GST_USE_UNSTABLE_API)
  target_include_directories(q4t_live_webrtc SYSTEM PRIVATE ${Q4T_WEBRTC_INCLUDE_DIR})
  target_compile_options(q4t_live_webrtc PRIVATE -Wall -Wextra)
  target_link_libraries(q4t_live_webrtc PRIVATE PkgConfig::Q4T_RTC_BASE
                        ${Q4T_WEBRTC_LIBRARY})
endif()

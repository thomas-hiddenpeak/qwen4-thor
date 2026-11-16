# Headers describe the stable 1.x ABI; no GStreamer library is linked into the
# core executable. System runtimes/plugins are loaded only in the video child.
include_guard(GLOBAL)
find_package(PkgConfig QUIET)
if(PkgConfig_FOUND)
  pkg_check_modules(Q4T_GSTREAMER QUIET gstreamer-1.0 gstreamer-app-1.0)
endif()
set(Q4T_SYSTEM_GSTREAMER_DIRECTORY "/usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}")
function(q4t_configure_native_video target)
  if(Q4T_GSTREAMER_FOUND)
    target_include_directories(${target} SYSTEM PRIVATE ${Q4T_GSTREAMER_INCLUDE_DIRS})
    target_compile_definitions(${target} PRIVATE Q4T_HAVE_GSTREAMER_HEADERS=1
      Q4T_SYSTEM_GSTREAMER_DIRECTORY="${Q4T_SYSTEM_GSTREAMER_DIRECTORY}")
  endif()
endfunction()

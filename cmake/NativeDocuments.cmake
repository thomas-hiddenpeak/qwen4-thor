# Only the isolated document worker dynamically loads these system codecs.
include_guard(GLOBAL)
find_path(Q4T_ARCHIVE_INCLUDE_DIR archive.h REQUIRED)
find_path(Q4T_XML_INCLUDE_DIR libxml/xmlreader.h PATH_SUFFIXES libxml2 REQUIRED)
find_file(Q4T_SYSTEM_ARCHIVE_LIBRARY libarchive.so.13
  PATHS /usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}
        /lib/${CMAKE_LIBRARY_ARCHITECTURE} /usr/lib64 /usr/lib /lib64 /lib
  NO_DEFAULT_PATH REQUIRED)
find_file(Q4T_SYSTEM_XML_LIBRARY libxml2.so.2
  PATHS /usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}
        /lib/${CMAKE_LIBRARY_ARCHITECTURE} /usr/lib64 /usr/lib /lib64 /lib
  NO_DEFAULT_PATH REQUIRED)
function(q4t_link_native_documents target)
  target_include_directories(${target} SYSTEM PRIVATE
    ${Q4T_ARCHIVE_INCLUDE_DIR} ${Q4T_XML_INCLUDE_DIR})
  target_compile_definitions(${target} PRIVATE
    Q4T_SYSTEM_ARCHIVE_LIBRARY="${Q4T_SYSTEM_ARCHIVE_LIBRARY}"
    Q4T_SYSTEM_XML_LIBRARY="${Q4T_SYSTEM_XML_LIBRARY}")
  target_link_libraries(${target} PRIVATE ${CMAKE_DL_LIBS})
endfunction()

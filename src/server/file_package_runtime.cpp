#include "file_package_runtime.h"

#include <dlfcn.h>

#include "file_package.h"
#include "object_protocol.h"

namespace q4t::server::file_detail {
namespace {
xmlParserInputPtr NoExternal(const char*, const char*, xmlParserCtxtPtr) {
  return nullptr;
}
}  // namespace
PackageRuntime& Runtime() {
  static PackageRuntime runtime;
  return runtime;
}
ObjectReply LoadPackageRuntime() {
  auto& api = Runtime();
  if (api.ready) return {};
  const auto unavailable = [] {
    return object_detail::Error(503, "file_package_dependency_unavailable",
                                "Document container codec unavailable");
  };
  // Explicit child-only initialization, never static initialization in the
  // model process. NODELETE keeps callbacks/data valid for this short lifetime.
  void* archive =
      dlopen(Q4T_SYSTEM_ARCHIVE_LIBRARY, RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  void* xml =
      dlopen(Q4T_SYSTEM_XML_LIBRARY, RTLD_NOW | RTLD_LOCAL | RTLD_NODELETE);
  if (!archive || !xml) return unavailable();
#define Q4T_LOAD(handle, symbol)                                           \
  api.symbol##_fn =                                                        \
      reinterpret_cast<decltype(api.symbol##_fn)>(dlsym(handle, #symbol)); \
  if (!api.symbol##_fn) return unavailable()
  Q4T_LOAD(archive, archive_read_new);
  Q4T_LOAD(archive, archive_read_support_filter_none);
  Q4T_LOAD(archive, archive_read_support_format_zip_seekable);
  Q4T_LOAD(archive, archive_read_open_memory);
  Q4T_LOAD(archive, archive_read_next_header);
  Q4T_LOAD(archive, archive_read_data);
  Q4T_LOAD(archive, archive_read_free);
  Q4T_LOAD(archive, archive_entry_filetype);
  Q4T_LOAD(archive, archive_entry_pathname_utf8);
  Q4T_LOAD(archive, archive_entry_size);
  Q4T_LOAD(archive, archive_entry_size_is_set);
  Q4T_LOAD(archive, archive_entry_is_encrypted);
  Q4T_LOAD(archive, archive_entry_hardlink);
  Q4T_LOAD(xml, xmlInitParser);
  Q4T_LOAD(xml, xmlSetExternalEntityLoader);
  Q4T_LOAD(xml, xmlReaderForMemory);
  Q4T_LOAD(xml, xmlFreeTextReader);
  Q4T_LOAD(xml, xmlTextReaderSetParserProp);
  Q4T_LOAD(xml, xmlTextReaderSetErrorHandler);
  Q4T_LOAD(xml, xmlTextReaderRead);
  Q4T_LOAD(xml, xmlTextReaderNodeType);
  Q4T_LOAD(xml, xmlTextReaderDepth);
  Q4T_LOAD(xml, xmlTextReaderConstLocalName);
  Q4T_LOAD(xml, xmlTextReaderConstNamespaceUri);
  Q4T_LOAD(xml, xmlTextReaderConstValue);
  Q4T_LOAD(xml, xmlTextReaderAttributeCount);
  Q4T_LOAD(xml, xmlTextReaderMoveToAttributeNo);
  Q4T_LOAD(xml, xmlTextReaderMoveToElement);
  Q4T_LOAD(xml, xmlTextReaderIsEmptyElement);
#undef Q4T_LOAD
  api.xmlInitParser_fn();
  api.xmlSetExternalEntityLoader_fn(NoExternal);
  api.ready = true;
  return {};
}
}  // namespace q4t::server::file_detail

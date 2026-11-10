#pragma once

#include <archive.h>
#include <archive_entry.h>
#include <libxml/parser.h>
#include <libxml/xmlreader.h>

namespace q4t::server::file_detail {
struct PackageRuntime {
  bool ready = false;
#define Q4T_PACKAGE_API(symbol) decltype(&symbol) symbol##_fn = nullptr
  Q4T_PACKAGE_API(archive_read_new);
  Q4T_PACKAGE_API(archive_read_support_filter_none);
  Q4T_PACKAGE_API(archive_read_support_format_zip_seekable);
  Q4T_PACKAGE_API(archive_read_open_memory);
  Q4T_PACKAGE_API(archive_read_next_header);
  Q4T_PACKAGE_API(archive_read_data);
  Q4T_PACKAGE_API(archive_read_free);
  Q4T_PACKAGE_API(archive_entry_filetype);
  Q4T_PACKAGE_API(archive_entry_pathname_utf8);
  Q4T_PACKAGE_API(archive_entry_size);
  Q4T_PACKAGE_API(archive_entry_size_is_set);
  Q4T_PACKAGE_API(archive_entry_is_encrypted);
  Q4T_PACKAGE_API(archive_entry_hardlink);
  Q4T_PACKAGE_API(xmlInitParser);
  Q4T_PACKAGE_API(xmlSetExternalEntityLoader);
  Q4T_PACKAGE_API(xmlReaderForMemory);
  Q4T_PACKAGE_API(xmlFreeTextReader);
  Q4T_PACKAGE_API(xmlTextReaderSetParserProp);
  Q4T_PACKAGE_API(xmlTextReaderSetErrorHandler);
  Q4T_PACKAGE_API(xmlTextReaderRead);
  Q4T_PACKAGE_API(xmlTextReaderNodeType);
  Q4T_PACKAGE_API(xmlTextReaderDepth);
  Q4T_PACKAGE_API(xmlTextReaderConstLocalName);
  Q4T_PACKAGE_API(xmlTextReaderConstNamespaceUri);
  Q4T_PACKAGE_API(xmlTextReaderConstValue);
  Q4T_PACKAGE_API(xmlTextReaderAttributeCount);
  Q4T_PACKAGE_API(xmlTextReaderMoveToAttributeNo);
  Q4T_PACKAGE_API(xmlTextReaderMoveToElement);
  Q4T_PACKAGE_API(xmlTextReaderIsEmptyElement);
#undef Q4T_PACKAGE_API
};
PackageRuntime& Runtime();
}  // namespace q4t::server::file_detail

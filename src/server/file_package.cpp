#include "file_package.h"

#include <array>
#include <chrono>
#include <cstring>
#include <unordered_set>

#include "file_package_runtime.h"
#include "object_protocol.h"
#include "q4t/server/lifetime.h"

namespace q4t::server::file_detail {
namespace {
using object_detail::Error;
ObjectReply Live(const BinaryAccess& access) {
  if (access.cancelled && access.cancelled())
    return Error(499, "request_cancelled", "Document extraction cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "Document extraction timed out");
  return {};
}
ObjectReply Invalid() {
  return Error(400, "invalid_file_document", "Invalid document container");
}
ObjectReply Capacity() {
  return Error(413, "file_document_capacity",
               "Document container capacity exceeded");
}
bool Name(std::string_view name, bool directory) {
  if (name.empty() || name.size() > 1024 || name.front() == '/' ||
      name.find('\\') != name.npos || name.find(':') != name.npos)
    return false;
  if (directory && name.back() == '/') name.remove_suffix(1);
  if (name.empty()) return false;
  while (!name.empty()) {
    const auto slash = name.find('/');
    const auto part = name.substr(0, slash);
    if (part.empty() || part == "." || part == "..") return false;
    for (unsigned char c : part)
      if (c < 32 || c == 127) return false;
    if (slash == name.npos) return true;
    name.remove_prefix(slash + 1);
    if (name.empty()) return false;
  }
  return false;
}
std::string String(const xmlChar* value) {
  return value ? reinterpret_cast<const char*>(value) : "";
}
void XmlError(void*, const char*, xmlParserSeverities,
              xmlTextReaderLocatorPtr) {}
}  // namespace

bool ValidPackageLimits(const PackageLimits& limits) {
  return limits.max_bytes && limits.max_bytes <= (512ULL << 20) &&
         limits.max_entry_bytes && limits.max_entry_bytes <= (64ULL << 20) &&
         limits.max_entries && limits.max_entries <= 65536 &&
         limits.max_xml_nodes && limits.max_xml_nodes <= 2000000 &&
         limits.max_xml_depth && limits.max_xml_depth <= 128;
}

std::string_view XmlNode::Attribute(std::string_view key,
                                    std::string_view uri) const {
  for (const auto& attr : attributes)
    if (attr.name == key && attr.space == uri) return attr.value;
  return {};
}

ObjectReply ReadPackage(std::string_view bytes, const PackageLimits& limits,
                        const BinaryAccess& access, Package* output) {
  if (output) output->clear();
  if (!output || !ValidPackageLimits(limits))
    return Error(500, "file_document_budget_invalid",
                 "Invalid container budget");
  auto& api = Runtime();
  if (!api.ready)
    return Error(503, "file_package_dependency_unavailable",
                 "Codec not initialized");
  if (bytes.empty()) return Invalid();
  if (bytes.size() > kFileInputBytes) return Capacity();
  if (auto reply = Live(access); !reply.ok()) return reply;
  archive* reader = api.archive_read_new_fn();
  if (!reader) return Capacity();
  ScopeExit cleanup([&] { api.archive_read_free_fn(reader); });
  if (api.archive_read_support_filter_none_fn(reader) != ARCHIVE_OK ||
      api.archive_read_support_format_zip_seekable_fn(reader) != ARCHIVE_OK ||
      api.archive_read_open_memory_fn(reader, bytes.data(), bytes.size()) !=
          ARCHIVE_OK)
    return Invalid();
  Package next;
  std::unordered_set<std::string> names;
  uint64_t total = 0;
  size_t entries = 0;
  std::array<char, 65536> buffer;
  for (;;) {
    if (auto reply = Live(access); !reply.ok()) return reply;
    archive_entry* entry = nullptr;
    const int status = api.archive_read_next_header_fn(reader, &entry);
    if (status == ARCHIVE_EOF) break;
    if (status != ARCHIVE_OK || !entry) return Invalid();
    if (++entries > limits.max_entries) return Capacity();
    const auto type = api.archive_entry_filetype_fn(entry);
    const bool directory = type == AE_IFDIR;
    const char* path = api.archive_entry_pathname_utf8_fn(entry);
    if (!path || strnlen(path, 1025) > 1024 || !Name(path, directory) ||
        (type != AE_IFREG && !directory) ||
        api.archive_entry_hardlink_fn(entry) ||
        api.archive_entry_is_encrypted_fn(entry))
      return Invalid();
    std::string name(path);
    if (directory && name.back() == '/') name.pop_back();
    if (!names.insert(name).second) return Invalid();
    const bool sized = api.archive_entry_size_is_set_fn(entry);
    const auto promised = api.archive_entry_size_fn(entry);
    if (sized && (promised < 0 || (directory && promised))) return Invalid();
    if (sized && (static_cast<uint64_t>(promised) > limits.max_entry_bytes ||
                  static_cast<uint64_t>(promised) > limits.max_bytes - total))
      return Capacity();
    PackageEntry member{std::move(name), {}};
    for (;;) {
      if (auto reply = Live(access); !reply.ok()) return reply;
      const auto count =
          api.archive_read_data_fn(reader, buffer.data(), buffer.size());
      if (count < 0 || static_cast<uint64_t>(count) > buffer.size())
        return Invalid();
      if (!count) break;
      if (directory) return Invalid();
      if (static_cast<uint64_t>(count) > limits.max_bytes - total ||
          static_cast<uint64_t>(count) >
              limits.max_entry_bytes - member.bytes.size())
        return Capacity();
      total += count;
      member.bytes.append(buffer.data(), count);
    }
    if (sized && static_cast<uint64_t>(promised) != member.bytes.size())
      return Invalid();
    if (!directory) next.push_back(std::move(member));
  }
  if (next.empty()) return Invalid();
  if (auto reply = Live(access); !reply.ok()) return reply;
  *output = std::move(next);
  return {};
}

ObjectReply VisitXml(std::string_view bytes, const PackageLimits& limits,
                     const BinaryAccess& access,
                     const std::function<ObjectReply(const XmlNode&)>& visit) {
  if (!visit || !ValidPackageLimits(limits))
    return Error(500, "file_document_budget_invalid", "Invalid XML budget");
  auto& api = Runtime();
  if (!api.ready)
    return Error(503, "file_package_dependency_unavailable",
                 "Codec not initialized");
  if (bytes.empty()) return Invalid();
  if (bytes.size() > limits.max_entry_bytes) return Capacity();
  if (auto reply = Live(access); !reply.ok()) return reply;
  auto* reader = api.xmlReaderForMemory_fn(
      bytes.data(), static_cast<int>(bytes.size()), nullptr, nullptr,
      XML_PARSE_NONET | XML_PARSE_COMPACT | XML_PARSE_NOERROR |
          XML_PARSE_NOWARNING);
  if (!reader) return Invalid();
  ScopeExit cleanup([&] { api.xmlFreeTextReader_fn(reader); });
  api.xmlTextReaderSetErrorHandler_fn(reader, XmlError, nullptr);
  for (int property : {XML_PARSER_LOADDTD, XML_PARSER_DEFAULTATTRS,
                       XML_PARSER_VALIDATE, XML_PARSER_SUBST_ENTITIES})
    if (api.xmlTextReaderSetParserProp_fn(reader, property, 0) != 0)
      return Invalid();
  size_t nodes = 0;
  uint64_t text_bytes = 0;
  for (;;) {
    if (auto reply = Live(access); !reply.ok()) return reply;
    const int status = api.xmlTextReaderRead_fn(reader);
    if (!status) break;
    if (status != 1) return Invalid();
    if (++nodes > limits.max_xml_nodes) return Capacity();
    const int type = api.xmlTextReaderNodeType_fn(reader);
    const int depth = api.xmlTextReaderDepth_fn(reader);
    if (depth < 0 || static_cast<size_t>(depth) > limits.max_xml_depth)
      return Capacity();
    if (type == XML_READER_TYPE_DOCUMENT_TYPE ||
        type == XML_READER_TYPE_ENTITY ||
        type == XML_READER_TYPE_ENTITY_REFERENCE)
      return Invalid();
    XmlNode node;
    node.depth = depth;
    const bool start = type == XML_READER_TYPE_ELEMENT;
    const bool end = type == XML_READER_TYPE_END_ELEMENT;
    if (start || end) {
      node.kind = start ? XmlKind::kStart : XmlKind::kEnd;
      node.name = String(api.xmlTextReaderConstLocalName_fn(reader));
      node.space = String(api.xmlTextReaderConstNamespaceUri_fn(reader));
      if (node.name.size() > 512 || node.space.size() > 1024) return Capacity();
      const int attributes = api.xmlTextReaderAttributeCount_fn(reader);
      if (attributes < 0 || attributes > 128) return Capacity();
      for (int i = 0; start && i < attributes; ++i) {
        if (api.xmlTextReaderMoveToAttributeNo_fn(reader, i) != 1)
          return Invalid();
        XmlAttribute attr{String(api.xmlTextReaderConstLocalName_fn(reader)),
                          String(api.xmlTextReaderConstNamespaceUri_fn(reader)),
                          String(api.xmlTextReaderConstValue_fn(reader))};
        if (attr.name.size() > 512 || attr.space.size() > 1024 ||
            attr.value.size() > (1ULL << 20) ||
            attr.value.size() > limits.max_bytes - text_bytes)
          return Capacity();
        text_bytes += attr.value.size();
        node.attributes.push_back(std::move(attr));
      }
      if (start && attributes && api.xmlTextReaderMoveToElement_fn(reader) != 1)
        return Invalid();
    } else if (type == XML_READER_TYPE_TEXT || type == XML_READER_TYPE_CDATA ||
               type == XML_READER_TYPE_WHITESPACE ||
               type == XML_READER_TYPE_SIGNIFICANT_WHITESPACE) {
      node.value = String(api.xmlTextReaderConstValue_fn(reader));
      if (node.value.size() > limits.max_bytes - text_bytes) return Capacity();
      text_bytes += node.value.size();
    } else {
      continue;
    }
    if (auto reply = visit(node); !reply.ok()) return reply;
    if (start && api.xmlTextReaderIsEmptyElement_fn(reader) == 1) {
      node.kind = XmlKind::kEnd;
      if (auto reply = visit(node); !reply.ok()) return reply;
    }
  }
  return Live(access);
}
}  // namespace q4t::server::file_detail

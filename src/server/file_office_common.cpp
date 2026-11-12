#include "file_office_internal.h"

#include <algorithm>
#include <charconv>

#include "object_protocol.h"

namespace q4t::server::file_detail {
ObjectReply OfficeInvalid() {
  return object_detail::Error(400, "invalid_file_document",
                              "Invalid Office document structure");
}
ObjectReply OfficeCapacity() {
  return object_detail::Error(413, "file_document_capacity",
                              "Office document exceeds extraction capacity");
}
bool Unsigned(std::string_view text, uint64_t* value) {
  if (text.empty()) return false;
  const auto parsed =
      std::from_chars(text.data(), text.data() + text.size(), *value);
  return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size();
}
bool OfficeSpace(std::string_view uri, std::string_view family) {
  return uri == "http://schemas.openxmlformats.org/" + std::string(family) +
                    "/2006/main" ||
         uri == "http://purl.oclc.org/ooxml/" + std::string(family) + "/main";
}
bool RelationType(std::string_view uri, std::string_view type) {
  return uri == std::string(kRelations) + "/" + std::string(type) ||
         uri == std::string(kStrictRelations) + "/" + std::string(type);
}
std::string_view RelationId(const XmlNode& node) {
  const auto value = node.Attribute("id", kRelations);
  return value.empty() ? node.Attribute("id", kStrictRelations) : value;
}
const std::string* OfficeContext::Part(std::string_view name) const {
  for (const auto& entry : package)
    if (entry.name == name) return &entry.bytes;
  return nullptr;
}
ObjectReply OfficeContext::Xml(
    std::string_view part,
    const std::function<ObjectReply(const XmlNode&)>& visit) const {
  const auto* bytes = Part(part);
  if (!bytes) return OfficeInvalid();
  return VisitOfficeXml(*bytes, package_limits, access, visit);
}
ObjectReply OfficeContext::Relations(std::string_view part,
                                     Relationships* out) const {
  out->clear();
  const auto slash = part.rfind('/');
  const auto directory =
      slash == part.npos ? std::string_view{} : part.substr(0, slash + 1);
  const auto filename = slash == part.npos ? part : part.substr(slash + 1);
  const auto name =
      std::string(directory) + "_rels/" + std::string(filename) + ".rels";
  if (!Part(name)) return {};
  Relationships next;
  bool root = false;
  auto reply = Xml(name, [&](const XmlNode& node) -> ObjectReply {
    if (node.kind != XmlKind::kStart) return {};
    if (!node.depth) {
      if (node.name != "Relationships" || node.space != kPackageRelations)
        return OfficeInvalid();
      root = true;
      return {};
    }
    if (node.depth != 1 || node.name != "Relationship" ||
        node.space != kPackageRelations)
      return OfficeInvalid();
    const auto id = node.Attribute("Id"), type = node.Attribute("Type"),
               target = node.Attribute("Target"),
               mode = node.Attribute("TargetMode");
    if (id.empty() || id.size() > 256 || type.empty() || type.size() > 1024 ||
        target.empty() || target.size() > 2048 ||
        (!mode.empty() && mode != "Internal" && mode != "External") ||
        !next.emplace(std::string(id),
                      Relationship{std::string(type), std::string(target),
                                   mode == "External"})
             .second)
      return OfficeInvalid();
    return {};
  });
  if (!reply.ok()) return reply;
  if (!root) return OfficeInvalid();
  *out = std::move(next);
  return {};
}
ObjectReply OfficeContext::Target(std::string_view source,
                                  const Relationship& relation,
                                  std::string* name) const {
  name->clear();
  if (relation.external) return OfficeInvalid();
  std::string target;
  const auto hex = [](char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
  };
  for (size_t i = 0; i < relation.target.size(); ++i) {
    unsigned char c = relation.target[i];
    if (c == '%') {
      if (relation.target.size() - i < 3) return OfficeInvalid();
      const int high = hex(relation.target[i + 1]),
                low = hex(relation.target[i + 2]);
      if (high < 0 || low < 0) return OfficeInvalid();
      c = high * 16 + low;
      // Encoded separators are not alternate paths to a different member.
      if (c == '/' || c == '\\') return OfficeInvalid();
      i += 2;
    }
    if (c < 32 || c == 127 || c == '\\' || c == ':' || c == '?' || c == '#')
      return OfficeInvalid();
    target += static_cast<char>(c);
  }
  std::string path;
  if (!target.empty() && target.front() == '/')
    target.erase(0, 1);
  else if (const auto slash = source.rfind('/'); slash != source.npos)
    path = source.substr(0, slash + 1);
  path += target;
  std::vector<std::string_view> parts;
  std::string_view remaining = path;
  while (!remaining.empty()) {
    const auto slash = remaining.find('/');
    const auto part = remaining.substr(0, slash);
    if (part.empty()) return OfficeInvalid();
    if (part == "..") {
      if (parts.empty()) return OfficeInvalid();
      parts.pop_back();
    } else if (part != ".") {
      parts.push_back(part);
    }
    if (slash == remaining.npos) break;
    remaining.remove_prefix(slash + 1);
    if (remaining.empty()) return OfficeInvalid();
  }
  for (auto part : parts) {
    if (!name->empty()) *name += '/';
    *name += part;
  }
  if (name->empty() || name->size() > 1024 || !Part(*name))
    return OfficeInvalid();
  return {};
}
ObjectReply OfficeContext::Text(std::string_view value) {
  if (value.size() > limits.max_text_bytes - retained_bytes)
    return OfficeCapacity();
  retained_bytes += value.size();
  document.text += value;
  return {};
}
ObjectReply OfficeContext::Rows(FileDocumentSheet& sheet,
                                const std::vector<std::string>& row,
                                uint64_t repeat) {
  if (!repeat || repeat > 1000000000 - sheet.source_rows)
    return OfficeInvalid();
  if (row.size() > limits.max_columns) return OfficeCapacity();
  sheet.columns = std::max(sheet.columns, row.size());
  uint64_t bytes = 0;
  for (const auto& cell : row) {
    if (cell.size() > limits.max_cell_bytes ||
        cell.size() > limits.max_text_bytes - bytes)
      return OfficeCapacity();
    bytes += cell.size();
  }
  const auto kept = std::min<uint64_t>(
      repeat, FileDocumentLimits::kSheetRows - sheet.rows.size());
  if (kept && (bytes > (limits.max_text_bytes - retained_bytes) / kept ||
               row.size() > (limits.max_cells - retained_cells) / kept))
    return OfficeCapacity();
  sheet.source_rows += repeat;
  for (uint64_t i = 0; i < kept; ++i) sheet.rows.push_back(row);
  retained_bytes += kept * bytes;
  retained_cells += kept * row.size();
  return {};
}
}  // namespace q4t::server::file_detail

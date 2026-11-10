#pragma once

#include <unordered_map>

#include "file_office.h"

namespace q4t::server::file_detail {
inline constexpr std::string_view kPackageRelations =
    "http://schemas.openxmlformats.org/package/2006/relationships";
inline constexpr std::string_view kRelations =
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
inline constexpr std::string_view kStrictRelations =
    "http://purl.oclc.org/ooxml/officeDocument/relationships";

bool OfficeSpace(std::string_view uri, std::string_view family);
bool RelationType(std::string_view uri, std::string_view type);
std::string_view RelationId(const XmlNode& node);
ObjectReply OfficeInvalid();
ObjectReply OfficeCapacity();
bool Unsigned(std::string_view text, uint64_t* value);
ObjectReply VisitOfficeXml(
    std::string_view bytes, const PackageLimits& limits,
    const BinaryAccess& access,
    const std::function<ObjectReply(const XmlNode&)>& visit);

struct Relationship {
  std::string type, target;
  bool external = false;
};
using Relationships = std::unordered_map<std::string, Relationship>;

struct OfficeContext {
  const Package& package;
  const FileDocumentLimits& limits;
  const PackageLimits& package_limits;
  const BinaryAccess& access;
  FileDocument& document;
  uint64_t retained_bytes = 0;
  size_t retained_cells = 0;
  size_t nested_documents = 0;
  const std::string* Part(std::string_view name) const;
  ObjectReply Xml(
      std::string_view part,
      const std::function<ObjectReply(const XmlNode&)>& visit) const;
  ObjectReply Relations(std::string_view part, Relationships* out) const;
  // Resolve a package relationship into an in-memory member. External targets
  // never trigger file/network IO, and cannot be an internal content part.
  ObjectReply Target(std::string_view source, const Relationship& relation,
                     std::string* name) const;
  ObjectReply Text(std::string_view value);
  ObjectReply Alternate(std::string_view source, std::string_view relation_id);
  ObjectReply Rows(FileDocumentSheet& sheet,
                   const std::vector<std::string>& row, uint64_t repeat = 1);
};

ObjectReply Word(OfficeContext& context, std::string_view main);
ObjectReply Slides(OfficeContext& context, std::string_view main);
ObjectReply Workbook(OfficeContext& context, std::string_view main);
ObjectReply OpenDocument(OfficeContext& context);
}  // namespace q4t::server::file_detail

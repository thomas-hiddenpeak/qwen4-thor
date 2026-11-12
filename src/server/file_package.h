// Internal document-container primitives. Call only in an isolated parser;
// load codecs before entering the pure-memory syscall sandbox.
#pragma once

#include <functional>
#include <string>
#include <string_view>
#include <vector>

#include "q4t/server/file_document.h"

namespace q4t::server::file_detail {
using PackageLimits = FilePackageLimits;
struct PackageEntry {
  std::string name, bytes;
};
using Package = std::vector<PackageEntry>;
enum class XmlKind { kStart, kEnd, kText };
struct XmlAttribute {
  std::string name, space, value;
};
struct XmlNode {
  XmlKind kind = XmlKind::kText;
  std::string name, space, value;
  std::vector<XmlAttribute> attributes;
  size_t depth = 0;
  std::string_view Attribute(std::string_view key,
                             std::string_view uri = {}) const;
};
bool ValidPackageLimits(const PackageLimits& limits);
ObjectReply LoadPackageRuntime();
// Original member names are identifiers in memory, never host paths. Every
// member's actual bytes/CRC is checked, even when a format ignores its data.
ObjectReply ReadPackage(std::string_view bytes, const PackageLimits& limits,
                        const BinaryAccess& access, Package* output);
// Expanded names, not lexical prefixes. DTD/entity declarations and references
// are rejected; predefined XML entities are decoded by the system parser.
// Empty elements produce both start and end events. No partial result policy
// is implied for the visitor: the caller publishes its document only on OK.
ObjectReply VisitXml(std::string_view bytes, const PackageLimits& limits,
                     const BinaryAccess& access,
                     const std::function<ObjectReply(const XmlNode&)>& visit);
}  // namespace q4t::server::file_detail

#include "file_office_internal.h"

#include <limits>

namespace q4t::server::file_detail {
namespace {
constexpr std::string_view kMc =
    "http://schemas.openxmlformats.org/markup-compatibility/2006";
constexpr std::string_view kXmlns = "http://www.w3.org/2000/xmlns/";
constexpr size_t kUnset = std::numeric_limits<size_t>::max();
struct Alternate {
  size_t depth;
  bool selected = false, fallback = false, choice = false;
};
bool Understood(std::string_view space) {
  for (auto family :
       {"wordprocessingml", "spreadsheetml", "presentationml", "drawingml"})
    if (OfficeSpace(space, family)) return true;
  // These wrappers contain the same WordprocessingML text-box content. We
  // consume their text, not their rendering, geometry, or embedded images.
  return space ==
             "http://schemas.microsoft.com/office/word/2010/"
             "wordprocessingShape" ||
         space ==
             "http://schemas.microsoft.com/office/word/2010/"
             "wordprocessingGroup";
}
}  // namespace

ObjectReply VisitOfficeXml(
    std::string_view bytes, const PackageLimits& limits,
    const BinaryAccess& access,
    const std::function<ObjectReply(const XmlNode&)>& visit) {
  std::vector<std::unordered_map<std::string, std::string>> namespaces;
  std::vector<Alternate> alternatives;
  size_t skip = kUnset, removed_depth = 0;
  return VisitXml(
      bytes, limits, access, [&](const XmlNode& node) -> ObjectReply {
        if (skip != kUnset) {
          if (node.kind == XmlKind::kEnd && node.depth == skip) skip = kUnset;
          return {};
        }
        if (node.kind == XmlKind::kStart) {
          namespaces.resize(node.depth + 1);
          namespaces.back().clear();
          for (const auto& attribute : node.attributes)
            if (attribute.space == kXmlns)
              namespaces.back().emplace(attribute.name, attribute.value);
        }
        if (node.space == kMc) {
          if (node.kind == XmlKind::kStart) {
            if (node.name == "AlternateContent") {
              alternatives.push_back({node.depth});
              ++removed_depth;
            } else if (node.name == "Choice" || node.name == "Fallback") {
              if (alternatives.empty() ||
                  node.depth != alternatives.back().depth + 1)
                return OfficeInvalid();
              auto& alternate = alternatives.back();
              if (alternate.fallback) return OfficeInvalid();
              bool applicable = node.name == "Fallback";
              if (applicable) {
                if (!alternate.choice) return OfficeInvalid();
                alternate.fallback = true;
              } else {
                alternate.choice = true;
                auto required = node.Attribute("Requires");
                if (required.empty()) return OfficeInvalid();
                applicable = true;
                bool have_prefix = false;
                while (!required.empty()) {
                  const auto start = required.find_first_not_of(" \t\r\n");
                  if (start == required.npos) break;
                  required.remove_prefix(start);
                  const auto end = required.find_first_of(" \t\r\n");
                  const std::string prefix(required.substr(0, end));
                  have_prefix = true;
                  std::string_view space;
                  for (auto i = namespaces.rbegin(); i != namespaces.rend();
                       ++i) {
                    const auto found = i->find(prefix);
                    if (found != i->end()) {
                      space = found->second;
                      break;
                    }
                  }
                  if (space.empty()) return OfficeInvalid();
                  applicable = applicable && Understood(space);
                  if (end == required.npos) break;
                  required.remove_prefix(end);
                }
                if (!have_prefix) return OfficeInvalid();
              }
              if (alternate.selected || !applicable)
                skip = node.depth;
              else {
                alternate.selected = true;
                ++removed_depth;
              }
            } else
              return OfficeInvalid();
          } else if (node.kind == XmlKind::kEnd) {
            if (!removed_depth || alternatives.empty()) return OfficeInvalid();
            --removed_depth;
            if (node.name == "AlternateContent") {
              if (!alternatives.back().choice) return OfficeInvalid();
              alternatives.pop_back();
            }
          }
          return {};
        }
        // Branch wrapper whitespace is not part of the document's visible text.
        if (!alternatives.empty() &&
            node.depth == alternatives.back().depth + 1) {
          if (node.kind != XmlKind::kText ||
              node.value.find_first_not_of(" \t\r\n") != node.value.npos)
            return OfficeInvalid();
          return {};
        }
        if (node.depth < removed_depth) return OfficeInvalid();
        XmlNode visible = node;
        visible.depth -= removed_depth;
        return visit(visible);
      });
}
}  // namespace q4t::server::file_detail

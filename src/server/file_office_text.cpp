#include "file_office_internal.h"

#include <limits>
#include <unordered_set>

namespace q4t::server::file_detail {
namespace {
constexpr size_t kUnset = std::numeric_limits<size_t>::max();
struct WordRefs {
  std::vector<std::string> stories;
  std::unordered_set<std::string> footnotes, endnotes, comments;
};
ObjectReply WordPart(
    OfficeContext& context, std::string_view part,
    std::string_view expected_root, WordRefs* references,
    const std::unordered_set<std::string>* selected = nullptr) {
  size_t text = kUnset, skip = kUnset;
  bool root = false;
  std::unordered_set<std::string> seen;
  auto reply = context.Xml(part, [&](const XmlNode& node) -> ObjectReply {
    const bool word = OfficeSpace(node.space, "wordprocessingml");
    if (skip != kUnset) {
      if (node.kind == XmlKind::kEnd && node.depth == skip) skip = kUnset;
      return {};
    }
    if (node.kind == XmlKind::kStart) {
      if (!node.depth) {
        root = word && node.name == expected_root;
        if (!root) return OfficeInvalid();
      }
      if (!root) return OfficeInvalid();
      if (selected && node.depth == 1) {
        const auto id = node.Attribute("id", node.space);
        if (!word || !selected->contains(std::string(id))) {
          skip = node.depth;
          return {};
        }
        if (!seen.emplace(id).second) return OfficeInvalid();
      }
      if (!word) return {};
      if (node.name == "del" || node.name == "moveFrom") {
        skip = node.depth;
      } else if (node.name == "t") {
        if (text != kUnset) return OfficeInvalid();
        text = node.depth;
      } else if (node.name == "tab") {
        return context.Text("\t");
      } else if (node.name == "br" || node.name == "cr") {
        return context.Text("\n");
      } else if (node.name == "noBreakHyphen") {
        return context.Text("‑");
      } else if (node.name == "softHyphen") {
        return context.Text("\xc2\xad");
      } else if (references && (node.name == "headerReference" ||
                                node.name == "footerReference")) {
        const auto id = RelationId(node);
        if (id.empty()) return OfficeInvalid();
        references->stories.emplace_back(id);
      } else if (references && (node.name == "footnoteReference" ||
                                node.name == "endnoteReference" ||
                                node.name == "commentReference")) {
        const auto id = node.Attribute("id", node.space);
        if (id.empty()) return OfficeInvalid();
        auto& ids = node.name == "footnoteReference"  ? references->footnotes
                    : node.name == "endnoteReference" ? references->endnotes
                                                      : references->comments;
        ids.emplace(id);
      } else if (node.name == "altChunk") {
        const auto id = RelationId(node);
        if (id.empty()) return OfficeInvalid();
        return context.Alternate(part, id);
      } else if (node.name == "sym") {
        const auto code = node.Attribute("char", node.space);
        const auto font = node.Attribute("font", node.space);
        return context.Text("[symbol " + std::string(font) + " " +
                            std::string(code) + "]");
      }
    } else if (node.kind == XmlKind::kText) {
      if (text != kUnset) return context.Text(node.value);
    } else if (node.kind == XmlKind::kEnd && word) {
      if (node.depth == text) text = kUnset;
      if (node.name == "p" || node.name == "tr") return context.Text("\n");
      if (node.name == "tc") return context.Text("\t");
    }
    return {};
  });
  if (!reply.ok()) return reply;
  if (!root || (selected && seen.size() != selected->size()))
    return OfficeInvalid();
  return {};
}
ObjectReply DrawingText(OfficeContext& context, std::string_view part,
                        std::string_view root_name) {
  size_t text = kUnset;
  return context.Xml(part, [&](const XmlNode& node) -> ObjectReply {
    const bool drawing = OfficeSpace(node.space, "drawingml");
    if (node.kind == XmlKind::kStart) {
      if (!node.depth && (!OfficeSpace(node.space, "presentationml") ||
                          node.name != root_name))
        return OfficeInvalid();
      if (drawing && node.name == "t") {
        if (text != kUnset) return OfficeInvalid();
        text = node.depth;
      }
      if (drawing && node.name == "br") return context.Text("\n");
      if (drawing && node.name == "tab") return context.Text("\t");
    } else if (node.kind == XmlKind::kText) {
      if (text != kUnset) return context.Text(node.value);
    } else if (node.kind == XmlKind::kEnd && drawing) {
      if (node.depth == text) text = kUnset;
      if (node.name == "p" || node.name == "tr") return context.Text("\n");
      if (node.name == "tc") return context.Text("\t");
    }
    return {};
  });
}
}  // namespace

ObjectReply Word(OfficeContext& context, std::string_view main) {
  Relationships relations;
  auto reply = context.Relations(main, &relations);
  if (!reply.ok()) return reply;
  WordRefs references;
  reply = WordPart(context, main, "document", &references);
  if (!reply.ok()) return reply;
  std::unordered_set<std::string> visited;
  for (const auto& id : references.stories) {
    if (!visited.insert(id).second) continue;
    const auto found = relations.find(id);
    if (found == relations.end()) return OfficeInvalid();
    const bool header = RelationType(found->second.type, "header");
    if (!header && !RelationType(found->second.type, "footer"))
      return OfficeInvalid();
    std::string part;
    reply = context.Target(main, found->second, &part);
    if (!reply.ok()) return reply;
    reply = context.Text(header ? "\n[Header]\n" : "\n[Footer]\n");
    if (!reply.ok()) return reply;
    reply = WordPart(context, part, header ? "hdr" : "ftr", nullptr);
    if (!reply.ok()) return reply;
  }
  for (const auto& [type, selected] : std::vector<
           std::pair<std::string_view, const std::unordered_set<std::string>*>>{
           {"footnotes", &references.footnotes},
           {"endnotes", &references.endnotes},
           {"comments", &references.comments}}) {
    if (selected->empty()) continue;
    const Relationship* relation = nullptr;
    for (const auto& [id, value] : relations) {
      if (!RelationType(value.type, type)) continue;
      if (relation) return OfficeInvalid();
      relation = &value;
    }
    if (!relation) return OfficeInvalid();
    std::string part;
    reply = context.Target(main, *relation, &part);
    if (!reply.ok()) return reply;
    reply = context.Text("\n[" + std::string(type) + "]\n");
    if (!reply.ok()) return reply;
    reply = WordPart(context, part, type, nullptr, selected);
    if (!reply.ok()) return reply;
  }
  return {};
}

ObjectReply Slides(OfficeContext& context, std::string_view main) {
  Relationships relations;
  auto reply = context.Relations(main, &relations);
  if (!reply.ok()) return reply;
  std::vector<std::string> ids;
  bool list = false;
  reply = context.Xml(main, [&](const XmlNode& node) -> ObjectReply {
    if (node.kind == XmlKind::kStart && !node.depth &&
        (!OfficeSpace(node.space, "presentationml") ||
         node.name != "presentation"))
      return OfficeInvalid();
    if (!OfficeSpace(node.space, "presentationml")) return {};
    if (node.name == "sldIdLst") {
      if (node.kind == XmlKind::kStart)
        list = true;
      else if (node.kind == XmlKind::kEnd)
        list = false;
    } else if (list && node.name == "sldId" && node.kind == XmlKind::kStart) {
      const auto id = RelationId(node);
      if (id.empty()) return OfficeInvalid();
      if (ids.size() >= 4096) return OfficeCapacity();
      ids.emplace_back(id);
    }
    return {};
  });
  if (!reply.ok()) return reply;
  std::unordered_set<std::string> visited;
  size_t number = 0;
  for (const auto& id : ids) {
    if (!visited.insert(id).second) return OfficeInvalid();
    const auto found = relations.find(id);
    if (found == relations.end() || !RelationType(found->second.type, "slide"))
      return OfficeInvalid();
    std::string part;
    reply = context.Target(main, found->second, &part);
    if (!reply.ok()) return reply;
    reply = context.Text("\n[Slide " + std::to_string(++number) + "]\n");
    if (!reply.ok()) return reply;
    reply = DrawingText(context, part, "sld");
    if (!reply.ok()) return reply;
    Relationships slide_relations;
    reply = context.Relations(part, &slide_relations);
    if (!reply.ok()) return reply;
    const Relationship* notes = nullptr;
    for (const auto& [note_id, relation] : slide_relations) {
      if (!RelationType(relation.type, "notesSlide")) continue;
      if (notes) return OfficeInvalid();
      notes = &relation;
    }
    if (notes) {
      std::string note_part;
      reply = context.Target(part, *notes, &note_part);
      if (!reply.ok()) return reply;
      reply = context.Text("[Notes]\n");
      if (!reply.ok()) return reply;
      reply = DrawingText(context, note_part, "notes");
      if (!reply.ok()) return reply;
    }
  }
  return {};
}
}  // namespace q4t::server::file_detail

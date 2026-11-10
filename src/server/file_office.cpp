#include "file_office_internal.h"

#include "file_rtf.h"
#include "object_protocol.h"

namespace q4t::server::file_detail {
namespace {
ObjectReply ParseDepth(const FileInputContent& input,
                       const FileDocumentLimits& limits,
                       const PackageLimits& package_limits,
                       const BinaryAccess& access, size_t depth,
                       FileDocument* output) {
  *output = {};
  if (depth > limits.max_nested_documents) return OfficeCapacity();
  Package package;
  auto reply = ReadPackage(input.bytes, package_limits, access, &package);
  if (!reply.ok()) return reply;
  FileDocument next;
  OfficeContext context{package, limits, package_limits, access, next};
  context.nested_documents = depth;
  if (const auto* mime = context.Part("mimetype");
      mime &&
      (mime->starts_with("application/vnd.oasis.opendocument.text") ||
       mime->starts_with("application/vnd.oasis.opendocument.spreadsheet") ||
       mime->starts_with("application/vnd.oasis.opendocument.presentation"))) {
    reply = OpenDocument(context);
  } else {
    if (!context.Part("[Content_Types].xml")) return OfficeInvalid();
    bool types = false;
    reply = context.Xml(
        "[Content_Types].xml", [&](const XmlNode& node) -> ObjectReply {
          if (node.kind == XmlKind::kStart && !node.depth) {
            types =
                node.name == "Types" && node.space ==
                                            "http://schemas.openxmlformats.org/"
                                            "package/2006/content-types";
            if (!types) return OfficeInvalid();
          }
          return {};
        });
    if (!reply.ok()) return reply;
    if (!types) return OfficeInvalid();
    Relationships relations;
    reply = context.Relations("", &relations);
    if (!reply.ok()) return reply;
    const Relationship* main = nullptr;
    for (const auto& [id, relation] : relations) {
      if (!RelationType(relation.type, "officeDocument")) continue;
      if (main) return OfficeInvalid();
      main = &relation;
    }
    if (!main) return OfficeInvalid();
    std::string part, kind;
    reply = context.Target("", *main, &part);
    if (!reply.ok()) return reply;
    reply = context.Xml(part, [&](const XmlNode& node) -> ObjectReply {
      if (node.kind != XmlKind::kStart || node.depth) return {};
      if (node.name == "document" &&
          OfficeSpace(node.space, "wordprocessingml"))
        kind = "word";
      else if (node.name == "presentation" &&
               OfficeSpace(node.space, "presentationml"))
        kind = "slides";
      else if (node.name == "workbook" &&
               OfficeSpace(node.space, "spreadsheetml"))
        kind = "sheets";
      else
        return OfficeInvalid();
      return {};
    });
    if (!reply.ok()) return reply;
    if (kind == "word")
      reply = Word(context, part);
    else if (kind == "slides")
      reply = Slides(context, part);
    else if (kind == "sheets")
      reply = Workbook(context, part);
    else
      return OfficeInvalid();
  }
  if (!reply.ok()) return reply;
  next.encoding = "utf-8";
  next.source_sha256 = object_detail::Digest(input.bytes);
  *output = std::move(next);
  return {};
}
}  // namespace

ObjectReply OfficeContext::Alternate(std::string_view source,
                                     std::string_view relation_id) {
  if (nested_documents >= limits.max_nested_documents) return OfficeCapacity();
  Relationships relations;
  auto reply = Relations(source, &relations);
  if (!reply.ok()) return reply;
  const auto found = relations.find(std::string(relation_id));
  if (found == relations.end() || !RelationType(found->second.type, "aFChunk"))
    return OfficeInvalid();
  std::string part;
  reply = Target(source, found->second, &part);
  if (!reply.ok()) return reply;
  FileInputContent input;
  input.filename = part;
  input.bytes = *Part(part);
  FileDocument child;
  const auto format = ClassifyFileDocument(input);
  if (format == FileDocumentFormat::kRtf)
    reply = ParseRtfDocument(input, limits, access, &child);
  else if (format == FileDocumentFormat::kOfficeZip)
    reply = ParseDepth(input, limits, package_limits, access,
                       nested_documents + 1, &child);
  else if (format == FileDocumentFormat::kText ||
           format == FileDocumentFormat::kCsv ||
           format == FileDocumentFormat::kTsv ||
           format == FileDocumentFormat::kIif)
    reply = ParsePlainFileDocument(input, format, limits, access, &child);
  else
    return object_detail::Error(503, "file_nested_codec_required",
                                "Nested document requires its format decoder");
  if (!reply.ok()) return reply;
  reply = Text(child.text);
  if (!reply.ok()) return reply;
  for (const auto& sheet : child.sheets) {
    reply = Text("\n[Sheet " + sheet.name + "]\n");
    if (!reply.ok()) return reply;
    for (const auto& row : sheet.rows) {
      for (size_t i = 0; i < row.size(); ++i) {
        if (i) {
          reply = Text("\t");
          if (!reply.ok()) return reply;
        }
        reply = Text(row[i]);
        if (!reply.ok()) return reply;
      }
      reply = Text("\n");
      if (!reply.ok()) return reply;
    }
  }
  return Text("\n");
}

ObjectReply ParseOfficePackage(const FileInputContent& input,
                               const FileDocumentLimits& limits,
                               const PackageLimits& package_limits,
                               const BinaryAccess& access,
                               FileDocument* output) {
  if (!output)
    return object_detail::Error(500, "file_document_state_invalid",
                                "Missing output");
  *output = {};
  if (!ValidFileDocumentLimits(limits) || !ValidPackageLimits(package_limits))
    return object_detail::Error(500, "file_document_budget_invalid",
                                "Invalid document budget");
  return ParseDepth(input, limits, package_limits, access, 0, output);
}
}  // namespace q4t::server::file_detail

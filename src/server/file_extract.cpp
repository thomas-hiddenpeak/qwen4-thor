#include "q4t/server/file_extract.h"

#include "object_protocol.h"
#include "q4t/server/http_framing.h"

namespace q4t::server {
namespace {
bool AppleMetadata(const FileInputContent& input) {
  const auto dot = input.filename.rfind('.');
  const auto extension =
      dot == input.filename.npos ? "" : http::Lower(input.filename.substr(dot));
  const auto mime = http::Lower(http::Trim(
      std::string_view(input.mime_type).substr(0, input.mime_type.find(';'))));
  return extension == ".pages" || extension == ".numbers" ||
         extension == ".key" || mime == "application/vnd.apple.pages" ||
         mime == "application/vnd.apple.numbers" ||
         mime == "application/vnd.apple.keynote";
}
}  // namespace

ObjectReply ExtractFileDocument(const FileInputContent& input,
                                const FileExtractOptions& options,
                                bool pdf_only, std::string_view detail,
                                const BinaryAccess& access,
                                FileDocument* output, GrammarControl control) {
  if (!output)
    return object_detail::Error(500, "file_document_state_invalid",
                                "Missing output");
  *output = {};
  const auto format = ClassifyFileDocument(input);
  if (pdf_only && format != FileDocumentFormat::kPdf)
    return object_detail::Error(400, "invalid_file_format",
                                "Chat file input requires a PDF");
  if (format == FileDocumentFormat::kPdf)
    return ParsePdfFileDocument(input, options.document, options.pdf, detail,
                                access, output, control);
  if (format == FileDocumentFormat::kOfficeBinary)
    return ParseLegacyFileDocument(input, options.document, options.legacy,
                                   access, output, control);
  // Older iWork documents can be a standalone XML or compressed XML stream.
  // A filename only selects a decoder; that decoder validates actual content.
  if (format == FileDocumentFormat::kOfficeZip && AppleMetadata(input) &&
      !std::string_view(input.bytes).starts_with("PK"))
    return ParseLegacyFileDocument(input, options.document, options.legacy,
                                   access, output, control);
  auto reply =
      ParseNativeFileDocument(input, options.document, access, output, control);
  if (reply.code == "file_document_codec_required")
    return ParseLegacyFileDocument(input, options.document, options.legacy,
                                   access, output, control);
  return reply;
}
}  // namespace q4t::server

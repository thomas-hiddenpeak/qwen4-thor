#include "q4t/server/file_source.h"

#include "file_fetch_internal.h"
#include "object_protocol.h"
#include "q4t/server/file_document.h"
#include "q4t/server/http_framing.h"

namespace q4t::server {
namespace {
using object_detail::Error;
bool Identifier(std::string_view value, size_t limit) {
  if (value.empty() || value.size() > limit) return false;
  for (unsigned char c : value)
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '_' || c == '-'))
      return false;
  return true;
}
std::string ShortcutKind(const FileInputContent& input) {
  const auto dot = input.filename.rfind('.');
  const auto extension =
      dot == input.filename.npos ? "" : http::Lower(input.filename.substr(dot));
  const auto mime = http::Lower(http::Trim(
      std::string_view(input.mime_type).substr(0, input.mime_type.find(';'))));
  if (extension == ".gdoc" || mime == "application/vnd.google-apps.document")
    return "document";
  if (extension == ".gsheet" ||
      mime == "application/vnd.google-apps.spreadsheet")
    return "spreadsheets";
  if (extension == ".gslides" ||
      mime == "application/vnd.google-apps.presentation")
    return "presentation";
  return {};
}
ObjectReply Shortcut(const FileInputContent& input, GoogleDocumentExport* plan,
                     std::string* original) {
  if (input.bytes.size() > 131072)
    return Error(413, "file_document_capacity",
                 "Document shortcut capacity exceeded");
  io::Json value;
  io::JsonParseLimits limits;
  limits.max_depth = 8;
  limits.max_values = 128;
  limits.max_string_bytes = 65536;
  limits.reject_duplicate_keys = true;
  if (!io::ParseJson(input.bytes, &value, false, limits) || !value.IsObject())
    return Error(400, "invalid_file_document", "Invalid document shortcut");
  *original = value.GetString("url");
  auto reply = PlanGoogleDocumentExport(*original, plan);
  if (!reply.ok()) return reply;
  if (plan->url.empty() || plan->kind != ShortcutKind(input))
    return Error(400, "invalid_file_document",
                 "Invalid document shortcut target");
  return {};
}
ObjectReply Fetch(const BinaryAccess& access, const FileInputSource& source,
                  const FileFetchOptions& options, FileInputBudget* budget,
                  const GoogleDocumentExport& plan, FileSourceReceipt* output,
                  GrammarControl control) {
  auto effective = source;
  if (!plan.url.empty()) effective.value = plan.url;
  FileFetchReceipt receipt;
  auto reply =
      FetchFileInput(access, effective, options, budget, &receipt, control);
  if (!reply.ok()) return reply;
  if (!plan.url.empty()) {
    // Authentication/HTML interstitials are not document content. The actual
    // OOXML container and semantics are validated by the isolated extractor.
    if (!std::string_view(receipt.content.bytes).starts_with("PK\x03\x04"))
      return Error(400, "file_source_export_failed",
                   "Document source did not provide an accessible export");
    receipt.content.filename = "google-document." + plan.format;
    output->urls.emplace_back(source.value);
  }
  output->content = std::move(receipt.content);
  output->urls.insert(output->urls.end(), receipt.urls.begin(),
                      receipt.urls.end());
  output->export_format = plan.format;
  return {};
}
}  // namespace

ObjectReply PlanGoogleDocumentExport(std::string_view text,
                                     GoogleDocumentExport* output) {
  if (!output)
    return Error(500, "file_source_state_invalid", "Missing source output");
  *output = {};
  file_detail::Url url;
  if (!file_detail::ParseUrl(text, &url))
    return Error(400, "invalid_file_url", "Invalid document URL");
  if (url.host != "docs.google.com" || !url.tls || url.port != 443) return {};
  std::string_view target(url.target);
  const auto query_at = target.find('?');
  auto path = target.substr(0, query_at);
  if (!path.starts_with('/')) return {};
  path.remove_prefix(1);
  const auto kind_end = path.find('/');
  if (kind_end == path.npos) return {};
  const auto kind = path.substr(0, kind_end);
  if (kind != "document" && kind != "spreadsheets" && kind != "presentation")
    return {};
  path.remove_prefix(kind_end + 1);
  if (path.starts_with("u/")) {
    path.remove_prefix(2);
    const auto end = path.find('/');
    if (end == path.npos || end == 0 || end > 8) return {};
    for (char c : path.substr(0, end))
      if (c < '0' || c > '9') return {};
    path.remove_prefix(end + 1);
  }
  if (!path.starts_with("d/")) return {};
  path.remove_prefix(2);
  const auto id_end = path.find('/');
  const auto id = path.substr(0, id_end);
  if (id == "e") return {};  // Published-content endpoint, already explicit.
  if (!Identifier(id, 1024))
    return Error(400, "invalid_file_url", "Invalid document identifier");
  const auto action =
      id_end == path.npos ? std::string_view{} : path.substr(id_end);
  if (action != "" && action != "/" && action != "/edit" && action != "/view" &&
      action != "/preview" && action != "/copy")
    return {};  // Includes caller-selected export format/gid.
  std::string resource_key;
  if (query_at != target.npos) {
    auto query = target.substr(query_at + 1);
    while (!query.empty()) {
      const auto end = query.find('&');
      const auto item = query.substr(0, end);
      if (item.starts_with("resourcekey=")) {
        const auto key = item.substr(12);
        if (!resource_key.empty() || !Identifier(key, 512))
          return Error(400, "invalid_file_url",
                       "Invalid document resource key");
        resource_key = key;
      }
      if (end == query.npos) break;
      query.remove_prefix(end + 1);
    }
  }
  GoogleDocumentExport next;
  next.kind = kind;
  next.format = kind == "document"       ? "docx"
                : kind == "spreadsheets" ? "xlsx"
                                         : "pptx";
  next.url = "https://docs.google.com/" + next.kind + "/d/" + std::string(id) +
             (kind == "presentation" ? "/export/pptx"
                                     : "/export?format=" + next.format);
  if (!resource_key.empty())
    next.url += (kind == "presentation" ? "?" : "&") +
                std::string("resourcekey=") + resource_key;
  *output = std::move(next);
  return {};
}

ObjectReply ResolveFileInput(ObjectStore* objects, const BinaryAccess& access,
                             const FileInputSource& source,
                             const FileFetchOptions& fetch,
                             FileInputBudget* budget, FileSourceReceipt* output,
                             GrammarControl control) {
  if (!output || !budget)
    return Error(500, "file_source_state_invalid", "Missing source state");
  *output = {};
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable",
                 "Authenticated scope required");
  auto bounded = access;
  bounded.cancelled = [&] {
    return (access.cancelled && access.cancelled()) ||
           (control.cancelled && control.cancelled());
  };
  if (bounded.cancelled())
    return Error(499, "request_cancelled", "File acquisition cancelled");
  if (std::chrono::steady_clock::now() >= access.deadline)
    return Error(408, "request_deadline_exceeded",
                 "File acquisition timed out");
  if (source.pdf_only && source.kind == FileInputSource::Kind::kUrl)
    return Error(400, "invalid_file_source",
                 "Chat file input requires local PDF content");
  FileSourceReceipt next;
  if (source.kind == FileInputSource::Kind::kUrl) {
    GoogleDocumentExport plan;
    auto reply = PlanGoogleDocumentExport(source.value, &plan);
    if (!reply.ok()) return reply;
    reply = Fetch(bounded, source, fetch, budget, plan, &next, control);
    if (!reply.ok()) return reply;
  } else {
    FileInputContent content;
    auto reply = ReadLocalFileInput(objects, bounded, source, budget, &content);
    if (!reply.ok()) return reply;
    if (source.pdf_only &&
        ClassifyFileDocument(content) != FileDocumentFormat::kPdf)
      return Error(400, "invalid_file_format",
                   "Chat file input requires a PDF");
    if (ShortcutKind(content).empty()) {
      next.content = std::move(content);
    } else {
      GoogleDocumentExport plan;
      std::string original;
      reply = Shortcut(content, &plan, &original);
      if (!reply.ok()) return reply;
      FileInputSource remote = source;
      remote.kind = FileInputSource::Kind::kUrl;
      remote.value = original;
      if (budget->used_bytes == budget->max_bytes)
        return Error(413, "file_input_capacity_exceeded",
                     "File input budget exceeded");
      // One logical file, while every acquired byte (including the shortcut)
      // pays the original request budget. Failed fetch attempts are not
      // refunded.
      FileInputBudget derived;
      derived.max_files = 1;
      derived.max_bytes = budget->max_bytes - budget->used_bytes;
      reply = Fetch(bounded, remote, fetch, &derived, plan, &next, control);
      budget->used_bytes += derived.used_bytes;
      if (!reply.ok()) return reply;
      next.indirect_source = std::move(content);
    }
  }
  *output = std::move(next);
  return {};
}
}  // namespace q4t::server

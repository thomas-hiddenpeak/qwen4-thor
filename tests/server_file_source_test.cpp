#include "q4t/server/file_source.h"

#include <cstdio>

#include "../src/server/object_protocol.h"
#include "q4t/server/file_extract.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"source", Clock::now() + 30s, {}}; }
std::string Encoded(std::string_view input) {
  constexpr char alphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string output;
  for (size_t i = 0; i < input.size(); i += 3) {
    uint32_t value = static_cast<unsigned char>(input[i]) << 16;
    if (i + 1 < input.size())
      value |= static_cast<unsigned char>(input[i + 1]) << 8;
    if (i + 2 < input.size()) value |= static_cast<unsigned char>(input[i + 2]);
    output += alphabet[value >> 18];
    output += alphabet[(value >> 12) & 63];
    output += i + 1 < input.size() ? alphabet[(value >> 6) & 63] : '=';
    output += i + 2 < input.size() ? alphabet[value & 63] : '=';
  }
  return output;
}
}  // namespace

// Explicit, bounded external-network fixture, not part of public CPU tests.
// It reports counts/identities, never source URLs, credentials or document
// text.
int FileSourceFixture(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "--file-source-fixture")
    return -1;
  if (argc != 3 && argc != 4) return 2;
  FileFetchOptions options;
  if (argc == 4) {
    Json policy;
    if (!Parse(argv[3], &policy) ||
        !ParseFileFetchOptions(policy, &options).ok())
      return 2;
  }
  FileInputSource source;
  const auto shortcut = OutputJson(Object({{"url", String(argv[2])}}));
  const auto encoded = Encoded(shortcut);
  source.value = encoded;
  source.filename = "official-sample.gsheet";
  FileInputBudget budget;
  FileSourceReceipt receipt;
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  const auto access = Access();
  auto reply = ResolveFileInput(nullptr, access, source, options, &budget,
                                &receipt, control);
  FileDocument document;
  if (reply.ok())
    reply = ExtractFileDocument(receipt.content, {}, false, "auto", access,
                                &document, control);
  size_t rows = 0;
  bool sample_header = false;
  for (const auto& sheet : document.sheets) {
    rows += sheet.rows.size();
    for (const auto& row : sheet.rows)
      for (const auto& cell : row) sample_header |= cell == "Student Name";
  }
  const auto result =
      Object({{"status", Number(reply.http_status)},
              {"code", String(reply.code)},
              {"bytes", Number(receipt.content.bytes.size())},
              {"sha256", String(receipt.content.sha256)},
              {"export_format", String(receipt.export_format)},
              {"hops", Number(receipt.urls.size())},
              {"sheets", Number(document.sheets.size())},
              {"rows", Number(rows)},
              {"official_sample_header", Boolean(sample_header)},
              {"shortcut_preserved",
               Boolean(receipt.indirect_source &&
                       receipt.indirect_source->bytes == shortcut)},
              {"acquisition_bytes", Number(budget.used_bytes)},
              {"logical_files", Number(budget.used_files)},
              {"started", Number(control.counters->started.load())},
              {"reaped", Number(control.counters->reaped.load())},
              {"active", Number(control.counters->active.load())}});
  std::puts(OutputJson(result).c_str());
  return reply.ok() && sample_header && receipt.indirect_source &&
                 budget.used_files == 1 &&
                 budget.used_bytes ==
                     shortcut.size() + receipt.content.bytes.size()
             ? 0
             : 1;
}

Q4T_TEST(file_source_google_export_plans_preserve_identity_and_resource_key) {
  for (const auto* kind : {"document", "spreadsheets", "presentation"}) {
    const std::string url =
        std::string("https://docs.google.com/") + kind +
        "/u/2/d/abc_X-123/edit?usp=sharing&resourcekey=0-Key#gid=7";
    GoogleDocumentExport plan;
    Q4T_CHECK(PlanGoogleDocumentExport(url, &plan).ok());
    const std::string expected =
        std::string("https://docs.google.com/") + kind + "/d/abc_X-123/" +
        (std::string_view(kind) == "presentation" ? "export/pptx?"
         : std::string_view(kind) == "document"   ? "export?format=docx&"
                                                  : "export?format=xlsx&") +
        "resourcekey=0-Key";
    Q4T_CHECK(plan.url == expected && plan.kind == kind);
  }
  return true;
}
Q4T_TEST(file_source_explicit_exports_and_unrelated_urls_stay_intact) {
  for (const auto* url :
       {"https://docs.google.com/spreadsheets/d/abc/export?format=csv&gid=7",
        "https://docs.google.com/document/d/e/published/pub",
        "https://docs.google.com/presentation/d/abc/export/pdf",
        "https://example.com/document/d/abc/edit",
        "https://docs.google.com.evil.example/document/d/abc/edit",
        "http://docs.google.com/document/d/abc/edit",
        "https://docs.google.com:444/document/d/abc/edit"}) {
    GoogleDocumentExport plan;
    Q4T_CHECK(PlanGoogleDocumentExport(url, &plan).ok() && plan.url.empty());
  }
  return true;
}
Q4T_TEST(file_source_google_url_rejects_credentials_and_ambiguous_keys) {
  for (const auto* url :
       {"https://user:secret@docs.google.com/document/d/abc/edit",
        "https://docs.google.com/document/d/abc%2Fdef/edit",
        "https://docs.google.com/document/d/abc/edit?resourcekey=",
        "https://docs.google.com/document/d/abc/"
        "edit?resourcekey=a&resourcekey=b",
        "https://docs.google.com/document/d/abc/edit?resourcekey=a%26x=y"}) {
    GoogleDocumentExport plan;
    Q4T_CHECK(PlanGoogleDocumentExport(url, &plan).http_status == 400 &&
              plan.url.empty());
  }
  return true;
}
Q4T_TEST(file_source_inline_identity_and_chat_restriction) {
  FileInputSource source;
  source.value = "YWJj";
  source.filename = "report.txt";
  FileInputBudget budget;
  FileSourceReceipt receipt;
  Q4T_CHECK(
      ResolveFileInput(nullptr, Access(), source, {}, &budget, &receipt).ok());
  Q4T_CHECK(receipt.content.bytes == "abc" &&
            receipt.content.sha256 == Digest("abc") && receipt.urls.empty() &&
            !receipt.indirect_source && budget.used_bytes == 3);
  source.pdf_only = true;
  Q4T_CHECK(ResolveFileInput(nullptr, Access(), source, {}, &budget, &receipt)
                    .http_status == 400 &&
            receipt.content.bytes.empty());
  return true;
}
Q4T_TEST(file_source_binary_signatures_override_google_shortcut_metadata) {
  for (const auto* bytes : {"%PDF-1.7", "PK\x03\x04", "{\\rtf1 "}) {
    const auto encoded = Encoded(bytes);
    FileInputSource source;
    source.filename = "display-only.gdoc";
    source.value = encoded;
    source.pdf_only = std::string_view(bytes).starts_with("%PDF");
    FileInputBudget budget;
    FileSourceReceipt receipt;
    GrammarControl control;
    control.counters = std::make_shared<GrammarWorkerCounters>();
    Q4T_CHECK(ResolveFileInput(nullptr, Access(), source, {}, &budget, &receipt, control).ok());
    Q4T_CHECK(receipt.content.bytes == bytes && !receipt.indirect_source &&
              receipt.urls.empty() && control.counters->started == 0);
  }
  return true;
}
Q4T_TEST(file_source_google_shortcut_requires_matching_type_and_fetch_policy) {
  const auto encoded =
      Encoded("{\"url\":\"https://docs.google.com/document/d/abc/edit\"}");
  FileInputSource source;
  source.value = encoded;
  source.filename = "document.gsheet";
  FileInputBudget budget;
  FileSourceReceipt receipt;
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  Q4T_CHECK(ResolveFileInput(nullptr, Access(), source, {}, &budget, &receipt,
                             control)
                .http_status == 400);
  source.filename = "document.gdoc";
  FileFetchOptions options;
  options.allowed_hosts = {"approved.example"};
  Q4T_CHECK(ResolveFileInput(nullptr, Access(), source, options, &budget,
                             &receipt, control)
                .http_status == 403);
  source.pdf_only = true;
  Q4T_CHECK(ResolveFileInput(nullptr, Access(), source, {}, &budget, &receipt,
                             control)
                .http_status == 400);
  Q4T_CHECK(control.counters->started == 0 && receipt.content.bytes.empty() &&
            !receipt.indirect_source);
  return true;
}
Q4T_TEST(file_source_shortcut_byte_budget_and_control_cancel_before_fetch) {
  const std::string bytes =
      "{\"url\":\"https://docs.google.com/document/d/abc/edit\"}";
  const auto encoded = Encoded(bytes);
  FileInputSource source;
  source.filename = "document.gdoc";
  source.value = encoded;
  FileInputBudget budget;
  budget.max_bytes = bytes.size();
  FileSourceReceipt receipt;
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  Q4T_CHECK(ResolveFileInput(nullptr, Access(), source, {}, &budget, &receipt,
                             control)
                .http_status == 413);
  Q4T_CHECK(budget.used_files == 1 && budget.used_bytes == bytes.size());
  control.cancelled = [] { return true; };
  Q4T_CHECK(ResolveFileInput(nullptr, Access(), source, {}, &budget, &receipt,
                             control)
                .http_status == 499);
  Q4T_CHECK(control.counters->started == 0 && receipt.content.bytes.empty());
  return true;
}

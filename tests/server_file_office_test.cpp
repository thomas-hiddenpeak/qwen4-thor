#include "q4t/server/file_document_worker.h"

#include <cstdio>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
FileInputContent Input(const char* name) {
  FileInputContent input;
  input.filename = name;
  std::ifstream stream(std::filesystem::path(__FILE__).parent_path() /
                           "fixtures/file_office" / name,
                       std::ios::binary);
  input.bytes = std::string(std::istreambuf_iterator<char>(stream), {});
  return input;
}
ObjectReply Read(const char* name, FileDocument* output,
                 FileDocumentLimits limits = {}) {
  const auto input = Input(name);
  if (input.bytes.empty())
    return object_detail::Error(500, "fixture_missing", name);
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  auto reply = ParseNativeFileDocument(
      input, limits, {"office-tests", Clock::now() + 3s, {}}, output, control);
  if (control.counters->started != 1 || control.counters->reaped != 1 ||
      control.counters->active != 0)
    return object_detail::Error(500, "fixture_ownership_failed", name);
  if (reply.ok() && output->source_sha256 != object_detail::Digest(input.bytes))
    return object_detail::Error(500, "fixture_digest_failed", name);
  if (!reply.ok())
    std::printf("office %s: %d %s\n", name, reply.http_status,
                reply.code.c_str());
  return reply;
}
bool ContainsOnce(const std::string& text, const std::string& value) {
  const auto first = text.find(value);
  return first != text.npos &&
         text.find(value, first + value.size()) == text.npos;
}
}  // namespace

Q4T_TEST(file_office_word_actual_stories_tables_and_rtf_import) {
  FileDocument output;
  Q4T_CHECK(Read("word.docx", &output).ok() && output.sheets.empty());
  for (const auto* value :
       {"Report 中文 😀\ttail", "Left\n\tRight", "Real header",
        "Selected footnote", "Actual comment", "Imported 中文"})
    Q4T_CHECK(ContainsOnce(output.text, value));
  for (const auto* value : {"DELETED SECRET", "UNUSED FOOTNOTE"})
    Q4T_CHECK(output.text.find(value) == output.text.npos);
  return true;
}
Q4T_TEST(file_office_strict_namespaces_and_compatibility_choose_one_branch) {
  FileDocument normal, strict;
  Q4T_CHECK(Read("word.docx", &normal).ok() &&
            Read("strict.docx", &strict).ok() && normal.text == strict.text);
  for (const auto* value : {"Choice once", "Fallback once"})
    Q4T_CHECK(ContainsOnce(strict.text, value));
  Q4T_CHECK(strict.text.find("BAD CHOICE") == strict.text.npos &&
            strict.text.find("BAD FALLBACK") == strict.text.npos);
  return true;
}
Q4T_TEST(file_office_slides_follow_relationship_order_and_include_notes) {
  FileDocument output;
  Q4T_CHECK(Read("slides.pptx", &output).ok());
  Q4T_CHECK(output.text ==
            "\n[Slide 1]\nFirst actual slide\n[Notes]\n"
            "Speaker notes\n\n[Slide 2]\nSecond actual slide\n");
  return true;
}
Q4T_TEST(file_office_xlsx_actual_coordinates_strings_styles_and_formula_cache) {
  FileDocument output;
  Q4T_CHECK(Read("sheets.xlsx", &output).ok() && output.sheets.size() == 3);
  const auto& sheet = output.sheets[0];
  Q4T_CHECK(sheet.name == "真实数据" && sheet.source_rows == 3 &&
            sheet.columns == 5 && sheet.rows.size() == 3 &&
            sheet.rows[1].empty());
  Q4T_CHECK(sheet.rows[0] == std::vector<std::string>({"中文 😀 literal_x0041_",
                                                       "", "inline\nline"}));
  Q4T_CHECK(sheet.rows[2][0].starts_with(" +42.0 ") &&
            ContainsOnce(sheet.rows[2][0], "number format: #,##0.00") &&
            ContainsOnce(sheet.rows[2][0], "cached formula result") &&
            ContainsOnce(sheet.rows[2][0], "SUM(20,22)") &&
            ContainsOnce(sheet.rows[2][1], "builtin 14; date system: 1904") &&
            sheet.rows[2][2] == "true" &&
            ContainsOnce(sheet.rows[2][3], "[t=shared][si=7]") &&
            ContainsOnce(sheet.rows[2][3], "cached value unavailable") &&
            sheet.rows[2][4] == "#DIV/0!");
  Q4T_CHECK(output.sheets[2].name == "Chart" && output.sheets[2].rows.empty());
  return true;
}
Q4T_TEST(file_office_xlsx_first_thousand_and_implicit_general_style) {
  FileDocument output;
  Q4T_CHECK(Read("sheets.xlsx", &output).ok());
  const auto& tail = output.sheets[1];
  Q4T_CHECK(tail.name == "Tail" && tail.source_rows == 1002 &&
            tail.rows.size() == 1000 && tail.rows.front()[0] == "row 1" &&
            tail.rows.back()[0] == "row 1000");
  Q4T_CHECK(Read("implicit.xlsx", &output).ok() && output.sheets.size() == 1 &&
            output.sheets[0].rows ==
                std::vector<std::vector<std::string>>({{"7"}}));
  return true;
}
Q4T_TEST(file_office_odf_text_slides_and_repeated_sparse_sheet_rows) {
  FileDocument output;
  Q4T_CHECK(Read("text.odt", &output).ok() &&
            output.text == "真实   spaces\ttab\nline\n");
  Q4T_CHECK(Read("slides.odp", &output).ok() &&
            output.text == "\n[Slide Opening]\n真实   spaces\ttab\nline\n");
  Q4T_CHECK(Read("sheets.ods", &output).ok() && output.sheets.size() == 2);
  const auto& sheet = output.sheets[0];
  Q4T_CHECK(sheet.name == "Repeated" && sheet.source_rows == 1006 &&
            sheet.rows.size() == 1000 && sheet.columns == 5 &&
            sheet.rows[0][0] == "alpha" && sheet.rows[0][1] == "alpha" &&
            sheet.rows[0][2].empty() && sheet.rows[0][3].empty() &&
            ContainsOnce(sheet.rows[0][4], "5 [display: 5.00]") &&
            ContainsOnce(sheet.rows[0][4], "of:=SUM([.A1:.B1])") &&
            sheet.rows.back()[0] == "2026-11-10");
  Q4T_CHECK(output.sheets[1].rows.size() == 1 &&
            output.sheets[1].rows[0].empty() && output.sheets[1].columns == 0);
  return true;
}
Q4T_TEST(file_office_bad_content_and_external_relationships_publish_nothing) {
  for (const auto* name :
       {"broken.docx", "bad-target.docx", "external.docx", "bad-shared.xlsx",
        "bad-numeric.xlsx", "bad-position.xlsx"}) {
    FileDocument output;
    output.text = "stale";
    Q4T_CHECK(Read(name, &output).http_status == 400 && output.text.empty() &&
              output.sheets.empty() && output.source_sha256.empty());
  }
  FileDocument output;
  Q4T_CHECK(Read("word.docx", &output).ok());
  return true;
}
Q4T_TEST(file_office_odf_sparse_rows_trim_only_trailing_grid_and_drawing_pages) {
  FileDocument output;
  Q4T_CHECK(Read("sparse.ods", &output).ok() && output.sheets.size() == 1);
  const auto& sheet = output.sheets[0];
  Q4T_CHECK(sheet.source_rows == 5 && sheet.rows.size() == 5 &&
            sheet.columns == 1 && sheet.rows[0][0] == "first" &&
            sheet.rows[1].empty() && sheet.rows[2].empty() &&
            sheet.rows[3].empty() && sheet.rows[4][0] == "fifth");
  Q4T_CHECK(Read("drawing.odg", &output).ok() &&
            output.text == "\n[Page Canvas]\n真实   spaces\ttab\nline\n");
  return true;
}
Q4T_TEST(file_office_nested_content_and_independent_capacity_budgets) {
  FileDocument output;
  Q4T_CHECK(Read("nested.docx", &output).ok() &&
            ContainsOnce(output.text, "Imported 中文"));
  for (int kind = 0; kind < 8; ++kind) {
    FileDocumentLimits limits;
    const char* name = "sheets.xlsx";
    switch (kind) {
      case 0:
        limits.max_text_bytes = 32;
        break;
      case 1:
        limits.max_cell_bytes = 3;
        break;
      case 2:
        limits.max_columns = 2;
        break;
      case 3:
        limits.max_cells = 2;
        break;
      case 4:
        limits.max_sheets = 1;
        break;
      case 5:
        limits.package.max_entry_bytes = 100;
        break;
      case 6:
        limits.package.max_xml_nodes = 10;
        break;
      case 7:
        limits.max_nested_documents = 0;
        name = "nested.docx";
        break;
    }
    Q4T_CHECK(Read(name, &output, limits).http_status == 413 &&
              output.text.empty() && output.sheets.empty() &&
              output.source_sha256.empty());
  }
  return true;
}

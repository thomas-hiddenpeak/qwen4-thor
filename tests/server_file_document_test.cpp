#include "q4t/server/file_document.h"

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"client:documents", Clock::now() + 3s, {}}; }
FileInputContent Input(std::string bytes, std::string name = "document.txt") {
  FileInputContent input;
  input.bytes = std::move(bytes);
  input.filename = std::move(name);
  return input;
}
ObjectReply Parse(const FileInputContent& input, FileDocument* document,
                  FileDocumentLimits limits = {},
                  BinaryAccess access = Access()) {
  return ParsePlainFileDocument(input, ClassifyFileDocument(input), limits,
                                access, document);
}
std::string Wide16(std::u16string_view text, bool little) {
  std::string bytes = little ? "\xff\xfe" : "\xfe\xff";
  for (uint16_t c : text) {
    bytes.push_back(little ? c & 255 : c >> 8);
    bytes.push_back(little ? c >> 8 : c & 255);
  }
  return bytes;
}
std::string Wide32(std::u32string_view text, bool little) {
  std::string bytes =
      little ? std::string("\xff\xfe\0\0", 4) : std::string("\0\0\xfe\xff", 4);
  for (uint32_t c : text)
    for (int i = 0; i < 4; ++i)
      bytes.push_back((c >> (8 * (little ? i : 3 - i))) & 255);
  return bytes;
}
}  // namespace

Q4T_TEST(file_document_magic_and_metadata_dispatch_do_not_read_binary_as_text) {
  Q4T_CHECK(ClassifyFileDocument(Input("%PDF-1.7\n", "file.csv")) ==
            FileDocumentFormat::kPdf);
  Q4T_CHECK(ClassifyFileDocument(Input("PK\x03\x04", "file.txt")) ==
            FileDocumentFormat::kOfficeZip);
  Q4T_CHECK(ClassifyFileDocument(Input("\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")) ==
            FileDocumentFormat::kOfficeBinary);
  Q4T_CHECK(ClassifyFileDocument(Input("{\\rtf1 example}")) ==
            FileDocumentFormat::kRtf);
  Q4T_CHECK(ClassifyFileDocument(Input("a,b", "../metadata.CSV")) ==
            FileDocumentFormat::kCsv);
  auto input = Input("bad document");
  input.mime_type = "application/pdf";
  Q4T_CHECK(ClassifyFileDocument(input) == FileDocumentFormat::kPdf);
  FileDocument result;
  Q4T_CHECK(ParsePlainFileDocument(input, FileDocumentFormat::kText, {},
                                   Access(), &result)
                    .http_status == 400 &&
            result.text.empty());
  return true;
}
Q4T_TEST(file_document_utf8_preserves_code_and_unicode_without_normalization) {
  const auto input = Input(
      "# 原始文档\r\nemoji 😀 / e\xcc\x81\nprint(\"text\")\n", "sample.md");
  FileDocument document;
  Q4T_CHECK(Parse(input, &document).ok() && document.text == input.bytes &&
            document.source_sha256 == Digest(input.bytes) &&
            document.encoding == "utf-8" && document.sheets.empty());
  const auto bom = Input("\xef\xbb\xbf" + input.bytes);
  Q4T_CHECK(Parse(bom, &document).ok() && document.text == input.bytes &&
            document.source_sha256 == Digest(bom.bytes));
  Q4T_CHECK(Parse(Input(""), &document).ok() && document.text.empty() &&
            document.source_sha256 == Digest(""));
  return true;
}
Q4T_TEST(file_document_utf16_utf32_bom_and_surrogates_are_exact) {
  FileDocument document;
  const std::string expected = "中文 😀\n";
  for (bool little : {false, true}) {
    auto input = Input(Wide16(u"中文 😀\n", little));
    Q4T_CHECK(Parse(input, &document).ok() && document.text == expected &&
              document.encoding == (little ? "utf-16le" : "utf-16be"));
    input = Input(Wide32(U"中文 😀\n", little));
    Q4T_CHECK(Parse(input, &document).ok() && document.text == expected &&
              document.encoding == (little ? "utf-32le" : "utf-32be"));
  }
  return true;
}
Q4T_TEST(file_document_invalid_encoding_never_replaces_or_publishes_text) {
  for (const auto& bytes :
       {std::string("\xc0\xaf"), std::string("\xed\xa0\x80"),
        std::string("\xf4\x90\x80\x80"), std::string("\x80"),
        std::string("\xe2\x82"), std::string("a\0b", 3),
        std::string("\xff\xfe\x01", 3), Wide16(std::u16string(1, 0xd800), true),
        Wide16(std::u16string(1, 0xdc00), false),
        Wide32(std::u32string(1, 0x110000), true)}) {
    FileDocument document;
    document.text = "previous";
    Q4T_CHECK(Parse(Input(bytes), &document).http_status == 400 &&
              document.text.empty() && document.source_sha256.empty());
  }
  return true;
}
Q4T_TEST(file_document_csv_has_real_cells_quotes_newlines_and_empty_fields) {
  const auto input =
      Input("name,value,note\r\n\"a,b\",\"two\r\nlines\",\"a\"\"b\"\r\nlast,,",
            "data.csv");
  FileDocument document;
  Q4T_CHECK(Parse(input, &document).ok() && document.text.empty() &&
            document.sheets.size() == 1);
  const auto& sheet = document.sheets[0];
  Q4T_CHECK(sheet.source_rows == 3 && sheet.columns == 3 &&
            sheet.rows.size() == 3 &&
            sheet.rows[1] ==
                std::vector<std::string>({"a,b", "two\r\nlines", "a\"b"}) &&
            sheet.rows[2] == std::vector<std::string>({"last", "", ""}));
  Q4T_CHECK(document.source_sha256 == Digest(input.bytes));
  return true;
}
Q4T_TEST(file_document_tsv_iif_and_empty_or_blank_records_are_explicit) {
  for (const auto* name : {"source.tsv", "source.iif"}) {
    FileDocument document;
    Q4T_CHECK(
        Parse(Input("!TRNS\tAMOUNT\nTRNS\t42\nENDTRNS\n", name), &document)
            .ok());
    Q4T_CHECK(document.sheets[0].source_rows == 3 &&
              document.sheets[0].rows[1] ==
                  std::vector<std::string>({"TRNS", "42"}) &&
              document.sheets[0].rows[2].size() == 1);
  }
  for (const auto& [text, rows] :
       std::vector<std::pair<std::string, size_t>>{{"", 0},
                                                   {"\n", 1},
                                                   {"\r\n\r\n", 2},
                                                   {"\"\"", 1},
                                                   {",", 1},
                                                   {"a\n", 1}}) {
    FileDocument document;
    Q4T_CHECK(Parse(Input(text, "empty.csv"), &document).ok() &&
              document.sheets[0].source_rows == rows);
  }
  return true;
}
Q4T_TEST(file_document_sheet_keeps_first_thousand_rows_with_full_source_count) {
  std::string csv;
  for (int i = 0; i < 1003; ++i) csv += std::to_string(i) + ",actual\n";
  FileDocument document;
  Q4T_CHECK(Parse(Input(csv, "rows.csv"), &document).ok());
  const auto& sheet = document.sheets[0];
  Q4T_CHECK(sheet.source_rows == 1003 && sheet.rows.size() == 1000 &&
            sheet.rows.front()[0] == "0" && sheet.rows.back()[0] == "999");
  // No synthetic model summary/header inference is produced by extraction.
  Q4T_CHECK(document.text.empty());
  csv += "\"unterminated";
  Q4T_CHECK(Parse(Input(csv, "rows.csv"), &document).http_status == 400 &&
            document.sheets.empty());
  return true;
}
Q4T_TEST(file_document_bad_csv_quotes_are_rejected_as_a_whole) {
  for (const auto* csv :
       {"\"unclosed", "abc\"def", "\"closed\"suffix", "\"a\" \n"}) {
    FileDocument document;
    Q4T_CHECK(Parse(Input(csv, "bad.csv"), &document).http_status == 400 &&
              document.sheets.empty() && document.source_sha256.empty());
  }
  return true;
}
Q4T_TEST(file_document_budgets_apply_to_actual_utf8_cells_and_structure) {
  FileDocument document;
  FileDocumentLimits limits;
  limits.max_text_bytes = 4;
  Q4T_CHECK(Parse(Input(Wide16(u"中", true)), &document, limits).ok() &&
            document.text.size() == 3);
  Q4T_CHECK(
      Parse(Input(Wide16(u"中文", true)), &document, limits).http_status ==
          413 &&
      document.text.empty());
  Q4T_CHECK(
      Parse(Input("abc,de", "table.csv"), &document, limits).http_status ==
          413 &&
      document.sheets.empty());
  limits = {};
  limits.max_cell_bytes = 2;
  Q4T_CHECK(Parse(Input("abc,x", "table.csv"), &document, limits).http_status ==
            413);
  limits = {};
  limits.max_columns = 2;
  Q4T_CHECK(Parse(Input("a,b,c", "table.csv"), &document, limits).http_status ==
            413);
  limits = {};
  limits.max_cells = 3;
  Q4T_CHECK(
      Parse(Input("a,b\nc,d", "table.csv"), &document, limits).http_status ==
      413);
  limits = {};
  limits.max_text_bytes = 0;
  Q4T_CHECK(Parse(Input("x"), &document, limits).http_status == 500);
  return true;
}
Q4T_TEST(file_document_cancel_and_deadline_leave_no_extracted_output) {
  FileDocument document;
  auto access = Access();
  int checks = 0;
  access.cancelled = [&] { return ++checks == 3; };
  Q4T_CHECK(Parse(Input(std::string(10000, 'x')), &document, {}, access)
                    .http_status == 499 &&
            document.text.empty() && document.source_sha256.empty());
  access = Access();
  access.deadline = Clock::now() - 1ms;
  Q4T_CHECK(
      Parse(Input("a,b", "table.csv"), &document, {}, access).http_status ==
          408 &&
      document.sheets.empty());
  return true;
}

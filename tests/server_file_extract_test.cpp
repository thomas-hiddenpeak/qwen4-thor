#include "q4t/server/file_extract.h"

#include <unistd.h>

#include <cstdio>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
namespace fs = std::filesystem;
using Clock = std::chrono::steady_clock;
FileInputContent Input(const char* folder, const char* name) {
  std::ifstream file(
      fs::path(__FILE__).parent_path() / "fixtures" / folder / name,
      std::ios::binary);
  FileInputContent input;
  input.filename = name;
  input.bytes.assign(std::istreambuf_iterator<char>(file), {});
  return input;
}
struct Fixture {
  fs::path base = fs::path(__FILE__).parent_path().parent_path() /
                  ".q4t-work/public-host-fixtures/file-extract" /
                  std::to_string(getpid());
  FileExtractOptions options;
  GrammarControl control;
  Fixture() {
    fs::create_directories(base);
    options.legacy.work_directory = (base / "codec").string();
    control.counters = std::make_shared<GrammarWorkerCounters>();
  }
  ~Fixture() {
    if (!control.counters->active) fs::remove_all(base);
  }
  ObjectReply Read(const FileInputContent& input, FileDocument* document,
                   bool pdf_only = false) {
    auto reply = ExtractFileDocument(input, options, pdf_only, "auto",
                                     {"extract", Clock::now() + 30s, {}},
                                     document, control);
    if (!reply.ok())
      std::printf("extract %s: %d %s\n", input.filename.c_str(),
                  reply.http_status, reply.code.c_str());
    return reply;
  }
  bool Clean() const {
    if (control.counters->active ||
        control.counters->started != control.counters->reaped)
      return false;
    if (!fs::exists(options.legacy.work_directory)) return true;
    for (const auto& item :
         fs::directory_iterator(options.legacy.work_directory))
      if (item.path().filename() != ".q4t-codec-root") return false;
    return true;
  }
};
bool Provenance(const FileInputContent& input, const FileDocument& document,
                const char* format) {
  const auto& conversion = document.conversion;
  return document.source_sha256 == object_detail::Digest(input.bytes) &&
         conversion.codec == "libreofficekit" && !conversion.product.empty() &&
         !conversion.version.empty() && !conversion.build_id.empty() &&
         conversion.output_format == format &&
         conversion.output_sha256.size() == 64 &&
         conversion.output_sha256 != document.source_sha256;
}
bool Cell(const FileDocument& document, const char* value) {
  for (const auto& sheet : document.sheets)
    for (const auto& row : sheet.rows)
      for (const auto& cell : row)
        if (cell == value) return true;
  return false;
}
}  // namespace

Q4T_TEST(file_extract_actual_pages_text_and_conversion_identity) {
  Fixture fixture;
  const auto input = Input("file_adapters", "pages.pages");
  FileDocument document;
  Q4T_CHECK(!input.bytes.empty() && fixture.Read(input, &document).ok());
  Q4T_CHECK(document.text.find("A5 page.") != document.text.npos &&
            Provenance(input, document, "odt") && fixture.Clean());
  return true;
}
Q4T_TEST(file_extract_actual_numbers_coordinates_and_values) {
  Fixture fixture;
  const auto input = Input("file_adapters", "numbers.numbers");
  FileDocument document;
  Q4T_CHECK(!input.bytes.empty() && fixture.Read(input, &document).ok());
  Q4T_CHECK(Cell(document, "B2") && Cell(document, "C2") &&
            Cell(document, "3") && Provenance(input, document, "ods") &&
            fixture.Clean());
  bool coordinates = false;
  for (const auto& sheet : document.sheets)
    coordinates |= sheet.rows.size() > 1 && sheet.rows[1].size() > 3 &&
                   sheet.rows[1][1] == "B2" && sheet.rows[1][3] == "3";
  Q4T_CHECK(coordinates);
  return true;
}
Q4T_TEST(file_extract_actual_keynote_empty_slide_and_provenance) {
  Fixture fixture;
  const auto input = Input("file_adapters", "keynote.key");
  FileDocument document;
  Q4T_CHECK(!input.bytes.empty() && fixture.Read(input, &document).ok());
  std::printf(
      "keynote format=%s codec=%s product=%s version=%s build=%s text=%s "
      "clean=%d\n",
      document.conversion.output_format.c_str(),
      document.conversion.codec.c_str(), document.conversion.product.c_str(),
      document.conversion.version.c_str(), document.conversion.build_id.c_str(),
      document.text.c_str(), fixture.Clean());
  Q4T_CHECK(document.text == "\n[Slide page1]\n" &&
            document.text.find("Transition") == document.text.npos &&
            Provenance(input, document, "odp") && fixture.Clean());
  return true;
}
Q4T_TEST(file_extract_actual_keynote_xml_notes_are_real_document_content) {
  Fixture fixture;
  const auto input = Input("file_adapters", "keynote-notes.key");
  FileDocument document;
  Q4T_CHECK(fixture.Read(input, &document).ok());
  Q4T_CHECK(document.text.find("Original Keynote notes 中文 42") !=
                document.text.npos &&
            Provenance(input, document, "odp") && fixture.Clean());
  return true;
}
Q4T_TEST(file_extract_actual_xlsb_binary_workbook_values) {
  Fixture fixture;
  auto input = Input("file_adapters", "sheet.xlsb");
  // The actual container selects the decoder, even with misleading metadata.
  input.filename = "metadata.txt";
  FileDocument document;
  Q4T_CHECK(!input.bytes.empty() && fixture.Read(input, &document).ok());
  Q4T_CHECK(Cell(document, "String1") && Cell(document, "String2") &&
            Provenance(input, document, "ods") && fixture.Clean());
  return true;
}
Q4T_TEST(file_extract_native_path_does_not_require_converter) {
  Fixture fixture;
  fixture.options.legacy.work_directory.clear();
  FileInputContent input;
  input.bytes = "Original UTF-8 中文\n";
  input.filename = "report.txt";
  FileDocument document;
  Q4T_CHECK(fixture.Read(input, &document).ok() &&
            document.text == input.bytes && document.conversion.codec.empty());
  Q4T_CHECK(fixture.Clean());
  return true;
}
Q4T_TEST(file_extract_chat_rejects_non_pdf_without_starting_decoder) {
  Fixture fixture;
  const auto input = Input("file_adapters", "pages.pages");
  FileDocument document;
  document.text = "stale";
  Q4T_CHECK(fixture.Read(input, &document, true).http_status == 400 &&
            document.text.empty() && fixture.control.counters->started == 0);
  return true;
}
Q4T_TEST(file_extract_corrupt_package_never_falls_back_to_converter) {
  Fixture fixture;
  auto input = Input("file_adapters", "pages.pages");
  input.bytes.resize(input.bytes.size() / 2);
  FileDocument document;
  Q4T_CHECK(fixture.Read(input, &document).http_status == 400 &&
            fixture.control.counters->started == 1 && fixture.Clean());
  Q4T_CHECK(document.text.empty() && document.conversion.codec.empty());
  return true;
}

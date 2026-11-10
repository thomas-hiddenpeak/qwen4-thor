#include "q4t/server/file_snapshot.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
const std::string kBinding = Digest("file input item and original part");
BinaryAccess Access() { return {"client:document", Clock::now() + 30s, {}}; }
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
struct Fixture {
  ObjectStoreOptions options;
  FileExtractOptions extraction;
  GrammarControl control;
  Fixture() {
    const auto* out = std::getenv("Q4T_TEST_OUTPUT_DIR");
    const auto root = fs::absolute(out ? out : ".q4t-work/file-snapshot-cpu");
    fs::create_directories(root);
    auto path = (root / "file-snapshot-XXXXXX").string();
    if (!mkdtemp(path.data()))
      throw std::runtime_error("file snapshot fixture");
    options.directory = path + "/objects";
    options.restart_backoff_ms = 0;
    extraction.legacy.work_directory = path + "/codec";
    control.counters = std::make_shared<GrammarWorkerCounters>();
  }
  FileInputSnapshot Extract(const char* folder, const char* name) {
    FileInputSnapshot input;
    input.detail = "low";
    auto& source = input.source.content;
    source.filename = name;
    std::ifstream file(
        fs::path(__FILE__).parent_path() / "fixtures" / folder / name,
        std::ios::binary);
    source.bytes.assign(std::istreambuf_iterator<char>(file), {});
    source.sha256 = Digest(source.bytes);
    auto reply = ExtractFileDocument(source, extraction, false, input.detail,
                                     Access(), &input.document, control);
    if (!reply.ok())
      throw std::runtime_error(reply.code + ": " + reply.message);
    return input;
  }
};
ObjectReply Save(InputSnapshotStore& store, Fixture& fixture,
                 const FileInputSnapshot& input, InputSnapshotRef* reference,
                 InputSnapshotHold* hold) {
  return SaveFileInputSnapshot(store, Access(), kBinding, input,
                               fixture.extraction, {}, 60, reference, hold);
}
ObjectReply Read(InputSnapshotStore& store, Fixture& fixture,
                 const InputSnapshotRef& reference, FileInputSnapshot* output) {
  return ReadFileInputSnapshot(store, Access(), reference, kBinding,
                               fixture.extraction, {}, output);
}
bool Empty(const FileInputSnapshot& input) {
  return input.source.content.bytes.empty() && input.document.text.empty() &&
         input.document.pages.empty() && input.document.sheets.empty() &&
         input.document.source_sha256.empty();
}
std::string Upload(ObjectStore& objects, std::string_view bytes) {
  FileUpload upload(objects, Access(), "multipart/form-data; boundary=snap");
  std::string head =
      "--snap\r\nContent-Disposition: form-data; name=\"purpose\"\r\n\r\n"
      "user_data\r\n--snap\r\nContent-Disposition: form-data; name=\"file\"; "
      "filename=\"original.txt\"\r\nContent-Type: text/plain\r\n\r\n";
  auto result = upload.Feed(head, Access().deadline);
  if (result.ok()) result = upload.Feed(bytes, Access().deadline);
  if (result.ok()) result = upload.Feed("\r\n--snap--\r\n", Access().deadline);
  if (result.ok()) result = upload.Finish();
  if (!result.ok()) throw std::runtime_error(result.message);
  return result.value.GetString("id");
}
}  // namespace

Q4T_TEST(
    file_snapshot_real_nested_pages_conversion_and_restart_without_codecs) {
  Fixture fixture;
  auto input = fixture.Extract("file_nested", "mixed.docx");
  Q4T_CHECK(!input.document.pages.empty() && !input.document.embedded.empty());
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  {
    ObjectStore objects(fixture.options);
    InputSnapshotStore snapshots(objects);
    Q4T_CHECK(Ok(Save(snapshots, fixture, input, &reference, &hold)));
  }
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  const auto started = fixture.control.counters->started.load();
  detail::SetDocumentWorkerEntryForTesting("--grammar-fixture-silent");
  detail::SetPdfWorkerEntryForTesting("--grammar-fixture-silent");
  ScopeExit reset([] {
    detail::SetDocumentWorkerEntryForTesting({});
    detail::SetPdfWorkerEntryForTesting({});
  });
  FileInputSnapshot restored;
  Q4T_CHECK(Ok(Read(snapshots, fixture, reference, &restored)) &&
            restored.source.content.bytes == input.source.content.bytes &&
            restored.document.text == input.document.text &&
            restored.detail == "low" &&
            restored.document.pages[0].png == input.document.pages[0].png &&
            restored.document.pages[0].source_path ==
                input.document.pages[0].source_path &&
            restored.document.embedded.size() ==
                input.document.embedded.size());
  for (size_t i = 0; i < restored.document.embedded.size(); ++i) {
    const auto& actual = restored.document.embedded[i];
    const auto& expected = input.document.embedded[i];
    Q4T_CHECK(actual.path == expected.path &&
              actual.source_sha256 == expected.source_sha256 &&
              actual.conversion.version == expected.conversion.version &&
              actual.conversion.build_id == expected.conversion.build_id &&
              actual.conversion.output_sha256 ==
                  expected.conversion.output_sha256);
  }
  Q4T_CHECK(fixture.control.counters->started == started &&
            !fixture.control.counters->active);
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  return true;
}

Q4T_TEST(file_snapshot_real_sheets_and_private_indirect_source_are_exact) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  for (const auto& [folder, name] :
       {std::pair{"file_office", "sheets.xlsx"},
        std::pair{"file_legacy", "sheet.xls"},
        std::pair{"file_adapters", "numbers.numbers"}}) {
    auto input = fixture.Extract(folder, name);
    Q4T_CHECK(!input.document.sheets.empty());
    if (std::string_view(name) == "sheets.xlsx") {
      // Typed persistence contract only; no simulated network success.
      FileInputContent shortcut;
      shortcut.filename = "example.gsheet";
      shortcut.bytes =
          R"({"url":"https://docs.google.com/spreadsheets/d/example/edit"})";
      shortcut.sha256 = Digest(shortcut.bytes);
      input.source.indirect_source = shortcut;
      input.source.urls = {
          "https://docs.google.com/spreadsheets/d/example/edit",
          "https://example.com/export?private=retained"};
      input.source.export_format = "xlsx";
    }
    InputSnapshotRef reference;
    InputSnapshotHold hold;
    Q4T_CHECK(Ok(Save(snapshots, fixture, input, &reference, &hold)));
    FileInputSnapshot restored;
    Q4T_CHECK(Ok(Read(snapshots, fixture, reference, &restored)) &&
              restored.source.urls == input.source.urls &&
              restored.source.export_format == input.source.export_format &&
              restored.document.sheets.size() == input.document.sheets.size());
    if (input.source.indirect_source)
      Q4T_CHECK(restored.source.indirect_source &&
                restored.source.indirect_source->bytes ==
                    input.source.indirect_source->bytes);
    for (size_t i = 0; i < input.document.sheets.size(); ++i) {
      const auto& actual = restored.document.sheets[i];
      const auto& expected = input.document.sheets[i];
      Q4T_CHECK(actual.name == expected.name && actual.rows == expected.rows &&
                actual.source_rows == expected.source_rows &&
                actual.columns == expected.columns);
    }
    Q4T_CHECK(restored.document.conversion.output_sha256 ==
                  input.document.conversion.output_sha256 &&
              restored.document.conversion.codec ==
                  input.document.conversion.codec);
    Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  }
  return true;
}

Q4T_TEST(file_snapshot_stored_file_delete_cannot_change_prepared_input) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  const std::string text = "Original project record: acceptance 42 中文\n";
  const auto id = Upload(objects, text);
  auto part = Object({{"type", String("input_file")}, {"file_id", String(id)}});
  FileInputSource source;
  Q4T_CHECK(Ok(ParseFileInputSource(part, FileInputProfile::kResponses,
                                    "input[0].content[0]", &source)));
  FileInputBudget budget;
  FileInputSnapshot input;
  Q4T_CHECK(Ok(ResolveFileInput(&objects, Access(), source, {}, &budget,
                                &input.source, fixture.control)) &&
            Ok(ExtractFileDocument(input.source.content, fixture.extraction,
                                   false, "auto", Access(), &input.document,
                                   fixture.control)));
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, fixture, input, &reference, &hold)) &&
            Ok(Files(objects).Erase(Access(), id)) &&
            Files(objects).Retrieve(Access(), id).http_status == 404);
  FileInputSnapshot restored;
  Q4T_CHECK(Ok(Read(snapshots, fixture, reference, &restored)) &&
            restored.source.content.file_id == id &&
            restored.source.content.bytes == text &&
            restored.document.text == text);
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  return true;
}

Q4T_TEST(file_snapshot_typed_receipt_rejects_wrong_parts_sources_and_pages) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  const auto input = fixture.Extract("file_nested", "mixed.docx");
  InputSnapshotRef original;
  InputSnapshotHold original_hold;
  Q4T_CHECK(Ok(Save(snapshots, fixture, input, &original, &original_hold)));
  InputSnapshot raw;
  Q4T_CHECK(Ok(snapshots.Read(Access(), original, kBinding, {}, &raw)));
  for (int mutation = 0; mutation < 8; ++mutation) {
    auto changed = raw;
    if (mutation == 0)
      Set(&changed.metadata, "source_sha256", String(Digest("wrong")));
    if (mutation == 1)
      Set(&Field(changed.metadata, "source"), "part", Number(10000));
    auto& page = Field(changed.metadata, "pages").array[0];
    if (mutation == 2) Set(&page, "part", Number(0));
    if (mutation == 3) Set(&page, "width", Number(1));
    if (mutation == 4)
      Set(&page, "source_path", Array({String("missing.pdf")}));
    if (mutation == 5) Set(&changed.metadata, "pending", Array());
    if (mutation == 6) changed.parts.push_back("unreferenced raw section");
    if (mutation == 7)
      Set(&Field(Field(changed.metadata, "embedded").array[0], "conversion"),
          "output_sha256", String("invalid"));
    std::vector<std::string_view> parts;
    for (const auto& part : changed.parts) parts.push_back(part);
    InputSnapshotRef reference;
    InputSnapshotHold hold;
    Q4T_CHECK(Ok(snapshots.Save(Access(), kBinding, changed.metadata, parts, {},
                                60, &reference, &hold)));
    FileInputSnapshot restored = input;
    Q4T_CHECK(Read(snapshots, fixture, reference, &restored).code ==
                  "file_snapshot_inconsistent" &&
              Empty(restored));
    Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  }
  Q4T_CHECK(Ok(snapshots.Release(Access(), &original_hold)));
  return true;
}

Q4T_TEST(file_snapshot_active_budgets_apply_to_history_without_partial_output) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  const auto input = fixture.Extract("file_nested", "mixed.docx");
  InputSnapshotRef reference;
  InputSnapshotHold hold;
  Q4T_CHECK(Ok(Save(snapshots, fixture, input, &reference, &hold)));
  for (int budget = 0; budget < 4; ++budget) {
    auto options = fixture.extraction;
    if (budget == 0) options.document.max_text_bytes = 1;
    if (budget == 1) options.document.max_embedded_documents = 1;
    if (budget == 2) options.pdf.max_pixels = 1;
    if (budget == 3) options.pdf.max_image_bytes = 1;
    FileInputSnapshot restored = input;
    Q4T_CHECK(ReadFileInputSnapshot(snapshots, Access(), reference, kBinding,
                                    options, {}, &restored)
                      .http_status == 413 &&
              Empty(restored));
  }
  auto cancelled = Access();
  cancelled.cancelled = [] { return true; };
  FileInputSnapshot restored = input;
  Q4T_CHECK(ReadFileInputSnapshot(snapshots, cancelled, reference, kBinding,
                                  fixture.extraction, {}, &restored)
                    .http_status == 499 &&
            Empty(restored));
  Q4T_CHECK(Ok(snapshots.Release(Access(), &hold)));
  return true;
}

Q4T_TEST(file_snapshot_never_persists_incomplete_or_mismatched_extraction) {
  Fixture fixture;
  ObjectStore objects(fixture.options);
  InputSnapshotStore snapshots(objects);
  const auto original = fixture.Extract("file_legacy", "sheet.xls");
  for (int mutation = 0; mutation < 4; ++mutation) {
    auto input = original;
    if (mutation == 0) input.document.pending.push_back({});
    if (mutation == 1) input.source.content.bytes += "changed";
    if (mutation == 2) input.document.conversion.version.clear();
    if (mutation == 3) input.document.sheets[0].source_rows = 0;
    InputSnapshotRef reference;
    InputSnapshotHold hold;
    Q4T_CHECK(Save(snapshots, fixture, input, &reference, &hold).code ==
                  "file_snapshot_inconsistent" &&
              reference.id.empty() && hold.id.empty());
  }
  return true;
}

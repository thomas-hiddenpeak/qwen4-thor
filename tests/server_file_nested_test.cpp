#include "q4t/server/file_extract.h"

#include <unistd.h>

#include <atomic>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
BinaryAccess Access() { return {"nested", Clock::now() + 30s, {}}; }
FileInputContent Input(const char* name, const char* folder = "file_nested") {
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
                  ".q4t-work/public-host-fixtures/file-nested" /
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
  ObjectReply Read(const FileInputContent& input, FileDocument* output) {
    auto reply = ExtractFileDocument(input, options, false, "low", Access(),
                                     output, control);
    if (!reply.ok())
      std::printf("nested %s: %d %s\n", input.filename.c_str(),
                  reply.http_status, reply.code.c_str());
    return reply;
  }
  bool Clean() const {
    if (control.counters->active ||
        control.counters->started != control.counters->reaped)
      return false;
    if (fs::exists(options.legacy.work_directory))
      for (const auto& item :
           fs::directory_iterator(options.legacy.work_directory))
        if (item.path().filename() != ".q4t-codec-root") return false;
    return true;
  }
};
bool Empty(const FileDocument& document) {
  return document.text.empty() && document.pages.empty() &&
         document.sheets.empty() && document.embedded.empty() &&
         document.pending.empty() && document.source_sha256.empty();
}
}  // namespace

int NestedDocumentFixture(int argc, char** argv) {
  if (argc < 2 || !std::string_view(argv[1]).starts_with("--nested-frame-"))
    return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const std::string_view mode(argv[1]);
  Frame frame;
  if (!detail::WorkerTransfer(&frame, sizeof(frame), false) || !frame.bytes ||
      frame.bytes > 8192)
    return 3;
  std::string text(frame.bytes, '\0');
  if (!detail::WorkerTransfer(text.data(), text.size(), false)) return 3;
  Json request;
  if (!Parse(text, &request)) return 3;
  const auto size = request.GetInt("bytes", -1);
  if (size < 0 || size > 1048576) return 3;
  std::string input(size, '\0');
  if (!detail::WorkerTransfer(input.data(), input.size(), false)) return 3;
  auto path = Array();
  path.array.push_back(String("word/page.pdf"));
  auto pending = Array();
  pending.array.push_back(
      Object({{"offset", Number(mode == "--nested-frame-offset" ? 100 : 2)},
              {"depth", Number(mode == "--nested-frame-depth" ? 2 : 1)},
              {"path", path},
              {"filename", String("word/page.pdf")},
              {"bytes",
               Number(mode == "--nested-frame-size" ? kFileInputBytes + 1 : 8)},
              {"sha256", String(Digest("%PDF-1.7"))}}));
  ObjectReply reply;
  reply.value = Object({{"text", String("body")},
                        {"encoding", String("utf-8")},
                        {"source_sha256", String(Digest(input))},
                        {"sheets", Array()},
                        {"embedded", Array()},
                        {"pending", pending}});
  text = OutputJson(WireReply(reply));
  frame = {kVersion, 1, text.size()};
  if (!detail::WorkerTransfer(&frame, sizeof(frame), true) ||
      !detail::WorkerTransfer(text.data(), text.size(), true))
    return 3;
  if (mode == "--nested-frame-digest") {
    std::string bytes = "bad-data";
    if (!detail::WorkerTransfer(bytes.data(), bytes.size(), true)) return 3;
  }
  // The truncated case sends a valid metadata frame and no declared bytes.
  return 0;
}

Q4T_TEST(file_nested_native_receipt_contains_ordered_actual_member_bytes) {
  Fixture fixture;
  const auto input = Input("mixed.docx");
  FileDocument document;
  Q4T_CHECK(
      ParseNativeFileDocument(input, {}, Access(), &document, fixture.control)
          .ok());
  Q4T_CHECK(document.pending.size() == 2 && document.embedded.empty() &&
            document.pages.empty());
  const auto& first = document.pending[0];
  const auto& second = document.pending[1];
  Q4T_CHECK(first.source.bytes == Input("word.doc", "file_legacy").bytes &&
            second.source.bytes == Input("page.pdf").bytes &&
            first.source.sha256 == Digest(first.source.bytes) &&
            first.path == std::vector<std::string>{"word/legacy.doc"} &&
            first.depth == 1 && second.depth == 1 &&
            first.text_offset < second.text_offset);
  Q4T_CHECK(document.text.substr(0, first.text_offset) == "Outer before\n" &&
            fixture.Clean());
  Q4T_CHECK(fixture.control.counters->started == 1);
  return true;
}
Q4T_TEST(file_nested_actual_legacy_and_pdf_keep_order_pages_and_provenance) {
  Fixture fixture;
  const auto input = Input("mixed.docx");
  FileDocument document;
  Q4T_CHECK(fixture.Read(input, &document).ok());
  const auto& text = document.text;
  const auto a = text.find("Outer before"),
             b = text.find("Legacy report 中文 😀"),
             c = text.find("Outer after 0"),
             d = text.find("Embedded actual PDF 42"),
             e = text.find("Outer after 1");
  Q4T_CHECK(a < b && b < c && c < d && d < e && e != text.npos);
  Q4T_CHECK(document.pending.empty() && document.embedded.size() == 2 &&
            document.source_sha256 == Digest(input.bytes));
  const auto& converted = document.embedded[0];
  Q4T_CHECK(converted.path == std::vector<std::string>{"word/legacy.doc"} &&
            converted.source_sha256 ==
                Digest(Input("word.doc", "file_legacy").bytes) &&
            converted.conversion.codec == "libreofficekit" &&
            !converted.conversion.version.empty());
  Q4T_CHECK(document.pages.size() == 1 && document.pages[0].width == 512 &&
            document.pages[0].source_path ==
                std::vector<std::string>{"word/page.pdf"} &&
            document.pages[0].number == 1 && fixture.Clean());
  return true;
}
Q4T_TEST(file_nested_multilevel_paths_and_source_identity_are_preserved) {
  Fixture fixture;
  FileDocument document;
  Q4T_CHECK(fixture.Read(Input("wrapped.docx"), &document).ok());
  Q4T_CHECK(document.embedded.size() == 3 && document.pending.empty() &&
            document.pages.size() == 1);
  Q4T_CHECK(
      document.embedded[0].path ==
          std::vector<std::string>{"word/nested.docx"} &&
      document.embedded[0].source_sha256 == Digest(Input("mixed.docx").bytes) &&
      document.pages[0].source_path ==
          std::vector<std::string>({"word/nested.docx", "word/page.pdf"}));
  Q4T_CHECK(document.text.find("Wrapper before") <
                document.text.find("Legacy report") &&
            document.text.find("Embedded actual PDF 42") <
                document.text.find("Wrapper after 0") &&
            fixture.Clean());
  return true;
}
Q4T_TEST(file_nested_failure_discards_earlier_success_and_next_request_works) {
  Fixture fixture;
  FileDocument document;
  Q4T_CHECK(fixture.Read(Input("bad-child.docx"), &document).http_status ==
                400 &&
            Empty(document) && fixture.Clean());
  FileInputContent plain;
  plain.filename = "text.txt";
  plain.bytes = "healthy core input";
  Q4T_CHECK(fixture.Read(plain, &document).ok() &&
            document.text == plain.bytes && fixture.Clean());
  return true;
}
Q4T_TEST(file_nested_tree_limits_cover_count_depth_pages_and_rendered_text) {
  for (int variant = 0; variant < 4; ++variant) {
    Fixture fixture;
    const char* name = "count.docx";
    if (variant == 0) fixture.options.document.max_embedded_documents = 2;
    if (variant == 1) {
      name = "wrapped.docx";
      fixture.options.document.max_nested_documents = 1;
    }
    if (variant == 2) {
      name = "two-pdf.docx";
      fixture.options.pdf.max_pages = 1;
    }
    if (variant == 3) {
      name = "mixed.docx";
      fixture.options.document.max_text_bytes = 160;
    }
    FileDocument document;
    Q4T_CHECK(fixture.Read(Input(name), &document).http_status == 413 &&
              Empty(document) && fixture.Clean());
    if (variant < 2) Q4T_CHECK(fixture.control.counters->started == 1);
  }
  return true;
}
Q4T_TEST(file_nested_cancel_after_first_child_starts_no_later_codec) {
  Fixture fixture;
  std::atomic<bool> cancelled{false};
  fixture.control.cancelled = [&] { return cancelled.load(); };
  fixture.control.worker_finished = [&](const GrammarWorkerReport&) {
    if (fixture.control.counters->started >= 4) cancelled.store(true);
  };
  FileDocument document;
  Q4T_CHECK(fixture.Read(Input("mixed.docx"), &document).http_status == 499 &&
            fixture.control.counters->started == 4 && Empty(document) &&
            fixture.Clean());
  return true;
}
Q4T_TEST(file_nested_total_deadline_includes_parent_cleanup_and_never_resets) {
  Fixture fixture;
  fixture.options.timeout_ms = 100;
  fixture.control.worker_finished = [](const GrammarWorkerReport&) {
    std::this_thread::sleep_for(150ms);
  };
  const auto began = Clock::now();
  FileDocument document;
  Q4T_CHECK(fixture.Read(Input("mixed.docx"), &document).http_status == 408 &&
            Clock::now() - began < 1s &&
            fixture.control.counters->started == 1 && Empty(document) &&
            fixture.Clean());
  return true;
}
Q4T_TEST(
    file_nested_private_receipt_rejects_offsets_depth_size_digest_and_truncation) {
  ScopeExit reset([] { detail::SetDocumentWorkerEntryForTesting(""); });
  for (const auto* entry :
       {"--nested-frame-offset", "--nested-frame-depth", "--nested-frame-size",
        "--nested-frame-digest", "--nested-frame-truncated"}) {
    Fixture fixture;
    detail::SetDocumentWorkerEntryForTesting(entry);
    FileDocument document;
    auto reply = ParseNativeFileDocument(Input("mixed.docx"), {}, Access(),
                                         &document, fixture.control);
    Q4T_CHECK(reply.http_status == 503 && Empty(document) && fixture.Clean());
  }
  return true;
}
Q4T_TEST(file_nested_compressed_iwork_xml_uses_actual_decoder) {
  Fixture fixture;
  const auto input = Input("keynote-notes-gzip.key", "file_adapters");
  FileDocument document;
  Q4T_CHECK(fixture.Read(input, &document).ok() &&
            document.text.find("Original Keynote notes 中文 42") !=
                document.text.npos &&
            document.conversion.codec == "libreofficekit" &&
            document.source_sha256 == Digest(input.bytes) && fixture.Clean());
  return true;
}

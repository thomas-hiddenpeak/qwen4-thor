#include "q4t/server/file_pdf.h"
#include "q4t/server/file_extract.h"

#include <fcntl.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/object_protocol.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
BinaryAccess Access() { return {"client:pdf", Clock::now() + 10s, {}}; }
GrammarControl Control() {
  GrammarControl control;
  control.counters = std::make_shared<GrammarWorkerCounters>();
  return control;
}
bool Clean(const GrammarControl& control, uint64_t count) {
  return control.counters->started == count &&
         control.counters->reaped == count && control.counters->active == 0;
}
// Original fixture PDF: real xref offsets, two independently colored vector
// pages, text and an optional rotated page. No external generator dependency.
FileInputContent Pdf(size_t pages = 2, bool rotated = false) {
  std::vector<std::string> objects = {
      "<< /Type /Catalog /Pages 2 0 R >>", "",
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"};
  std::string kids;
  for (size_t page = 0; page < pages; ++page) {
    const auto id = objects.size() + 1;
    kids += std::to_string(id) + " 0 R ";
    objects.push_back(
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] "
        "/Resources << /Font << /F1 3 0 R >> >> /Contents " +
        std::to_string(id + 1) + " 0 R" + (rotated ? " /Rotate 90" : "") +
        " >>");
    const std::string content =
        std::string(page % 2 ? "0 0 1" : "1 0 0") +
        " rg 0 0 400 200 re f\n0 0 0 rg BT /F1 18 Tf 30 80 Td "
        "(Q4T actual PDF page " +
        std::to_string(page + 1) + ") Tj ET\n";
    objects.push_back("<< /Length " + std::to_string(content.size()) +
                      " >>\nstream\n" + content + "endstream");
  }
  objects[1] = "<< /Type /Pages /Kids [" + kids + "] /Count " +
               std::to_string(pages) + " >>";
  std::string pdf = "%PDF-1.7\n";
  std::vector<size_t> offsets;
  for (size_t i = 0; i < objects.size(); ++i) {
    offsets.push_back(pdf.size());
    pdf += std::to_string(i + 1) + " 0 obj\n" + objects[i] + "\nendobj\n";
  }
  const size_t xref = pdf.size();
  pdf += "xref\n0 " + std::to_string(objects.size() + 1) +
         "\n0000000000 65535 f \n";
  for (size_t offset : offsets) {
    char line[32];
    std::snprintf(line, sizeof(line), "%010zu 00000 n \n", offset);
    pdf += line;
  }
  pdf += "trailer\n<< /Size " + std::to_string(objects.size() + 1) +
         " /Root 1 0 R >>\nstartxref\n" + std::to_string(xref) + "\n%%EOF\n";
  FileInputContent input;
  input.bytes = std::move(pdf);
  input.filename = "../../display;only.pdf";
  input.mime_type = "application/pdf";
  return input;
}
bool Save(const FileDocument& document, std::string_view prefix) {
  const char* root = std::getenv("Q4T_TEST_OUTPUT_DIR");
  const auto destination =
      root ? std::filesystem::path(root)
           : std::filesystem::path(__FILE__).parent_path().parent_path() /
                 ".q4t-work/public-host-fixtures/pdf";
  std::filesystem::create_directories(destination);
  for (const auto& page : document.pages) {
    std::ofstream file(
        destination /
            (std::string(prefix) + std::to_string(page.number) + ".png"),
        std::ios::binary);
    file.write(page.png.data(), page.png.size());
    if (!file) return false;
  }
  return true;
}
}  // namespace

Q4T_TEST(file_extract_pdf_signature_routes_every_page_with_detail) {
  auto input = Pdf(1);
  input.filename = "misleading.txt";
  input.mime_type = "text/plain";
  const auto control = Control();
  FileDocument document;
  Q4T_CHECK(ExtractFileDocument(input, {}, true, "low", Access(), &document,
                                 control).ok());
  Q4T_CHECK(document.text.find("Q4T actual PDF page 1") != document.text.npos &&
            document.pages.size() == 1 && document.pages[0].width == 512 &&
            document.conversion.codec.empty() &&
            document.source_sha256 == Digest(input.bytes));
  Q4T_CHECK(control.counters->active == 0 &&
            control.counters->started == control.counters->reaped);
  return true;
}

int PdfSandboxFixture(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--pdf-sandbox-fixture")) return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const int executable = open("/usr/bin/pdfinfo", O_PATH | O_CLOEXEC);
  if (executable != 4 || !detail::EnterPdfSandbox(executable)) return 2;
  uint64_t result[9]{};
  int file = open("/etc/hostname", O_RDONLY | O_CLOEXEC);
  result[0] = file < 0 && errno == EACCES;
  if (file >= 0) close(file);
  file = open("/etc/ld.so.cache", O_RDONLY | O_CLOEXEC);
  result[1] = file >= 0;
  if (file >= 0) close(file);
  file = open("/tmp/q4t-pdf-sandbox-must-not-create", O_WRONLY | O_CREAT, 0600);
  result[2] = file < 0 && errno == EPERM;
  if (file >= 0) close(file);
  int network = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  result[3] = network < 0 && errno == EPERM;
  if (network >= 0) close(network);
  result[4] = kill(getpid(), 0) < 0 && errno == EPERM;
  result[5] = syscall(SYS_clone3, nullptr, 0) < 0 && errno == EPERM;
  char binary[] = "/usr/bin/pdfinfo";
  char* args[] = {binary, nullptr};
  char* env[] = {nullptr};
  result[6] = execve(binary, args, env) < 0 && errno == EPERM;
  result[7] = syscall(SYS_execveat, 99, "", args, env, AT_EMPTY_PATH) < 0 &&
              errno == EPERM;
  file = open("/proc/self/environ", O_RDONLY | O_CLOEXEC);
  result[8] = file < 0 && errno == EACCES;
  if (file >= 0) close(file);
  return detail::WorkerTransfer(result, sizeof(result), true) ? 0 : 3;
}

Q4T_TEST(file_pdf_actual_text_and_every_page_images) {
  const auto input = Pdf();
  const auto control = Control();
  FileDocument document;
  const auto reply =
      ParsePdfFileDocument(input, {}, {}, "low", Access(), &document, control);
  if (!reply.ok()) std::fprintf(stderr, "PDF error %s\n", reply.code.c_str());
  Q4T_CHECK(reply.ok() && document.pages.size() == 2 &&
            document.text.find("Q4T actual PDF page 1") != std::string::npos &&
            document.text.find("Q4T actual PDF page 2") != std::string::npos &&
            document.source_sha256 == Digest(input.bytes) &&
            document.encoding == "utf-8" && Clean(control, 4));
  for (const auto& page : document.pages)
    Q4T_CHECK(page.width == 512 && page.height == 256);
  Q4T_CHECK(Save(document, "pdf-low-"));
  return true;
}
Q4T_TEST(file_pdf_high_detail_preserves_rotated_geometry) {
  const auto control = Control();
  FileDocument document;
  Q4T_CHECK(ParsePdfFileDocument(Pdf(1, true), {}, {}, "high", Access(),
                                 &document, control)
                .ok() &&
            document.pages.size() == 1 && document.pages[0].width == 1024 &&
            document.pages[0].height == 2048 && Clean(control, 3));
  Q4T_CHECK(Save(document, "pdf-high-"));
  return true;
}
Q4T_TEST(file_pdf_page_and_text_budgets_never_publish_partial) {
  auto control = Control();
  FileDocument document;
  FilePdfOptions options;
  options.max_pages = 1;
  Q4T_CHECK(ParsePdfFileDocument(Pdf(), {}, options, "low", Access(), &document,
                                 control)
                    .http_status == 413 &&
            document.text.empty() && document.pages.empty() &&
            Clean(control, 1));
  control = Control();
  FileDocumentLimits limits;
  limits.max_text_bytes = 4;
  Q4T_CHECK(ParsePdfFileDocument(Pdf(), limits, {}, "low", Access(), &document,
                                 control)
                    .http_status == 413 &&
            document.source_sha256.empty() && Clean(control, 2));
  return true;
}
Q4T_TEST(file_pdf_image_bytes_and_actual_pixel_budgets) {
  auto control = Control();
  FileDocument document;
  FilePdfOptions options;
  options.max_image_bytes = 8;
  Q4T_CHECK(ParsePdfFileDocument(Pdf(), {}, options, "low", Access(), &document,
                                 control)
                    .http_status == 413 &&
            document.pages.empty() && Clean(control, 3));
  control = Control();
  options = {};
  options.max_pixels = 512 * 256;
  Q4T_CHECK(ParsePdfFileDocument(Pdf(1), {}, options, "low", Access(),
                                 &document, control)
                .ok() &&
            Clean(control, 3));
  control = Control();
  --options.max_pixels;
  Q4T_CHECK(ParsePdfFileDocument(Pdf(1), {}, options, "low", Access(),
                                 &document, control)
                    .http_status == 413 &&
            document.text.empty() && document.pages.empty() &&
            Clean(control, 3));
  return true;
}
Q4T_TEST(file_pdf_damaged_input_does_not_poison_native_text) {
  auto control = Control();
  auto input = Pdf();
  input.bytes = "%PDF-1.7\nBroken document\n";
  FileDocument document;
  Q4T_CHECK(
      ParsePdfFileDocument(input, {}, {}, "auto", Access(), &document, control)
              .http_status == 400 &&
      document.text.empty() && Clean(control, 1));
  input.bytes = "normal text remains healthy";
  input.filename = "normal.txt";
  input.mime_type = "text/plain";
  control = Control();
  Q4T_CHECK(
      ParseNativeFileDocument(input, {}, Access(), &document, control).ok() &&
      document.text == input.bytes && Clean(control, 1));
  return true;
}
Q4T_TEST(file_pdf_sandbox_enforces_system_read_only_and_no_network_processes) {
  auto control = Control();
  control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> worker;
  Q4T_CHECK(
      detail::GrammarProcess::Start("--pdf-sandbox-fixture", control, &worker));
  uint64_t report[9]{};
  Q4T_CHECK(worker->Transfer(report, sizeof(report), false, Clock::now() + 2s));
  for (auto passed : report) Q4T_CHECK(passed == 1);
  std::string tail;
  Q4T_CHECK(worker->CollectOutput(&tail, 0, Clock::now() + 2s) &&
            tail.empty() && Clean(control, 1));
  return true;
}
Q4T_TEST(file_pdf_cancel_and_deadline_reap_only_owned_worker) {
  ScopeExit reset([] { detail::SetPdfWorkerEntryForTesting(""); });
  detail::SetPdfWorkerEntryForTesting("--grammar-fixture-silent");
  auto control = Control();
  auto access = Access();
  access.deadline = Clock::now() + 80ms;
  FileDocument document;
  const auto start = Clock::now();
  Q4T_CHECK(
      ParsePdfFileDocument(Pdf(), {}, {}, "low", access, &document, control)
              .http_status == 408 &&
      Clock::now() - start < 1s && Clean(control, 1));
  detail::SetPdfWorkerEntryForTesting("");
  control = Control();
  bool cancelled = false;
  control.worker_ready = [&] { cancelled = true; };
  access = Access();
  access.cancelled = [&] { return cancelled; };
  Q4T_CHECK(
      ParsePdfFileDocument(Pdf(), {}, {}, "low", access, &document, control)
              .http_status == 499 &&
      document.pages.empty() && Clean(control, 1));
  return true;
}
Q4T_TEST(file_pdf_invalid_config_owner_detail_and_cancel_launch_nothing) {
  const auto control = Control();
  FileDocument document;
  auto access = Access();
  access.scope.clear();
  Q4T_CHECK(
      ParsePdfFileDocument(Pdf(), {}, {}, "low", access, &document, control)
          .http_status == 503);
  FilePdfOptions options;
  options.max_pages = 0;
  Q4T_CHECK(ParsePdfFileDocument(Pdf(), {}, options, "low", Access(), &document,
                                 control)
                .http_status == 500);
  Q4T_CHECK(ParsePdfFileDocument(Pdf(), {}, {}, "original", Access(), &document,
                                 control)
                .http_status == 400);
  access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(
      ParsePdfFileDocument(Pdf(), {}, {}, "low", access, &document, control)
              .http_status == 499 &&
      control.counters->started == 0 && document.pages.empty());
  return true;
}

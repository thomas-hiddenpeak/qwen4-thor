#include "q4t/server/file_legacy.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <thread>

#include "../src/server/file_legacy_workspace.h"
#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
namespace fs = std::filesystem;
fs::path Base(pid_t parent) {
  return fs::path(__FILE__).parent_path().parent_path() /
         ".q4t-work/public-host-fixtures/legacy-document" /
         std::to_string(parent);
}
std::string Bytes(const fs::path& path) {
  std::ifstream input(path, std::ios::binary);
  return std::string(std::istreambuf_iterator<char>(input), {});
}
FileInputContent Input(const char* name) {
  FileInputContent input;
  input.filename = name;
  input.bytes =
      Bytes(fs::path(__FILE__).parent_path() / "fixtures/file_legacy" / name);
  return input;
}
struct Fixture {
  fs::path base = Base(getpid());
  FileLegacyOptions options;
  GrammarControl control;
  Fixture() {
    fs::create_directories(base);
    options.work_directory = (base / "codec").string();
    control.counters = std::make_shared<GrammarWorkerCounters>();
    control.worker_finished = [](const GrammarWorkerReport& report) {
      if (report.exit_status || !report.reaped)
        std::printf("legacy worker status=%d reaped=%d\n", report.exit_status,
                    report.reaped);
    };
  }
  ~Fixture() {
    if (control.counters->active == 0) fs::remove_all(base);
  }
  ObjectReply Read(const FileInputContent& input, FileDocument* output,
                   FileDocumentLimits limits = {}) {
    auto reply = ParseLegacyFileDocument(
        input, limits, options, {"legacy-test", Clock::now() + 30s, {}}, output,
        control);
    if (!reply.ok())
      std::printf("legacy %s: %d %s\n", input.filename.c_str(),
                  reply.http_status, reply.code.c_str());
    return reply;
  }
  bool Clean() const {
    if (control.counters->active ||
        control.counters->started != control.counters->reaped)
      return false;
    if (!fs::exists(options.work_directory)) return true;
    for (const auto& entry : fs::directory_iterator(options.work_directory))
      if (entry.path().filename() != ".q4t-codec-root") return false;
    return true;
  }
};
bool Once(const std::string& text, const std::string& part) {
  const auto first = text.find(part);
  return first != text.npos &&
         text.find(part, first + part.size()) == text.npos;
}
}  // namespace

int LegacyDocumentFixture(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "--legacy-sandbox-fixture")
    return -1;
  if (!detail::EnterGrammarWorker(argc, argv)) return 2;
  const auto base = Base(getppid());
  legacy_detail::Workspace workspace;
  if (!workspace.Open((base / "codec").string()) || !workspace.Clean() ||
      !workspace.Create(std::string(32, 'a')))
    return 3;
  const int listener =
      detail::EnterLegacyDocumentSandbox(workspace.Directory());
  if (listener < 0 || !detail::SendWorkerIoListener(listener)) return 4;
  close(listener);
  const auto outside = base / "private.txt";
  const int file =
      openat(workspace.Directory(), "test", O_RDWR | O_CREAT, 0600);
  if (file < 0 || write(file, "a", 1) != 1) return 5;
  bool isolated = true;
  isolated &= open(outside.c_str(), O_RDONLY) < 0;
  isolated &= open(outside.c_str(), O_WRONLY) < 0;
  isolated &= socket(AF_INET, SOCK_STREAM, 0) < 0;
  isolated &= socket(AF_UNIX, SOCK_STREAM, 0) < 0;
  isolated &= syscall(SYS_execve, "/bin/true", nullptr, nullptr) < 0;
  isolated &= syscall(SYS_kill, getppid(), 0) < 0;
  isolated &= syscall(SYS_fchmod, file, 0777) < 0;
  iovec vector{const_cast<char*>("b"), 1};
  isolated &= writev(file, &vector, 1) < 0;
  isolated &= syscall(SYS_fallocate, file, 0, 0, 4096) < 0;
  isolated &=
      mmap(nullptr, 4096, PROT_WRITE, MAP_SHARED, file, 0) == MAP_FAILED;
  auto* view =
      static_cast<char*>(mmap(nullptr, 4096, PROT_READ, MAP_SHARED, file, 0));
  isolated &= view != MAP_FAILED;
  if (view != MAP_FAILED) {
    const bool writable = mprotect(view, 4096, PROT_READ | PROT_WRITE) == 0;
    isolated &= view[0] == 'a' && writable;
    if (writable) view[0] = 'z';
    char original = 0;
    isolated &= pread(file, &original, 1, 0) == 1 && original == 'a';
    munmap(view, 4096);
  }
  isolated &= open("/dev/nvidia0", O_RDWR) < 0;
  bool thread_isolated = false;
  std::thread thread([&] {
    thread_isolated = open(outside.c_str(), O_RDONLY) < 0 &&
                      socket(AF_INET, SOCK_STREAM, 0) < 0;
  });
  thread.join();
  close(file);
  const char result = isolated && thread_isolated ? 'Y' : 'N';
  return detail::WorkerTransfer(const_cast<char*>(&result), 1, true) ? 0 : 6;
}

Q4T_TEST(file_legacy_actual_doc_unicode_and_confirmed_cleanup) {
  Fixture fixture;
  const auto input = Input("word.doc");
  Q4T_CHECK(input.bytes.starts_with(std::string("\xd0\xcf\x11\xe0", 4)));
  FileDocument output;
  Q4T_CHECK(fixture.Read(input, &output).ok());
  Q4T_CHECK(Once(output.text, "Legacy report 中文 😀") &&
            Once(output.text, "Real second paragraph") &&
            output.source_sha256 == object_detail::Digest(input.bytes));
  Q4T_CHECK(fixture.control.counters->started == 3 && fixture.Clean());
  return true;
}
Q4T_TEST(file_legacy_actual_xls_sheet_coordinates_values_and_formula) {
  Fixture fixture;
  FileDocument output;
  const auto input = Input("sheet.xls");
  Q4T_CHECK(fixture.Read(input, &output).ok() && output.sheets.size() == 1);
  const auto& sheet = output.sheets.front();
  std::printf("legacy sheet name=%s source_rows=%llu columns=%zu retained=%zu\n",
              sheet.name.c_str(),
              static_cast<unsigned long long>(sheet.source_rows),
              sheet.columns, sheet.rows.size());
  for (size_t i = 0; i < std::min<size_t>(sheet.rows.size(), 2); ++i)
    for (const auto& cell : sheet.rows[i])
      std::printf("fixture cell: %.512s\n", cell.c_str());
  Q4T_CHECK(sheet.name == "Actual data" && sheet.source_rows == 2 &&
            sheet.columns == 2 && sheet.rows.size() == 2 &&
            sheet.rows[0][0] == "Item 中文" && sheet.rows[0][1] == "Count" &&
            sheet.rows[1][0] == "Original" &&
            sheet.rows[1][1].starts_with("42") &&
            Once(sheet.rows[1][1], "SUM(20;22)") && fixture.Clean());
  return true;
}
Q4T_TEST(file_legacy_actual_ppt_slide_order) {
  Fixture fixture;
  FileDocument output;
  Q4T_CHECK(fixture.Read(Input("slides.ppt"), &output).ok());
  Q4T_CHECK(Once(output.text, "Legacy slide 中文") &&
            Once(output.text, "Actual second slide") &&
            output.text.find("Legacy slide") <
                output.text.find("Actual second") &&
            fixture.Clean());
  return true;
}
Q4T_TEST(file_legacy_corrupt_input_is_local_and_next_document_works) {
  Fixture fixture;
  auto input = Input("word.doc");
  input.bytes.resize(32);
  FileDocument output;
  output.text = "stale";
  Q4T_CHECK(fixture.Read(input, &output).http_status == 400 &&
            output.text.empty() && output.source_sha256.empty() &&
            fixture.Clean());
  Q4T_CHECK(fixture.Read(Input("word.doc"), &output).ok() && fixture.Clean());
  return true;
}
Q4T_TEST(file_legacy_resource_rejection_cleans_and_publishes_nothing) {
  Fixture fixture;
  FileDocument output;
  fixture.options.io.write_bytes = 1;
  Q4T_CHECK(fixture.Read(Input("word.doc"), &output).http_status == 413 &&
            output.text.empty() && fixture.Clean());
  fixture.options.io = {};
  fixture.options.io.file_creations = 1;
  Q4T_CHECK(fixture.Read(Input("word.doc"), &output).http_status == 413 &&
            fixture.Clean());
  fixture.options.io = {};
  FileDocumentLimits limits;
  limits.max_text_bytes = 4;
  Q4T_CHECK(fixture.Read(Input("word.doc"), &output, limits).http_status ==
                413 &&
            output.text.empty() && fixture.Clean());
  return true;
}
Q4T_TEST(file_legacy_preflight_and_active_cancellation_are_bounded) {
  Fixture fixture;
  FileDocument output;
  auto access =
      BinaryAccess{"legacy-test", Clock::now() + 30s, [] { return true; }};
  Q4T_CHECK(ParseLegacyFileDocument(Input("word.doc"), {}, fixture.options,
                                    access, &output, fixture.control)
                    .http_status == 499 &&
            fixture.control.counters->started == 0);
  access.cancelled = [&] { return fixture.control.counters->started > 0; };
  const auto start = Clock::now();
  Q4T_CHECK(ParseLegacyFileDocument(Input("word.doc"), {}, fixture.options,
                                    access, &output, fixture.control)
                .http_status == 499);
  Q4T_CHECK(Clock::now() - start < 1s && fixture.Clean());
  fixture.options.timeout_ms = 1;
  Q4T_CHECK(fixture.Read(Input("word.doc"), &output).http_status == 408 &&
            output.text.empty() && fixture.Clean());
  return true;
}
Q4T_TEST(file_legacy_workspace_lease_ownership_and_unrelated_data) {
  Fixture fixture;
  const auto root = fixture.options.work_directory;
  {
    legacy_detail::Workspace first, second;
    Q4T_CHECK(first.Open(root) && !second.Open(root));
    Q4T_CHECK(first.Create(std::string(32, '1')));
    Q4T_CHECK(!first.Create(std::string(32, '2')));
  }
  {
    legacy_detail::Workspace next;
    Q4T_CHECK(next.Open(root) && next.Clean() && fixture.Clean());
  }
  std::ofstream(root + "/unrelated") << "retain";
  {
    legacy_detail::Workspace refused;
    Q4T_CHECK(!refused.Open(root));
  }
  Q4T_CHECK(Bytes(root + "/unrelated") == "retain");
  fs::remove(root + "/unrelated");
  fs::create_directory_symlink(root, fixture.base / "alias");
  legacy_detail::Workspace symlink;
  Q4T_CHECK(!symlink.Open((fixture.base / "alias").string()));
  return true;
}
Q4T_TEST(file_legacy_workspace_cleanup_deep_tree_never_follows_symlink) {
  Fixture fixture;
  const auto outside = fixture.base / "private.txt";
  std::ofstream(outside) << "retain";
  legacy_detail::Workspace workspace;
  Q4T_CHECK(workspace.Open(fixture.options.work_directory) &&
            workspace.Create(std::string(32, 'b')));
  fs::create_symlink(outside, fs::path(workspace.Path()) / "link");
  int current = dup(workspace.Directory());
  Q4T_CHECK(current >= 0);
  for (int i = 0; i < 128; ++i) {
    Q4T_CHECK(mkdirat(current, "deep", 0700) == 0);
    const int next = openat(current, "deep", O_RDONLY | O_DIRECTORY);
    close(current);
    Q4T_CHECK(next >= 0);
    current = next;
  }
  close(current);
  Q4T_CHECK(workspace.Clean() && Bytes(outside) == "retain" && fixture.Clean());
  return true;
}
Q4T_TEST(
    file_legacy_sandbox_enforces_files_network_writes_and_thread_inheritance) {
  Fixture fixture;
  std::ofstream(fixture.base / "private.txt") << "retain";
  fixture.control.domain = IsolatedWorkerDomain::kFileParse;
  std::unique_ptr<detail::GrammarProcess> process;
  Q4T_CHECK(detail::GrammarProcess::Start("--legacy-sandbox-fixture",
                                          fixture.control, &process));
  Q4T_CHECK(process->ReceiveIoBudget({}, Clock::now() + 3s));
  std::string output;
  Q4T_CHECK(process->CollectOutput(&output, 1, Clock::now() + 3s) &&
            output == "Y");
  Q4T_CHECK(fixture.control.counters->active == 0 &&
            Bytes(fixture.base / "private.txt") == "retain");
  legacy_detail::Workspace cleanup;
  Q4T_CHECK(cleanup.Open(fixture.options.work_directory) && cleanup.Clean() &&
            fixture.Clean());
  return true;
}

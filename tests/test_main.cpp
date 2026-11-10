// Strict by default. --required-list selects exact names and forbids skips.
#include "q4t/test.h"

#include <fstream>
#include <string>

#if defined(Q4T_GRAMMAR_PROCESS_TESTS) || defined(Q4T_CACHE_WORKER_ENTRY)
#include "q4t/server/cache_io.h"
#endif

#ifdef Q4T_ENABLE_XGRAMMAR
#include "q4t/server/structured_decoder.h"
#endif

#ifdef Q4T_GRAMMAR_PROCESS_TESTS
#include "q4t/server/object_store.h"
#include "q4t/server/file_fetch.h"
#include "q4t/server/file_document_worker.h"
#include "q4t/server/file_pdf.h"
#include "q4t/server/file_legacy.h"
#include "q4t/server/state_key_admin.h"
int CacheIoFixture(int argc, char** argv);
int GrammarProcessFixture(int argc, char** argv);
int ConversationApiFixture(int argc, char** argv);
int FilesApiFixture(int argc, char** argv);
int FileFetchFixture(int argc, char** argv);
int FileSourceFixture(int argc, char** argv);
int DocumentSandboxFixture(int argc, char** argv);
int WorkerIoBudgetFixture(int argc, char** argv);
int LegacyDocumentFixture(int argc, char** argv);
int PdfSandboxFixture(int argc, char** argv);
int PackageFixture(int argc, char** argv);
int ResponsesApiFixture(int argc, char** argv);
int ResponsesJobsFixture(int argc, char** argv);
#endif

int main(int argc, char** argv) {
#if defined(Q4T_GRAMMAR_PROCESS_TESTS) || defined(Q4T_CACHE_WORKER_ENTRY)
  const int cache_worker = q4t::server::MaybeRunCacheWorker(argc, argv);
  if (cache_worker >= 0) return cache_worker;
#endif
#ifdef Q4T_ENABLE_XGRAMMAR
  const int worker = q4t::server::MaybeRunGrammarWorker(argc, argv);
  if (worker >= 0) return worker;
#endif
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
  const int cache_fixture = CacheIoFixture(argc, argv);
  if (cache_fixture >= 0) return cache_fixture;
  const int object_worker = q4t::server::MaybeRunObjectWorker(argc, argv);
  if (object_worker >= 0) return object_worker;
  const int fetch_worker = q4t::server::MaybeRunFileFetchWorker(argc, argv);
  if (fetch_worker >= 0) return fetch_worker;
  const int document_worker = q4t::server::MaybeRunFileDocumentWorker(argc, argv);
  if (document_worker >= 0) return document_worker;
  const int pdf_worker = q4t::server::MaybeRunFilePdfWorker(argc, argv);
  if (pdf_worker >= 0) return pdf_worker;
  const int legacy_worker = q4t::server::MaybeRunFileLegacyWorker(argc, argv);
  if (legacy_worker >= 0) return legacy_worker;
  const int state_keys = q4t::server::MaybeRunStateKeyCommand(argc, argv);
  if (state_keys >= 0) return state_keys;
  const int retention = q4t::server::MaybeRunStateRetentionCommand(argc, argv);
  if (retention >= 0) return retention;
  const int fixture = GrammarProcessFixture(argc, argv);
  if (fixture >= 0) return fixture;
  const int conversation = ConversationApiFixture(argc, argv);
  if (conversation >= 0) return conversation;
  const int files = FilesApiFixture(argc, argv);
  if (files >= 0) return files;
  const int file_fetch = FileFetchFixture(argc, argv);
  if (file_fetch >= 0) return file_fetch;
  const int file_source = FileSourceFixture(argc, argv);
  if (file_source >= 0) return file_source;
  const int document_sandbox = DocumentSandboxFixture(argc, argv);
  if (document_sandbox >= 0) return document_sandbox;
  const int io_budget = WorkerIoBudgetFixture(argc, argv);
  if (io_budget >= 0) return io_budget;
  const int legacy_fixture = LegacyDocumentFixture(argc, argv);
  if (legacy_fixture >= 0) return legacy_fixture;
  const int pdf_sandbox = PdfSandboxFixture(argc, argv);
  if (pdf_sandbox >= 0) return pdf_sandbox;
  const int package_fixture = PackageFixture(argc, argv);
  if (package_fixture >= 0) return package_fixture;
  const int responses = ResponsesApiFixture(argc, argv);
  if (responses >= 0) return responses;
  const int jobs = ResponsesJobsFixture(argc, argv);
  if (jobs >= 0) return jobs;
#endif
  std::string filter, required;
  bool list = false, allow_skips = false, has_filter = false;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    if (arg == "--list" && !list) {
      list = true;
    } else if (arg == "--allow-skips" && !allow_skips) {
      allow_skips = true;
    } else if (arg == "--required-list" && required.empty() && i + 1 < argc) {
      required = argv[++i];
      if (required.empty()) return 2;
    } else if (!arg.starts_with('-') && !has_filter) {
      filter = arg;
      has_filter = true;
    } else {
      std::fprintf(stderr, "Invalid test arguments\n");
      return 2;
    }
  }
  if ((!required.empty() && (has_filter || allow_skips || list)) ||
      (list && (has_filter || allow_skips))) return 2;
  if (list) {
    for (const auto& c : q4t::test::Registry())
      std::printf("%s\n", c.name.c_str());
    return q4t::test::Registry().empty() ? 2 : 0;
  }
  std::vector<std::string> names;
  if (!required.empty()) {
    std::ifstream input(required);
    if (!input) return 2;
    std::string name;
    while (std::getline(input, name)) {
      if (!name.empty() && name.back() == '\r') name.pop_back();
      if (!name.empty() && !name.starts_with('#')) names.push_back(name);
    }
    if (input.bad()) return 2;
  } else {
    for (const auto& c : q4t::test::Registry())
      if (c.name.find(filter) != std::string::npos) names.push_back(c.name);
  }
  return q4t::test::RunTests(names, allow_skips);
}

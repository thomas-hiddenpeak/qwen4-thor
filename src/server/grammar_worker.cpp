#include "q4t/server/structured_decoder.h"

#include <unistd.h>

#include <cstring>
#include <cstdio>
#include <exception>
#include <new>
#include <stdexcept>

#include "grammar_worker_internal.h"
#include "q4t/server/structured_admission.h"
#include "q4t/server/custom_grammar.h"
#include "q4t/server/output_schema.h"
#include "schema_idna.h"
#include "schema_token_guard.h"

#ifdef Q4T_ENABLE_XGRAMMAR
#include <xgrammar/xgrammar.h>
#endif

namespace q4t::server {
#ifdef Q4T_ENABLE_XGRAMMAR
namespace {
using detail::GrammarReply;
using detail::WorkerCode;
using detail::WorkerTransfer;

class Input {
 public:
  explicit Input(const std::vector<char>& bytes) : bytes_(bytes) {}
  uint32_t Integer() {
    uint32_t value;
    Read(&value, sizeof(value));
    return value;
  }
  std::string String() {
    const uint32_t size = Integer();
    if (size > bytes_.size() - offset_)
      throw std::runtime_error("input length");
    std::string value(bytes_.data() + offset_, size);
    offset_ += size;
    return value;
  }
  bool Finished() const { return offset_ == bytes_.size(); }

 private:
  void Read(void* out, size_t size) {
    if (size > bytes_.size() - offset_)
      throw std::runtime_error("input length");
    std::memcpy(out, bytes_.data() + offset_, size);
    offset_ += size;
  }
  const std::vector<char>& bytes_;
  size_t offset_ = 0;
};

uint64_t ReportedRss() {
  FILE* file = std::fopen("/proc/self/status", "r");
  if (!file) return 0;
  uint64_t bytes = 0;
  char line[256];
  while (std::fgets(line, sizeof(line), file)) {
    unsigned long kb;
    if (std::sscanf(line, "VmHWM: %lu kB", &kb) == 1) {
      bytes = static_cast<uint64_t>(kb) * 1024;
      break;
    }
  }
  std::fclose(file);
  return bytes;
}

bool Reply(WorkerCode code) {
  GrammarReply reply;
  reply.code = code;
  reply.reported_rss_bytes = ReportedRss();
  return WorkerTransfer(&reply, sizeof(reply), true);
}

void RequireGuard(const Status& status) {
  if (status) return;
  if (status.message().starts_with("schema_resource_exhausted:"))
    throw std::length_error("schema guard resource limit");
  if (status.message().starts_with("schema_dependency_unavailable:"))
    throw SchemaDependencyUnavailable("schema guard dependency unavailable");
  throw std::runtime_error("schema guard rejected output");
}

void CheckGuardBindings(const io::Json& format, const SchemaTokenGuard& guard,
                         int depth = 0) {
  if (depth > 32) throw std::length_error("structural format depth");
  const auto type = format.GetString("type");
  if (type == "q4t_schema") {
    const auto* schema = format.Find("schema");
    if (!schema || !guard.Covers(format.GetString("scope"), *schema))
      throw std::runtime_error("schema semantic binding missing");
    return;
  }
  // Schema literals and custom grammar source are data, never formats.
  if (type == "json_schema" || type == "q4t_custom") return;
  for (const auto& [key, value] : format.object)
    if (key != "q4t_guard") CheckGuardBindings(value, guard, depth + 1);
  for (const auto& value : format.array)
    CheckGuardBindings(value, guard, depth + 1);
}

bool Mask(xgrammar::GrammarMatcher& matcher, std::vector<int32_t>& mask,
          SchemaTokenGuard& guard, const std::vector<std::string>& vocabulary,
          const std::vector<int32_t>& stops) {
  GrammarReply reply;
  auto receipts = guard.Receipts();
  reply.receipts = static_cast<uint32_t>(receipts.size());
  const auto send_receipts = [&] {
    return receipts.empty() ||
           WorkerTransfer(receipts.data(), receipts.size() * sizeof(SchemaReceipt),
                          true);
  };
  if (matcher.IsTerminated()) {
    reply.flags = 1;
    reply.reported_rss_bytes = ReportedRss();
    return WorkerTransfer(&reply, sizeof(reply), true) && send_receipts();
  }
  std::fill(mask.begin(), mask.end(), -1);
  int64_t shape = static_cast<int64_t>(mask.size());
  DLTensor tensor{};
  tensor.data = mask.data();
  tensor.device = {kDLCPU, 0};
  tensor.ndim = 1;
  tensor.dtype = {kDLInt, 32, 1};
  tensor.shape = &shape;
  reply.flags = matcher.FillNextTokenBitmask(&tensor) ? 2 : 0;
  if (guard.Active()) {
    RequireGuard(guard.Filter(vocabulary, stops, mask));
    reply.flags = 2;
  }
  reply.words = static_cast<uint32_t>(mask.size());
  reply.reported_rss_bytes = ReportedRss();
  return WorkerTransfer(&reply, sizeof(reply), true) &&
         WorkerTransfer(mask.data(), mask.size() * sizeof(int32_t), true) &&
         send_receipts();
}

int WorkerMain(int argc, char** argv) {
  // Backup watchdog also bounds a child orphaned during startup. The parent
  // enforces a tighter monotonic deadline including exec and transport.
  alarm(kGrammarCompileMilliseconds / 1000 + 1);
  if (!detail::EnterGrammarWorker(argc, argv)) {
    Reply(WorkerCode::kResource);
    return 2;
  }
  if (!Reply(WorkerCode::kOk)) return 2;
  try {
    uint32_t size;
    if (!WorkerTransfer(&size, sizeof(size), false) ||
        size > detail::kGrammarInputLimit)
      return 2;
    std::vector<char> bytes(size);
    if (!WorkerTransfer(bytes.data(), bytes.size(), false)) return 2;
    Input input(bytes);
    const uint32_t vocab_size = input.Integer();
    const uint32_t count = input.Integer();
    const uint32_t stops = input.Integer();
    if (!vocab_size || vocab_size > 300000 || !count || count > vocab_size ||
        !stops || stops > 32)
      return 2;
    const auto tag = input.String();
    if (tag.empty() || tag.size() > detail::kGrammarDescriptionLimit) return 2;
    std::vector<int32_t> stop_ids;
    for (uint32_t i = 0; i < stops; ++i) {
      const uint32_t token = input.Integer();
      if (token >= count) return 2;
      stop_ids.push_back(static_cast<int32_t>(token));
    }
    std::vector<std::string> vocabulary(count);
    VocabularyStorageBudget budget;
    if (!budget.Charge(vocabulary.capacity() * sizeof(std::string)))
      throw std::bad_alloc();
    for (auto& token : vocabulary) {
      token = input.String();
      if (!budget.Charge(token.capacity() + 1)) throw std::bad_alloc();
    }
    if (!input.Finished()) return 2;
    io::Json document;
    RequireGuard(io::ParseJson(tag, &document, true,
                               {64, 262144, detail::kGrammarDescriptionLimit,
                                true, true}));
    SchemaTokenGuard guard;
    OutputSchemaGraph::StringCheck string_check;
#ifdef Q4T_GRAMMAR_PROCESS_TESTS
    // Host-test ELF only. The production worker has no injected dependency
    // selector and inherits only its fixed locale/allocator environment.
    const auto test_failure = document.GetString("q4t_test_schema_failure");
    if (test_failure == "dependency" || test_failure == "resource") {
      string_check = [test_failure](const io::Json&, std::string_view,
                                    size_t*, bool*) {
        return Status::Fail(test_failure == "dependency"
                                ? "schema_dependency_unavailable: host fixture"
                                : "schema_resource_exhausted: host fixture");
      };
      std::erase_if(document.object, [](const auto& member) {
        return member.first == "q4t_test_schema_failure";
      });
    }
#endif
    if (const auto* configuration = document.Find("q4t_guard"))
      RequireGuard(guard.Init(*configuration, std::move(string_check)));
    CheckGuardBindings(document, guard);
    const xgrammar::TokenizerInfo tokenizer(
        vocabulary, xgrammar::VocabType::RAW, vocab_size, stop_ids, false);
    // Retain all compiler and matcher allocation inside this limited process.
    xgrammar::GrammarCompiler compiler(tokenizer, 1, false, 0);
    if (!Reply(WorkerCode::kOk)) return 2;
    const auto grammar = compiler.CompileStructuralTag(
        ExpandCustomGrammarFormats(OutputJson(document)));
    if (grammar.MemorySizeBytes() > kCompiledGrammarLimit) {
      Reply(WorkerCode::kResource);
      return 2;
    }
    xgrammar::GrammarMatcher matcher(grammar);
    std::vector<int32_t> mask((vocab_size + 31) / 32, -1);
    if (!Mask(matcher, mask, guard, vocabulary, stop_ids)) return 2;
    alarm(0);
    while (!matcher.IsTerminated()) {
      int32_t token;
      if (!WorkerTransfer(&token, sizeof(token), false)) return 2;
      alarm(kGrammarStepMilliseconds / 1000 + 1);
      if (token < 0 || static_cast<uint32_t>(token) >= vocab_size ||
          static_cast<size_t>(token) >= vocabulary.size() ||
          !matcher.AcceptToken(token)) {
        Reply(WorkerCode::kInvalid);
        return 2;
      }
      const bool stop = std::find(stop_ids.begin(), stop_ids.end(), token) !=
                        stop_ids.end();
      RequireGuard(guard.Accept(vocabulary[token], stop));
      if (!Mask(matcher, mask, guard, vocabulary, stop_ids)) return 2;
      alarm(0);
    }
    return 0;
  } catch (const std::bad_alloc&) {
    Reply(WorkerCode::kResource);
  } catch (const std::length_error&) {
    Reply(WorkerCode::kResource);
  } catch (const SchemaDependencyUnavailable&) {
    Reply(WorkerCode::kDependency);
  } catch (const std::exception&) {
    // Library diagnostics can contain user schemas; keep the wire error fixed.
    Reply(WorkerCode::kInvalid);
  } catch (...) {
    Reply(WorkerCode::kInternal);
  }
  return 2;
}
}  // namespace
#endif

int MaybeRunGrammarWorker(int argc, char** argv) {
  if (argc < 2 || std::strcmp(argv[1], "--q4t-grammar-worker")) return -1;
#ifdef Q4T_ENABLE_XGRAMMAR
  return WorkerMain(argc, argv);
#else
  return 2;
#endif
}
}  // namespace q4t::server

#include "schema_token_guard.h"

#include <algorithm>
#include <unordered_map>

#include "q4t/server/schema_prefix.h"
#include "schema_string.h"

namespace q4t::server {
namespace {
constexpr size_t kOutputLimit = 1024 * 1024;
constexpr size_t kWork = 8 * 1024 * 1024;
bool Space(char c) { return c == ' ' || c == '\t' || c == '\n' || c == '\r'; }
bool ExternalFailure(const Status& status) {
  return status.message().starts_with("schema_resource_exhausted:") ||
         status.message().starts_with("schema_dependency_unavailable:");
}
Status Resource() {
  return Status::Fail("schema_resource_exhausted: semantic token guard");
}
}  // namespace

struct SchemaTokenGuard::Impl {
  struct Binding {
    io::Json schema;
    OutputSchemaGraph graph;
    trace::Digest digest{};
    std::string begin, end;
  };
  std::unique_ptr<Binding> content;
  std::vector<std::unique_ptr<Binding>> tools;
  OutputSchemaGraph::StringCheck strings;
  bool thinking = false;
  bool json_content = false;
  std::string accepted;
  std::vector<SchemaReceipt> receipts;

  Status Add(const io::Json& spec, std::unique_ptr<Binding>* out,
             std::string_view scope) {
    const auto* schema = spec.Find("schema");
    if (!schema) return Status::Fail("missing guard schema");
    auto binding = std::make_unique<Binding>();
    binding->schema = *schema;
    auto status =
        binding->graph.Init(binding->schema, spec.GetBool("strict"), nullptr);
    if (status)
      status = SchemaBindingDigest(scope, binding->schema, &binding->digest);
    if (!status) return status;
    binding->begin = spec.GetString("begin");
    binding->end = spec.GetString("end");
    *out = std::move(binding);
    return {};
  }

  Status Evaluate(std::string_view text, bool stop, size_t* work, bool* valid,
                  std::vector<SchemaReceipt>* completed,
                  const OutputSchemaGraph::StringCheck& check) const {
    *valid = false;
    if (completed) completed->clear();
    size_t completed_count = 0;
    bool saw_tool = false;
    if (text.size() > kOutputLimit) return Resource();
    if (text.size() > *work) return Resource();
    *work -= text.size();
    if (thinking) {
      const auto close = text.find("</think>");
      if (close == text.npos) {
        *valid = !stop;
        return {};
      }
      text.remove_prefix(close + 8);
    }
    while (true) {
      while (!text.empty() && Space(text.front())) text.remove_prefix(1);
      if (text.empty()) {
        *valid = !stop || !content || completed_count != 0 || saw_tool;
        return {};
      }
      const Binding* binding = nullptr;
      bool partial = false;
      for (const auto& candidate : tools) {
        if (text.starts_with(candidate->begin)) {
          binding = candidate.get();
          text.remove_prefix(candidate->begin.size());
          break;
        }
        partial |= candidate->begin.starts_with(text);
      }
      if (partial && !binding) {
        *valid = !stop;
        return {};
      }
      if (!binding && text.starts_with("<tool_call>")) {
        // A legacy/simple or custom tool is governed by its existing grammar.
        // JSON-encoded custom input escapes '<', preventing frame confusion.
        const auto end = text.find("</tool_call>");
        if (end == text.npos) {
          *valid = !stop;
          return {};
        }
        text.remove_prefix(end + 12);
        saw_tool = true;
        continue;
      }
      if (!binding && content) binding = content.get();
      if (!binding && json_content && text.front() == '{') {
        // A simple JSON schema/json_object branch is already checked by the
        // lexical matcher and parent's native checker. Still parse its JSON
        // boundaries here: marker-shaped strings are never tool frames.
        SchemaPrefixValue prefix;
        size_t consumed = 0;
        auto status = ParseSchemaPrefix(text, &prefix, &consumed, work);
        if (!status) return ExternalFailure(status) ? status : Status();
        if (!prefix.complete) {
          *valid = !stop;
          return {};
        }
        text.remove_prefix(consumed);
        continue;
      }
      if (!binding) {
        // Free content before an auto tool. Partial marker suffixes remain
        // under the lexical grammar until the complete next frame arrives.
        const auto next = text.find("<tool_call>");
        if (next == text.npos) {
          *valid = true;
          return {};
        }
        text.remove_prefix(next);
        continue;
      }
      SchemaPrefixValue prefix;
      size_t consumed = 0;
      auto status = ParseSchemaPrefix(text, &prefix, &consumed, work);
      if (status) status = binding->graph.CheckPrefix(prefix, work, check);
      if (!status) return ExternalFailure(status) ? status : Status();
      if (!prefix.complete) {
        *valid = !stop;
        return {};
      }
      if (++completed_count > 128) return Resource();
      // CheckPrefix invokes the full independent graph validator for a
      // completed root. Its result, rather than lexical acceptance, permits
      // this digest to cross the worker boundary.
      if (completed) {
        SchemaReceipt receipt;
        receipt.binding = binding->digest;
        status = SchemaValueDigest(prefix.value, &receipt.value);
        if (!status) return status;
        completed->push_back(receipt);
      }
      text.remove_prefix(consumed);
      // JSON parser consumes trailing whitespace; framing starts with the
      // next non-whitespace byte. Lexical grammar still enforces exact bytes.
      std::string_view end = binding->end;
      while (!end.empty() && Space(end.front())) end.remove_prefix(1);
      if (!text.starts_with(end)) {
        *valid = !stop && end.starts_with(text);
        return {};
      }
      text.remove_prefix(end.size());
      saw_tool |= binding != content.get();
    }
  }
};

SchemaTokenGuard::SchemaTokenGuard() : impl_(std::make_unique<Impl>()) {}
SchemaTokenGuard::~SchemaTokenGuard() = default;
Status SchemaTokenGuard::Init(const io::Json& configuration,
                              OutputSchemaGraph::StringCheck strings) {
  impl_ = std::make_unique<Impl>();
  impl_->strings = strings ? std::move(strings) : CheckSchemaString;
  if (configuration.IsNull()) return {};
  if (!configuration.IsObject()) return Status::Fail("invalid semantic guard");
  impl_->thinking = configuration.GetBool("thinking");
  impl_->json_content = configuration.GetBool("json_content");
  if (const auto* content = configuration.Find("content")) {
    auto status = impl_->Add(*content, &impl_->content, "");
    if (!status) return status;
  }
  if (const auto* tools = configuration.GetArray("tools")) {
    if (tools->array.size() > 128) return Resource();
    for (const auto& tool : tools->array) {
      std::unique_ptr<Impl::Binding> binding;
      auto status = impl_->Add(tool, &binding, tool.GetString("name"));
      if (!status) return status;
      if (!binding->begin.starts_with("<tool_call>") ||
          !binding->end.ends_with("</tool_call>") ||
          binding->begin.size() > 512 || binding->end.size() > 512)
        return Status::Fail("invalid schema tool frame");
      for (const auto& old : impl_->tools)
        if (old->begin == binding->begin)
          return Status::Fail("duplicate schema tool frame");
      impl_->tools.push_back(std::move(binding));
    }
  }
  return {};
}
bool SchemaTokenGuard::Active() const {
  return impl_->content || !impl_->tools.empty();
}
bool SchemaTokenGuard::Covers(std::string_view scope,
                              const io::Json& schema) const {
  trace::Digest digest;
  if (!SchemaBindingDigest(scope, schema, &digest)) return false;
  if (impl_->content && impl_->content->digest == digest) return true;
  return std::any_of(
      impl_->tools.begin(), impl_->tools.end(),
      [&](const auto& binding) { return binding->digest == digest; });
}
const std::vector<SchemaReceipt>& SchemaTokenGuard::Receipts() const {
  return impl_->receipts;
}
Status SchemaTokenGuard::Filter(std::span<const std::string> vocabulary,
                                std::span<const int32_t> stops,
                                std::span<int32_t> mask) {
  if (!Active()) return {};
  if (vocabulary.size() > mask.size() * 32)
    return Status::Fail("invalid semantic mask shape");
  size_t work = kWork;
  // Completed strings shared across candidate suffixes are checked once per
  // step. Cached dependency/resource errors are never converted into false.
  std::unordered_map<const io::Json*, std::unordered_map<std::string, bool>>
      cache;
  size_t cache_bytes = 0, cache_values = 0;
  const auto check = [&](const io::Json& schema, std::string_view value,
                         size_t* budget, bool* accepted) -> Status {
    auto& values = cache[&schema];
    const auto found = values.find(std::string(value));
    if (found != values.end()) {
      *accepted = found->second;
      return {};
    }
    auto status = impl_->strings(schema, value, budget, accepted);
    if (status && cache_values < 8192 &&
        cache_bytes + value.size() < kOutputLimit) {
      values.emplace(value, *accepted);
      cache_bytes += value.size();
      ++cache_values;
    }
    return status;
  };
  std::string_view body = impl_->accepted;
  bool reasoning = false;
  if (impl_->thinking) {
    const auto end = body.find("</think>");
    reasoning = end == body.npos;
    if (!reasoning) body.remove_prefix(end + 8);
  }
  bool in_string = false, escaped = false;
  if (!reasoning)
    for (const char c : body) {
      if (escaped) {
        escaped = false;
      } else if (in_string && c == '\\') {
        escaped = true;
      } else if (c == '"') {
        in_string = !in_string;
      }
    }
  const bool free_content =
      !impl_->content &&
      (body.rfind("<tool_call>") == body.npos ||
       (body.rfind("</tool_call>") != body.npos &&
        body.rfind("</tool_call>") > body.rfind("<tool_call>")));
  for (size_t i = 0; i < vocabulary.size(); ++i) {
    const auto bit = uint32_t{1} << (i % 32);
    if (!(static_cast<uint32_t>(mask[i / 32]) & bit)) continue;
    const bool stop = std::find(stops.begin(), stops.end(), i) != stops.end();
    if (!stop && vocabulary[i].empty()) {
      mask[i / 32] =
          static_cast<int32_t>(static_cast<uint32_t>(mask[i / 32]) & ~bit);
      continue;
    }
    if (vocabulary[i].size() > kOutputLimit - impl_->accepted.size())
      return Resource();
    if (!stop) {
      // The lexical matcher already proves syntax/length/pattern viability.
      // No value becomes complete inside a string without a quote; no JSON
      // value exists before a reasoning/tool boundary. Check only candidates
      // that can cross those boundaries, including split marker bytes.
      if (!reasoning && in_string &&
          vocabulary[i].find('"') == std::string::npos)
        continue;
      const auto crosses = [&](std::string_view marker) {
        const auto tail = body.substr(body.size() > marker.size() - 1
                                          ? body.size() - marker.size() + 1
                                          : 0);
        return (std::string(tail) + vocabulary[i]).find(marker) !=
               std::string::npos;
      };
      if (reasoning && !crosses("</think>")) continue;
      if (!reasoning && free_content && !crosses("<tool_call>")) continue;
    }
    std::string candidate = impl_->accepted;
    if (!stop) candidate += vocabulary[i];
    bool valid = false;
    auto status =
        impl_->Evaluate(candidate, stop, &work, &valid, nullptr, check);
    if (!status) return status;
    if (!valid)
      mask[i / 32] =
          static_cast<int32_t>(static_cast<uint32_t>(mask[i / 32]) & ~bit);
  }
  return {};
}
Status SchemaTokenGuard::Accept(std::string_view bytes, bool stop) {
  if (!Active()) return {};
  if (bytes.size() > kOutputLimit - impl_->accepted.size()) return Resource();
  std::string candidate = impl_->accepted;
  if (!stop) candidate += bytes;
  size_t work = kWork;
  bool valid = false;
  std::vector<SchemaReceipt> completed;
  auto status = impl_->Evaluate(candidate, stop, &work, &valid, &completed,
                                impl_->strings);
  if (!status) return status;
  if (!valid) return Status::Fail("invalid structured value token");
  impl_->accepted = std::move(candidate);
  impl_->receipts = std::move(completed);
  return {};
}

}  // namespace q4t::server

#include "q4t/server/prompt_cache_diagnostics.h"

#include <algorithm>

#include "q4t/server/schema_graph.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
const Json& Get(const Json& object, const char* name) {
  static const Json absent;
  const auto* value = object.Find(name);
  return value ? *value : absent;
}
std::string Key(std::string_view scope, std::string_view id) {
  return std::to_string(scope.size()) + ':' + std::string(scope) +
         std::string(id);
}
trace::Digest Block(std::span<const int32_t> tokens) {
  return trace::Sha256(
      {reinterpret_cast<const uint8_t*>(tokens.data()), tokens.size_bytes()});
}
Json Type(std::string type) {
  return Object({{"type", String(std::move(type))}});
}
constexpr const char* kReasons[] = {
    "model_changed",    "prompt_cache_key_changed", "service_tier_changed",
    "tools_changed",    "text_format_changed",      "reasoning_effort_changed",
    "verbosity_changed"};
}  // namespace

bool PromptCacheDiagnostics::Settings(const io::Json& options,
                                      std::array<trace::Digest, 7>* hashes) {
  const Json* values[] = {&Get(options, "model"),
                          &Get(options, "prompt_cache_key"),
                          &Get(options, "service_tier"),
                          &Get(options, "tools"),
                          &Get(Get(options, "text"), "format"),
                          &Get(Get(options, "reasoning"), "effort"),
                          &Get(Get(options, "text"), "verbosity")};
  for (size_t i = 0; i < hashes->size(); ++i) {
    std::string canonical;
    if (!CanonicalSchemaValue(*values[i], &canonical)) return false;
    (*hashes)[i] = trace::Sha256(
        {reinterpret_cast<const uint8_t*>(canonical.data()), canonical.size()});
  }
  return true;
}

void PromptCacheDiagnostics::Expire(Clock::time_point now) {
  std::erase_if(records_, [&](const auto& entry) {
    return now - entry.second.created >= std::chrono::hours(1);
  });
}

io::Json PromptCacheDiagnostics::Compare(
    std::string_view scope, std::string_view comparison,
    const io::Json& settings, std::span<const int32_t> tokens, int restored,
    bool available, bool compacted, Clock::time_point now) noexcept {
  try {
    if (comparison.empty()) return {};
    if (tokens.size() > kMaxTokens || restored < 0 ||
        static_cast<size_t>(restored) > tokens.size())
      return Type("unavailable");
    const std::lock_guard lock(mutex_);
    Expire(now);
    const auto it = records_.find(Key(scope, comparison));
    if (it == records_.end()) return Type("comparison_response_not_found");
    if (!available) return Type("unavailable");
    const auto& old = it->second;
    std::array<trace::Digest, 7> hashes;
    if (!Settings(settings, &hashes)) return Type("unavailable");
    std::string reason;
    for (size_t i = 0; i < hashes.size(); ++i)
      if (hashes[i] != old.settings[i]) {
        reason = kReasons[i];
        break;
      }
    int matching = 0;
    if (reason.empty()) {
      for (const auto& expected : old.blocks) {
        const int count = std::min(kBlockTokens, old.reusable - matching);
        if (tokens.size() < static_cast<size_t>(matching + count) ||
            Block(tokens.subspan(matching, count)) != expected)
          break;
        matching += count;
      }
      if (matching < old.reusable)
        reason =
            compacted != old.compacted ? "context_compacted" : "input_changed";
    }
    if (reason.empty())
      return Type(restored >= old.reusable ? "cache_hit" : "unavailable");
    return Object({{"type", String("cache_miss")},
                   {"reason", String(std::move(reason))},
                   {"comparison_reusable_tokens", Number(old.reusable)},
                   {"cache_missed_tokens", Number(old.reusable - matching)}});
  } catch (...) {
    // Omitting optional diagnostics under allocation failure is safer than
    // failing an otherwise healthy model response. No store or model mutates.
    return {};
  }
}

std::optional<PromptCacheDiagnostics::Observation>
PromptCacheDiagnostics::Observe(const io::Json& settings,
                                std::span<const int32_t> tokens, int reusable,
                                bool compacted) noexcept {
  try {
    if (reusable < 1024 || reusable > kMaxTokens ||
        static_cast<size_t>(reusable) > tokens.size())
      return {};
    Observation record;
    record.reusable = reusable;
    record.compacted = compacted;
    if (!Settings(settings, &record.settings)) return {};
    for (int i = 0; i < reusable; i += kBlockTokens)
      record.blocks.push_back(
          Block(tokens.subspan(i, std::min(kBlockTokens, reusable - i))));
    return record;
  } catch (...) {
    return {};
  }
}

bool PromptCacheDiagnostics::Remember(std::string_view scope,
                                      std::string_view id,
                                      Observation observation,
                                      Clock::time_point now) noexcept {
  try {
    if (id.empty() || scope.size() > 4096 || id.size() > 256 ||
        observation.reusable < 1024 || observation.reusable > kMaxTokens ||
        observation.blocks.size() !=
            static_cast<size_t>((observation.reusable + kBlockTokens - 1) /
                                kBlockTokens))
      return false;
    Record record;
    static_cast<Observation&>(record) = std::move(observation);
    record.scope = scope;
    record.created = now;
    const std::lock_guard lock(mutex_);
    Expire(now);
    const auto key = Key(scope, id);
    // One identity can be observed once; a diagnostic cannot rewrite history.
    if (records_.contains(key)) return false;
    size_t client_count = 0;
    auto client_oldest = records_.end();
    auto oldest = records_.end();
    for (auto it = records_.begin(); it != records_.end(); ++it) {
      if (oldest == records_.end() ||
          it->second.created < oldest->second.created)
        oldest = it;
      if (it->second.scope != scope) continue;
      ++client_count;
      if (client_oldest == records_.end() ||
          it->second.created < client_oldest->second.created)
        client_oldest = it;
    }
    if (client_count >= kClientEntries)
      records_.erase(client_oldest);
    else if (records_.size() >= kMaxEntries)
      records_.erase(oldest);
    records_.emplace(key, std::move(record));
    return true;
  } catch (...) {
    return false;
  }
}
}  // namespace q4t::server

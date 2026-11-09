#include "q4t/server/prompt_cache_policy.h"

#include <algorithm>
#include <climits>
#include <map>

#include "q4t/server/application_routes.h"
#include "q4t/server/chat_prompt.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/session_cache.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
Status Invalid(std::string path, std::string message, std::string* param) {
  *param = std::move(path);
  return Status::Fail(std::move(message));
}
Status Ttl(const Json& value, const std::string& path, int* seconds,
           std::string* param) {
  if (!value.IsString() || (value.str != "30m" && value.str != "12h"))
    return Invalid(path, "Cache TTL must be 30m or 12h", param);
  *seconds = value.str == "12h" ? 43200 : 1800;
  return {};
}
int PrefixCount(std::span<const size_t> starts, size_t prompt_bytes,
                size_t boundary_bytes) {
  auto end = std::lower_bound(starts.begin(), starts.end(), boundary_bytes);
  if (boundary_bytes == prompt_bytes)
    end = starts.end();
  else if (end != starts.begin() &&
           (end == starts.end() || *end != boundary_bytes))
    end = std::lower_bound(starts.begin(), end, *(end - 1));
  return static_cast<int>(end - starts.begin());
}
}  // namespace

Status ParsePromptCacheOptions(const io::Json& request,
                               PromptCacheOptions* options,
                               std::string* error_param) {
  *options = {};
  error_param->clear();
  if (const auto* key = request.Find("prompt_cache_key");
      key && !key->IsNull()) {
    if (!key->IsString() ||
        std::count_if(key->str.begin(), key->str.end(),
                      [](unsigned char c) { return (c & 0xc0) != 0x80; }) > 512)
      return Invalid("prompt_cache_key", "Cache key exceeds 512 characters",
                     error_param);
    options->key = key->str;
    options->requested = true;
  }
  if (const auto* retention = request.Find("prompt_cache_retention");
      retention && !retention->IsNull()) {
    if (!retention->IsString() ||
        (retention->str != "in_memory" && retention->str != "24h"))
      return Invalid("prompt_cache_retention", "Invalid cache retention",
                     error_param);
    options->retention = retention->str;
    options->retention_requested = true;
    options->requested = true;
  }
  const auto* value = request.Find("prompt_cache_options");
  if (!value || value->IsNull()) return {};
  options->requested = true;
  options->modern_controls = true;
  auto status =
      InputFields(*value, {"mode", "ttl", "prewarm", "comparison_response_id"},
                  error_param, "prompt_cache_options");
  if (!status) return status;
  if (const auto* mode = value->Find("mode")) {
    if (!mode->IsString() ||
        (mode->str != "implicit" && mode->str != "explicit"))
      return Invalid("prompt_cache_options.mode", "Invalid cache mode",
                     error_param);
    options->explicit_only = mode->str == "explicit";
  }
  if (const auto* ttl = value->Find("ttl")) {
    status = Ttl(*ttl, "prompt_cache_options.ttl", &options->ttl_seconds,
                 error_param);
    if (!status) return status;
  }
  if (const auto* prewarm = value->Find("prewarm")) {
    if (!prewarm->IsBool())
      return Invalid("prompt_cache_options.prewarm", "Expected boolean",
                     error_param);
    options->prewarm = prewarm->boolean;
  }
  if (const auto* id = value->Find("comparison_response_id");
      id && !id->IsNull()) {
    if (!id->IsString() || !ValidApplicationId(id->str))
      return Invalid("prompt_cache_options.comparison_response_id",
                     "Invalid comparison response ID", error_param);
    options->comparison_response_id = id->str;
  }
  return {};
}

io::Json PublicPromptCacheOptions(const PromptCacheOptions& options) {
  auto result =
      Object({{"mode", String(options.explicit_only ? "explicit" : "implicit")},
              {"ttl", String(options.ttl_seconds == 43200 ? "12h" : "30m")}});
  if (!options.comparison_response_id.empty())
    Set(&result, "comparison_response_id",
        String(options.comparison_response_id));
  return result;
}

SessionCacheLifetime PromptCacheLifetime(const PromptCacheOptions& options,
                                         int write_ttl, bool responses_api) {
  // Modern per-breakpoint TTL takes precedence. The legacy retention field
  // controls the total lifetime when used on its own. All tiers remain
  // bounded and can be evicted for capacity or worker failure.
  const bool modern = options.modern_controls || write_ttl > 3600 ||
                      (responses_api && !options.retention_requested);
  return {write_ttl, 1800, modern || options.retention == "24h" ? 86400 : 3600};
}

Status ParsePromptCacheBreakpoint(const io::Json* value, std::string_view path,
                                  bool* present, int* ttl_seconds,
                                  std::string* error_param) {
  *present = false;
  *ttl_seconds = 0;
  if (!value || value->IsNull()) return {};
  auto status = InputFields(*value, {"mode", "ttl"}, error_param, path);
  if (!status) return status;
  const auto* mode = value->Find("mode");
  if (!mode || !mode->IsString() || mode->str != "explicit")
    return Invalid(std::string(path) + ".mode",
                   "Cache breakpoint requires explicit mode", error_param);
  if (const auto* ttl = value->Find("ttl")) {
    status = Ttl(*ttl, std::string(path) + ".ttl", ttl_seconds, error_param);
    if (!status) return status;
  }
  *present = true;
  return {};
}

Status PlanPromptCache(const PromptCacheOptions& options,
                       std::span<const PromptCacheBoundary> boundaries,
                       int prompt_tokens, PromptCachePlan* plan) {
  *plan = {};
  if (prompt_tokens < 0 ||
      (options.ttl_seconds != 1800 && options.ttl_seconds != 43200))
    return Status::Fail("Invalid prompt cache planning limits");
  using Kind = PromptCacheBoundary::Kind;
  std::vector<PromptCacheBoundary> explicit_points;
  std::vector<int> eligible;
  std::vector<int> initial;
  for (auto boundary : boundaries) {
    if (boundary.tokens < 0 || boundary.tokens >= prompt_tokens ||
        (boundary.ttl_seconds != 0 && boundary.ttl_seconds != 1800 &&
         boundary.ttl_seconds != 43200))
      return Status::Fail("Invalid prompt cache boundary");
    if (boundary.tokens < 1024) continue;
    if (!boundary.ttl_seconds) boundary.ttl_seconds = options.ttl_seconds;
    if (boundary.kind == Kind::kExplicit)
      explicit_points.push_back(boundary);
    else if (boundary.kind == Kind::kEligibleMessage)
      eligible.push_back(boundary.tokens);
    else
      initial.push_back(boundary.tokens);
  }
  std::stable_sort(
      explicit_points.begin(), explicit_points.end(),
      [](const auto& a, const auto& b) { return a.tokens < b.tokens; });
  std::sort(eligible.begin(), eligible.end());
  std::sort(initial.begin(), initial.end());
  const size_t lookback = 50;
  for (size_t i = 0; i < explicit_points.size(); ++i)
    if (i < 2 || explicit_points.size() - i <= lookback)
      plan->reads.push_back(explicit_points[i].tokens);
  int implicit = 0;
  if (!options.explicit_only) {
    if (!initial.empty()) implicit = initial.back();
    if (!eligible.empty()) implicit = std::max(implicit, eligible.back());
    for (size_t i = 0; i < eligible.size(); ++i)
      if (eligible.size() - i <= 21) plan->reads.push_back(eligible[i]);
    if (!initial.empty()) plan->reads.push_back(initial.back());
  }
  const size_t explicit_slots = options.explicit_only ? 4 : 3;
  std::map<int, int> writes;
  for (size_t i = 0; i < explicit_points.size(); ++i)
    if (explicit_points.size() - i <= explicit_slots) {
      const auto& point = explicit_points[i];
      writes[point.tokens] = std::max(writes[point.tokens], point.ttl_seconds);
    }
  if (implicit)
    writes[implicit] = std::max(writes[implicit], options.ttl_seconds);
  int covering_ttl = 0;
  for (auto it = writes.rbegin(); it != writes.rend(); ++it) {
    covering_ttl = std::max(covering_ttl, it->second);
    it->second = covering_ttl;
  }
  for (const auto& [tokens, ttl] : writes)
    plan->writes.push_back({tokens, ttl});
  std::sort(plan->reads.begin(), plan->reads.end());
  plan->reads.erase(std::unique(plan->reads.begin(), plan->reads.end()),
                    plan->reads.end());
  return {};
}

Status CompletePromptPrefix(std::span<const size_t> token_starts,
                            size_t prompt_bytes, size_t boundary_bytes,
                            int* tokens) {
  *tokens = 0;
  if (boundary_bytes > prompt_bytes || token_starts.size() > INT_MAX ||
      !std::is_sorted(token_starts.begin(), token_starts.end()) ||
      (!token_starts.empty() && token_starts.back() >= prompt_bytes))
    return Status::Fail("Invalid token source offsets for cache boundary");
  *tokens = PrefixCount(token_starts, prompt_bytes, boundary_bytes);
  return {};
}

Status PlanPromptCacheLayout(
    const PromptCacheOptions& options, const io::Json& messages,
    const ChatPromptLayout& layout,
    std::span<const PromptCacheInputBoundary> explicit_boundaries,
    std::span<const size_t> token_starts, size_t prompt_bytes,
    PromptCachePlan* plan) {
  *plan = {};
  if (!messages.IsArray() || messages.array.size() != layout.messages.size() ||
      messages.array.size() != layout.part_ends.size() ||
      token_starts.size() > INT_MAX ||
      !std::is_sorted(token_starts.begin(), token_starts.end()) ||
      (!token_starts.empty() && token_starts.back() >= prompt_bytes))
    return Status::Fail("Invalid prompt cache layout");
  std::vector<PromptCacheBoundary> points;
  using Kind = PromptCacheBoundary::Kind;
  const auto append = [&](size_t byte, int ttl, Kind kind) -> Status {
    if (byte > prompt_bytes)
      return Status::Fail("Cache boundary outside actual prompt");
    // Validate offsets once, then use logarithmic lookup per boundary.
    const int tokens = PrefixCount(token_starts, prompt_bytes, byte);
    // The renderer adds the assistant generation prefix after message ends.
    if (tokens >= static_cast<int>(token_starts.size()))
      return Status::Fail("Cache boundary requires an uncached prompt token");
    points.push_back({tokens, ttl, kind});
    return {};
  };
  for (const auto& point : explicit_boundaries) {
    if (point.message_index >= layout.messages.size() ||
        point.part_index >= layout.part_ends[point.message_index].size())
      return Status::Fail("Cache content boundary outside actual layout");
    const auto& span = layout.messages[point.message_index];
    const size_t source =
        layout.part_ends[point.message_index][point.part_index];
    if (span.prompt_begin > prompt_bytes ||
        span.source_size > prompt_bytes - span.prompt_begin)
      return Status::Fail("Cache content span outside actual prompt");
    const size_t used =
        source <= span.source_begin
            ? 0
            : std::min(source - span.source_begin, span.source_size);
    auto status =
        append(span.prompt_begin + used, point.ttl_seconds, Kind::kExplicit);
    if (!status) return status;
  }
  size_t initial_end = 0;
  for (size_t i = 0; i < messages.array.size() &&
                     IsChatInstructionRole(messages.array[i].GetString("role"));
       ++i)
    initial_end = std::max(initial_end, layout.messages[i].prompt_end);
  if (initial_end) {
    auto status = append(initial_end, 0, Kind::kInitialInstructions);
    if (!status) return status;
  }
  for (size_t i = 0; i < messages.array.size(); ++i) {
    const auto role = messages.array[i].GetString("role");
    if (role != "user" && role != "tool") continue;
    size_t end = layout.messages[i].prompt_end;
    if (role == "tool") {
      // Tool replies may be rendered in call order instead of input order.
      while (i + 1 < messages.array.size() &&
             messages.array[i + 1].GetString("role") == "tool") {
        ++i;
        end = std::max(end, layout.messages[i].prompt_end);
      }
    }
    auto status = append(end, 0, Kind::kEligibleMessage);
    if (!status) return status;
  }
  return PlanPromptCache(options, points, static_cast<int>(token_starts.size()),
                         plan);
}
}  // namespace q4t::server

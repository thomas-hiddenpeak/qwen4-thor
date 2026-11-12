#include "q4t/server/vector_search_api.h"

#include "object_protocol.h"
#include "q4t/model/embedding_config.h"
#include "q4t/server/application_query.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
using namespace object_detail;
namespace {
ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply RateError(ClientRateResult result) {
  if (result == ClientRateResult::kAllowed) return {};
  const bool quota =
      result == ClientRateResult::kRpm || result == ClientRateResult::kTpm;
  return Error(quota ? 429 : 503,
               result == ClientRateResult::kRpm      ? "client_rpm_exhausted"
               : result == ClientRateResult::kTpm    ? "client_tpm_exhausted"
               : result == ClientRateResult::kFrozen ? "service_draining"
                                                     : "client_rate_invalid",
               "Search request quota unavailable");
}
}  // namespace

ObjectReply HandleVectorSearchRequest(
    ObjectStore& objects, ClientAdmission& clients, bool configured,
    std::string_view method, std::string_view target, const std::string& body,
    const ClientIdentity& client, const BinaryAccess& access,
    ClientRateLease& rate, ActualUsageCounter& usage,
    const VectorNativeSearchCallbacks& callbacks) {
  const auto route = ParseVectorStoreRoute(target);
  if (route.kind != VectorStoreRouteKind::kSearch)
    return Error(404, "route_not_found", "Search route not found");
  if (method != "POST") {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value = Object({{"allow", String("POST")}});
    return reply;
  }
  if (!clients.OwnsIdentity(client))
    return Error(503, "object_scope_unavailable",
                 "Authenticated search owner unavailable");
  const auto scope = client.Scoped() ? "client:" + std::string(client.Id())
                                     : "local:anonymous";
  if (scope != access.scope)
    return Error(403, "object_scope_mismatch", "Search client changed");
  const auto split = target.find('?');
  auto query = ParseApplicationQuery(
      split == target.npos ? "" : target.substr(split + 1));
  if (!query.ok()) return query;
  if (!query.value.object.empty())
    return Invalid("Search does not accept query parameters", "query");
  Json request;
  auto status = io::ParseJson(body, &request, true, kRequestJsonLimits);
  if (!status) return Invalid(status.message(), "body");
  VectorSearchPlan plan;
  ChatPlanError error;
  status = PlanVectorSearch(request, &plan, &error);
  if (!status) {
    auto reply = Error(error.http_status, error.code, status.message());
    reply.value = Object({{"param", String(error.param)}});
    return reply;
  }
  if (!configured || !callbacks.embed || !callbacks.encode ||
      (plan.rewrite_query && !callbacks.rewrite))
    return Error(503, "vector_search_backend_unavailable",
                 "Native search execution is not configured");
  VectorRewritePlan rewrite;
  if (plan.rewrite_query) {
    auto reply = PlanVectorRewrite(plan.queries, &rewrite);
    if (!reply.ok()) return reply;
  }
  usage.Enable();
  ModelPreparation preparation(clients, client, rate, 0, 0,
                               rewrite.output_tokens);
  VectorSearchUsage actual;
  ScopeExit settle([&] {
    preparation.CloseStep();
    if (actual.unknown) {
      usage.Merge(std::nullopt);
      rate.Start();
    } else {
      usage.Merge(ActualUsage{actual.committed_input});
      rate.StartPrompt(preparation.InputTokens() + actual.committed_input);
    }
    rate.Completion(preparation.OutputTokens());
    rate.Settle();
  });
  VectorSearchCallbacks execution;
  execution.encode = callbacks.encode;
  execution.embed = callbacks.embed;
  execution.rewrite = [&](const auto& original, auto* rewritten) {
    VectorRewriteResult result;
    auto reply = callbacks.rewrite(rewrite, preparation, &result);
    preparation.CloseStep();
    if (!reply.ok()) return reply;
    if (!preparation.Reserved() || !preparation.InputTokens() ||
        !preparation.OutputTokens())
      return Error(502, "vector_query_rewrite_invalid",
                   "Native query rewrite has no completed model work");
    return ParseVectorRewrite(original, result.content, result.finish_reason,
                              rewritten);
  };
  execution.reserve = [&](uint64_t tokens) {
    if (const auto* limits = client.Limits();
        limits &&
        (preparation.InputTokens() > limits->max_prompt_tokens ||
         tokens > limits->max_prompt_tokens - preparation.InputTokens()))
      return Error(400, "client_prompt_limit_exceeded",
                   "Search exceeds cumulative client input limit");
    return RateError(preparation.Reserved()
                         ? tokens ? rate.ExtendPrompt(clients, client, tokens)
                                  : ClientRateResult::kAllowed
                         : rate.Reserve(clients, client, tokens, 0));
  };
  return ExecuteVectorSearchRequest(objects, access, std::string(route.store),
                                    request, model::EmbeddingConfig::kHidden,
                                    execution, &actual);
}
}  // namespace q4t::server

#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <set>

#include "object_protocol.h"
#include "q4t/server/mcp_policy.h"
#include "q4t/server/input_snapshot_bindings.h"
#include "q4t/server/responses_owner.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply PlanError(const Status& status, const ChatPlanError& error) {
  auto result = Error(error.http_status, error.code, status.message());
  result.value = Object({{"param", String(error.param)}});
  return result;
}
bool SamePolicy(const Json& previous, const Json& current) {
  const auto policy = [](const Json& definition) {
    auto result = Object();
    for (const auto* field :
         {"allowed_tools", "allowed_callers", "require_approval"})
      if (const auto* value = definition.Find(field); value && !value->IsNull())
        Set(&result, field, *value);
    return result;
  };
  std::string left, right;
  return InputValueIdentity(policy(previous), &left) &&
         InputValueIdentity(policy(current), &right) && left == right;
}
}  // namespace

ObjectReply ResponsesExecution::CaptureMcpCredentials() {
  ChatPlanError error;
  auto status = CaptureResponsesMcpCredentials(&envelope_.request,
                                               &mcp_credentials_, &error);
  if (!status) return PlanError(status, error);
  if (const auto* tools = envelope_.request.Find("tools"))
    Set(&envelope_.options, "tools", *tools);
  return {};
}

ObjectReply ResponsesExecution::PrepareMcp(const ChatPlanConfig& config,
                                           Json* continuation) {
  // The executor retains the exact immutable policy used for preparation;
  // later dispatch/approval/resource callbacks do not receive ChatPlanConfig.
  cipher_config_.mcp_policy = config.mcp_policy;
  cipher_config_.mcp_tunnels = config.mcp_tunnels;
  auto& input = continuation ? *continuation : generation_history_;
  const auto* declared = envelope_.request.GetArray("tools");
  auto definitions = declared ? *declared : Array();
  // Include declarations available at their actual discovery activation. All
  // persisted definitions are already credential-free; only this request's
  // explicitly supplied credentials can be rebound below.
  ResponsesToolHistory discovery;
  ChatPlanError error;
  if (HasResponsesToolDiscovery(definitions, input)) {
    auto status =
        ResolveResponsesToolHistory(definitions, input, &discovery, &error);
    if (!status) return PlanError(status, error);
    definitions = std::move(discovery.active);
  }
  mcp_definitions_.clear();
  for (size_t i = 0; i < definitions.array.size(); ++i) {
    const auto& tool = definitions.array[i];
    if (tool.GetString("type") != "mcp") continue;
    ResponsesMcpDefinition definition;
    auto status = PlanResponsesMcpDefinition(
        tool, &definition, &error, "tools[" + std::to_string(i) + ']');
    if (!status) return PlanError(status, error);
    BindResponsesMcpCredentials(&definition, mcp_credentials_);
    // Missing/revoked deployment routes do not prevent offline consumption of
    // authenticated frozen history. Any new I/O must resolve again below.
    (void)ResolveMcpDeploymentTarget(config.mcp_policy.get(), access_.scope,
                                     &definition);
    mcp_definitions_.push_back(std::move(definition));
  }
  const bool history =
      std::any_of(input.array.begin(), input.array.end(), [](const auto& item) {
        return item.GetString("type").starts_with("mcp_");
      });
  const bool program_mcp =
      std::any_of(programs_.begin(), programs_.end(), [](const auto& program) {
        return program.terminal.IsNull() &&
               std::any_of(program.tools.array.begin(),
                           program.tools.array.end(), [](const auto& tool) {
                             return tool.GetString("type") == "mcp";
                           });
      });
  if (mcp_definitions_.empty() && !history && !program_mcp &&
      plan_.mcp_catalogs.IsNull())
    return {};
  // Validate ordinary roles, tool causality, discovery and file shapes before
  // fetching remote metadata. This projection grants no MCP callable and is
  // never sent to the model; real schema/choice planning follows the listing.
  auto preflight = envelope_.request;
  Set(&preflight, "input", input);
  ResponsesInputPlan checked_input;
  auto checked = NormalizeResponsesInput(preflight, &checked_input, &error,
                                         config.count_input_only, true, true,
                                         nullptr, false, nullptr,
                                         envelope_.multi_agent_beta);
  if (!checked) return PlanError(checked, error);
  ResponsesToolCatalog checked_tools;
  checked = PlanResponsesToolCatalog(definitions, &checked_tools, &error);
  if (!checked) return PlanError(checked, error);
  if (plan_.execution_owner.IsNull())
    plan_.execution_owner = CurrentResponseOwner();
  mcp_state_ = std::make_unique<ResponsesMcpState>(objects_, config, plan_.id);
  mcp_fetch_ = config.mcp_policy    ? config.mcp_policy->fetch
               : config.file_inputs ? config.file_inputs->fetch
                                    : FileFetchOptions{};
  if (plan_.mcp_catalogs.IsNull()) plan_.mcp_catalogs = Array();
  if (!plan_.mcp_catalogs.IsArray() || plan_.mcp_catalogs.array.size() > 128 ||
      OutputJson(plan_.mcp_catalogs).size() > (4U << 20))
    return Error(503, "mcp_preparation_invalid", "Invalid owned MCP catalogs");
  for (auto& item : input.array) {
    if (!item.GetString("type").starts_with("mcp_")) continue;
    auto authenticated = mcp_state_->Authenticate(item, access_);
    if (!authenticated.ok()) return authenticated;
    item = std::move(authenticated.value);
  }
  if (!continuation) mcp_resume_ = Array();
  std::set<std::string> approvals;
  const auto approvals_input = continuation ? Array() : plan_.own_input;
  for (const auto& item : approvals_input.array) {
    if (item.GetString("type") != "mcp_approval_response") continue;
    if (!approvals.insert(item.GetString("approval_request_id")).second)
      return Error(400, "invalid_request", "Duplicate MCP approval response");
    if (mcp_resume_.array.size() >= kMaxNativeToolCalls)
      return Error(503, "mcp_execution_capacity",
                   "Approval work exceeds budget");
    mcp_resume_.array.push_back(item);
  }
  // A queued preparation reuses its exact owned listing. Never perform an
  // unrecorded network refresh because a worker or host restarted.
  for (auto& item : plan_.mcp_catalogs.array) {
    if (item.GetString("type") != "mcp_list_tools")
      return Error(503, "mcp_preparation_invalid", "Expected owned catalog");
    auto authenticated = mcp_state_->Authenticate(item, access_);
    if (!authenticated.ok()) return authenticated;
    item = std::move(authenticated.value);
    if (std::none_of(input.array.begin(), input.array.end(),
                     [&](const auto& old) {
                       return old.GetString("id") == item.GetString("id");
                     }))
      input.array.push_back(item);
  }
  std::set<std::string> labels;
  for (auto& definition : mcp_definitions_) {
    const auto label = definition.declaration.GetString("server_label");
    if (!labels.insert(label).second)
      return Error(400, "invalid_request", "Duplicate MCP server label");
    bool available = false;
    for (auto i = input.array.rbegin(); i != input.array.rend(); ++i) {
      if (i->GetString("type") != "mcp_list_tools" ||
          i->GetString("server_label") != label)
        continue;
      auto opened = mcp_state_->OpenCatalog(*i, definition, access_);
      if (opened.code == "mcp_target_changed" && require_frozen_inputs_ &&
          !continuation)
        return opened;
      if (opened.code == "mcp_server_changed" ||
          opened.code == "mcp_target_changed")
        continue;
      if (!opened.ok()) return opened;
      const bool policy_changed = !SamePolicy(*opened.value.Find("declaration"),
                                              definition.declaration);
      const bool owned =
          std::any_of(plan_.mcp_catalogs.array.begin(),
                      plan_.mcp_catalogs.array.end(), [&](const auto& catalog) {
                        return catalog.GetString("id") == i->GetString("id");
                      });
      const bool refresh = policy_changed || (!owned && i->Find("error") &&
                                              !i->Find("error")->IsNull());
      if (policy_changed && require_frozen_inputs_ && !continuation)
        return Error(409, "mcp_preparation_policy_changed",
                     "Queued catalog must retain its original policy");
      if (opened.value.GetBool("credentials_required") &&
          !ResponsesMcpHasCredentials(definition) &&
          (require_frozen_inputs_ || refresh) && !config.count_input_only)
        return Error(400, "mcp_credentials_required",
                     "Resupply request credentials for this MCP server");
      // A new request/explicit discovery may refresh metadata once. Old
      // proposals keep their own authority; a failed listing cannot poison
      // all later requests, and new policy must match new model bindings.
      if (refresh && (!require_frozen_inputs_ || continuation)) break;
      available = true;
      break;
    }
    if (available) continue;
    if (require_frozen_inputs_ && !continuation)
      return Error(409, "mcp_preparation_missing",
                   "Queued MCP work has no original catalog");
    auto resolved = ResolveMcpDeploymentTarget(config.mcp_policy.get(),
                                               access_.scope, &definition);
    if (!resolved.ok()) return resolved;
    McpTransportReceipt receipt;
    McpSession session;
    auto listed =
        InvokeMcp({access_.scope, access_.deadline, access_.cancelled},
                  definition.credentials, mcp_fetch_, "tools/list", Object(),
                  {}, &receipt, {}, &session, config.mcp_tunnels);
    if (listed.commit_unknown || listed.http_status == 408 ||
        listed.http_status == 499)
      return listed;
    const auto* tools = receipt.result.GetArray("tools");
    const bool success = listed.ok() && receipt.error.IsNull() && tools;
    const auto failure = success        ? ""
                         : !listed.ok() ? listed.code + ": " + listed.message
                                        : "MCP tools/list failed";
    auto catalog = mcp_state_->Catalog(
        definition, success ? *tools : Array(), access_, 30 * 24 * 3600, "",
        failure, session.state.Has("protocol") ? &session.state : nullptr);
    if (!catalog.ok() || catalog.commit_unknown)
      return BindMutationResult(std::move(catalog));
    if (plan_.mcp_catalogs.array.size() >= 128)
      return Error(503, "mcp_catalog_capacity", "Catalog count exhausted");
    plan_.mcp_catalogs.array.push_back(catalog.value);
    if (OutputJson(plan_.mcp_catalogs).size() > (4U << 20))
      return Error(503, "mcp_catalog_capacity",
                   "Catalog byte budget exhausted");
    input.array.push_back(std::move(catalog.value));
  }
  if (input.array.size() > 4096)
    return Error(400, "input_limit_exceeded", "MCP history exceeds item limit");
  return {};
}

Status ResponsesExecution::BeginMcpCatalogs(ResponsesOutput* output,
                                            std::string* wire, size_t begin) {
  wire->clear();
  for (size_t i = begin; i < plan_.mcp_catalogs.array.size(); ++i) {
    const auto& catalog = plan_.mcp_catalogs.array[i];
    auto starting = catalog;
    Set(&starting, "tools", Array());
    std::erase_if(starting.object,
                  [](const auto& field) { return field.first == "error"; });
    size_t index = 0;
    std::string part;
    auto status = output->StartMcpItem(starting, &index, &part);
    if (!status) return status;
    *wire += part;
    status = output->CompleteMcpItem(index, catalog, &part);
    if (!status) return status;
    *wire += part;
  }
  return {};
}
}  // namespace q4t::server

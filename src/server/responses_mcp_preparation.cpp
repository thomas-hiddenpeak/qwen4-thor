#include "q4t/server/responses_execution.h"

#include <algorithm>
#include <set>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply PlanError(const Status& status, const ChatPlanError& error) {
  auto result = Error(error.http_status, error.code, status.message());
  result.value = Object({{"param", String(error.param)}});
  return result;
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

ObjectReply ResponsesExecution::PrepareMcp(const ChatPlanConfig& config) {
  const auto* declared = envelope_.request.GetArray("tools");
  auto definitions = declared ? *declared : Array();
  // Include declarations available at their actual discovery activation. All
  // persisted definitions are already credential-free; only this request's
  // explicitly supplied credentials can be rebound below.
  ResponsesToolHistory discovery;
  ChatPlanError error;
  if (HasResponsesToolDiscovery(definitions, generation_history_)) {
    auto status = ResolveResponsesToolHistory(definitions, generation_history_,
                                              &discovery, &error);
    if (!status) return PlanError(status, error);
    for (const auto& active : discovery.active.array) {
      if (active.GetString("type") != "mcp") continue;
      std::erase_if(definitions.array, [&](const auto& old) {
        return old.GetString("type") == "mcp" &&
               old.GetString("server_label") ==
                   active.GetString("server_label");
      });
      definitions.array.push_back(active);
    }
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
    mcp_definitions_.push_back(std::move(definition));
  }
  const bool history =
      std::any_of(generation_history_.array.begin(),
                  generation_history_.array.end(), [](const auto& item) {
                    return item.GetString("type").starts_with("mcp_");
                  });
  if (mcp_definitions_.empty() && !history && plan_.mcp_catalogs.IsNull())
    return {};
  // Validate ordinary roles, tool causality, discovery and file shapes before
  // fetching remote metadata. This projection grants no MCP callable and is
  // never sent to the model; real schema/choice planning follows the listing.
  auto preflight = envelope_.request;
  Set(&preflight, "input", generation_history_);
  ResponsesInputPlan checked_input;
  auto checked = NormalizeResponsesInput(preflight, &checked_input, &error,
                                         config.count_input_only, true, true);
  if (!checked) return PlanError(checked, error);
  ResponsesToolCatalog checked_tools;
  checked = PlanResponsesToolCatalog(definitions, &checked_tools, &error);
  if (!checked) return PlanError(checked, error);
  mcp_state_ = std::make_unique<ResponsesMcpState>(objects_, config);
  mcp_fetch_ =
      config.file_inputs ? config.file_inputs->fetch : FileFetchOptions{};
  if (plan_.mcp_catalogs.IsNull()) plan_.mcp_catalogs = Array();
  if (!plan_.mcp_catalogs.IsArray() || plan_.mcp_catalogs.array.size() > 128 ||
      OutputJson(plan_.mcp_catalogs).size() > (4U << 20))
    return Error(503, "mcp_preparation_invalid", "Invalid owned MCP catalogs");
  for (auto& item : generation_history_.array) {
    if (!item.GetString("type").starts_with("mcp_")) continue;
    auto authenticated = mcp_state_->Authenticate(item, access_);
    if (!authenticated.ok()) return authenticated;
    item = std::move(authenticated.value);
  }
  mcp_resume_ = Array();
  std::set<std::string> approvals;
  for (const auto& item : plan_.own_input.array) {
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
    if (std::none_of(generation_history_.array.begin(),
                     generation_history_.array.end(), [&](const auto& old) {
                       return old.GetString("id") == item.GetString("id");
                     }))
      generation_history_.array.push_back(item);
  }
  std::set<std::string> labels;
  for (const auto& definition : mcp_definitions_) {
    const auto label = definition.declaration.GetString("server_label");
    if (!labels.insert(label).second)
      return Error(400, "invalid_request", "Duplicate MCP server label");
    bool available = false;
    for (auto i = generation_history_.array.rbegin();
         i != generation_history_.array.rend(); ++i) {
      if (i->GetString("type") != "mcp_list_tools" ||
          i->GetString("server_label") != label)
        continue;
      auto opened = mcp_state_->OpenCatalog(*i, definition, access_);
      if (opened.code == "mcp_server_changed") continue;
      if (!opened.ok()) return opened;
      if (opened.value.GetBool("credentials_required") &&
          !ResponsesMcpHasCredentials(definition) && require_frozen_inputs_ &&
          !config.count_input_only)
        return Error(400, "mcp_credentials_required",
                     "Resupply request credentials for this MCP server");
      available = true;
      break;
    }
    if (available) continue;
    if (require_frozen_inputs_)
      return Error(409, "mcp_preparation_missing",
                   "Queued MCP work has no original catalog");
    if (definition.target_kind != "server_url")
      return Error(503, "mcp_target_mapping_required",
                   "MCP connector or tunnel needs a deployment mapping");
    McpTransportReceipt receipt;
    auto listed =
        InvokeMcp({access_.scope, access_.deadline, access_.cancelled},
                  definition.credentials, mcp_fetch_, "tools/list", Object(),
                  {}, &receipt);
    if (listed.commit_unknown || listed.http_status == 408 ||
        listed.http_status == 499)
      return listed;
    const auto* tools = receipt.result.GetArray("tools");
    const bool success = listed.ok() && receipt.error.IsNull() && tools;
    const auto failure = success        ? ""
                         : !listed.ok() ? listed.code + ": " + listed.message
                                        : "MCP tools/list failed";
    auto catalog = mcp_state_->Catalog(definition, success ? *tools : Array(),
                                       access_, 30 * 24 * 3600, "", failure);
    if (!catalog.ok() || catalog.commit_unknown)
      return BindMutationResult(std::move(catalog));
    if (plan_.mcp_catalogs.array.size() >= 128)
      return Error(503, "mcp_catalog_capacity", "Catalog count exhausted");
    plan_.mcp_catalogs.array.push_back(catalog.value);
    if (OutputJson(plan_.mcp_catalogs).size() > (4U << 20))
      return Error(503, "mcp_catalog_capacity",
                   "Catalog byte budget exhausted");
    generation_history_.array.push_back(std::move(catalog.value));
  }
  if (generation_history_.array.size() > 4096)
    return Error(400, "input_limit_exceeded", "MCP history exceeds item limit");
  return {};
}

Status ResponsesExecution::BeginMcpCatalogs(ResponsesOutput* output,
                                            std::string* wire) {
  wire->clear();
  for (const auto& catalog : plan_.mcp_catalogs.array) {
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

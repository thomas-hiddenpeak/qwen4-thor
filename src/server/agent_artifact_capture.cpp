#include "agent_artifact_internal.h"

#include <algorithm>
#include <ctime>

#include "agent_environment_internal.h"
#include "agent_session_internal.h"
#include "object_protocol.h"
#include "q4t/io/base64.h"

namespace q4t::server::agent_detail {
namespace {
using namespace object_detail;
using io::Json;
constexpr uint64_t kArtifactBytes = 200ULL << 20;
constexpr uint64_t kTurnBytes = 500ULL << 20;
constexpr std::string_view kOutputs = "/workspace/outputs/";
ObjectReply Invalid() {
  return Error(503, "agent_artifact_state_invalid",
               "Stored workspace and turn output disagree");
}
BinaryAccess Access(const StateAccess& access) {
  return {access.scope, access.deadline, access.cancelled};
}
}  // namespace

TurnArtifacts::~TurnArtifacts() {
  const BinaryAccess cleanup{
      access_.scope,
      std::chrono::steady_clock::now() + std::chrono::milliseconds(250),
      {}};
  for (auto& stage : stages_)
    if (!stage.id.empty() && !stage.closed && !stage.commit_unknown) {
      try {
        binary_.Abort(cleanup, &stage);
      } catch (...) {
        // Any remaining stage expires without exposing a partial artifact.
      }
    }
}
void TurnArtifacts::Settle(const ObjectReply& outcome) {
  if (outcome.ok() || outcome.commit_unknown) stages_.clear();
}

ObjectReply TurnArtifacts::Stage(const Json& file, const Json& installed,
                                 const Json& workspace, const Json& resource,
                                 std::vector<ObjectRequest>* writes) {
  const auto bytes = static_cast<uint64_t>(resource.GetInt("size_bytes"));
  auto& stage = stages_.emplace_back();
  auto access = Access(access_);
  auto result = binary_.Begin(access, bytes, 600, &stage);
  if (!result.ok()) return result;
  if (file.GetString("type") == "file_id") {
    if (installed.GetString("file_id") != file.GetString("file_id"))
      return Invalid();
    result = environment_detail::ReadWorkspaceCopy(
        objects_, access_, resource.GetString("environment_id"), workspace,
        installed, [&](std::string_view chunk) {
          return binary_.Append(access, &stage, chunk);
        });
    if (!result.ok()) return result;
  } else {
    const auto* data = file.Find("data");
    size_t decoded = 0;
    if (file.GetString("type") != "inline" || !data || !data->IsString() ||
        !io::Base64DecodedSize(data->str, &decoded) || decoded != bytes)
      return Invalid();
    // Interior groups end on complete base64 quartets and fit one binary chunk.
    constexpr size_t encoded_chunk = kBinaryChunkBytes / 3 * 4;
    std::string_view encoded(data->str);
    while (!encoded.empty()) {
      FileInputSource chunk;
      chunk.value = encoded.substr(0, encoded_chunk);
      chunk.param = "outputs";
      FileInputContent content;
      FileInputBudget budget;
      result = ReadLocalFileInput(&objects_, access, chunk, &budget, &content);
      if (!result.ok()) return result;
      result = binary_.Append(access, &stage, content.bytes);
      if (!result.ok()) return result;
      encoded.remove_prefix(chunk.value.size());
    }
  }
  ObjectRequest root;
  root.kind = ObjectKind::kToolRun;
  root.operation = ObjectOperation::kPut;
  root.scope = access.scope;
  root.id = resource.GetString("id");
  root.ttl_seconds = 0;
  root.mutate_before = stage.expires;
  root.data = Object(
      {{"format", String("q4t.agent_artifact.v1")}, {"resource", resource}});
  return binary_.PreparePublish(access, stage, std::move(root), writes);
}

ObjectReply TurnArtifacts::Prepare(const std::string& session,
                                   const std::string& response,
                                   std::vector<ObjectRequest>* writes) {
  if (!writes || writes->empty() || writes->front().id != response)
    return Invalid();
  const auto& record = writes->front().data;
  const auto* saved = record.Find("response");
  if (!saved) return Invalid();
  if (session.empty() || saved->GetString("status") != "completed") return {};
  auto owner = ReadRow(objects_, session, access_);
  if (!owner.ok()) return owner;
  const auto& source = Field(owner.value, "data");
  const auto* links = source.GetArray("turns");
  if (!links) return Invalid();
  const auto link = std::ranges::find_if(links->array, [&](const Json& step) {
    return step.GetString("response_id") == response;
  });
  if (link == links->array.end() || !link->Has("id")) return Invalid();
  auto settings = SessionSettings(source, &*link);
  if (!settings.ok()) return settings;
  const auto* environment = settings.value.Find("environment");
  if (!environment || environment->GetString("type") != "openai_hosted")
    return {};
  const auto* files = environment->GetArray("files");
  if (!files) return Invalid();
  const auto is_output = [](const Json& file) {
    return file.GetString("path").starts_with(kOutputs);
  };
  if (std::ranges::none_of(files->array, is_output)) return {};
  AgentSessionView view;
  auto projected = ProjectStep(settings.value, *link, record, &view);
  if (!projected.ok() || view.turn.GetString("status") != "completed")
    return projected;

  ObjectRequest workspace;
  workspace.scope = access_.scope;
  workspace.id = environment->GetString("id");
  workspace.kind = ObjectKind::kEnvironment;
  auto row = objects_.Execute(workspace, access_.deadline, access_.cancelled);
  if (!row.ok()) return row;
  const auto& stored = Field(row.value, "data");
  const auto* sealed = stored.Find("sealed_definition");
  if (!sealed || stored.GetString("bound_session") != session) return Invalid();
  Json definition;
  auto opened = environment_detail::OpenDefinition(
      objects_, access_, workspace.id, *sealed, &definition);
  if (!opened.ok()) return opened;
  const auto* contents = definition.GetArray("files");
  if (!contents) return Invalid();
  const auto completed = record.GetInt("agent_turn_finished_at");
  if (completed <= 0) return Invalid();
  auto ids = Array();
  uint64_t total = 0;
  for (const auto& file : files->array) {
    if (!is_output(file)) continue;
    const auto path = file.GetString("path");
    const auto size = file.GetInt("size_bytes", -1);
    if (size < 0 || !environment_detail::ValidWorkspaceFile(path))
      return Invalid();
    const auto bytes = static_cast<uint64_t>(size);
    if (bytes > kArtifactBytes || bytes > kTurnBytes - total)
      return Error(413, "agent_artifact_capacity",
                   "Turn output exceeds artifact capacity");
    total += bytes;
    const auto content = std::ranges::find_if(
        contents->array,
        [&](const Json& item) { return item.GetString("path") == path; });
    if (content == contents->array.end()) return Invalid();
    const auto id =
        "artifact_" + Digest(OutputJson(Array(
                          {String(session), String(response), String(path)})));
    auto resource = Object({{"id", String(id)},
                            {"object", String("agent.session.artifact")},
                            {"created_at", Number(completed)},
                            {"environment_id", String(workspace.id)},
                            {"session_id", String(session)},
                            {"turn_id", *link->Find("id")},
                            {"path", String(path)},
                            {"size_bytes", Number(size)}});
    auto staged = Stage(*content, file, stored, resource, writes);
    if (!staged.ok()) return staged;
    ids.array.push_back(String(id));
  }
  Set(&writes->front().data, "agent_artifact_ids", std::move(ids));
  return {};
}
}  // namespace q4t::server::agent_detail

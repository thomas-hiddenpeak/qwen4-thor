#include "q4t/server/object_retention.h"

#include <algorithm>
#include <deque>
#include <unordered_map>
#include <unordered_set>

#include "q4t/server/application_routes.h"
#include "q4t/server/application_payload.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/batch_state.h"
#include "q4t/server/input_snapshot.h"
#include "q4t/server/batch_checkpoint.h"
#include "q4t/trace/sha256.h"

namespace q4t::server {
namespace {
using io::Json;
// A Batch checkpoint may retain both output lanes (16K chunks each), its
// original source and 391 pages. The planner's total graph budget remains 64M.
constexpr size_t kReferenceBytes = 4 * 1024 * 1024;
constexpr size_t kPlanBytes = 64 * 1024 * 1024;

class Inspector {
 public:
  ObjectRetentionInfo value;

  void Reference(const Json* field) {
    if (!field || field->IsNull()) return;
    if (!field->IsString() || !ValidApplicationId(field->str)) {
      value.uncertain = true;
      return;
    }
    if (reference_bytes_ >
        kReferenceBytes - field->str.size() - sizeof(std::string)) {
      value.uncertain = true;
      return;
    }
    reference_bytes_ += field->str.size() + sizeof(std::string);
    value.references.push_back(field->str);
  }

  void Items(const Json* items, size_t depth = 0) {
    if (!items || items->IsNull() || items->IsString()) return;
    if (depth > 64 || ++work_ > 2 * 1024 * 1024) {
      value.uncertain = true;
      return;
    }
    if (items->IsArray()) {
      for (const auto& item : items->array) Items(&item, depth + 1);
      return;
    }
    if (!items->IsObject()) {
      value.uncertain = true;
      return;
    }
    Reference(items->Find("id"));
    Reference(items->Find("file_id"));
    if (items->GetString("type").starts_with("mcp_"))
      Reference(items->Find("approval_request_id"));
    // Traverse typed item/content containers, not metadata, arbitrary tool
    // arguments, text, or user-authored JSON strings that look like IDs.
    for (const auto* key : {"content", "content_parts", "output", "results"})
      Items(items->Find(key), depth + 1);
    if (const auto* file = items->Find("file"); file && file->IsObject())
      Reference(file->Find("file_id"));
  }

  void AgentTools(const Json* tools) {
    if (!tools || !tools->IsArray()) {
      value.uncertain = true;
      return;
    }
    for (const auto& tool : tools->array) {
      const auto type = tool.GetString("type");
      if (type == "mcp") {
        Reference(tool.Find("credential_id"));
      } else if (type != "function" && type != "web_search" &&
                 type != "tool_search" && type != "programmatic_tool_calling" &&
                 type != "computer_use" && type != "automations") {
        value.uncertain = true;
      }
    }
  }

  void RequiredId(const Json& record, const char* field,
                  std::string_view prefix, bool pinned = true) {
    const auto* id = record.Find(field);
    if (!id || !id->IsString() || !id->str.starts_with(prefix) ||
        id->str.size() == prefix.size() || !ValidApplicationId(id->str)) {
      value.uncertain = true;
      return;
    }
    if (pinned) Reference(id);
  }

  bool StoredString(const Json* slot, const std::vector<std::string>& path,
                    const std::vector<ApplicationStringRef>& payloads) {
    if (!slot) return false;
    if (slot->IsString()) return !slot->str.empty();
    return slot->IsNull() && std::ranges::any_of(payloads, [&](const auto& ref) {
      return ref.path == path;
    });
  }

  void HostedEnvironment(const Json& data, bool is_template,
                         const std::vector<ApplicationStringRef>& payloads) {
    const auto* resource = data.Find("resource");
    const auto* files = resource ? resource->GetArray("files") : nullptr;
    const auto* skills = resource ? resource->GetArray("skills") : nullptr;
    const auto* sealed = data.Find("sealed_definition");
    const auto object =
        is_template ? "agent.environment.template" : "agent.environment";
    const auto id = resource ? resource->GetString("id") : "";
    if (!files || !skills || resource->GetString("object") != object ||
        !ValidApplicationId(id) || !id.starts_with("environment_") ||
        is_template != id.starts_with("environment_template_")) {
      value.uncertain = true;
      return;
    }
    const auto status = resource->GetString("status");
    if (!is_template && status == "disconnected") {
      value.uncertain = sealed || data.Has("bound_session");
      return;
    }
    if (sealed && sealed->IsObject()) {
      const auto* parts = sealed->GetArray("parts");
      if (sealed->GetString("format") != "q4t.environment.definition.v1" ||
          !StoredString(sealed->Find("index"), {"sealed_definition", "index"},
                        payloads) ||
          !parts || parts->array.empty()) {
        value.uncertain = true;
        return;
      }
      for (size_t i = 0; i < parts->array.size(); ++i)
        if (!StoredString(&parts->array[i],
                          {"sealed_definition", "parts", std::to_string(i)},
                          payloads))
          value.uncertain = true;
    } else if (!StoredString(sealed, {"sealed_definition"}, payloads)) {
      value.uncertain = true;
    }
    for (const auto& file : files->array) {
      const auto type = file.GetString("type");
      if (type == "file_id") {
        // Templates resolve the source later. Installed files already own a
        // private copy, including after the original Files object is deleted.
        RequiredId(file, "file_id", "file_", is_template);
      } else if (type != "inline") {
        value.uncertain = true;
      }
    }
    for (const auto& skill : skills->array) {
      if (skill.GetString("type") == "skill_reference")
        RequiredId(skill, "skill_id", "");
      else if (skill.GetString("type") != "inline")
        value.uncertain = true;
    }
    if (is_template) return;
    const auto* vaults = data.GetArray("vault_ids");
    if (!vaults || data.GetInt("warm_until") <= 0) {
      value.uncertain = true;
      return;
    }
    if (status == "connected")
      RequiredId(data, "bound_session", "agent_session_");
    else if (status != "ready")
      value.uncertain = true;
    for (const auto& vault : vaults->array) {
      if (!vault.IsString() || !vault.str.starts_with("vault_"))
        value.uncertain = true;
      Reference(&vault);
    }
  }

  void Request(const Json* request) {
    if (!request || request->IsNull()) return;
    if (!request->IsObject()) {
      value.uncertain = true;
      return;
    }
    Reference(request->Find("previous_response_id"));
    if (const auto* conversation = request->Find("conversation"))
      Reference(conversation->IsObject() ? conversation->Find("id")
                                         : conversation);
    if (const auto* prompt = request->Find("prompt");
        prompt && prompt->IsObject())
      Reference(prompt->Find("id"));
    Items(request->Find("input"));
    Items(request->Find("messages"));
  }

  void Manifest(const Json* blob) {
    BinaryManifest manifest;
    if (!blob || !ParseBinaryManifest(*blob, &manifest)) {
      value.uncertain = true;
      return;
    }
    const auto* chunks = blob->GetArray("chunks");
    for (const auto& chunk : chunks->array) Reference(chunk.Find("id"));
  }
  void Blob(const Json& data) { Manifest(data.Find("blob")); }

  void Snapshots(const Json& data) {
    const auto* snapshots = data.Find("input_snapshots");
    if (!snapshots) return;  // Records written before file input support.
    if (!snapshots->IsArray() || snapshots->array.size() > 4096) {
      value.uncertain = true;
      return;
    }
    for (const auto& binding : snapshots->array) {
      const auto* snapshot = binding.Find("snapshot");
      InputSnapshotRef reference;
      if (!snapshot || !ParseInputSnapshotReference(*snapshot, &reference)) {
        value.uncertain = true;
        return;
      }
      Reference(snapshot->Find("id"));
    }
  }

  void Payloads(const std::vector<ApplicationStringRef>& references) {
    for (const auto& ref : references) {
      Json id;
      id.type = Json::Type::kString;
      id.str = ref.snapshot.id;
      Reference(&id);
    }
  }

  ObjectRetentionInfo Finish() {
    auto& references = value.references;
    std::sort(references.begin(), references.end());
    references.erase(std::unique(references.begin(), references.end()),
                     references.end());
    if (value.Bytes() > kReferenceBytes) value.uncertain = true;
    if (value.uncertain) {
      // Incomplete reference extraction conservatively protects this owner.
      std::vector<std::string>().swap(references);
    }
    return std::move(value);
  }

 private:
  size_t reference_bytes_ = 0;
  size_t work_ = 0;
};

std::string Key(std::string_view owner, std::string_view id) {
  return std::string(owner) + ':' + std::string(id);
}
}  // namespace

size_t ObjectRetentionInfo::Bytes() const {
  size_t bytes = references.capacity() * sizeof(std::string);
  if (!owning_automation.empty()) bytes += owning_automation.capacity() + 1;
  for (const auto& reference : references) bytes += reference.capacity() + 1;
  return bytes;
}

ObjectRetentionInfo InspectAutomationTarget(const Json& target) {
  Inspector inspector;
  if (target.GetString("type") == "existing_session") {
    inspector.RequiredId(target, "session_id", "agent_session_");
    return inspector.Finish();
  }
  const auto* env = target.Find("environment");
  const auto* vaults = target.GetArray("vault_ids");
  if (target.GetString("type") != "new_session" || !env || !vaults) {
    inspector.value.uncertain = true;
    return inspector.Finish();
  }
  inspector.Reference(target.Find("agent_id"));
  if (const auto* agent = target.Find("agent"); agent && agent->IsObject())
    inspector.AgentTools(agent->GetArray("tools"));
  inspector.Reference(env->Find("environment_id"));
  inspector.Reference(env->Find("environment_template_id"));
  if (const auto* files = env->GetArray("files"))
    for (const auto& file : files->array)
      if (file.GetString("type") == "file_id")
        inspector.RequiredId(file, "file_id", "file_");
  if (const auto* skills = env->GetArray("skills"))
    for (const auto& skill : skills->array)
      if (skill.GetString("type") == "skill_reference")
        inspector.Reference(skill.Find("skill_id"));
  for (const auto& id : vaults->array) inspector.Reference(&id);
  inspector.Items(target.Find("input"));
  return inspector.Finish();
}

ObjectRetentionInfo InspectObjectRetention(std::string_view kind,
                                           const io::Json& data) {
  Inspector inspector;
  if (!data.IsObject()) {
    inspector.value.uncertain = true;
    return inspector.Finish();
  }
  const bool application = SupportsApplicationPayload(kind);
  std::vector<ApplicationStringRef> payloads;
  const bool valid_payloads =
      !application || ParseApplicationPayloadReferences(data, &payloads);
  if (kind == "response") {
    if (const auto* response = data.Find("response");
        response && response->IsObject()) {
      const auto status = response->GetString("status");
      inspector.value.active = status == "queued" || status == "in_progress";
      inspector.value.uncertain =
          !inspector.value.active && status != "completed" &&
          status != "incomplete" && status != "failed" && status != "cancelled";
      inspector.Items(response->Find("output"));
    } else {
      inspector.value.uncertain = true;
    }
    if (const auto* ids = data.Find("agent_artifact_ids")) {
      if (!ids->IsArray() || !data.Has("agent_session_id")) {
        inspector.value.uncertain = true;
      } else {
        for (const auto& id : ids->array) {
          if (!id.IsString() || !id.str.starts_with("artifact_"))
            inspector.value.uncertain = true;
          inspector.Reference(&id);
        }
      }
    }
    inspector.Request(data.Find("request"));
    inspector.Items(data.Find("history"));
    inspector.Items(data.Find("own_input"));
    inspector.Items(data.Find("mcp_catalogs"));
    if (const auto* continuation = data.Find("continuation")) {
      const auto* id = continuation->Find("conversation_id");
      if (id && id->IsString() && !id->str.empty()) inspector.Reference(id);
    }
  } else if (kind == "conversation") {
    inspector.value.uncertain = !data.GetArray("items");
    inspector.Items(data.Find("items"));
  } else if (kind == "item") {
    inspector.value.uncertain =
        !data.Find("item") || !data.Find("item")->IsObject();
    inspector.Items(data.Find("item"));
  } else if (kind == "audio" &&
             data.GetString("format") == "q4t.chat_audio.v1") {
    // Self-contained transcript and completed waveform digest; expiry is
    // mandatory and does not extend when another message references the ID.
    inspector.value.uncertain =
        data.GetInt("expires_at") <= 0 || data.GetString("transcript").empty();
  } else if (kind == "voice_consent" &&
             data.GetString("format") == "q4t.voice_consent.v1") {
    inspector.Blob(data);
  } else if (kind == "voice" &&
             data.GetString("format") == "q4t.voice.v1") {
    inspector.Blob(data);
    inspector.Reference(data.Find("consent"));
    if (!data.Find("consent") || data.Find("consent")->IsNull())
      inspector.value.uncertain = true;
  } else if (kind == "chat") {
    inspector.value.uncertain = !data.GetArray("messages");
    inspector.Items(data.Find("messages"));
  } else if (kind == "prompt") {
    inspector.value.uncertain = !data.GetArray("versions");
    if (const auto* versions = data.GetArray("versions"))
      for (const auto& version : versions->array) {
        if (const auto* definition = version.Find("definition");
            definition && definition->Find("request"))
          inspector.Request(definition->Find("request"));
        else
          inspector.value.uncertain = true;
      }
  } else if (kind == "vector_store" &&
             data.GetString("format") == "q4t.vector_store.v1") {
    const auto* store = data.Find("vector_store");
    const auto* members = data.GetArray("members");
    if (!store || !members || members->array.size() > 10000) {
      inspector.value.uncertain = true;
    } else {
      inspector.value.references_before = store->GetInt("expires_at");
      for (const auto& member : members->array)
        inspector.Reference(member.Find("id"));
      if (const auto* batches = data.GetArray("batches")) {
        if (batches->array.size() > 10000)
          inspector.value.uncertain = true;
        else
          for (const auto& batch : batches->array) inspector.Reference(&batch);
      } else if (data.Has("batches")) {
        inspector.value.uncertain = true;
      }
    }
  } else if (kind == "vector_store_batch" &&
             data.GetString("format") == "q4t.vector_store_batch.v1") {
    // Historical counts and metadata must not pin detached files or indexes.
    // The live parent retains this receipt; store expiration releases it.
    inspector.value.collect_when_unreferenced = true;
    inspector.value.uncertain =
        !data.GetArray("members") || !data.Find("batch");
    if (const auto* members = data.GetArray("members"))
      for (const auto& row : members->array)
        if (row.GetBool("detached") && row.GetString("status") == "in_progress")
          inspector.Reference(row.Find("id"));
  } else if (kind == "vector_store_file" &&
             data.GetString("format") == "q4t.vector_store_file.v1") {
    const auto* file = data.Find("file");
    const auto status = file ? file->GetString("status") : "";
    inspector.value.uncertain = status != "in_progress" &&
                                status != "completed" && status != "failed" &&
                                status != "cancelled";
    // Only the live owning store or an explicit bounded read hold retains
    // this member. An abandoned job cannot protect an expired store forever.
    inspector.value.collect_when_unreferenced = true;
    if (file) inspector.Reference(file->Find("id"));
  } else if (kind == "batch" && data.GetString("format") == "q4t.batch.v1") {
    const auto* batch = data.Find("batch");
    const auto* pages = data.GetArray("pages");
    if (!batch || !batch->IsObject() || !pages ||
        pages->array.size() > kBatchPages) {
      inspector.value.uncertain = true;
    } else {
      const auto status = batch->GetString("status");
      inspector.value.active = status == "validating" ||
                               status == "in_progress" ||
                               status == "finalizing" || status == "cancelling";
      inspector.value.uncertain |=
          !inspector.value.active && !BatchTerminal(status);
      const auto* released = data.Find("resources_released");
      if (released && (!released->IsBool() ||
                       (released->boolean && !BatchTerminal(status))))
        inspector.value.uncertain = true;
      if (!ValidBatchOutputFailure(data)) inspector.value.uncertain = true;
      if (data.GetBool("resources_released") && data.Has("output_failure")) {
        BatchOutputFailure failure;
        if (status != "failed" ||
            !ParseBatchOutputFailure(*data.Find("output_failure"), &failure) ||
            failure.retain_until <= 0) {
          inspector.value.uncertain = true;
        } else {
          // Release queue admission independently from the original results.
          // These references expire; the historical receipt is not a pin.
          inspector.value.references_before = failure.retain_until;
          for (const auto& page : pages->array) inspector.Reference(&page);
        }
      }
      if (!data.GetBool("resources_released")) {
        inspector.Blob(data);
        for (const auto& page : pages->array) inspector.Reference(&page);
        if (const auto* progress = data.Find("finalization")) {
          if (progress->GetString("format") != "q4t.batch_finalization.v1")
            inspector.value.uncertain = true;
          for (const auto* lane : {"output", "error"}) {
            const auto* blob = progress->Find(lane);
            if (!blob) {
              inspector.value.uncertain = true;
              continue;
            }
            inspector.Manifest(blob);
          }
        }
      }
      // Result Files have their own declared expiry. A historical Batch
      // receipt must not pin their content beyond output_expires_after.
    }
  } else if (kind == "agent" && data.GetString("object") == "agent") {
    inspector.AgentTools(data.GetArray("tools"));
  } else if (kind == "vault" && data.GetString("object") == "vault") {
    // The container metadata has no content or credential object edges.
  } else if (kind == "webhook_event") {
    const auto* event = data.Find("event");
    const auto* deliveries = data.GetArray("deliveries");
    inspector.value.uncertain =
        data.GetString("format") != "q4t.webhook.event.v1" || !event ||
        !event->GetString("id").starts_with("evt_") || !deliveries;
    // The queue is retained up to its absolute delivery deadline, even
    // when the sender is disabled. Pending work must not pin an expired row.
    if (data.GetInt("deliver_before") <= 0) inspector.value.uncertain = true;
  } else if (kind == "webhook_endpoint") {
    const auto* endpoint = data.Find("resource");
    inspector.value.uncertain =
        data.GetString("format") != "q4t.webhook.endpoint.v1" || !endpoint ||
        endpoint->GetString("object") != "webhook_endpoint" ||
        !endpoint->GetString("id").starts_with("whe_") ||
        data.GetString("sealed_keys").empty();
    // A subscription remains active until its owner deletes it.
    inspector.value.active = !inspector.value.uncertain;
  } else if (kind == "automation" || kind == "invocation") {
    const auto* row = data.Find("resource");
    const auto status = row ? row->GetString("status") : "";
    const bool parent = kind == "automation";
    const bool queued = !parent && status == "queued";
    inspector.value.active = queued || (parent && status == "active");
    const auto object = parent ? "agent.automation" : "agent.automation.invocation";
    if (!row || row->GetString("object") != object ||
        (!inspector.value.active && status != "completed" &&
         !(parent ? status == "paused" : status == "skipped" || status == "failed")))
      inspector.value.uncertain = true;
    if (parent || queued) {
      const auto* refs = data.GetArray("target_references");
      if (!refs || !data.Has("sealed_target")) inspector.value.uncertain = true;
      if (refs)
        for (const auto& id : refs->array) inspector.Reference(&id);
    }
    if (parent) inspector.Reference(data.Find("pending_delivery"));
    if (!parent && row) {
      inspector.Reference(row->Find("session_id"));
      inspector.RequiredId(*row, "automation_id", "automation_", false);
      inspector.value.owning_automation = row->GetString("automation_id");
    }
  } else if (kind == "environment_template" || kind == "environment") {
    inspector.HostedEnvironment(data, kind == "environment_template", payloads);
    if (kind == "environment") inspector.Snapshots(data);
  } else if (kind == "thread") {
    if (const auto* runs = data.Find("runs")) {
      if (!runs->IsArray()) inspector.value.uncertain = true;
      for (const auto& entry : runs->array) {
        const auto* responses = entry.GetArray("response_ids");
        if (!responses || responses->array.empty()) inspector.value.uncertain = true;
        else for (const auto& id : responses->array) inspector.Reference(&id);
      }
    }
  } else if (kind == "agent_session") {
    const auto* session = data.Find("session");
    const auto* agent = session ? session->Find("agent") : nullptr;
    const auto* vaults = session ? session->GetArray("vault_ids") : nullptr;
    const auto* turns = data.GetArray("turns");
    const auto* env = session ? session->Find("environment") : nullptr;
    const bool hosted = env && env->GetString("type") == "openai_hosted";
    const bool idle = hosted && session->GetString("status") == "idle" &&
                      data.GetBool("created_idle");
    if (!session || session->GetString("object") != "agent.session" ||
        !agent || !vaults || !turns ||
        (turns->array.empty() && !idle)) {
      inspector.value.uncertain = true;
    } else {
      if (hosted) inspector.RequiredId(*env, "id", "environment_");
      inspector.AgentTools(agent->GetArray("tools"));
      if (const auto* versions = data.Find("settings")) {
        if (!versions->IsObject()) inspector.value.uncertain = true;
        for (const auto& [_, delta] : versions->object) {
          if (const auto* changed = delta.Find("agent"); changed && changed->Has("tools"))
            inspector.AgentTools(changed->GetArray("tools"));
        }
      }
      inspector.Items(data.Find("pending_tool_results"));
      for (const auto& vault : vaults->array) inspector.Reference(&vault);
      for (const auto& turn : turns->array) {
        if (!turn.Has("response_id")) inspector.value.uncertain = true;
        inspector.Reference(turn.Find("response_id"));
        inspector.Items(turn.Find("saved_tool_results"));
      }
    }
  } else if (kind == "live_recording" &&
             data.GetString("format") == "q4t.live_recording.v1") {
    inspector.Blob(data);
    inspector.Reference(data.Find("fork_state_id"));
  } else if (kind == "live_fork_state" &&
             data.GetString("format") == "q4t.live_fork.v1") {
    inspector.value.collect_when_unreferenced = true;
  } else if (kind == "skill" && data.GetString("format") == "q4t.skill.v1") {
    const auto* versions = data.GetArray("versions");
    if (!versions)
      inspector.value.uncertain = true;
    else
      for (const auto& version : versions->array)
        inspector.RequiredId(version, "id", "skillver_");
  } else if (kind == "skill_version" &&
             data.GetString("format") == "q4t.skill_version.v1") {
    inspector.Blob(data);
    inspector.value.collect_when_unreferenced = true;
  } else if (kind == "container" &&
             data.GetString("format") == "q4t.container.v1") {
    // Public metadata survives expiration. It must not retain workspace bytes.
  } else if (kind == "container_file" &&
             data.GetString("format") == "q4t.container_file.v1") {
    inspector.Blob(data);
    inspector.value.collect_when_unreferenced = true;
  } else if (kind == "file" && data.GetString("format") == "q4t.file.v1") {
    inspector.Blob(data);
  } else if (kind == "input_snapshot" &&
             data.GetString("format") == "q4t.input_snapshot.v1") {
    inspector.Blob(data);
    inspector.Snapshots(data);
    inspector.value.collect_when_unreferenced = true;
  } else if (kind == "tool_run") {
    const auto format = data.GetString("format");
    if (format == "q4t.container_workspace.v1") {
      const auto* files = data.GetArray("files");
      if (!files)
        inspector.value.uncertain = true;
      else
        for (const auto& file : files->array)
          inspector.RequiredId(file, "id", "cfile_");
      if (const auto* mounts = data.Find("mounted_skills")) {
        if (!mounts->IsArray()) inspector.value.uncertain = true;
        else for (const auto& mount : mounts->array)
          inspector.RequiredId(mount, "id", "mount_");
      }
    } else if (format == "q4t.container_skill.v1") {
      inspector.Blob(data);
      inspector.value.collect_when_unreferenced = true;
    } else if (format == "q4t.environment.creation.v1") {
      inspector.RequiredId(data, "environment_id", "environment_");
      const auto digest = data.GetString("request_sha256");
      if (digest.size() != 64 ||
          digest.find_first_not_of("0123456789abcdef") != digest.npos)
        inspector.value.uncertain = true;
    } else if (format == "q4t.web.index.v1") {
      // Complete inline page snapshots have no application-object edges.
      // URLs, page text and image metadata are data, never retention IDs.
      const auto* pages = data.GetArray("pages");
      const auto digest = [](const std::string& text) {
        return text.size() == 64 &&
               std::all_of(text.begin(), text.end(), [](char c) {
                 return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
               });
      };
      inspector.value.uncertain =
          data.object.size() != 5 || !pages || pages->array.size() > 32 ||
          !digest(data.GetString("policy_sha256")) ||
          !digest(data.GetString("scope_sha256")) ||
          data.GetInt("observed_at") < 1;
      if (pages)
        for (const auto& page : pages->array) {
          inspector.value.uncertain |= !page.IsObject() ||
              page.object.size() != 6 || !page.GetArray("links") ||
              !page.GetArray("images") ||
              !digest(page.GetString("source_sha256"));
          for (const auto* key : {"url", "title", "text"})
            inspector.value.uncertain |= !page.Find(key) ||
                                         !page.Find(key)->IsString();
        }
    } else if (format == "q4t.mcp.approval.v1") {
      const auto* sealed = data.Find("sealed");
      inspector.value.uncertain =
          data.object.size() != 3 || !sealed || !sealed->IsString() ||
          !sealed->str.starts_with("q4t1:") ||
          !data.GetString("call_id").starts_with("mcp_");
      inspector.Reference(data.Find("call_id"));
    } else if (format == "q4t.mcp.catalog.v1" || format == "q4t.mcp.call.v1") {
      // Native authority stays until its original expiry or a live public
      // reference, including before the Response commits. Locally prepared
      // media has explicit snapshot dependencies mirrored in the sealed call;
      // remote resource URIs themselves never become local file references.
      const auto* sealed = data.Find("sealed");
      const bool media =
          format == "q4t.mcp.call.v1" && data.Has("input_snapshots");
      inspector.value.uncertain = data.object.size() != (media ? 3U : 2U) ||
                                  !sealed || !sealed->IsString() ||
                                  !sealed->str.starts_with("q4t1:");
      if (media) inspector.Snapshots(data);
    } else if (format == "q4t.binary_chunk.v1") {
      inspector.value.collect_when_unreferenced = true;
    } else if (format == "q4t.binary_stage.v1" ||
               format == "q4t.binary_read.v1") {
      inspector.Blob(data);
    } else if (format == "q4t.agent_artifact.v1") {
      const auto* row = data.Find("resource");
      if (!row || row->GetString("object") != "agent.session.artifact" ||
          !row->GetString("id").starts_with("artifact_"))
        inspector.value.uncertain = true;
      inspector.Blob(data);
      inspector.value.collect_when_unreferenced = true;
    } else if (format == "q4t.upload_part.v1") {
      inspector.Blob(data);
      inspector.value.collect_when_unreferenced = true;
    } else if (format == "q4t.batch_result.v1") {
      inspector.Blob(data);
      inspector.value.collect_when_unreferenced = true;
    } else if (IsBatchPage(data)) {
      inspector.value.collect_when_unreferenced = true;
      const auto* rows = data.GetArray("rows");
      if (!rows || rows->array.empty() || rows->array.size() > kBatchPageRows ||
          !ValidApplicationId(data.GetString("batch_id"))) {
        inspector.value.uncertain = true;
      } else {
        for (const auto& row : rows->array) {
          const auto status = row.GetString("status");
          inspector.value.uncertain |=
              status != "pending" && status != "running" &&
              status != "completed" && status != "failed";
          inspector.Reference(row.Find("result_id"));
          if (const auto* background = row.Find("background_response")) {
            BatchResponseLink link;
            if (!ParseBatchResponseLink(*background, &link) ||
                link.batch_id != data.GetString("batch_id") ||
                link.request_id != row.GetString("request_id") ||
                status == "pending")
              inspector.value.uncertain = true;
            else if (status == "running")
              inspector.Reference(background->Find("response_id"));
          }
          if (row.Has("prepared_inline"))
            inspector.value.uncertain |=
                row.Has("input_snapshots") ||
                !ValidBatchInlineInput(*row.Find("prepared_inline"));
          if (row.Has("result_inline"))
            inspector.value.uncertain |= !ValidBatchInlineResult(row);
          inspector.Snapshots(row);
          inspector.Request(row.Find("request_dependencies"));
        }
      }
    } else if (format == "q4t.input_snapshot_hold.v1") {
      const auto id = data.GetString("snapshot_id");
      inspector.value.uncertain = !id.starts_with("input_snapshot_");
      inspector.Reference(data.Find("snapshot_id"));
    } else if (format == "q4t.upload.v1") {
      const auto* upload = data.Find("upload");
      const auto* parts = data.GetArray("parts");
      if (!upload || !upload->IsObject() || !parts) {
        inspector.value.uncertain = true;
      } else if (upload->GetString("status") == "pending") {
        inspector.value.references_before = upload->GetInt("expires_at");
        inspector.value.uncertain = inspector.value.references_before <= 0;
        for (const auto& part : parts->array) inspector.Reference(&part);
      } else if (upload->GetString("status") != "completed" &&
                 upload->GetString("status") != "cancelled" &&
                 upload->GetString("status") != "expired") {
        inspector.value.uncertain = true;
      }
    } else {
      inspector.value.uncertain = true;
    }
  } else {
    // Future Files/Batch/tool executors must register their real lifecycle and
    // dependency extraction before automatic collection can safely use them.
    inspector.value.uncertain = true;
  }
  // Snapshot validation adds uncertainty; ordinary shape checks above must
  // never overwrite a malformed private dependency's conservative protection.
  if (application) {
    inspector.Snapshots(data);
    inspector.Payloads(payloads);
    if (!valid_payloads) inspector.value.uncertain = true;
  }
  return inspector.Finish();
}

Status PlanObjectRetention(std::span<const ObjectRetentionNode> nodes,
                           int64_t now, int64_t item_created_before,
                           size_t limit, ObjectRetentionPlan* plan) {
  *plan = {};
  if (now <= 0 || item_created_before < 0 || item_created_before > now ||
      limit == 0 || limit > 128 || nodes.size() > 100000)
    return Status::Fail("Invalid retention planning limits");
  std::unordered_map<std::string, std::vector<size_t>> identities;
  std::unordered_set<std::string_view> uncertain_owners;
  std::vector<bool> candidate(nodes.size()), protected_node(nodes.size());
  std::deque<size_t> queue;
  std::string fingerprint =
      "q4t-retention-v1:" + std::to_string(item_created_before) + ':' +
      std::to_string(limit) + '\n';
  size_t bytes = nodes.size() * (sizeof(ObjectRetentionNode) + 128);
  for (size_t i = 0; i < nodes.size(); ++i) {
    const auto& n = nodes[i];
    if (!n.retention || n.owner.size() != 64 || n.digest.size() != 64 ||
        !ValidApplicationId(n.id) || n.kind.empty() || n.created <= 0 ||
        n.expires < 0)
      return Status::Fail("Invalid retention metadata");
    bytes += n.retention->Bytes() + n.owner.size() + n.id.size() +
             n.kind.size() + n.digest.size();
    if (bytes > kPlanBytes)
      return Status::Fail("Retention planning metadata limit exceeded");
    identities[Key(n.owner, n.id)].push_back(i);
    if (n.retention->uncertain) uncertain_owners.insert(n.owner);
    candidate[i] = n.retention->collect_when_unreferenced ||
                   (n.expires > 0 && n.expires <= now) ||
                   (item_created_before && n.kind == "item" && n.expires == 0 &&
                    n.created < item_created_before);
    fingerprint += std::string(n.owner) + ':' + std::string(n.kind) + ':' +
                   std::string(n.id) + ':' + std::string(n.digest) + '\n';
  }
  for (size_t i = 0; i < nodes.size(); ++i) {
    const auto& n = nodes[i];
    if (!n.retention->owning_automation.empty()) {
      const auto parent = identities.find(Key(n.owner, n.retention->owning_automation));
      candidate[i] = candidate[i] || parent == identities.end() ||
          std::ranges::none_of(parent->second, [&](size_t index) {
            return nodes[index].kind == "automation";
          });
    }
    const bool unknown = uncertain_owners.contains(n.owner);
    plan->active += n.retention->active;
    plan->uncertain += unknown;
    if (!candidate[i] || n.retention->active || unknown) {
      protected_node[i] = true;
      queue.push_back(i);
    }
  }
  size_t work = 0;
  while (!queue.empty()) {
    const auto i = queue.front();
    queue.pop_front();
    if (nodes[i].retention->references_before > 0 &&
        nodes[i].retention->references_before <= now)
      continue;
    for (const auto& ref : nodes[i].retention->references) {
      if (++work > 2 * 1024 * 1024)
        return Status::Fail("Retention reference work limit exceeded");
      const auto found = identities.find(Key(nodes[i].owner, ref));
      if (found == identities.end()) continue;
      for (const auto target : found->second)
        if (!protected_node[target]) {
          protected_node[target] = true;
          queue.push_back(target);
        }
    }
  }
  std::string_view owner;
  for (size_t i = 0; i < nodes.size(); ++i) {
    if (!candidate[i]) continue;
    if (protected_node[i]) {
      plan->referenced += !nodes[i].retention->active &&
                          !uncertain_owners.contains(nodes[i].owner);
      continue;
    }
    ++plan->eligible;
    if (owner.empty()) owner = nodes[i].owner;
    if (plan->selected.size() < limit && nodes[i].owner == owner) {
      plan->selected.push_back(i);
      plan->bytes += nodes[i].bytes;
      fingerprint += "delete:" + std::to_string(i) + '\n';
    }
  }
  fingerprint += "eligible:" + std::to_string(plan->eligible);
  plan->sha256 = trace::HexDigest(
      trace::Sha256({reinterpret_cast<const uint8_t*>(fingerprint.data()),
                     fingerprint.size()}));
  return {};
}
}  // namespace q4t::server

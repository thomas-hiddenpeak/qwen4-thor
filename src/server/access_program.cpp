#include "q4t/server/access_program.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <set>

#include "q4t/server/chat_plan.h"
#include "q4t/server/model_catalog.h"
#include "q4t/trace/sha256.h"
#include "responses_json.h"

namespace q4t::server {
namespace {
using namespace responses_detail;
constexpr size_t kPolicyBytes = 64 * 1024;

Status InvalidPolicy() {
  return Status::Fail("Invalid native access program policy");
}
bool Fields(const Json& value, std::initializer_list<std::string_view> names) {
  if (!value.IsObject() || value.object.size() != names.size()) return false;
  return std::all_of(
      value.object.begin(), value.object.end(), [&](const auto& field) {
        return std::find(names.begin(), names.end(), field.first) !=
               names.end();
      });
}
Status Failure(ChatPlanError* error, int status, const char* code,
               const char* message,
               const char* param = "access_programs.cyber") {
  *error = {code, param, status};
  return Status::Fail(message);
}
}  // namespace

Status ParseAccessProgramPolicy(std::string_view raw,
                                AccessProgramPolicy* output) {
  if (!output || raw.empty() || raw.size() > kPolicyBytes)
    return InvalidPolicy();
  Json value;
  if (!io::ParseJson(std::string(raw), &value, true,
                     {.max_depth = 6,
                      .max_values = 512,
                      .max_string_bytes = kPolicyBytes,
                      .reject_duplicate_keys = true}) ||
      !Fields(value, {"version", "authority", "model", "cyber_tier",
                      "minimum_grant", "default_selection", "standard_allowed",
                      "program_instructions", "clients"}) ||
      !value.Find("version")->IsNumber() || value.Find("version")->str != "1" ||
      value.GetString("authority") != "q4t-local" ||
      !ValidModelIdentifier(value.GetString("model")) ||
      !value.Find("standard_allowed")->IsBool())
    return InvalidPolicy();
  AccessProgramPolicy candidate;
  candidate.model = value.GetString("model");
  candidate.tier = value.GetString("cyber_tier");
  candidate.minimum_grant = value.GetString("minimum_grant");
  candidate.standard_allowed = value.GetBool("standard_allowed");
  const auto selected = value.GetString("default_selection");
  candidate.default_standard = selected == "standard";
  candidate.instructions = value.GetString("program_instructions");
  if ((candidate.tier != "blue" && candidate.tier != "red") ||
      (candidate.minimum_grant != "blue" && candidate.minimum_grant != "red") ||
      (candidate.tier == "red" && candidate.minimum_grant != "red") ||
      (selected != "standard" && selected != "model_tier") ||
      (candidate.default_standard && !candidate.standard_allowed) ||
      candidate.instructions.find_first_not_of(" \t\n\r\v\f") ==
          std::string::npos ||
      candidate.instructions.size() > 16384 ||
      candidate.instructions.find('\0') != std::string::npos)
    return InvalidPolicy();
  const auto* clients = value.GetArray("clients");
  if (!clients || clients->array.size() > kMaxClients) return InvalidPolicy();
  std::set<std::string> ids;
  for (const auto& client : clients->array) {
    const auto id = client.GetString("id");
    const auto level = client.GetString("grant");
    if (!Fields(client, {"id", "grant"}) || !ValidClientId(id) ||
        !ids.insert(id).second || (level != "blue" && level != "red"))
      return InvalidPolicy();
    candidate.clients.push_back({id, level});
  }
  candidate.sha256 = trace::HexDigest(trace::Sha256(
      {reinterpret_cast<const uint8_t*>(raw.data()), raw.size()}));
  *output = std::move(candidate);
  return {};
}

Status LoadAccessProgramPolicy(const std::string& path,
                               AccessProgramPolicy* output) {
  const int fd =
      ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
  if (fd < 0) return InvalidPolicy();
  struct Close {
    int fd;
    ~Close() { ::close(fd); }
  } close{fd};
  struct stat before {
  }, after{};
  if (::fstat(fd, &before) || !S_ISREG(before.st_mode) ||
      before.st_uid != ::geteuid() || (before.st_mode & 0077) ||
      before.st_nlink != 1 || before.st_size <= 0 ||
      static_cast<uint64_t>(before.st_size) > kPolicyBytes)
    return InvalidPolicy();
  std::string raw(static_cast<size_t>(before.st_size) + 1, '\0');
  size_t count = 0;
  while (count < raw.size()) {
    const auto got = ::read(fd, raw.data() + count, raw.size() - count);
    if (got < 0 && errno == EINTR) continue;
    if (got < 0) return InvalidPolicy();
    if (!got) break;
    count += static_cast<size_t>(got);
  }
  if (::fstat(fd, &after) || count != static_cast<size_t>(before.st_size) ||
      before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
      before.st_size != after.st_size || before.st_mode != after.st_mode ||
      before.st_uid != after.st_uid || before.st_nlink != after.st_nlink ||
      before.st_mtim.tv_sec != after.st_mtim.tv_sec ||
      before.st_mtim.tv_nsec != after.st_mtim.tv_nsec ||
      before.st_ctim.tv_sec != after.st_ctim.tv_sec ||
      before.st_ctim.tv_nsec != after.st_ctim.tv_nsec)
    return InvalidPolicy();
  raw.resize(count);
  return ParseAccessProgramPolicy(raw, output);
}

Status BindAccessProgramPolicy(const AccessProgramPolicy& policy,
                               const std::string& actual_model,
                               const GatewayClientPolicy* clients) {
  if (policy.model != actual_model || !clients) return InvalidPolicy();
  for (const auto& grant : policy.clients)
    if (std::none_of(clients->clients.begin(), clients->clients.end(),
                     [&](const auto& client) { return client.id == grant.id; }))
      return InvalidPolicy();
  return {};
}

Status ResolveAccessProgram(const Json& request, const ChatPlanConfig& config,
                            AccessProgramSelection* output,
                            ChatPlanError* error) {
  *output = {};
  std::string selected;
  if (const auto* access = request.Find("access_programs")) {
    if (!access->IsObject())
      return Failure(error, 400, "invalid_request", "Expected access object",
                     "access_programs");
    if (access->object.size() > 1 ||
        (!access->object.empty() && access->object[0].first != "cyber"))
      return Failure(error, 400, "invalid_request", "Unknown access field",
                     "access_programs");
    if (const auto* cyber = access->Find("cyber")) {
      if (!cyber->IsString() ||
          (cyber->str != "standard" && cyber->str != "daybreak_blue" &&
           cyber->str != "daybreak_red"))
        return Failure(error, 400, "invalid_access_program",
                       "Invalid cyber access program");
      selected = cyber->str;
    }
  }
  const bool explicit_selection = !selected.empty();
  const auto* policy = config.access_program_policy.get();
  if (!policy) {
    if (explicit_selection && selected != "standard")
      return Failure(error, 400, "unsupported_access_program",
                     "The served model has no configured native cyber tier");
  } else {
    if (policy->model != config.model)
      return Failure(error, 503, "access_program_policy_invalid",
                     "Access program model binding changed");
    const auto tier = "daybreak_" + policy->tier;
    bool authorized = false;
    if (config.client_limits)
      for (const auto& grant : policy->clients)
        if (grant.id == config.client_limits->id)
          authorized = grant.level == "red" || policy->minimum_grant == "blue";
    if (!explicit_selection) {
      selected = policy->default_standard ? "standard" : tier;
      if (!authorized && policy->tier == "blue" && policy->standard_allowed)
        selected = "standard";
    }
    if (selected != "standard" && selected != tier)
      return Failure(error, 400, "invalid_access_program",
                     "Selected program is incompatible with this model tier");
    if (selected == "standard" && !policy->standard_allowed)
      return Failure(error, 400, "invalid_access_program",
                     "This model policy requires its cyber access program");
    if (selected != "standard" && !authorized)
      return Failure(
          error, 403, "access_program_not_enabled",
          "The authenticated client lacks the required native grant");
    output->provenance =
        Object({{"authority", String("q4t-local")},
                {"policy_sha256", String(policy->sha256)},
                {"backend_model", String(config.model)},
                {"treatment", String(selected == "standard"
                                         ? "standard"
                                         : "operator_system_instructions")}});
    if (selected != "standard") output->instructions = policy->instructions;
  }
  if (explicit_selection || (!selected.empty() && selected != "standard"))
    output->selected = Object({{"cyber", String(selected)}});
  return {};
}

void AddAccessProgramInstructions(const AccessProgramSelection& selection,
                                  Json* messages) {
  if (!selection.instructions.empty())
    messages->array.insert(
        messages->array.begin(),
        Object({{"role", String("system")},
                {"content", String(selection.instructions)}}));
}

Json AccessProgramCapabilities(const AccessProgramPolicy* policy) {
  return Object({{"enabled", Boolean(true)},
                 {"authority", String("q4t-local")},
                 {"policy_configured", Boolean(policy != nullptr)},
                 {"policy_sha256", String(policy ? policy->sha256 : "")},
                 {"cyber_tier", String(policy ? policy->tier : "standard")},
                 {"treatment", String("operator_system_instructions")},
                 {"client_authorization", Boolean(true)},
                 {"cloud_approval", Boolean(false)},
                 {"runtime_file_dependency", Boolean(false)},
                 {"queued_recovery", String("revalidate_identity_and_grant")}});
}
}  // namespace q4t::server

#include "containers_internal.h"

#include <algorithm>
#include <ctime>

#include "object_protocol.h"
#include "q4t/server/files.h"
#include "q4t/server/lifetime.h"
#include "q4t/server/request_json.h"

namespace q4t::server::container_detail {
namespace {
using namespace object_detail;
using Kind = ContainerRouteKind;
BinaryAccess Source(const StateAccess& access) {
  return {access.scope, access.deadline, access.cancelled};
}
ObjectReply Missing() {
  return Error(404, "object_not_found", "Container file not found");
}
ObjectReply Entry(Stored* state, std::string_view filename, uint64_t bytes,
                  ObjectRequest* write, Json* entry) {
  const auto slash = filename.find_last_of("/\\");
  if (slash != filename.npos) filename.remove_prefix(slash + 1);
  if (filename.empty() || filename == "." || filename == ".." ||
      std::ranges::any_of(filename,
                          [](unsigned char c) { return c < 32 || c == 127; }))
    return Invalid("Expected a file name", "file");
  write->kind = ObjectKind::kContainerFile;
  write->operation = ObjectOperation::kPut;
  write->scope = state->metadata.scope;
  write->ttl_seconds = 0;
  write->id = NewObjectId(write->kind);
  if (write->id.empty())
    return Error(503, "object_entropy_unavailable", "No container file ID");
  auto& files = Field(state->workspace.data, "files");
  std::string path = "/mnt/data/" + std::string(filename);
  if (std::ranges::any_of(files.array, [&](const Json& f) {
        return f.GetString("path") == path;
      }))
    path = "/mnt/data/" + write->id + "/" + std::string(filename);
  *entry = Object({{"object", String("container.file")},
                   {"id", String(write->id)},
                   {"container_id", String(state->metadata.id)},
                   {"created_at", Number(std::time(nullptr))},
                   {"bytes", Number(bytes)},
                   {"path", String(path)},
                   {"source", String("user")}});
  ChatPlanError validation;
  if (!ValidateContainer(*entry, ContainerShape::kFileCreated, &validation))
    return Corrupt();
  files.array.push_back(*entry);
  write->data = Object(
      {{"format", String("q4t.container_file.v1")}, {"resource", *entry}});
  return {};
}
ObjectReply FilePage(const Json& files, const Json& query) {
  ChatPlanError validation;
  auto valid =
      ValidateContainer(query, ContainerShape::kFileQuery, &validation);
  if (!valid) return Invalid(valid.message(), validation.param);
  const auto limit = query.GetInt("limit", 20);
  if (limit < 1 || limit > 100)
    return Invalid("Expected 1 to 100 entries", "limit");
  std::vector<const Json*> sorted;
  for (const auto& f : files.array) sorted.push_back(&f);
  const bool desc = query.GetString("order", "desc") == "desc";
  std::ranges::sort(sorted, [&](auto* a, auto* b) {
    const auto left = std::pair{a->GetInt("created_at"), a->GetString("id")};
    const auto right = std::pair{b->GetInt("created_at"), b->GetString("id")};
    return desc ? left > right : left < right;
  });
  auto start = sorted.begin();
  if (query.Has("after")) {
    const auto id = query.GetString("after");
    start = std::ranges::find_if(
        sorted, [&](auto* r) { return r->GetString("id") == id; });
    if (start == sorted.end())
      return Invalid("Unknown container file cursor", "after");
    ++start;
  }
  auto data = Array();
  while (start != sorted.end() &&
         data.array.size() < static_cast<size_t>(limit))
    data.array.push_back(**start++);
  const auto first =
      data.array.empty() ? String("") : *data.array.front().Find("id");
  const auto last =
      data.array.empty() ? String("") : *data.array.back().Find("id");
  ObjectReply result;
  result.value = Object({{"object", String("list")},
                         {"data", std::move(data)},
                         {"first_id", first},
                         {"last_id", last},
                         {"has_more", Boolean(start != sorted.end())}});
  return result;
}
}  // namespace

void CloseFiles(ObjectStore& objects, const StateAccess& access,
                std::vector<BinaryRead>* pins) {
  BinaryStore binary(objects);
  const BinaryAccess cleanup{
      access.scope,
      std::chrono::steady_clock::now() + std::chrono::seconds(3),
      {}};
  for (auto& pin : *pins) {
    if (pin.id.empty() || pin.closed || pin.commit_unknown) continue;
    try {
      if (!binary.Close(cleanup, &pin).ok())
        objects.RecordAuxiliaryCleanupFailure();
    } catch (...) {
      objects.RecordAuxiliaryCleanupFailure();
    }
  }
}
ObjectReply PrepareFiles(ObjectStore& objects, const StateAccess& access,
                         const Json& ids, Stored* state,
                         std::vector<BinaryRead>* pins,
                         std::vector<ObjectRequest>* writes) {
  if (ids.array.size() > kMaxTransactionObjects - 2)
    return Invalid("Files exceed atomic container creation capacity",
                   "file_ids");
  Files files(objects);
  for (const auto& id : ids.array) {
    auto stored = files.Retrieve(Source(access), id.str);
    if (!stored.ok()) return stored;
    auto& pin = pins->emplace_back();
    auto opened = files.Open(Source(access), id.str, &pin);
    if (!opened.ok()) return opened;
    ObjectRequest write;
    Json item;
    auto added = Entry(state, stored.value.GetString("filename"),
                       pin.manifest.bytes, &write, &item);
    if (!added.ok()) return added;
    Set(&write.data, "blob", BinaryManifestJson(pin.manifest));
    writes->push_back(std::move(write));
  }
  return {};
}

ObjectReply ContainerFiles(ObjectStore& objects, const StateAccess& access,
                           const ContainerRoute& route, std::string_view method,
                           const Json& query, const std::string& body,
                           Stored state, BinaryRead* download,
                           BinaryStage* upload, std::string_view filename) {
  auto& files = Field(state.workspace.data, "files");
  const auto touch = [&]() -> ObjectReply {
    if (Field(state.metadata.data, "resource").GetInt("last_active_at") >=
        std::time(nullptr))
      return {};
    return Commit(objects, access, state);
  };
  if (route.kind == Kind::kFiles && method == "POST") {
    std::vector<ObjectRequest> writes;
    std::vector<BinaryRead> pins;
    ScopeExit cleanup([&] { CloseFiles(objects, access, &pins); });
    if (upload) {
      ObjectRequest root;
      Json entry;
      auto added =
          Entry(&state, filename, upload->manifest.bytes, &root, &entry);
      if (!added.ok()) return added;
      auto ready = BinaryStore(objects).PreparePublish(
          Source(access), *upload, std::move(root), &writes);
      if (!ready.ok()) return ready;
    } else {
      Json input;
      auto valid = io::ParseJson(body, &input, true, kApplicationJsonLimits);
      if (!valid || !input.IsObject() || input.object.size() != 1 ||
          !input.Find("file_id") || !input.Find("file_id")->IsString())
        return Invalid("Expected one Files ID or one uploaded file", "file_id");
      auto ready =
          PrepareFiles(objects, access, Array({*input.Find("file_id")}), &state,
                       &pins, &writes);
      if (!ready.ok()) return ready;
    }
    auto metadata = files.array.back();
    auto result = Commit(objects, access, std::move(state), std::move(writes));
    if (result.commit_unknown) {
      Set(&result.value, "file_id", *metadata.Find("id"));
      if (upload) upload->commit_unknown = true;
    } else if (result.ok()) {
      if (upload) upload->closed = true;
      result.value = std::move(metadata);
    }
    return result;
  }
  if (route.kind == Kind::kFiles) {
    auto page = FilePage(files, query);
    if (!page.ok()) return page;
    auto refreshed = touch();
    return refreshed.ok() ? page : refreshed;
  }
  const auto found = std::ranges::find_if(files.array, [&](const Json& f) {
    return f.GetString("id") == route.file;
  });
  if (found == files.array.end()) return Missing();
  ChatPlanError error;
  if (found->GetString("container_id") != route.container ||
      !ValidateContainer(*found, ContainerShape::kFileResource, &error))
    return Corrupt();
  ObjectRequest child;
  child.kind = ObjectKind::kContainerFile;
  child.scope = access.scope;
  child.id = route.file;
  if (method == "DELETE") {
    auto row = objects.Execute(child, access.deadline, access.cancelled);
    if (!row.ok()) return row;
    child.operation = ObjectOperation::kErase;
    child.expected_revision = row.value.GetInt("revision");
    files.array.erase(found);
    auto result = Commit(objects, access, std::move(state), {std::move(child)});
    if (result.ok())
      result.value = Object({{"object", String("container.file.deleted")},
                             {"id", String(std::string(route.file))},
                             {"deleted", Boolean(true)}});
    return result;
  }
  auto result = touch();
  if (!result.ok()) return result;
  if (route.kind == Kind::kContent) {
    if (!download)
      return Error(503, "container_download_unavailable",
                   "No download transport");
    return BinaryStore(objects).Open(Source(access), child.kind, child.id, 3600,
                                     download);
  }
  result.value = *found;
  return result;
}
}  // namespace q4t::server::container_detail

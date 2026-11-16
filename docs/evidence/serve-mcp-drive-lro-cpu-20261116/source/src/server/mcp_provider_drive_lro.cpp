#include "mcp_provider_common.h"

#include <algorithm>

#include "object_protocol.h"
#include "q4t/server/input_contract.h"

namespace q4t::server::mcp_detail {
namespace {
using namespace object_detail;
using namespace provider_detail;
constexpr std::string_view kDrive = "https://www.googleapis.com/drive/v3/";
bool Name(std::string_view name) {
  return !name.empty() && name.size() <= 2048 && name != "." && name != ".." &&
         std::none_of(name.begin(), name.end(),
                      [](unsigned char c) { return c < 32 || c == 127; });
}
ProviderRequest Read(std::string url, std::string_view id,
                     std::string_view key) {
  ProviderRequest request{std::move(url)};
  if (!key.empty())
    request.headers["X-Goog-Drive-Resource-Keys"] =
        std::string(id) + '/' + std::string(key);
  return request;
}
ObjectReply OperationError(const Json& error) {
  const auto* code = error.Find("code");
  if (!error.IsObject() || !code || !IsInputInteger(*code) || code->number < 1)
    return Protocol();
  // Keep authenticated failures known without echoing provider diagnostics or
  // treating the provider's cancellation as cancellation of our own worker.
  constexpr int status[] = {500, 499, 500, 400, 504, 404, 409, 403, 429,
                            400, 409, 400, 501, 500, 503, 500, 401};
  const int http = code->number <= 16 ? status[code->AsInt()] : 500;
  auto reply = Error(502, "mcp_provider_operation_failed",
                     "Provider download operation failed");
  reply.value = Object({{"upstream_status", Number(http)}});
  return reply;
}
}  // namespace
ObjectReply GoogleDriveDownload(std::string_view file_id,
                                std::string_view resource_key,
                                std::string_view operation_name,
                                const ProviderFetch& fetch, Json* value,
                                Json* content) {
  if (!Identifier(file_id) ||
      (!resource_key.empty() && !Identifier(resource_key)) ||
      (!operation_name.empty() && !Name(operation_name)))
    return Invalid();
  const bool start = operation_name.empty();
  auto request =
      Read(std::string(kDrive) +
               (start ? "files/" + ProviderEscape(file_id) +
                            "/download?mimeType=video%2Fmp4"
                      : "operations/" + ProviderEscape(operation_name)),
           file_id, resource_key);
  if (start) request.method = "POST";
  Json operation;
  auto reply = fetch(request, &operation);
  if (!reply.ok()) return reply;
  const auto name = operation.GetString("name");
  const auto* done = operation.Find("done");
  const auto* response = operation.Find("response");
  const auto* error = operation.Find("error");
  if (!Name(name) || (!start && name != operation_name) ||
      (done && !done->IsNull() && !done->IsBool()))
    return Protocol();
  const bool complete = done && done->IsBool() && done->boolean;
  if (!complete &&
      ((response && !response->IsNull()) || (error && !error->IsNull())))
    return Protocol();
  std::string key(resource_key);
  if (const auto* metadata = operation.Find("metadata");
      metadata && !metadata->IsNull()) {
    if (!metadata->IsObject()) return Protocol();
    if (const auto* actual = metadata->Find("resourceKey");
        actual && !actual->IsNull()) {
      if (!actual->IsString() || !Identifier(actual->str)) return Protocol();
      key = actual->str;
    }
  }
  if (complete && error && !error->IsNull()) {
    if (response && !response->IsNull()) return Protocol();
    return OperationError(*error);
  }
  Set(value, "operation_name", String(name));
  if (!complete) {
    auto resume = Object({{"file_id", String(std::string(file_id))},
                          {"operation_name", String(name)}});
    if (!key.empty()) Set(&resume, "resource_key", String(key));
    Set(value, "kind", String("pending_download"));
    Set(value, "done", Boolean(false));
    Set(value, "retry_after_seconds", Number(10));
    Set(value, "resume_arguments", std::move(resume));
    return {};
  }
  if (!response || !response->IsObject() ||
      response->GetString("@type") !=
          "type.googleapis.com/google.apps.drive.v3.DownloadFileResponse" ||
      !GoogleDownloadOrigin(response->GetString("downloadUri")))
    return Protocol();
  const auto* partial = response->Find("partialDownloadAllowed");
  if (partial && !partial->IsBool()) return Protocol();
  request = Read(response->GetString("downloadUri"), file_id, key);
  request.binary = true;
  request.google_download = true;
  Json bytes;
  reply = fetch(request, &bytes);
  if (!reply.ok()) return reply;
  const auto* data = bytes.Find("data");
  const auto* size = bytes.Find("size");
  if (!data || !data->IsString() || !size || !IsInputInteger(*size) ||
      size->number < 0)
    return Protocol();
  auto mime = bytes.GetString("mimeType");
  // This operation explicitly exports MP4. A login/error HTML response must
  // not become an apparently successful video; the media decoder still owns
  // validation of the actual bytes after transport succeeds.
  if (mime.empty() || mime == "application/octet-stream") mime = "video/mp4";
  if (mime != "video/mp4") return Protocol();
  content->array.push_back(
      Resource("q4t-provider://googledrive-operations/" + ProviderEscape(name),
               mime, data->str));
  Set(value, "kind", String("file"));
  Set(value, "done", Boolean(true));
  Set(value, "content_mime_type", String(mime));
  Set(value, "content_bytes", *size);
  // Download URIs and raw operation metadata stay inside the worker. A resume
  // is identified by its operation, never falsely labelled with current file
  // metadata. No automatic polling, waiting or new export follows this call.
  return {};
}
}  // namespace q4t::server::mcp_detail

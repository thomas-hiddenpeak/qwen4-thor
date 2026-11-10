#include "q4t/server/batch_input.h"

#include <algorithm>
#include <stdexcept>

#include "object_protocol.h"
#include "q4t/server/chat_storage.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/request_json.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
}  // namespace

bool ValidBatchEndpoint(std::string_view endpoint) {
  for (const auto* name :
       {"/v1/responses", "/v1/chat/completions", "/v1/embeddings",
        "/v1/completions", "/v1/moderations", "/v1/images/generations",
        "/v1/images/edits", "/v1/videos"})
    if (endpoint == name) return true;
  return false;
}

ObjectReply ParseBatchCreate(const Json& request, BatchCreate* result) {
  if (!result) return Error(500, "batch_options_invalid", "Missing output");
  std::string param;
  auto status = InputFields(request,
                            {"input_file_id", "endpoint", "completion_window",
                             "metadata", "output_expires_after"},
                            &param);
  if (!status) return Invalid(status.message(), param);
  BatchCreate parsed;
  const auto* file = request.Find("input_file_id");
  if (!file || !file->IsString() || !ValidApplicationId(file->str))
    return Invalid("input_file_id requires a file ID", "input_file_id");
  parsed.input_file_id = file->str;
  parsed.endpoint = request.GetString("endpoint");
  if (!ValidBatchEndpoint(parsed.endpoint))
    return Invalid("Invalid batch endpoint", "endpoint");
  if (request.GetString("completion_window") != "24h")
    return Invalid("completion_window must be 24h", "completion_window");
  if (const auto* metadata = request.Find("metadata")) {
    status = ValidateChatMetadata(*metadata, &param);
    if (!status) return Invalid(status.message(), param);
    parsed.metadata = *metadata;
  }
  if (const auto* expiry = request.Find("output_expires_after")) {
    status = InputFields(*expiry, {"anchor", "seconds"}, &param,
                         "output_expires_after");
    if (!status) return Invalid(status.message(), param);
    if (expiry->GetString("anchor") != "created_at")
      return Invalid("Output file expiration must anchor to created_at",
                     "output_expires_after.anchor");
    const auto* seconds = expiry->Find("seconds");
    if (!seconds || !IsInputInteger(*seconds) || seconds->AsInt() < 3600 ||
        seconds->AsInt() > 30 * 24 * 3600)
      return Invalid("Output file expiration requires 3600..2592000 seconds",
                     "output_expires_after.seconds");
    parsed.output_ttl_seconds = seconds->AsInt();
  }
  *result = std::move(parsed);
  return {};
}

BatchInput::BatchInput(std::string endpoint, Check check, Stage stage,
                       BatchInputLimits limits, std::function<bool()> cancelled)
    : endpoint_(std::move(endpoint)),
      check_(std::move(check)),
      stage_(std::move(stage)),
      limits_(limits),
      cancelled_(std::move(cancelled)) {
  if (!ValidBatchEndpoint(endpoint_) || !check_ || !stage_ ||
      !limits.max_bytes || limits.max_bytes > kBatchInputBytes ||
      !limits.max_requests || limits.max_requests > kBatchRequests ||
      !limits.max_line_bytes || limits.max_line_bytes > kChatBodyBytes)
    throw std::invalid_argument("Invalid batch input validation limits");
}

ObjectReply BatchInput::Fail(ObjectReply reply) {
  if (result_.ok()) {
    if (!reply.value.IsObject()) reply.value = Object();
    Set(&reply.value, "line", Number(rows_ + 1));
    result_ = std::move(reply);
  }
  return result_;
}

ObjectReply BatchInput::Interrupted() {
  if (!result_.ok()) return result_;
  if (cancelled_ && cancelled_())
    return Fail(Error(499, "batch_cancelled", "Batch validation cancelled"));
  return {};
}

ObjectReply BatchInput::Line(bool newline) {
  if (rows_ >= limits_.max_requests)
    return Fail(Invalid("Batch request limit exceeded", "input_file_id"));
  BatchInputRow row;
  row.line = rows_ + 1;
  row.offset = offset_;
  row.wire_bytes = buffer_.size() + (newline ? 1 : 0);
  if (!buffer_.empty() && buffer_.back() == '\r') buffer_.pop_back();
  Json value;
  auto status = io::ParseJson(buffer_, &value, true, kApplicationJsonLimits);
  if (!status) return Fail(Invalid(status.message(), "input_file_id"));
  std::string param;
  status = InputFields(value, {"custom_id", "method", "url", "body"}, &param);
  if (!status) return Fail(Invalid(status.message(), param));
  const auto* id = value.Find("custom_id");
  if (!id || !id->IsString() || id->str.empty())
    return Fail(Invalid("custom_id requires a nonempty string", "custom_id"));
  if (value.GetString("method") != "POST")
    return Fail(Invalid("Batch request method must be POST", "method"));
  if (value.GetString("url") != endpoint_)
    return Fail(Invalid("Request URL must equal the batch endpoint", "url"));
  const auto* body = value.Find("body");
  if (!body || !body->IsObject())
    return Fail(Invalid("Batch request body must be an object", "body"));
  if (const auto* stream = body->Find("stream");
      stream && !stream->IsNull() && (!stream->IsBool() || stream->boolean))
    return Fail(
        Invalid("Batch does not stream individual responses", "body.stream"));
  if (!ids_.insert(Digest(id->str)).second)
    return Fail(Invalid("Duplicate custom_id in batch", "custom_id"));
  row.custom_id = id->str;
  // Moving the actual body preserves exact numeric spellings and arbitrary
  // nested endpoint fields. It never serializes through binary64.
  row.body = std::move(Field(value, "body"));
  BatchRowCheck checked;
  auto reply = check_(row, &checked);
  if (!reply.ok()) return Fail(std::move(reply));
  if (checked.canonical_model.empty() ||
      (endpoint_ != "/v1/embeddings" && checked.embedding_inputs))
    return Fail(Error(500, "batch_validator_invalid",
                      "Endpoint validator omitted its model contract"));
  if (!model_.empty() && model_ != checked.canonical_model)
    return Fail(
        Invalid("Batch requests must use a single model", "body.model"));
  if (checked.embedding_inputs > kBatchRequests - embedding_inputs_)
    return Fail(Invalid("Batch embedding input limit exceeded", "body.input"));
  reply = Interrupted();
  if (!reply.ok()) return reply;
  const auto wire_bytes = row.wire_bytes;
  reply = stage_(std::move(row));
  if (!reply.ok()) return Fail(std::move(reply));
  model_ = std::move(checked.canonical_model);
  embedding_inputs_ += checked.embedding_inputs;
  ++rows_;
  offset_ += wire_bytes;
  buffer_.clear();
  return {};
}

ObjectReply BatchInput::Feed(std::string_view bytes) {
  if (!result_.ok()) return result_;
  if (finished_)
    return Fail(Invalid("Batch input was already finished", "input_file_id"));
  auto reply = Interrupted();
  if (!reply.ok()) return reply;
  if (bytes.size() > limits_.max_bytes - bytes_)
    return Fail(
        Invalid("Batch input file byte limit exceeded", "input_file_id"));
  bytes_ += bytes.size();
  while (!bytes.empty()) {
    const auto end = bytes.find('\n');
    const auto length = end == bytes.npos ? bytes.size() : end;
    if (length > limits_.max_line_bytes - buffer_.size())
      return Fail(Invalid("Batch request line byte limit exceeded", "body"));
    buffer_.append(bytes.substr(0, length));
    bytes.remove_prefix(length);
    if (end == bytes.npos) break;
    bytes.remove_prefix(1);
    reply = Interrupted();
    if (reply.ok()) reply = Line(true);
    if (!reply.ok()) return reply;
  }
  return {};
}

ObjectReply BatchInput::Finish() {
  if (!result_.ok()) return result_;
  if (finished_)
    return Fail(Invalid("Batch input was already finished", "input_file_id"));
  auto reply = Interrupted();
  if (!reply.ok()) return reply;
  if (!buffer_.empty()) reply = Line(false);
  if (!reply.ok()) return reply;
  if (!rows_)
    return Fail(Invalid("Batch input file is empty", "input_file_id"));
  finished_ = true;
  return {};
}
}  // namespace q4t::server

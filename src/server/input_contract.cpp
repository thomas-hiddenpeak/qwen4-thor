#include "q4t/server/input_contract.h"

#include <algorithm>
#include <cmath>
#include <unordered_set>

#include "q4t/server/output_schema.h"

namespace q4t::server {

bool IsInputInteger(const io::Json& value) {
  return value.IsNumber() && std::isfinite(value.number) &&
         std::trunc(value.number) == value.number &&
         (value.str.empty() || IsExactJsonInteger(value.str));
}

bool ValidInputName(std::string_view name) {
  return !name.empty() && name.size() <= 256 &&
         std::none_of(name.begin(), name.end(), [](unsigned char c) {
           return c < 0x20 || c == 0x7f || c == '<' || c == '>';
         });
}

bool ValidRequestId(std::string_view id) {
  return !id.empty() && id.size() <= 128 && !id.starts_with("chatcmpl-auto-") &&
         std::all_of(id.begin(), id.end(), [](unsigned char c) {
           return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '-' || c == '_';
         });
}

bool ValidCancelToken(std::string_view token) {
  return token.size() == 64 &&
         std::all_of(token.begin(), token.end(), [](unsigned char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

Status InputFields(const io::Json& object,
                   std::initializer_list<std::string_view> fields) {
  if (!object.IsObject()) return Status::Fail("expected JSON object");
  for (const auto& [key, value] : object.object) {
    (void)value;
    if (std::find(fields.begin(), fields.end(), key) == fields.end())
      return Status::Fail("unsupported field: " + key);
  }
  return Status();
}

Status ValidateToolDefinitions(const io::Json& request) {
  const auto* tools = request.Find("tools");
  if (!tools || tools->IsNull()) return Status();
  if (!tools->IsArray()) return Status::Fail("tools must be an array or null");
  std::unordered_set<std::string> names;
  for (const auto& tool : tools->array) {
    auto status = InputFields(tool, {"type", "function"});
    if (!status) return status;
    const auto* fn = tool.Find("function");
    if (tool.GetString("type") != "function" || !fn)
      return Status::Fail("tools require type=function and function");
    status = InputFields(*fn, {"name", "description", "parameters", "strict"});
    if (!status) return status;
    const auto name = fn->GetString("name");
    if (!ValidInputName(name) || !names.insert(name).second)
      return Status::Fail("invalid or duplicate function name");
    if (const auto* description = fn->Find("description");
        description && !description->IsString())
      return Status::Fail("function.description must be a string");
    if (const auto* strict = fn->Find("strict");
        strict && !strict->IsNull() && !strict->IsBool())
      return Status::Fail("function.strict must be boolean or null");
    if (const auto* parameters = fn->Find("parameters")) {
      if (!parameters->IsObject())
        return Status::Fail("function.parameters must be an object");
      if (const auto* properties = parameters->Find("properties");
          properties && !properties->IsObject())
        return Status::Fail("function properties must be an object");
      // Ordinary auto/raw schemas remain advisory. The finite schema profile
      // is checked separately whenever a decoding guarantee is requested.
    }
  }
  return Status();
}

Status ValidateRequestCredentials(const io::Json& request) {
  const auto* id = request.Find("request_id");
  const auto* token = request.Find("cancel_token");
  if ((id && (!id->IsString() || !ValidRequestId(id->str) || !token ||
              !token->IsString() || !ValidCancelToken(token->str))) ||
      (!id && token))
    return Status::Fail("request_id requires a fresh 64-hex cancel_token");
  return Status();
}

Status ValidateCancelContract(const io::Json& request) {
  auto status = InputFields(request, {"request_id", "cancel_token"});
  if (!status) return status;
  if (!request.Has("request_id"))
    return Status::Fail("cancellation requires request_id");
  return ValidateRequestCredentials(request);
}

}  // namespace q4t::server

#include "q4t/server/voice_api.h"

#include "object_protocol.h"
#include "q4t/server/application_query.h"
#include "q4t/server/application_routes.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/voice_verification.h"

namespace q4t::server {
namespace {
using namespace object_detail;
ObjectReply Invalid(std::string message, std::string param) {
  auto reply = Error(400, "invalid_request", std::move(message));
  reply.value = Object({{"param", String(std::move(param))}});
  return reply;
}
ObjectReply Parsed(const Status& status, const ChatPlanError& error) {
  auto reply = Error(error.http_status, error.code, status.message());
  reply.value = Object({{"param", String(error.param)}});
  return reply;
}
}  // namespace

ObjectReply HandleVoiceRequest(ObjectStore& objects, std::string_view method,
                               std::string_view target,
                               std::string_view content_type,
                               std::string_view body,
                               const BinaryAccess& access,
                               const VoiceVerifier& verify) {
  if (access.scope.empty())
    return Error(503, "object_scope_unavailable", "Voice owner unavailable");
  if (!IsVoiceTarget(target) || target.find('#') != target.npos)
    return Error(404, "route_not_found", "Voice route not found");
  const auto separator = target.find('?');
  const auto path = target.substr(0, separator);
  const bool consent = path.starts_with("/v1/audio/voice_consents");
  const std::string_view root =
      consent ? "/v1/audio/voice_consents" : "/v1/audio/voices";
  const bool collection = path == root;
  std::string id;
  if (!collection) {
    id = path.substr(root.size() + 1);
    if (!ValidApplicationId(id)) return Invalid("Invalid voice identity", "id");
  }
  if (!VoiceMethodAllowed(method, target)) {
    auto reply = Error(405, "method_not_allowed", "Method not allowed");
    reply.value = Object(
        {{"param", String("method")},
         {"allow",
          String(consent ? (collection ? "GET, POST" : "GET, POST, DELETE")
                         : (collection ? "POST" : "DELETE"))}});
    return reply;
  }
  if (method != "POST" && !body.empty())
    return Invalid("Request body is not allowed on this operation", "body");
  auto query = ParseApplicationQuery(
      separator == target.npos ? "" : target.substr(separator + 1));
  if (!query.ok()) return query;
  VoiceRegistry registry(objects);
  if (consent && collection && method == "GET") {
    std::string field;
    const auto fields = InputFields(query.value, {"limit", "after"}, &field);
    if (!fields) return Invalid(fields.message(), field);
    const auto limit = query.value.GetInt("limit", 20);
    if (limit < 1 || limit > 100)
      return Invalid("limit must be 1..100", "limit");
    const auto after = query.value.GetString("after");
    if (!after.empty() && !ValidApplicationId(after))
      return Invalid("Invalid consent cursor", "after");
    return registry.ListConsents(limit, after, access);
  }
  if (!query.value.object.empty() || separator != target.npos)
    return Invalid("Query is not allowed on this operation", "query");
  if (!collection) {
    if (method == "DELETE")
      return consent ? registry.DeleteConsent(id, access)
                     : registry.DeleteVoice(id, access);
    if (method == "GET") return registry.RetrieveConsent(id, access);
    std::string name;
    ChatPlanError error;
    const auto status =
        ParseVoiceConsentUpdate(content_type, body, &name, &error);
    if (!status) return Parsed(status, error);
    return registry.UpdateConsent(id, name, access);
  }
  if (!verify)
    return Error(503, "voice_verification_unavailable",
                 "Voice verifier unavailable");
  VoiceSampleEvidence evidence;
  std::string phrase;
  ChatPlanError error;
  if (consent) {
    VoiceConsentRequest request;
    const auto status =
        ParseVoiceConsentRequest(content_type, body, &request, &error);
    if (!status) return Parsed(status, error);
    if (VoiceConsentPhrase(request.language).empty())
      return Invalid("Consent language has no verification phrase", "language");
    auto reply =
        verify(request.recording, request.language, &evidence, &phrase);
    if (!reply.ok()) return reply;
    if (phrase != VoiceConsentPhrase(request.language))
      return Error(503, "voice_verification_invalid",
                   "Consent evidence mismatch");
    return registry.CreateConsent(request, evidence, std::move(phrase), access);
  }
  VoiceCreateRequest request;
  const auto status =
      ParseVoiceCreateRequest(content_type, body, &request, &error);
  if (!status) return Parsed(status, error);
  if (request.type == VoiceType::kPrompt)
    return Error(503, "voice_design_not_configured",
                 "Native voice design model is not configured");
  VoiceConsentBinding binding;
  auto reply = registry.BindConsent(request.consent, access, &binding);
  if (!reply.ok()) return reply;
  reply = verify(request.audio_sample, {}, &evidence, &phrase);
  if (!reply.ok()) return reply;
  // CAS at publication rechecks the consent after potentially long model work.
  return registry.CreateSampleVoice(request, evidence, binding, access);
}
}  // namespace q4t::server

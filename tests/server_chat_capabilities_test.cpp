#include "q4t/server/chat_capabilities.h"

#include <algorithm>
#include <string>

#include "q4t/io/json.h"
#include "q4t/server/chat_contract.h"
#include "q4t/server/chat_protocol_identity.h"
#include "q4t/test.h"

namespace {
bool Hash(const std::string& text) {
  return text.size() == 64 && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}
}  // namespace

Q4T_TEST(chat_capabilities_identity) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
                q4t::server::ChatCapabilitiesJson("m", false, false), &caps)
                .ok());
  Q4T_CHECK(caps.GetString("object") == "q4t.capabilities");
  Q4T_CHECK(caps.GetInt("schema_version") == 1);
  Q4T_CHECK(caps.GetBool("implementation_only"));
  Q4T_CHECK(!caps.GetBool("release_attestation", true));
  Q4T_CHECK(caps.GetString("protocol_source_sha256") ==
            q4t::server::identity::kProtocol);
  Q4T_CHECK(Hash(caps.GetString("protocol_source_sha256")));
  const auto* templ = caps.Find("template");
  Q4T_CHECK(templ && templ->IsObject());
  for (const auto* key : {"reference_sha256", "renderer_source_sha256",
                          "tool_history_source_sha256", "fixture_sha256"})
    Q4T_CHECK(Hash(templ->GetString(key)));
  return true;
}

Q4T_TEST(chat_capabilities_option_contract) {
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
                q4t::server::ChatCapabilitiesJson("m", false, true), &caps)
                .ok());
  const auto* tools = caps.Find("tools");
  Q4T_CHECK(tools && tools->IsObject());
  const auto* choices = tools->GetArray("tool_choice");
  Q4T_CHECK(choices && choices->array.size() == 1);
  Q4T_CHECK(choices->array[0].IsString() && choices->array[0].str == "auto");
  Q4T_CHECK(!tools->GetBool("strict", true));
  Q4T_CHECK(!tools->GetBool("constrained_decoding", true));
  for (const auto* choice : {"auto", "none", "required"}) {
    q4t::io::Json request;
    Q4T_CHECK(q4t::io::ParseJson(
                  std::string(R"({"messages":[{"role":"user","content":"q"}],)"
                              R"("tool_choice":")") +
                      choice + "\"}",
                  &request)
                  .ok());
    Q4T_CHECK(q4t::server::ValidateChatContract(request, "m", false).ok() ==
              (std::string(choice) == "auto"));
  }
  return true;
}

Q4T_TEST(chat_capabilities_runtime_flags) {
  for (const bool media : {false, true}) {
    for (const bool mtp : {false, true}) {
      q4t::io::Json caps;
      Q4T_CHECK(q4t::io::ParseJson(
                    q4t::server::ChatCapabilitiesJson("m", media, mtp), &caps)
                    .ok());
      const auto* runtime = caps.Find("runtime");
      Q4T_CHECK(runtime && runtime->GetBool("media_enabled") == media);
      Q4T_CHECK(runtime->GetBool("mtp_enabled") == mtp);
      const auto* cancellation = caps.Find("cancellation");
      Q4T_CHECK(cancellation &&
                !cancellation->GetBool("acceptance_is_drained", true) &&
                !cancellation->GetBool("request_replay_idempotent", true));
    }
  }
  return true;
}

Q4T_TEST(chat_capabilities_model_escaping) {
  const std::string model = "test\"\\\nmodel";
  q4t::io::Json caps;
  Q4T_CHECK(q4t::io::ParseJson(
                q4t::server::ChatCapabilitiesJson(model, false, false), &caps)
                .ok());
  Q4T_CHECK(caps.GetString("model") == model);
  return true;
}

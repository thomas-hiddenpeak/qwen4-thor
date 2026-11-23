#include "q4t/server/live_shape.h"

#include <filesystem>
#include <fstream>
#include <iterator>

#include "q4t/server/live_types_data.h"
#include "q4t/trace/sha256.h"
#include "q4t/test.h"

Q4T_TEST(live_shape_matches_actual_sdk_type_oracle) {
  using namespace q4t::server;
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/live/sdk_vectors.json";
  std::ifstream input(path);
  Q4T_CHECK(input.good());
  const std::string text{std::istreambuf_iterator<char>(input), {}};
  q4t::io::Json document;
  Q4T_CHECK(q4t::io::ParseJson(text, &document));
  Q4T_CHECK(document.GetString("profile") == "q4t-sdk-live-shape-oracle-v1");
  Q4T_CHECK(document.GetString("validation_mode") ==
            "concrete-sdk-union-members");
  const auto data = live_types::kData;
  const auto hash = q4t::trace::HexDigest(q4t::trace::Sha256(
      {reinterpret_cast<const uint8_t*>(data.data()), data.size()}));
  Q4T_CHECK(document.GetString("graph_sha256") == hash);
  const auto* vectors = document.GetArray("vectors");
  Q4T_CHECK(vectors && vectors->array.size() == 808);
  size_t differences = 0;
  for (const auto& vector : vectors->array) {
    const auto shape = vector.GetString("shape");
    auto kind = LiveShape::kClientEvent;
    if (shape == "client_event")
      kind = LiveShape::kClientEvent;
    else if (shape == "server_event")
      kind = LiveShape::kServerEvent;
    else if (shape == "session_start")
      kind = LiveShape::kSessionStart;
    else if (shape == "session_update")
      kind = LiveShape::kSessionUpdate;
    else if (shape == "session")
      kind = LiveShape::kSession;
    else if (shape == "media_create")
      kind = LiveShape::kMediaCreate;
    else if (shape == "media_created")
      kind = LiveShape::kMediaCreated;
    else if (shape == "sideband_client")
      kind = LiveShape::kSidebandClient;
    else if (shape == "sideband_server")
      kind = LiveShape::kSidebandServer;
    else if (shape == "fork_client")
      kind = LiveShape::kForkClient;
    else if (shape == "fork_server")
      kind = LiveShape::kForkServer;
    else if (shape == "session_accept")
      kind = LiveShape::kSessionAccept;
    else if (shape == "session_fork")
      kind = LiveShape::kSessionFork;
    else if (shape == "session_forked")
      kind = LiveShape::kSessionForked;
    else if (shape == "session_refer")
      kind = LiveShape::kSessionRefer;
    else if (shape == "session_reject")
      kind = LiveShape::kSessionReject;
    else
      Q4T_CHECK(false);
    const auto* value = vector.Find("input");
    Q4T_CHECK(value);
    ChatPlanError error;
    const auto status = ValidateLiveShape(*value, kind, &error);
    if (status.ok() != vector.GetBool("accepted")) {
      ++differences;
      std::fprintf(stderr, "Live SDK difference: %s (%s)\n",
                   vector.GetString("name").c_str(), status.message().c_str());
    }
  }
  std::printf("Live SDK vectors: %zu, differences: %zu\n",
              vectors->array.size(), differences);
  Q4T_CHECK(differences == 0);
  return true;
}

Q4T_TEST(live_shape_duplicate_keys_and_bounded_error_path) {
  using namespace q4t::server;
  q4t::io::Json value;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"type":"session.close","event_id":"event_1"})", &value));
  ChatPlanError error;
  Q4T_CHECK(ValidateLiveShape(value, LiveShape::kClientEvent, &error));
  value.object.push_back(value.object.back());
  Q4T_CHECK(!ValidateLiveShape(value, LiveShape::kClientEvent, &error));
  value.object.back().first = std::string(10000, 'x');
  Q4T_CHECK(!ValidateLiveShape(value, LiveShape::kClientEvent, &error));
  Q4T_CHECK(error.param.size() < 128);
  Q4T_CHECK(!ValidateLiveShape(value, LiveShape::kClientEvent, nullptr));
  return true;
}

Q4T_TEST(live_shape_large_owned_string_rejected_before_schema_walk) {
  using namespace q4t::server;
  q4t::io::Json value;
  value.type = q4t::io::Json::Type::kString;
  value.str.assign(16ULL << 20, 'x');
  ChatPlanError error;
  Q4T_CHECK(!ValidateLiveShape(value, LiveShape::kClientEvent, &error));
  Q4T_CHECK(error.code == "item_validation_limit_exceeded");
  Q4T_CHECK(error.http_status == 503);
  return true;
}

Q4T_TEST(live_shape_invalid_root_does_not_accept_an_empty_object) {
  using namespace q4t::server;
  q4t::io::Json value;
  Q4T_CHECK(q4t::io::ParseJson("{}", &value));
  ChatPlanError error;
  Q4T_CHECK(!ValidateLiveShape(value, static_cast<LiveShape>(1000), &error));
  Q4T_CHECK(error.code == "item_contract_unavailable");
  return true;
}

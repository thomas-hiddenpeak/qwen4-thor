#include "q4t/server/realtime_shape.h"

#include <filesystem>
#include <fstream>
#include <iterator>

#include "q4t/server/realtime_types_data.h"
#include "q4t/trace/sha256.h"
#include "q4t/test.h"

Q4T_TEST(realtime_shape_matches_actual_sdk_type_oracle) {
  using namespace q4t::server;
  const auto path = std::filesystem::path(__FILE__).parent_path() /
                    "fixtures/realtime/sdk_vectors.json";
  std::ifstream input(path);
  Q4T_CHECK(input.good());
  const std::string text{std::istreambuf_iterator<char>(input), {}};
  q4t::io::Json document;
  Q4T_CHECK(q4t::io::ParseJson(text, &document));
  Q4T_CHECK(document.GetString("profile") ==
            "q4t-sdk-realtime-shape-oracle-v1");
  Q4T_CHECK(document.GetString("validation_mode") ==
            "concrete-sdk-union-members");
  const auto data = realtime_types::kData;
  const auto hash = q4t::trace::HexDigest(q4t::trace::Sha256(
      {reinterpret_cast<const uint8_t*>(data.data()), data.size()}));
  Q4T_CHECK(document.GetString("graph_sha256") == hash);
  const auto* vectors = document.GetArray("vectors");
  Q4T_CHECK(vectors && vectors->array.size() >= 300);
  size_t differences = 0;
  for (const auto& vector : vectors->array) {
    const auto shape = vector.GetString("shape");
    const auto kind = shape == "conversation_item" ? RealtimeShape::kItem
                      : shape == "client_event"    ? RealtimeShape::kClientEvent
                      : shape == "server_event"    ? RealtimeShape::kServerEvent
                      : shape == "response_create"
                          ? RealtimeShape::kResponseCreate
                      : shape == "session" ? RealtimeShape::kSession
                                           : RealtimeShape::kResponse;
    const auto* value = vector.Find("input");
    Q4T_CHECK(value);
    ChatPlanError error;
    const auto status = ValidateRealtimeShape(*value, kind, &error);
    if (status.ok() != vector.GetBool("accepted")) {
      ++differences;
      std::fprintf(stderr, "Realtime SDK difference: %s (%s)\n",
                   vector.GetString("name").c_str(), status.message().c_str());
    }
  }
  std::printf("Realtime SDK vectors: %zu, differences: %zu\n",
              vectors->array.size(), differences);
  Q4T_CHECK(differences == 0);
  return true;
}

Q4T_TEST(realtime_shape_duplicate_keys_and_bounded_error_path) {
  using namespace q4t::server;
  q4t::io::Json value;
  Q4T_CHECK(q4t::io::ParseJson(
      R"({"type":"function_call_output","call_id":"c","output":""})", &value));
  ChatPlanError error;
  Q4T_CHECK(ValidateRealtimeShape(value, RealtimeShape::kItem, &error));
  value.object.push_back(value.object.back());
  Q4T_CHECK(!ValidateRealtimeShape(value, RealtimeShape::kItem, &error));
  value.object.back().first = std::string(10000, 'x');
  Q4T_CHECK(!ValidateRealtimeShape(value, RealtimeShape::kItem, &error));
  Q4T_CHECK(error.param.size() < 128);
  Q4T_CHECK(!ValidateRealtimeShape(value, RealtimeShape::kItem, nullptr));
  return true;
}

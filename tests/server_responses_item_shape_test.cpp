#include "q4t/server/responses_item_shape.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>

#include "q4t/server/responses_item_types_data.h"
#include "q4t/trace/sha256.h"
#include "q4t/test.h"

Q4T_TEST(responses_item_shape_matches_actual_sdk_type_oracle) {
  namespace fs = std::filesystem;
  using namespace q4t::server;
  const char* override = std::getenv("Q4T_ITEM_SHAPE_VECTORS");
  const auto path = override ? fs::path(override)
                             : fs::path(__FILE__).parent_path() /
                                   "fixtures/responses_items/sdk_vectors.json";
  std::ifstream input(path);
  Q4T_CHECK(input.good());
  const std::string text{std::istreambuf_iterator<char>(input), {}};
  q4t::io::Json document;
  Q4T_CHECK(q4t::io::ParseJson(text, &document));
  Q4T_CHECK(document.GetString("profile") == "q4t-sdk-item-shape-oracle-v1");
  Q4T_CHECK(document.GetString("validation_mode") ==
            "concrete-sdk-union-members");
  const auto& data = item_types::kData;
  const auto hash = q4t::trace::HexDigest(q4t::trace::Sha256(
      {reinterpret_cast<const uint8_t*>(data.data()), data.size()}));
  Q4T_CHECK(document.GetString("graph_sha256") == hash);
  const auto* vectors = document.GetArray("vectors");
  Q4T_CHECK(vectors && vectors->array.size() >= 600);
  size_t passed = 0, differences = 0;
  std::ofstream report;
  if (const auto* root = std::getenv("Q4T_TEST_OUTPUT_DIR"))
    report.open(fs::path(root) / "item-shape-differential.log");
  for (const auto& vector : vectors->array) {
    const auto shape = vector.GetString("shape");
    const auto kind = shape == "input_item" ? ResponsesItemShape::kInput
                      : shape == "output_item"
                          ? ResponsesItemShape::kOutput
                          : ResponsesItemShape::kConversation;
    ChatPlanError error;
    const auto* value = vector.Find("input");
    Q4T_CHECK(value != nullptr);
    const auto status = ValidateResponsesItemShape(*value, kind, &error);
    const bool equal = status.ok() == vector.GetBool("accepted");
    if (equal)
      ++passed;
    else {
      ++differences;
      std::fprintf(stderr,
                   "SDK/native mismatch: %s accepted=%d native=%d %s %s\n",
                   vector.GetString("name").c_str(), vector.GetBool("accepted"),
                   status.ok(), error.param.c_str(), status.message().c_str());
    }
    if (report)
      report << vector.GetString("name") << ' ' << equal << ' '
             << vector.GetBool("accepted") << ' ' << status.ok() << ' '
             << error.code << ' ' << error.param << '\n';
  }
  std::printf("SDK/native item vectors: %zu matched, %zu differed\n", passed,
              differences);
  Q4T_CHECK(differences == 0);
  return true;
}

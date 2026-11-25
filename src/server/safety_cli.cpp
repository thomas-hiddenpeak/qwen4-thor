#include "q4t/server/safety_records.h"

#include <array>
#include <cstdio>

#include "object_protocol.h"
#include "q4t/server/client_identity.h"
#include "q4t/server/state_key_admin.h"

namespace q4t::server {
int MaybeRunSafetyCommand(int argc, char** argv) {
  using namespace object_detail;
  if (argc < 2 || std::string_view(argv[1]) != "safety") return -1;
  if (argc == 3 && std::string_view(argv[2]) == "--help") {
    std::puts(
        "q4t safety publish --object-store-dir ABS --client-id ID\n"
        "Read the approved resource envelope from stdin (up to 4 KiB).");
    return 0;
  }
  if (argc != 7 || std::string_view(argv[2]) != "publish") return 2;
  std::string directory, client;
  for (int i = 3; i < argc; i += 2) {
    const auto option = std::string_view(argv[i]);
    if (option == "--object-store-dir" && directory.empty())
      directory = argv[i + 1];
    else if (option == "--client-id" && client.empty())
      client = argv[i + 1];
    else
      return 2;
  }
  if (StateKeyAdminAddress(directory).empty() || !ValidClientId(client))
    return 2;
  std::array<char, 4097> input;
  const auto size = std::fread(input.data(), 1, input.size(), stdin);
  if (std::ferror(stdin) || !size || size == input.size()) return 2;
  Json envelope;
  io::JsonParseLimits limits;
  limits.max_depth = 8;
  limits.max_values = 64;
  limits.max_string_bytes = 4096;
  limits.reject_duplicate_keys = true;
  if (!io::ParseJson(std::string(input.data(), size), &envelope, true,
                     limits) ||
      !envelope.IsObject() || !envelope.Has("resource"))
    return 2;
  for (const auto& [key, _] : envelope.object)
    if (key != "resource" && key != "explanation_expires_at" &&
        key != "record_expires_at")
      return 2;
  Set(&envelope, "version", Number(1));
  Set(&envelope, "action", String("safety_publish"));
  Set(&envelope, "client_id", String(client));
  if (OutputJson(envelope).size() > 4096) return 2;
  auto result = ExchangeStateKeyCommand(
      directory, envelope,
      std::chrono::steady_clock::now() + std::chrono::seconds(5));
  if (result.commit_unknown) {
    result.message =
        "Publication receipt unavailable; retrieve the reviewed safety ID";
    result.value =
        Object({{"id", String(envelope.Find("resource")->GetString("id"))}});
  }
  std::puts(OutputJson(WireReply(result)).c_str());
  return result.ok() ? 0 : 1;
}
}  // namespace q4t::server

#include "q4t/server/state_key_admin.h"

#include <charconv>
#include <cstdio>
#include <stdexcept>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
void Usage() {
  std::printf(
      "Usage: q4t state-keys <inspect|initialize|rotate>\n"
      "  --object-store-dir ABSOLUTE_DIRECTORY [--offline]\n"
      "  rotate: --expected-revision N [--retire KEY_ID ...]\n"
      "Live commands use a same-UID Unix socket; no model restart.\n"
      "--offline requires exclusive ownership of the stopped object store.\n"
      "Inspect keys after an unknown receipt; never blindly retry rotation.\n");
}
}  // namespace

int MaybeRunStateKeyCommand(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "state-keys") return -1;
  if (argc == 3 && std::string_view(argv[2]) == "--help") {
    Usage();
    return 0;
  }
  bool dispatched = false;
  try {
    const std::string action = argc > 2 ? argv[2] : "";
    if (action != "inspect" && action != "initialize" && action != "rotate") {
      Usage();
      return 2;
    }
    std::string directory;
    bool offline = false;
    int64_t revision = 0;
    auto retired = Array();
    for (int i = 3; i < argc; ++i) {
      const std::string_view name(argv[i]);
      if (name == "--offline" && !offline) {
        offline = true;
      } else if (name == "--object-store-dir" && directory.empty() &&
                 i + 1 < argc) {
        directory = argv[++i];
      } else if (name == "--expected-revision" && !revision && i + 1 < argc) {
        const std::string_view value(argv[++i]);
        const auto parsed = std::from_chars(
            value.data(), value.data() + value.size(), revision);
        if (parsed.ec != std::errc{} ||
            parsed.ptr != value.data() + value.size() || revision <= 0 ||
            revision > (1LL << 53) - 1)
          throw std::runtime_error("invalid revision");
      } else if (name == "--retire" && i + 1 < argc &&
                 retired.array.size() < StateCipher::kMaxKeys) {
        retired.array.push_back(String(argv[++i]));
      } else {
        throw std::runtime_error("invalid key command option");
      }
    }
    if (StateKeyAdminAddress(directory).empty() ||
        (action == "rotate" ? !revision : (revision || !retired.array.empty())))
      throw std::runtime_error("invalid key command configuration");
    auto request = Object({{"version", Number(1)}, {"action", String(action)}});
    if (action == "rotate") {
      Set(&request, "expected_revision", Number(revision));
      Set(&request, "retire", std::move(retired));
    }
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(5);
    ObjectReply reply;
    dispatched = true;
    if (offline) {
      ObjectStoreOptions options;
      options.directory = directory;
      ObjectStore objects(options);
      reply = HandleStateKeyCommand(objects, request,
                                    {"local:key-admin", deadline, {}});
    } else {
      reply = ExchangeStateKeyCommand(directory, request, deadline);
    }
    const auto text = OutputJson(WireReply(reply));
    std::printf("%s\n", text.c_str());
    return reply.ok() ? 0 : 1;
  } catch (...) {
    std::fprintf(stderr, "%s\n",
                 dispatched
                     ? "{\"code\":\"state_key_command_receipt_unavailable\","
                       "\"action\":\"inspect_before_retry\"}"
                     : "{\"code\":\"state_key_command_invalid\"}");
    return dispatched ? 1 : 2;
  }
}
}  // namespace q4t::server

#include "q4t/server/state_key_admin.h"

#include <charconv>
#include <algorithm>
#include <cstdio>
#include <stdexcept>

#include "object_protocol.h"

namespace q4t::server {
namespace {
using namespace object_detail;
void Usage() {
  std::printf(
      "Usage: q4t state-retention <inspect|collect>\n"
      "  --object-store-dir ABSOLUTE_DIRECTORY [--offline]\n"
      "  [--item-created-before UNIX_SECONDS] [--limit 1..128]\n"
      "  collect: --expected-plan SHA256\n"
      "Default: expired objects only; active tasks and references protected.\n"
      "Item cutoff explicitly selects unreferenced permanent items created\n"
      "before that time; it is not the time they became unreferenced.\n"
      "Inspect then collect the exact plan; changed state returns conflict.\n"
      "Live commands use a same-UID Unix socket; no model restart.\n"
      "--offline requires exclusive ownership of the stopped object store.\n"
      "Unknown receipt: inspect again; do not blindly retry collection.\n");
}
int64_t Integer(std::string_view text, int64_t maximum) {
  int64_t value = 0;
  const auto parsed =
      std::from_chars(text.data(), text.data() + text.size(), value);
  if (parsed.ec != std::errc{} || parsed.ptr != text.data() + text.size() ||
      value <= 0 || value > maximum)
    throw std::runtime_error("invalid retention integer");
  return value;
}
}  // namespace

int MaybeRunStateRetentionCommand(int argc, char** argv) {
  if (argc < 2 || std::string_view(argv[1]) != "state-retention") return -1;
  if (argc == 3 && std::string_view(argv[2]) == "--help") {
    Usage();
    return 0;
  }
  bool dispatched = false;
  try {
    const std::string action = argc > 2 ? argv[2] : "";
    if (action != "inspect" && action != "collect") {
      Usage();
      return 2;
    }
    std::string directory, expected;
    bool offline = false, limit_set = false;
    int64_t cutoff = 0, limit = 32;
    for (int i = 3; i < argc; ++i) {
      const std::string_view name(argv[i]);
      if (name == "--offline" && !offline) {
        offline = true;
      } else if (name == "--object-store-dir" && directory.empty() &&
                 i + 1 < argc) {
        directory = argv[++i];
      } else if (name == "--expected-plan" && expected.empty() &&
                 i + 1 < argc) {
        expected = argv[++i];
      } else if (name == "--item-created-before" && !cutoff && i + 1 < argc) {
        cutoff = Integer(argv[++i], (1LL << 53) - 1);
      } else if (name == "--limit" && !limit_set && i + 1 < argc) {
        limit = Integer(argv[++i], 128);
        limit_set = true;
      } else {
        throw std::runtime_error("invalid retention option");
      }
    }
    if (StateKeyAdminAddress(directory).empty() ||
        (action == "collect" ? (expected.size() != 64 ||
                                !std::all_of(expected.begin(), expected.end(),
                                             [](char c) {
                                               return (c >= '0' && c <= '9') ||
                                                      (c >= 'a' && c <= 'f');
                                             }))
                             : !expected.empty()))
      throw std::runtime_error("invalid retention configuration");
    auto request = Object({{"version", Number(1)},
                           {"action", String("retention_" + action)},
                           {"item_created_before", Number(cutoff)},
                           {"limit", Number(limit)}});
    if (!expected.empty()) Set(&request, "expected_plan", String(expected));
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(5);
    ObjectReply reply;
    dispatched = true;
    if (offline) {
      ObjectStoreOptions options;
      options.directory = directory;
      ObjectStore objects(options);
      reply = HandleStateKeyCommand(objects, request,
                                    {"local:retention-admin", deadline, {}});
    } else {
      reply = ExchangeStateKeyCommand(directory, request, deadline);
      if (reply.code == "state_key_admin_unavailable") {
        reply.code = "state_retention_admin_unavailable";
        reply.message = reply.commit_unknown
                            ? "Retention receipt unavailable; inspect again"
                            : "Local retention administration is unavailable";
      }
    }
    std::printf("%s\n", OutputJson(WireReply(reply)).c_str());
    return reply.ok() ? 0 : 1;
  } catch (...) {
    std::fprintf(stderr, "%s\n",
                 dispatched
                     ? "{\"code\":\"state_retention_receipt_unavailable\","
                       "\"action\":\"inspect_before_retry\"}"
                     : "{\"code\":\"state_retention_command_invalid\"}");
    return dispatched ? 1 : 2;
  }
}
}  // namespace q4t::server

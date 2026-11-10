#include "q4t/server/file_input_policy.h"

#include <sys/stat.h>
#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "../src/server/object_protocol.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace q4t::server::object_detail;
namespace fs = std::filesystem;
constexpr const char* kPolicy = R"({"version":1,
  "max_source_bytes":1000000,"max_files":4,"max_rendered_bytes":2000000,
  "max_retained_bytes":3000000,"hold_seconds":120,
  "summary_output_tokens":128,"total_summary_output_tokens":256,
  "extraction_timeout_ms":5000,
  "fetch":{"public_networks":false,"allow_http":false,
    "allowed_hosts":["private.example"],"allowed_networks":["10.2.0.0/16"],
    "ca_file":"/private/trust.pem","ports":[443],"timeout_ms":1500,
    "max_redirects":0},
  "document":{"max_text_bytes":100000,"max_cell_bytes":10000,
    "max_columns":32,"max_cells":1000,"max_sheets":2,
    "max_nested_documents":0,"max_embedded_documents":4,"timeout_ms":2000},
  "package":{"max_bytes":2000000,"max_entry_bytes":1000000,
    "max_entries":100,"max_xml_nodes":1000,"max_xml_depth":20},
  "pdf":{"max_pages":4,"max_pixels":1000000,"max_image_bytes":1000000,
    "timeout_ms":4000},
  "legacy":{"max_converted_bytes":1000000,"timeout_ms":4000},
  "legacy_io":{"write_bytes":2000000,"file_creations":30,
    "thread_creations":0,"notifications":5000},
  "image":{"max_bytes":1000000,"max_pixels":1000000,"max_edge":1024,
    "max_conversion_bytes":4000000,"codec_timeout_ms":1500},
  "storage":{"max_bytes":6000000,"max_metadata_bytes":1000000,
    "max_parts":20}})";
struct Fixture {
  fs::path root;
  Fixture() {
    const char* output = std::getenv("Q4T_TEST_OUTPUT_DIR");
    fs::path parent = output ? output : ".q4t-work/file-policy-tests";
    fs::create_directories(parent);
    auto pattern = fs::absolute(parent / "policy-XXXXXX").string();
    if (!mkdtemp(pattern.data())) throw std::runtime_error("policy fixture");
    root = pattern;
  }
  ~Fixture() { fs::remove_all(root); }
};
}  // namespace

Q4T_TEST(file_input_policy_defaults_and_all_preparation_domains) {
  FileInputPolicy policy;
  Q4T_CHECK(ParseFileInputPolicy(R"({"version":1})", &policy));
  Q4T_CHECK(ValidFileExpansionOptions(policy.options));
  Q4T_CHECK(policy.options.max_source_bytes == 50000000 &&
            policy.options.total_summary_output_tokens == 8192 &&
            policy.options.extraction.legacy.work_directory.empty());
  Q4T_CHECK(ParseFileInputPolicy(kPolicy, &policy));
  const auto& o = policy.options;
  Q4T_CHECK(policy.sha256 == Digest(kPolicy) && o.max_files == 4 &&
            o.max_retained_bytes == 3000000 && o.max_source_bytes == 1000000 &&
            o.summary_output_tokens == 128 &&
            o.total_summary_output_tokens == 256 && o.hold_seconds == 120);
  Q4T_CHECK(!o.fetch.public_networks && !o.fetch.allow_http &&
            o.fetch.ca_file == "/private/trust.pem" &&
            o.fetch.allowed_networks[0] == "10.2.0.0/16" &&
            o.fetch.allowed_hosts[0] == "private.example" &&
            o.fetch.ports == std::vector<int>{443});
  Q4T_CHECK(o.extraction.timeout_ms == 5000 &&
            o.extraction.document.max_cells == 1000 &&
            o.extraction.document.package.max_xml_depth == 20 &&
            o.extraction.document.max_nested_documents == 0 &&
            o.extraction.pdf.max_pages == 4 &&
            o.extraction.legacy.io.thread_creations == 0 &&
            o.image.max_edge == 1024 && o.storage.max_parts == 20);
  return true;
}

Q4T_TEST(file_input_policy_invalid_configuration_is_transactional) {
  FileInputPolicy policy;
  Q4T_CHECK(ParseFileInputPolicy(kPolicy, &policy));
  for (const auto* raw :
       {"{}",
        "null",
        "[]",
        R"({"version":1,"version":1})",
        R"({"version":1.0})",
        R"({"version":2})",
        R"({"version":1,"max_files":0})",
        R"({"version":1,"max_files":1.5})",
        R"({"version":1,"max_files":true})",
        R"({"version":1,"max_files":1e2})",
        R"({"version":1,"max_files":-1})",
        R"({"version":1,"max_source_bytes":50000001})",
        R"({"version":1,"max_retained_bytes":18446744073709551616})",
        R"({"version":1,"total_summary_output_tokens":65537})",
        R"({"version":1,"pdf":{"timeout_ms":120001}})",
        R"({"version":1,"image":{"codec_timeout_ms":0}})",
        R"({"version":1,"storage":{"max_bytes":1024}})",
        R"({"version":1,"package":null})",
        R"({"version":1,"document":{"rows":20}})",
        R"({"version":1,"legacy":{"work_directory":"/unsafe"}})",
        R"({"version":1,"fetch":{"ca_file":"relative"}})",
        R"({"version":1,"fetch":{"allowed_networks":["invalid"]}})",
        R"({"version":1,"fetch":{"ports":[443,443]}})",
        R"({"version":1,"fetch":{"proxy":"http://proxy"}})",
        R"({"version":1,"arbitrary":true})"}) {
    Q4T_CHECK(!ParseFileInputPolicy(raw, &policy));
    Q4T_CHECK(policy.sha256 == Digest(kPolicy) &&
              policy.options.max_files == 4);
  }
  Q4T_CHECK(!ParseFileInputPolicy(std::string(65537, ' '), &policy));
  Q4T_CHECK(!ParseFileInputPolicy(kPolicy, nullptr));
  return true;
}

Q4T_TEST(
    file_input_policy_private_snapshot_rejects_links_devices_and_mutability) {
  Fixture fixture;
  auto path = fixture.root / "policy.json";
  { std::ofstream(path) << kPolicy; }
  Q4T_CHECK(::chmod(path.c_str(), 0600) == 0);
  FileInputPolicy policy;
  Q4T_CHECK(LoadFileInputPolicy(path.string(), &policy));
  fs::create_symlink(path, fixture.root / "link");
  Q4T_CHECK(!LoadFileInputPolicy((fixture.root / "link").string(), &policy));
  fs::create_hard_link(path, fixture.root / "hard");
  Q4T_CHECK(!LoadFileInputPolicy(path.string(), &policy));
  fs::remove(fixture.root / "hard");
  Q4T_CHECK(::chmod(path.c_str(), 0644) == 0);
  Q4T_CHECK(!LoadFileInputPolicy(path.string(), &policy));
  auto fifo = fixture.root / "fifo";
  Q4T_CHECK(::mkfifo(fifo.c_str(), 0600) == 0);
  Q4T_CHECK(!LoadFileInputPolicy(fifo.string(), &policy));
  Q4T_CHECK(!LoadFileInputPolicy(fixture.root.string(), &policy));
  fs::remove(path);
  Q4T_CHECK(!LoadFileInputPolicy(path.string(), &policy));
  Q4T_CHECK(policy.sha256 == Digest(kPolicy) && policy.options.max_files == 4);
  return true;
}

Q4T_TEST(file_input_policy_discovery_matches_effective_limits_and_redacts) {
  FileInputPolicy policy;
  Q4T_CHECK(ParseFileInputPolicy(kPolicy, &policy));
  policy.options.extraction.legacy.work_directory = "/private/scratch";
  const auto description =
      FileInputPolicyDescription(policy.options, policy.sha256);
  Q4T_CHECK(description.GetInt("max_source_bytes") == 1000000 &&
            description.GetInt("max_retained_bytes") == 3000000 &&
            description.GetInt("model_media_items") == 8 &&
            description.GetInt("spreadsheet_rows") == 1000 &&
            !description.GetBool("retained_budget_is_rss_limit"));
  const auto raw = OutputJson(description);
  for (const auto* secret : {"/private/", "10.2.0.0", "private.example"})
    Q4T_CHECK(raw.find(secret) == raw.npos);
  Q4T_CHECK(description.Find("fetch")->GetInt("allowed_hosts_count") == 1 &&
            description.Find("fetch")->GetBool("custom_ca"));
  ChatPlanConfig config;
  config.model = "cpu";
  config.file_inputs = std::make_shared<FileExpansionOptions>(policy.options);
  config.file_input_policy_sha256 = policy.sha256;
  ServerOptions requested;
  requested.file_input_policy_file = "/private/deployment.json";
  Json effective;
  Q4T_CHECK(q4t::io::ParseJson(
      EffectiveServeConfigJson(requested, config, true, false, 32),
      &effective));
  Q4T_CHECK(OutputJson(*effective.Find("file_inputs")) == raw);
  Q4T_CHECK(OutputJson(effective).find("/private/") == std::string::npos);
  return true;
}

Q4T_TEST(file_input_policy_cli_and_internal_configuration_fail_before_io) {
  ServerOptions options;
  options.model_dir = "/read-only-model";
  const std::string_view args[]{"--file-input-policy-file", "/private/p.json"};
  Q4T_CHECK(ParseServerOptions(args, &options));
  Q4T_CHECK(options.file_input_policy_file == "/private/p.json");
  const std::string_view missing[]{"--file-input-policy-file"};
  Q4T_CHECK(!ParseServerOptions(missing, &options));
  Q4T_CHECK(options.file_input_policy_file == "/private/p.json");
  options.file_input_policy_file = std::string("abc\0def", 7);
  Q4T_CHECK(!ValidateServerOptions(options));
  FileExpansionOptions expansion;
  expansion.storage.max_parts = 0;
  Q4T_CHECK(!ValidFileExpansionOptions(expansion));
  expansion = {};
  expansion.extraction.pdf.timeout_ms = 0;
  Q4T_CHECK(!ValidFileExpansionOptions(expansion));
  expansion = {};
  expansion.extraction.legacy.io.notifications = 0;
  Q4T_CHECK(!ValidFileExpansionOptions(expansion));
  expansion = {};
  expansion.fetch.ports.clear();
  Q4T_CHECK(!ValidFileExpansionOptions(expansion));
  return true;
}

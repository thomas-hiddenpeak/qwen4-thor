#include "q4t/server/skill_worker.h"

#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <thread>

#include "q4t/server/files_http.h"

#include "../src/server/object_protocol.h"
#include "q4t/io/base64.h"
#include "q4t/server/containers.h"
#include "q4t/server/skills.h"
#include "q4t/server/state_cipher.h"

#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
BinaryAccess Access() {
  return {"client:skill", std::chrono::steady_clock::now() + 15s, {}};
}
SkillPackage Directory(std::string markdown) {
  SkillPackage request;
  request.files = {{"release-check/sKiLl.Md", std::move(markdown)},
                   {"release-check/材料.txt", std::string("log\0data", 8)}};
  return request;
}
const char* kManifest =
    "---\r\nname: release-check\r\ndescription: >-\r\n"
    "  Read the publication.\r\n  核对版本。\r\n"
    "metadata:\r\n  owner: operations\r\n---\r\n# Check\nRun the read-only "
    "query.\n";
bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}
}  // namespace

Q4T_TEST(
    skill_directory_zip_roundtrip_validates_folded_yaml_and_exact_file_bytes) {
  const auto source = Directory(kManifest);
  SkillPackage zipped;
  Q4T_CHECK(Ok(ProcessSkillPackage(source, false, Access(), &zipped)));
  Q4T_CHECK(zipped.name == "release-check" &&
            zipped.description == "Read the publication. 核对版本。");
  Q4T_CHECK(zipped.files.empty() && zipped.archive.starts_with("PK"));
  SkillPackage expanded;
  Q4T_CHECK(Ok(ProcessSkillPackage(zipped, true, Access(), &expanded)));
  Q4T_CHECK(expanded.name == zipped.name && expanded.archive == zipped.archive);
  Q4T_CHECK(expanded.files.size() == source.files.size());
  for (size_t i = 0; i < source.files.size(); ++i) {
    Q4T_CHECK(source.files[i].name == expanded.files[i].name);
    Q4T_CHECK(source.files[i].bytes == expanded.files[i].bytes);
  }
  return true;
}

Q4T_TEST(skill_manifest_and_member_conflicts_leave_previous_result_untouched) {
  SkillPackage result;
  result.name = "previous";
  for (
      const auto* bad :
      {"---\nname: other\ndescription: use\n---\n",
       "---\nname: release-check\nname: release-check\ndescription: use\n---\n",
       "---\nname: release-check\ndescription: [one,two]\n---\n",
       "---\nname: release-check\ndescription: use\nmetadata: [text]\n---\n"}) {
    auto fail = ProcessSkillPackage(Directory(bad), true, Access(), &result);
    Q4T_CHECK(fail.http_status == 400 &&
              fail.code == "invalid_skill_manifest" &&
              result.name == "previous");
  }
  for (const auto path :
       {"release-check/nested/SKILL.md", "second-root/data",
        "release-check/材料.txt/nested", "release-check/sKiLl.Md"}) {
    auto input = Directory(kManifest);
    input.files.push_back({path, "data"});
    auto fail = ProcessSkillPackage(std::move(input), false, Access(), &result);
    Q4T_CHECK(fail.http_status == 400 && result.name == "previous");
  }
  return true;
}

Q4T_TEST(skill_worker_rejects_mixed_sources_cancelled_and_truncated_inputs) {
  auto input = Directory(kManifest);
  SkillPackage output;
  auto access = Access();
  access.cancelled = [] { return true; };
  Q4T_CHECK(ProcessSkillPackage(input, false, access, &output).http_status ==
            499);
  input.archive = "PK";
  Q4T_CHECK(ProcessSkillPackage(input, false, Access(), &output).http_status ==
            400);
  input.files.clear();
  Q4T_CHECK(ProcessSkillPackage(input, true, Access(), &output).http_status ==
            400);
  Q4T_CHECK(output.name.empty() && output.archive.empty() &&
            output.files.empty());
  return true;
}

namespace {
using namespace q4t::server::object_detail;
ObjectStoreOptions SkillStoreOptions() {
  const auto* base = std::getenv("Q4T_TEST_OUTPUT_DIR");
  auto root = std::filesystem::absolute(base ? base : ".q4t-work/skill-cpu");
  std::filesystem::create_directories(root);
  auto directory = (root / "skill-XXXXXX").string();
  if (!mkdtemp(directory.data())) throw std::runtime_error("skill fixture");
  ObjectStoreOptions options;
  options.directory = directory + "/objects";
  options.restart_backoff_ms = 0;
  return options;
}
StateAccess Owner(std::string scope = "client:skill") {
  return {std::move(scope), std::chrono::steady_clock::now() + 15s, {}};
}
ObjectReply Request(ObjectStore& store, std::string_view method,
                    std::string_view path, const std::string& body = "") {
  return HandleSkills(store, Owner(), method, path, body);
}
ObjectReply Upload(ObjectStore& store, std::string_view path, std::string note,
                   bool make_default = false) {
  auto input = Directory("---\nname: release-check\ndescription: " + note +
                         "\n---\nRead the paired receipt.\n");
  input.files.back().bytes = note;
  SkillPackage parsed;
  auto result = ProcessSkillPackage(std::move(input), false, Access(), &parsed);
  if (!result.ok()) return result;
  return HandleSkills(store, Owner(), "POST", path, "", &parsed, make_default);
}
}  // namespace

Q4T_TEST(
    skill_registry_scopes_monotonic_versions_cursors_and_default_deletion) {
  ObjectStore store(SkillStoreOptions());
  const auto skill = Upload(store, "/v1/skills", "release one");
  Q4T_CHECK(Ok(skill));
  const auto root = "/v1/skills/" + skill.value.GetString("id");
  const auto versions = root + "/versions";
  const auto second = Upload(store, versions, "release two");
  Q4T_CHECK(Ok(second) && second.value.GetString("version") == "2" &&
            second.value.GetString("id").starts_with("skillver_"));
  Q4T_CHECK(Request(store, "GET", versions + "/0").http_status == 400);
  Q4T_CHECK(Request(store, "POST", root, R"({"default_version":"abc"})")
                .http_status == 400);
  Q4T_CHECK(
      Request(store, "GET", "/v1/skills?after=skill_missing").http_status ==
      404);
  Q4T_CHECK(
      Request(store, "GET", versions + "?after=skillver_missing").http_status ==
      404);
  auto sibling = Upload(store, "/v1/skills", "sibling");
  Q4T_CHECK(Ok(sibling));
  auto sibling_versions =
      Request(store, "GET",
              "/v1/skills/" + sibling.value.GetString("id") + "/versions");
  Q4T_CHECK(Ok(sibling_versions));
  Q4T_CHECK(Request(store, "GET",
                    versions + "?after=" +
                        sibling_versions.value.GetString("first_id"))
                .http_status == 400);
  auto info = Request(store, "GET", root);
  Q4T_CHECK(Ok(info) && info.value.GetString("default_version") == "1" &&
            info.value.GetString("latest_version") == "2");
  for (const auto& path : {std::string("/v1/skills"), versions}) {
    auto page = Request(store, "GET", path + "?limit=0");
    Q4T_CHECK(Ok(page) && page.value.GetArray("data")->array.empty() &&
              page.value.Find("first_id")->IsNull() &&
              page.value.GetBool("has_more"));
    Q4T_CHECK(Request(store, "GET", path + "?limit=0.5").http_status == 400);
  }
  const auto page = Request(store, "GET", versions + "?limit=1&order=asc");
  Q4T_CHECK(Ok(page) && page.value.GetBool("has_more"));
  auto after = Request(
      store, "GET",
      versions + "?limit=1&order=asc&after=" + page.value.GetString("last_id"));
  Q4T_CHECK(Ok(after) &&
            after.value.GetString("first_id") == second.value.GetString("id") &&
            !after.value.GetBool("has_more"));
  Q4T_CHECK(Request(store, "DELETE", versions + "/1").http_status == 400);
  Q4T_CHECK(
      HandleSkills(store, Owner("client:other"), "GET", versions + "/2", "")
          .http_status == 404);
  Q4T_CHECK(Ok(Request(store, "POST", root, R"({"default_version":"2"})")));
  Q4T_CHECK(Ok(Request(store, "DELETE", versions + "/1")));
  auto third = Upload(store, versions, "release three", true);
  Q4T_CHECK(Ok(third) && third.value.GetString("version") == "3");
  Q4T_CHECK(Ok(Request(store, "POST", root, R"({"default_version":"2"})")));
  Q4T_CHECK(Ok(Request(store, "DELETE", versions + "/3")));
  Q4T_CHECK(Request(store, "GET", root).value.GetString("latest_version") ==
            "2");
  auto fourth = Upload(store, versions, "release four");
  Q4T_CHECK(Ok(fourth) && fourth.value.GetString("version") == "4");
  Q4T_CHECK(Ok(Request(store, "DELETE", versions + "/4")));
  Q4T_CHECK(Ok(Request(store, "DELETE", versions + "/2")));
  Q4T_CHECK(Request(store, "GET", root).http_status == 404);
  return true;
}

Q4T_TEST(container_skill_mount_survives_source_deletion_and_owner_restart) {
  const auto options = SkillStoreOptions();
  std::string container;
  {
    ObjectStore store(options);
    auto first = Upload(store, "/v1/skills", "production receipt one");
    Q4T_CHECK(Ok(first));
    const auto skill = first.value.GetString("id");
    const auto url = "/v1/skills/" + skill;
    Q4T_CHECK(Ok(Upload(store, url + "/versions", "production receipt two")));
    auto ref = Object(
        {{"type", String("skill_reference")}, {"skill_id", String(skill)}});
    auto latest = ref;
    Set(&latest, "version", String("latest"));
    SkillPackage bundle;
    Q4T_CHECK(Ok(
        ProcessSkillPackage(Directory(kManifest), false, Access(), &bundle)));
    std::string encoded;
    Q4T_CHECK(q4t::io::EncodeBase64(bundle.archive, 1 << 20, &encoded));
    const auto inlined =
        Object({{"type", String("inline")},
                {"name", String("local-check")},
                {"description", String("inline receipt")},
                {"source", Object({{"type", String("base64")},
                                   {"media_type", String("application/zip")},
                                   {"data", String(encoded)}})}});
    auto spec = Object({{"name", String("versioned-receipts")},
                        {"skills", Array({ref, latest, inlined})}});
    auto denied = HandleContainers(store, Owner("client:other"), "POST",
                                   "/v1/containers", OutputJson(spec));
    Q4T_CHECK(denied.http_status == 404);
    auto bad = ref;
    Set(&bad, "skill_id", String("skill_missing"));
    auto partial = spec;
    Set(&partial, "skills", Array({inlined, bad}));
    Q4T_CHECK(HandleContainers(store, Owner(), "POST", "/v1/containers",
                               OutputJson(partial))
                  .http_status == 404);
    auto list = HandleContainers(store, Owner(), "GET", "/v1/containers", "");
    Q4T_CHECK(Ok(list) && list.value.GetArray("data")->array.empty());
    auto made = HandleContainers(store, Owner(), "POST", "/v1/containers",
                                 OutputJson(spec));
    Q4T_CHECK(Ok(made));
    container = made.value.GetString("id");
    Q4T_CHECK(Ok(Request(store, "DELETE", url)));
  }
  ObjectStore store(options);
  std::vector<FilePackageEntry> files;
  Q4T_CHECK(
      ReadContainerSkillFiles(store, Owner("client:other"), container, &files)
          .http_status == 404);
  Q4T_CHECK(Ok(ReadContainerSkillFiles(store, Owner(), container, &files)));
  Q4T_CHECK(files.size() == 6 && files[1].bytes == "production receipt one" &&
            files[3].bytes == "production receipt two" &&
            files[5].bytes == std::string("log\0data", 8));
  Q4T_CHECK(files[1].name != files[3].name && files[3].name != files[5].name);
  Q4T_CHECK(Ok(HandleContainers(store, Owner(), "DELETE",
                                "/v1/containers/" + container, "")));
  Q4T_CHECK(
      ReadContainerSkillFiles(store, Owner(), container, &files).http_status ==
      404);
  return true;
}

Q4T_TEST(skill_content_http_serves_zip_and_base64_across_binary_chunks) {
  ObjectStore store(SkillStoreOptions());
  BinaryStore binary(store);
  BinaryStage stage;
  Q4T_CHECK(Ok(binary.Begin(Access(), kBinaryChunkBytes + 9, 3600, &stage)));
  const std::vector<std::string> pieces = {
      std::string(kBinaryChunkBytes, '\xfa'), "X", "Y", "ABCD"};
  std::string bytes;
  for (const auto& piece : pieces) {
    Q4T_CHECK(Ok(binary.Append(Access(), &stage, piece)));
    bytes += piece;
  }
  ObjectRequest file;
  file.kind = ObjectKind::kFile;
  file.operation = ObjectOperation::kPut;
  file.id = NewObjectId(file.kind);
  file.scope = Access().scope;
  file.ttl_seconds = 0;
  file.data = Object({{"format", String("q4t.file.v1")},
                      {"file", Object({{"id", String(file.id)}})}});
  Q4T_CHECK(Ok(binary.Publish(Access(), &stage, file)));
  std::string base64;
  Q4T_CHECK(q4t::io::EncodeBase64(bytes, 2 * kBinaryChunkBytes, &base64));
  for (const auto* mime : {"application/zip", "application/json"}) {
    BinaryRead read;
    Q4T_CHECK(Ok(binary.Open(Access(), file.kind, file.id, 3600, &read)));
    std::array<int, 2> fds{};
    Q4T_CHECK(socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, fds.data()) ==
              0);
    std::jthread writer([&] {
      SendPinnedBinary(fds[0], binary, Access(), read, mime);
      shutdown(fds[0], SHUT_WR);
    });
    std::string received;
    std::array<char, 65536> buf{};
    for (;;) {
      const auto count = recv(fds[1], buf.data(), buf.size(), 0);
      if (count <= 0) break;
      received.append(buf.data(), count);
    }
    writer.join();
    close(fds[0]);
    close(fds[1]);
    const auto end = received.find("\r\n\r\n");
    const std::string expected = std::string_view(mime) == "application/json"
                                     ? '"' + base64 + '"'
                                     : bytes;
    Q4T_CHECK(read.closed && end != received.npos &&
              received.starts_with("HTTP/1.1 200"));
    Q4T_CHECK(received.find("Content-Type: " + std::string(mime) + "\r\n") <
              end);
    Q4T_CHECK(received.find("Content-Length: " +
                            std::to_string(expected.size()) + "\r\n") < end);
    Q4T_CHECK(received.substr(end + 4) == expected);
  }
  return true;
}

Q4T_TEST(container_legacy_skill_options_bind_once_and_preserve_domain_secrets) {
  ObjectStore store(SkillStoreOptions());
  auto skill = Upload(store, "/v1/skills", "record before migration");
  Q4T_CHECK(Ok(skill));
  const auto skill_id = skill.value.GetString("id");
  auto made = HandleContainers(store, Owner(), "POST", "/v1/containers",
                               R"({"name":"existing"})");
  Q4T_CHECK(Ok(made));
  const auto id = made.value.GetString("id");
  ObjectRequest workspace;
  workspace.kind = ObjectKind::kToolRun;
  workspace.scope = Owner().scope;
  workspace.id = "workspace_" + id;
  auto current = store.Execute(workspace, Owner().deadline);
  Q4T_CHECK(Ok(current));
  auto old =
      Object({{"skills", Array({Object({{"type", String("skill_reference")},
                                        {"skill_id", String(skill_id)}})})},
              {"domain_secrets",
               Array({Object({{"value", String("opaque-legacy-fixture")}})})}});
  StateCipher cipher(store);
  const StateCipherBinding binding{"q4t-containers", id, "execution-options",
                                   id};
  std::string sealed;
  Q4T_CHECK(Ok(cipher.Seal(OutputJson(old), binding, Owner(), &sealed)));
  workspace.operation = ObjectOperation::kPut;
  workspace.data = *current.value.Find("data");
  std::erase_if(workspace.data.object, [](const auto& field) {
    return field.first == "mounted_skills";
  });
  Set(&workspace.data, "execution_options", String(sealed));
  workspace.expected_revision = current.value.GetInt("revision");
  workspace.created_at = current.value.GetInt("created_at");
  workspace.expires_at = current.value.GetInt("expires_at");
  Q4T_CHECK(Ok(store.Execute(workspace, Owner().deadline)));
  std::vector<FilePackageEntry> files;
  Q4T_CHECK(Ok(ReadContainerSkillFiles(store, Owner(), id, &files)));
  Q4T_CHECK(files.size() == 2 &&
            files.back().bytes == "record before migration");
  Q4T_CHECK(Ok(Request(store, "DELETE", "/v1/skills/" + skill_id)));
  files.clear();
  Q4T_CHECK(Ok(ReadContainerSkillFiles(store, Owner(), id, &files)) &&
            files.size() == 2);
  workspace.operation = ObjectOperation::kGet;
  auto migrated = store.Execute(workspace, Owner().deadline);
  Q4T_CHECK(Ok(migrated));
  const auto* data = migrated.value.Find("data");
  Q4T_CHECK(data->GetArray("mounted_skills")->array.size() == 1);
  Q4T_CHECK(OutputJson(*data).find("opaque-legacy-fixture") ==
            std::string::npos);
  std::string retained;
  Q4T_CHECK(Ok(cipher.Open(data->GetString("execution_options"), binding,
                           Owner(), &retained)));
  Q4T_CHECK(retained == OutputJson(Object({{"domain_secrets",
                                            *old.Find("domain_secrets")}})));
  return true;
}

#include "q4t/server/video_characters.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>

#include "../src/server/object_protocol.h"
#include "q4t/server/binary_store.h"
#include "q4t/server/http_connection.h"
#include "q4t/server/object_retention.h"
#include "q4t/server/lifetime.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
using q4t::server::object_detail::Digest;

bool Ok(const ObjectReply& reply) {
  if (!reply.ok())
    std::fprintf(stderr, "%s: %s\n", reply.code.c_str(), reply.message.c_str());
  return reply.ok();
}

Q4T_TEST(video_character_reads_have_reserved_observation_capacity) {
  const std::string create(kVideoCharacters);
  const auto item = create + "/char_123";
  Q4T_CHECK(VideoCharacterMethodAllowed("POST", create) &&
            IsBinaryUpload("POST", create, "multipart/form-data") &&
            ClassifyHttpConnection("POST", create) ==
                HttpConnectionClass::kBusiness);
  Q4T_CHECK(VideoCharacterMethodAllowed("GET", item) &&
            !IsBinaryUpload("GET", item) &&
            ClassifyHttpConnection("GET", item) ==
                HttpConnectionClass::kObservation);
  Q4T_CHECK(!VideoCharacterMethodAllowed("DELETE", item) &&
            !VideoCharacterMethodAllowed("GET", create + "/char_") &&
            !VideoCharacterMethodAllowed("GET", item + "/content"));
  return true;
}

Q4T_TEST(video_character_keeps_original_clip_behind_scoped_metadata) {
  auto directory =
      (std::filesystem::temp_directory_path() / "q4t-video-character-XXXXXX")
          .string();
  Q4T_CHECK(mkdtemp(directory.data()));
  ScopeExit cleanup([&] { std::filesystem::remove_all(directory); });
  ObjectStoreOptions options;
  options.directory = directory + "/objects";
  std::ifstream input(std::filesystem::path(__FILE__).parent_path() /
                          "fixtures/file_video/h264.mp4",
                      std::ios::binary);
  Q4T_CHECK(input.good());
  const std::string video(std::istreambuf_iterator<char>(input), {});
  const StateAccess owner{"client:video", Clock::now() + 25s, {}};
  const StateAccess peer{"client:peer", owner.deadline, {}};
  const BinaryAccess binary{owner.scope, owner.deadline, {}};
  const std::string form = "multipart/form-data; boundary=character";
  const std::string path = "/v1/videos/characters";
  const auto name_field = [](std::string name) {
    return "--character\r\nContent-Disposition: form-data; name=\"name\""
           "\r\n\r\n" +
           name + "\r\n";
  };
  const auto file_field =
      "--character\r\nContent-Disposition: form-data; "
      "name=\"video\"; filename=\"clip.mp4\"\r\n"
      "Content-Type: video/mp4\r\n\r\n" +
      video + "\r\n";
  const std::string end = "--character--\r\n";
  std::string name;
  name.push_back('\0');
  for (int i = 1; i < 80; ++i) name += "影";
  std::string id;
  {
    ObjectStore store(options);
    auto post = [&](const std::string& body) {
      return HandleVideoCharacter(store, owner, "POST", path, form, body);
    };
    Q4T_CHECK(post(file_field + name_field(name + "片") + end).http_status ==
              400);
    Q4T_CHECK(post(name_field(name) + name_field("extra") + end).http_status ==
              400);
    Q4T_CHECK(post(name_field(name) + file_field).http_status == 400);
    Q4T_CHECK(post(name_field(std::string(1, static_cast<char>(0xff))) +
                   file_field + end)
                  .http_status == 400);
    Q4T_CHECK(HandleVideoCharacter(store, owner, "POST", path,
                                   "application/json", "{}")
                  .http_status == 415);
    auto created = post(file_field + name_field(name) + end);
    Q4T_CHECK(Ok(created) && created.value.object.size() == 3);
    id = created.value.GetString("id");
    Q4T_CHECK(id.starts_with("char_") &&
              created.value.GetString("name") == name);
  }
  ObjectStore reopened(options);
  auto get = [&](const StateAccess& access) {
    return HandleVideoCharacter(reopened, access, "GET", path + "/" + id, {},
                                {});
  };
  Q4T_CHECK(get(peer).http_status == 404);
  auto retained = get(owner);
  Q4T_CHECK(Ok(retained) && retained.value.GetString("id") == id &&
            retained.value.GetString("name") == name);
  BinaryStore blobs(reopened);
  BinaryRead read;
  q4t::io::Json metadata;
  Q4T_CHECK(Ok(blobs.Open(binary, ObjectKind::kVideoCharacter, id, 60, &read,
                          &metadata)));
  std::string saved;
  Q4T_CHECK(read.manifest.chunks.size() == 1 &&
            Ok(blobs.Read(binary, read, 0, &saved)) && saved == video &&
            metadata.GetString("source_sha256") == Digest(video));
  const auto retention = InspectObjectRetention("video_character", metadata);
  Q4T_CHECK(!retention.uncertain && !retention.collect_when_unreferenced &&
            retention.references.size() == 1 &&
            retention.references.front() == read.manifest.chunks.front().id);
  return Ok(blobs.Close(binary, &read));
}
}  // namespace

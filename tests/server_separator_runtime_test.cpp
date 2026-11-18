#include "q4t/audio/separator_config.h"
#include "q4t/server/chat_capabilities.h"
#include "q4t/server/server_options.h"
#include "q4t/test.h"

namespace {
using namespace q4t::server;
}

Q4T_TEST(separator_options_require_complete_pipeline_and_atomic_limits) {
  ServerOptions options;
  options.model_dir = "/readonly-main";
  const auto parse = [&](std::initializer_list<std::string_view> args) {
    return ParseServerOptions({args.begin(), args.size()}, &options).ok();
  };
  Q4T_CHECK(!parse({"--separator-model-dir", "/optional/separator"}));
  Q4T_CHECK(options.separator_model_dir.empty());
  Q4T_CHECK(!parse({"--separator-model-dir", "/optional/separator",
                    "--speaker-model-dir", "/optional/speaker"}));
  Q4T_CHECK(parse({"--separator-model-dir", "/optional/separator",
                   "--speaker-model-dir", "/optional/speaker",
                   "--asr-model-dir", "/optional/asr", "--aligner-model-dir",
                   "/optional/aligner", "--vad-model-dir", "/optional/vad"}));
  for (auto path : {"relative", "/", "/a/", "/a//b", "/a/../b"})
    Q4T_CHECK(!parse({"--separator-model-dir", path}));
  for (auto flag :
       {"--separator-max-audio-seconds", "--separator-workspace-mib"})
    for (auto value : {"0", "-1", "2.5", "2147483647", "1x"})
      Q4T_CHECK(!parse({flag, value}));
  Q4T_CHECK(!parse({"--separator-workspace-mib", "512"}));
  Q4T_CHECK(!parse({"--separator-max-audio-seconds", "31"}));
  Q4T_CHECK(options.separator_limits.workspace_mib == 2048);
  Q4T_CHECK(parse({"--separator-max-audio-seconds", "8",
                   "--separator-workspace-mib", "512"}));
  Q4T_CHECK(options.separator_limits.workspace_mib == 512 &&
            options.separator_limits.max_audio_seconds == 8);
  Q4T_CHECK(options.separator_model_dir == "/optional/separator");
  return true;
}

Q4T_TEST(
    separator_capabilities_distinguish_actual_tracks_and_release_evidence) {
  using C = q4t::audio::SeparatorConfig;
  for (bool enabled : {false, true}) {
    q4t::io::Json root;
    Q4T_CHECK(q4t::io::ParseJson(
        ChatCapabilitiesJson("q4t", false, true, 0, nullptr, false, true, true,
                             true, 64, true, enabled),
        &root));
    const auto* asr = root.Find("transcriptions");
    Q4T_CHECK(asr);
    const auto* separator = asr->Find("separator");
    Q4T_CHECK(separator &&
              separator->GetBool("configured", !enabled) == enabled);
    Q4T_CHECK(separator->GetString("profile") == C::kProfile &&
              separator->GetString("revision") == C::kRevision &&
              separator->GetString("weight_sha256") == C::kWeightSha256);
    Q4T_CHECK(separator->GetInt("simultaneous_sources") == 2 &&
              separator->GetInt("sample_rate") == 8000 &&
              separator->GetInt("output_sample_rate") == 16000);
    Q4T_CHECK(!separator->GetBool("core_readiness_dependency", true));
    Q4T_CHECK(separator->GetString("budget") == "shared_request");
    Q4T_CHECK(separator->GetString("speaker_identity") ==
              "cross_track_acoustic_clustering");
    Q4T_CHECK(root.GetBool("implementation_only") &&
              !root.GetBool("release_attestation"));
  }
  return true;
}

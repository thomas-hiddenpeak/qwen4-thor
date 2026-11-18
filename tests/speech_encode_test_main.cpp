#include "q4t/server/file_audio.h"
#include "q4t/server/speech_encode.h"
#include "q4t/server/speech_codec_stream.h"
#include "q4t/test.h"

#include <fstream>

int SpeechEncodeFixture(int argc, char** argv);
int SpeechCodecStreamFixture(int argc, char** argv);
int SpeechEncodeReplay(int argc, char** argv);
int main(int argc, char** argv) {
  const int replay = SpeechEncodeReplay(argc, argv);
  if (replay >= 0) return replay;
  const int speech_stream =
      q4t::server::MaybeRunSpeechCodecStreamWorker(argc, argv);
  if (speech_stream >= 0) return speech_stream;
  const int stream_fixture = SpeechCodecStreamFixture(argc, argv);
  if (stream_fixture >= 0) return stream_fixture;
  const int encoder = q4t::server::MaybeRunSpeechEncodeWorker(argc, argv);
  if (encoder >= 0) return encoder;
  const int decoder = q4t::server::MaybeRunFileAudioWorker(argc, argv);
  if (decoder >= 0) return decoder;
  const int fixture = SpeechEncodeFixture(argc, argv);
  if (fixture >= 0) return fixture;
  if (argc == 2 && std::string_view(argv[1]) == "--list") {
    for (const auto& item : q4t::test::Registry())
      std::printf("%s\n", item.name.c_str());
    return q4t::test::Registry().empty() ? 2 : 0;
  }
  if (argc != 3 || std::string_view(argv[1]) != "--required-list") return 2;
  std::ifstream input(argv[2]);
  if (!input) return 2;
  std::vector<std::string> names;
  std::string name;
  while (std::getline(input, name)) {
    if (!name.empty() && name.back() == '\r') name.pop_back();
    if (!name.empty() && !name.starts_with('#')) names.push_back(name);
  }
  return input.bad() ? 2 : q4t::test::RunTests(names);
}

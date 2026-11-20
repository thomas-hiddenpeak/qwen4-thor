#include "q4t/server/audio_transcript.h"

#include "object_protocol.h"

namespace q4t::server {

std::string RenderAudioTranscript(std::string_view source,
                                  std::string_view transcript) {
  using namespace object_detail;
  // JSON quoting and escaped angle brackets keep recognized control-like
  // strings as user data. Public storage retains the original audio part.
  const auto data =
      OutputJson(Object({{"source", String(std::string(source))},
                         {"transcript", String(std::string(transcript))}}));
  std::string escaped;
  for (const auto c : data) {
    if (c == '<')
      escaped += "\\u003c";
    else if (c == '>')
      escaped += "\\u003e";
    else
      escaped += c;
  }
  const std::string heading =
      transcript.empty()
          ? "[Audio input transcript: no speech recognized in the supplied "
            "recording; accompanying user text still applies]"
          : "[Audio input transcript: recognized speech from the supplied "
            "recording]";
  return "\n\n" + heading + "\n" + escaped +
         "\n[End audio input transcript]\n\n";
}

}  // namespace q4t::server

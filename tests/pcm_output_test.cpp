#include "q4t/audio/pcm_output.h"
#include "q4t/test.h"

#include <array>
#include <limits>

Q4T_TEST(speech_pcm_has_exact_signed_little_endian_samples) {
  const std::array<float, 5> pcm{-1, -0.5, 0, 0.5, 1};
  std::string bytes;
  Q4T_CHECK(q4t::audio::EncodeSpeechPcm(pcm, false, 10, &bytes));
  Q4T_CHECK(bytes ==
            std::string("\x00\x80\x00\xc0\x00\x00\x00\x40\xff\x7f", 10));
  Q4T_CHECK(q4t::audio::EncodeSpeechPcm(pcm, true, 54, &bytes));
  Q4T_CHECK(bytes.size() == 54 && bytes.substr(0, 4) == "RIFF");
  Q4T_CHECK(bytes.substr(8, 8) == "WAVEfmt ");
  Q4T_CHECK(static_cast<unsigned char>(bytes[4]) == 46);
  Q4T_CHECK(bytes.substr(24, 4) == std::string("\xc0\x5d\x00\x00", 4));
  Q4T_CHECK(bytes.substr(36, 4) == "data" && bytes[40] == 10);
  return true;
}

Q4T_TEST(speech_pcm_failure_preserves_output_and_rejects_fake_silence) {
  std::string output = "preserved";
  for (float value : {std::numeric_limits<float>::quiet_NaN(),
                      std::numeric_limits<float>::infinity(), 1.01f, -1.01f}) {
    Q4T_CHECK(!q4t::audio::EncodeSpeechPcm({&value, 1}, false, 100, &output));
    Q4T_CHECK(output == "preserved");
  }
  const std::array<float, 2> values{0.5, -0.5};
  Q4T_CHECK(!q4t::audio::EncodeSpeechPcm(values, false, 3, &output));
  Q4T_CHECK(!q4t::audio::EncodeSpeechPcm(values, true, 47, &output));
  Q4T_CHECK(!q4t::audio::EncodeSpeechPcm({}, true, 100, &output));
  Q4T_CHECK(output == "preserved");
  return true;
}

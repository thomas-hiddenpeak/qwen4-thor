#include "q4t/server/voice_verification.h"

#include <cmath>

#include "q4t/server/voice_request.h"

namespace q4t::server {
std::string_view VoiceConsentPhrase(std::string_view language) {
  if (!WellFormedVoiceLanguage(language)) return {};
  std::string primary(language.substr(0, language.find('-')));
  for (char& c : primary)
    if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
  // Protocol wording pinned from the official custom-voices guide. Audio is
  // processed locally; the organization named in the phrase is not contacted.
  constexpr std::pair<std::string_view, std::string_view> phrases[] = {
      {"de",
       R"(Ich bin der Eigentümer dieser Stimme und bin damit einverstanden, dass OpenAI diese Stimme zur Erstellung eines synthetischen Stimmmodells verwendet.)"},
      {"en",
       R"(I am the owner of this voice and I consent to OpenAI using this voice to create a synthetic voice model.)"},
      {"es",
       R"(Soy el propietario de esta voz y doy mi consentimiento para que OpenAI la utilice para crear un modelo de voz sintética.)"},
      {"fr",
       R"(Je suis le propriétaire de cette voix et j'autorise OpenAI à utiliser cette voix pour créer un modèle de voix synthétique.)"},
      {"hi",
       R"(मैं इस आवाज का मालिक हूं और मैं सिंथेटिक आवाज मॉडल बनाने के लिए OpenAI को इस आवाज का उपयोग करने की सहमति देता हूं)"},
      {"id",
       R"(Saya adalah pemilik suara ini dan saya memberikan persetujuan kepada OpenAI untuk menggunakan suara ini guna membuat model suara sintetis.)"},
      {"it",
       R"(Sono il proprietario di questa voce e acconsento che OpenAI la utilizzi per creare un modello di voce sintetica.)"},
      {"ja",
       R"(私はこの音声の所有者であり、OpenAIがこの音声を使用して音声合成 モデルを作成することを承認します。)"},
      {"ko",
       R"(나는 이 음성의 소유자이며 OpenAI가 이 음성을 사용하여 음성 합성 모델을 생성할 것을 허용합니다.)"},
      {"nl",
       R"(Ik ben de eigenaar van deze stem en ik geef OpenAI toestemming om deze stem te gebruiken om een synthetisch stemmodel te maken.)"},
      {"pl",
       R"(Jestem właścicielem tego głosu i wyrażam zgodę na wykorzystanie go przez OpenAI w celu utworzenia syntetycznego modelu głosu.)"},
      {"pt",
       R"(Eu sou o proprietário desta voz e autorizo o OpenAI a usá-la para criar um modelo de voz sintética.)"},
      {"ru",
       R"(Я являюсь владельцем этого голоса и даю согласие OpenAI на использование этого голоса для создания модели синтетического голоса.)"},
      {"uk",
       R"(Я є власником цього голосу і даю згоду OpenAI використовувати цей голос для створення синтетичної голосової моделі.)"},
      {"vi",
       R"(Tôi là chủ sở hữu giọng nói này và tôi đồng ý cho OpenAI sử dụng giọng nói này để tạo mô hình giọng nói tổng hợp.)"},
      {"zh", R"(我是此声音的拥有者并授权OpenAI使用此声音创建语音合成模型)"},
  };
  for (const auto& [tag, phrase] : phrases)
    if (tag == primary) return phrase;
  return {};
}

Status BuildVoiceIdentity(std::span<const audio::SpeakerWindow> windows,
                          std::span<const audio::SpeakerEmbedding> embeddings,
                          size_t total_samples, std::vector<float>* identity,
                          const std::function<bool()>& cancelled) {
  if (!identity || windows.empty() || windows.size() > 1024 ||
      windows.size() != embeddings.size() || total_samples > 60 * 16000)
    return Status::Fail("Voice speaker windows exceed verification capacity");
  std::vector<audio::SpeakerEmbedding> normalized(embeddings.size());
  std::array<double, 192> sum{};
  size_t previous_end = 0;
  for (size_t row = 0; row < windows.size(); ++row) {
    if (cancelled && cancelled())
      return Status::Fail("Voice verification cancelled");
    const auto& window = windows[row];
    if (window.begin_sample > window.core_begin_sample ||
        window.core_begin_sample >= window.core_end_sample ||
        window.core_end_sample > window.end_sample ||
        window.end_sample > total_samples ||
        window.core_begin_sample < previous_end)
      return Status::Fail("Voice speech core boundaries are inconsistent");
    previous_end = window.core_end_sample;
    double norm = 0;
    for (const auto value : embeddings[row]) {
      if (!std::isfinite(value))
        return Status::Fail("Voice identity is nonfinite");
      norm += double(value) * value;
    }
    if (!(norm > 0) || !std::isfinite(norm))
      return Status::Fail("Voice identity is empty");
    norm = std::sqrt(norm);
    const auto weight = window.core_end_sample - window.core_begin_sample;
    for (size_t i = 0; i < sum.size(); ++i) {
      normalized[row][i] = embeddings[row][i] / norm;
      sum[i] += double(normalized[row][i]) * weight;
    }
    // Reuse the qualified native speaker reference cosine threshold. Unlike
    // an average-link cluster, one strong speaker cannot mask another voice.
    for (size_t other = 0; other < row; ++other) {
      double cosine = 0;
      for (size_t i = 0; i < sum.size(); ++i)
        cosine += double(normalized[row][i]) * normalized[other][i];
      if (cosine < .65)
        return Status::Fail(
            "Recording contains inconsistent speaker identities");
    }
  }
  double norm = 0;
  for (const auto value : sum) norm += value * value;
  if (!(norm > 0) || !std::isfinite(norm))
    return Status::Fail("Voice aggregate identity is invalid");
  norm = std::sqrt(norm);
  std::vector<float> next(sum.size());
  for (size_t i = 0; i < next.size(); ++i) next[i] = sum[i] / norm;
  if (cancelled && cancelled())
    return Status::Fail("Voice verification cancelled");
  *identity = std::move(next);
  return {};
}
}  // namespace q4t::server

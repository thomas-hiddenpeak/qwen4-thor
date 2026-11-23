#include "q4t/server/live_recording.h"

#include <algorithm>
#include <cmath>
#include <limits>

#include "object_protocol.h"
#include "q4t/audio/pcm_output.h"
#include "q4t/server/input_contract.h"
#include "q4t/server/realtime_pcm.h"

namespace q4t::server {
namespace {
using namespace object_detail;
bool Rate(uint64_t rate) {
  return rate == 8000 || rate == 16000 || rate == 24000;
}
bool Format(RealtimeAudioFormat format) {
  return format.encoding == RealtimeAudioEncoding::kPcm16
             ? format.rate == 16000 || format.rate == 24000
             : (format.encoding == RealtimeAudioEncoding::kMuLaw ||
                format.encoding == RealtimeAudioEncoding::kALaw) &&
                   format.rate == 8000;
}
Status BadLayout() { return Status::Fail("Invalid stereo recording layout"); }
ObjectReply Invalid() {
  return Error(400, "invalid_recording", "Invalid recording sample extent");
}
ObjectReply Corrupt() {
  return Error(503, "recording_corrupt", "Stored recording is inconsistent");
}
bool OK(const ObjectReply& reply) {
  return reply.ok() && !reply.commit_unknown && reply.http_status >= 200 &&
         reply.http_status < 300;
}
bool Unsigned(const Json* value, uint64_t max) {
  return value && IsInputInteger(*value) && value->number >= 0 &&
         value->number <= double(max);
}
}  // namespace

Json LiveRecordingMetadata(const LiveRecordingLayout& layout) {
  auto tracks = Array();
  for (const auto& track : layout.tracks) {
    auto spans = Array();
    for (const auto& s : track)
      spans.array.push_back(Object({{"frame", Number(s.frame)},
                                    {"chunk", Number(s.chunk)},
                                    {"frames", Number(s.frames)}}));
    tracks.array.push_back(std::move(spans));
  }
  return Object({{"format", String("q4t.live_recording.v1")},
                 {"sample_rate", Number(layout.rate)},
                 {"frames", Number(layout.frames)},
                 {"tracks", std::move(tracks)}});
}
Status ReadLiveRecordingLayout(const Json& data, const BinaryManifest& blob,
                               LiveRecordingLayout* out) {
  const auto* tracks = data.GetArray("tracks");
  if (!out || data.GetString("format") != "q4t.live_recording.v1" ||
      !Unsigned(data.Find("sample_rate"), 24000) ||
      !Rate(data.GetInt("sample_rate")) || !tracks ||
      tracks->array.size() != 2 || blob.chunks.size() > kBinaryMaxChunks)
    return BadLayout();
  LiveRecordingLayout result;
  result.rate = data.GetInt("sample_rate");
  if (!Unsigned(data.Find("frames"), 3600ULL * result.rate)) return BadLayout();
  result.frames = data.GetInt("frames");
  std::vector<bool> used(blob.chunks.size());
  size_t count = 0;
  uint64_t bytes = 0;
  for (size_t ch = 0; ch < 2; ++ch) {
    const auto& track = tracks->array[ch];
    if (!track.IsArray() || track.array.size() > used.size() - count)
      return BadLayout();
    uint64_t previous = 0;
    for (const auto& s : track.array) {
      if (!s.IsObject() || s.object.size() != 3 ||
          !Unsigned(s.Find("frame"), result.frames) ||
          !Unsigned(s.Find("frames"), kBinaryChunkBytes / 2) ||
          !Unsigned(s.Find("chunk"), kBinaryMaxChunks))
        return BadLayout();
      const uint64_t frame = s.GetInt("frame"), frames = s.GetInt("frames"),
                     chunk = s.GetInt("chunk");
      if (!frames || chunk >= used.size() || used[chunk] || frame < previous ||
          frames > result.frames - frame ||
          blob.chunks[chunk].bytes != frames * 2)
        return BadLayout();
      previous = frame + frames;
      bytes += frames * 2;
      used[chunk] = true;
      ++count;
      result.tracks[ch].push_back({frame, size_t(chunk), uint32_t(frames)});
    }
  }
  if (count != used.size() || bytes != blob.bytes) return BadLayout();
  *out = std::move(result);
  return {};
}

LiveRecordingWriter::LiveRecordingWriter(RealtimeAudioFormat format,
                                         Append append)
    : format_(format), append_(std::move(append)) {
  layout_.rate = format.rate;
  if (!Format(format_) || !append_) error_ = Invalid();
}
ObjectReply LiveRecordingWriter::Flush(size_t ch) {
  auto& bytes = pending_[ch];
  if (bytes.empty()) return {};
  if (chunks_ >= kBinaryMaxChunks)
    return Error(413, "recording_capacity",
                 "Recording exceeds its segment budget");
  auto reply = append_(bytes);
  if (!OK(reply)) return reply;
  const auto frames = uint32_t(bytes.size() / 2);
  layout_.tracks[ch].push_back({end_[ch] - frames, chunks_++, frames});
  bytes.clear();
  return {};
}
ObjectReply LiveRecordingWriter::Audio(LiveAudioChannel channel,
                                       uint64_t begin_ticks,
                                       std::string_view encoded) {
  if (!OK(error_)) return error_;
  if (done_) return Invalid();
  const size_t ch = static_cast<size_t>(channel);
  const size_t width =
      format_.encoding == RealtimeAudioEncoding::kPcm16 ? 2 : 1;
  const size_t frames = encoded.size() / width;
  const uint64_t ticks_per_frame = kRealtimeTicksPerSecond / format_.rate;
  const uint64_t begin = begin_ticks / ticks_per_frame;
  if (ch >= 2 || encoded.empty() || encoded.size() % width ||
      begin_ticks > kRealtimeSessionTicks ||
      frames > (kRealtimeSessionTicks - begin_ticks) / ticks_per_frame ||
      begin < end_[ch])
    return error_ = Invalid();
  if (begin != end_[ch]) {
    error_ = Flush(ch);
    if (!OK(error_)) return error_;
    end_[ch] = begin;
  }
  size_t at = 0;
  while (at < encoded.size()) {
    const auto take = std::min((encoded.size() - at) / width,
                               (kBinaryChunkBytes - pending_[ch].size()) / 2);
    if (width == 2)
      pending_[ch].append(encoded.substr(at, take * 2));
    else {
      for (size_t i = 0; i < take; ++i) {
        const auto sample = uint16_t(int32_t(
            DecodeRealtimeSample(format_.encoding, uint8_t(encoded[at + i])) *
            32768.f));
        pending_[ch].push_back(char(sample & 255));
        pending_[ch].push_back(char(sample >> 8));
      }
    }
    end_[ch] += take;
    at += take * width;
    if (pending_[ch].size() == kBinaryChunkBytes) {
      error_ = Flush(ch);
      if (!OK(error_)) return error_;
    }
  }
  return {};
}
ObjectReply LiveRecordingWriter::Finish(uint64_t end_ticks,
                                        LiveRecordingLayout* layout) {
  if (!OK(error_)) return error_;
  if (done_ || !layout || end_ticks > kRealtimeSessionTicks) return Invalid();
  const uint64_t step = kRealtimeTicksPerSecond / format_.rate;
  layout_.frames = (end_ticks + step - 1) / step;
  if (layout_.frames < std::max(end_[0], end_[1])) return error_ = Invalid();
  for (size_t ch = 0; ch < 2; ++ch) {
    error_ = Flush(ch);
    if (!OK(error_)) return error_;
  }
  done_ = true;
  *layout = std::move(layout_);
  return {};
}

LiveRecordingWaveReader::LiveRecordingWaveReader(LiveRecordingLayout layout,
                                                 Read read)
    : layout_(std::move(layout)), read_(std::move(read)) {}
ObjectReply LiveRecordingWaveReader::Next(std::string* bytes) {
  if (failed_ || !bytes || !read_ || !Rate(layout_.rate) ||
      layout_.frames > 3600ULL * layout_.rate)
    return Corrupt();
  if (!header_) {
    if (!audio::PcmWaveHeader(layout_.rate, 2, layout_.frames, Bytes(), bytes))
      return Corrupt();
    header_ = true;
    return {};
  }
  const auto frames = std::min<uint64_t>(16384, layout_.frames - frame_);
  std::string block(frames * 4, '\0');
  for (size_t ch = 0; ch < 2; ++ch) {
    const auto& track = layout_.tracks[ch];
    while (span_[ch] < track.size()) {
      const auto& part = track[span_[ch]];
      if (part.frame >= frame_ + frames) break;
      if (pcm_[ch].empty()) {
        auto reply = read_(part.chunk, &pcm_[ch]);
        if (!OK(reply) || pcm_[ch].size() != uint64_t(part.frames) * 2) {
          failed_ = true;
          return OK(reply) ? Corrupt() : reply;
        }
      }
      const auto stop = std::min(part.frame + part.frames, frame_ + frames);
      for (uint64_t i = std::max(frame_, part.frame); i < stop; ++i) {
        const auto at = (i - frame_) * 4 + ch * 2, from = (i - part.frame) * 2;
        block[at] = pcm_[ch][from];
        block[at + 1] = pcm_[ch][from + 1];
      }
      if (stop < part.frame + part.frames) break;
      pcm_[ch].clear();
      ++span_[ch];
    }
  }
  frame_ += frames;
  *bytes = std::move(block);
  return {};
}
}  // namespace q4t::server

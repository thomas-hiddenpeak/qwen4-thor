#include "q4t/audio/diarization.h"

#include <algorithm>
#include <cmath>
#include <numeric>
#include <set>

namespace q4t::audio {
namespace {
Status Interrupted() { return Status::Fail("speaker operation cancelled"); }
bool Normalize(const SpeakerEmbedding& source, SpeakerEmbedding* out) {
  double square = 0;
  for (double value : source) {
    if (!std::isfinite(value)) return false;
    square += value * value;
  }
  if (square < 1e-12) return false;
  const double scale = 1. / std::sqrt(square);
  for (size_t i = 0; i < source.size(); ++i) (*out)[i] = source[i] * scale;
  return true;
}
double Cosine(const SpeakerEmbedding& a, const SpeakerEmbedding& b) {
  double dot = 0;
  for (size_t i = 0; i < a.size(); ++i) dot += double(a[i]) * b[i];
  return std::clamp(dot, -1., 1.);
}
std::string UnknownName(size_t index) {
  std::string name;
  do {
    name += char('A' + index % 26);
    index /= 26;
    if (!index) break;
    --index;
  } while (true);
  std::reverse(name.begin(), name.end());
  return name;
}
}  // namespace

Status PlanSpeakerWindows(std::span<const VadChunk> chunks,
                          size_t total_samples,
                          const SpeakerWindowOptions& options,
                          std::vector<SpeakerWindow>* out,
                          const std::function<bool()>& cancelled) {
  if (!out || !total_samples || total_samples > kVadMaxSamples ||
      options.window_samples < 16000 || options.window_samples > 480000 ||
      options.step_samples < 1600 ||
      options.step_samples > options.window_samples || !options.max_windows ||
      options.max_windows > 1024 || chunks.size() > 1024)
    return Status::Fail("speaker window limits invalid");
  if (cancelled && cancelled()) return Interrupted();
  std::vector<VadChunk> regions;
  size_t previous = 0;
  bool join = false;
  for (const auto& chunk : chunks) {
    if (cancelled && cancelled()) return Interrupted();
    if (chunk.begin_sample < previous ||
        chunk.end_sample <= chunk.begin_sample ||
        chunk.end_sample > total_samples)
      return Status::Fail("speaker VAD source interval invalid");
    if (join && chunk.begin_sample == previous)
      regions.back().end_sample = chunk.end_sample;
    else
      regions.push_back(chunk);
    previous = chunk.end_sample;
    join = chunk.forced_split;
  }
  std::vector<SpeakerWindow> result;
  for (const auto& region : regions) {
    size_t begin = region.begin_sample;
    const size_t first = result.size();
    const size_t length = region.end_sample - region.begin_sample;
    const size_t width = std::min(length, options.window_samples);
    while (true) {
      if (cancelled && cancelled()) return Interrupted();
      if (result.size() >= options.max_windows)
        return Status::Fail("speaker window capacity exhausted");
      result.push_back({begin, begin + width, begin, begin + width});
      if (begin + width == region.end_sample) break;
      begin = std::min(begin + options.step_samples, region.end_sample - width);
    }
    // The center sum is bounded by four times kVadMaxSamples. Every core
    // remains inside its acoustic window, with exact, nonoverlapping edges.
    for (size_t i = first + 1; i < result.size(); ++i) {
      const auto& prior = result[i - 1];
      const auto& next = result[i];
      const size_t edge = (prior.begin_sample + prior.end_sample +
                           next.begin_sample + next.end_sample) /
                          4;
      result[i - 1].core_end_sample = edge;
      result[i].core_begin_sample = edge;
    }
  }
  if (cancelled && cancelled()) return Interrupted();
  *out = std::move(result);
  return {};
}

Status ClusterSpeakerWindows(std::span<const SpeakerWindow> windows,
                             std::span<const SpeakerEmbedding> embeddings,
                             std::span<const SpeakerReference> references,
                             const SpeakerClusteringOptions& options,
                             std::vector<SpeakerTurn>* out,
                             const std::function<bool()>& cancelled) {
  if (!out || windows.size() != embeddings.size() || !options.max_windows ||
      options.max_windows > 1024 || windows.size() > options.max_windows ||
      !options.max_speakers || options.max_speakers > 64 ||
      references.size() > 4 || !std::isfinite(options.merge_cosine) ||
      options.merge_cosine <= 0 || options.merge_cosine >= 1 ||
      !std::isfinite(options.reference_cosine) ||
      options.reference_cosine <= 0 || options.reference_cosine >= 1 ||
      !std::isfinite(options.reference_margin) ||
      options.reference_margin < 0 || options.reference_margin > 1)
    return Status::Fail("speaker clustering limits or shape invalid");
  if (cancelled && cancelled()) return Interrupted();
  const size_t count = windows.size();
  std::vector<SpeakerEmbedding> vectors(count), known(references.size());
  std::set<std::string> names;
  for (size_t i = 0; i < references.size(); ++i) {
    const auto& reference = references[i];
    if (reference.name.empty() || reference.name.size() > 256 ||
        reference.name.find('\0') != std::string::npos ||
        !names.insert(reference.name).second ||
        !Normalize(reference.embedding, &known[i]))
      return Status::Fail("speaker reference name or embedding invalid");
  }
  size_t previous = 0;
  for (size_t i = 0; i < count; ++i) {
    if (cancelled && cancelled()) return Interrupted();
    const auto& w = windows[i];
    if (w.begin_sample > w.core_begin_sample ||
        w.core_begin_sample < previous ||
        w.core_end_sample <= w.core_begin_sample ||
        w.end_sample < w.core_end_sample || w.end_sample > kVadMaxSamples ||
        w.end_sample - w.begin_sample > 480000 ||
        !Normalize(embeddings[i], &vectors[i]))
      return Status::Fail("speaker acoustic window or embedding invalid");
    previous = w.core_end_sample;
  }
  std::vector<float> distances(count * count);
  for (size_t i = 0; i < count; ++i) {
    if (cancelled && cancelled()) return Interrupted();
    for (size_t j = i + 1; j < count; ++j)
      distances[i * count + j] = distances[j * count + i] =
          1. - Cosine(vectors[i], vectors[j]);
  }
  std::vector<size_t> parent(count), sizes(count, 1);
  std::iota(parent.begin(), parent.end(), 0);
  const auto root = [&](size_t index) {
    while (parent[index] != index) {
      parent[index] = parent[parent[index]];
      index = parent[index];
    }
    return index;
  };
  size_t clusters = count;
  while (clusters > 1) {
    double best = 2.;
    size_t a = count, b = count;
    for (size_t i = 0; i < count; ++i) {
      if (cancelled && cancelled()) return Interrupted();
      if (!sizes[i]) continue;
      for (size_t j = i + 1; j < count; ++j) {
        if (sizes[j] && distances[i * count + j] < best) {
          best = distances[i * count + j];
          a = i;
          b = j;
        }
      }
    }
    if (a == count || best > 1. - options.merge_cosine) break;
    for (size_t j = 0; j < count; ++j) {
      if (!sizes[j] || j == a || j == b) continue;
      distances[a * count + j] = distances[j * count + a] =
          (double(sizes[a]) * distances[a * count + j] +
           double(sizes[b]) * distances[b * count + j]) /
          (sizes[a] + sizes[b]);
    }
    parent[b] = a;
    sizes[a] += sizes[b];
    sizes[b] = 0;
    --clusters;
  }
  // Assign reference names before counting distinct voices: disconnected
  // clusters can legitimately match the same caller-provided speaker.
  std::vector<std::string> labels(count);
  std::set<std::string> assigned;
  size_t next_unknown = 0;
  for (size_t cluster = 0; cluster < count; ++cluster) {
    if (cancelled && cancelled()) return Interrupted();
    if (!sizes[cluster]) continue;
    std::array<double, 192> sum{};
    for (size_t i = 0; i < count; ++i) {
      if (root(i) != cluster) continue;
      for (size_t d = 0; d < sum.size(); ++d) sum[d] += vectors[i][d];
    }
    SpeakerEmbedding centroid{}, normalized;
    for (size_t d = 0; d < sum.size(); ++d)
      centroid[d] = sum[d] / sizes[cluster];
    if (!Normalize(centroid, &normalized))
      return Status::Fail("speaker cluster has degenerate centroid");
    double best = -2, second = -2;
    size_t match = known.size();
    for (size_t i = 0; i < known.size(); ++i) {
      const double score = Cosine(normalized, known[i]);
      if (score > best) {
        second = best;
        best = score;
        match = i;
      } else {
        second = std::max(second, score);
      }
    }
    std::string label;
    if (match != known.size() && best >= options.reference_cosine) {
      if (second >= options.reference_cosine &&
          best - second <= options.reference_margin)
        return Status::Fail("speaker reference match is ambiguous");
      label = references[match].name;
    } else {
      do {
        label = UnknownName(next_unknown++);
      } while (names.contains(label));
    }
    assigned.insert(label);
    if (assigned.size() > options.max_speakers)
      return Status::Fail("speaker identity capacity exhausted");
    labels[cluster] = std::move(label);
  }
  std::vector<SpeakerTurn> result;
  for (size_t i = 0; i < count; ++i) {
    if (cancelled && cancelled()) return Interrupted();
    const auto& label = labels[root(i)];
    const auto& window = windows[i];
    if (!result.empty() && result.back().speaker == label &&
        result.back().end_sample == window.core_begin_sample)
      result.back().end_sample = window.core_end_sample;
    else
      result.push_back(
          {window.core_begin_sample, window.core_end_sample, label});
  }
  if (cancelled && cancelled()) return Interrupted();
  *out = std::move(result);
  return {};
}
}  // namespace q4t::audio

#include "noise_fft.h"

#include <algorithm>
#include <cmath>
#include <numbers>

namespace q4t::audio::detail {
void NoiseFft(NoiseSpectrum* values, bool inverse) {
  auto& a = *values;
  for (size_t i = 1, j = 0; i < a.size(); ++i) {
    size_t bit = a.size() >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) std::swap(a[i], a[j]);
  }
  for (size_t width = 2; width <= a.size(); width <<= 1) {
    const double angle = (inverse ? 2 : -2) * std::numbers::pi / width;
    const std::complex<double> step(std::cos(angle), std::sin(angle));
    for (size_t begin = 0; begin < a.size(); begin += width) {
      std::complex<double> phase(1, 0);
      for (size_t k = 0; k < width / 2; ++k) {
        const auto even = a[begin + k];
        const auto odd = phase * a[begin + k + width / 2];
        a[begin + k] = even + odd;
        a[begin + k + width / 2] = even - odd;
        phase *= step;
      }
    }
  }
  if (inverse)
    for (auto& value : a) value /= double(a.size());
}
}  // namespace q4t::audio::detail

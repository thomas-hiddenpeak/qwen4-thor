// Fixed radix-2 DFT primitive. Standard equations, no external FFT library.
#pragma once

#include <array>
#include <complex>

namespace q4t::audio::detail {
inline constexpr size_t kNoiseFft = 512;
using NoiseSpectrum = std::array<std::complex<double>, kNoiseFft>;
void NoiseFft(NoiseSpectrum* values, bool inverse);
}  // namespace q4t::audio::detail

// Included after the independent state fixture. No production range builder
// is used to construct the scoring reference or its pooled-state snapshots.
namespace {
void ScoringContract(Fixture& f) {
  auto& d = f.draft;
  const size_t pool = 47104;
  auto capture = [&] {
    Image result;
    for (auto [p, n] : std::initializer_list<std::pair<void*, size_t>>{
             {d.kv_cache, pool * 2048}, {d.idx_raw, pool * 256},
             {d.idx_comp, pool * 256}, {d.page_table, 4u * 16384 * 4},
             {d.d_rope_pos, 12u * 16384 * 4}})
      result.push_back({p, Read(p, n)});
    return result;
  };
  // Finite, distinct per-slot values exercise foreign-slice selection. All
  // capacities, page-table offsets and RoPE addresses are enumerated here.
  for (auto [p, width] :
       std::initializer_list<std::pair<uint16_t*, size_t>>{
           {d.kv_cache, 1024}, {d.idx_raw, 128}, {d.idx_comp, 128}}) {
    std::vector<uint16_t> values(pool * width);
    for (size_t i = 0; i < values.size(); ++i)
      values[i] = 0x3b80 + ((i * 37 + i / width * 13) % 768);
    Cuda(cudaMemcpy(p, values.data(), values.size() * 2,
                    cudaMemcpyHostToDevice));
  }
  const auto initial = capture();
  struct Case { int slot, base, n; };
  for (const auto c : {Case{3, 0, 17}, Case{2, 8190, 17},
                       Case{1, 8192, 17}, Case{0, 12288, 4096}}) {
    Buffer x(size_t(c.n) * 2560), out(size_t(c.n) * 2560);
    Buffer pos(size_t(c.n) * 2), slots(size_t(c.n) * 2);
    const size_t ws_bytes = model::FullAttentionWorkspaceBytes(d.full_attn,
                                                               c.n);
    Buffer ws((ws_bytes + 1) / 2);
    std::vector<uint16_t> input(size_t(c.n) * 2560);
    for (size_t i = 0; i < input.size(); ++i)
      input[i] = 0x3c00 + ((i * 17 + i / 2560) % 512);
    std::vector<int> positions(c.n), ids(c.n, c.slot);
    std::iota(positions.begin(), positions.end(), c.base);
    Cuda(cudaMemcpy(x.p, input.data(), input.size() * 2,
                    cudaMemcpyHostToDevice));
    Cuda(cudaMemcpy(pos.p, positions.data(), c.n * 4, cudaMemcpyHostToDevice));
    Cuda(cudaMemcpy(slots.p, ids.data(), c.n * 4, cudaMemcpyHostToDevice));
    const size_t offset = std::accumulate(f.caps.begin(),
                                         f.caps.begin() + c.slot, size_t{0});
    std::vector<uint8_t> reference;
    Image expected;
    for (int mode = 0; mode < 3; ++mode) {
      Put(initial);
      const bool sliced = mode == 0;
      Check(model::FullAttentionForward(d.full_attn, x.p, out.p,
          reinterpret_cast<int*>(pos.p),
          d.d_rope_pos + (sliced ? c.slot * 3 * 16384 : 0),
          d.kv_cache, d.page_table + (sliced ? c.slot * 16384 : 0),
          d.idx_raw + (sliced ? offset * 128 : 0),
          d.idx_comp + (sliced ? offset * 128 : 0), c.n, ws.p, ws_bytes,
          nullptr, sliced ? nullptr : reinterpret_cast<int*>(slots.p),
          c.base + c.n - 1, -1,
          mode == 1 ? d.idx_comp + offset * 128 : nullptr));
      Cuda(cudaStreamSynchronize(nullptr));
      const auto output = Read(out.p, input.size() * 2);
      const auto state = capture();
      const auto* bf = reinterpret_cast<const uint16_t*>(output.data());
      for (size_t i = 0; i < input.size(); ++i)
        Require((bf[i] & 0x7f80) != 0x7f80, "nonfinite attention output");
      if (mode == 0) {
        reference = output;
        expected = state;
      } else {
        Equal(state, expected);
        if (mode == 1)
          Require(output == reference, "independent sliced GEMM reference");
        else {
          size_t diff = 0;
          for (size_t i = 0; i < output.size(); ++i)
            diff += output[i] != reference[i];
          std::printf("SCORING_LEGACY slot=%d base=%d rows=%d "
                      "differing_output_bytes=%zu state_exact=1\n",
                      c.slot, c.base, c.n, diff);
        }
      }
    }
    // The reference must itself preserve every other slot, all metadata,
    // and all cache positions outside the explicitly written rows/groups.
    for (size_t field = 0; field < expected.size(); ++field) {
      for (size_t i = 0; i < expected[field].bytes.size(); ++i) {
        bool writable = false;
        if (field < 3) {
          const size_t width = field == 0 ? 2048 : 256;
          const size_t lo = offset + (field == 2 ? c.base / 4 : c.base);
          const size_t hi = offset + (field == 2 ?
              (c.base + c.n) / 4 : c.base + c.n);
          writable = i >= lo * width && i < hi * width;
        }
        if (!writable)
          Require(expected[field].bytes[i] == initial[field].bytes[i],
                  "peer slot or readonly metadata changed");
      }
    }
    std::printf("PASS scoring_reference slot=%d base=%d rows=%d exact=1\n",
                c.slot, c.base, c.n);
    std::fflush(stdout);
  }
  Put(initial);
}
}  // namespace

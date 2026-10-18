from pathlib import Path
p=Path('tests/mtp_spec_multi_test.cpp');s=p.read_text().replace('#include <fcntl.h>','#include <fcntl.h>\n#include <fstream>\n#include <stdexcept>')
pos=s.index('bool RunMultiStep(')
s=s[:pos]+'''// Optional raw contract artifacts for parent/candidate byte-for-byte comparison.
// Only initialized outputs/state are serialized; scratch padding is excluded.
void WriteDevice(std::ofstream& out, const void* pointer, size_t bytes) {
  std::vector<char> data(bytes);
  if (cudaMemcpy(data.data(), pointer, bytes, cudaMemcpyDeviceToHost) !=
      cudaSuccess)
    throw std::runtime_error("contract state readback failed");
  out.write(data.data(), data.size());
}
template <typename T>
void WriteVector(std::ofstream& out, const std::vector<T>& values) {
  const size_t count = values.size();
  out.write(reinterpret_cast<const char*>(&count), sizeof(count));
  out.write(reinterpret_cast<const char*>(values.data()), count * sizeof(T));
}

'''+s[pos:]
s=s.replace('bool accept_first = false) {','bool accept_first = false, int broken_offsets = 0) {')
s=s.replace('  mcfg.max_seq = B;','  mcfg.max_seq = B;\n  mcfg.max_len = 64;\n  mcfg.max_prefill = 16;')
s=s.replace('  // 6. The batched speculative step.','''  // 6. Reuse the exact same scratch over consecutive full-accept steps.
  int* const offset_pointer = mtp.d_ms_gather_off;
  const size_t offset_capacity = mtp.gather_off_capacity;
  Q4T_CHECK(offset_pointer && offset_capacity == size_t(B * (k + 1)));
  for (int b = 0; b < B; ++b)
    Q4T_CHECK(cudaMemset(next_g[b], 0, hc_dim * 2) == cudaSuccess);
  const int steps = zero_head ? 3 : 1;
  bool ok = true;
  for (int step = 0; step < steps; ++step) {
  // The batched speculative step.''')
s=s.replace('  s = MtpSpeculativeStepMulti(m, mtp, seq_ptrs,', '''  if (broken_offsets == 1) mtp.d_ms_gather_off = nullptr;
  if (broken_offsets == 2) mtp.gather_off_capacity = 0;
  s = MtpSpeculativeStepMulti(m, mtp, seq_ptrs,''',1)
s=s.replace('  if (!s.ok()) {\n    std::printf("  MtpSpeculativeStepMulti failed:', '''  mtp.d_ms_gather_off = offset_pointer;
  mtp.gather_off_capacity = offset_capacity;
  if (broken_offsets) {
    Q4T_CHECK(!s.ok());
    Q4T_CHECK(s.message().find("gather offset scratch capacity") !=
              std::string::npos);
    Q4T_CHECK(cudaStreamQuery(nullptr) == cudaSuccess);
    for (int b = 0; b < B; ++b)
      Q4T_CHECK(accepted_count[b] == 0 && next_b[b] == -1 && next_d0[b] == -1);
    break;
  }
  if (!s.ok()) {
    std::printf("  MtpSpeculativeStepMulti failed:''',1)
s=s.replace('  // The step does NOT advance', '''  Q4T_CHECK(mtp.d_ms_gather_off == offset_pointer);
  Q4T_CHECK(mtp.gather_off_capacity == offset_capacity);
  if (const char* directory = std::getenv("Q4T_MTP_CONTRACT_DIR")) {
    const std::string name = std::to_string(k) + "-" +
        std::to_string(terminal_sequences) + "-" + std::to_string(zero_head) +
        "-" + std::to_string(accept_first) + "-" + std::to_string(step);
    std::ofstream out(std::string(directory) + "/" + name + ".bin",
                      std::ios::binary);
    WriteVector(out, accepted);
    WriteVector(out, accepted_count);
    WriteVector(out, next_b);
    WriteVector(out, next_d0);
    q4t::model::ModelStateSnapshot recurrent;
    Q4T_CHECK(q4t::model::ModelSnapshotState(m, &recurrent, nullptr).ok());
    WriteVector(out, recurrent.data);
    WriteDevice(out, mtp.kv_cache, mtp.kv_bytes * B);
    WriteDevice(out, mtp.idx_raw, mtp.idx_bytes * B);
    WriteDevice(out, mtp.idx_comp, mtp.idx_bytes * B);
    WriteDevice(out, mtp.d_ms_vlogits, size_t(B * (k + 1)) * vocab * 2);
    WriteDevice(out, mtp.d_ms_vtrunk, size_t(B * (k + 1)) * hc_dim * 2);
    int rows = 0;
    for (int b = 0; b < B; ++b) {
      WriteDevice(out, next_g[b], hc_dim * 2);
      WriteVector(out, seqs[b].history);
      out.write(reinterpret_cast<const char*>(&seqs[b].position), sizeof(int));
      if (next_d0[b] != -1) rows += accepted_count[b];
    }
    if (rows) {
      WriteDevice(out, mtp.d_ms_ext_logits, size_t(rows) * vocab * 2);
      WriteDevice(out, mtp.d_ms_ext_multi, size_t(rows) * hc_dim * 2);
      WriteDevice(out, mtp.d_ms_ext_sample, size_t(rows) * hs * 2);
      WriteDevice(out, mtp.d_ms_gather, size_t(rows) * hc_dim * 2);
    }
    Q4T_CHECK(out.good());
  }
  // The step does NOT advance''',1)
s=s.replace('Q4T_CHECK(seqs[b].position == (b == 0 ? T0 : T1) + count);','Q4T_CHECK(seqs[b].position ==\n              (b == 0 ? T0 : T1) + (zero_head ? step * (k + 1) : 0) + count);')
s=s.replace('  bool ok = true;\n  for (int b = 0; b < B; ++b) {','  for (int b = 0; b < B; ++b) {')
s=s.replace('  if (ok) std::printf("  PASS:', '''  if (zero_head) {
    b_tok = next_b;
    d0 = next_d0;
    for (int b = 0; b < B; ++b)
      Q4T_CHECK(cudaMemcpy(g_in[b], next_g[b], hc_dim * 2,
                           cudaMemcpyDeviceToDevice) == cudaSuccess);
  }
  }
  if (ok) std::printf("  PASS:''')
s=s.replace('Q4T_TEST(mtp_spec_multi_full_k3)', '''Q4T_TEST(mtp_spec_multi_full_k2) { return RunMultiStep(2, 0, true); }
Q4T_TEST(mtp_spec_multi_offsets_null) {
  return RunMultiStep(3, 0, false, false, 1);
}
Q4T_TEST(mtp_spec_multi_offsets_capacity) {
  return RunMultiStep(3, 0, false, false, 2);
}
Q4T_TEST(mtp_spec_multi_full_k3)''')
p.write_text(s)

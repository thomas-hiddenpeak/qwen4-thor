// Differential test: C++ BuildRopePositions vs Python mrope_ref.py.
// Build & run:
//   g++ -std=c++17 -O2 -o /tmp/mrope_diff_test tools/mrope_diff_test.cpp
//   /tmp/mrope_diff_test
// Compares the exact algorithm from src/model/model.cu against the
// Python reference (tools/mrope_ref.py) on mixed text+vision sequences.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

// ---- Verbatim copy of BuildRopePositions from src/model/model.cu ----
static int BuildRopePositions(const int32_t* input_ids, int T, int m,
                              int img_id, int vid_id, int max_len,
                              const std::vector<std::array<int, 3>>& grids,
                              std::vector<int>* rope_pos) {
  rope_pos->assign(3 * max_len, 0);
  int clock = 0;
  int gi = 0;
  int t = 0;
  int maxv = 0;
  while (t < T) {
    const int32_t id = input_ids[t];
    if (id != img_id && id != vid_id) {
      int run = 0;
      while (t + run < T) {
        const int32_t d = input_ids[t + run];
        if (d == img_id || d == vid_id) break;
        ++run;
      }
      for (int j = 0; j < run; ++j) {
        const int v = clock + j;
        (*rope_pos)[t + j] = v;
        (*rope_pos)[max_len + (t + j)] = v;
        (*rope_pos)[2 * max_len + (t + j)] = v;
        if (v > maxv) maxv = v;
      }
      clock += run;
      t += run;
      continue;
    }
    if (gi >= static_cast<int>(grids.size())) {
      const int v = t;
      (*rope_pos)[t] = v;
      (*rope_pos)[max_len + t] = v;
      (*rope_pos)[2 * max_len + t] = v;
      if (v > maxv) maxv = v;
      ++t;
      continue;
    }
    const int gt = grids[gi][0];
    const int gh = grids[gi][1];
    const int gw = grids[gi][2];
    const int mh = gh / m, mw = gw / m;
    const int per_frame = mh * mw;
    const int n = gt * per_frame;
    for (int k = 0; k < n && t + k < T; ++k) {
      const int tc = k / per_frame;
      const int rem = k % per_frame;
      const int hc = rem / mw;
      const int wc = rem % mw;
      const int idx = t + k;
      (*rope_pos)[idx] = clock + tc;
      (*rope_pos)[max_len + idx] = clock + hc;
      (*rope_pos)[2 * max_len + idx] = clock + wc;
      const int v = clock + tc;
      if (v > maxv) maxv = v;
    }
    clock += (std::max(gh, gw) / m);
    t += n;
    ++gi;
  }
  return maxv + 1 - T;
}

// ---- Python reference values (from tools/mrope_ref.py, m=2) ----
// parts = [text 4, vision (1,4,4), text 3, vision (2,4,4), text 2]
// T=21, delta=-8
// mrope[0]: [0,1,2,3, 4,4,4,4, 6,7,8, 9,9,9,9, 10,10,10,10, 11,12]
// mrope[1]: [0,1,2,3, 4,4,5,5, 6,7,8, 9,9,10,10, 9,9,10,10, 11,12]
// mrope[2]: [0,1,2,3, 4,5,4,5, 6,7,8, 9,10,9,10, 9,10,9,10, 11,12]
static const int kT = 21;
static const int kMaxLen = 64;  // must be >= T
static const int kImgId = 248056;
static const int kVidId = 248057;
static const int kDelta = -8;
static const int kRefT[21] = {0,1,2,3, 4,4,4,4, 6,7,8, 9,9,9,9, 10,10,10,10, 11,12};
static const int kRefH[21] = {0,1,2,3, 4,4,5,5, 6,7,8, 9,9,10,10, 9,9,10,10, 11,12};
static const int kRefW[21] = {0,1,2,3, 4,5,4,5, 6,7,8, 9,10,9,10, 9,10,9,10, 11,12};

// Build the input_ids: text(4) + img(1) + text(3) + vid(1) + text(2)
// The placeholder is a single token (img_id or vid_id) that the
// BuildRopePositions function expands using the grid.
static void BuildInputIds(int32_t* ids, int* n) {
  int i = 0;
  for (int j = 0; j < 4; ++j) ids[i++] = 100 + j;  // text
  ids[i++] = kImgId;  // image placeholder
  for (int j = 0; j < 3; ++j) ids[i++] = 200 + j;  // text
  ids[i++] = kVidId;  // video placeholder
  for (int j = 0; j < 2; ++j) ids[i++] = 300 + j;  // text
  *n = i;
}

int main() {
  int32_t ids[64];
  int n = 0;
  BuildInputIds(ids, &n);
  // n should be 11 (4+1+3+1+2), but BuildRopePositions expands the
  // placeholders using the grids. The T parameter is the FULL sequence
  // length AFTER expansion. Let's compute it:
  // text(4) + vision(1,4,4) m=2 -> 1*2*2=4 + text(3) + vision(2,4,4) m=2
  // -> 2*2*2=8 + text(2) = 4+4+3+8+2 = 21
  // But BuildRopePositions takes the RAW input_ids (with placeholders) and
  // T = raw length. The expansion happens internally.
  // Wait - looking at the code more carefully: the function iterates over
  // input_ids[0..T-1] and when it hits img_id/vid_id, it consumes `n`
  // positions (from the grid). So T must be the EXPANDED length, and the
  // input_ids must have the placeholder repeated n times? No...
  //
  // Actually, looking at the code: when it hits a vision token, it does
  // `t += n` where n = gt * per_frame. So it skips n positions in the
  // input_ids array. This means the input_ids must have n placeholder
  // tokens (or at least n positions) for each vision block.
  //
  // But in practice (model.cu RunPrefill), the input_ids are the FULL
  // tokenized sequence where the image placeholder is repeated for each
  // merged token. So T = 21 and input_ids has 21 entries where positions
  // 4-7 are img_id and positions 11-18 are vid_id.
  //
  // Let me rebuild:
  int i2 = 0;
  for (int j = 0; j < 4; ++j) ids[i2++] = 100 + j;  // text[0..3]
  for (int j = 0; j < 4; ++j) ids[i2++] = kImgId;   // img[4..7] (4 tokens)
  for (int j = 0; j < 3; ++j) ids[i2++] = 200 + j;  // text[8..10]
  for (int j = 0; j < 8; ++j) ids[i2++] = kVidId;   // vid[11..18] (8 tokens)
  for (int j = 0; j < 2; ++j) ids[i2++] = 300 + j;  // text[19..20]
  n = i2;  // should be 21

  std::vector<std::array<int, 3>> grids = {
      {1, 4, 4},  // image: T=1, H=4, W=4
      {2, 4, 4},  // video: T=2, H=4, W=4
  };

  std::vector<int> rope_pos;
  int delta = BuildRopePositions(ids, n, 2, kImgId, kVidId, kMaxLen, grids,
                                 &rope_pos);

  int errors = 0;
  if (n != kT) {
    std::printf("FAIL: T=%d expected %d\n", n, kT);
    ++errors;
  }
  if (delta != kDelta) {
    std::printf("FAIL: delta=%d expected %d\n", delta, kDelta);
    ++errors;
  }
  for (int t = 0; t < kT; ++t) {
    int cpp_t = rope_pos[t];
    int cpp_h = rope_pos[kMaxLen + t];
    int cpp_w = rope_pos[2 * kMaxLen + t];
    if (cpp_t != kRefT[t]) {
      std::printf("FAIL: t[%d]=%d expected %d\n", t, cpp_t, kRefT[t]);
      ++errors;
    }
    if (cpp_h != kRefH[t]) {
      std::printf("FAIL: h[%d]=%d expected %d\n", t, cpp_h, kRefH[t]);
      ++errors;
    }
    if (cpp_w != kRefW[t]) {
      std::printf("FAIL: w[%d]=%d expected %d\n", t, cpp_w, kRefW[t]);
      ++errors;
    }
  }

  // Decode rule: for a text token at logical p, mrope rows == p + delta.
  // Check the last text token (logical 20).
  int p = kT - 1;
  int dec = p + delta;
  if (rope_pos[p] != dec || rope_pos[kMaxLen + p] != dec ||
      rope_pos[2 * kMaxLen + p] != dec) {
    std::printf("FAIL: decode rule at p=%d: got (%d,%d,%d) expected (%d,%d,%d)\n",
                p, rope_pos[p], rope_pos[kMaxLen + p], rope_pos[2 * kMaxLen + p],
                dec, dec, dec);
    ++errors;
  }

  // Pure-text sanity: no vision -> delta 0, mrope == logical.
  {
    int32_t text_ids[10];
    for (int j = 0; j < 10; ++j) text_ids[j] = 500 + j;
    std::vector<int> tp;
    int td = BuildRopePositions(text_ids, 10, 2, kImgId, kVidId, kMaxLen, {},
                                &tp);
    if (td != 0) {
      std::printf("FAIL: pure-text delta=%d expected 0\n", td);
      ++errors;
    }
    for (int j = 0; j < 10; ++j) {
      if (tp[j] != j || tp[kMaxLen + j] != j || tp[2 * kMaxLen + j] != j) {
        std::printf("FAIL: pure-text pos[%d]=(%d,%d,%d) expected (%d,%d,%d)\n",
                    j, tp[j], tp[kMaxLen + j], tp[2 * kMaxLen + j], j, j, j);
        ++errors;
      }
    }
  }

  if (errors == 0) {
    std::printf("PASS: all %d positions match Python reference, delta=%d, "
                "decode rule holds, pure-text OK\n",
                kT * 3, delta);
    return 0;
  }
  std::printf("FAIL: %d errors\n", errors);
  return 1;
}

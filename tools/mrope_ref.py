#!/usr/bin/env python3
"""Reference for the qwen4_exp 3D MRoPE position computation.

Mirrors transformers 5.16.1 Qwen4ExpTextRotaryEmbedding + get_rope_index +
get_vision_position_ids (see reference/.venv-tokenizers/.../qwen4_exp/
modeling_qwen4_exp.py). Used to cross-check the C++ implementation.

The LLM consumes a 4-row position_ids:
  row 0 = logical positions 0..T-1  (causal mask, KV paging, indexer groups)
  rows 1-3 = (t, h, w) 3D mrope     (ALL RoPE: main attn Q/K, indexer Q/K,
                                     compressed keys)

For a vision block with grid (T, H, W) and spatial_merge_size m:
  - it occupies T*(H/m)*(W/m) tokens in the sequence (the ViT merged output)
  - merged token k (0-based, t-major / block-major spatial):
        t = k // ((H/m)*(W/m))
        h = (k % ((H/m)*(W/m))) // (W/m)
        w = k % (W/m)
  - the 3 mrope rows for that token are (t+start, h+start, w+start)
  - after the block, the text clock advances by max(H, W) // m  (NOT the
    token count) -> this is what creates the rope delta.

For a text run of length L starting at text-clock `c`:
  - logical positions are the sequence indices (0..T-1 overall)
  - mrope rows are all (c, c+1, ..., c+L-1)  (t=h=w)
  - after the run, c += L.

rope_delta = max(mrope row) + 1 - T. During incremental decode at logical
position p, the 3 mrope rows are all (p + rope_delta).
"""
import numpy as np


def vision_block_positions(start, T, H, W, m):
  """Return (n_tokens, (3, n_tokens) mrope rows) for one vision block.

  Mirrors get_vision_position_ids(start_position=start, grid_thw=(T,H,W),
  temp_merge_size=1, spatial_merge_size=m).
  """
  gt, gh, gw = T, H // m, W // m
  n = gt * gh * gw
  pos_t = np.arange(gt)
  pos_h = np.arange(gh) + start
  pos_w = np.arange(gw) + start
  tt, hh, ww = np.meshgrid(pos_t, pos_h, pos_w, indexing="ij")
  out = np.stack([tt, hh, ww], axis=0).reshape(3, -1)
  out[0] += start  # after the time_interval multiply
  return n, out


def compute_mrope(text_runs, vision_grids, m=2):
  """Compute the 3 mrope rows + delta for a sequence.

  text_runs: list of int lengths (text segments, in order)
  vision_grids: list of (T, H, W) grids (in order)
  Returns (mrope (3, T), delta, logical (T,)).
  """
  # Build the token-type sequence (0=text, 1=vision) with lengths.
  segments = []  # (kind, length, grid_or_None)
  # Interleave: we assume the caller passes an ordered list of parts.
  return _compute(parts, m)


def _compute(parts, m):
  """parts: list of ('text', len) or ('vision', (T,H,W))."""
  rows = []  # each (3, n)
  cur = 0  # text clock
  seq_len = 0
  for kind, val in parts:
    if kind == "text":
      L = val
      base = np.arange(L) + cur
      rows.append(np.stack([base, base, base], axis=0))
      cur += L
      seq_len += L
    else:
      T, H, W = val
      n, block = vision_block_positions(cur, T, H, W, m)
      rows.append(block)
      cur += max(H, W) // m
      seq_len += n
  mrope = np.concatenate(rows, axis=1)  # (3, seq_len)
  delta = int(mrope.max() + 1 - seq_len)
  logical = np.arange(seq_len)
  return mrope, delta, logical


def main():
  # Example: "describe (4 text) [image 1x4x4] then (3 text) [video 2x4x4] end (2 text)"
  # image grid (T=1, H=4, W=4), m=2 -> 1*2*2 = 4 tokens
  # video grid (T=2, H=4, W=4), m=2 -> 2*2*2 = 8 tokens
  parts = [
      ("text", 4),
      ("vision", (1, 4, 4)),
      ("text", 3),
      ("vision", (2, 4, 4)),
      ("text", 2),
  ]
  mrope, delta, logical = _compute(parts, m=2)
  T = logical.size
  print(f"T={T} delta={delta}")
  print("logical:", logical.tolist())
  print("mrope[0] (t):", mrope[0].tolist())
  print("mrope[1] (h):", mrope[1].tolist())
  print("mrope[2] (w):", mrope[2].tolist())
  # Verify decode rule: for a text token at logical p, mrope rows == p+delta.
  # Check the last text token (logical T-1).
  p = T - 1
  print(f"\nlast text token logical={p}: mrope rows = {mrope[:, p].tolist()}, "
        f"p+delta = {p + delta}")
  assert (mrope[:, p] == p + delta).all(), "decode rule mismatch"
  # Pure-text sanity: no vision -> delta 0, mrope == logical.
  mrope_t, delta_t, logical_t = _compute([("text", 10)], m=2)
  assert delta_t == 0 and (mrope_t == logical_t[None, :]).all()
  print("\nOK: pure-text delta=0 and mrope==logical; decode rule holds.")


if __name__ == "__main__":
  main()

# Native image detail CPU fixture

Original procedural RGB image (2305x1153). No third-party image or model output.
`generate.py` records exact Pillow 12.3.0 / NumPy 1.26.4 RGB bicubic, float32
normalization and block-major patch hashes, independently of the C++ processor.
`auto` is the existing Qwen reference policy. Other dimensions are explicit
q4t policy examples, not a claim of OpenAI model internals or token prices.
`original` uses only right/bottom edge padding and preserves all source pixels.

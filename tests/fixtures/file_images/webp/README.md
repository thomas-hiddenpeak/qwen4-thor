# Original WebP inputs and independent decoded pixel references

`generate.py` creates procedural RGB/RGBA and fixed-seed noise sources with
Pillow 12.3.0. Lossy, lossless, alpha, metadata, capacity and separate animation
cases are actual WebP bitstreams. PNG references come from Pillow's real WebP
decoder followed by convert("RGB"), never from q4t. Producer, library version
and exact file SHA256 are in identity.json. No external imagery/model/GPU.

# Original file-image fixtures

The 64 × 32 image has a red left half and a green right half. It was created
for this project by `generate.py`; no third-party image was copied. PNG and
JPEG contain the same original RGB scene. The generator version and exact
compressed identities are in `identity.json`.

CPU tests use the real source reader, private snapshot and pixel processor.
They do not claim GPU vision or semantic model acceptance. Decoder numerical
reference tests remain separate; this task checks byte preservation and the
directly affected source/restore boundary.

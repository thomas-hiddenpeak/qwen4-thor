# Format adapter fixtures

These are test-data derivatives of LibreOffice **24.2.7.2**, covered by its
MPL-2.0 source license (included as `COPYING.MPL`). `sources.json` records the
fixed upstream URLs, original and derived SHA256, and every removed member.
They are not renamed OOXML files. No third-party application code is copied.

Pages, Numbers and modern Keynote keep their original document/index and metadata
bytes; photo/texture resources and JPEG previews are omitted. The tests cover
text, sheet coordinates and conversion provenance, not image preservation.
The XLSB fixture is unchanged upstream test data. All source material remains
available at the exact upstream URLs; these fixture modifications are also
under MPL-2.0. This license applies to this data, not the project's own code.

Reproduce with `python3 -B tests/fixtures/file_adapters/prepare.py --output
.q4t-work/adapters-reproduced`. The optional `--downloaded` directory allows
already-downloaded, digest-checked originals. Expected text/cells come from
the originals: Pages `A5 page.`, Numbers B2/C2 and the numeric D2 value 3,
XLSB `String1`/`String2`. Modern Keynote contains one empty slide; its binary
`Transition` string is animation metadata, not visible text. The legacy
Keynote derivative decompresses `index.apxl.gz` and inserts original Unicode
notes into the existing first-slide paragraph. The transform is recorded;
the notes are not passed as separate plaintext input to the decoder.

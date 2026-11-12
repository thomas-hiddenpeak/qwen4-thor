# Original legacy-document fixtures

The flat ODF files here are original project test content. `word.doc`,
`sheet.xls` and `slides.ppt` are actual Microsoft compound binary files
exported from those sources with the system LibreOffice 24.2.7.2. They are
not renamed OOXML files or external documents. Generation records and SHA256
are preserved with the corresponding acceptance evidence.

Reproduce into a new work directory (CPU-only, no model):

```sh
python3 -B tests/fixtures/file_legacy/generate.py \
  --output .q4t-work/legacy-fixtures-regenerated
```

This uses a separate profile per document and disables GPU acceleration.
Only the trusted flat files in this directory are accepted by the generator;
the production decoder uses the independent filesystem/syscall sandbox.
The exports contain timestamps, so newly generated files need not have the
same byte digest. Tests verify original text, real rows/formulas and slide
order. They do not certify every legacy document, embedded object or drawing.

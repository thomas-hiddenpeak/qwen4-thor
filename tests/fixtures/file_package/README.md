# Original document-container fixtures

`python3 -B generate.py` reproduces these small ZIP inputs using only Python's
standard library. They cover deflate, a ZIP64 member, a missing central end
record, duplicate/traversing/symlink names, an encrypted flag, wrong CRC and
an expanded-size limit. Contents and generator are project-original MIT data.
No fixture is extracted to the filesystem by the runtime.

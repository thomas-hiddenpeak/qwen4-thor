# Structured output dependency notices

These notices accompany the optional XGrammar-enabled runner distribution.
Dependency sources are fetched unchanged; they are not vendored here.

| Component | Source identity | Notice |
|---|---|---|
| XGrammar | ca1acf28bf35b9ba9092800b69aebc7d4ce1f7a7 | [Apache 2.0](XGrammar-LICENSE), [copyright](XGrammar-NOTICE) |
| DLPack | bbd2f4d32427e548797929af08cfe2a9cbb3cf12 | [Apache 2.0](DLPack-LICENSE) |
| picojson copy included in XGrammar | same XGrammar source identity | [BSD terms and copyright](picojson-LICENSE) |
| regex-syntax | 0.8.8, archive SHA256 `7a2d987857b319362043e95f5353c0535c1f58eec5336fdfcf626430af7def58` | [MIT](regex-syntax-LICENSE-MIT), [Apache 2.0](regex-syntax-LICENSE-APACHE) |

Custom tool grammar preparation statically links this parser, which has no
transitive dependencies. It implements Rust regex syntax and Unicode classes
inside the existing limited grammar worker. Rust is a build tool, not a deployed
service. No network or external regex process is used at request time.

The statically linked Rust 1.90 standard library notices are retained in
[rust-standard-library](rust-standard-library/COPYRIGHT-library.html).

Keep this directory with exported XGrammar-enabled binaries. The local
2026-11-02 distribution also retains copies under `build/licenses/structured_output/`.

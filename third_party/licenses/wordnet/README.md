# WordNet 3.0 lexical data

The METEOR evaluation metric uses the bundled English lexical data from Princeton WordNet 3.0. The table contains single-token synonym targets and the four parts of speech's inflection exceptions. The database license is reproduced in LICENSE and in the uncompressed table. The table is used for Evals text grading. [Princeton WordNet license](https://wordnet.princeton.edu/license-and-commercial-use)

To reproduce the table, run the following from the repository root using the WordNet 3.0 corpus distributed by NLTK. The generator verifies the SHA-256 of the original archive before reading it. [NLTK WordNet corpus](https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip)

```sh
python3 tools/serve_responses/prepare_meteor_lexicon.py wordnet.zip third_party/wordnet/meteor-3.0.zlib
```

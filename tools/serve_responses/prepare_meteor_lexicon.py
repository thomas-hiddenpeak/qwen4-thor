"""Build the compact English WordNet lexical tables used by the METEOR grader."""
import argparse
from collections import defaultdict
import hashlib
from pathlib import Path
import struct
import zipfile
import zlib

SOURCE_SHA256 = 'cbda5ea6eef7f36a97a43d4a75f85e07fccbb4f23657d27b4ccbc93e2646ab59'
PARTS = ('noun', 'verb', 'adj', 'adv')


def tables(corpus):
    # Store the lexical memberships only. Definitions and semantic relation
    # pointers (hypernyms, antonyms, etc.) do not participate in synonym matches.
    forms = defaultdict(lambda: [set() for _ in PARTS])
    exceptions = defaultdict(lambda: [set() for _ in PARTS])
    for pos, name in enumerate(PARTS):
        synsets = {}
        for row in corpus.read('wordnet/data.'+name).splitlines():
            if not row or row.startswith(b' '):
                continue
            fields = row.partition(b'|')[0].split()
            count = int(fields[3], 16)
            words = fields[4:4+2*count:2]
            if name == 'adj':
                words = [w.split(b'(')[0] if w.endswith((b'(p)', b'(a)', b'(ip)')) else w for w in words]
            synsets[fields[0]] = {w for w in words if b'_' not in w and w == w.lower()}
        for row in corpus.read('wordnet/index.'+name).splitlines():
            if not row or row.startswith(b' '):
                continue
            fields = row.split()
            n = int(fields[2])
            for offset in fields[-n:]:
                forms[fields[0]][pos].update(synsets[offset])
        for row in corpus.read('wordnet/'+name+'.exc').splitlines():
            inflected, *lemmas = row.split()
            # WordNetCorpusReader keeps the last row for duplicate forms.
            exceptions[inflected][pos] = set(lemmas)
    return forms, exceptions


def encode(corpus):
    forms, exceptions = tables(corpus)
    # Synonym targets, absent conjugations, and exception lemmas share the
    # same stable word IDs. Zero is a valid ID.
    vocabulary = set(forms) | set(exceptions)
    for table in (forms, exceptions):
        for entries in table.values():
            for words in entries:
                vocabulary.update(words)
    names = sorted(vocabulary)
    ids = {w:i for i,w in enumerate(names)}
    strings = bytearray()
    offsets = [0]
    for word in names:
        strings.extend(word)
        offsets.append(len(strings))
    # Repeated set IDs avoid emitting the same synset union for each lemma.
    groups = {(): 0}
    matrix = bytearray()
    for word in names:
        for table in (forms, exceptions):
            for values in table.get(word, ((),)*len(PARTS)):
                entry = tuple(sorted(ids[w] for w in values))
                matrix.extend(struct.pack('<I', groups.setdefault(entry, len(groups))))
    group_offsets = [0]
    members = []
    for group in groups:
        members.extend(group)
        group_offsets.append(len(members))
    def uints(values):
        return struct.pack('<'+str(len(values))+'I', *values)
    license_text = corpus.read('wordnet/LICENSE')
    header = b'Q4TWN01\0'+uints([len(names), len(strings), len(groups), len(members), len(license_text)])
    return b''.join([header, license_text, uints(offsets), strings, matrix,
                     uints(group_offsets), uints(members)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wordnet_zip', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    with args.wordnet_zip.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != SOURCE_SHA256:
            parser.error('unexpected WordNet 3.0 corpus identity')
        source.seek(0)
        with zipfile.ZipFile(source) as corpus:
            raw = encode(corpus)
    args.output.write_bytes(zlib.compress(raw, level=9))


if __name__ == '__main__':
    main()

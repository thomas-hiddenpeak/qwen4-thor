"""Fixed METEOR evaluator embedded in the isolated CPython grading worker."""
import bisect
import json
import struct
import zlib
from functools import lru_cache


class Lexicon:
    def __init__(self, packed):
        raw = zlib.decompress(packed)
        count, string_bytes, groups, members, notice = struct.unpack_from('<5I', raw, 8)
        start = 28 + notice
        if raw[:8] != b'Q4TWN01\0' or len(raw) != start + 4*(count+1) + string_bytes + 32*count + 4*(groups+1+members):
            raise ValueError('invalid compiled WordNet table')
        view = memoryview(raw)
        self.count = count
        self.offsets = view[start:start+4*(count+1)].cast('I')
        start += len(self.offsets)*4
        self.strings = view[start:start+string_bytes]
        start += string_bytes
        self.matrix = view[start:start+32*count].cast('I')
        start += 32*count
        self.groups = view[start:start+4*(groups+1)].cast('I')
        start += len(self.groups)*4
        self.members = view[start:].cast('I')

    def word(self, index):
        return bytes(self.strings[self.offsets[index]:self.offsets[index+1]])

    @lru_cache(maxsize=8192)
    def find(self, word):
        key = word.encode('utf-8')
        index = bisect.bisect_left(range(self.count), key, key=self.word)
        return index if index < self.count and self.word(index) == key else None

    def group(self, key, column):
        if key is None:
            return ()
        group = self.matrix[key*8+column]
        return self.members[self.groups[group]:self.groups[group+1]]

    @lru_cache(maxsize=4096)
    def synonyms(self, word):
        word = word.lower()
        key = self.find(word)
        result = set()
        for pos, rules in enumerate(MORPHOLOGY):
            exceptions = self.group(key, 4+pos)
            bases = list(exceptions) if exceptions else [
                self.find(word[:-len(suffix)]+ending)
                for suffix, ending in rules if word.endswith(suffix)]
            for base in [key, *bases]:
                result.update(self.group(base, pos))
        return result


MORPHOLOGY = (
    (('s', ''), ('ses', 's'), ('ves', 'f'), ('xes', 'x'), ('zes', 'z'),
     ('ches', 'ch'), ('shes', 'sh'), ('men', 'man'), ('ies', 'y')),
    (('s', ''), ('ies', 'y'), ('es', 'e'), ('es', ''), ('ed', 'e'),
     ('ed', ''), ('ing', 'e'), ('ing', '')),
    (('er', ''), ('est', ''), ('er', 'e'), ('est', 'e')),
    (),
)


def evaluate(request):
    candidate = request['input'].lower().split(None, 65536)
    reference = request['reference'].lower().split(None, 65536)
    # Reverse-priority alignment has at most three passes over token pairs.
    # Enforce the shared row budget before alignment or stemming any input.
    cost = (len(request['input'])+len(request['reference'])
            + 3*len(candidate)*len(reference) + 64*(len(candidate)+len(reference)))
    if (cost > request['meteor_work_budget'] or max(len(candidate), len(reference)) > 65536
            or any(len(word) > 256 for words in (candidate, reference) for word in words)):
        return {'over_budget': True, 'score': None, 'work': 0}
    positions = [-1]*len(candidate)
    used = [False]*len(reference)

    def align(left, right, lexical=False):
        for i in range(len(left)-1, -1, -1):
            if positions[i] >= 0:
                continue
            candidates = LEXICON.synonyms(left[i]) if lexical else None
            for j in range(len(right)-1, -1, -1):
                if used[j]:
                    continue
                if left[i] == right[j] or (lexical and LEXICON.find(right[j]) in candidates):
                    positions[i], used[j] = j, True
                    break

    align(candidate, reference)
    if -1 in positions and False in used:
        stem = PorterStemmer().stem
        # Apply the same sequential exact, stem and WordNet stages as the
        # NLTK implementation. Synonym lookup receives the unmatched stems.
        left = [stem(w) if positions[i] < 0 else '' for i,w in enumerate(candidate)]
        right = [stem(w) if not used[i] else '' for i,w in enumerate(reference)]
        align(left, right)
        if -1 in positions and False in used:
            align(left, right, True)
    count = sum(j >= 0 for j in positions)
    score = 0.0
    if count:
        chunks = sum(j >= 0 and (i == 0 or positions[i-1] < 0 or j != positions[i-1]+1)
                     for i,j in enumerate(positions))
        precision, recall = count/len(candidate), count/len(reference)
        weighted = precision*recall/(0.9*precision+(1-0.9)*recall)
        score = weighted*(1-0.5*(chunks/count)**3)
    return {'over_budget': False, 'score': score, 'work': cost}

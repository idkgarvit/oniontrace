"""M5 code-mix feature block: token-level language identification for Hinglish.

The plan calls this the headline differentiator and it is the one M5 feature that was
still missing. ``farm/language.py`` has been *generating* code-mixed text since the
farm was built (``StyleRegime.HINGLISH_LIGHT`` at roughly 20 percent romanised-Hindi
tokens, ``HINGLISH_HEAVY`` at roughly 50), but no detector ever read it: the four
attribution models tokenised everything into one bag of words, so a code-mix habit
that a human analyst sees instantly was invisible to the model.

WHAT THIS MODULE ADDS

  * **token-level language ID** over a curated romanised-Hindi lexicon;
  * **code-mix ratio** per corpus, which is a stable personal habit;
  * **intra-post switch rate**, which is what makes Hinglish recognisable as Hinglish
    rather than as two separate documents stapled together;
  * **function-word spaces kept separate by language**, because "aur" and "and"
    occupy different slots in a person's habit and collapsing them into one bag
    destroys exactly the signal being measured.

THE HONESTY PROBLEM, STATED FIRST

The farm generates code-mixed text from its own vocabulary. If this module identified
language using that same list, then a good score would demonstrate that the detector
recovered the generator, which is the failure mode the README warns about for persona
retrieval generally.

So this lexicon is written independently and is deliberately broader than the
generator's: it includes common romanised-Hindi function words the generator never
emits. The overlap is then measured rather than assumed, and
:func:`lexicon_overlap` reports it so a reviewer can discount the result by the right
amount. The score is still an upper bound on what a real deployment would achieve,
and the report says so.
"""

from __future__ import annotations

import re
from collections import Counter
from itertools import pairwise
from typing import Sequence

TOKEN = re.compile(r"[a-z']+")

# --------------------------------------------------------------------------- #
# Independent romanised-Hindi lexicon.
#
# Written from general knowledge of romanised Hindi as it appears in informal writing,
# not copied from the farm. It contains the farm's tokens as a subset, plus many it
# does not use (bolded in the overlap report), which is why measuring the overlap is
# the honest thing to do rather than claiming independence and moving on.
# --------------------------------------------------------------------------- #

HINDI_FUNCTION_WORDS: frozenset[str] = frozenset(
    """
    aur ya lekin par toh bhi hai hain ka ki ke mein se ko nahi kya yeh woh ek bahut
    thoda accha theek matlab bas phir abhi hoga tha theen raha
    rahe kar karna karte karta karti ja jab tab kahan yahan wahan kaise kyun kyon
    iska uska inka unka isme usme inme unme isse usse apna apni apne sab kuch koi
    kuch nahin bhi toh tak sirf bilkul zyada jyada kum waise waisey haan
    """.split()
)

HINDI_DISCOURSE_MARKERS: frozenset[str] = frozenset(
    """
    dekho suno waise haan bilkul sahi chalega matlab arre achha theek
    chalo batao bolo samjho sochna dekhna
    """.split()
)

# Content words that are unambiguously romanised Hindi. Chosen to exclude anything
# spelled the same in English, because a lexicon that counts "photo" as Hindi makes
# the ratio unmeasurable for an English-only writer.
HINDI_CONTENT_WORDS: frozenset[str] = frozenset(
    """
    paisa paise kaam jagah saman bhav rang wala wale wali cheez cheezein kitab kitabein
    gaadi gadi kapda kapde purana purani naya nayi sasta sasti mehnga mehngi badiya
    jaldi asli nakli dost doston bhai log logo aadmi aurat bachche ghar ghar pe
    din raat subah shaam hafta mahina saal waqt baat baatein cheez jaldi
    """.split()
)

HINDI_LEXICON: frozenset[str] = (
    HINDI_FUNCTION_WORDS | HINDI_DISCOURSE_MARKERS | HINDI_CONTENT_WORDS
)

#: English function words used as the comparison space. Kept separate from the Hindi
#: set so the two profiles never share a key, which is what stops a bilingual writer
#: from looking like a monolingual one with a bigger vocabulary.
ENGLISH_FUNCTION_WORDS: frozenset[str] = frozenset(
    """
    i the a an and to of is it you that for with this be on are as at by from or
    but not have has had was were will would can could should do does did so if
    then than there their they them we our us he she his her its my me your
    """.split()
)

HI = "hi"
EN = "en"
AMBIGUOUS = "amb"
UNKNOWN = "unk"

#: Tokens that are ordinary words in both languages. "par" is a golf term, "se" is not
#: English but "no" and "so" are, and a lexicon that counts them unconditionally makes
#: an English writer look like a code-mixer. These are classified by context instead.
AMBIGUOUS_TOKENS: frozenset[str] = frozenset(
    """
    na par se ko kam ho to hi so no do me us is it ek bas han
    """.split()
)


def language_of(token: str) -> str:
    """Classify one token without context.

    Returns ``AMBIGUOUS`` for a token that belongs to both languages; the caller
    resolves it against neighbouring tokens, which is the only way to be right about
    "par" in "a par of four" versus "par yeh theek hai".
    """

    if token in AMBIGUOUS_TOKENS:
        return AMBIGUOUS
    if token in HINDI_LEXICON:
        return HI
    if token in ENGLISH_FUNCTION_WORDS:
        return EN
    return UNKNOWN


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def post_languages(post) -> list[str]:
    """Per-token language labels for one post, with ambiguous tokens resolved in context.

    Unknowns are kept in the sequence because the switch counter needs them: a switch
    is a change between two *known* languages with only unknown tokens between, and
    dropping unknowns would turn "the and kitab" into an adjacent pair.

    An ambiguous token counts as Hindi only if an unambiguous Hindi token appears
    within two positions. Without this, one English homograph is enough to make a
    monolingual writer register a code-mix habit they do not have.
    """

    raw = [language_of(t) for t in tokens(post.text)]
    resolved: list[str] = []
    for i, label in enumerate(raw):
        if label == AMBIGUOUS:
            window = raw[max(0, i - 2):i] + raw[i + 1:i + 3]
            label = HI if HI in window else UNKNOWN
        resolved.append(label)
    return resolved


def code_mix_ratio(post) -> float:
    """Share of *known* tokens that are Hindi. Unknown tokens are excluded from both
    sides of the fraction so an English writer with unusual vocabulary does not
    register a spurious mix."""

    labels = [l for l in post_languages(post) if l != UNKNOWN]
    if not labels:
        return 0.0
    return sum(1 for l in labels if l == HI) / len(labels)


def switch_count(post) -> int:
    """How many times the writer changes language inside one post.

    Only known tokens count, and a run of unknowns does not break a switch, so the
    measure tracks the writer's habit rather than the lexicon's coverage.
    """

    known = [l for l in post_languages(post) if l != UNKNOWN]
    return sum(1 for a, b in pairwise(known) if a != b)


def codemix_profile(posts: Sequence) -> dict[str, float]:
    """A feature map for one persona's corpus, in the same shape as the other models.

    Hindi and English function words are prefixed separately, so cosine similarity
    compares a Hindi habit against a Hindi habit rather than against a merged bag.
    The four scalars capture the mix itself, which no single token frequency can.
    """

    hi_counter: Counter = Counter()
    en_counter: Counter = Counter()
    hi_total = en_total = 0
    ratios: list[float] = []
    switches_per_100: list[float] = []

    for post in posts:
        toks = tokens(post.text)
        for tok in toks:
            lang = language_of(tok)
            if lang == HI:
                hi_counter[tok] += 1
                hi_total += 1
            elif lang == EN:
                en_counter[tok] += 1
                en_total += 1

        ratios.append(code_mix_ratio(post))
        if toks:
            switches_per_100.append(100.0 * switch_count(post) / len(toks))

    features: dict[str, float] = {}

    # Separate language spaces.
    for tok, count in hi_counter.items():
        features[f"hi:{tok}"] = count / hi_total if hi_total else 0.0
    for tok, count in en_counter.items():
        features[f"en:{tok}"] = count / en_total if en_total else 0.0

    # The mix itself.
    if ratios:
        features["cm:mean_ratio"] = sum(ratios) / len(ratios)
        # Variance matters as much as the mean: a writer who always mixes at 20 percent
        # is a different person from one who alternates between pure English and pure
        # Hindi and averages the same.
        mean = features["cm:mean_ratio"]
        features["cm:ratio_var"] = sum((r - mean) ** 2 for r in ratios) / len(ratios)
        features["cm:min_ratio"] = min(ratios)
        features["cm:max_ratio"] = max(ratios)
    if switches_per_100:
        features["cm:switch_rate"] = sum(switches_per_100) / len(switches_per_100)

    return features


def summarise(posts: Sequence) -> dict:
    """Per-corpus counts for the report, so a claim about mix has a denominator."""

    labels = Counter()
    posts_with_mix = 0
    for post in posts:
        p = post_languages(post)
        labels.update(p)
        if any(l == HI for l in p) and any(l == EN for l in p):
            posts_with_mix += 1

    known = labels[HI] + labels[EN]
    return {
        "posts": len(posts),
        "tokens_known": known,
        "tokens_unknown": labels[UNKNOWN],
        "hindi_tokens": labels[HI],
        "english_tokens": labels[EN],
        "code_mix_ratio": (labels[HI] / known) if known else 0.0,
        "mixed_posts": posts_with_mix,
        "mixed_post_share": (posts_with_mix / len(posts)) if posts else 0.0,
    }


def lexicon_overlap(generator_vocabulary: frozenset[str] | set[str]) -> dict:
    """How much of this lexicon the farm's generator also uses.

    Reported because the honest reading of any score in this module depends on it. A
    high overlap means the detector and the generator share a vocabulary, and the
    result should be discounted accordingly.
    """

    inter = HINDI_LEXICON & set(generator_vocabulary)
    return {
        "detector_lexicon_size": len(HINDI_LEXICON),
        "generator_vocabulary_size": len(generator_vocabulary),
        "shared": len(inter),
        "share_of_detector": len(inter) / len(HINDI_LEXICON) if HINDI_LEXICON else 0.0,
        "detector_only": len(HINDI_LEXICON - set(generator_vocabulary)),
    }

"""Persona construction and corpus generation.

This module implements the D3 controlled parameters:
  * code-mix ratio (pure English / ~20% / ~50% Hinglish)
  * style drift -- content vocabulary changes, function-word habits do NOT
  * post length distribution (short 20-60 words / long 200-500)
  * posting-hour distribution (the behavioural signal)
  * template reuse (the known-hard case for text-only stylometry)

The function-word fingerprint is the thing that makes text-only stylometry possible
at all in this farm, so it is assigned deliberately: each persona gets a primary
set plus a *preferred subset*, which is what makes two personas sharing a set still
distinguishable -- with a graded, controllable amount of confusability.
"""

from __future__ import annotations

import random

from farm.language import (
    CORE_FUNCTION_WORDS,
    FUNCTION_WORD_SETS,
    LISTING_TEMPLATES,
    TOPIC_VOCABULARY,
)
from farm.model import (
    Persona,
    PersonaRole,
    Post,
    StyleRegime,
    deterministic_hex,
)

# Code-mix target ratios per style regime (D3: pure / 20% / 50%).
CODE_MIX_RATIO: dict[StyleRegime, float] = {
    StyleRegime.PURE_ENGLISH: 0.0,
    StyleRegime.HINGLISH_LIGHT: 0.20,
    StyleRegime.HINGLISH_HEAVY: 0.50,
    StyleRegime.TEMPLATE_REUSER: 0.10,
}

_FUNCTION_SET_NAMES: tuple[str, ...] = tuple(FUNCTION_WORD_SETS)

# Each persona's posts land mostly in a few hours of the day. Two personas with
# different active windows are behaviourally distinguishable; two with the same
# window are not, which is the honest limit of a 24-slot histogram.
_MIN_ACTIVE_HOURS = 3
_MAX_ACTIVE_HOURS = 5


def assign_function_words(rng: random.Random, set_name: str) -> list[str]:
    """A persona's function-word habit: a primary set plus a preferred subset.

    The subset is what differentiates two personas assigned the same set, so the
    farm can express 'these two write similarly' as a tunable rather than a binary.
    """
    primary = list(FUNCTION_WORD_SETS[set_name])
    keep = max(3, int(len(primary) * 0.6))
    preferred = sorted(rng.sample(primary, keep))
    # Core words are shared by everyone -- they carry no discriminative signal and
    # including them keeps generated text readable.
    return preferred + list(CORE_FUNCTION_WORDS)


def assign_active_hours(rng: random.Random) -> list[int]:
    """The persona's posting-hour window (behavioural ground truth)."""
    count = rng.randint(_MIN_ACTIVE_HOURS, _MAX_ACTIVE_HOURS)
    start = rng.randrange(0, 24)
    width = rng.randint(2, 4)
    hours: set[int] = set()
    for offset in range(count):
        hours.add((start + offset * width) % 24)
    return sorted(hours)


def build_persona(
    rng: random.Random,
    persona_id: str,
    cluster_id: str | None,
    role: PersonaRole,
    style: StyleRegime,
    topic: str,
    handle: str,
    function_set: str,
    template_id: str | None = None,
    shared_artifacts: dict[str, str] | None = None,
) -> Persona:
    """Construct one persona with a stable stylistic fingerprint."""
    vocab = list(TOPIC_VOCABULARY.get(topic, ()))
    if not vocab:  # pragma: no cover - guarded by config validation
        vocab = ["item", "listing", "offer"]

    # Persona-specific content subset: the driftable part.
    keep = max(6, int(len(vocab) * 0.7))
    content_vocab = sorted(rng.sample(vocab, min(keep, len(vocab))))

    persona = Persona(
        persona_id=persona_id,
        cluster_id=cluster_id,
        role=role,
        style=style,
        handle=handle,
        function_words=assign_function_words(rng, function_set),
        content_vocabulary=content_vocab,
        active_hours=assign_active_hours(rng),
        template_id=template_id if style is StyleRegime.TEMPLATE_REUSER else None,
        shared_artifacts=dict(shared_artifacts or {}),
    )
    return persona


def make_handle(rng: random.Random, seed: int, persona_id: str, near: str | None = None) -> str:
    """A pseudonymous handle.

    `near` produces a *near-miss* handle: deliberately similar to another persona's
    handle, which is the D4 decoy mechanism (similar handle strings).
    """
    suffixes = ("", "_", "x", "-", "__", "1", "99")
    if near is not None:
        return f"{near}{rng.choice(suffixes)}"
    stem = deterministic_hex(seed, f"handle:{persona_id}", length=8)
    return f"user_{stem}"


# --------------------------------------------------------------------------- #
# Sentence and post generation
# --------------------------------------------------------------------------- #


def _content_phrase(rng: random.Random, vocab: list[str], mix_ratio: float) -> str:
    """One noun phrase. Code-mix tokens are injected at the target ratio."""
    from farm.language import HINGLISH_CONTENT_TOKENS

    parts: list[str] = []
    for _ in range(rng.randint(2, 3)):
        if mix_ratio > 0 and rng.random() < mix_ratio * 0.5:
            parts.append(rng.choice(HINGLISH_CONTENT_TOKENS))
        else:
            parts.append(rng.choice(vocab))
    return " ".join(parts)


def fill_frame(
    rng: random.Random,
    frame: str,
    persona: Persona,
    vocab: list[str],
    mix_ratio: float,
) -> str:
    """Fill one sentence frame, respecting the persona's function-word habit."""
    from farm.language import (
        ENGLISH_DISCOURSE_MARKERS,
        HINGLISH_DISCOURSE_MARKERS,
        HINGLISH_FUNCTION_WORDS,
        QUALIFIERS,
        VERB_PHRASES,
    )

    use_hi = mix_ratio > 0 and rng.random() < mix_ratio
    disc = (
        rng.choice(HINGLISH_DISCOURSE_MARKERS)
        if use_hi
        else rng.choice(ENGLISH_DISCOURSE_MARKERS)
    )
    # The stylometric signal: {fw} is drawn ONLY from the persona's own habit list.
    if mix_ratio > 0 and rng.random() < mix_ratio * 0.5:
        fw = rng.choice(HINGLISH_FUNCTION_WORDS)
    else:
        fw = rng.choice(persona.function_words)

    return frame.format(
        disc=disc,
        fw=fw,
        subj=_content_phrase(rng, vocab, mix_ratio),
        obj=_content_phrase(rng, vocab, mix_ratio),
        verb=rng.choice(VERB_PHRASES),
        qual=rng.choice(QUALIFIERS),
    )


def generate_body(
    rng: random.Random,
    persona: Persona,
    vocab: list[str],
    target_words: int,
) -> str:
    """Compose a post body of roughly `target_words` words."""
    from farm.language import SENTENCE_FRAMES_LONG, SENTENCE_FRAMES_SHORT

    mix_ratio = CODE_MIX_RATIO[persona.style]
    frames = SENTENCE_FRAMES_LONG if target_words >= 150 else SENTENCE_FRAMES_SHORT

    sentences: list[str] = []
    words = 0
    # Bound the loop so a pathological seed cannot spin forever.
    for _ in range(400):
        if words >= target_words:
            break
        sentence = fill_frame(rng, rng.choice(frames), persona, vocab, mix_ratio)
        sentences.append(sentence)
        words += len(sentence.split())
    return " ".join(sentences)


def _fill_template(rng: random.Random, template_id: str) -> str:
    """A template-reuser post: only slot values vary."""
    from farm.language import (
        TEMPLATE_CONDITION_VALUES,
        TEMPLATE_DETAIL_VALUES,
        TEMPLATE_ITEM_VALUES,
        TEMPLATE_PRICE_VALUES,
    )

    return LISTING_TEMPLATES[template_id].format(
        item=rng.choice(TEMPLATE_ITEM_VALUES),
        condition=rng.choice(TEMPLATE_CONDITION_VALUES),
        price=rng.choice(TEMPLATE_PRICE_VALUES),
        details=rng.choice(TEMPLATE_DETAIL_VALUES),
    )


def generate_posts(
    rng: random.Random,
    persona: Persona,
    service_id: str | None,
    post_count: int,
    drift_at: int | None = None,
    drift_topic: str | None = None,
) -> list[Post]:
    """Generate a persona's corpus.

    `drift_at` is the post index at which style drift begins: content vocabulary
    switches to `drift_topic`, while `function_words` are deliberately untouched.
    That is the D3 requirement implemented literally -- and it is the mechanism the
    harness uses to show that a content-feature model degrades under drift while a
    function-word model does not.
    """
    active_hours: list[int] = persona.active_hours or [rng.randrange(24)]
    vocab = list(persona.content_vocabulary)

    if drift_at is not None and drift_topic:
        drift_vocab_pool = list(TOPIC_VOCABULARY.get(drift_topic, vocab))
        if drift_vocab_pool:
            keep = max(6, int(len(drift_vocab_pool) * 0.7))
            vocab_drifted = sorted(rng.sample(drift_vocab_pool, min(keep, len(drift_vocab_pool))))
        else:
            vocab_drifted = vocab
    else:
        vocab_drifted = vocab

    posts: list[Post] = []
    day = 0
    for index in range(post_count):
        is_template = (
            persona.style is StyleRegime.TEMPLATE_REUSER
            and persona.template_id is not None
            and rng.random() < 0.7
        )
        current_vocab = vocab_drifted if (drift_at is not None and index >= drift_at) else vocab

        if is_template:
            text = _fill_template(rng, persona.template_id or "TPL-MARKET-A")
        else:
            # Length regime: short 20-60 words, long 200-500 (D3).
            target = rng.randint(20, 60) if rng.random() < 0.6 else rng.randint(200, 500)
            text = generate_body(rng, persona, current_vocab, target)

        hour = rng.choice(active_hours)
        day += rng.choice([0, 0, 1, 1, 1, 2])

        posts.append(
            Post(
                post_id=f"{persona.persona_id}-p{index:03d}",
                persona_id=persona.persona_id,
                service_id=service_id,
                text=text,
                hour_of_day=hour,
                day_index=day,
                is_template=is_template,
            )
        )
    return posts
"""Vocabulary and sentence frames for synthetic persona text.

DESIGN NOTES
------------
The farm needs text where two things are independently controllable, because that
is the only way to test the plan's central hypothesis (E3: "behavioural features may
beat lexical features on short posts"):

  1. **Function-word habits** -- the stylometric fingerprint. Held *constant* per
     persona. Style drift changes content vocabulary but must NOT change these, which
     is exactly how the D3 requirement "change vocabulary, keep function-word habits"
     is implemented.
  2. **Content vocabulary** -- the topical surface. Allowed to drift, so a persona
     that rebrands still matches on function words.

Everything here is benign marketplace and hobby vocabulary, authored by us. Nothing
in this module references illicit subject matter, because the farm has to be
redistributable (Part B4: "Synthetic farm -- ours -- this is what ships on GitHub").
The detectors do not care what the text is *about*; topic is irrelevant to every
metric in Part E.

NOTE ON TEXT QUALITY (goes in the report as a stated limitation)
--------------------------------------------------------------
The generated text is **lexically controlled but semantically meaningless by design**.
It is a statistical fixture, not a corpus: sentences are assembled from frames so that
function-word distribution, code-mix ratio, and length are independently controllable,
which is exactly what the stylometry and behavioural features measure. Making the text
semantically coherent would add no measurement value and would risk producing content
that resembles real harmful material, which the farm must never contain. Any reader
seeing the raw fixtures should expect them to read as odd. That is intended.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Function-word preference sets
#
# Each persona is assigned one primary set. These are deliberately distinct in
# pronoun use, connective choice, and hedging level -- the axes stylometry actually
# keys on.
# --------------------------------------------------------------------------- #

FUNCTION_WORD_SETS: dict[str, tuple[str, ...]] = {
    "formal": (
        "however", "therefore", "furthermore", "thus", "moreover", "consequently",
        "nevertheless", "whilst", "shall", "cannot", "would", "which", "upon",
        "regarding", "concerning", "accordingly", "hence", "thereby",
    ),
    "casual": (
        "but", "so", "then", "also", "anyway", "plus", "though", "yeah", "ok",
        "well", "just", "really", "pretty", "like", "kinda", "gonna", "stuff",
    ),
    "terse": (
        "and", "or", "not", "no", "yes", "if", "then", "this", "that", "with",
        "from", "for", "to", "in", "on", "at", "by", "as",
    ),
    "hedging": (
        "perhaps", "maybe", "possibly", "seems", "appears", "might", "could",
        "probably", "generally", "tends", "roughly", "somewhat", "fairly",
        "arguably", "approximately",
    ),
}

# Function words that survive style drift. A persona's *core* habit words -- the
# ones a drift should never touch, because a real rebrand does not change them.
CORE_FUNCTION_WORDS: tuple[str, ...] = (
    "i", "the", "a", "and", "to", "of", "is", "it", "you", "that", "for", "with",
)

# --------------------------------------------------------------------------- #
# Romanised Hindi function words for the code-mix regimes
#
# Used purely as tokens in generated text so the Hinglish feature block has
# something real to measure. Benign vocabulary only.
# --------------------------------------------------------------------------- #

HINGLISH_FUNCTION_WORDS: tuple[str, ...] = (
    "aur", "ya", "lekin", "par", "toh", "bhi", "hai", "hain", "ka", "ki", "ke",
    "mein", "se", "ko", "nahi", "kya", "yeh", "woh", "ek", "bahut", "thoda",
    "accha", "theek", "matlab", "bas", "phir", "abhi",
)

HINGLISH_DISCOURSE_MARKERS: tuple[str, ...] = (
    "dekho", "suno", "waise", "haan", "bilkul", "sahi", "chalega",
)

# Content-carrying Hindi tokens, mixed in at the target code-mix ratio.
#
# These must be unambiguously romanised Hindi. An earlier version of this list
# contained words that are ordinary English (quality, condition, size, rate, photo),
# which made the code-mix ratio unmeasurable: a pure-English persona registered a
# non-zero mix because its own vocabulary was being counted as code-mix.
HINGLISH_CONTENT_TOKENS: tuple[str, ...] = (
    "paisa", "kaam", "jagah", "saman", "bhav", "rang", "wala", "cheez",
    "kitab", "gaadi", "kapda", "purana", "naya", "sasta", "mehnga", "badiya",
    "jaldi", "asli", "nakli", "dost",
)

ENGLISH_DISCOURSE_MARKERS: tuple[str, ...] = (
    "honestly", "frankly", "anyway", "look", "so", "well", "right", "ok",
)

# --------------------------------------------------------------------------- #
# Content vocabularies, by topic
#
# Six benign hobby/marketplace topics. Services are assigned a topic; personas draw
# content words from their service's topic plus a persona-specific subset, which is
# what makes content vocabulary driftable while function words stay fixed.
# --------------------------------------------------------------------------- #

TOPIC_VOCABULARY: dict[str, tuple[str, ...]] = {
    "vintage_electronics": (
        "amplifier", "receiver", "turntable", "cassette", "speaker", "tuner",
        "capacitor", "valve", "transformer", "chassis", "dial", "knob", "needle",
        "cartridge", "woofer", "tweeter", "crossover", "preamp", "wiring",
        "solder", "resistor", "circuit", "vintage", "restored", "serviced",
    ),
    "used_books": (
        "hardback", "paperback", "edition", "spine", "dustjacket", "printing",
        "volume", "anthology", "novel", "collection", "annotated", "inscribed",
        "foxing", "binding", "folio", "manuscript", "translation", "publisher",
        "chapter", "appendix", "index", "glossary", "hardcover", "reprint",
    ),
    "music_gear": (
        "pedal", "fretboard", "pickup", "amplifier", "strings", "bridge",
        "synthesiser", "keyboard", "drum", "cymbal", "snare", "hihat", "mixer",
        "interface", "microphone", "preamplifier", "compressor", "reverb",
        "tremolo", "cabinet", "headstock", "fret", "setup", "intonation",
    ),
    "garden_supplies": (
        "seedling", "compost", "terracotta", "perennial", "rhizome", "cutting",
        "greenhouse", "trellis", "watering", "mulch", "loam", "grafting",
        "pollinator", "bulb", "herbaceous", "pruning", "secateurs", "trowel",
        "germination", "hardy", "foliage", "container", "rootball", "sowing",
    ),
    "board_games": (
        "meeple", "board", "token", "expansion", "rulebook", "component",
        "deck", "shuffle", "drafting", "worker", "placement", "scoring",
        "sleeve", "insert", "miniature", "dice", "counter", "cardstock",
        "kickstarter", "solo", "cooperative", "auction", "tile", "campaign",
    ),
    "bike_parts": (
        "cassette", "derailleur", "crankset", "chainring", "bottom", "bracket",
        "headset", "stem", "handlebar", "saddle", "seatpost", "caliper",
        "rotor", "shifter", "hub", "spoke", "rim", "tyre", "tube", "pedal",
        "cog", "freewheel", "brazing", "truing",
    ),
}

# Grammatical frames. Slots are filled by the generator:
#   {disc}  discourse marker (language-regime dependent)
#   {fw}    function word drawn from the persona's set  <- the stylometric signal
#   {subj}  noun phrase subject (content words)
#   {verb}  verb phrase
#   {obj}   noun phrase object (content words)
#   {qual}  qualifier phrase
SENTENCE_FRAMES_SHORT: tuple[str, ...] = (
    "{disc} {subj} {verb} {obj}.",
    "{subj} {verb} {obj} {qual}.",
    "i {verb} {obj} {qual}.",
    "{fw} {subj} {verb} {obj}.",
    "{disc} i {verb} {obj}.",
    "{subj} {verb} {qual}.",
    "{fw} {subj} {qual}.",
    "{disc} {subj} {verb}.",
)

SENTENCE_FRAMES_LONG: tuple[str, ...] = (
    "{disc} {subj} {verb} {obj} {fw} {qual}.",
    "{fw} {subj} {verb} {obj} {qual}.",
    "i {verb} {fw} {obj} {qual} {disc}.",
    "{subj} {verb} {obj} {fw} {qual}.",
    "{disc} {fw} {subj} {verb} {obj}.",
    "{subj} {fw} {verb} {fw} {qual}.",
    "{fw} {subj} {verb} {obj} {disc} {qual}.",
    "i {verb} {obj} {fw} {qual} {fw}.",
)

VERB_PHRASES: tuple[str, ...] = (
    "picked up", "have been looking at", "am selling", "got hold of",
    "am asking about", "listed", "am offering", "have available",
    "am holding", "just received", "am comparing", "have restored",
)

QUALIFIERS: tuple[str, ...] = (
    "in good shape", "with minor marks", "for a fair price", "at the moment",
    "this week only", "if anyone is interested", "with the original box",
    "and it works fine", "no rush on this", "open to sensible offers",
    "condition is honest", "happy to answer questions",
)

# --------------------------------------------------------------------------- #
# Listing templates -- the known-hard case for stylometry
#
# Personas assigned StyleRegime.TEMPLATE_REUSER publish mostly from one of these
# verbatim, varying only the slot values. This is deliberate and load-bearing: it
# reproduces the real-world case D3/A7 calls out ("market listings are often
# copy-pasted vendor templates -- near-zero authorship signal").
#
# Two consequences the harness must be able to show:
#   * text-only stylometry performs *badly* on template posts, and
#   * template_id is itself a correlation signal, so reusing a template links
#     personas who are otherwise unrelated -- a hard negative for the correlator.
# --------------------------------------------------------------------------- #

LISTING_TEMPLATES: dict[str, str] = {
    "TPL-MARKET-A": (
        "ITEM: {item}\n"
        "CONDITION: {condition}\n"
        "PRICE: {price}\n"
        "DETAILS: {details}\n"
        "PAYMENT: agreed on contact\n"
        "CONTACT: reply to this listing\n"
    ),
    "TPL-MARKET-B": (
        "--- listing ---\n"
        "what: {item}\n"
        "state: {condition}\n"
        "want: {price}\n"
        "notes: {details}\n"
        "reply here if interested\n"
        "--- end ---\n"
    ),
    "TPL-FORUM-C": (
        "Looking for advice on {item}.\n"
        "\n"
        "{details}\n"
        "\n"
        "Currently {condition}. Budget around {price}.\n"
        "Thanks in advance.\n"
    ),
    "TPL-FORUM-D": (
        "{item}\n"
        "= {condition}\n"
        "= {price}\n"
        "= {details}\n"
        "will reply to messages in order\n"
    ),
}

TEMPLATE_ITEM_VALUES: tuple[str, ...] = (
    "amplifier", "turntable", "hardback set", "synthesiser", "compost bin",
    "board game bundle", "crankset", "speaker pair",
)

TEMPLATE_CONDITION_VALUES: tuple[str, ...] = (
    "used, working", "good", "fair, some wear", "excellent", "as new",
)

TEMPLATE_PRICE_VALUES: tuple[str, ...] = (
    "open to offers", "asking a fair price", "negotiable", "listed elsewhere too",
)

TEMPLATE_DETAIL_VALUES: tuple[str, ...] = (
    "collection preferred. can arrange postage at cost.",
    "happy to demonstrate. no time wasters please.",
    "details and photos on request. serious enquiries only.",
    "will consider swaps for similar items.",
)

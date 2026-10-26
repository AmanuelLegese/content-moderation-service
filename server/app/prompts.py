"""Generation prompt screening (rule 4). Runs before anything is generated or scored.

This is a deliberately conservative keyword/regex screen with normalisation against
the usual evasions (leetspeak, homoglyphs, spaced-out letters, number words). In
production it would be the first layer in front of an LLM classifier: see README part 3.
False positives refuse a prompt (the user rephrases); false negatives reach the generator.
"""

import re
import unicodedata

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17,
}
HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
    "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ⅼ": "l",
})
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
ZERO_WIDTH = re.compile("[​‌‍⁠﻿­]")
# "r a p e", "r.a.p.e", "u-n-d-e-r-a-g-e": three or more single letters split by separators
SPACED = re.compile(r"\b(?:[a-z][\s._\-*/]+){2,}[a-z]\b")

AGE_NUM = re.compile(
    r"\b(\d{1,2})\s*[-\s]?\s*(?:years?[-\s]*olds?|yrs?[-\s]*olds?|y\s*/\s*o\b|yo\b)"
    r"|\bage[ds]?\s*(?:of\s*)?(\d{1,2})\b"
)
AGE_WORD = re.compile(
    r"\b(" + "|".join(NUMBER_WORDS) + r")\s*[-\s]?\s*(?:years?[-\s]*olds?|yrs?|yo\b|y/o)"
)

SEXUAL = r"(?:sex|sexual|sexy|fuck\w*|nude|naked|porn\w*|penetrat\w*|oral|blowjob|intercourse|mating|erotic\w*|aroused|orgasm\w*|explicit|lewd|horny)"
ANIMAL = r"(?:dog|dogs|horse|horses|animal|animals|pony|goat|sheep|pig|donkey|beast|canine|equine)"
FAMILY = r"(?:sister|brother|mother|mom|mum|father|dad|daughter|son|aunt|uncle|niece|nephew|cousin|sibling|siblings)"

RULES: dict[str, list[str]] = {
    "minor": [
        r"\bunder\s*age\w*", r"\bminors?\b", r"\bchild\w*", r"\bkids?\b", r"\btoddlers?\b",
        r"\binfants?\b", r"\bpre\s*-?\s*teens?\b", r"\bteens?\b", r"\bteenage\w*", r"\bschool\s*(?:girl|boy)s?\b",
        r"\blol(?:i|ita|icon)\b", r"\bshota\w*", r"\bjailbait\b", r"\b(?:young|little|tiny)\s+(?:girl|boy)s?\b",
        r"\b(?:middle|high|junior\s*high|elementary)\s*school\w*", r"\bpre\s*-?\s*pubescent\b", r"\bpubescent\b",
        r"\bbarely\s*legal\b", r"\bpedo\w*", r"\bpaedo\w*", r"\b\d{1,2}(?:st|nd|rd|th)\s*grade\w*",
    ],
    "sexual_violence": [
        r"\brap(?:e|ed|es|ing|ist)\b", r"\bnon\s*-?\s*con\w*", r"\bwithout\s+(?:\w+\s+)?consent\b",
        r"\bagainst\s+(?:her|his|their|my|your)\s+will\b", r"\bforced\s+(?:sex|into|to)\b", r"\bdrugged\b",
        r"\bunconscious\b", r"\bpassed\s*out\b", r"\bmolest\w*", r"\bsexual(?:ly)?\s+assault\w*",
        r"\bcoerc\w*", r"\bdate\s*rape\b",
    ],
    "bestiality": [
        r"\bbestiality\b", r"\bzoophil\w*", r"\bknotting\b",
        rf"\b{SEXUAL}\b.*\b{ANIMAL}\b", rf"\b{ANIMAL}\b.*\b{SEXUAL}\b",
    ],
    "self_harm": [
        r"\bself\s*-?\s*harm\w*", r"\bsuicid\w*", r"\bcutting\s+(?:her|him|them|my|your)sel(?:f|ves)\b",
        r"\bslit(?:ting)?\s+(?:her|his|their|my)?\s*wrists?\b", r"\bkill(?:s|ing)?\s+(?:her|him|them|my|your)sel(?:f|ves)\b",
        r"\boverdos\w*", r"\bhang(?:s|ing)?\s+(?:her|him|them)sel(?:f|ves)\b",
    ],
    "real_person": [
        r"\bcelebrit\w*", r"\bceleb\b", r"\bfamous\s+(?:actress|actor|singer|person|model|star|influencer)\b",
        r"\bdeep\s*fake\w*", r"\breal\s+person\b", r"\blook\s*-?\s*alikes?\b", r"\bdoppelgang\w*",
        r"\bmy\s+(?:ex|coworker|co-worker|colleague|neighbou?r|boss|classmate|teacher|roommate)\b",
        r"\b(?:taylor\s*swift|scarlett\s*johansson|emma\s*watson|billie\s*eilish|kim\s*kardashian|ariana\s*grande"
        r"|selena\s*gomez|margot\s*robbie|jennifer\s*lawrence|gal\s*gadot|zendaya|beyonce|rihanna|sydney\s*sweeney)\b",
    ],
    "incest": [
        r"\bincest\w*", rf"\b{SEXUAL}\b.*\b(?:own|real)\s+{FAMILY}\b", rf"\b{FAMILY}\s+and\s+(?:her|his|their)\s+{FAMILY}\b.*\b{SEXUAL}\b",
        rf"\b{SEXUAL}\b.*\b{FAMILY}\s+and\s+(?:her|his|their)\s+{FAMILY}\b",
    ],
    "trafficking": [
        r"\btraffick\w*", r"\bsex\s*slaves?\b", r"\bsold\s+(?:for|as|into)\s+sex\b", r"\bkidnapp?\w*",
        r"\benslav\w*", r"\bcaptive\b",
    ],
}
COMPILED = {k: [re.compile(p) for p in v] for k, v in RULES.items()}
# Unambiguous terms matched inside text with every separator removed ("u n d e r a g e")
COMPACT_TERMS = {
    "minor": ["underage", "preteen", "jailbait", "schoolgirl", "lolicon", "pedophil"],
    "sexual_violence": ["nonconsensual", "daterape"],
    "bestiality": ["bestiality", "zoophil"],
    "self_harm": ["selfharm", "suicide"],
    "incest": ["incest"],
    "trafficking": ["trafficking", "sexslave"],
}


def normalise(text: str) -> tuple[str, str]:
    """Return (text with digits kept, text with leetspeak mapped). Both lowercase."""
    t = unicodedata.normalize("NFKC", text)
    t = ZERO_WIDTH.sub("", t).lower().translate(HOMOGLYPHS)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    leet = t.translate(LEET)
    leet = SPACED.sub(lambda m: re.sub(r"[^a-z]", "", m.group(0)), leet)
    return t, leet


def check_prompt(prompt: str) -> list[str]:
    """Return refusal reasons; an empty list means the prompt may be generated."""
    digits, text = normalise(prompt)
    compact = re.sub(r"[^a-z]", "", text)
    found: dict[str, str] = {}

    for m in AGE_NUM.finditer(digits):
        age = int(m.group(1) or m.group(2))
        if age < 18:
            found.setdefault("minor", m.group(0).strip())
    for m in AGE_WORD.finditer(text):
        found.setdefault("minor", m.group(0).strip())

    for category, patterns in COMPILED.items():
        for p in patterns:
            m = p.search(text)
            if m:
                found.setdefault(category, m.group(0)[:40].strip())
                break
        if category not in found:
            for term in COMPACT_TERMS.get(category, []):
                if term in compact:
                    found[category] = term
                    break

    return [f"prompt refused: {cat} ({match!r})" for cat, match in found.items()]

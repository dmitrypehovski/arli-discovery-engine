"""Intake: a person's words turned into something the network can compare.

Why this exists. Everything else in this network assumes both sides can produce features: an agent from an
address, a hosted agent from a form. The person who has no agent and no vocabulary sits outside all of it,
and there are far more of them than there are agents. The intake is that door: they write what they need in
their own words, and this turns the words into the same canonical tokens every other participant uses.

How it reads, and how it does not. It does not understand meaning. It looks for things it can name: a
taxonomy entry, a city, an industry, a capacity, a budget, a compliance mark, a stage. Everything it names
becomes a feature; everything it does not name is returned as written, in `not_understood`, so the person is
told what was dropped instead of being handed a confident list the machine invented. The rule is the same as
the rest of the engine: where the machine cannot decide, it says so.

Three deliberate rules, because loose matching invents constraints:

- A phrase matches on word boundaries, never inside a word. "ul" inside "fulfilment" is not a certificate,
  which is a real defect the first version of this file had.
- An entry is matched whole, or by a shared eight-character stem, and only when that stem names exactly one
  entry. "warehouse" reaches "warehousing". "learning" reaches three entries and is therefore returned to
  the person with those three named, because choosing one for them would be a guess dressed as a reading.
- Which way a statement points is decided before anything is named, because it decides the namespace: the
  same words are a need from a buyer and an offer from a supplier, and those are not interchangeable.

Where the taxonomy comes from. The production taxonomy (31 categories, 335 subcategories) is the company's
licensed data and is loaded by the platform at runtime from a file outside this repository, through
`Taxonomy.from_file`. The repository ships a small built-in set so the mechanism is testable and the open
code carries no third-party data it may not redistribute.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from arli_engine.core.buckets import profile_of

DEMAND_MARKERS = ("need", "needs", "needed", "looking for", "seeking", "seek", "want", "wanted", "require",
                  "required", "requirement", "must have", "procure", "buy", "buying", "hire", "hiring",
                  "нужно", "нужен", "нужна", "ищу", "ищем", "требуется", "хотим", "надо")
SUPPLY_MARKERS = ("offer", "offers", "offering", "supply", "supplies", "supplying", "provide", "provides",
                  "available", "sell", "selling", "we build", "we run", "we do",
                  "есть", "предлагаем", "предлагаю", "поставляем", "делаем", "можем", "оказываем")
PERSON_MARKERS = ("freelancer", "self employed", "self-employed", "i am a", "i'm a", "as a person",
                  "фрилансер", "самозанятый")

CITIES = (
    "shenzhen", "shanghai", "beijing", "guangzhou", "hangzhou", "hong kong", "singapore", "tokyo", "osaka",
    "seoul", "taipei", "bangkok", "jakarta", "mumbai", "bangalore", "delhi", "dubai", "riyadh", "istanbul",
    "berlin", "munich", "hamburg", "paris", "lyon", "madrid", "barcelona", "lisbon", "amsterdam",
    "rotterdam", "brussels", "zurich", "geneva", "vienna", "warsaw", "prague", "budapest", "stockholm",
    "oslo", "copenhagen", "helsinki", "dublin", "london", "manchester", "edinburgh", "milan", "rome",
    "athens", "new york", "san francisco", "sf", "boston", "chicago", "austin", "seattle", "los angeles",
    "toronto", "vancouver", "mexico city", "sao paulo", "buenos aires", "bogota", "lima", "lagos",
    "nairobi", "cairo", "johannesburg", "cape town", "sydney", "melbourne", "auckland",
    "шэньчжэнь", "шанхай", "пекин", "москва", "берлин", "лондон",
)

INDUSTRIES = (
    "fintech", "banking", "insurance", "payments", "healthcare", "pharma", "medical", "retail", "ecommerce",
    "e-commerce", "logistics", "freight", "manufacturing", "hardware", "electronics", "semiconductor",
    "automotive", "aerospace", "solar", "battery", "construction", "real estate", "hospitality", "travel",
    "education", "media", "gaming", "agriculture", "food", "fashion", "textiles", "government", "telecom",
    "chemicals", "финтех", "логистика", "производство", "здравоохранение", "энергетика",
)

COMPLIANCE = ("gdpr", "soc2", "soc 2", "iso 27001", "iso27001", "hipaa", "pci dss", "iso 13485", "haccp",
              "ce mark", "iatf 16949", "as9100")

STAGES = ("pre-seed", "preseed", "seed", "series a", "series b", "series c", "enterprise", "smb", "startup",
          "scale-up", "public company")

UNITS = {"team": "teams", "teams": "teams", "people": "people", "person": "people", "persons": "people",
         "engineer": "engineers", "engineers": "engineers", "order": "orders", "orders": "orders",
         "container": "containers", "containers": "containers", "line": "lines", "lines": "lines",
         "unit": "units", "units": "units", "pallet": "pallets", "pallets": "pallets"}

STOP = frozenset("""the and for with that this from our your you are was were not but any all one two three
near looking need needs needed want wants wanted offer offers offering supply supplies providing provide provides
available must have has can will would should about into over under below more less than then when where
which who what how why also very just some such them they their its it of in on at to by as is be we us i a
an do does did based clients client company business work looking for someone something things thing
нужно нужны нужен есть также под над до для что как или мы вы они это склад""".split())

# Numbers written as words, because a person writes "three teams" and a capacity that reads only digits
# would drop it. Russian and Chinese included: the intake is the door for people, not only for typists.
NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "один": "1", "одна": "1", "два": "2", "две": "2", "три": "3", "четыре": "4",
    "пять": "5", "шесть": "6", "一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
}

STEM_CHARS = 8  # long enough that "machine" and "machinery" stop colliding
STEM_SPECIFICITY = 1  # a stem must name exactly one entry, or the choice goes back to the person


def _plain(text: str) -> str:
    """Lower case, keep letters, digits and hyphens, and collapse runs of space."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\-\s]", " ", str(text or "").lower(), flags=re.UNICODE)).strip()


def _boundary(phrase: str) -> str:
    return rf"(?<![\w\-]){re.escape(phrase)}(?![\w\-])"


@dataclass
class Taxonomy:
    """The published list of things a business can need or offer: a slug, its title, and its category.

    The slug is the feature value, so a buyer who needs `customs-clearance` and a supplier who offers it
    write the same token. The category is the bucket a search uses, which is why a profile built here lands
    in the same index as everybody else's instead of a private one.
    """

    entries: dict[str, dict] = field(default_factory=dict)  # slug -> {title, category}
    source: str = "built-in"
    _stems: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def built_in(cls) -> "Taxonomy":
        """A small set so the mechanism is testable; the shape is the same as the production file."""
        library = {
            "transportation-supply-chain-logistics": {
                "warehousing": "Warehousing", "customs-clearance": "Customs Clearance",
                "freight-forwarding": "Freight Forwarding", "last-mile-delivery": "Last Mile Delivery"},
            "manufacturing-and-production": {
                "packaging": "Packaging", "injection-moulding": "Injection Moulding", "machining": "Machining"},
            "design": {"brand-identity": "Brand Identity", "product-design": "Product Design",
                       "packaging-design": "Packaging Design"},
            "marketing-and-public-relations": {"market-research": "Market Research",
                                               "content-marketing": "Content Marketing"},
            "human-resources": {"recruiting": "Recruiting", "payroll": "Payroll"},
            "finance": {"accounting": "Accounting", "audit": "Audit"},
            "law-regulation-compliance": {"contracts": "Contracts", "company-formation": "Company Formation"},
            "media-and-communications": {"video-production": "Video Production",
                                         "translation-and-localisation": "Translation And Localisation"},
            "software-and-it": {"machine-learning": "Machine Learning", "backend-development":
                                "Backend Development", "security-testing": "Security Testing"},
        }
        entries = {slug: {"title": title, "category": category}
                   for category, slugs in library.items() for slug, title in slugs.items()}
        return cls(entries=entries, source="built-in")

    @classmethod
    def from_file(cls, path: str | Path) -> "Taxonomy":
        """The production taxonomy: {category: {title, subcategories: {slug: title}, blurb}}."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries: dict[str, dict] = {}
        for category, node in data.items():
            for slug, title in (node.get("subcategories") or {}).items():
                entries[slug] = {"title": title, "category": category}
        return cls(entries=entries, source=str(path))

    def __post_init__(self) -> None:
        index: dict[str, list[str]] = {}
        for slug in self.entries:
            for word in slug.split("-"):
                if len(word) >= STEM_CHARS:
                    index.setdefault(word[:STEM_CHARS], []).append(slug)
        self._stems = {stem: slugs for stem, slugs in index.items() if len(slugs) <= STEM_SPECIFICITY}

    def size(self) -> int:
        return len(self.entries)

    def category_of(self, slug: str) -> str:
        return (self.entries.get(slug) or {}).get("category", "")

    def phrases(self) -> list[tuple[int, str, str]]:
        """(word count, slug, phrase) for the longest-match rule, longest first."""
        out: list[tuple[int, str, str]] = []
        for slug, node in self.entries.items():
            out.append((len(slug.split("-")), slug, slug))
            title = str(node["title"]).lower()
            out.append((len(title.split()), slug, title))
        out.sort(key=lambda item: -item[0])
        return out

    def stems(self) -> dict[str, list[str]]:
        """Eight-character stem -> the single entry it names. A stem naming several entries is not usable."""
        return self._stems

    def all_stems(self) -> dict[str, list[str]]:
        """Every stem with its candidates, so an ambiguous word can be handed back rather than decided."""
        index: dict[str, list[str]] = {}
        for slug in self.entries:
            for word in slug.split("-"):
                if len(word) >= STEM_CHARS:
                    index.setdefault(word[:STEM_CHARS], []).append(slug)
        return index


def _infer_kind(text: str) -> tuple[str | None, str]:
    low = " " + _plain(text) + " "
    for marker in SUPPLY_MARKERS:
        if f" {marker} " in low or f" {marker}," in low:
            return "supply", marker
    for marker in DEMAND_MARKERS:
        if f" {marker} " in low or f" {marker}," in low:
            return "demand", marker
    for marker in PERSON_MARKERS:
        if marker in low:
            return "person", marker
    return None, ""


def read_request(text: str, *, taxonomy: Taxonomy | None = None, kind: str | None = None) -> dict:
    """Turn a written request into features, and report what could not be placed.

    Deterministic by design: the same sentence gives the same features every time, and every feature can be
    pointed back at the phrase it came from. A silent guess here would put words in a person's mouth and
    then match on them.
    """
    taxonomy = taxonomy or Taxonomy.built_in()
    residual = _plain(text)
    understood: list[dict] = []

    # Which way it points is decided first, because it decides the namespace: the same words are a need from
    # a buyer and an offer from a supplier.
    inferred, evidence = _infer_kind(text)
    direction = kind or inferred
    namespace = "offer" if direction == "supply" else "need"

    def claim(feature: str, phrase: str) -> None:
        """Name a phrase and take it out of the sentence, so it is neither read twice nor reported missing."""
        nonlocal residual
        if not phrase or not re.search(_boundary(phrase), residual):
            return
        residual = re.sub(_boundary(phrase), " ", residual, count=1)
        understood.append({"phrase": phrase, "feature": feature})

    # Whole entries first, longest phrase before shorter, on word boundaries.
    for _length, slug, phrase in taxonomy.phrases():
        if re.search(_boundary(phrase), residual):
            claim(f"{namespace}:{slug}", phrase)

    def place(pool, prefix: str) -> None:
        """Named things that are properties rather than goods: where, which industry, which mark.

        Before the stems, because "retail" and "legal" are properties a both sides can state, and letting a
        stem claim them first would turn a location or a sector into a service nobody offered.
        """
        for marker in pool:
            if re.search(_boundary(marker), residual):
                claim(f"{prefix}:{re.sub(r'[^\w]+', '-', marker)}", marker)

    place(CITIES, "city")
    place(INDUSTRIES, "industry")
    place(COMPLIANCE, "compliance")
    place(STAGES, "stage")

    # Then stems, for a word whose form differs from the entry: "warehouse" reaches "warehousing". Only a
    # stem that names exactly one entry is used; where it names several the word is handed back to the
    # person with the candidates, because choosing one for them would be a guess presented as a reading.
    ambiguous: list[dict] = []
    every_stem = taxonomy.all_stems()
    for stem, slugs in taxonomy.stems().items():
        found = re.search(rf"(?<![\w\-])({re.escape(stem)}[\w]*)(?![\w\-])", residual)
        if found:
            claim(f"{namespace}:{slugs[0]}", found.group(1))
    for stem, slugs in every_stem.items():
        if len(slugs) <= 1:
            continue
        found = re.search(rf"(?<![\w\-])({re.escape(stem)}[\w]*)(?![\w\-])", residual)
        if found and len(slugs) > 1:
            ambiguous.append({
                "word": found.group(1),
                "could_be": [{"slug": slug, "title": taxonomy.entries[slug]["title"]} for slug in slugs[:5]],
            })

    # Capacity and budget: a number with a unit, because a number without one means nothing and inventing a
    # unit would be a constraint nobody stated.
    # The number words are turned into digits in the sentence itself, so a phrase that is claimed later is
    # found where it actually is: claiming "3 teams" against a sentence that still says "three teams" fails
    # silently, which is exactly the sort of quiet drop this file exists to avoid.
    residual = re.sub(rf"(?<![\w\-])({'|'.join(NUMBER_WORDS)})(?![\w\-])",
                      lambda hit: NUMBER_WORDS[hit.group(1)], residual)
    for found in re.finditer(r"(\d+)\s*(teams?|people|persons?|engineers?|orders?|containers?|lines?|units?|pallets?)\b",
                             residual):
        amount, unit = found.group(1), found.group(2)
        claim(f"capacity:{amount}-{UNITS.get(unit, unit)}", found.group(0).strip())
    money = re.search(r"(?:under|below|up to)\s*\$?\s*(\d+)\s*(k|m)?", residual)
    if money:
        claim(f"budget:under-{money.group(1)}{money.group(2) or ''}", money.group(0).strip())

    # One feature twice is one feature: a person who names the same thing in two places is not asking twice.
    features = list(dict.fromkeys(item["feature"] for item in understood))
    city = next((item["feature"].split(":", 1)[1] for item in understood
                 if item["feature"].startswith("city:")), "")

    # Stop words are grammar, not meaning. What is left is the words that carried meaning and found no home,
    # and those go back to the person so they can say them another way.
    words_unplaced = sorted({word for word in residual.split()
                             if len(word) > 2 and word not in STOP and not word.isdigit()})

    questions: list[str] = []
    if not features:
        questions.append("Nothing in that could be placed. Name the thing you need in a word or two, "
                         "for example \"warehousing\" or \"brand identity\".")
    else:
        if not city:
            questions.append("Where? A city or country, so the other side can be near enough to matter.")
        if direction is None:
            questions.append("Are you buying, offering, or asking as a person?")
        for item in ambiguous:
            titles = ", ".join(candidate["title"] for candidate in item["could_be"])
            questions.append(f"The word \"{item['word']}\" fits more than one entry ({titles}). "
                             "Which one, or say it another way.")
        if words_unplaced:
            questions.append("These words were not placed and nothing is being matched on them: "
                             + ", ".join(words_unplaced[:12]) + ". Say them another way, or leave them out.")

    return {
        "features": features[:64],
        "feature_count": len(features[:64]),
        "buckets": profile_of(features, bucket_of=lambda feature: taxonomy.category_of(feature.split(":", 1)[-1])),
        "understood": understood,
        "not_understood": words_unplaced,
        "ambiguous": ambiguous,
        "kind": direction,
        "kind_evidence": evidence,
        "city": city,
        "taxonomy": {"source": taxonomy.source, "entries": taxonomy.size()},
        "questions": questions,
        "note": "Every feature can be pointed back at the phrase it came from. Words the intake could not "
                "place are listed rather than silently dropped.",
    }
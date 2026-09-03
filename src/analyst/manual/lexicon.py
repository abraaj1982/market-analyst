"""Keyword lexicon for scoring company announcements.

Why a lexicon and not a language model:

  * It is **transparent**. Every score comes with the exact terms that produced
    it, so a reader can look at the reading and disagree with it on specifics.
    A model that outputs "negative, 0.7" cannot be argued with.
  * It is **free and offline**, which is the constraint this whole system was
    built under.
  * It is **stable**. The same headline scores identically today and in a year,
    so stored history stays comparable.

What it is not: it does not understand negation, sarcasm, or context. A
headline saying "profit did not fall" will read as negative. That limitation is
stated in the report rather than hidden, and every score can be overridden by
hand.

Terms are matched in **both English and Arabic**, because MSX announcements and
local coverage are frequently Arabic even when the interface is English.
Weights are deliberately coarse: 3 = decisive, 2 = material, 1 = mild.
"""
from __future__ import annotations

import re
import unicodedata

#: term -> weight. Positive terms.
POSITIVE: dict[str, int] = {
    # earnings and profitability
    "profit rose": 3, "profit up": 3, "net profit increased": 3, "record profit": 3,
    "earnings growth": 3, "beat expectations": 3, "exceeded expectations": 3,
    "profit": 1, "profitable": 2, "surplus": 2, "margin improvement": 2,
    "revenue growth": 2, "revenue increased": 2, "turnaround": 2,
    # distributions
    "dividend increase": 3, "raised dividend": 3, "special dividend": 3,
    "bonus shares": 2, "dividend": 1, "distribution approved": 2, "buyback": 2,
    "share buyback": 3,
    # business momentum
    "new contract": 3, "contract award": 3, "awarded": 2, "wins tender": 3,
    "expansion": 2, "acquisition": 2, "new project": 2, "partnership": 2,
    "capacity increase": 2, "licence granted": 2, "license granted": 2,
    "upgrade": 2, "credit rating upgrade": 3, "listing": 1,
    # arabic
    "ارتفاع الأرباح": 3, "زيادة الأرباح": 3, "نمو الأرباح": 3, "أرباح قياسية": 3,
    "توزيعات نقدية": 2, "زيادة التوزيعات": 3, "أسهم منحة": 2, "توزيع أرباح": 2,
    "عقد جديد": 3, "ترسية": 3, "ترسية مناقصة": 3, "توسعة": 2, "استحواذ": 2,
    "مشروع جديد": 2, "شراكة": 2, "اتفاقية": 1, "نمو الإيرادات": 2,
    "ارتفاع الإيرادات": 2, "تحسن": 2, "رفع التصنيف": 3, "فائض": 2, "أرباح": 1,
}

#: term -> weight. Negative terms.
NEGATIVE: dict[str, int] = {
    # earnings
    "loss": 3, "net loss": 3, "profit fell": 3, "profit declined": 3,
    "earnings miss": 3, "missed expectations": 3, "below expectations": 2,
    "impairment": 3, "write-down": 3, "writedown": 3, "provision": 2,
    "revenue declined": 2, "margin pressure": 2, "deficit": 3,
    # distributions
    "dividend cut": 3, "suspended dividend": 3, "no dividend": 2,
    "dividend suspension": 3, "reduced dividend": 3,
    # governance and legal
    "investigation": 3, "lawsuit": 2, "fine": 2, "penalty": 2, "fraud": 3,
    "resignation": 2, "ceo resigns": 3, "auditor": 1, "qualified opinion": 3,
    "delisting": 3, "trading suspension": 3, "suspended": 2, "default": 3,
    "restructuring": 2, "downgrade": 3, "credit rating downgrade": 3,
    "going concern": 3, "capital increase": 1, "rights issue": 2,
    # arabic
    "خسارة": 3, "خسائر": 3, "انخفاض الأرباح": 3, "تراجع الأرباح": 3,
    "تراجع الإيرادات": 2, "مخصصات": 2, "انخفاض القيمة": 3, "عجز": 3,
    "وقف التوزيعات": 3, "خفض التوزيعات": 3, "عدم توزيع": 2,
    "تحقيق": 2, "دعوى": 2, "غرامة": 2, "مخالفة": 2, "استقالة": 2,
    "إيقاف التداول": 3, "شطب": 3, "تعثر": 3, "خفض التصنيف": 3,
    "إعادة هيكلة": 2, "زيادة رأس المال": 1, "تحفظ المدقق": 3,
}

#: Arabic diacritics and tatweel, stripped before matching.
_ARABIC_NOISE = re.compile(r"[ؗ-ًؚ-ْـ]")


def normalise(text: str) -> str:
    """Lowercase, strip Arabic diacritics, and unify alef/ya/ta-marbuta forms.

    Without this, "الأرباح" and "الارباح" are different strings and half the
    real headlines miss every term in the lexicon.
    """
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = _ARABIC_NOISE.sub("", text)
    for src, dst in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه")):
        text = text.replace(src, dst)
    return re.sub(r"\s+", " ", text).strip()


_NORMALISED_POSITIVE = {normalise(k): v for k, v in POSITIVE.items()}
_NORMALISED_NEGATIVE = {normalise(k): v for k, v in NEGATIVE.items()}


def score_text(text: str) -> tuple[float, dict[str, list[str]]]:
    """Return (sentiment in [-1, 1], matched terms by polarity).

    Longer phrases are matched first and then removed from the working text, so
    "dividend cut" does not also score the positive term "dividend".
    """
    working = normalise(text)
    matched: dict[str, list[str]] = {"positive": [], "negative": []}
    positive_weight = negative_weight = 0

    ordered = sorted(
        [(t, w, "negative") for t, w in _NORMALISED_NEGATIVE.items()]
        + [(t, w, "positive") for t, w in _NORMALISED_POSITIVE.items()],
        key=lambda item: len(item[0]),
        reverse=True,
    )

    for term, weight, polarity in ordered:
        if term and term in working:
            matched[polarity].append(term)
            if polarity == "positive":
                positive_weight += weight
            else:
                negative_weight += weight
            working = working.replace(term, " ")

    total = positive_weight + negative_weight
    if total == 0:
        return 0.0, matched
    # Saturating rather than linear: five negative terms is not five times worse
    # than one, it is the same story told five ways.
    raw = (positive_weight - negative_weight) / total
    magnitude = min(1.0, total / 6.0)
    return round(raw * magnitude, 4), matched

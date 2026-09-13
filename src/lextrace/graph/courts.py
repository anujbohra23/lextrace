"""Federal court metadata and contextual authority relationships.

These rules describe institutional hierarchy, not whether an opinion governs a
particular proposition. Publication, en banc status, and issue fit need review.
"""

from typing import Literal

from pydantic import Field

from lextrace.retrieval.contracts import Record

CourtLevel = Literal["supreme", "appeals", "district"]
AuthorityCategory = Literal[
    "CONTROLLING", "PERSUASIVE", "SAME_COURT", "NON_CONTROLLING", "UNKNOWN"
]


class Court(Record):
    court_id: str
    name: str
    abbreviation: str
    level: CourtLevel
    circuit: str | None = None
    jurisdiction: str = "federal"
    parent_court_id: str | None = None


class AuthorityRelationship(Record):
    case_id: str
    matter_id: str | None = None
    forum_court: str | None = None
    authority_court: str | None = None
    category: AuthorityCategory
    rationale: str
    hierarchy_path: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)


_COURTS: dict[str, Court] = {
    "scotus": Court(
        court_id="scotus",
        name="Supreme Court of the United States",
        abbreviation="U.S.",
        level="supreme",
    )
}
_CIRCUIT_NAMES = (
    "First",
    "Second",
    "Third",
    "Fourth",
    "Fifth",
    "Sixth",
    "Seventh",
    "Eighth",
    "Ninth",
    "Tenth",
    "Eleventh",
)
for _number in range(1, 12):
    _id = f"ca{_number}"
    _ordinal = (
        "st"
        if _number == 1
        else "nd"
        if _number == 2
        else "rd"
        if _number == 3
        else "th"
    )
    _COURTS[_id] = Court(
        court_id=_id,
        name=f"U.S. Court of Appeals for the {_CIRCUIT_NAMES[_number - 1]} Circuit",
        abbreviation=f"{_number}{_ordinal} Cir.",
        level="appeals",
        circuit=_id,
        parent_court_id="scotus",
    )
for _id, _name, _circuit in (
    ("cadc", "U.S. Court of Appeals for the D.C. Circuit", "cadc"),
    ("cafc", "U.S. Court of Appeals for the Federal Circuit", "cafc"),
):
    _COURTS[_id] = Court(
        court_id=_id,
        name=_name,
        abbreviation=_id.upper(),
        level="appeals",
        circuit=_circuit,
        parent_court_id="scotus",
    )
for _id, _name, _abbr, _circuit in (
    ("nysd", "Southern District of New York", "S.D.N.Y.", "ca2"),
    ("nyed", "Eastern District of New York", "E.D.N.Y.", "ca2"),
    ("nynd", "Northern District of New York", "N.D.N.Y.", "ca2"),
    ("nywd", "Western District of New York", "W.D.N.Y.", "ca2"),
    ("ctd", "District of Connecticut", "D. Conn.", "ca2"),
    ("vtd", "District of Vermont", "D. Vt.", "ca2"),
    ("cand", "Northern District of California", "N.D. Cal.", "ca9"),
    ("cacd", "Central District of California", "C.D. Cal.", "ca9"),
    ("dcd", "District of Columbia", "D.D.C.", "cadc"),
):
    _COURTS[_id] = Court(
        court_id=_id,
        name=f"U.S. District Court for the {_name}",
        abbreviation=_abbr,
        level="district",
        circuit=_circuit,
        parent_court_id=_circuit,
    )

_ALIASES = {
    court.abbreviation.lower().replace(" ", ""): key for key, court in _COURTS.items()
}
_ALIASES.update({"s.d.n.y.": "nysd", "e.d.n.y.": "nyed", "n.d.cal.": "cand"})


def court_for(value: str | None) -> Court | None:
    """Resolve a known CourtListener ID or common forum abbreviation."""
    if not value:
        return None
    key = value.strip().lower()
    return _COURTS.get(key) or _COURTS.get(_ALIASES.get(key.replace(" ", ""), ""))


def classify_authority(
    case_id: str,
    authority_court_id: str | None,
    forum_court_id: str | None,
    *,
    matter_id: str | None = None,
) -> AuthorityRelationship:
    """Classify federal institutional relationship without deciding issue fit."""
    forum = court_for(forum_court_id)
    authority = court_for(authority_court_id)
    resolved_forum = forum.court_id if forum else forum_court_id
    resolved_authority = authority.court_id if authority else authority_court_id
    if forum is None or authority is None:
        return AuthorityRelationship(
            case_id=case_id,
            matter_id=matter_id,
            forum_court=resolved_forum,
            authority_court=resolved_authority,
            category="UNKNOWN",
            rationale="Forum or authority court is not mapped.",
            uncertainty=["Court hierarchy could not be established."],
        )
    path = [authority.court_id, forum.court_id]
    uncertainty = [
        "Hierarchy does not establish publication, precedential status, or issue fit."
    ]
    if authority.court_id == forum.court_id:
        category: AuthorityCategory = "SAME_COURT"
        reason = "Own-court decision; binding effect requires review."
    elif authority.level == "supreme":
        category = "CONTROLLING"
        reason = "Supreme Court authority is above this federal forum."
    elif authority.level == "appeals" and authority.circuit == forum.circuit:
        if forum.level == "district":
            category = "CONTROLLING"
            reason = "This is the appellate circuit governing the Matter's district."
        else:
            category = "SAME_COURT"
            reason = "This decision is from the Matter's appellate court."
    elif authority.level == "district" and authority.circuit == forum.circuit:
        category = "PERSUASIVE"
        reason = "A different district in the same circuit is persuasive only."
    elif authority.level == "district":
        category = "PERSUASIVE"
        reason = "A different district court is persuasive only."
    else:
        category = "PERSUASIVE"
        reason = "Another federal appellate circuit is persuasive, not governing."
    return AuthorityRelationship(
        case_id=case_id,
        matter_id=matter_id,
        forum_court=resolved_forum,
        authority_court=resolved_authority,
        category=category,
        rationale=reason,
        hierarchy_path=path,
        uncertainty=uncertainty,
    )

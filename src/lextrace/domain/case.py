"""Validated internal decision and opinion models."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Opinion(BaseModel):
    """One opinion, with its source and text provenance intact."""

    model_config = ConfigDict(extra="forbid")
    source_id: NonEmpty
    kind: NonEmpty
    text: NonEmpty
    text_source_field: Literal["html_with_citations", "plain_text"]


class Case(BaseModel):
    """One decision cluster, rather than an entire litigation docket."""

    model_config = ConfigDict(extra="forbid")
    source: Literal["courtlistener"] = "courtlistener"
    source_id: NonEmpty
    source_url: HttpUrl
    name: NonEmpty
    date_filed: date | None
    court_id: NonEmpty
    docket_number: NonEmpty | None
    opinions: Annotated[list[Opinion], Field(min_length=1)]

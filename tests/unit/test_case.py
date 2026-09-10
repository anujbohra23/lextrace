"""Internal model validation."""

import pytest
from pydantic import ValidationError

from lextrace.domain.case import Case, Opinion


def test_opinion_trims_fields() -> None:
    opinion = Opinion(
        source_id=" 3 ", kind=" unknown ", text=" text ", text_source_field="plain_text"
    )
    assert opinion.text == "text"
    assert opinion.source_id == "3"


@pytest.mark.parametrize("field", ["source_id", "kind", "text"])
def test_opinion_rejects_blank(field: str) -> None:
    data = {
        "source_id": "3",
        "kind": "unknown",
        "text": "text",
        "text_source_field": "plain_text",
    }
    data[field] = " "
    with pytest.raises(ValidationError):
        Opinion.model_validate(data)


def test_case_rejects_empty_opinions() -> None:
    with pytest.raises(ValidationError):
        Case.model_validate(
            {
                "source_id": "1",
                "source_url": "https://www.courtlistener.com/opinion/1/a/",
                "name": "A",
                "date_filed": None,
                "court_id": "scotus",
                "docket_number": None,
                "opinions": [],
            }
        )


def test_old_cases_allow_unknown_reporter_citations() -> None:
    case = Case.model_validate(
        {
            "source_id": "1",
            "source_url": "https://www.courtlistener.com/opinion/1/a/",
            "name": "A",
            "date_filed": None,
            "court_id": "ca2",
            "docket_number": None,
            "opinions": [
                {
                    "source_id": "2",
                    "kind": "unknown",
                    "text": "Text",
                    "text_source_field": "plain_text",
                }
            ],
        }
    )
    assert case.reporter_citations is None

"""Bounded model-output contracts for evidence-led product comparisons."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scout.contracts import ExtractedItem, ExtractionPlan, FieldSpec, RunMeta


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class Constraint(StrictModel):
    field: str = Field(min_length=1, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    operator: Literal["min", "max", "equals", "contains"]
    value: str = Field(min_length=1, max_length=200)
    unit: str = Field(default="", max_length=30)

    @field_validator("unit", mode="before")
    @classmethod
    def empty_null_unit(cls, value: object) -> object:
        return "" if value is None else value


class ProductPlan(StrictModel):
    scope: Literal["web", "site", "url"]
    goal: str = Field(min_length=1, max_length=1000)
    site: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2000)
    country: str = Field(default="", max_length=80)
    constraints: list[Constraint] = Field(default_factory=list, max_length=20)
    preferences: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("site", "url", "country", mode="before")
    @classmethod
    def empty_null_scope_fields(cls, value: object) -> object:
        return "" if value is None else value

    @model_validator(mode="after")
    def check_scope(self) -> "ProductPlan":
        if self.scope == "site" and not self.site:
            raise ValueError("A named-site plan requires a site name or domain.")
        if self.scope == "url":
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("URL research requires an HTTP(S) URL.")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("URL credentials are not allowed.")
        if len({c.field for c in self.constraints}) != len(self.constraints):
            # Ranges use distinct fields (e.g. price_min/price_max would be ambiguous).
            # Multiple operators on one field are supported; exact duplicates are not.
            keys = {(c.field, c.operator) for c in self.constraints}
            if len(keys) != len(self.constraints):
                raise ValueError("Duplicate constraints.")
        return self


class Fact(StrictModel):
    value: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=1500)
    unit: str = Field(default="", max_length=30)


class ProductCandidate(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    page_id: str = Field(min_length=1, max_length=30)
    facts: dict[str, Fact] = Field(max_length=30)
    score: float = Field(ge=0, le=100)

    @model_validator(mode="after")
    def safe_fields(self) -> "ProductCandidate":
        import re

        reserved = {"domain", "source_url", "checked_at", "match_score"}
        if any(
            name in reserved or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", name)
            for name in self.facts
        ):
            raise ValueError("Invalid or reserved fact field name.")
        return self


class ProductExtractionPlan(ExtractionPlan):
    fields: list[FieldSpec] = Field(min_length=1, max_length=34)


class ProductResult(StrictModel):
    url: str
    instruction: str
    plan: ProductExtractionPlan
    items: list[ExtractedItem]
    meta: RunMeta
    raw_text_preview: str = ""


class ResearchAction(StrictModel):
    operation: Literal["search", "fetch"]
    query: str = Field(default="", max_length=500)
    domain: str = Field(default="", max_length=253)
    url: str = Field(default="", max_length=2000)


class ResearchTurn(StrictModel):
    actions: list[ResearchAction] = Field(default_factory=list, max_length=10)
    dismissed_domains: list[str] = Field(default_factory=list, max_length=30)
    candidates: list[ProductCandidate] = Field(default_factory=list, max_length=20)
    done: bool = False


class Presentation(StrictModel):
    """Formatting choices only: no model-authored facts can enter the output."""

    candidate_ids: list[str] = Field(max_length=150)
    highlight_fields: list[str] = Field(default_factory=list, max_length=12)

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
Category = Literal["ACCOUNT", "ORDER", "REFUND", "INVOICE", "CONTACT", "PAYMENT", "FEEDBACK", "DELIVERY", "SHIPPING", "SUBSCRIPTION", "CANCEL"]
Priority = Literal["low", "normal", "high", "urgent"]
Product = Literal["storefront", "billing", "fulfillment", "membership", "support"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Ticket(Strict):
    text: Text


class Triage(Strict):
    category: Category
    priority: Priority
    product: Product
    summary: Text
    draft_reply: Text


class Served(Triage):
    prompt_version: str
    model: str
    git_sha: str
    cohort: str
    mode: str
    first_pass: bool
    fallback: bool


class Failure(Strict):
    error: str
    prompt_version: str
    model: str
    git_sha: str


class Fault(Exception):
    def __init__(self, code: str, status: int, retry_after: float = 0):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after

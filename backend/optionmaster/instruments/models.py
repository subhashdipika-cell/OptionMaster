from pydantic import BaseModel, Field


class QuantityRequest(BaseModel):
    contract_security_id: int = Field(gt=0)
    lots: int = Field(default=1, gt=0)
    quantity: int | None = Field(default=None, gt=0)

from pydantic import BaseModel, Field


class PackageInput(BaseModel):
    cutoff_seq: int = Field(ge=1)


class OfflineCheck(BaseModel):
    package: dict
    anchor: dict | None = None
    expect_key_id: str | None = Field(default=None, max_length=32)

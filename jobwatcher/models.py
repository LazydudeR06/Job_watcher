from dataclasses import dataclass


SOURCE_VALUES = ("remotive", "remoteok", "himalayas", "adzuna")


@dataclass(frozen=True)
class Job:
    """One normalized job record."""

    source: str
    source_job_id: str
    title: str
    company: str | None
    location: str | None
    url: str
    posted_at: str | None
    raw_text: str

    def __post_init__(self) -> None:
        if self.source not in SOURCE_VALUES:
            raise ValueError(f"Unsupported source: {self.source}")

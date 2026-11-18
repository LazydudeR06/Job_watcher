from abc import ABC, abstractmethod

from ..models import Job


class Collector(ABC):
    """Interface for public job feed collectors."""

    name: str

    @abstractmethod
    def fetch(self) -> list[Job]:
        """Fetch normalized jobs from this collector."""
        raise NotImplementedError

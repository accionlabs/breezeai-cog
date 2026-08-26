# infra/interface.py

from abc import ABC, abstractmethod


class InfraStream(ABC):

    @abstractmethod
    def write_line(self, line: str) -> None:
        """Write a line to the stream."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> str:
        """Close the stream and return the storage key."""
        raise NotImplementedError
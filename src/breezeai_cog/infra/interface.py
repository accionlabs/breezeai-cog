from abc import ABC, abstractmethod


class InfraStream(ABC):
    """A streaming upload of NDJSON lines to provider-backed object storage.

    Contract every implementation must honour:

    * **Retries belong to the provider SDK.** The stream source is not
      replayable (bytes are consumed as they are written), so a whole-upload
      retry would re-send only the unconsumed tail. Let the SDK retry at a
      granularity where it holds the bytes itself — e.g. botocore re-sends an
      individual multipart part from its own buffer.
    * **``close()`` must raise if the stored object is incomplete, and must
      not return a key in that case.** Callers treat a returned key as proof
      that the object is complete and readable: ``/api/analyze-diff`` passes
      it to the backend via ``/code-ontology/stream-ingest``, which downloads
      and ingests it later. Swallowing an upload error and returning the key
      anyway would point the backend at a corrupt object.
    """

    @abstractmethod
    def write_line(self, line: str) -> None:
        """Write a line to the stream."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> str:
        """Finish the upload and return the storage key.

        Raises:
            Exception: if the upload failed. No key is returned for a partial
                or corrupt object.
        """
        raise NotImplementedError

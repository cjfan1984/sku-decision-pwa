import sys
from pathlib import Path

import pytest
from google.auth.exceptions import TransportError as GoogleTransportError
from requests.exceptions import ChunkedEncodingError as RequestsChunkedEncodingError
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import sync_google as syncer


def test_snapshot_read_retries_transient_timeout(monkeypatch):
    attempts = []
    sleeps = []

    def read(_spreadsheet_id):
        attempts.append(1)
        if len(attempts) < 3:
            raise RequestsTimeout("temporary")
        return [{"SKU_KEY": "0001|A", "产品名称/SKU": "A"}]

    monkeypatch.setattr(syncer, "_read_snapshot", read)
    monkeypatch.setattr(syncer.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(syncer.time, "sleep", sleeps.append)

    assert syncer.snapshot_rows("sheet") == [{"SKU_KEY": "0001|A", "产品名称/SKU": "A"}]
    assert len(attempts) == 3
    assert sleeps == [1, 2]


def test_snapshot_read_does_not_retry_non_transient_error(monkeypatch):
    attempts = []

    def read(_spreadsheet_id):
        attempts.append(1)
        raise ValueError("bad worksheet")

    monkeypatch.setattr(syncer, "_read_snapshot", read)

    with pytest.raises(ValueError, match="bad worksheet"):
        syncer.snapshot_rows("sheet")
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "error",
    [
        RequestsConnectionError("offline"),
        RequestsChunkedEncodingError("truncated"),
        GoogleTransportError("token transport failed"),
    ],
)
def test_transport_failures_are_transient(error):
    assert syncer._is_transient_google_error(error)

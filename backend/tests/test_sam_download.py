from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from app.research.models import SourceResultStatus
from app.research.sources.sam_exclusions import (
    SAM_EXTRACT_API,
    SamDownloadError,
    _resolve_extract_response,
    store_uploaded_extract,
)


FIXTURE = Path(__file__).parent / "fixtures" / "sam_exclusions_v2_sample.csv"
OFFICIAL_NAME = "SAM_Exclusions_Public_Extract_V2_26263.ZIP"
INNER_CSV_NAME = "SAM_Exclusions_Public_Extract_V2_26263.CSV"


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(INNER_CSV_NAME, FIXTURE.read_bytes())
    return buffer.getvalue()


def _response(*, content: bytes = b"", json_data=None, headers=None, status: int = 200, url: str = SAM_EXTRACT_API):
    request = httpx.Request("GET", url)
    if json_data is not None:
        return httpx.Response(status, json=json_data, headers=headers or {}, request=request)
    return httpx.Response(status, content=content, headers=headers or {}, request=request)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected follow-up HTTP request")
        return self.responses.pop(0)


def test_direct_zip_without_content_disposition_uses_inner_filename_for_freshness(tmp_path):
    response = _response(
        content=_zip_bytes(),
        headers={"content-type": "application/zip"},
    )
    data, filename = _resolve_extract_response(FakeClient([]), response, api_key="test-key")

    # The outer response has no trustworthy filename, so the downloader must not
    # invent today's official filename merely to make the data appear fresh.
    assert filename == "SAM_Exclusions_Public_Extract_V2_download.ZIP"

    dataset = store_uploaded_extract(data, filename, cache_dir=tmp_path)
    assert dataset.csv_name == INNER_CSV_NAME
    assert dataset.extract_date is not None
    assert dataset.extract_date.isoformat() == "2026-09-20"


def test_json_filename_response_is_resolved_with_follow_up_download():
    first = _response(json_data={"fileName": OFFICIAL_NAME}, headers={"content-type": "application/json"})
    second = _response(
        content=_zip_bytes(),
        headers={
            "content-type": "application/zip",
            "content-disposition": f'attachment; filename="{OFFICIAL_NAME}"',
        },
    )
    client = FakeClient([second])

    data, filename = _resolve_extract_response(client, first, api_key="test-key")

    assert data == _zip_bytes()
    assert filename == OFFICIAL_NAME
    assert len(client.calls) == 1
    follow_url, kwargs = client.calls[0]
    assert follow_url == SAM_EXTRACT_API
    assert kwargs["params"]["fileName"] == OFFICIAL_NAME
    assert kwargs["params"]["api_key"] == "test-key"


def test_json_same_host_download_url_is_allowed_and_receives_api_key():
    download_url = "https://api.sam.gov/data-services/v1/extracts?fileName=" + OFFICIAL_NAME
    first = _response(json_data={"downloadUrl": download_url}, headers={"content-type": "application/json"})
    second = _response(
        content=_zip_bytes(),
        headers={"content-type": "application/zip"},
        url=download_url,
    )
    client = FakeClient([second])

    _data, filename = _resolve_extract_response(client, first, api_key="test-key")

    assert filename == "SAM_Exclusions_Public_Extract_V2_download.ZIP"
    assert len(client.calls) == 1
    follow_url, _kwargs = client.calls[0]
    assert follow_url.startswith("https://api.sam.gov/")
    assert "api_key=test-key" in follow_url


def test_json_external_download_url_is_refused():
    first = _response(
        json_data={"downloadUrl": "https://example.com/not-sam.zip"},
        headers={"content-type": "application/json"},
    )

    with pytest.raises(SamDownloadError) as exc_info:
        _resolve_extract_response(FakeClient([]), first, api_key="test-key")

    assert exc_info.value.status == SourceResultStatus.DATASET_MALFORMED


def test_unauthorized_response_stays_auth_required():
    response = _response(status=403)

    with pytest.raises(SamDownloadError) as exc_info:
        _resolve_extract_response(FakeClient([]), response, api_key="bad-key")

    assert exc_info.value.status == SourceResultStatus.AUTH_REQUIRED
    assert exc_info.value.http_status == 403

import pytest

from agent_dev.adapters.design_references import DesignReferences
from agent_dev.domain.content import digest
from agent_dev.domain.design_reference import figma_locator, stitch_locator
from agent_dev.domain.errors import DomainError


def test_figma_file_and_frame_locator():
    row = figma_locator("https://www.figma.com/design/AbC123/Design?node-id=12-34&t=temporary")
    assert row["file_key"] == "AbC123"
    assert row["node_id"] == "12:34"
    assert "?" not in row["url"]
    assert figma_locator("ABC")["node_id"] == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://figma.com/design/ABC",
        "https://evil.test/design/ABC",
        "https://figma.com@evil.test/design/ABC",
        "https://figma.com/design/ABC?node-id=bad",
        "https://figma.com:443/design/ABC",
    ],
)
def test_unsafe_figma_urls_rejected(url):
    with pytest.raises(DomainError):
        figma_locator(url)


def test_read_only_reference_cache_and_tamper_detection(tmp_path):
    class Fake:
        root = tmp_path

        def show(self, key):
            return {"provider": "stitch"}

        def call(self, connection_id, operation, arguments, *, scope):
            assert operation == "get_screen"
            assert arguments == {"name": "projects/p1/screens/s1"}
            return {
                "content": {"html": "<script>bad()</script>"},
                "digest": digest("source"),
                "fetched_at": 0,
                "truncated": False,
            }

    service = DesignReferences(tmp_path, integrations=Fake())
    row = service.inspect("stitch", {"project_id": "p1", "screen_id": "s1"})
    assert service.get(row["id"])["trust"] == "untrusted_data"
    path = service.directory / (row["id"] + ".json")
    path.write_text(path.read_text().replace("bad()", "other()"))
    with pytest.raises(DomainError):
        service.get(row["id"])
    with pytest.raises(DomainError):
        stitch_locator("../escape", "s")

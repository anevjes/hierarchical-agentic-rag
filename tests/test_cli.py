import argparse
import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hierarchical_rag import cli
from hierarchical_rag.models import Answer, Citation, InvestigationResult
from hierarchical_rag.provision import index_definition


@pytest.mark.parametrize("command", ["ask", "ingest", "catalog"])
@pytest.mark.parametrize("encoding", ["cp1252", "ascii", "utf-8"])
async def test_cli_json_round_trips_unicode_on_redirected_stdout(
    settings, monkeypatch, tmp_path, command, encoding
):
    text = "Threshold \u2264 5; \u5730\u8cea; \U0001f30d"
    result = InvestigationResult(
        status="answered", question=text,
        answer=Answer(answer=text, citations=[Citation(evidence_id="p1", quote=text)]),
        retrieved_chunks=[], evidence=[], tool_calls=0, searches=0, stop_reason=text,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    async def execute(args, settings, usage):
        with usage.operation("test", document=text):
            pass
        return result if command == "ask" else None

    monkeypatch.setattr(cli, "_run", execute)
    report_path = tmp_path / "usage.json"
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding=encoding, errors="strict") as stdout:
        with monkeypatch.context() as context:
            context.setattr("sys.stdout", stdout)
            await cli.run(argparse.Namespace(command=command, usage_report=report_path))
            stdout.flush()
        raw = buffer.getvalue()
    assert raw.isascii()
    output = json.loads(raw)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["events"][0]["document"] == text
    if command == "ask":
        assert output["answer"]["answer"] == text
        assert output["answer"]["citations"][0]["quote"] == text
        assert output["usage"] == report
    else:
        assert output == report


@pytest.mark.parametrize(
    "arguments",
    [
        ["ingest", "--top", "0"],
        ["ingest", "--top", "-1"],
        ["ingest", "--top", "1.5"],
        ["ingest", "--top"],
        ["ingest", "--blob", "a.pdf", "--top", "20"],
    ],
)
def test_invalid_top_rejected_before_azure(monkeypatch, arguments, capsys):
    run = AsyncMock()
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr("sys.argv", ["hrag", *arguments])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "--top" in capsys.readouterr().err
    run.assert_not_called()


@pytest.mark.parametrize(
    "arguments,top,prefix,blob",
    [
        (["ingest", "--top", "20"], 20, "", None),
        (["ingest", "--prefix", "manuals/", "--top", "20"], 20, "manuals/", None),
        (["ingest"], None, "", None),
        (["ingest", "--blob", "manuals/a.pdf"], None, "", "manuals/a.pdf"),
    ],
)
def test_ingest_arguments(monkeypatch, arguments, top, prefix, blob):
    run = AsyncMock()
    monkeypatch.setattr(cli, "run", run)
    monkeypatch.setattr("sys.argv", ["hrag", *arguments])
    cli.main()
    args = run.call_args.args[0]
    assert (args.top, args.prefix, args.blob) == (top, prefix, blob)


@pytest.fixture
def ingestion_cli(monkeypatch, settings):
    visited = []

    async def listing(**kwargs):
        for name in ["manuals/notes.txt", "manuals/a.pdf", "manuals/b.PDF", "manuals/c.pdf"]:
            visited.append(name)
            yield SimpleNamespace(name=name)

    source = Mock(list_blobs=Mock(side_effect=listing))
    storage = Mock(get_container_client=Mock(side_effect=[source, Mock()]))
    index = Mock(get_index=AsyncMock(return_value=index_definition(settings)))

    def context(value):
        manager = AsyncMock()
        manager.__aenter__.return_value = value
        return manager

    monkeypatch.setattr(cli, "Settings", lambda: settings)
    for name, value in [
        ("DefaultAzureCredential", Mock()),
        ("BlobServiceClient", storage),
        ("SearchIndexClient", index),
        ("SearchClient", Mock()),
        ("ContentUnderstandingClient", Mock()),
        ("AsyncAzureOpenAI", Mock()),
    ]:
        monkeypatch.setattr(cli, name, Mock(return_value=context(value)))
    monkeypatch.setattr(cli, "get_bearer_token_provider", Mock())
    ingest = AsyncMock(return_value=2)
    monkeypatch.setattr(cli, "ingest_pdf", ingest)
    return source, ingest, visited


@pytest.mark.parametrize("top,expected", [(1, 1), (2, 2), (20, 3), (None, 3)])
async def test_top_counts_pdfs_and_stops_listing(ingestion_cli, top, expected, caplog):
    source, ingest, visited = ingestion_cli
    caplog.set_level(logging.INFO, logger=cli.__name__)
    await cli.run(argparse.Namespace(command="ingest", blob=None, prefix="manuals/", top=top))
    source.list_blobs.assert_called_once_with(name_starts_with="manuals/")
    assert [call.args[0] for call in ingest.await_args_list] == [
        "manuals/a.pdf", "manuals/b.PDF", "manuals/c.pdf"
    ][:expected]
    assert len(visited) == expected + 1
    assert f"Ingested {expected} PDF(s), {expected * 2} chunks" in caplog.text
    assert ("Reached --top" in caplog.text) == (top in (1, 2))


async def test_exact_blob_does_not_list(ingestion_cli):
    source, ingest, visited = ingestion_cli
    await cli.run(argparse.Namespace(command="ingest", blob="one.pdf", prefix="", top=None))
    source.list_blobs.assert_not_called()
    assert not visited
    assert ingest.await_args.args[0] == "one.pdf"


async def test_failure_still_stops_ingestion(ingestion_cli, caplog):
    _, ingest, visited = ingestion_cli
    ingest.side_effect = RuntimeError("analysis failed")
    with pytest.raises(RuntimeError, match="analysis failed"):
        await cli.run(argparse.Namespace(command="ingest", blob=None, prefix="", top=20))
    assert len(visited) == 2
    assert ingest.await_count == 1
    assert "Reached --top" not in caplog.text


async def test_top_preserves_no_matching_pdf_error(ingestion_cli):
    source, ingest, _ = ingestion_cli

    async def listing(**kwargs):
        yield SimpleNamespace(name="notes.txt")

    source.list_blobs.side_effect = listing
    with pytest.raises(ValueError, match="No PDF blobs matched"):
        await cli.run(argparse.Namespace(command="ingest", blob=None, prefix="", top=20))
    ingest.assert_not_called()

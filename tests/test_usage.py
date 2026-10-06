import argparse
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_agent import ScriptedClient, answer, assessment, tool_call
from test_ingestion import ingestion_clients

from hierarchical_rag import cli
from hierarchical_rag.agent import investigate
from hierarchical_rag.ingestion import ingest_pdf
from hierarchical_rag.investigation import Investigation
from hierarchical_rag.usage import TokenRates, UsageTracker


def test_partial_costs_and_token_subsets():
    usage = UsageTracker("ask", {"chat": TokenRates(input=2, output=8, cached_input=0.5)})
    with usage.operation("investigator", "chat") as event:
        event.record({
            "input_token_count": 1000,
            "output_token_count": 200,
            "cache_read_input_token_count": 400,
            "reasoning_output_token_count": 100,
        })
    with usage.operation("iq_retrieval"):
        pass
    report = usage.report("answered")
    assert report.reported_tokens["total_tokens"] == 1200
    assert report.reported_tokens["reasoning_output_tokens"] == 100
    assert report.estimated_token_cost_usd == pytest.approx(0.003)
    assert report.unpriced_events == 1
    assert report.events[1].total_tokens is None
    assert report.elapsed_seconds >= 0
    assert report.finished_at >= report.started_at


def test_unknown_usage_and_missing_prices_are_not_zero_cost():
    usage = UsageTracker("ingest")
    with usage.operation("content_understanding"):
        pass
    report = usage.report("completed")
    assert all(value is None for value in report.reported_tokens.values())
    assert report.estimated_token_cost_usd is None
    assert report.unpriced_events == 1


@pytest.mark.parametrize("rate", [dict(input=-1), dict(input=float("nan")), dict(input=1, extra=5)])
def test_invalid_rates_rejected(rate):
    with pytest.raises(ValueError):
        TokenRates(**rate)


def test_unknown_cached_count_does_not_guess_discount():
    usage = UsageTracker("ask", {"chat": TokenRates(input=1, output=2, cached_input=0.1)})
    with usage.operation("writer", "chat") as event:
        event.record({"input_token_count": 100, "output_token_count": 20})
    assert usage.report("answered").estimated_token_cost_usd is None


def test_rates_without_cache_discount_charge_input_once():
    usage = UsageTracker("ask", {"chat": TokenRates(input=1, output=2)})
    with usage.operation("writer", "chat") as event:
        event.record({"input_token_count": 100, "output_token_count": 20,
                      "cache_read_input_token_count": 50})
    assert usage.report("answered").estimated_token_cost_usd == pytest.approx(0.00014)


def test_failed_operation_retains_usage_and_original_exception():
    usage = UsageTracker("ask")
    with pytest.raises(RuntimeError, match="failure"):
        with usage.operation("writer", "chat") as event:
            event.record({"input_token_count": 10, "output_token_count": 2})
            raise RuntimeError("failure")
    report = usage.report("failed")
    assert report.events[0].status == "failed"
    assert report.reported_tokens["total_tokens"] == 12
    # Snapshot is independent of subsequent mutations.
    usage.events[0].input_tokens = 999
    assert report.events[0].input_tokens == 10


async def test_ingestion_aggregates_embedding_tokens_and_unknown_cu(settings):
    _, source, pages, cu, search = ingestion_clients()
    usage = UsageTracker(
        "ingest", {settings.embedding_deployment: TokenRates(input=0.1)}
    )
    await ingest_pdf("a.pdf", settings, source, pages, cu, search,
                     search.embedding_client, usage=usage)
    report = usage.report("completed")
    assert report.reported_tokens["input_tokens"] == 20
    assert report.estimated_token_cost_usd == pytest.approx(0.000002)
    assert report.events[0].stage == "content_understanding"
    assert report.events[0].input_tokens is None
    assert report.counters["documents_completed"] == 1
    assert report.counters["pages_extracted"] == 2
    assert report.counters["chunks_embedded"] == 2
    assert report.counters["artifact_blobs_uploaded"] == 6
    assert report.counters["source_bytes_downloaded"] == len(b"%PDF-1.7\n")


async def test_ingestion_failure_keeps_spent_tokens(settings):
    _, source, pages, cu, search = ingestion_clients()
    search.upload_documents.side_effect = RuntimeError("index failed")
    usage = UsageTracker("ingest")
    with pytest.raises(RuntimeError, match="index failed"):
        await ingest_pdf("a.pdf", settings, source, pages, cu, search,
                         search.embedding_client, usage=usage)
    report = usage.report("failed")
    assert report.reported_tokens["input_tokens"] == 20
    assert report.counters["documents_started"] == 1
    assert report.counters.get("documents_completed", 0) == 0


async def test_native_maf_loop_usage_aggregated_once(settings, backend, document):
    docid = document.document_id
    eid = f"{docid}:{document.revision}:p1"
    client = ScriptedClient(
        [
            tool_call("open_pages", document_id=docid, start_page=1, end_page=1),
            assessment([eid]),
            answer(eid),
        ],
        usage_details={
            "input_token_count": 100,
            "output_token_count": 20,
            "total_token_count": 120,
            "cache_read_input_token_count": 10,
            "reasoning_output_token_count": 5,
        },
    )
    result = await investigate("warranty", Investigation(settings, backend), client)
    assert len(client.requests) == 3
    assert [event.stage for event in result.usage.events] == ["investigator", "writer"]
    assert [event.input_tokens for event in result.usage.events] == [200, 100]
    assert result.usage.reported_tokens["total_tokens"] == 360
    assert result.usage.reported_tokens["cached_input_tokens"] == 30
    assert result.usage.reported_tokens["reasoning_output_tokens"] == 15
    assert result.usage.counters["pages_opened"] == 1
    assert result.usage.counters["iq_searches"] == 1


async def test_stopped_ask_includes_usage(settings, backend, document):
    client = ScriptedClient(
        [assessment([f"{document.document_id}:abc123:p1"])],
        usage_details={"input_token_count": 50, "output_token_count": 10},
    )
    result = await investigate("warranty", Investigation(settings, backend), client)
    assert result.status == "insufficient_context"
    assert result.usage.status == result.status
    assert result.usage.reported_tokens["total_tokens"] == 60


async def test_model_timeout_keeps_prior_assessment_usage(settings, backend, document):
    class SlowClient(ScriptedClient):
        async def _inner_get_response(self, **kwargs):
            if len(self.requests) == 2:
                await asyncio.sleep(10)
            return await super()._inner_get_response(**kwargs)

    settings.query_timeout_seconds = 1
    client = SlowClient(
        [
            tool_call("open_pages", document_id=document.document_id, start_page=1, end_page=1),
            assessment([], sufficient=False, gaps=["need more pages"]),
        ],
        usage_details={"input_token_count": 100, "output_token_count": 20},
    )
    result = await investigate("warranty", Investigation(settings, backend), client)
    assert result.status == "budget_exhausted"
    assert result.usage.reported_tokens["total_tokens"] == 240
    assert [event.status for event in result.usage.events] == ["completed", "failed"]
    assert result.usage.events[-1].total_tokens is None


async def test_rejected_answer_keeps_usage_without_leaking_content(
    settings, backend, document, caplog
):
    usage = UsageTracker("ask")
    eid = f"{document.document_id}:{document.revision}:p1"
    client = ScriptedClient(
        [
            tool_call("open_pages", document_id=document.document_id, start_page=1, end_page=1),
            assessment([eid]),
            answer(eid, "Fabricated private quote"),
        ],
        usage_details={"input_token_count": 100, "output_token_count": 20},
    )
    with pytest.raises(ValueError, match="substring"):
        await investigate("private question", Investigation(settings, backend), client, usage=usage)
    report = usage.report("failed")
    assert report.reported_tokens["total_tokens"] == 360
    assert "private question" not in report.model_dump_json() + caplog.text
    assert "Fabricated private quote" not in report.model_dump_json() + caplog.text
    assert "Warranty lasts" not in report.model_dump_json() + caplog.text


async def test_no_hits_report_does_not_invent_tokens(settings, backend):
    backend.hits = []
    result = await investigate("unknown", Investigation(settings, backend), ScriptedClient([]))
    assert result.usage.events == []
    assert result.usage.reported_tokens["total_tokens"] is None


@pytest.mark.parametrize("fail", [False, True])
async def test_cli_report_written_on_completion_or_failure(
    settings, monkeypatch, tmp_path, fail, capsys
):
    path = tmp_path / "usage.json"
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    async def execute(args, settings, usage):
        with usage.operation("embedding", settings.embedding_deployment) as event:
            event.record({"input_token_count": 12, "output_token_count": 0})
        if fail:
            raise RuntimeError("index unavailable")
        return None

    monkeypatch.setattr(cli, "_run", execute)
    args = argparse.Namespace(command="ingest", usage_report=path)
    if fail:
        with pytest.raises(RuntimeError, match="index unavailable"):
            await cli.run(args)
    else:
        await cli.run(args)
    report = json.loads(path.read_text())
    assert report["status"] == ("failed" if fail else "completed")
    assert report["reported_tokens"]["input_tokens"] == 12
    output = capsys.readouterr().out
    assert (not output) if fail else json.loads(output) == report


async def test_report_path_never_overwrites_before_api_calls(settings, monkeypatch, tmp_path):
    path = tmp_path / "existing.json"
    path.write_text("important")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    execute = AsyncMock()
    monkeypatch.setattr(cli, "_run", execute)
    with pytest.raises(FileExistsError):
        await cli.run(argparse.Namespace(command="ingest", usage_report=path))
    execute.assert_not_called()
    assert path.read_text() == "important"


@pytest.mark.parametrize("service_fails", [False, True])
async def test_report_write_error_is_explicit_without_masking_service_failure(
    settings, monkeypatch, tmp_path, service_fails, caplog
):
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        cli, "_run",
        AsyncMock(
            return_value=None,
            side_effect=RuntimeError("service failed") if service_fails else None,
        ),
    )

    def write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(cli, "write_usage_report", write)
    expected = RuntimeError if service_fails else OSError
    with pytest.raises(expected, match="service failed" if service_fails else "disk full"):
        await cli.run(argparse.Namespace(command="ingest", usage_report=tmp_path / "usage.json"))
    assert "Could not write usage report" in caplog.text


async def test_cli_ask_keeps_answer_shape_and_matches_saved_report(
    settings, backend, monkeypatch, tmp_path, capsys
):
    backend.hits = []
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    async def execute(args, settings, usage):
        return await investigate(
            "unknown", Investigation(settings, backend), ScriptedClient([]), usage=usage
        )

    monkeypatch.setattr(cli, "_run", execute)
    path = tmp_path / "ask.json"
    await cli.run(argparse.Namespace(command="ask", usage_report=path))
    output = json.loads(capsys.readouterr().out)
    assert output["answer"] is None
    assert output["status"] == "insufficient_context"
    assert output["usage"] == json.loads(path.read_text())
    assert output["usage"]["counters"]["iq_searches"] == 1


@pytest.mark.parametrize("command", [["ingest"], ["ask", "a question"]])
def test_usage_report_parser(monkeypatch, tmp_path, command):
    path = tmp_path / "usage.json"
    execute = AsyncMock()
    monkeypatch.setattr(cli, "run", execute)
    monkeypatch.setattr("sys.argv", ["hrag", *command, "--usage-report", str(path)])
    cli.main()
    assert execute.call_args.args[0].usage_report == path

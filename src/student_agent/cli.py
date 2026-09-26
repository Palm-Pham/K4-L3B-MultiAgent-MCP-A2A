from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .cases import load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import connect_gateway
from .submission import package_submission, validate_artifacts
from .trace import TraceWriter
from .workflow import solve_case


def _root(value: str) -> Path:
    return Path(value).resolve()


async def _check_mcp(root: Path, case_id: str) -> None:
    """Check essential order retrieval without modifying submission artifacts."""
    settings = Settings.load(root)
    cases = load_case_set(root)
    if case_id not in cases.cases:
        raise ValueError(f"unknown case: {case_id}")
    case = cases.cases[case_id]
    request = case.get("customer_request")
    order_id = request.get("claimed_order_id") if isinstance(request, dict) else None
    if not isinstance(order_id, str) or not order_id:
        raise ValueError(f"{case_id} has no claimed_order_id for the MCP check")
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        if "get_order" not in await gateway.list_tools():
            raise RuntimeError("MCP Gateway does not expose get_order")
        evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
        print(f"OK: {case_id} / order evidence {evidence['evidence_ref']}")


def _error_messages(exc: BaseException) -> list[str]:
    if isinstance(exc, BaseExceptionGroup):
        return [message for child in exc.exceptions for message in _error_messages(child)]
    return [f"{type(exc).__name__}: {exc}"]


async def _show_tools(root: Path) -> None:
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        for tool in await gateway.list_tools():
            print(tool)


def _prepare_resume(
    output_root: Path, trace_path: Path, case_ids: tuple[str, ...], contracts: Contracts
) -> set[str]:
    """Validate completed outputs and remove trace events from an interrupted case."""
    output_files = {path.stem: path for path in output_root.glob("*.json") if path.is_file()}
    unknown = sorted(set(output_files) - set(case_ids))
    if unknown:
        raise ValueError(f"cannot resume with unknown outputs: {unknown}")

    completed: set[str] = set()
    for case_id, path in output_files.items():
        output = json.loads(path.read_text(encoding="utf-8"))
        contracts.validate_output(output, f"outputs/{case_id}.json")
        if output.get("case_id") != case_id:
            raise ValueError(f"outputs/{case_id}.json has a mismatched case_id")
        completed.add(case_id)

    retained: list[str] = []
    if trace_path.exists():
        for number, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            contracts.validate_trace(event, f"traces/trace.jsonl:{number}")
            if event["case_id"] in completed:
                retained.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    temporary = trace_path.with_suffix(".jsonl.tmp")
    temporary.write_text("\n".join(retained) + ("\n" if retained else ""), encoding="utf-8")
    temporary.replace(trace_path)
    return completed


async def _run(
    root: Path, *, resume: bool = False, continue_on_error: bool = False
) -> None:
    settings = Settings.load(root)
    case_set = load_case_set(root)
    contracts = Contracts(root / "contracts" / "schemas")
    output_root = root / "outputs"
    trace_path = root / "traces" / "trace.jsonl"
    output_root.mkdir(parents=True, exist_ok=True)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    if resume:
        completed = _prepare_resume(output_root, trace_path, case_set.case_ids, contracts)
        print(f"RESUME: keeping {len(completed)} completed outputs")
    else:
        completed = set()
        for stale in output_root.glob("*.json"):
            stale.unlink()
        trace_path.unlink(missing_ok=True)
    trace = TraceWriter(trace_path, contracts)

    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        discovered_tools = await gateway.list_tools()
        if not discovered_tools:
            raise RuntimeError("MCP Gateway returned no tools")
        failed: list[str] = []
        for case_id in case_set.case_ids:
            if case_id in completed:
                continue
            print(f"RUN: {case_id}", flush=True)
            case = case_set.cases[case_id]
            try:
                trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
                output = await solve_case(case, gateway, trace)
                contracts.validate_output(output, f"outputs/{case_id}.json")
                if output.get("case_id") != case_id:
                    raise ValueError(f"solver returned a mismatched case_id for {case_id}")
                target = output_root / f"{case_id}.json"
                temporary = target.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                temporary.replace(target)
                trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
            except (OSError, RuntimeError, ValueError) as exc:
                if not continue_on_error:
                    raise
                failed.append(case_id)
                print(f"SKIP: {case_id}: {exc}", file=sys.stderr, flush=True)
        if failed:
            raise RuntimeError(
                f"run completed with {len(failed)} failed cases: {', '.join(failed)}"
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Day09 L3B student workflow")
    result.add_argument("--root", default=".", help="repository root (default: current directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-inputs", help="validate case-set.json and all 100 inputs")
    commands.add_parser("mcp-tools", help="authenticate and list discovered MCP tools")
    check = commands.add_parser("mcp-check", help="check order retrieval without changing outputs")
    check.add_argument("--case-id", default="L3B_CASE_001")
    run = commands.add_parser("run", help="run the implemented workflow for all cases")
    run.add_argument(
        "--resume", action="store_true", help="keep valid completed outputs and continue"
    )
    run.add_argument(
        "--continue-on-error", action="store_true", help="continue after a failed case"
    )
    commands.add_parser("validate", help="validate outputs and observable trace")
    package = commands.add_parser("package", help="validate and build the submission ZIP")
    package.add_argument("--output", default="dist/submission.zip")
    return result


def main() -> None:
    args = parser().parse_args()
    root = _root(args.root)
    try:
        if args.command == "validate-inputs":
            case_set = load_case_set(root)
            print(
                f"OK: {case_set.variant_id} / {case_set.version} / "
                f"{len(case_set.case_ids)} cases"
            )
        elif args.command == "mcp-tools":
            asyncio.run(_show_tools(root))
        elif args.command == "mcp-check":
            asyncio.run(_check_mcp(root, args.case_id))
        elif args.command == "run":
            asyncio.run(
                _run(root, resume=args.resume, continue_on_error=args.continue_on_error)
            )
        elif args.command == "validate":
            case_set = load_case_set(root)
            contracts = Contracts(root / "contracts" / "schemas")
            _, trace = validate_artifacts(root, case_set, contracts)
            print(f"OK: {len(case_set.case_ids)} outputs / {len(trace)} trace events")
        elif args.command == "package":
            destination = package_submission(root, root / args.output)
            print(f"OK: {destination}")
    except (OSError, RuntimeError, ValueError, ExceptionGroup) as exc:
        print("ERROR: " + " | ".join(dict.fromkeys(_error_messages(exc))), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

"""Human CLI for claimrev: inspect state, approve decisions, recover exports, and run plugins.

Human decision resolution (`approve`) is intentionally exposed ONLY through this out-of-band CLI
and NEVER registered as an MCP tool. In single-account local operation, this boundary is enforced
at the product interface level (documented in README).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .computations import ComputationRegistry
from .errors import ClaimRevError
from .service import ClaimRevService


def _load_registry(plugins: list[str] | None = None) -> ComputationRegistry:
    reg = ComputationRegistry()
    specs = list(plugins or [])
    env_plugins = os.environ.get("CLAIMREV_PLUGINS", "").strip()
    if env_plugins:
        for item in env_plugins.split(","):
            item = item.strip()
            if item and item not in specs:
                specs.append(item)
    for spec in specs:
        reg.load_plugin_spec(spec)
    return reg


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claimrev", description="claimrev ledger & human approval CLI")
    p.add_argument(
        "--store",
        default=os.environ.get("CLAIMREV_STORE", str(Path.home() / ".claimrev" / "default")),
        help="Path to claimrev store directory (or set CLAIMREV_STORE)",
    )
    p.add_argument(
        "--plugin",
        action="append",
        default=[],
        help="Computation plugin import spec (module:register). Can also set CLAIMREV_PLUGINS.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # status
    p_status = sub.add_parser("status", help="Show task state, claims, data series, and pending decisions")
    p_status.add_argument("--task-id", default=None)
    p_status.add_argument("--all-statuses", action="store_true")

    # decisions
    p_dec = sub.add_parser("decisions", help="List pending human decisions")
    p_dec.add_argument("--task-id", default=None)

    # approve (Human-only out-of-band CLI approval channel!)
    p_app = sub.add_parser("approve", help="Approve a pending human decision card (human CLI only)")
    p_app.add_argument("--decision-id", required=True)
    p_app.add_argument("--option", required=True, help="Exact option_id from the decision card")
    p_app.add_argument("--idempotency-key", default=None)
    p_app.add_argument("--resolved-by", default="human-cli")
    p_app.add_argument(
        "--resolve-only",
        action="store_true",
        help="Only mark decision RESOLVED without immediately committing the proposal",
    )

    # approve-web (Thin local browser Human Decision Surface on 127.0.0.1 with one-time token)
    p_web = sub.add_parser("approve-web", help="Launch local browser approval card (127.0.0.1 + one-time token)")
    p_web.add_argument("--decision-id", default=None, help="Specific decision_id to open (defaults to latest pending)")
    p_web.add_argument("--port", type=int, default=0, help="Local port (0 = automatic ephemeral port)")
    p_web.add_argument("--no-open", action="store_true", help="Do not auto-open the system browser")

    # commit
    p_com = sub.add_parser("commit", help="Commit a READY or RESOLVED proposal")
    p_com.add_argument("--proposal-id", required=True)
    p_com.add_argument("--task-id", default=None)

    # recover
    p_rec = sub.add_parser("recover", help="Rebuild missing/interrupted report exports from SQLite + archive")
    p_rec.add_argument("--task-id", default=None)

    # ingest-batch
    p_batch = sub.add_parser("ingest-batch", help="Ingest a new data batch for an existing series")
    p_batch.add_argument("--task-id", default=None)
    p_batch.add_argument("--series-id", required=True)
    p_batch.add_argument("--file", required=True)
    p_batch.add_argument("--vintage-tag", default=None)
    p_batch.add_argument("--release-key", default=None)

    # seed-example (generic loader hook so core does not import examples statically)
    p_seed = sub.add_parser("seed-module", help="Run a seed function spec (e.g. pkg.seed:seed_initial_report)")
    p_seed.add_argument("--spec", required=True, help="module:callable accepting store_dir")
    p_seed.add_argument("--reset", action="store_true", help="Remove existing store directory before seeding")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store_dir = Path(args.store).expanduser().resolve()

    try:
        if args.cmd == "seed-module":
            if args.reset and store_dir.exists():
                import shutil

                shutil.rmtree(store_dir)
            import importlib

            mod_name, fn_name = args.spec.split(":", 1)
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
            res = fn(store_dir)
            _print_json(res)
            return 0

        reg = _load_registry(args.plugin)
        svc = ClaimRevService(store_dir, registry=reg)

        if args.cmd == "status":
            res = svc.get_task_state(task_id=args.task_id, include_all_statuses=args.all_statuses)
            _print_json(res)
        elif args.cmd == "decisions":
            state = svc.get_task_state(task_id=args.task_id)
            _print_json(state["pending_decisions"])
        elif args.cmd == "approve":
            res = svc.resolve_decision(
                decision_id=args.decision_id,
                selected_option=args.option,
                idempotency_key=args.idempotency_key,
                resolved_by=args.resolved_by,
                auto_commit=not args.resolve_only,
            )
            _print_json(res)
        elif args.cmd == "approve-web":
            import webbrowser
            from .approval_ui import create_approval_server

            server, url, _ = create_approval_server(svc, decision_id=args.decision_id, port=args.port)
            print(f"Human Decision Surface 已启动：{url}", flush=True)
            print("按 Ctrl+C 退出本地审批服务。", flush=True)
            if not args.no_open:
                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.cmd == "commit":
            res = svc.commit_revision(proposal_id=args.proposal_id, task_id=args.task_id)
            _print_json(res)
        elif args.cmd == "recover":
            res = svc.recover_exports(task_id=args.task_id)
            _print_json(res)
        elif args.cmd == "ingest-batch":
            res = svc.ingest_data_batch(
                task_id=args.task_id,
                series_id=args.series_id,
                file_path=args.file,
                vintage_tag=args.vintage_tag,
                release_key=args.release_key,
            )
            _print_json(res)
        return 0
    except ClaimRevError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

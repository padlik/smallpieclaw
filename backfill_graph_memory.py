"""
backfill_graph_memory.py
------------------------
One-time CLI tool to seed the LadybugDB graph store from existing
LongTermMemory entries (data/longterm_memory.json).

Usage examples
--------------
# Dry-run: count what would be imported without touching anything
python backfill_graph_memory.py --config config.toml --dry-run

# Import up to 20 entries (incremental)
python backfill_graph_memory.py --config config.toml --limit 20

# Import everything, skipping already-imported entries
python backfill_graph_memory.py --config config.toml

# Re-import all entries regardless of prior state
python backfill_graph_memory.py --config config.toml --force

# Custom paths
python backfill_graph_memory.py --config config.toml \\
    --longterm-path data/longterm_memory.json \\
    --state-file data/graph_memory_backfill_state.json

Prerequisites
-------------
- Graph memory must be enabled in config.toml: [graph_memory] enabled = true
- ladybug package must be installed: pip install ladybug
- The main agent process must NOT be running against the same graph DB at the
  same time (LadybugDB embedded; single process access).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Callable

# ---------------------------------------------------------------------------
# Bootstrap — ensure the repo root is on sys.path regardless of cwd
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill")


def _load_toml(path: str) -> dict:
    try:
        import tomli  # type: ignore[import]
    except ImportError:
        import tomllib as tomli  # type: ignore[no-redef]  # Python 3.11+
    with open(path, "rb") as f:
        return tomli.load(f)


def _build_llm_call(cfg: dict, app_cfg, all_models: list) -> "Callable[[str], str]":
    """Build a one-shot LLM callable that uses the extraction model."""
    from config_schema import resolve_model_id
    from llm_client import LLMClient
    from token_usage import get_registry as get_token_registry

    gm_cfg = app_cfg.graph_memory
    extraction_selector = gm_cfg.extraction_model or app_cfg.agent.default_model
    extraction_model_id = resolve_model_id(extraction_selector, all_models)
    if not extraction_model_id:
        extraction_model_id = resolve_model_id(app_cfg.agent.default_model, all_models) or ""

    extraction_model_cfg = next(
        (m for m in all_models if m.get("model") == extraction_model_id),
        all_models[0] if all_models else {},
    )
    # Force low-temperature, bounded extraction. chat() reads temperature/max_tokens
    # from the active model config (not call kwargs), so inject them onto a COPY of
    # the model dict — never mutate the shared all_models entry. Reasoning models
    # strip temperature automatically inside LLMClient, so this is provider-safe.
    extraction_model_cfg = dict(extraction_model_cfg)
    extraction_model_cfg["temperature"] = 0.1
    extraction_model_cfg["max_tokens"] = 1024
    extraction_model_name = extraction_model_cfg.get("model", "")
    other_models = [m for m in all_models if m.get("model") != extraction_model_name]
    extraction_cfg = dict(cfg)
    extraction_cfg["models"] = [extraction_model_cfg] + other_models
    extraction_agent = dict(cfg.get("agent", {}))
    extraction_agent["default_model"] = extraction_model_name
    extraction_cfg["agent"] = extraction_agent

    logger.info("Extraction model: %s", extraction_model_id or "(default)")

    def _llm_call(prompt: str) -> str:
        llm = LLMClient(extraction_cfg, usage_registry=get_token_registry(), caller_tag="backfill")
        try:
            messages = [{"role": "user", "content": prompt}]
            return llm.chat(messages)
        finally:
            llm.close()

    return _llm_call


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the LadybugDB graph store from existing LongTermMemory entries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--config", default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )
    parser.add_argument(
        "--longterm-path", default="",
        help="Override path to longterm_memory.json",
    )
    parser.add_argument(
        "--state-file", default="",
        help="Override path to backfill state file (default: data/graph_memory_backfill_state.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count and preview without writing to the graph or updating state",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process at most N entries (useful for incremental runs)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore existing state file and reprocess all entries",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-entry results",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load config
    # ------------------------------------------------------------------
    if not os.path.exists(args.config):
        logger.error("Config file not found: %s", args.config)
        sys.exit(1)

    raw_cfg = _load_toml(args.config)

    from config_schema import parse_config
    from exceptions import ConfigError
    try:
        app_cfg = parse_config(raw_cfg)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Check graph memory is enabled + ladybug is available
    # ------------------------------------------------------------------
    if not app_cfg.graph_memory.enabled:
        logger.error(
            "Graph memory is not enabled in config.toml.\n"
            "Set [graph_memory] enabled = true and run again."
        )
        sys.exit(1)

    try:
        import ladybug  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        logger.error(
            "The 'ladybug' package is not installed.\n"
            "Install it with: pip install ladybug"
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Resolve paths
    # ------------------------------------------------------------------
    gm_cfg = app_cfg.graph_memory
    paths = raw_cfg.get("paths", {})
    longterm_path = (
        args.longterm_path
        or paths.get("longterm_memory_file", "data/longterm_memory.json")
    )
    state_file = (
        args.state_file
        or os.path.join(os.path.dirname(longterm_path), "graph_memory_backfill_state.json")
    )

    if not os.path.exists(longterm_path):
        logger.warning("LongTermMemory file not found: %s — nothing to import.", longterm_path)
        sys.exit(0)

    # ------------------------------------------------------------------
    # Load LongTermMemory entries
    # ------------------------------------------------------------------
    from memory_store import LongTermMemory
    from llm_client import LLMClient
    from token_usage import get_registry as get_token_registry

    logger.info("Loading LongTermMemory from %s", longterm_path)
    # For dry-run we only need the file reader, not the embed LLM.
    # Avoid spinning up LLMClient (and incurring API calls) until needed.
    long_term = LongTermMemory(path=longterm_path, llm=None)
    entries = long_term.entries()

    logger.info("Found %d LongTermMemory entries", len(entries))
    if not entries:
        logger.info("Nothing to import.")
        sys.exit(0)

    # Compute limit display
    logger.info(
        "Mode: %s  |  Limit: %s  |  Force: %s  |  State: %s",
        "DRY-RUN" if args.dry_run else "LIVE",
        str(args.limit) if args.limit is not None else "all",
        str(args.force),
        state_file,
    )

    # ------------------------------------------------------------------
    # DRY-RUN fast path — count/preview without opening the graph DB,
    # calling embeddings, or making LLM calls.
    # ------------------------------------------------------------------
    if args.dry_run:
        from graph_memory import (
            BackfillResult,
            BackfillEntryResult,
            _load_backfill_state,
            _entry_checksum,
        )
        logger.info("DRY-RUN — counting entries only; no DB, embeddings, or LLM calls.")
        state = _load_backfill_state(state_file)
        imported_map = state.get("imported", {})
        result = BackfillResult(total=len(entries))
        processed = 0
        for entry_id, entry in entries:
            if args.limit is not None and processed >= args.limit:
                break
            checksum = _entry_checksum(entry)
            if not args.force and entry_id in imported_map:
                if imported_map[entry_id].get("checksum") == checksum:
                    result.skipped += 1
                    result.entries.append(BackfillEntryResult(entry_id=entry_id, status="skipped"))
                    continue
            processed += 1
            result.imported += 1
            result.entries.append(BackfillEntryResult(entry_id=entry_id, status="imported (dry-run)"))

        _print_summary(result, dry_run=True, verbose=args.verbose)
        sys.exit(0)

    # ------------------------------------------------------------------
    # LIVE path — open LLM client, embedding, and graph store.
    # ------------------------------------------------------------------
    llm = LLMClient(raw_cfg, usage_registry=get_token_registry(), caller_tag="backfill-embed")

    # ------------------------------------------------------------------
    # Build graph store
    # ------------------------------------------------------------------
    from graph_memory import (
        GraphMemoryStore,
        backfill_longterm_to_graph,
    )

    logger.info("Opening graph store at %s", gm_cfg.db_path)
    try:
        embedding_dim = len(llm.embed("test"))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to determine embedding dimension: %s", exc)
        llm.close()
        sys.exit(1)

    try:
        store = GraphMemoryStore(
            db_path=gm_cfg.db_path,
            embedder_fn=lambda text: llm.embed(text),
            embedding_dim=embedding_dim,
            buffer_pool_mb=gm_cfg.buffer_pool_mb,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to open graph store: %s", exc)
        llm.close()
        sys.exit(1)

    # ------------------------------------------------------------------
    # Build extraction LLM callable
    # ------------------------------------------------------------------
    all_models = raw_cfg.get("models", [])
    llm_call_fn = _build_llm_call(raw_cfg, app_cfg, all_models)

    # ------------------------------------------------------------------
    # Run backfill
    # ------------------------------------------------------------------
    logger.info("Starting backfill...")
    try:
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=store,
            llm_call_fn=llm_call_fn,
            state_path=state_file,
            dry_run=False,
            limit=args.limit,
            force=args.force,
        )
    finally:
        store.close()
        llm.close()

    _print_summary(result, dry_run=False, verbose=args.verbose)

    if result.failed > 0:
        sys.exit(1)


def _print_summary(result, *, dry_run: bool, verbose: bool) -> None:
    print()
    print("=" * 60)
    print("Backfill complete" + (" (DRY-RUN)" if dry_run else ""))
    print("=" * 60)
    print(f"  Total entries:      {result.total}")
    print(f"  Imported:           {result.imported}")
    print(f"  Skipped (cached):   {result.skipped}")
    print(f"  No extraction:      {result.no_extraction}")
    print(f"  Failed:             {result.failed}")
    print(f"  Total entities:     {result.total_entities}")
    print(f"  Total facts:        {result.total_facts}")
    if dry_run:
        print()
        print("  [DRY-RUN] No graph writes, embeddings, or LLM calls were made.")
    print("=" * 60)

    if verbose or result.failed > 0:
        print()
        for er in result.entries:
            if verbose or er.status == "failed":
                line = f"  {er.entry_id[:12]}..  {er.status}"
                if er.entities or er.facts:
                    line += f"  ({er.entities} entities, {er.facts} facts)"
                if er.error:
                    line += f"  ERROR: {er.error}"
                print(line)


if __name__ == "__main__":
    main()

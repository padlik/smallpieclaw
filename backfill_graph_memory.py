"""
backfill_graph_memory.py
------------------------
One-time CLI tool to seed the LadybugDB graph store from existing
LongTermMemory entries. Defaults to ``$XDG_DATA_HOME/<agent_name>/longterm_memory.json``.

Usage examples
--------------
# Dry-run: count what would be imported without touching anything
python backfill_graph_memory.py --config config.toml --agent-name piclaw --dry-run

# Import up to 20 entries (incremental)
python backfill_graph_memory.py --config config.toml --agent-name piclaw --limit 20

# Import everything, skipping already-imported entries
python backfill_graph_memory.py --config config.toml --agent-name piclaw

# Re-import all entries regardless of prior state
python backfill_graph_memory.py --config config.toml --agent-name piclaw --force

# Override paths explicitly (e.g. importing from a different agent's export)
python backfill_graph_memory.py --config config.toml --agent-name piclaw \\
    --longterm-path /path/to/longterm_memory.json \\
    --state-file /path/to/graph_memory_backfill_state.json

Prerequisites
-------------
- Graph memory must be enabled in config.toml: [graph_memory] enabled = true
- ladybug package must be installed: pip install ladybug
- The main agent process must NOT be running against the same graph DB at the
  same time (LadybugDB embedded; single process access).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from graph_memory import (
    EXTRACTION_PROMPT,
    GraphMemoryStore,
    parse_extraction,
)
from providers._errors import LLMError

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


class _ProgressPrinter:
    """Per-entry progress display for the backfill CLI.

    Compatible with the ``notify_fn`` parameter of ``backfill_longterm_to_graph``.

    - **TTY**: overwrites a single line using ``\\r`` with an ASCII progress bar.
    - **verbose** (``--verbose``): prints one permanent line per entry.
    - **Non-TTY / pipe**: emits ``logger.info`` every 10 entries and on failure.
    """

    def __init__(self, total: int, verbose: bool) -> None:
        self._total = total
        self._verbose = verbose
        self._is_tty = sys.stdout.isatty()
        self._did_print = False

    def __call__(
        self,
        current: int,
        total: int,
        result: object,
        entry_result: object,
    ) -> None:
        self._did_print = True
        eid: str = getattr(entry_result, "entry_id", "")
        status: str = getattr(entry_result, "status", "")
        entities: int = getattr(entry_result, "entities", 0)
        facts: int = getattr(entry_result, "facts", 0)
        error: str = getattr(entry_result, "error", "")
        imported: int = getattr(result, "imported", 0)
        skipped: int = getattr(result, "skipped", 0)
        failed: int = getattr(result, "failed", 0)

        if self._verbose:
            w = len(str(total))
            line = f"  {current:{w}}/{total}  {eid[:16]}  {status}"
            if entities or facts:
                line += f"  ({entities} ent, {facts} fact)"
            if error:
                line += f"  ERROR: {error}"
            print(line)
            return

        if self._is_tty:
            term_width = shutil.get_terminal_size(fallback=(80, 24)).columns
            pct = current * 100 // total if total else 100
            bar_width = min(20, max(10, term_width // 5))
            filled = bar_width * current // total if total else bar_width
            bar = "█" * filled + "░" * (bar_width - filled)
            w = len(str(total))
            short_status = (
                status
                .replace("imported (dry-run)", "dry-run")
                .replace("no_extraction", "no-extr")
            )
            counters = f"imp:{imported} skip:{skipped} fail:{failed}"
            line = f"\r  [{current:{w}}/{total}] {bar} {pct:>3}%  {counters}  {eid[:8]}.. {short_status}"
            if len(line) > term_width:
                line = line[:term_width]
            sys.stdout.write(line)
            sys.stdout.flush()
        else:
            if status == "failed" or current % 10 == 0:
                logger.info(
                    "Backfill progress: %d/%d | imp:%d skip:%d fail:%d | %s %s",
                    current, total, imported, skipped, failed, eid[:16], status,
                )

    def finalize(self) -> None:
        """Print a trailing newline to end the ``\\r`` progress line (TTY only)."""
        if self._did_print and self._is_tty and not self._verbose:
            print()


def _load_toml(path: str) -> dict:
    try:
        import tomli  # type: ignore[import]
    except ImportError:
        import tomllib as tomli  # type: ignore[no-redef]  # Python 3.11+
    with open(path, "rb") as f:
        return tomli.load(f)


# ---------------------------------------------------------------------------
# LongTermMemory → Graph backfill service
# ---------------------------------------------------------------------------


@dataclass
class BackfillEntryResult:
    """Outcome for a single LongTermMemory entry."""

    entry_id: str
    status: str           # "imported" | "skipped" | "no_extraction" | "failed"
    entities: int = 0
    facts: int = 0
    episode_id: str = ""
    error: str = ""


@dataclass
class BackfillResult:
    """Summary of a complete backfill run."""

    total: int = 0
    imported: int = 0
    skipped: int = 0
    no_extraction: int = 0
    failed: int = 0
    total_entities: int = 0
    total_facts: int = 0
    entries: list[BackfillEntryResult] = field(default_factory=list)


def _entry_checksum(entry: dict) -> str:
    """Stable SHA-256 fingerprint for a LongTermMemory entry dict."""
    sig = f"{entry.get('content', '')}|{entry.get('source', '')}|{entry.get('timestamp', '')}"
    return "sha256:" + hashlib.sha256(sig.encode()).hexdigest()


def _load_backfill_state(state_path: str) -> dict:
    """Load the migration state JSON; return empty dict on missing/corrupt file."""
    if not os.path.exists(state_path):
        return {"version": 1, "imported": {}}
    try:
        with open(state_path) as f:
            data = json.load(f)
        if not isinstance(data.get("imported"), dict):
            raise ValueError("malformed state file")
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("Backfill state file unreadable (%s) — starting fresh", exc)
        return {"version": 1, "imported": {}}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Backfill state file unreadable with unexpected error (%s) — starting fresh", exc)
        return {"version": 1, "imported": {}}


def _save_backfill_state(state_path: str, state: dict) -> None:
    """Atomic write of the migration state JSON.

    Raises OSError on failure so callers know idempotency state was not
    persisted and can treat the entry as failed rather than silently
    risking duplicate imports on the next run.
    """
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    tmp = f"{state_path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, state_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def backfill_longterm_to_graph(
    long_term_entries: list[tuple[str, dict]],
    store: GraphMemoryStore,
    llm_call_fn: Callable[[str], str],
    state_path: str,
    dry_run: bool = False,
    limit: Optional[int] = None,
    force: bool = False,
    notify_fn: Optional[Callable[[int, int, "BackfillResult", "BackfillEntryResult"], None]] = None,
) -> BackfillResult:
    """Seed the graph store from a snapshot of LongTermMemory entries.

    Parameters
    ----------
    long_term_entries : list of (entry_id, entry_dict) from LongTermMemory.entries()
    store             : live GraphMemoryStore instance (must be open)
    llm_call_fn       : callable(prompt) -> str — same signature as GraphMemoryWriter
    state_path        : path to JSON state file tracking imported IDs
    dry_run           : if True, count/preview only — no graph writes, no state update
    limit             : stop after N entries (useful for incremental processing)
    force             : ignore state file and reprocess all entries
    notify_fn         : optional progress callback called after each entry is processed.
                        Signature: notify_fn(current, total, result, entry_result)
                        where current = number of entries processed so far (1-based).

    Returns a BackfillResult with per-entry outcomes and aggregate counts.
    """
    state = {} if dry_run else _load_backfill_state(state_path)
    imported_map: dict = state.get("imported", {})

    result = BackfillResult(total=len(long_term_entries))
    processed = 0

    def _notify(er: BackfillEntryResult) -> None:
        if notify_fn is not None:
            notify_fn(len(result.entries), result.total, result, er)

    for entry_id, entry in long_term_entries:
        if limit is not None and processed >= limit:
            break

        checksum = _entry_checksum(entry)

        # Skip if already imported with the same checksum
        if not force and entry_id in imported_map:
            existing = imported_map[entry_id]
            if existing.get("checksum") == checksum:
                result.skipped += 1
                er = BackfillEntryResult(entry_id=entry_id, status="skipped")
                result.entries.append(er)
                _notify(er)
                continue

        content = entry.get("content", "").strip()
        source = entry.get("source", "manual")
        timestamp = entry.get("timestamp", "")
        processed += 1

        if dry_run:
            # In dry-run mode just count; don't call LLM or touch graph
            result.imported += 1
            er = BackfillEntryResult(entry_id=entry_id, status="imported (dry-run)")
            result.entries.append(er)
            _notify(er)
            continue

        # Format a stable migration text block that the extraction prompt can parse
        migration_text = (
            f"Long-term memory entry {entry_id}\n"
            f"Source: {source}\n"
            f"Timestamp: {timestamp}\n\n"
            f"{content}"
        )

        prompt = EXTRACTION_PROMPT + migration_text
        try:
            response = llm_call_fn(prompt)
        except (LLMError, httpx.HTTPError, httpx.TimeoutException) as exc:
            result.failed += 1
            er = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
            result.entries.append(er)
            logger.warning("Backfill LLM extraction failed for %s: %s", entry_id, exc)
            _notify(er)
            continue
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            er = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
            result.entries.append(er)
            logger.warning("Backfill LLM extraction failed unexpectedly for %s: %s", entry_id, exc)
            _notify(er)
            continue

        extraction = parse_extraction(response)
        if not extraction:
            # No entities/facts found — still write episode for raw-text retrieval
            try:
                ep_id = store.add_episode(content[:2000], user_id="backfill", source="longterm_memory_backfill")
            except (KeyError, ValueError, RuntimeError) as exc:
                result.failed += 1
                er = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
                result.entries.append(er)
                logger.warning("Backfill add_episode failed for %s: %s", entry_id, exc)
                _notify(er)
                continue
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                er = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
                result.entries.append(er)
                logger.warning("Backfill add_episode failed unexpectedly for %s: %s", entry_id, exc)
                _notify(er)
                continue
            result.no_extraction += 1
            entry_result = BackfillEntryResult(
                entry_id=entry_id, status="no_extraction", episode_id=ep_id
            )
            result.entries.append(entry_result)
            prev_state_ne = imported_map.get(entry_id)
            imported_map[entry_id] = {
                "checksum": checksum,
                "episode_id": ep_id,
                "imported_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                _save_backfill_state(state_path, {"version": 1, "imported": imported_map})
            except OSError as exc:
                logger.error("Backfill state save failed for %s — skipping state update: %s", entry_id, exc)
                if prev_state_ne is not None:
                    imported_map[entry_id] = prev_state_ne
                else:
                    imported_map.pop(entry_id, None)
                result.no_extraction -= 1
                result.entries[-1] = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
                result.failed += 1
            _notify(result.entries[-1])
            continue

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entity_ids: dict[str, str] = {}
        entities_written = 0
        facts_written = 0

        for entity in extraction.entities:
            try:
                eid = store.upsert_entity(entity.name, entity.entity_type, ts)
                entity_ids[entity.name.lower()] = eid
                entities_written += 1
            except (KeyError, ValueError, RuntimeError) as exc:
                logger.debug("Backfill upsert_entity failed for '%s': %s", entity.name, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Backfill upsert_entity failed unexpectedly for '%s': %s", entity.name, exc)

        for fact in extraction.facts:
            src_key = fact.source.lower()
            tgt_key = fact.target.lower()
            src_id = entity_ids.get(src_key)
            tgt_id = entity_ids.get(tgt_key)
            try:
                if src_id is None:
                    src_id = store.upsert_entity(fact.source, "other", ts)
                    entity_ids[src_key] = src_id
                if tgt_id is None:
                    tgt_id = store.upsert_entity(fact.target, "other", ts)
                    entity_ids[tgt_key] = tgt_id
                store.add_relation(src_id, tgt_id, fact.relation_type, fact.fact, ts)
                facts_written += 1
            except (KeyError, ValueError, RuntimeError) as exc:
                logger.debug("Backfill add_relation failed: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Backfill add_relation failed unexpectedly: %s", exc)

        try:
            ep_id = store.add_episode(content[:2000], user_id="backfill", source="longterm_memory_backfill")
        except (KeyError, ValueError, RuntimeError) as exc:
            result.failed += 1
            er = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
            result.entries.append(er)
            logger.warning("Backfill add_episode failed for %s: %s", entry_id, exc)
            _notify(er)
            continue
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            er = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
            result.entries.append(er)
            logger.warning("Backfill add_episode failed unexpectedly for %s: %s", entry_id, exc)
            _notify(er)
            continue

        result.imported += 1
        result.total_entities += entities_written
        result.total_facts += facts_written
        entry_result = BackfillEntryResult(
            entry_id=entry_id,
            status="imported",
            entities=entities_written,
            facts=facts_written,
            episode_id=ep_id,
        )
        result.entries.append(entry_result)
        logger.debug(
            "Backfill: %s → %d entities, %d facts, ep=%s",
            entry_id,
            entities_written,
            facts_written,
            ep_id,
        )

        prev_state_main = imported_map.get(entry_id)
        imported_map[entry_id] = {
            "checksum": checksum,
            "episode_id": ep_id,
            "imported_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _save_backfill_state(state_path, {"version": 1, "imported": imported_map})
        except OSError as exc:
            logger.error("Backfill state save failed for %s — skipping state update: %s", entry_id, exc)
            if prev_state_main is not None:
                imported_map[entry_id] = prev_state_main
            else:
                imported_map.pop(entry_id, None)
            result.imported -= 1
            result.total_entities -= entities_written
            result.total_facts -= facts_written
            result.entries[-1] = BackfillEntryResult(entry_id=entry_id, status="failed", error=str(exc))
            result.failed += 1
        _notify(result.entries[-1])

    return result


def _build_llm_call(cfg: dict, app_cfg, all_models: list) -> "Callable[[str], str]":
    """Build a one-shot LLM callable that uses the extraction model.

    Delegates to the shared ``build_extraction_llm_call`` helper in graph_memory.py
    so that both the backfill CLI and the live main-process path use identical logic.
    """
    from graph_memory import build_extraction_llm_call

    return build_extraction_llm_call(cfg, app_cfg, all_models, caller_tag="backfill")


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
        help="Override path to longterm_memory.json (default: XDGPaths.data_home for --agent-name)",
    )
    parser.add_argument(
        "--agent-name", required=True,
        help="Agent name used to resolve default XDG paths",
    )
    parser.add_argument(
        "--db-path", default="",
        help="Override the graph DB path (default: XDGPaths.graph_memory_db for --agent-name)",
    )
    parser.add_argument(
        "--state-file", default="",
        help="Override path to backfill state file (default: XDGPaths.data_home/graph_memory_backfill_state.json)",
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
    from xdg import xdg_paths

    xdg = xdg_paths(args.agent_name)
    try:
        app_cfg = parse_config(raw_cfg, vault_file=str(xdg.secrets_file), agent_name=args.agent_name)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    # Use the resolved (env-expanded) config dict from here on.
    raw_cfg = app_cfg._raw

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
    db_path = args.db_path or str(xdg.graph_memory_db)
    longterm_path = args.longterm_path or str(xdg.data_home / "longterm_memory.json")
    state_file = args.state_file or str(xdg.data_home / "graph_memory_backfill_state.json")

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
        logger.info("DRY-RUN — counting entries only; no DB, embeddings, or LLM calls.")
        state = _load_backfill_state(state_file)
        imported_map = state.get("imported", {})
        result = BackfillResult(total=len(entries))
        processed = 0
        progress = _ProgressPrinter(len(entries), args.verbose)
        for entry_id, entry in entries:
            if args.limit is not None and processed >= args.limit:
                break
            checksum = _entry_checksum(entry)
            if not args.force and entry_id in imported_map:
                if imported_map[entry_id].get("checksum") == checksum:
                    result.skipped += 1
                    er = BackfillEntryResult(entry_id=entry_id, status="skipped")
                    result.entries.append(er)
                    progress(len(result.entries), len(entries), result, er)
                    continue
            processed += 1
            result.imported += 1
            er = BackfillEntryResult(entry_id=entry_id, status="imported (dry-run)")
            result.entries.append(er)
            progress(len(result.entries), len(entries), result, er)

        progress.finalize()
        _print_summary(result, dry_run=True, verbose=args.verbose)
        sys.exit(0)

    # ------------------------------------------------------------------
    # LIVE path — open LLM client, embedding, and graph store.
    # ------------------------------------------------------------------
    llm = LLMClient(raw_cfg, usage_registry=get_token_registry(), caller_tag="backfill-embed")

    # ------------------------------------------------------------------
    # Build graph store
    # ------------------------------------------------------------------
    logger.info("Opening graph store at %s", db_path)
    try:
        embedding_dim = len(llm.embed("test"))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to determine embedding dimension: %s", exc)
        llm.close()
        sys.exit(1)

    try:
        store = GraphMemoryStore(
            db_path=db_path,
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
    progress = _ProgressPrinter(len(entries), args.verbose)
    try:
        result = backfill_longterm_to_graph(
            long_term_entries=entries,
            store=store,
            llm_call_fn=llm_call_fn,
            state_path=state_file,
            dry_run=False,
            limit=args.limit,
            force=args.force,
            notify_fn=progress,
        )
    finally:
        store.close()
        llm.close()

    progress.finalize()
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

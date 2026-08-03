"""
migrate.py
----------
One-shot migration from the pre-XDG agent_home-relative layout to XDG Base
Directory paths.

    python migrate.py --agent-name <name> [--source <agent_home_dir>] [--dry-run]

Detection: the old layout is present if ``<source>/config.toml`` exists and no
migration sentinel exists yet in the XDG state home. Safe to run repeatedly —
every copy step is skip-if-destination-exists, and a sentinel is written after
a successful run so subsequent invocations are no-ops.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from xdg import XDGPaths, migration_sentinel_exists, write_migration_sentinel, xdg_paths

logger = logging.getLogger(__name__)


def _copy_file(src: Path, dest: Path, *, dry_run: bool, summary: list[str]) -> None:
    """Copy *src* to *dest* via a temp file + rename. Skips if dest already exists."""
    if not src.exists():
        return
    if dest.exists():
        summary.append(f"skip (exists): {dest}")
        return
    if dry_run:
        summary.append(f"would copy: {src} -> {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
    shutil.copy2(src, tmp_dest)
    tmp_dest.rename(dest)
    summary.append(f"copied: {src} -> {dest}")


def _copy_tree(src: Path, dest: Path, *, dry_run: bool, summary: list[str]) -> None:
    """Recursively copy *src* directory to *dest* via a temp dir + rename. Skips if dest already exists."""
    if not src.is_dir():
        return
    if dest.exists():
        summary.append(f"skip (exists): {dest}")
        return
    if dry_run:
        summary.append(f"would copy tree: {src} -> {dest}")
        return
    tmp_dest = dest.with_name(dest.name + ".tmp")
    if tmp_dest.exists():
        shutil.rmtree(tmp_dest)
    shutil.copytree(src, tmp_dest)
    tmp_dest.rename(dest)
    summary.append(f"copied tree: {src} -> {dest}")


def _run_migration_steps(paths: XDGPaths, source: Path, *, dry_run: bool) -> list[str]:
    """Execute all migration steps in order. Returns a human-readable summary."""
    summary: list[str] = []

    _copy_file(source / "config.toml", paths.config_file, dry_run=dry_run, summary=summary)
    _copy_file(source / "scheduler.toml", paths.scheduler_config, dry_run=dry_run, summary=summary)
    _copy_file(source / "data" / "memory.json", paths.memory_file, dry_run=dry_run, summary=summary)

    for variant_name in ("graph_memory", "graph_memory.wal", "graph_memory.wal.checkpoint"):
        variant = source / "data" / variant_name
        _copy_file(variant, paths.data_home / variant_name, dry_run=dry_run, summary=summary)

    _copy_file(source / "data" / "scheduler_state.json", paths.scheduler_state, dry_run=dry_run, summary=summary)
    _copy_file(source / "data" / "scheduler_commands.json", paths.scheduler_commands, dry_run=dry_run, summary=summary)
    _copy_file(source / "data" / "scheduled_jobs.json", paths.scheduler_jobs, dry_run=dry_run, summary=summary)
    _copy_file(source / "data" / "job_execution_log.jsonl", paths.job_execution_log, dry_run=dry_run, summary=summary)
    _copy_file(source / "data" / "results_memory.json", paths.data_home / "results_memory.json", dry_run=dry_run, summary=summary)
    _copy_file(source / "data" / "longterm_memory.json", paths.data_home / "longterm_memory.json", dry_run=dry_run, summary=summary)
    _copy_file(
        source / "data" / "graph_memory_backfill_state.json",
        paths.data_home / "graph_memory_backfill_state.json",
        dry_run=dry_run, summary=summary,
    )
    _copy_file(source / "data" / "trusted_dirs.json", paths.state_home / "trusted_dirs.json", dry_run=dry_run, summary=summary)
    _copy_tree(source / "skills", paths.skills_dir, dry_run=dry_run, summary=summary)

    tool_index = source / "data" / "tool_index.json"
    if tool_index.exists():
        if dry_run:
            summary.append(f"would delete (regeneratable): {tool_index}")
        else:
            tool_index.unlink()
            summary.append(f"deleted (regeneratable): {tool_index}")

    return summary


def main(agent_name: str, source: Path, dry_run: bool = False) -> list[str]:
    """Run the migration for *agent_name* from *source*. Returns a summary of actions.

    Exits silently (returns an empty summary) if no old layout is present, or
    if a migration sentinel already exists.
    """
    paths = xdg_paths(agent_name)
    source = Path(source)

    if migration_sentinel_exists(paths):
        return []
    if not (source / "config.toml").exists():
        return []

    summary = _run_migration_steps(paths, source, dry_run=dry_run)

    if not dry_run:
        paths.state_home.mkdir(parents=True, exist_ok=True)
        write_migration_sentinel(paths)

    for line in summary:
        logger.info("migrate: %s", line)

    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate agent storage to XDG Base Directory paths.")
    parser.add_argument("--agent-name", required=True, help="Agent name (resolves XDG target paths)")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).parent,
        help="Directory containing the old agent_home-relative layout (default: this script's directory)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing anything")
    return parser


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_parser().parse_args()
    result = main(args.agent_name, args.source, dry_run=args.dry_run)
    if not result:
        print("Nothing to migrate.")
    else:
        print(f"Migration {'(dry run) ' if args.dry_run else ''}summary:")
        for line in result:
            print(f"  {line}")
    sys.exit(0)

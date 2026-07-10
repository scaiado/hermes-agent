"""CLI commands for the shadow memory provider.

Commands are gated by the active memory.provider being "shadow" and are
registered under ``hermes shadow <subcommand>``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.config import load_config, cfg_get


def _provider_config() -> dict[str, Any]:
    """Return the shadow configuration, preferring config.yaml."""
    config = load_config()
    shadow = cfg_get(config, "memory", "shadow", default={}) or {}
    # Local JSON overrides if the desktop UI wrote it directly.
    json_path = get_hermes_home() / "shadow" / "config.json"
    if json_path.exists():
        try:
            local = json.loads(json_path.read_text())
            shadow.update(local)
        except Exception:
            pass
    return shadow


def _report_path() -> Path:
    return get_hermes_home() / "memories" / "shadow.db"


def _load_store() -> Any:
    from plugins.memory.shadow.shadow_store import ShadowEvidenceStore
    return ShadowEvidenceStore(_report_path())


def cmd_preflight(args) -> None:
    """Check shadow/Fuli readiness without mutating memory or downloading models."""
    import importlib
    import time
    from plugins.memory import load_memory_provider

    home = get_hermes_home()
    cfg = _provider_config()
    primary_name = cfg.get("primary_provider", "honcho")
    secondary_name = cfg.get("secondary_provider", "fuli")

    print("\nShadow memory preflight")
    print("─" * 40)
    print(f"  Hermes home:        {home}")
    print(f"  Primary:            {primary_name}")
    print(f"  Secondary:          {secondary_name}")
    print(f"  Enabled:            {cfg.get('enabled', False)}")
    print(f"  Mirror writes:      {cfg.get('mirror_writes', False)}")
    print(f"  Compare reads:      {cfg.get('compare_reads', False)}")
    print(f"  Sample rate:        {cfg.get('sample_rate', 0.0)}")
    print(f"  Timeout:            {cfg.get('timeout_ms', 250)} ms")
    print(f"  Capture content:    {cfg.get('capture_content', False)}")
    print(f"  Namespace:          {cfg.get('namespace', 'hermes:default')}")

    # Primary availability
    primary = load_memory_provider(primary_name)
    print(f"  Primary available:  {primary is not None and primary.is_available()}")

    # Fuli import path and version/commit
    fuli_module = importlib.import_module("fuli")
    fuli_path = getattr(fuli_module, "__file__", "unknown")
    print(f"  Fuli import path:   {fuli_path}")
    fuli_version = getattr(fuli_module, "__version__", "unknown")
    print(f"  Fuli version:       {fuli_version}")
    try:
        import subprocess
        fuli_commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(Path(fuli_path).parent.parent),
                capture_output=True,
                text=True,
                check=False,
            )
            .stdout
            .strip()
        )
        print(f"  Fuli commit:        {fuli_commit or 'unknown'}")
    except Exception:
        print("  Fuli commit:        unknown")

    # Model cache (sentence-transformers uses the Hugging Face cache)
    model = cfg.get("embedding_model") or "sentence-transformers/all-MiniLM-L6-v2"
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    normalized = "models--" + model.replace("/", "--")
    model_cached = (cache_dir / normalized).exists()
    print(f"  Model cached:       {model_cached} ({model})")

    # Writable directories
    memories_dir = home / "memories"
    shadow_dir = home / "shadow"
    try:
        memories_dir.mkdir(parents=True, exist_ok=True)
        shadow_dir.mkdir(parents=True, exist_ok=True)
        print(f"  DB dir writable:    {memories_dir}")
        print(f"  Shadow config dir:  {shadow_dir}")
    except Exception as e:
        print(f"  DB dir writable:    ERROR {e}")

    # Evidence store counts
    evidence_path = home / "memories" / "shadow.db"
    if evidence_path.exists():
        try:
            store = _load_store()
            report = store.report()
            print(f"  Mirrored writes:    {report['writes']['attempted']}")
            print(f"  Read samples:       {report['reads']['sample_count']}")
        except Exception as e:
            print(f"  Evidence store:     ERROR {e}")
    else:
        print("  Evidence store:     not created yet")
    print()


def cmd_status(args) -> None:
    """Show shadow provider status."""
    cfg = _provider_config()
    print("\nShadow memory provider status")
    print("─" * 40)
    print(f"  Primary:    {cfg.get('primary_provider', 'honcho')}")
    print(f"  Secondary:  {cfg.get('secondary_provider', 'fuli')}")
    print(f"  Enabled:    {cfg.get('enabled', False)}")
    print(f"  Mirror:     {cfg.get('mirror_writes', False)}")
    print(f"  Compare:    {cfg.get('compare_reads', False)}")
    print(f"  Sample:     {cfg.get('sample_rate', 0.0)}")
    print(f"  Timeout:    {cfg.get('timeout_ms', 250)} ms")
    print(f"  Namespace:  {cfg.get('namespace', 'hermes:default')}")
    print()


def cmd_report(args) -> None:
    """Show evidence-store comparison report."""
    if not _report_path().exists():
        print("\n  No shadow evidence store yet.\n")
        return
    store = _load_store()
    report = store.report()
    print("\nShadow comparison report")
    print("─" * 40)
    print(f"  Writes attempted:          {report['writes']['attempted']}")
    print(f"  Writes mirrored:           {report['writes']['shadow_success']}")
    print(f"  Mirror failure rate:       {report['writes']['shadow_failure_rate']:.2%}")
    print(f"  Avg mirror latency:        {report['writes']['average_latency_ms']:.1f} ms")
    print(f"  Read samples:              {report['reads']['sample_count']}")
    print(f"  Avg primary latency:       {report['reads']['average_primary_latency_ms']:.1f} ms")
    print(f"  Avg shadow latency:        {report['reads']['average_shadow_latency_ms']:.1f} ms")
    for k in ("overlap_at_1", "overlap_at_3", "overlap_at_5"):
        v = report["reads"][k]
        print(f"  {k}:                       {v if v is not None else 'n/a'}")
    print(f"  Read error rate:           {report['reads']['error_rate']:.2%}")
    print(f"  Namespaces:                {', '.join(report['namespaces']) or '(none)'}")
    print()


def cmd_compare(args) -> None:
    """Run a one-off comparison query."""
    from plugins.memory import load_memory_provider
    query = getattr(args, "query", None)
    if not query:
        print("\n  Usage: hermes shadow compare --query '...'\n")
        return
    cfg = _provider_config()
    primary_name = cfg.get("primary_provider", "honcho")
    secondary_name = cfg.get("secondary_provider", "fuli")
    try:
        primary = load_memory_provider(primary_name)
        secondary = load_memory_provider(secondary_name)
    except Exception as e:
        print(f"\n  Failed to load providers: {e}\n")
        return
    if primary is None or secondary is None:
        print("\n  One or both providers are unavailable.\n")
        return
    import time
    start = time.monotonic()
    primary_result = primary.handle_tool_call("honcho_search", {"query": query})
    primary_latency = (time.monotonic() - start) * 1000.0
    start = time.monotonic()
    secondary_result = secondary.handle_tool_call("fuli_memory_search", {"query": query, "top_k": 5})
    secondary_latency = (time.monotonic() - start) * 1000.0
    print(f"\nQuery: {query}")
    print(f"  Primary latency:   {primary_latency:.1f} ms")
    print(f"  Shadow latency:    {secondary_latency:.1f} ms")
    print(f"  Primary result:    {primary_result[:200]}...")
    print(f"  Shadow result:     {secondary_result[:200]}...")
    print()


def cmd_namespace(args) -> None:
    """Get or set the shadow namespace."""
    cfg = _provider_config()
    if getattr(args, "set", None):
        # Note: config.yaml editing should be done via `hermes config set`;
        # this is a convenience that writes the local JSON config.
        json_path = get_hermes_home() / "shadow" / "config.json"
        existing = json.loads(json_path.read_text()) if json_path.exists() else {}
        existing["namespace"] = args.set
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(existing, indent=2))
        print(f"\n  Shadow namespace set to: {args.set}\n")
    else:
        print(f"\n  Shadow namespace: {cfg.get('namespace', 'hermes:default')}\n")


def register_cli(subparser) -> None:
    """Build the ``hermes shadow`` argparse tree."""
    subs = subparser.add_subparsers(dest="shadow_command")
    subs.add_parser("preflight", help="Check shadow/Fuli readiness without mutating memory")
    subs.add_parser("status", help="Show shadow provider status")
    subs.add_parser("report", help="Show comparison report")
    compare = subs.add_parser("compare", help="Run one-off comparison query")
    compare.add_argument("--query", required=True, help="Query text to compare")
    namespace = subs.add_parser("namespace", help="Show or set shadow namespace")
    namespace.add_argument("--set", help="Namespace value (e.g. hermes:default)")
    subparser.set_defaults(func=cmd_dispatch)


def cmd_dispatch(args) -> None:
    cmd = getattr(args, "shadow_command", None)
    if cmd == "preflight":
        cmd_preflight(args)
    elif cmd == "status":
        cmd_status(args)
    elif cmd == "report":
        cmd_report(args)
    elif cmd == "compare":
        cmd_compare(args)
    elif cmd == "namespace":
        cmd_namespace(args)
    else:
        print("\n  Usage: hermes shadow {preflight|status|report|compare|namespace}\n")

#!/usr/bin/env python3
"""Unified hook executor for hookify plugin.

All hook scripts (userpromptsubmit, stop, pretooluse, posttooluse) share
the same structure: setup sys.path, import core modules, read stdin JSON,
load rules for an event, evaluate, print JSON, exit 0.

This module extracts that shared logic. Each hook script becomes a thin
wrapper that calls `run_hook()` with its event resolution strategy.

Usage from a hook script:

    from hookify.hooks.hook_runner import run_hook
    run_hook(event='stop')                    # fixed event
    run_hook(event_from_tool_name=True)       # derive event from tool_name
"""

import os
import sys
import json
from typing import Optional


def _setup_plugin_path() -> None:
    """Add plugin root to Python path so hookify imports work."""
    plugin_root = os.environ.get('CLAUDE_PLUGIN_ROOT')
    if plugin_root:
        parent_dir = os.path.dirname(plugin_root)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        if plugin_root not in sys.path:
            sys.path.insert(0, plugin_root)


def _resolve_event_from_tool(input_data: dict) -> Optional[str]:
    """Map tool_name to event type for PreToolUse/PostToolUse hooks."""
    tool_name = input_data.get('tool_name', '')
    if tool_name == 'Bash':
        return 'bash'
    elif tool_name in ['Edit', 'Write', 'MultiEdit']:
        return 'file'
    return None


def run_hook(
    event: Optional[str] = None,
    event_from_tool_name: bool = False,
) -> None:
    """Run the hookify rule evaluation pipeline.

    Args:
        event: Fixed event name ('prompt', 'stop', etc.).
                Mutually exclusive with event_from_tool_name.
        event_from_tool_name: If True, derive event from input_data['tool_name'].
                              Used by PreToolUse and PostToolUse hooks.

    Always exits with code 0 and outputs JSON to stdout.
    """
    # Ensure plugin path is set up before importing core modules
    _setup_plugin_path()

    try:
        from hookify.core.config_loader import load_rules
        from hookify.core.rule_engine import RuleEngine
    except ImportError as e:
        error_msg = {"systemMessage": f"Hookify import error: {e}"}
        print(json.dumps(error_msg), file=sys.stdout)
        sys.exit(0)

    try:
        input_data = json.load(sys.stdin)

        # Resolve event
        resolved_event = event
        if event_from_tool_name:
            resolved_event = _resolve_event_from_tool(input_data)

        rules = load_rules(event=resolved_event)

        engine = RuleEngine()
        result = engine.evaluate_rules(rules, input_data)

        print(json.dumps(result), file=sys.stdout)

    except Exception as e:
        error_output = {
            "systemMessage": f"Hookify error: {str(e)}"
        }
        print(json.dumps(error_output), file=sys.stdout)

    finally:
        sys.exit(0)

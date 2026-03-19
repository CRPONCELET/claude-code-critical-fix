#!/usr/bin/env python3
"""UserPromptSubmit hook executor for hookify plugin.

This script is called by Claude Code when user submits a prompt.
It reads .claude/hookify.*.local.md files and evaluates rules.
"""

import os
import sys

# CRITICAL: Add plugin root to Python path for imports
PLUGIN_ROOT = os.environ.get('CLAUDE_PLUGIN_ROOT')
if PLUGIN_ROOT:
    parent_dir = os.path.dirname(PLUGIN_ROOT)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    if PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, PLUGIN_ROOT)

from hookify.hooks.hook_runner import run_hook

if __name__ == '__main__':
    run_hook(event='prompt')

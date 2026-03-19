#!/usr/bin/env python3
"""Tests unitaires pour le parseur YAML artisanal et le chargement de regles.

Couvre:
- Extraction de frontmatter YAML (cas simples, listes, dicts imbriques)
- Construction de Rule depuis frontmatter
- Cas limites: fichier vide, pas de frontmatter, frontmatter malformed
- Conversion legacy pattern -> conditions
"""

import os
import sys
import tempfile
import unittest

# Ajuster le path pour importer hookify comme package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from hookify.core.config_loader import (
    Condition,
    Rule,
    extract_frontmatter,
    load_rule_file,
    load_rules,
)


class TestExtractFrontmatter(unittest.TestCase):
    """Tests pour extract_frontmatter() — le parseur YAML artisanal."""

    def test_simple_key_value(self):
        """Paires cle-valeur simples."""
        content = """---
name: test-rule
enabled: true
event: bash
action: warn
---

Message body here.
"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(fm["name"], "test-rule")
        self.assertTrue(fm["enabled"])
        self.assertEqual(fm["event"], "bash")
        self.assertEqual(fm["action"], "warn")
        self.assertEqual(msg, "Message body here.")

    def test_boolean_parsing(self):
        """Les valeurs true/false doivent etre converties en bool Python."""
        content = """---
enabled: true
disabled: false
---

body
"""
        fm, msg = extract_frontmatter(content)
        self.assertIs(fm["enabled"], True)
        self.assertIs(fm["disabled"], False)

    def test_quoted_values(self):
        """Les guillemets simples et doubles doivent etre retires."""
        content = """---
name: "quoted-name"
pattern: 'single-quoted'
---

body
"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(fm["name"], "quoted-name")
        self.assertEqual(fm["pattern"], "single-quoted")

    def test_no_frontmatter(self):
        """Un fichier sans frontmatter retourne un dict vide."""
        content = "Pas de frontmatter ici."
        fm, msg = extract_frontmatter(content)
        self.assertEqual(fm, {})
        self.assertEqual(msg, content)

    def test_incomplete_frontmatter(self):
        """Un seul delimiteur --- sans fermeture."""
        content = """---
name: broken
"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(fm, {})

    def test_empty_content(self):
        """Contenu vide."""
        fm, msg = extract_frontmatter("")
        self.assertEqual(fm, {})
        self.assertEqual(msg, "")

    def test_simple_list(self):
        """Liste avec elements simples (strings)."""
        content = """---
tags:
  - alpha
  - beta
  - gamma
---

body
"""
        fm, msg = extract_frontmatter(content)
        self.assertIn("tags", fm)
        self.assertEqual(fm["tags"], ["alpha", "beta", "gamma"])

    def test_list_of_dicts_multiline(self):
        """Liste de dicts multi-lignes (style conditions hookify)."""
        content = """---
name: test
conditions:
  - field: command
    operator: regex_match
    pattern: rm\\s+-rf
  - field: file_path
    operator: contains
    pattern: .env
---

Warning message.
"""
        fm, msg = extract_frontmatter(content)
        self.assertIn("conditions", fm)
        self.assertEqual(len(fm["conditions"]), 2)
        self.assertEqual(fm["conditions"][0]["field"], "command")
        self.assertEqual(fm["conditions"][0]["operator"], "regex_match")
        self.assertEqual(fm["conditions"][1]["field"], "file_path")
        self.assertEqual(fm["conditions"][1]["operator"], "contains")

    def test_list_of_dicts_inline(self):
        """Liste de dicts inline (comma-separated)."""
        content = """---
name: inline-test
conditions:
  - field: command, operator: regex_match, pattern: dangerous
---

body
"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(len(fm["conditions"]), 1)
        self.assertEqual(fm["conditions"][0]["field"], "command")
        self.assertEqual(fm["conditions"][0]["operator"], "regex_match")

    def test_comment_lines_ignored(self):
        """Les lignes commentees dans le frontmatter sont ignorees."""
        content = """---
name: test
# This is a comment
enabled: true
---

body
"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(fm["name"], "test")
        self.assertTrue(fm["enabled"])
        self.assertNotIn("#", fm)

    def test_message_body_stripped(self):
        """Le corps du message est nettoye des espaces en debut/fin."""
        content = """---
name: test
---

   Trimmed message body

"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(msg, "Trimmed message body")

    def test_pattern_with_regex_special_chars(self):
        """Un pattern contenant des caracteres regex speciaux."""
        content = r"""---
name: regex-rule
pattern: rm\s+-rf\s+/
---

body
"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(fm["pattern"], r"rm\s+-rf\s+/")

    def test_multiple_top_level_keys_with_list_at_end(self):
        """Plusieurs cles top-level dont une liste en derniere position."""
        content = """---
name: multi
enabled: true
event: file
conditions:
  - field: content
    operator: contains
    pattern: TODO
---

body
"""
        fm, msg = extract_frontmatter(content)
        self.assertEqual(fm["name"], "multi")
        self.assertTrue(fm["enabled"])
        self.assertEqual(fm["event"], "file")
        self.assertEqual(len(fm["conditions"]), 1)


class TestCondition(unittest.TestCase):
    """Tests pour Condition.from_dict()."""

    def test_from_dict_complete(self):
        """Construction avec tous les champs."""
        data = {"field": "command", "operator": "contains", "pattern": "rm"}
        cond = Condition.from_dict(data)
        self.assertEqual(cond.field, "command")
        self.assertEqual(cond.operator, "contains")
        self.assertEqual(cond.pattern, "rm")

    def test_from_dict_defaults(self):
        """Valeurs par defaut quand les champs sont absents."""
        cond = Condition.from_dict({})
        self.assertEqual(cond.field, "")
        self.assertEqual(cond.operator, "regex_match")
        self.assertEqual(cond.pattern, "")

    def test_from_dict_partial(self):
        """Construction avec seulement certains champs."""
        cond = Condition.from_dict({"field": "file_path"})
        self.assertEqual(cond.field, "file_path")
        self.assertEqual(cond.operator, "regex_match")


class TestRuleFromDict(unittest.TestCase):
    """Tests pour Rule.from_dict()."""

    def test_legacy_pattern_bash(self):
        """Un pattern legacy avec event=bash cree une condition sur 'command'."""
        fm = {"name": "rm-check", "enabled": True, "event": "bash", "pattern": r"rm\s+-rf"}
        rule = Rule.from_dict(fm, "Danger!")
        self.assertEqual(rule.name, "rm-check")
        self.assertTrue(rule.enabled)
        self.assertEqual(rule.event, "bash")
        self.assertEqual(len(rule.conditions), 1)
        self.assertEqual(rule.conditions[0].field, "command")
        self.assertEqual(rule.conditions[0].operator, "regex_match")
        self.assertEqual(rule.conditions[0].pattern, r"rm\s+-rf")
        self.assertEqual(rule.message, "Danger!")

    def test_legacy_pattern_file(self):
        """Un pattern legacy avec event=file cree une condition sur 'new_text'."""
        fm = {"name": "log-check", "enabled": True, "event": "file", "pattern": r"console\.log"}
        rule = Rule.from_dict(fm, "Warning")
        self.assertEqual(rule.conditions[0].field, "new_text")

    def test_legacy_pattern_other_event(self):
        """Un pattern legacy avec event=all cree une condition sur 'content'."""
        fm = {"name": "generic", "enabled": True, "event": "all", "pattern": "test"}
        rule = Rule.from_dict(fm, "msg")
        self.assertEqual(rule.conditions[0].field, "content")

    def test_explicit_conditions_override_pattern(self):
        """Les conditions explicites sont preferees au pattern legacy."""
        fm = {
            "name": "explicit",
            "enabled": True,
            "event": "bash",
            "pattern": "ignored",
            "conditions": [
                {"field": "command", "operator": "contains", "pattern": "actual"}
            ],
        }
        rule = Rule.from_dict(fm, "msg")
        self.assertEqual(len(rule.conditions), 1)
        self.assertEqual(rule.conditions[0].pattern, "actual")

    def test_defaults(self):
        """Valeurs par defaut pour les champs absents."""
        rule = Rule.from_dict({}, "body")
        self.assertEqual(rule.name, "unnamed")
        self.assertTrue(rule.enabled)
        self.assertEqual(rule.event, "all")
        self.assertEqual(rule.action, "warn")
        self.assertIsNone(rule.pattern)
        self.assertIsNone(rule.tool_matcher)

    def test_action_block(self):
        """L'action 'block' est preservee."""
        fm = {"name": "blocker", "action": "block"}
        rule = Rule.from_dict(fm, "blocked")
        self.assertEqual(rule.action, "block")

    def test_disabled_rule(self):
        """Une regle desactivee."""
        fm = {"name": "off", "enabled": False}
        rule = Rule.from_dict(fm, "")
        self.assertFalse(rule.enabled)


class TestLoadRuleFile(unittest.TestCase):
    """Tests pour load_rule_file()."""

    def _write_temp_rule(self, content: str) -> str:
        """Ecrit un fichier temporaire et retourne son chemin."""
        fd, path = tempfile.mkstemp(suffix=".local.md")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def test_valid_rule_file(self):
        """Chargement d'un fichier regle valide."""
        path = self._write_temp_rule("""---
name: test-rm
enabled: true
event: bash
pattern: rm -rf
action: block
---

Dangerous command!
""")
        try:
            rule = load_rule_file(path)
            self.assertIsNotNone(rule)
            self.assertEqual(rule.name, "test-rm")
            self.assertEqual(rule.action, "block")
            self.assertIn("Dangerous command!", rule.message)
        finally:
            os.unlink(path)

    def test_file_without_frontmatter(self):
        """Un fichier sans frontmatter retourne None."""
        path = self._write_temp_rule("Pas de frontmatter")
        try:
            rule = load_rule_file(path)
            self.assertIsNone(rule)
        finally:
            os.unlink(path)

    def test_nonexistent_file(self):
        """Un fichier inexistant retourne None sans lever d'exception."""
        rule = load_rule_file("/nonexistent/path/to/file.md")
        self.assertIsNone(rule)

    def test_complex_conditions_file(self):
        """Fichier avec conditions multi-lignes."""
        path = self._write_temp_rule("""---
name: sensitive-files
enabled: true
event: file
action: warn
conditions:
  - field: file_path
    operator: regex_match
    pattern: \\.env$|\\.env\\.|credentials
---

Sensitive file detected.
""")
        try:
            rule = load_rule_file(path)
            self.assertIsNotNone(rule)
            self.assertEqual(len(rule.conditions), 1)
            self.assertEqual(rule.conditions[0].field, "file_path")
            self.assertEqual(rule.conditions[0].operator, "regex_match")
        finally:
            os.unlink(path)


class TestLoadRules(unittest.TestCase):
    """Tests pour load_rules() avec filtrage par event."""

    def setUp(self):
        """Cree un repertoire .claude temporaire avec des fichiers de regles."""
        self._orig_dir = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        os.chdir(self._tmpdir)
        os.makedirs(".claude", exist_ok=True)

        # Regle bash
        with open(".claude/hookify.bash-rule.local.md", "w") as f:
            f.write("""---
name: bash-rule
enabled: true
event: bash
pattern: rm -rf
action: block
---

Block rm.
""")

        # Regle file
        with open(".claude/hookify.file-rule.local.md", "w") as f:
            f.write("""---
name: file-rule
enabled: true
event: file
pattern: console\\.log
action: warn
---

Console.log warning.
""")

        # Regle desactivee
        with open(".claude/hookify.disabled-rule.local.md", "w") as f:
            f.write("""---
name: disabled-rule
enabled: false
event: bash
pattern: sudo
---

Disabled.
""")

    def tearDown(self):
        """Nettoie le repertoire temporaire."""
        os.chdir(self._orig_dir)
        import shutil
        shutil.rmtree(self._tmpdir)

    def test_load_all_enabled(self):
        """Sans filtre d'event, toutes les regles activees sont chargees."""
        rules = load_rules()
        self.assertEqual(len(rules), 2)
        names = {r.name for r in rules}
        self.assertIn("bash-rule", names)
        self.assertIn("file-rule", names)
        self.assertNotIn("disabled-rule", names)

    def test_filter_by_event_bash(self):
        """Filtre event=bash retourne seulement les regles bash + all."""
        rules = load_rules(event="bash")
        names = {r.name for r in rules}
        self.assertIn("bash-rule", names)
        self.assertNotIn("file-rule", names)

    def test_filter_by_event_file(self):
        """Filtre event=file retourne seulement les regles file."""
        rules = load_rules(event="file")
        names = {r.name for r in rules}
        self.assertIn("file-rule", names)
        self.assertNotIn("bash-rule", names)

    def test_filter_nonexistent_event(self):
        """Un event inconnu ne retourne aucune regle."""
        rules = load_rules(event="nonexistent")
        self.assertEqual(len(rules), 0)

    def test_empty_directory(self):
        """Un repertoire sans fichiers hookify retourne une liste vide."""
        # Supprimer les fichiers de regles
        import glob as g
        for f in g.glob(".claude/hookify.*.local.md"):
            os.unlink(f)
        rules = load_rules()
        self.assertEqual(len(rules), 0)


if __name__ == "__main__":
    unittest.main()

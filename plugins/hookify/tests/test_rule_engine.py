#!/usr/bin/env python3
"""Tests unitaires pour le moteur de regles hookify (RuleEngine).

Couvre:
- Matching de conditions: regex_match, contains, equals, not_contains, starts_with, ends_with
- Extraction de champs: command, file_path, new_text, content, reason, user_prompt
- Tool matcher: wildcard, pipe-separated, exact
- Evaluation de regles multiples: warn vs block, priorite
- Cas limites: regex invalide, champs manquants, conditions vides
- Evenements Stop et UserPromptSubmit
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from hookify.core.config_loader import Condition, Rule
from hookify.core.rule_engine import RuleEngine, compile_regex


class TestCompileRegex(unittest.TestCase):
    """Tests pour le cache de compilation regex."""

    def test_valid_regex(self):
        """Une regex valide est compilee correctement."""
        # Vider le cache pour ce test
        compile_regex.cache_clear()
        pattern = compile_regex(r"rm\s+-rf")
        self.assertIsNotNone(pattern)
        self.assertTrue(pattern.search("rm -rf /tmp"))

    def test_case_insensitive(self):
        """Les regex sont compilees en case-insensitive."""
        pattern = compile_regex(r"DELETE")
        self.assertTrue(pattern.search("delete from table"))

    def test_cache_returns_same_object(self):
        """Le cache LRU retourne le meme objet pour le meme pattern."""
        compile_regex.cache_clear()
        p1 = compile_regex("test")
        p2 = compile_regex("test")
        self.assertIs(p1, p2)


class TestRuleEngineConditionOperators(unittest.TestCase):
    """Tests pour chaque operateur de condition."""

    def setUp(self):
        self.engine = RuleEngine()

    def _make_rule(self, field: str, operator: str, pattern: str,
                   action: str = "warn", tool_matcher: str = None) -> Rule:
        return Rule(
            name="test",
            enabled=True,
            event="bash",
            conditions=[Condition(field=field, operator=operator, pattern=pattern)],
            action=action,
            tool_matcher=tool_matcher,
            message="Test message",
        )

    def _bash_input(self, command: str) -> dict:
        return {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "hook_event_name": "PreToolUse",
        }

    def test_regex_match_positive(self):
        """regex_match detecte un pattern present."""
        rule = self._make_rule("command", "regex_match", r"rm\s+-rf")
        result = self.engine.evaluate_rules([rule], self._bash_input("rm -rf /tmp"))
        self.assertIn("systemMessage", result)

    def test_regex_match_negative(self):
        """regex_match ne match pas quand le pattern est absent."""
        rule = self._make_rule("command", "regex_match", r"rm\s+-rf")
        result = self.engine.evaluate_rules([rule], self._bash_input("ls -la"))
        self.assertEqual(result, {})

    def test_contains_positive(self):
        """contains detecte une sous-chaine."""
        rule = self._make_rule("command", "contains", "sudo")
        result = self.engine.evaluate_rules([rule], self._bash_input("sudo apt install"))
        self.assertIn("systemMessage", result)

    def test_contains_negative(self):
        """contains ne match pas quand la sous-chaine est absente."""
        rule = self._make_rule("command", "contains", "sudo")
        result = self.engine.evaluate_rules([rule], self._bash_input("apt install"))
        self.assertEqual(result, {})

    def test_equals(self):
        """equals matche seulement l'egalite exacte."""
        rule = self._make_rule("command", "equals", "exit")
        self.assertIn("systemMessage",
                       self.engine.evaluate_rules([rule], self._bash_input("exit")))
        self.assertEqual({},
                         self.engine.evaluate_rules([rule], self._bash_input("exit 0")))

    def test_not_contains(self):
        """not_contains matche quand la sous-chaine est absente."""
        rule = self._make_rule("command", "not_contains", "test")
        self.assertIn("systemMessage",
                       self.engine.evaluate_rules([rule], self._bash_input("ls -la")))
        self.assertEqual({},
                         self.engine.evaluate_rules([rule], self._bash_input("npm test")))

    def test_starts_with(self):
        """starts_with matche le debut de la valeur."""
        rule = self._make_rule("command", "starts_with", "curl")
        self.assertIn("systemMessage",
                       self.engine.evaluate_rules([rule], self._bash_input("curl https://example.com")))
        self.assertEqual({},
                         self.engine.evaluate_rules([rule], self._bash_input("wget https://example.com")))

    def test_ends_with(self):
        """ends_with matche la fin de la valeur."""
        rule = self._make_rule("command", "ends_with", "--force")
        self.assertIn("systemMessage",
                       self.engine.evaluate_rules([rule], self._bash_input("git push --force")))
        self.assertEqual({},
                         self.engine.evaluate_rules([rule], self._bash_input("git push")))

    def test_unknown_operator_no_match(self):
        """Un operateur inconnu ne matche jamais."""
        rule = self._make_rule("command", "unknown_op", "test")
        result = self.engine.evaluate_rules([rule], self._bash_input("test"))
        self.assertEqual(result, {})

    def test_invalid_regex_no_crash(self):
        """Une regex invalide ne crash pas, retourne simplement False."""
        rule = self._make_rule("command", "regex_match", "[invalid(")
        result = self.engine.evaluate_rules([rule], self._bash_input("anything"))
        self.assertEqual(result, {})


class TestRuleEngineFieldExtraction(unittest.TestCase):
    """Tests pour l'extraction de champs depuis differents types d'input."""

    def setUp(self):
        self.engine = RuleEngine()

    def _make_rule(self, field: str, operator: str = "contains", pattern: str = "target") -> Rule:
        return Rule(
            name="test",
            enabled=True,
            event="all",
            conditions=[Condition(field=field, operator=operator, pattern=pattern)],
            message="Found",
        )

    def test_extract_bash_command(self):
        """Extraction du champ 'command' pour un outil Bash."""
        rule = self._make_rule("command", "contains", "dangerous")
        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "dangerous command here"},
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertIn("systemMessage", result)

    def test_extract_edit_file_path(self):
        """Extraction du champ 'file_path' pour un outil Edit."""
        rule = self._make_rule("file_path", "contains", ".env")
        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/project/.env.local", "new_string": "KEY=val"},
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertIn("systemMessage", result)

    def test_extract_edit_new_text(self):
        """Extraction de 'new_text' alias 'new_string' pour Edit."""
        rule = self._make_rule("new_text", "contains", "console.log")
        input_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "app.js", "new_string": "console.log('debug')"},
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertIn("systemMessage", result)

    def test_extract_write_content(self):
        """Extraction de 'content' pour un outil Write."""
        rule = self._make_rule("content", "contains", "password")
        input_data = {
            "tool_name": "Write",
            "tool_input": {"file_path": "config.py", "content": "password = '123'"},
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertIn("systemMessage", result)

    def test_extract_multiedit_content(self):
        """Extraction concatenee de 'content' pour MultiEdit."""
        rule = self._make_rule("content", "contains", "TODO")
        input_data = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "app.py",
                "edits": [
                    {"new_string": "# TODO: fix this"},
                    {"new_string": "# Another edit"},
                ],
            },
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertIn("systemMessage", result)

    def test_extract_reason_stop_event(self):
        """Extraction du champ 'reason' pour un evenement Stop."""
        rule = self._make_rule("reason", "contains", "done")
        input_data = {
            "tool_name": "",
            "tool_input": {},
            "reason": "I'm done with the task",
            "hook_event_name": "Stop",
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertIn("systemMessage", result)

    def test_extract_user_prompt(self):
        """Extraction du champ 'user_prompt'."""
        rule = self._make_rule("user_prompt", "contains", "deploy")
        input_data = {
            "tool_name": "",
            "tool_input": {},
            "user_prompt": "Please deploy to production",
            "hook_event_name": "UserPromptSubmit",
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertIn("systemMessage", result)

    def test_extract_transcript_from_file(self):
        """Extraction du champ 'transcript' depuis un fichier."""
        # Creer un fichier transcript temporaire
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write("User ran npm test and it passed.")

        try:
            rule = self._make_rule("transcript", "contains", "npm test")
            input_data = {
                "tool_name": "",
                "tool_input": {},
                "transcript_path": path,
                "hook_event_name": "Stop",
            }
            result = self.engine.evaluate_rules([rule], input_data)
            self.assertIn("systemMessage", result)
        finally:
            os.unlink(path)

    def test_transcript_file_not_found(self):
        """Un fichier transcript inexistant ne crash pas."""
        rule = self._make_rule("transcript", "contains", "test")
        input_data = {
            "tool_name": "",
            "tool_input": {},
            "transcript_path": "/nonexistent/file.txt",
            "hook_event_name": "Stop",
        }
        # Ne doit pas lever d'exception
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertEqual(result, {})

    def test_field_not_found_returns_no_match(self):
        """Un champ inexistant ne matche pas."""
        rule = self._make_rule("nonexistent_field", "contains", "anything")
        input_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }
        result = self.engine.evaluate_rules([rule], input_data)
        self.assertEqual(result, {})


class TestRuleEngineToolMatcher(unittest.TestCase):
    """Tests pour le matching de tool_name."""

    def setUp(self):
        self.engine = RuleEngine()

    def _make_rule_with_matcher(self, matcher: str) -> Rule:
        return Rule(
            name="test",
            enabled=True,
            event="bash",
            conditions=[Condition(field="command", operator="contains", pattern="test")],
            tool_matcher=matcher,
            message="Matched",
        )

    def test_wildcard_matches_any_tool(self):
        """Le matcher '*' matche n'importe quel outil."""
        rule = self._make_rule_with_matcher("*")
        input_data = {"tool_name": "Bash", "tool_input": {"command": "test"}}
        self.assertIn("systemMessage", self.engine.evaluate_rules([rule], input_data))

    def test_exact_tool_match(self):
        """Match exact sur le nom de l'outil."""
        rule = self._make_rule_with_matcher("Bash")
        input_data = {"tool_name": "Bash", "tool_input": {"command": "test"}}
        self.assertIn("systemMessage", self.engine.evaluate_rules([rule], input_data))

    def test_exact_tool_no_match(self):
        """Pas de match si l'outil ne correspond pas."""
        rule = self._make_rule_with_matcher("Edit")
        input_data = {"tool_name": "Bash", "tool_input": {"command": "test"}}
        self.assertEqual({}, self.engine.evaluate_rules([rule], input_data))

    def test_pipe_separated_matcher(self):
        """Match sur un des outils separes par |."""
        rule = self._make_rule_with_matcher("Edit|Write")
        input_edit = {"tool_name": "Edit", "tool_input": {"command": "test"}}
        input_write = {"tool_name": "Write", "tool_input": {"command": "test"}}
        input_bash = {"tool_name": "Bash", "tool_input": {"command": "test"}}
        self.assertIn("systemMessage", self.engine.evaluate_rules([rule], input_edit))
        self.assertIn("systemMessage", self.engine.evaluate_rules([rule], input_write))
        self.assertEqual({}, self.engine.evaluate_rules([rule], input_bash))


class TestRuleEngineMultipleRules(unittest.TestCase):
    """Tests pour l'evaluation de regles multiples et priorite block > warn."""

    def setUp(self):
        self.engine = RuleEngine()

    def test_no_rules_no_match(self):
        """Pas de regles -> pas de resultat."""
        result = self.engine.evaluate_rules(
            [], {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        )
        self.assertEqual(result, {})

    def test_single_warning(self):
        """Une seule regle warn qui matche."""
        rule = Rule(
            name="warn-rule",
            enabled=True,
            event="bash",
            conditions=[Condition(field="command", operator="contains", pattern="sudo")],
            action="warn",
            message="Be careful with sudo",
        )
        result = self.engine.evaluate_rules(
            [rule], {"tool_name": "Bash", "tool_input": {"command": "sudo apt"}}
        )
        self.assertIn("systemMessage", result)
        self.assertIn("warn-rule", result["systemMessage"])
        self.assertNotIn("hookSpecificOutput", result)

    def test_block_pretooluse(self):
        """Une regle block sur PreToolUse retourne permissionDecision=deny."""
        rule = Rule(
            name="block-rule",
            enabled=True,
            event="bash",
            conditions=[Condition(field="command", operator="contains", pattern="rm -rf")],
            action="block",
            message="Blocked!",
        )
        result = self.engine.evaluate_rules(
            [rule],
            {
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
                "hook_event_name": "PreToolUse",
            },
        )
        self.assertIn("hookSpecificOutput", result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_block_stop_event(self):
        """Une regle block sur Stop retourne decision=block."""
        rule = Rule(
            name="stop-block",
            enabled=True,
            event="stop",
            conditions=[Condition(field="reason", operator="contains", pattern="no tests")],
            action="block",
            message="Run tests first!",
        )
        result = self.engine.evaluate_rules(
            [rule],
            {
                "tool_name": "",
                "tool_input": {},
                "reason": "no tests were run",
                "hook_event_name": "Stop",
            },
        )
        self.assertIn("decision", result)
        self.assertEqual(result["decision"], "block")

    def test_block_other_event(self):
        """Une regle block sur un event generique retourne juste systemMessage."""
        rule = Rule(
            name="generic-block",
            enabled=True,
            event="all",
            conditions=[Condition(field="command", operator="contains", pattern="danger")],
            action="block",
            message="Blocked",
        )
        result = self.engine.evaluate_rules(
            [rule],
            {
                "tool_name": "Bash",
                "tool_input": {"command": "danger"},
                "hook_event_name": "SomeOtherEvent",
            },
        )
        self.assertIn("systemMessage", result)
        self.assertNotIn("hookSpecificOutput", result)
        self.assertNotIn("decision", result)

    def test_multiple_warnings_combined(self):
        """Plusieurs regles warn combinent leurs messages."""
        rules = [
            Rule(
                name="warn-1",
                enabled=True,
                event="bash",
                conditions=[Condition(field="command", operator="contains", pattern="sudo")],
                action="warn",
                message="Warning 1",
            ),
            Rule(
                name="warn-2",
                enabled=True,
                event="bash",
                conditions=[Condition(field="command", operator="contains", pattern="apt")],
                action="warn",
                message="Warning 2",
            ),
        ]
        result = self.engine.evaluate_rules(
            rules, {"tool_name": "Bash", "tool_input": {"command": "sudo apt install"}}
        )
        self.assertIn("warn-1", result["systemMessage"])
        self.assertIn("warn-2", result["systemMessage"])

    def test_block_takes_priority_over_warn(self):
        """Les regles block prennent priorite sur les warn."""
        rules = [
            Rule(
                name="warn-rule",
                enabled=True,
                event="bash",
                conditions=[Condition(field="command", operator="contains", pattern="rm")],
                action="warn",
                message="Warning",
            ),
            Rule(
                name="block-rule",
                enabled=True,
                event="bash",
                conditions=[Condition(field="command", operator="contains", pattern="rm")],
                action="block",
                message="Blocked!",
            ),
        ]
        result = self.engine.evaluate_rules(
            rules,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf"},
                "hook_event_name": "PreToolUse",
            },
        )
        # Block prend priorite: on doit avoir permissionDecision=deny
        self.assertIn("hookSpecificOutput", result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        # Seul le message block est present
        self.assertIn("block-rule", result["systemMessage"])

    def test_rule_without_conditions_no_match(self):
        """Une regle sans conditions ne matche jamais."""
        rule = Rule(
            name="empty",
            enabled=True,
            event="bash",
            conditions=[],
            message="Should not match",
        )
        result = self.engine.evaluate_rules(
            [rule], {"tool_name": "Bash", "tool_input": {"command": "anything"}}
        )
        self.assertEqual(result, {})

    def test_multiple_conditions_all_must_match(self):
        """Toutes les conditions doivent matcher (logique AND)."""
        rule = Rule(
            name="multi-cond",
            enabled=True,
            event="file",
            conditions=[
                Condition(field="file_path", operator="contains", pattern=".env"),
                Condition(field="new_text", operator="contains", pattern="SECRET"),
            ],
            message="Sensitive!",
        )
        # Les deux conditions matchent
        input_match = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/project/.env",
                "new_string": "SECRET_KEY=abc123",
            },
        }
        self.assertIn("systemMessage", self.engine.evaluate_rules([rule], input_match))

        # Seulement file_path matche
        input_partial = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/project/.env",
                "new_string": "DEBUG=true",
            },
        }
        self.assertEqual({}, self.engine.evaluate_rules([rule], input_partial))


if __name__ == "__main__":
    unittest.main()

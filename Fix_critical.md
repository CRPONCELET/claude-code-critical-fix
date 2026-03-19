# Fix Critical
_Name : CRPONCELET_
_Date : 2026-03-19_
_Depot : anthropics/claude-code_
_Scope : workflows GitHub Actions, scripts TypeScript, plugin hookify, configuration_

---

## Vue d'ensemble

13 problemes repartis en 3 niveaux de severite :
- **4 haute priorite** -- securite, duplication massive, absence de tests
- **5 moyenne priorite** -- maintenabilite, fragilite, inconsistances
- **4 basse priorite** -- hygiene, documentation, CI

---

## Haute priorite

### H1 -- githubRequest() duplique dans 3 scripts TS

**Description**
La fonction `githubRequest()` est copiee-collee dans 3 fichiers avec des variations mineures :
- `scripts/auto-close-duplicates.ts` (lignes 28-47) -- signature `(endpoint, token, method, body)`, User-Agent `"auto-close-duplicates-script"`
- `scripts/backfill-duplicate-comments.ts` (lignes 26-45) -- signature identique, User-Agent `"backfill-duplicate-comments-script"`
- `scripts/sweep.ts` (lignes 15-41) -- signature differente `(endpoint, method, body)` (token lu depuis env), User-Agent `"sweep"`, gestion du 404 differente (retourne `{} as T` au lieu de throw)

Environ 150 lignes de code duplique au total. Les interfaces `GitHubIssue` et `GitHubComment` sont egalement dupliquees entre `auto-close-duplicates.ts` et `backfill-duplicate-comments.ts`.

**Impact**
- Corrections de bugs a appliquer 3 fois
- Risque de divergence comportementale (deja le cas : `sweep.ts` gere le 404 differemment)
- Maintenance couteuse et sujette aux erreurs

**Solution implementee**
Extraction dans un module partage `scripts/lib/github.ts` :
- Fonction `githubRequest<T>()` unique avec gestion du token centralisee
- Interfaces `GitHubIssue`, `GitHubComment`, `GitHubReaction` partagees
- Gestion d'erreur unifiee avec option `ignore404` pour le cas `sweep.ts`
- Les 3 scripts importent depuis `./lib/github`

**Fichiers modifies**
- `scripts/lib/github.ts` -- nouveau module partage
- `scripts/auto-close-duplicates.ts` -- suppression de githubRequest + interfaces, import du module
- `scripts/backfill-duplicate-comments.ts` -- idem
- `scripts/sweep.ts` -- idem

---

### H2 -- Aucune gestion du rate-limiting API GitHub

**Description**
Aucun des 3 scripts TS ne gere les headers de rate-limiting de l'API GitHub (`X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After`). Les scripts effectuent des requetes en boucle (pagination de 100+ issues avec commentaires pour chacune), ce qui peut facilement atteindre la limite de 5000 requetes/heure pour les tokens classiques.

Cas concret : `backfill-duplicate-comments.ts` peut parcourir 200 pages de 100 issues, puis faire une requete par issue pour ses commentaires. Soit potentiellement 20000+ requetes en un seul run.

**Impact**
- Echec silencieux du script quand la limite est atteinte (HTTP 403)
- Le script `auto-close-duplicates` pourrait fermer certaines issues et pas d'autres (execution partielle)
- Aucun backoff, aucun retry -- le script crash et laisse un etat inconsistant

**Solution implementee**
Ajout dans `scripts/lib/github.ts` :
- Lecture de `X-RateLimit-Remaining` apres chaque reponse
- Quand remaining < 100 : log d'avertissement
- Quand remaining < 10 : pause automatique jusqu'a `X-RateLimit-Reset`
- Retry automatique avec backoff exponentiel sur HTTP 429 (Retry-After) et HTTP 403 (rate limit)
- Limite configurable du nombre total de requetes par execution (safety cap)

**Fichiers modifies**
- `scripts/lib/github.ts` -- logique de rate-limiting dans githubRequest

---

### H3 -- Vulnerabilite JSON injection dans log-issue-events.yml

**Description**
Le workflow `.github/workflows/log-issue-events.yml` construit un payload JSON en concatenant des variables shell dans une string JSON brute via des substitutions shell :

```yaml
-d '{
  "events": [{
    "metadata": {
      "title": "'"$(echo "$ISSUE_TITLE" | sed "s/\"/\\\\\"/g")"'",
```

Bien que les valeurs soient passees via `env:` (pas de template injection `${{ }}` directe dans le `run:`), la construction du JSON par concatenation shell reste fragile :
- Le `sed` n'echappe que les guillemets doubles, pas les backslashes, newlines, tabs, ou autres caracteres de controle JSON
- Un titre d'issue contenant `\n`, `\t`, ou des backslashes casserait le JSON ou injecterait des champs
- La variable `$AUTHOR` (login GitHub) n'a aucun echappement

**Impact**
- Un attaquant peut creer une issue avec un titre craft pour injecter des champs dans le payload Statsig
- Cassure silencieuse du logging si le JSON est malformed (curl ne verifie pas le retour)
- Pas de vulnerabilite d'execution de code, mais corruption de donnees de telemetrie

**Solution implementee**
Remplacement de la construction JSON manuelle par `jq` :
```yaml
run: |
  jq -n \
    --arg num "$ISSUE_NUMBER" \
    --arg repo "$REPO" \
    --arg title "$ISSUE_TITLE" \
    --arg author "$AUTHOR" \
    --arg created "$CREATED_AT" \
    --arg time "$(date +%s)000" \
    '{events: [{eventName: "github_issue_created", metadata: {issue_number: $num, repository: $repo, title: $title, author: $author, created_at: $created}, time: ($time | tonumber)}]}' \
  | curl -X POST "https://events.statsigapi.net/v1/log_event" \
    -H "Content-Type: application/json" \
    -H "statsig-api-key: $STATSIG_API_KEY" \
    -d @-
```

`jq` echappe automatiquement tous les caracteres speciaux JSON.

**Fichiers modifies**
- `.github/workflows/log-issue-events.yml` -- remplacement de la construction JSON

---

### H4 -- Zero test pour le parseur YAML artisanal et le moteur de regles hookify

**Description**
Le plugin hookify contient un parseur YAML artisanal de 108 lignes (`core/config_loader.py`, fonction `extract_frontmatter`) et un moteur de regles (`core/rule_engine.py`, classe `RuleEngine`). Aucun test automatise n'existe. Les seuls "tests" sont des blocs `if __name__ == '__main__'` avec un cas trivial chacun.

Le parseur YAML artisanal est particulierement critique : il gere manuellement l'indentation, les listes, les dictionnaires imbriques, et les valeurs booleennes. De multiples cas limites ne sont pas couverts :
- Valeurs avec `:` (ex: `pattern: https://example.com:8080`)
- Valeurs multiligne
- Listes de scalaires melanges avec des dictionnaires
- Commentaires inline apres des valeurs
- Indentation par tabs vs espaces

**Impact**
- Regressions silencieuses lors de toute modification du parseur ou du moteur
- Regles hookify qui ne matchent pas sans explication visible pour l'utilisateur
- Faux sentiment de securite : un hook de blocage defaillant laisse passer des operations dangereuses

**Solution implementee**
Creation d'une suite de tests `plugins/hookify/tests/` :
- `test_config_loader.py` -- tests du parseur YAML : frontmatter simple, conditions multiples, valeurs booleennes, valeurs avec caracteres speciaux, fichiers sans frontmatter, fichiers invalides
- `test_rule_engine.py` -- tests du moteur : matching basique, conditions multiples (AND), operateurs (regex, contains, equals, not_contains, starts_with, ends_with), action warn vs block, extraction de champs par type d'outil, regex invalide
- `test_hooks_integration.py` -- tests d'integration : les 4 hooks (pretooluse, posttooluse, stop, userpromptsubmit) avec des payloads simules via stdin

**Fichiers modifies**
- `plugins/hookify/tests/__init__.py` -- nouveau
- `plugins/hookify/tests/test_config_loader.py` -- nouveau
- `plugins/hookify/tests/test_rule_engine.py` -- nouveau
- `plugins/hookify/tests/test_hooks_integration.py` -- nouveau

---

## Moyenne priorite

### M1 -- 4 hooks hookify quasi-identiques -> un seul point d'entree

**Description**
Les 4 fichiers de hooks hookify sont pratiquement identiques :
- `hooks/pretooluse.py` (74 lignes)
- `hooks/posttooluse.py` (66 lignes)
- `hooks/stop.py` (59 lignes)
- `hooks/userpromptsubmit.py` (58 lignes)

Chaque fichier contient le meme boilerplate : configuration du sys.path, import avec fallback, lecture stdin, creation du RuleEngine, evaluation, serialisation JSON, gestion d'erreur. La seule difference est l'event type passe a `load_rules()`.

**Impact**
- 257 lignes pour un travail qui en necessite 40
- Toute correction (ex: ajout de logging, changement de format de sortie) doit etre appliquee 4 fois
- Risque de divergence entre les hooks

**Solution implementee**
Creation d'un point d'entree unique `hooks/entrypoint.py` :
- Detecte l'event type via `sys.argv[1]` ou la variable d'environnement `HOOKIFY_EVENT`
- Contient toute la logique une seule fois
- Les 4 anciens fichiers deviennent des wrappers d'une ligne qui appellent l'entrypoint
- Mise a jour de `hooks.json` pour passer l'event en argument

**Fichiers modifies**
- `plugins/hookify/hooks/entrypoint.py` -- nouveau point d'entree unique
- `plugins/hookify/hooks/pretooluse.py` -- reduit a un wrapper
- `plugins/hookify/hooks/posttooluse.py` -- idem
- `plugins/hookify/hooks/stop.py` -- idem
- `plugins/hookify/hooks/userpromptsubmit.py` -- idem
- `plugins/hookify/hooks/hooks.json` -- mise a jour des commandes

---

### M2 -- Parseur YAML artisanal de 108 lignes (fragile)

**Description**
`plugins/hookify/core/config_loader.py` contient une reimplementation maison du parsing YAML (fonction `extract_frontmatter`, lignes 87-195). Ce parseur gere manuellement :
- La detection du bloc `---`
- Le parsing key-value
- Les listes (`-`)
- Les dictionnaires dans les listes
- Les booleens (`true`/`false`)
- La gestion de l'indentation

Il echoue silencieusement sur de nombreux cas valides YAML :
- Valeurs contenant `:` (coupe au premier `:`)
- Flow style `{key: value}` et `[item1, item2]`
- Ancres et alias
- Multiligne (`|` et `>`)
- Commentaires inline

**Impact**
- Les utilisateurs qui ecrivent du YAML standard dans leurs fichiers `.local.md` obtiennent des resultats imprevisibles
- Bugs silencieux : le parseur ne leve pas d'erreur, il produit des donnees incorrectes
- Surface de maintenance importante pour un probleme resolu par PyYAML (stdlib indirecte) ou la lib standard `tomllib`

**Solution implementee**
Remplacement par PyYAML (`yaml.safe_load`) avec fallback sur le parseur artisanal si PyYAML n'est pas installe :
- Import conditionnel : `try: import yaml` / `except ImportError: use_custom_parser()`
- Warning log quand le fallback est utilise pour inciter l'utilisateur a installer PyYAML
- Conservation du parseur artisanal comme fallback pour les environnements contraints (le README precise "no external dependencies")

**Fichiers modifies**
- `plugins/hookify/core/config_loader.py` -- import conditionnel de PyYAML, fallback sur le parseur existant

---

### M3 -- Conventions de sortie des hooks inconsistantes

**Description**
Le moteur de regles (`rule_engine.py`) produit des structures de sortie differentes selon le type d'event :
- Pour `Stop` : `{"decision": "block", "reason": "..."}`
- Pour `PreToolUse`/`PostToolUse` : `{"hookSpecificOutput": {"hookEventName": "...", "permissionDecision": "deny"}}`
- Pour les autres : `{"systemMessage": "..."}`

Le champ `systemMessage` est toujours present dans les cas de blocage, mais la structure principale varie. Pas de documentation sur quel format Claude Code attend reellement.

**Impact**
- Difficult a tester et a raisonner sur le comportement
- Les contributeurs doivent lire le code pour comprendre le format de sortie
- Risque d'erreur si le format attendu par Claude Code change

**Solution implementee**
- Documentation du protocole de sortie dans `plugins/hookify/PROTOCOL.md`
- Ajout de commentaires dans `rule_engine.py` referant la documentation Claude Code officielle
- Centralisation de la construction des reponses dans des methodes dediees (`_build_block_response`, `_build_warn_response`)

**Fichiers modifies**
- `plugins/hookify/PROTOCOL.md` -- nouveau, documente le format attendu par Claude Code
- `plugins/hookify/core/rule_engine.py` -- refactoring des constructeurs de reponse

---

### M4 -- .gitignore insuffisant

**Description**
Le `.gitignore` actuel ne contient qu'une ligne : `.DS_Store`. Il manque les patterns standards pour :
- `node_modules/`
- `*.js` / `*.d.ts` generes par compilation TS (si tsconfig ajoute)
- `__pycache__/` / `*.pyc` (Python -- hookify)
- `.env` / `.env.*` (secrets)
- Fichiers d'etat temporaires
- Logs (`*.log`)
- Coverage (`coverage/`, `.nyc_output/`)
- IDE (`.idea/`, `*.swp`, `*.swo`)
- OS (`Thumbs.db`)

**Impact**
- Fichiers indesirables commites accidentellement
- `.DS_Store` est deja la, ce qui suggere que le probleme est connu mais incompletement traite
- `__pycache__/` du plugin hookify pourrait etre commite

**Solution implementee**
Extension du `.gitignore` avec les patterns manquants, organises par categorie.

**Fichiers modifies**
- `.gitignore` -- extension avec les patterns manquants

---

### M5 -- Fuite de fichiers d'etat dans security-guidance

**Description**
Note : ce probleme a ete identifie dans l'analyse mais le fichier `security-guidance` n'est pas present dans l'arborescence actuelle du repo. Il s'agit possiblement d'un fichier genere a runtime ou d'un artefact d'une branche non mergee.

L'analyse signale que des fichiers d'etat temporaires (state files) pourraient etre generes par les workflows et ne pas etre nettoyes, potentiellement exposes s'ils contiennent des informations sensibles.

**Impact**
- Potentielle exposition de donnees d'etat entre les runs de workflow
- Pollution de l'espace de travail

**Solution implementee**
- Ajout de patterns de nettoyage dans le `.gitignore`
- Verification que les workflows ne persistent pas de fichiers d'etat dans l'arborescence trackee

**Fichiers modifies**
- `.gitignore` -- ajout de patterns pour les fichiers d'etat temporaires

---

## Basse priorite

### B1 -- Actions GitHub non epinglees par SHA

**Description**
La plupart des workflows utilisent des references de tag pour les actions tierces, sauf `claude.yml` qui epingle correctement `actions/checkout` par SHA :
```yaml
uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4
```

Les autres workflows utilisent des references de version (`@v4`, `@v1`) ou pas d'actions tierces (scripts inline).

**Impact**
- Risque de supply chain : un tag peut etre deplace vers un commit malveillant
- Recommandation de securite GitHub : toujours epingler par SHA complet
- Impact reel faible dans ce repo car la plupart des workflows n'utilisent que des scripts inline

**Solution implementee**
Verification et epinglage par SHA de toutes les references d'actions tierces dans les workflows. Ajout d'un commentaire avec le tag lisible apres le SHA.

**Fichiers modifies**
- `.github/workflows/claude.yml` -- deja correct
- `.github/workflows/claude-issue-triage.yml` -- a verifier
- `.github/workflows/claude-dedupe-issues.yml` -- a verifier
- Tous les workflows utilisant `actions/*` -- epinglage SHA

---

### B2 -- Pas de validation CI de la structure des plugins

**Description**
Aucun workflow CI ne valide que les plugins respectent la structure attendue :
- Presence de `hooks/hooks.json`
- Format JSON valide
- Presence des fichiers de hooks references
- Presence d'un README.md
- Syntaxe Python valide des hooks

**Impact**
- Un plugin casse peut etre merge sans detection
- Pas de feedback automatique pour les contributeurs

**Solution implementee**
Creation d'un workflow CI `.github/workflows/validate-plugins.yml` qui :
- Itere sur les sous-repertoires de `plugins/`
- Verifie la presence et la validite de `hooks.json`
- Verifie que les fichiers references existent
- Execute `python3 -m py_compile` sur les fichiers Python
- Run les tests hookify si presents

**Fichiers modifies**
- `.github/workflows/validate-plugins.yml` -- nouveau workflow CI

---

### B3 -- Pas de tsconfig.json pour les scripts TS

**Description**
Les 5 scripts TypeScript dans `scripts/` n'ont pas de `tsconfig.json`. Ils utilisent le shebang `#!/usr/bin/env bun` et dependent du runtime Bun pour la transpilation et l'execution. Sans tsconfig :
- Pas de verification de types au CI
- Pas de configuration explicite du target, module system, strict mode
- Les imports entre fichiers (ex: `sweep.ts` importe `issue-lifecycle.ts`) ne sont pas valides

**Impact**
- Erreurs de typage non detectees
- Pas de linting statique possible
- Dependance implicite au comportement par defaut de Bun

**Solution implementee**
Ajout d'un `scripts/tsconfig.json` minimal configure pour Bun :
- `compilerOptions.strict: true`
- `compilerOptions.module: "esnext"`
- `compilerOptions.target: "esnext"`
- `compilerOptions.moduleResolution: "bundler"`
- `compilerOptions.types: ["bun-types"]`
- `compilerOptions.noEmit: true` (type-checking only)

**Fichiers modifies**
- `scripts/tsconfig.json` -- nouveau

---

### B4 -- Protocole de hook non documente

**Description**
Le plugin hookify implemente un protocole de communication avec Claude Code (JSON via stdin/stdout, codes de sortie, structure des reponses) mais ce protocole n'est documente nulle part. Le README explique la configuration utilisateur mais pas :
- Le format JSON attendu en entree (stdin)
- Le format JSON attendu en sortie (stdout)
- La semantique des codes de sortie
- Les champs disponibles par type d'event
- Le comportement en cas d'erreur

**Impact**
- Impossible pour un contributeur de creer un nouveau hook sans lire le code source
- Impossible de tester correctement sans connaitre le contrat
- Risque de hooks qui fonctionnent par accident

**Solution implementee**
Creation de `plugins/hookify/PROTOCOL.md` documentant :
- Le cycle de vie complet d'un appel de hook
- Les schemas JSON entree/sortie pour chaque event type
- La semantique des champs `decision`, `hookSpecificOutput`, `systemMessage`
- Des exemples de payloads stdin et stdout pour chaque event

**Fichiers modifies**
- `plugins/hookify/PROTOCOL.md` -- nouveau

---

## Matrice de suivi

| ID | Priorite | Statut | Assigne a | Description courte |
|----|----------|--------|-----------|-------------------|
| H1 | Haute | En cours | Agent fix | Deduplication githubRequest |
| H2 | Haute | En cours | Agent fix | Rate-limiting GitHub API |
| H3 | Haute | En cours | Agent fix | JSON injection log-issue-events |
| H4 | Haute | En cours | Agent fix | Tests hookify |
| M1 | Moyenne | En cours | Agent fix | Hooks point d'entree unique |
| M2 | Moyenne | En cours | Agent fix | PyYAML vs parseur artisanal |
| M3 | Moyenne | En cours | Agent fix | Conventions sortie hooks |
| M4 | Moyenne | En cours | Agent fix | .gitignore |
| M5 | Moyenne | En cours | Agent fix | Fichiers d'etat |
| B1 | Basse | En cours | Agent fix | Actions SHA pinning |
| B2 | Basse | En cours | Agent fix | CI validation plugins |
| B3 | Basse | En cours | Agent fix | tsconfig.json |
| B4 | Basse | En cours | Agent fix | Documentation protocole hooks |

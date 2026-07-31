"""Static audit of the shipped application source.

This is a standing guard, not a one-off check: if a future change reintroduces
a statement that could destroy financial history, this test fails.

Every allowance below is deliberate and annotated. Anything not explicitly
allowed is a failure.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "plaid_monthly_cashflow" / "app"
PYTHON_SOURCES = sorted(APP_DIR.rglob("*.py"))
STATIC_SOURCES = sorted((APP_DIR / "static").glob("*.js")) + sorted((APP_DIR / "static").glob("*.html"))


def executable_python(original: str) -> str:
    """Return the source with comments and docstrings removed.

    The audit targets *code*. SQL lives in ordinary string literals so those
    must be kept, but prose in comments and docstrings -- including this
    project's own descriptions of the bugs being prevented -- must not trip
    the guard.
    """
    lines = original.splitlines(keepends=True)
    doc_lines: set[int] = set()
    tree = ast.parse(original)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            doc_lines.update(range(value.lineno - 1, value.end_lineno))

    kept = ["\n" if index in doc_lines else line for index, line in enumerate(lines)]
    joined = "".join(kept)
    return "".join(
        token.string if token.type != tokenize.COMMENT else ""
        for token in tokenize.generate_tokens(io.StringIO(joined).readline)
    )


def strip_markup_comments(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", text)

# Tables holding financial history. Nothing may delete from or drop these.
FINANCIAL_TABLES = {
    "transaction_events",
    "account_observations",
    "transactions",
    "accounts",
    "items",
    "sync_runs",
    "sync_log",
    "settings",
}

# Columns that must never be blanked by an UPDATE.
PROTECTED_COLUMNS = ("raw_json", "payload_hash", "amount", "merchant_name", "event_identity")

# Matches a real UPDATE statement and captures its table. Deliberately does not
# match the "ON CONFLICT (...) DO UPDATE SET" upsert form, which is checked
# separately because it can only ever touch the row being inserted.
UPDATE_STATEMENT = r"UPDATE\s+(\w+)\s+SET"


def sources() -> list[tuple[Path, str]]:
    return [(path, executable_python(path.read_text(encoding="utf-8"))) for path in PYTHON_SOURCES]


@pytest.mark.parametrize("pattern", ["DELETE\\s+FROM", "\\bDROP\\s+TABLE\\b", "\\bTRUNCATE\\b", "\\bVACUUM\\b"])
def test_no_destructive_sql_in_application_source(pattern: str) -> None:
    offenders: list[str] = []
    for path, text in sources():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line}: {match.group(0)}")
    assert not offenders, "Destructive SQL found: " + "; ".join(offenders)


def test_only_views_are_dropped_and_only_to_be_recreated() -> None:
    """DROP VIEW is permitted; a view holds no data and is rebuilt immediately."""
    for path, text in sources():
        for match in re.finditer(r"\bDROP\s+(\w+)", text, re.IGNORECASE):
            assert match.group(1).upper() == "VIEW", f"{path.name}: DROP {match.group(1)}"
        # Every dropped view is recreated in the same script.
        for view in re.findall(r"DROP VIEW IF EXISTS (\w+)", text):
            assert f"CREATE VIEW {view}" in text, f"{view} dropped without being recreated"


def test_no_filesystem_deletion_of_database_or_key_files() -> None:
    forbidden = (
        r"\.unlink\(",
        r"os\.remove\(",
        r"os\.unlink\(",
        r"shutil\.rmtree\(",
        r"\.rmdir\(",
        r"os\.truncate\(",
    )
    offenders: list[str] = []
    for path, text in sources():
        for pattern in forbidden:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: {match.group(0)}")
    assert not offenders, "Filesystem deletion found: " + "; ".join(offenders)


def test_no_update_blanks_a_protected_column() -> None:
    """Catches the old `UPDATE transactions SET raw_json = NULL` class of bug."""
    offenders: list[str] = []
    for path, text in sources():
        for column in PROTECTED_COLUMNS:
            for match in re.finditer(rf"{column}\s*=\s*NULL", text, re.IGNORECASE):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: {column} = NULL")
    assert not offenders, "Protected column cleared: " + "; ".join(offenders)


def test_ledger_tables_are_never_updated() -> None:
    """The two append-only tables must only ever appear in INSERT/SELECT."""
    offenders: list[str] = []
    for path, text in sources():
        for match in re.finditer(UPDATE_STATEMENT, text, re.IGNORECASE):
            table = match.group(1).lower()
            if table in {"transaction_events", "account_observations"}:
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}: UPDATE {table}")
    assert not offenders, "Append-only table mutated: " + "; ".join(offenders)


def test_permitted_updates_touch_only_operational_metadata() -> None:
    """Enumerate every UPDATE target and confirm it is non-financial."""
    allowed = {
        "items",  # cursor, active flag, plaid_env, access token on disconnect
        "sync_runs",  # completion status of the run in flight
        "backfill_state",  # backfill bookkeeping
        "settings",  # key/value app settings (ON CONFLICT DO UPDATE)
    }
    found: set[str] = set()
    for _, text in sources():
        found.update(match.group(1).lower() for match in re.finditer(UPDATE_STATEMENT, text, re.IGNORECASE))
    unexpected = found - allowed
    assert not unexpected, f"Unexpected UPDATE targets: {sorted(unexpected)}"


def test_upsert_on_conflict_never_overwrites_a_ledger_row() -> None:
    """Ledger inserts must use DO NOTHING, never DO UPDATE."""
    for path, text in sources():
        for match in re.finditer(r"ON CONFLICT\((\w+)\) DO UPDATE", text, re.IGNORECASE):
            assert match.group(1).lower() not in {
                "event_identity",
                "observation_identity",
            }, f"{path.name}: ledger row overwritten via ON CONFLICT DO UPDATE"


def test_no_database_recreation_helpers_remain() -> None:
    banned_names = (
        "delete_all_plaid_data",
        "_safe_unlink",
        "reset_database",
        "recreate_database",
        "wal_checkpoint(TRUNCATE)",
    )
    for path, text in sources():
        for name in banned_names:
            assert name not in text, f"{path.name} still references {name}"


def test_frontend_exposes_no_data_deleting_control() -> None:
    for path in STATIC_SOURCES:
        text = strip_markup_comments(path.read_text(encoding="utf-8")).lower()
        for phrase in ("delete local data", "delete local cached", "delete all", "wipe"):
            assert phrase not in text, f"{path.name} references '{phrase}'"
        assert "disconnectbutton" not in text


def test_no_committed_secrets_in_repository_sources() -> None:
    """Cheap secret scan over everything that ships."""
    token_shapes = (
        re.compile(r"\b(?:access|public|secret)-(?:sandbox|development|production)-[A-Za-z0-9]{10,}\b"),
        re.compile(r"\bBEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY\b"),
    )
    repo = Path(__file__).resolve().parents[1]
    scanned = 0
    for path in repo.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
            continue
        if path.suffix.lower() not in {".py", ".js", ".html", ".css", ".yaml", ".yml", ".md", ".sh", ".toml", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for shape in token_shapes:
            match = shape.search(text)
            assert match is None, f"Possible secret in {path.relative_to(repo)}"
    assert scanned > 10

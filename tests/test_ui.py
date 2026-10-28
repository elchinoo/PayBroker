"""The pages' regression tests under tests/ui run in node against a tiny fake DOM (no browser, no server). Skipped without node."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent / "ui"
STATIC = Path(__file__).resolve().parent.parent / "src" / "paybroker" / "static"
SCRIPTS = sorted(p.name for p in UI.glob("*.mjs") if p.name != "fakedom.mjs")
needs_node = pytest.mark.skipif(shutil.which("node") is None or not (STATIC.parent.parent.parent.parent / "agentpg-kit").exists(),
                                reason="needs node and the sibling agentpg-kit checkout (its /kit/*.js the pages import)")


@needs_node
@pytest.mark.parametrize("script", SCRIPTS)
def test_page_script(script):
    out = subprocess.run(["node", str(UI / script)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith("ok:")


def test_every_page_has_a_regression_script():
    assert {"cases_typing.mjs", "overview_focus.mjs", "rules_typing.mjs", "merchants_typing.mjs", "settings_typing.mjs",
            "payments_typing.mjs", "maintenance_panel.mjs", "triage_typing.mjs", "dialogs.mjs", "insights_tab.mjs", "audit_tab.mjs"} <= set(SCRIPTS)


def test_the_pages_never_build_markup_from_strings():
    for js in STATIC.glob("*.js"):
        src = js.read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert bad not in src, f"{js.name} uses {bad}"


def test_the_pages_never_use_native_dialogs():
    native = re.compile(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\s*\(")   # confirmDialog( and promptDialog( do not match
    for page in [*STATIC.glob("*.js"), *STATIC.glob("*.html")]:
        for n, line in enumerate(page.read_text().splitlines(), 1):
            assert not native.search(line), f"{page.name}:{n} calls a native dialog; use confirmDialog/promptDialog from /kit/ui.js"

"""The printed report shows every collapsed section, without its label."""

import re

from app.web import STATIC_DIR


def _print_rules() -> dict[str, dict[str, str]]:
    """Selector -> declarations for every rule inside an ``@media print`` block."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC_DIR / "app.css").read_text(), flags=re.S)
    rules: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"@media\s+print\s*\{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css[m.end():i - 1]):
            decls = dict(
                (k.strip(), v.strip()) for k, _, v in
                (d.partition(":") for d in body.split(";") if d.strip())
            )
            for sel in selectors.split(","):
                rules.setdefault(" ".join(sel.split()), {}).update(decls)
    return rules


def test_print_opens_collapsed_sections_and_closes_them_after():
    js = (STATIC_DIR / "app.js").read_text()
    assert 'addEventListener("beforeprint"' in js
    assert 'addEventListener("afterprint"' in js
    # Only the sections that were closed are opened, and so closed again after.
    assert 'querySelectorAll("details:not([open])")' in js
    assert "bindPrintDetails();" in js


def test_print_css_hides_the_label_and_keeps_a_no_script_fallback():
    rules = _print_rules()
    # A "details > *" rule also matches <summary> and prints a lone label.
    assert "details > *" not in rules
    assert rules["details > summary"]["display"] == "none !important"
    assert rules["details::details-content"]["content-visibility"] == "visible"
    # Alone in its rule: browsers that do not know the selector drop only it.
    css = (STATIC_DIR / "app.css").read_text()
    assert re.search(r"@media print \{\s*details::details-content \{[^}]*\}\s*\}", css)


def test_vitals_card_can_break_across_pages(admin):
    rules = _print_rules()
    assert rules[".card"]["break-inside"] == "avoid"
    assert rules[".card.report-vitals"]["break-inside"] == "auto"
    assert rules[".chart-wrap"]["break-inside"] == "avoid"
    assert rules[".vital-report > h3"]["break-after"] == "avoid"
    page = admin.get("/admin/report").text
    assert '<section class="card report-vitals">' in page
    assert "/static/app.js?v=" in page

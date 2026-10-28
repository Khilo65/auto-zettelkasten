from auto_zettelkasten.relationship_mapping import _section


def test_repeated_export_heading_does_not_hide_substantive_section():
    text = '## Thesis\n\n# Thesis\n\nA substantive argument.\n\n## Method and Research Design\n\n# Method and Research Design\n\nA bounded design.\n'
    assert _section(text, 'Thesis') == ('A substantive argument.', 5)
    assert _section(text, 'Method and Research Design') == ('A bounded design.', 11)


def test_same_named_heading_after_body_starts_a_new_section():
    text = '## Thesis\n\nFirst argument.\n\n# Thesis\n\nSecond argument.\n'
    assert _section(text, 'Thesis') == ('First argument.', 3)


def test_different_heading_is_not_skipped():
    assert _section('## Thesis\n\n## Abstract\nAn abstract.\n', 'Thesis') is None

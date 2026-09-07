from copy import deepcopy
import json

import pytest
from auto_zettelkasten import readers

QUOTE = 'The sample included 24 participants'
SOURCE = f'--- Page 2 ---\nBackground.\n--- Page 3 ---\n{QUOTE}.'


def memo(locator='p. 8', quote=QUOTE):
    return {
        **dict.fromkeys(readers.REQUIRED_CHUNK_EVIDENCE_KEYS, 'Not reported.'),
        'statistical_context': (
            f'Evidence: "{quote}"; Reporter: source account; Measured entity: adults; '
            f'Value and unit: 24 participants; Qualification: pilot only; Locator: {locator}.\n'
            'No other results reported.'
        ),
    }


@pytest.mark.parametrize('metadata,expected', [
    ({'_source_context': {'ordinal_to_printed_page': {'3': '17'}}}, 'p. 17'),
    ({}, 'PDF p. 3'),
    ({'extraction': {'ordinal_to_printed_page': {'3': 'iv'}}}, 'p. iv'),
    ({'_source_context': {'ordinal_to_printed_page': {'3': '17', '4': '17'}}}, 'PDF p. 3'),
])
def test_unique_quote_changes_only_locator(metadata, expected):
    original = memo('Citation locator: p. 8')
    before = deepcopy((original, metadata))
    result = readers._canonicalize_chunk_evidence_locators(original, SOURCE.replace('24 participants', '24\n  participants'), metadata)
    assert result == {**original, 'statistical_context': original['statistical_context'].replace('p. 8.', expected + '.')}
    assert (original, metadata) == before
    assert readers._canonicalize_chunk_evidence_locators(result, SOURCE, metadata) == result


@pytest.mark.parametrize('source,locator,quote', [
    (SOURCE + '\n' + QUOTE, 'p. 8', QUOTE),
    (SOURCE + '\n--- Page 4 ---\n' + QUOTE, 'p. 8', QUOTE),
    (QUOTE + '\n' + SOURCE, 'p. 8', QUOTE),
    (SOURCE + '\n--- Page 3 ---\nOther.', 'p. 8', QUOTE),
    ('--- Page ' + '9' * 5000 + ' ---\n' + QUOTE, 'p. 8', QUOTE),
    (SOURCE + '\n--- Page 03 ---\nOther.', 'p. 8', QUOTE),
    (SOURCE + '\n--- Page 1 ---\nOther.', 'p. 8', QUOTE),
    (SOURCE.replace('Page 2', 'Page 0'), 'p. 8', QUOTE),
    (QUOTE, 'p. 8', QUOTE),
    (QUOTE + '\n--- Page 2 ---\nOther.', 'p. 8', QUOTE),
    ('--- Page 2 ---\nThe sample included\n--- Page 3 ---\n24 participants', 'p. 8', QUOTE),
    (SOURCE, 'p. 8', QUOTE.lower()),
    (SOURCE, 'p. 8', QUOTE.replace('24', '25')),
    (SOURCE.replace('included', 'in-\ncluded'), 'p. 8', QUOTE),
    (SOURCE.replace('sample', 'sample,'), 'p. 8', QUOTE),
    (SOURCE, 'pp. 8–9', QUOTE),
    (SOURCE, 'p. 8-9', QUOTE),
    (SOURCE, 'p. 8; PDF p. 3', QUOTE),
    (SOURCE, 'the opening section', QUOTE),
    (SOURCE, 'p. 8', '24 participants'),
    ('--- Page 2 ---\na b c', 'p. 8', 'a b c'),
])
def test_uncertain_or_non_singleton_unchanged(source, locator, quote):
    original = memo(locator, quote)
    assert readers._canonicalize_chunk_evidence_locators(original, source, {}) == original


@pytest.mark.parametrize('reader_type', [readers.DeepSeekReader, readers.OpenRouterReader, readers.GeminiReader, readers.OllamaReader, readers.CodexReader])
def test_shared_summarize_preserves_raw_and_uses_same_fitted_prompt(monkeypatch, reader_type):
    reader = reader_type(model="gpt-5.6-luna") if reader_type in (readers.OpenRouterReader, readers.CodexReader) else reader_type()
    raw = readers._ProviderText(json.dumps(memo()), {'fixture': 'completion'})
    before = str(raw), dict(raw.completion)
    calls = []
    fits = []
    metadata = {'_source_context': {'ordinal_to_printed_page': {'3': '17'}}}
    metadata_before = deepcopy(metadata)
    monkeypatch.setattr(reader, '_authorize_request', lambda: None)
    monkeypatch.setattr(reader, '_reserved_output_tokens', lambda contract, count: count)
    monkeypatch.setattr(reader, '_ensure_prompt_fits', lambda *args, **kwargs: None)
    monkeypatch.setattr(reader, '_prompt_fits', lambda *args: fits.append(args) or True)
    monkeypatch.setattr(reader, '_generate_with_reasoning', lambda *args, **kwargs: calls.append(args) or raw)
    assert reader.chunk_evidence_fits(SOURCE, metadata, max_output_tokens=200)
    result = reader.summarize_chunk(SOURCE, metadata, max_output_tokens=200)
    assert len(calls) == 1
    assert calls[0][:3] == fits[0]
    assert result['statistical_context'] == memo()['statistical_context'].strip().replace('p. 8.', 'p. 17.')
    assert (str(raw), raw.completion) == before
    assert metadata == metadata_before


def test_malformed_repeated_labels_remain_unchanged():
    original = memo()
    original['statistical_context'] = (
        f'Evidence: "{QUOTE}"; Reporter: source; '
        + 'Measured entity: adults; Value and unit: 24; ' * 10000
        + 'Locator: p. 8.'
    )
    assert readers._canonicalize_chunk_evidence_locators(original, SOURCE, {}) == original

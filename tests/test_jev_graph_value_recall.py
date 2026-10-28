"""Exercise optional graph-value projections through actual bounded recall."""
from copy import deepcopy

import pytest

from auto_zettelkasten.jev_graph_value import project_graph_value_edges
from auto_zettelkasten.retrieval import LocalRetrievalIndex
from test_jev_graph_value import request, artifact


def index():
    return LocalRetrievalIndex([
        {'source_id': 'source-a', 'note_id': 'note-a', 'title': 'Entry', 'thesis': 'starttoken',
         'source_scope': 'note_only', 'evidence_eligibility': 'substantive_bounded'},
        {'source_id': 'source-b', 'note_id': 'note-b', 'title': 'Neighbor', 'thesis': 'distinct material',
         'source_scope': 'note_only', 'evidence_eligibility': 'substantive_bounded'},
    ])


def accepted(**extra):
    return {'source_id': 'source-a', 'target_source_id': 'source-b', 'relation_id': 'inferred-ab',
            'relation_type': 'complements', 'decision_status': 'accepted', 'derived': True, **extra}


def projection(label, edges, view):
    req = request()
    assessment = {'request': req, 'artifact': artifact(req, label), 'current_request': req}
    return project_graph_value_edges(edges, [assessment], view=view)


def recall(edges):
    return index().recall('starttoken', relationships=edges, max_notes=2, neighbor_cap=1)


def ids(result):
    return {row['source_id'] for row in result['notes']}


def test_misleading_suppresses_actual_accepted_neighbor_traversal_without_mutation():
    edges = [accepted()]
    original = deepcopy(edges)
    assert ids(recall(edges)) == {'source-a', 'source-b'}
    projected = projection('misleading', edges, 'recall')
    assert projected['edges'] == []
    assert ids(recall(projected['edges'])) == {'source-a'}
    assert edges == original


def test_redundant_hides_graph_edge_but_preserves_actual_recall_route():
    edges = [accepted()]
    assert projection('redundant', edges, 'graph')['edges'] == []
    preserved = projection('redundant', edges, 'recall')['edges']
    assert ids(recall(preserved)) == {'source-a', 'source-b'}


@pytest.mark.parametrize('relation,provenance', [('cites', 'citation'), ('supports', 'manual')])
def test_independent_citation_and_manual_routes_survive_inferred_veto(relation, provenance):
    edge = accepted(derived=False, relation_id='independent-record', relation_type=relation,
                    provenance=provenance)
    projected = projection('misleading', [accepted(), edge], 'recall')
    assert [row['relation_id'] for row in projected['edges']] == ['independent-record']
    assert ids(recall(projected['edges'])) == {'source-a', 'source-b'}


def test_unaccepted_semantic_suggestions_are_not_pretended_to_be_recall_links():
    suggestion = accepted(decision_status='suggested', relation_type='semantic_similarity')
    assert ids(recall([suggestion])) == {'source-a'}
    assert ids(recall(projection('helpful', [suggestion], 'recall')['edges'])) == {'source-a'}

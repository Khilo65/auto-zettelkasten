import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def runner():
    path = Path(__file__).resolve().parents[1] / "tools" / "run_retrieval_comparison.py"
    spec = importlib.util.spec_from_file_location("comparison_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lexical_runner_never_silently_replaces_missing_dense_model(tmp_path):
    module = runner()
    text = "Mediation can resolve armed conflict."
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps({"source_id": "a", "title": "Mediation", "text": text,
                                  "content_sha256": hashlib.sha256(text.encode()).hexdigest()}) + "\n")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{"id": "q", "query": "mediation"}]}))
    output = tmp_path / "results"
    assert module.main(["--corpus", str(corpus), "--tasks", str(tasks), "--output", str(output),
                        "--variants", "lexical", "dense"]) == 0
    records = json.loads((output / "results.json").read_text())
    assert records[0]["status"] == "retrieved"
    assert records[1]["status"] == "blocked"
    assert "no model was loaded" in records[1]["reason"]
    assert json.loads((output / "run.json").read_text())["provider_calls"] == 0
    with pytest.raises(ValueError, match="fresh"):
        module.private_output(output)


def test_runner_results_cannot_enter_a_git_tree(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        runner().private_output(tmp_path / "experiment")
    assert not (tmp_path / "experiment").exists()


def test_vector_file_and_row_order_must_match_frozen_identity(tmp_path):
    np = pytest.importorskip("numpy")
    module = runner()
    path = tmp_path / "vectors.npy"
    np.save(path, np.asarray([[1., 0.], [0., 1.]], dtype="float32"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"vectors_sha256": digest, "source_ids": ["a", "b"]}
    assert module.verified_vector_matrix(path, ["a", "b"], manifest, manifest).shape == (2, 2)
    with pytest.raises(ValueError, match="row ordering"):
        module.verified_vector_matrix(path, ["b", "a"], manifest, manifest)
    np.save(path, np.asarray([[0., 1.], [1., 0.]], dtype="float32"))
    with pytest.raises(ValueError, match="file hash"):
        module.verified_vector_matrix(path, ["a", "b"], manifest, manifest)


def test_legacy_profiles_accept_declared_rows_collection(tmp_path):
    module = runner()
    text = "Mediation can resolve armed conflict."
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(json.dumps({"source_id": "a", "title": "Mediation", "text": text,
                                  "content_sha256": hashlib.sha256(text.encode()).hexdigest()}) + "\n")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{"id": "q", "query": "mediation"}]}))
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({"rows": [{"source_id": "a", "thesis": text}]}))
    output = tmp_path / "results"
    assert module.main(["--corpus", str(corpus), "--tasks", str(tasks), "--output", str(output),
                        "--variants", "legacy", "--legacy-profiles", str(profiles)]) == 0
    assert json.loads((output / "results.json").read_text())[0]["status"] == "retrieved"

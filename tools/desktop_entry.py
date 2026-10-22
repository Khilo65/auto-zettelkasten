"""Frozen desktop entry point; keep the GUI on the process main thread."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile

from auto_zettelkasten.desktop import main as desktop_main


def local_mapping_smoke_test() -> dict:
    """Exercise bundled native and dynamic imports without reading model weights."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import numpy as np
    import torch
    import igraph
    import leidenalg
    from sentence_transformers.models import Transformer, Pooling, Normalize
    from transformers import AutoTokenizer
    from transformers.models.bert.modeling_bert import BertModel

    versions = {name: importlib.metadata.version(name) for name in (
        "numpy", "torch", "transformers", "sentence-transformers", "igraph", "leidenalg",
    )}
    assert all((Transformer, Pooling, Normalize, BertModel))
    tensor = torch.ones((2, 2), device="cpu")
    assert np.array_equal((tensor @ tensor).numpy(), np.full((2, 2), 2))
    graph = igraph.Graph(n=3, edges=[(0, 1), (1, 2)], directed=False)
    partition = leidenalg.find_partition(graph, leidenalg.RBConfigurationVertexPartition, seed=0)
    assert len(partition.membership) == 3
    with tempfile.TemporaryDirectory(prefix="az-local-runtime-smoke-") as temporary:
        root = Path(temporary)
        (root / "config.json").write_text('{"model_type":"bert"}', encoding="utf-8")
        (root / "tokenizer_config.json").write_text('{"tokenizer_class":"BertTokenizer"}', encoding="utf-8")
        (root / "vocab.txt").write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\nhello\nworld\n", encoding="utf-8")
        tokenizer = AutoTokenizer.from_pretrained(root, local_files_only=True, trust_remote_code=False)
        assert tokenizer("hello world")["input_ids"] == [2, 5, 6, 3]
    return {"status": "passed", "platform": sys.platform,
            "checks": ["local_mapping_imports", "cpu_tensor", "leiden_native", "local_tokenizer", "mapping_metadata"],
            "versions": versions}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--local-mapping-smoke-test" not in args:
        return desktop_main(args)
    parser = argparse.ArgumentParser(description="Check bundled local-mapping dependencies offline.")
    parser.add_argument("--local-mapping-smoke-test", action="store_true")
    parser.add_argument("--smoke-report", type=Path)
    options = parser.parse_args(args)
    try:
        report = local_mapping_smoke_test()
    except Exception as exc:
        report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    if options.smoke_report:
        options.smoke_report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    elif sys.stdout is not None:
        print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

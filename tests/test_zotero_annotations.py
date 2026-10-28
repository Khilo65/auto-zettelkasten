from concurrent.futures import ThreadPoolExecutor
import json
import stat

import pytest

from auto_zettelkasten.zotero_annotations import (
    AnnotationConflict, AnnotationError, END, HEADING, PendingAnnotations, START,
    annotation_recall_records, annotation_uri, api_to_reader, project_annotations,
    reader_to_api, strip_annotation_projection, validate_position, verified_annotation_span,
)


SCOPE = "users/0"
ATTACHMENT = "PDFA2345"
KEY = "ANNA2345"


def api(*, kind="highlight", key=KEY, version=3, page=0, text="A quoted finding.", comment="My interpretation."):
    position = {"pageIndex": page, "rects": [[10, 20, 30, 40]]}
    if kind == "ink":
        position = {"pageIndex": page, "paths": [[10, 20, 30, 40]], "width": 2}
    if kind == "text":
        position.update(fontSize=12, rotation=0)
    return {"key": key, "version": version, "itemType": "annotation", "parentItem": ATTACHMENT,
            "annotationType": kind, "annotationPosition": json.dumps(position),
            "annotationPageLabel": "xv", "annotationSortIndex": "00000|000010|00020",
            "annotationText": text, "annotationComment": comment,
            "annotationColor": "#ffd400", "annotationIsExternal": False,
            "tags": [{"tag": "中文"}, {"tag": "Research"}]}


def reader(**kwargs):
    return api_to_reader(api(**kwargs), scope=SCOPE, attachment_key=ATTACHMENT, writable=True)


def groups(*records):
    return [{"scope": SCOPE, "attachment_key": ATTACHMENT, "title": "Source PDF",
             "confirmed": True, "annotations": list(records or [reader()])}]


@pytest.mark.parametrize("kind", ["highlight", "underline", "note", "text", "image", "ink"])
def test_round_trip_keeps_geometry_identity_and_page_label(kind):
    original = api(kind=kind)
    decoded = api_to_reader({"key": KEY, "version": 3, "data": original}, scope=SCOPE, attachment_key=ATTACHMENT, writable=True)
    encoded = reader_to_api(decoded, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=3, existing=decoded)
    assert json.loads(encoded["annotationPosition"]) == json.loads(original["annotationPosition"])
    assert encoded["annotationPageLabel"] == "xv"
    assert encoded["key"] == KEY and encoded["parentItem"] == ATTACHMENT
    assert "annotationText" in encoded if kind in {"highlight", "underline"} else "annotationText" not in encoded
    assert annotation_uri(SCOPE, ATTACHMENT, KEY, 0) == "zotero://open-pdf/library/items/PDFA2345?page=1&annotation=ANNA2345"


def test_next_page_highlight_and_library_scope():
    original = api()
    original["annotationPosition"] = json.dumps({"pageIndex": 9, "rects": [[0, 0, 2, 2]], "nextPageRects": [[1, 1, 3, 3]]})
    decoded = api_to_reader(original, scope="groups/123", attachment_key=ATTACHMENT)
    assert decoded["position"]["nextPageRects"] == [[1, 1, 3, 3]]
    assert "groups/123/items/PDFA2345?page=10" in annotation_uri("groups/123", ATTACHMENT, KEY, 9)
    with pytest.raises(AnnotationError):
        api_to_reader({"library": {"type": "group", "id": 123}, "data": original}, scope="groups/456", attachment_key=ATTACHMENT)
    assert api_to_reader({"library": {"type": "user", "id": 123}, "data": original}, scope="users/0", attachment_key=ATTACHMENT)["id"] == KEY


def test_unknown_external_status_stays_read_only_without_creation_provenance():
    original = api(); del original["annotationIsExternal"]
    assert api_to_reader(original, scope=SCOPE, attachment_key=ATTACHMENT, writable=True)["readOnly"]
    assert not api_to_reader(original, scope=SCOPE, attachment_key=ATTACHMENT, writable=True, known_internal=True)["readOnly"]
    original["annotationIsExternal"] = True
    assert api_to_reader(original, scope=SCOPE, attachment_key=ATTACHMENT, writable=True, known_internal=True)["readOnly"]


@pytest.mark.parametrize("change", [
    {"parentItem": "WRNG2345"}, {"itemType": "book"}, {"key": "bad"},
    {"annotationType": "unknown"}, {"annotationPosition": "{"}, {"version": True},
    {"annotationColor": "url(secret)"}, {"annotationComment": "bad\x00"}, {"tags": ["not-object"]},
])
def test_bad_api_records_rejected(change):
    original = api(); original.update(change)
    with pytest.raises(AnnotationError):
        api_to_reader(original, scope=SCOPE, attachment_key=ATTACHMENT)


@pytest.mark.parametrize("position", [
    {"pageIndex": -1, "rects": [[1, 2, 3, 4]]},
    {"pageIndex": True, "rects": [[1, 2, 3, 4]]},
    {"pageIndex": 0, "rects": [[1, 2, 3]]},
    {"pageIndex": 0, "rects": [[3, 2, 1, 4]]},
    {"pageIndex": 0, "rects": [[0, 0, float("inf"), 4]]},
    {"pageIndex": 0, "rects": [[0, 0, 10**1000, 4]]},
    {"pageIndex": 0, "rects": []},
    {"pageIndex": 0, "rects": [[0, 0, 1, 1]] * 2001},
    {"pageIndex": 0, "rects": [[0, 0, 1, 1]], "url": "external"},
])
def test_invalid_geometry_is_bounded(position):
    with pytest.raises(AnnotationError):
        validate_position(position, "highlight")


def test_drawing_and_text_geometry_validation():
    with pytest.raises(AnnotationError):
        validate_position({"pageIndex": 0, "paths": [[1, 2, 3]], "width": 1}, "ink")
    with pytest.raises(AnnotationError):
        validate_position({"pageIndex": 0, "paths": [[1, 2]], "width": 0}, "ink")
    with pytest.raises(AnnotationError):
        validate_position({"pageIndex": 0, "rects": [[1, 2, 3, 4]], "fontSize": 0, "rotation": 0}, "text")
    with pytest.raises(AnnotationError):
        validate_position({"pageIndex": 0, "rects": [[1, 2, 3, 4]], "nextPageRects": [[1, 2, 3, 4]]}, "note")
    with pytest.raises(AnnotationError):
        validate_position('{"pageIndex":0,"pageIndex":1,"rects":[[1,2,3,4]]}', "note")


def test_write_identity_version_and_external_permissions_cannot_be_forged():
    original = reader()
    assert api_to_reader(api(), scope=SCOPE, attachment_key=ATTACHMENT)["readOnly"]
    for field, value in [("id", "ANNA2346"), ("scope", "groups/9"), ("attachment_key", "PDFB2345")]:
        draft = {**original, field: value}
        with pytest.raises(AnnotationError):
            reader_to_api(draft, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=3, existing=original)
    with pytest.raises(AnnotationConflict):
        reader_to_api(original, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=2, existing=original)
    with pytest.raises(AnnotationError):
        reader_to_api(original, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=3)
    external = {**api(), "annotationIsExternal": True}
    locked = api_to_reader(external, scope=SCOPE, attachment_key=ATTACHMENT, writable=True)
    assert locked["isExternal"] and locked["readOnly"]
    forged = {**locked, "readOnly": False, "isExternal": False}
    with pytest.raises(AnnotationError):
        reader_to_api(forged, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=3, existing=locked)


def test_native_sort_index_color_and_text_normalization():
    original = reader()
    draft = {**original, "color": "#FFCC00", "comment": "  Cafe\u0301 \n", "text": "  A quote.  ", "pageLabel": "  xv  ",
             "tags": [{"name": "  Cafe\u0301  "}, {"name": "Café"}]}
    encoded = reader_to_api(draft, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=3, existing=original)
    assert encoded["annotationColor"] == "#ffcc00"
    assert encoded["annotationComment"] == "Café" and encoded["annotationText"] == "A quote."
    assert encoded["annotationPageLabel"] == "xv"
    assert encoded["tags"] == [{"tag": "Café"}]
    for invalid in ["", "00000|000001", "00000|000001|000001", "٠٠٠٠٠|000001|00001"]:
        with pytest.raises(AnnotationError):
            reader_to_api({**original, "sortIndex": invalid}, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=3, existing=original)
    for invalid_key in ["ANNA1234", "ANN02345", "ANNO2345"]:
        legacy = reader(key=invalid_key)
        with pytest.raises(AnnotationError):
            reader_to_api(legacy, scope=SCOPE, attachment_key=ATTACHMENT, expected_version=3, existing=legacy)


def test_projection_is_deterministic_updates_by_key_preserves_prose_and_crlf():
    original = "# Personal title\r\n\r\nHuman prose.\r\n\r\n## Related notes — automatic\r\n<!-- auto-zettelkasten:related:start -->\r\n[[Other]]\r\n"
    a, b = reader(), reader(key="ANNA2346", page=2)
    result = project_annotations(original, groups(b, a))
    assert result["text"].index(HEADING) < result["text"].index("## Related notes")
    assert result["text"].startswith("# Personal title\r\n\r\nHuman prose.\r\n\r\n")
    assert result["text"].endswith("[[Other]]\r\n")
    assert "\n" not in result["text"].replace("\r\n", "")
    assert result["text"].index("annotation=ANNA2345") < result["text"].index("annotation=ANNA2346")
    same = project_annotations(result["text"], groups(a, b), expected_hash=result["block_hash"])
    assert same == result
    a["comment"] = "Changed comment"
    updated = project_annotations(result["text"], groups(a), expected_hash=result["block_hash"])
    assert "ANNA2346" not in updated["text"] and updated["text"].count("annotation=ANNA2345") == 1
    assert "Changed comment" in updated["text"]
    emptied = project_annotations(updated["text"], [], expected_hash=updated["block_hash"])
    assert "No confirmed annotations" in emptied["text"] and "ANNA2345" not in emptied["text"]


@pytest.mark.parametrize("mutation", [
    lambda text: text.replace("quoted finding", "user-edited quote"),
    lambda text: text.replace(START, ""),
    lambda text: text.replace(START, START + "\n" + START),
    lambda text: text.replace(END, ""),
    lambda text: "No markers remain.",
])
def test_damaged_or_edited_projection_preserved(mutation):
    projected = project_annotations("Personal prose\n", groups())
    changed = mutation(projected["text"])
    with pytest.raises(AnnotationConflict):
        project_annotations(changed, groups(), expected_hash=projected["block_hash"])
    assert strip_annotation_projection(changed, expected_hash=projected["block_hash"]) == changed
    assert verified_annotation_span(changed, projected["block_hash"]) is None


def test_unverified_projection_is_never_hidden_from_recall():
    result = project_annotations("Source summary.\n", groups())
    assert strip_annotation_projection(result["text"]) == result["text"]
    span = verified_annotation_span(result["text"], result["block_hash"])
    assert span and result["text"][span[0]:span[1]].startswith(START)
    stripped = strip_annotation_projection(result["text"], expected_hash=result["block_hash"])
    assert "Source summary." in stripped and "quoted finding" not in stripped
    with pytest.raises(AnnotationConflict):
        project_annotations(result["text"], groups())


def test_projection_requires_confirmed_snapshot_and_matching_pdf():
    group = groups()[0]
    with pytest.raises(AnnotationError):
        project_annotations("", [{**group, "confirmed": False}])
    with pytest.raises(AnnotationConflict):
        project_annotations("", [{**group, "pdf_hash": "new", "expected_pdf_hash": "old"}])
    with pytest.raises(AnnotationError):
        project_annotations("", groups(reader(), reader()))
    with pytest.raises(AnnotationError):
        project_annotations("", [group, group])


def test_quote_comment_markdown_escaping_and_visual_preview():
    draft = reader(text="[[Injected]] <script>bad</script>\n" + START, comment="![image](https://bad) **claim**")
    projected = project_annotations("Manual [[link]]\n", groups(draft))["text"]
    assert projected.count(START) == 1
    assert "<script>" not in projected and "[[Injected]]" not in projected
    assert "**Source quotation:**" in projected and "**Personal comment:**" in projected
    visual = reader(kind="image", text="Do not fabricate this as a quote")
    result = project_annotations("", groups(visual), previews={(SCOPE, ATTACHMENT, KEY): "annotations/preview.png"})
    assert "![Annotation preview](annotations/preview.png)" in result["text"]
    assert "Do not fabricate" not in result["text"]
    with pytest.raises(AnnotationError):
        project_annotations("", groups(visual), previews={(SCOPE, ATTACHMENT, KEY): "../secret.png"})
    with pytest.raises(AnnotationError):
        project_annotations("", groups(visual), previews={(SCOPE, ATTACHMENT, KEY): "https://bad/image.png"})


def test_recall_keeps_quotes_comments_and_locator_separate():
    record = annotation_recall_records(groups())[0]
    assert record["quote"] == "A quoted finding." and record["comment"] == "My interpretation."
    assert record["page_index"] == 0 and record["page_label"] == "xv"
    assert record["scope"] == SCOPE and record["annotation_key"] == KEY


def enqueue(outbox, **kwargs):
    return outbox.enqueue(scope=SCOPE, attachment_key=ATTACHMENT, annotation_key=KEY,
                          operation="upsert", payload=api(), base_version=3, **kwargs)


def test_outbox_recovery_does_not_resend_unknown_outcome(tmp_path):
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = enqueue(outbox)
    dispatched = outbox.mark_dispatched(queued["id"], queued["revision"])
    reopened = PendingAnnotations(tmp_path / "outbox")
    assert reopened.pending()[0]["state"] == "dispatched"
    with pytest.raises(AnnotationConflict):
        reopened.mark_dispatched(dispatched["id"], dispatched["revision"])
    with pytest.raises(AnnotationConflict):
        enqueue(reopened, expected_revision=dispatched["revision"])
    uncertain = reopened.mark_uncertain(dispatched["id"], dispatched["revision"])
    confirmed = reopened.reconcile(uncertain["id"], uncertain["revision"], remote_record=api(version=4))
    assert confirmed["state"] == "confirmed" and reopened.pending() == []
    assert reopened.lookup(SCOPE, ATTACHMENT, KEY) == confirmed
    assert stat.S_IMODE((tmp_path / "outbox").stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in (tmp_path / "outbox").iterdir())


def test_outbox_unchanged_requeues_but_remote_edits_conflict(tmp_path):
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = enqueue(outbox)
    dispatched = outbox.mark_dispatched(queued["id"], queued["revision"])
    queued = outbox.reconcile(dispatched["id"], dispatched["revision"], remote_record=api())
    assert queued["state"] == "queued"
    dispatched = outbox.mark_dispatched(queued["id"], queued["revision"])
    conflict = outbox.reconcile(dispatched["id"], dispatched["revision"], remote_record=api(version=4, comment="Other application's edit"))
    assert conflict["state"] == "conflict" and conflict["payload"]["annotationComment"] == "My interpretation."


def test_outbox_delete_only_confirmed_by_authoritative_absence(tmp_path):
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = outbox.enqueue(scope=SCOPE, attachment_key=ATTACHMENT, annotation_key=KEY, operation="delete", base_version=3)
    dispatched = outbox.mark_dispatched(queued["id"], queued["revision"])
    with pytest.raises(AnnotationError):
        outbox.reconcile(dispatched["id"], dispatched["revision"])
    with pytest.raises(AnnotationError):
        outbox.reconcile(dispatched["id"], dispatched["revision"], remote_record=api(), confirmed_absent=True)
    confirmed = outbox.confirm(dispatched["id"], dispatched["revision"], confirmed_absent=True)
    assert confirmed["state"] == "confirmed"


def test_outbox_reconciliation_checks_identity_and_stale_revisions(tmp_path):
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = enqueue(outbox)
    dispatched = outbox.mark_dispatched(queued["id"], queued["revision"])
    with pytest.raises(AnnotationError):
        outbox.confirm(dispatched["id"], dispatched["revision"], remote_record=api(key="ANNA9999", version=4))
    with pytest.raises(AnnotationConflict):
        outbox.mark_uncertain(queued["id"], queued["revision"])
    assert outbox.get(queued["id"])["state"] == "dispatched"


def test_concurrent_enqueue_has_one_winner_and_isolates_library(tmp_path):
    root = tmp_path / "outbox"
    def attempt(_):
        try:
            return enqueue(PendingAnnotations(root))["state"]
        except AnnotationConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert outcomes.count("queued") == 1 and outcomes.count("conflict") == 5
    other = PendingAnnotations(root).enqueue(scope="groups/2", attachment_key=ATTACHMENT, annotation_key=KEY,
                                            operation="upsert", payload=api(), base_version=3)
    assert other["state"] == "queued" and len(PendingAnnotations(root).pending()) == 2


def test_outbox_rejects_public_directory_symlinks_and_external_records(tmp_path):
    public = tmp_path / "public"; public.mkdir(mode=0o755)
    with pytest.raises(AnnotationError):
        PendingAnnotations(public).pending()
    real = tmp_path / "real"; real.mkdir(mode=0o700)
    link = tmp_path / "link"; link.symlink_to(real, target_is_directory=True)
    with pytest.raises((AnnotationError, OSError)):
        PendingAnnotations(link).pending()
    outbox = PendingAnnotations(real)
    with pytest.raises(AnnotationError):
        outbox.enqueue(scope=SCOPE, attachment_key=ATTACHMENT, annotation_key=KEY, operation="upsert",
                       payload={**api(), "annotationIsExternal": True}, base_version=3)


def test_outbox_create_recovers_ack_loss_without_duplicate_creation(tmp_path):
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = outbox.enqueue(scope=SCOPE, attachment_key=ATTACHMENT, annotation_key=KEY,
                          operation="upsert", payload=api(version=0), base_version=0)
    dispatched = outbox.mark_dispatched(queued["id"], queued["revision"])
    confirmed = outbox.confirm(dispatched["id"], dispatched["revision"], remote_record=api(version=1))
    assert confirmed["state"] == "confirmed"
    with pytest.raises(AnnotationConflict):
        outbox.mark_dispatched(confirmed["id"], confirmed["revision"])


def test_outbox_crash_during_atomic_replace_preserves_previous_revision(tmp_path, monkeypatch):
    import auto_zettelkasten.zotero_annotations as annotations
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = enqueue(outbox)
    def interrupted(*args, **kwargs):
        raise OSError("simulated interruption")
    monkeypatch.setattr(annotations.os, "replace", interrupted)
    with pytest.raises(OSError):
        outbox.mark_dispatched(queued["id"], queued["revision"])
    assert outbox.get(queued["id"]) == queued
    assert not list((tmp_path / "outbox").glob(".pending-*"))


def test_explicit_discard_preserves_payload_and_refuses_unknown_dispatch(tmp_path):
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = enqueue(outbox)
    discarded = outbox.discard(queued["id"], queued["revision"])
    assert discarded["state"] == "discarded" and discarded["payload"] == queued["payload"]
    assert outbox.pending() == [] and outbox.get(queued["id"])["events"] == ["queued", "discarded"]
    queued = enqueue(outbox, expected_revision=discarded["revision"])
    sent = outbox.mark_dispatched(queued["id"], queued["revision"])
    with pytest.raises(AnnotationConflict):
        outbox.discard(sent["id"], sent["revision"])
    uncertain = outbox.mark_uncertain(sent["id"], sent["revision"])
    with pytest.raises(AnnotationConflict):
        outbox.discard(uncertain["id"], uncertain["revision"])


def test_creation_receipt_allows_reconcile_unchanged_record_without_external_flag(tmp_path):
    outbox = PendingAnnotations(tmp_path / "outbox")
    queued = enqueue(outbox)
    sent = outbox.mark_dispatched(queued["id"], queued["revision"])
    remote = api(); del remote["annotationIsExternal"]
    again = outbox.reconcile(sent["id"], sent["revision"], remote_record=remote, known_internal=True)
    assert again["state"] == "queued"

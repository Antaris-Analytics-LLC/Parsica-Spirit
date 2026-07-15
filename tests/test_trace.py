"""Routing trace hygiene: content-free by construction, scrub-gated anyway, hash-only
queries, best-effort emission that never scaffolds a store and never raises."""
import json
import os
import tempfile

from parsica_spirit.trace import emit_trace, exposure, query_hash


def _read_traces(root):
    tdir = os.path.join(root, ".parsica_router", "trace")
    blob, rows = "", []
    if os.path.isdir(tdir):
        for fn in sorted(os.listdir(tdir)):
            text = open(os.path.join(tdir, fn), encoding="utf-8").read()
            blob += text
            rows += [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    return blob, rows


def test_emit_writes_event_and_exposures():
    root = tempfile.mkdtemp(prefix="tr_root_")
    eid = emit_trace(root, scope_id="proj-a", route_mode="federated",
                     qhash=query_hash("secret question"),
                     weave_candidates=2, spirit_candidates=1, returned=3, deduped=0,
                     layer_order=["weave", "spirit"],
                     exposures=[exposure("c1", "recent", 0, source_kind="mirror_chunk"),
                                exposure("r9", "durable", 0, source_kind="lesson",
                                         content_hash="cafe1234")])
    assert eid
    blob, rows = _read_traces(root)
    kinds = [r["kind"] for r in rows]
    assert kinds == ["trace", "exposure", "exposure"]
    assert rows[0]["event_id"] == eid and all(r["event_id"] == eid for r in rows[1:])
    assert rows[0]["route_mode"] == "federated" and rows[0]["layer_order"] == ["weave", "spirit"]


def test_query_text_never_lands_only_its_hash():
    root = tempfile.mkdtemp(prefix="tr_q_")
    q = "the DISTINCTIVEQUERYTEXT nobody may persist"
    emit_trace(root, scope_id="proj-a", route_mode="federated", qhash=query_hash(q))
    blob, rows = _read_traces(root)
    assert "DISTINCTIVEQUERYTEXT" not in blob
    assert rows[0]["query_hash"] == query_hash(q)


def test_emit_never_scaffolds_and_never_raises():
    missing = os.path.join(tempfile.mkdtemp(prefix="tr_m_"), "nope")
    assert emit_trace(missing, scope_id="s", route_mode="federated") is None
    assert not os.path.exists(missing), "a trace must never create the store"
    # a hostile payload must not raise either (best-effort contract)
    assert emit_trace(missing, scope_id="s", route_mode="x",
                      exposures=[{"weird": object()}]) is None


def test_string_values_pass_the_scrub_gate():
    """Belt and suspenders: the fields are content-free by design, but if a secret
    ever rides in through one (an error message, a hostile label), it lands redacted."""
    root = tempfile.mkdtemp(prefix="tr_s_")
    emit_trace(root, scope_id="proj-a", route_mode="spirit_failed",
               error_kind="boom sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA in message")
    blob, _rows = _read_traces(root)
    assert "sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA" not in blob

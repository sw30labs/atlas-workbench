"""Real loopback HTTP authorization against temporary, fictional workspaces."""

import http.client
import json
import shutil
import sqlite3
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from workbench_atlas.web import enterprise, server
from workbench_atlas.web.access import sign_proxy_headers

SECRET = b"synthetic-proxy-http-test-material-0001"
ORIGIN = "https://atlas.example.org"


@pytest.fixture
def proxy_dashboard(settings, tmp_path, monkeypatch):
    configured = settings.model_copy(update={
        "context_enabled": True, "context_db_path": tmp_path / "organization.db",
    })
    monkeypatch.setattr(server, "get_settings", lambda: configured)
    monkeypatch.setattr(enterprise, "PREVIEWS", enterprise.PreviewCache())
    monkeypatch.setenv("ATLAS_ACCESS_MODE", "proxy")
    monkeypatch.setenv("ATLAS_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("ATLAS_PROXY_HMAC_SECRET", SECRET.decode())
    monkeypatch.setenv("ATLAS_PROXY_NONCE_DB", str(tmp_path / "nonces.sqlite3"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], configured
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def signed_request(port, path, body=None, *, role="viewer", subject="synthetic-viewer",
                   capabilities=(), signed=True, headers=None):
    method = "POST" if body is not None else "GET"
    raw = json.dumps(body).encode() if body is not None else b""
    outgoing = {"Origin": ORIGIN, "Content-Type": "application/json"}
    if signed:
        outgoing.update(sign_proxy_headers(secret=SECRET, public_origin=ORIGIN, subject=subject,
            role=role, method=method, target=path, body=raw, capabilities=capabilities))
    outgoing.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=raw, headers=outgoing)
        response = connection.getresponse()
        payload = response.read()
        return response.status, json.loads(payload)
    finally:
        connection.close()


def test_every_api_requires_signed_identity_and_replay_is_rejected(proxy_dashboard):
    port, _ = proxy_dashboard
    assert signed_request(port, "/api/data", signed=False)[0] == 401
    headers = sign_proxy_headers(secret=SECRET, public_origin=ORIGIN, subject="synthetic-viewer",
                                 role="viewer", method="GET", target="/api/access")
    status, principal = signed_request(port, "/api/access", signed=False, headers=headers)
    assert status == 200 and principal["subject"] == "synthetic-viewer"
    assert signed_request(port, "/api/access", signed=False, headers=headers)[0] == 401
    assert signed_request(port, "/api/unknown", role="admin")[0] == 403


def test_proxy_subject_overrides_context_actor_and_records_the_verified_identity(proxy_dashboard):
    port, settings = proxy_dashboard
    body = {"kind": "services", "actor": "forged-user", "record": {
        "id": "synthetic-service", "name": "Fictional service", "owner": "Fictional owner",
        "source": "Fictional HTTP test fixture", "classification": "synthetic",
        "impact_tolerance": "Synthetic two-hour limit",
    }}
    assert signed_request(port, "/api/context/save", body)[0] == 403
    assert not settings.context_db_path.exists()
    status, result = signed_request(port, "/api/context/save", body, role="analyst", subject="verified-analyst")
    assert status == 200, result
    with sqlite3.connect(settings.context_db_path) as connection:
        assert connection.execute("SELECT actor FROM context_decisions").fetchone()[0] == "verified-analyst"


def test_reviewer_identity_overrides_supplied_evidence_actor(proxy_dashboard):
    port, settings = proxy_dashboard
    # Keep fictional source provenance; make this synthetic fixture reviewable.
    with sqlite3.connect(settings.evidence_db_path) as connection:
        connection.execute("UPDATE evidence_events SET review_status='pending' WHERE event_id='SYN-EVT-000'")
    body = {"kind": "event", "action": "reject", "record_id": "SYN-EVT-000",
            "reason": "Synthetic HTTP rejection", "reviewer": "forged-reviewer"}
    assert signed_request(port, "/api/review/decide", body, role="analyst")[0] == 403
    status, result = signed_request(port, "/api/review/decide", body, role="reviewer", subject="verified-reviewer")
    assert status == 200, result
    with sqlite3.connect(settings.evidence_db_path) as connection:
        assert connection.execute("SELECT actor FROM review_decisions").fetchone()[0] == "verified-reviewer"


def test_export_requires_capability_and_frozen_preview_cannot_cross_users(proxy_dashboard):
    port, settings = proxy_dashboard
    with sqlite3.connect(settings.scenario_db_path) as connection:
        connection.execute("INSERT INTO scenarios (scenario_id,title,description,status,origin) VALUES (?,?,?,?,?)",
            ("SYN-AUTH-SCN", "Fictional scenario", "Synthetic test", "active", "synthetic_fixture"))
    body = {"selection": {"scenario_ids": ["SYN-AUTH-SCN"]}}
    assert signed_request(port, "/api/exports/preview", body)[0] == 403
    status, result = signed_request(port, "/api/exports/preview", body,
                                     capabilities=("export",), subject="verified-exporter")
    assert status == 200, result
    download = {"preview_token": result["preview_token"], "format": "json"}
    assert signed_request(port, "/api/exports/download", download,
                          capabilities=("export",), subject="another-exporter")[0] == 400
    assert signed_request(port, "/api/exports/download", download,
                          capabilities=("export",), subject="verified-exporter")[0] == 200


def test_origin_and_role_denials_happen_before_job_execution(proxy_dashboard, monkeypatch):
    port, _ = proxy_dashboard
    calls = []
    monkeypatch.setattr(server.runner, "start_job", lambda phase, params: (calls.append(params) or ("synthetic-job", None)))
    body = {"phase": "phase4", "actor": "forged"}
    assert signed_request(port, "/api/run", body, role="reviewer")[0] == 403
    assert signed_request(port, "/api/run", body, role="analyst", headers={"Origin": "https://other.example.org"})[0] == 403
    assert not calls
    status, result = signed_request(port, "/api/run", body, role="analyst", subject="verified-analyst")
    assert status == 202, result
    assert calls[0]["actor"] == "verified-analyst"


@pytest.mark.parametrize("status,badge,is_error", [
    (401, "SIGN IN REQUIRED", True),
    (403, "MODEL STATUS RESTRICTED", False),
    (404, "API STALE — RESTART SERVER", True),
    (503, "MODEL STATUS UNAVAILABLE", True),
])
def test_model_status_distinguishes_permission_from_server_failure(status, badge, is_error):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the dashboard status function")
    html = (Path(server.__file__).parent / "static/index.html").read_text()
    function = "async function checkOmlx() {" + html.split("async function checkOmlx() {", 1)[1].split("setInterval(checkOmlx", 1)[0]
    script = """
const elements = Object.fromEntries(['#badge-mode','#omlx-status'].map(id=>[id,{
  textContent:'old connection details', bad:true,
  classList:{toggle(_name,value){elements[id].bad=value;}}
}]));
const $ = id => elements[id];
let OMLX={reachable:true,base_url:'synthetic previous endpoint',model:'previous model'};
const fetch=async()=>({ok:false,status:STATUS});
FUNCTION
checkOmlx().then(()=>console.log(JSON.stringify({badge:elements['#badge-mode'].textContent,
  bad:elements['#badge-mode'].bad,detail:elements['#omlx-status'].textContent,connection:OMLX})));
""".replace("STATUS", str(status)).replace("FUNCTION", function)
    completed = subprocess.run([node, "-"], input=script, capture_output=True, text=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    assert result["badge"] == badge and result["bad"] is is_error
    assert result["connection"]["base_url"] == "" and result["connection"]["model"] == ""
    assert "old connection details" not in result["detail"]


@pytest.mark.parametrize("mode,may_review,expected_name", [
    ("proxy", False, "verified-reviewer"),
    ("proxy", True, "verified-reviewer"),
    ("local", True, "Chosen local name"),
])
def test_batch_decisions_obey_role_and_show_bound_identity(mode, may_review, expected_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the dashboard review controls")
    html = (Path(server.__file__).parent / "static/index.html").read_text()
    functions = "function bindVerifiedReviewIdentity() {" + html.split("function bindVerifiedReviewIdentity() {", 1)[1].split("async function renderReview(", 1)[0]
    script = """
const elements={};
const $=id=>elements[id]||(elements[id]={value:'Chosen local name',checked:true,
  textContent:'',classList:{toggle(){}},setAttribute(){},removeAttribute(){},querySelectorAll(){return [];}});
const $$=()=>[];
const window={atlasPrincipal:{mode:MODE,subject:'verified-reviewer'},atlasMayReview:MAY_REVIEW};
const selected=[{kind:'event',report:{approvable:true}}];
const selectedReviewItems=()=>selected,reviewItems=()=>selected,rvKey=()=> 'synthetic-event';
const rvNeedsReviewer=()=>false,rvNeedsAttestation=()=>false,updateReviewButtons=()=>{};
const reviewState={batchRunning:false,checked:new Set(['synthetic-event'])};
$('#review-batch-note').value='Synthetic rejection reason';
FUNCTIONS
updateReviewBatchControls();
console.log(JSON.stringify({approve:$('#review-batch-approve').disabled,
 reject:$('#review-batch-reject').disabled,
 single:$('#rv-reviewer').value,batch:$('#review-batch-reviewer').value,
 singleReadonly:$('#rv-reviewer').readOnly,batchReadonly:$('#review-batch-reviewer').readOnly}));
""".replace("MODE", json.dumps(mode)).replace("MAY_REVIEW", json.dumps(may_review)).replace("FUNCTIONS", functions)
    completed = subprocess.run([node, "-"], input=script, capture_output=True, text=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    assert result["approve"] is (not may_review) and result["reject"] is (not may_review)
    assert result["single"] == result["batch"] == expected_name
    assert result["singleReadonly"] is (mode == "proxy") and result["batchReadonly"] is (mode == "proxy")


def test_batch_decision_confirms_in_page_and_disarms_on_change():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the dashboard review controls")
    html = (Path(server.__file__).parent / "static/index.html").read_text()
    end = '].join("\\n");'
    signature = "const rvBatchSignature" + html.split("const rvBatchSignature", 1)[1].split(end, 1)[0] + end
    controls = "function bindVerifiedReviewIdentity() {" + html.split("function bindVerifiedReviewIdentity() {", 1)[1].split("async function renderReview(", 1)[0]
    submit = "function setReviewBatchStatus(" + html.split("function setReviewBatchStatus(", 1)[1].split("function reviewJobLine(", 1)[0]
    script = """
const elements={};
const $=id=>elements[id]||(elements[id]={value:'',checked:true,disabled:false,className:'',
  textContent:'',classList:{toggle(){}},setAttribute(){},removeAttribute(){},querySelectorAll(){return [];}});
const $$=()=>[];
const window={atlasMayReview:true};
const selected=[{kind:'event',record_id:'EV-SYN',report:{approvable:true}}];
const selectedReviewItems=()=>selected,reviewItems=()=>selected,rvKey=it=>it.kind+':'+it.record_id;
const rvNeedsReviewer=()=>false,rvNeedsAttestation=()=>false,updateReviewButtons=()=>{};
const reviewState={batchRunning:false,checked:new Set(['event:EV-SYN']),batchArmed:null};
let posts=0;
const fetch=async()=>{posts++;return {ok:true,status:200,json:async()=>({results:[]})};};
$('#review-batch-note').value='Synthetic rejection reason';
$('#review-batch-reviewer').value='Synthetic Reviewer';
SIGNATURE
CONTROLS
SUBMIT
(async()=>{
  updateReviewBatchControls();
  await submitReviewBatch('reject');
  const armed={posts,label:$('#review-batch-reject').textContent,status:$('#review-batch-status').textContent};
  $('#review-batch-note').value='Edited reason';
  updateReviewBatchControls();
  const disarmed={armed:reviewState.batchArmed,label:$('#review-batch-reject').textContent};
  await submitReviewBatch('reject');
  await submitReviewBatch('reject').catch(()=>{});
  console.log(JSON.stringify({armed,disarmed,posts}));
})();
""".replace("SIGNATURE", signature).replace("CONTROLS", controls).replace("SUBMIT", submit)
    completed = subprocess.run([node, "-"], input=script, capture_output=True, text=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    # First click only arms and explains; nothing is sent.
    assert result["armed"]["posts"] == 0
    assert result["armed"]["label"] == "CONFIRM REJECT 1"
    assert "again to confirm" in result["armed"]["status"]
    # Editing the shared note disarms, so the next click re-arms rather than sends.
    assert result["disarmed"] == {"armed": None, "label": "REJECT 1 SELECTED"}
    # Re-arm, then the confirming click sends exactly one batch request.
    assert result["posts"] == 1

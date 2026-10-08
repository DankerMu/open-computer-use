# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
"""Office host contracts through real JS modules and controlled browser dependencies."""
from __future__ import annotations

import json
import subprocess
from urllib.parse import quote

import pytest

from tests.orchestrator.test_preview_prefix import CHAT, NODE, SERVER_DIR


_HARNESS = r"""
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const [directory, optionsJson] = process.argv.slice(2);
const options = JSON.parse(optionsJson);
const chat = options.chat;
const file = options.file ?? 'document-id';
const calls = [], messages = [], scripts = [], configs = [], order = [], actionStates = [];
const listeners = new Map(), timers = new Map();
let timerId = 0, attempts = 0, resolveCreate, resolveSave, pendingLoad;
let now = 0, activeStatuses = 0, maxActiveStatuses = 0, statusReads = 0;
const heldStatuses = [];
const clone = value => JSON.parse(JSON.stringify(value));
const signed = {documentType:'word', document:{key:'stable-key',url:'http://private.test/office/source/ticket',fileType:'docx',permissions:{edit:true}},editorConfig:{callbackUrl:'http://private.test/office/callback/chat/session',mode:'edit',customization:{forcesave:false}},token:'signed-configuration-token'};
const initial = {session_id:'session-id',file_id:file,document_key:'stable-key',state:'editing',reason:null,save_seq:2,last_committed_seq:2,last_published_seq:2,workspace_changed:false,saved_as:null,...options.status};
let currentStatus = clone(initial);
const response = (status, body) => ({ok:status>=200&&status<300,status,json:async()=>clone(body)});
function element(tag) {
  return {tagName:tag.toUpperCase(),children:[],style:{},attributes:{},hidden:false,textContent:'',parentNode:null,
    setAttribute(key,value){this.attributes[key]=String(value);},
    removeAttribute(key){delete this.attributes[key];},
    appendChild(child){this.children.push(child);child.parentNode=this;
      if(this.tagName==='HEAD'&&child.tagName==='SCRIPT'){
        scripts.push(child.src);
        if(options.api==='timeout'){pendingLoad=child.onload;return child;}
        queueMicrotask(()=>{
          if(options.api==='error'){child.onerror?.();return;}
          hostWindow.DocsAPI=options.api==='missing'?{}:{DocEditor:class {
            constructor(id,config){attempts++;if(options.api==='throw')throw new Error('constructor failed');
              configs.push(config);
              if(options.syncError)config.events.onError({data:{errorCode:-18,errorDescription:'connection lost'}});
              if(options.syncModified)config.events.onDocumentStateChange({data:true});
              config.events.onDocumentReady?.({});
            }
          }};
          child.onload?.();
        });
      }return child;
    },
    replaceChildren(...children){for(const child of this.children)child.parentNode=null;this.children=[];for(const child of children)this.appendChild(child);},
    remove(){if(this.parentNode){const list=this.parentNode.children;list.splice(list.indexOf(this),1);this.parentNode=null;}}
  };
}
const container=element('section');container.id='office-editor';
const hostDocument={head:element('head'),createElement:element};
const hostWindow={location:{origin:'https://webui.test'},parent:{
  postMessage(data,target){messages.push({data:clone(data),target});order.push('message:'+data.type);
    if(data.type==='ocu:office-ready'&&options.replyOnReady){dispatch(open);if(options.duplicateOnReady)dispatch({...open,generation:8});}
  }
},addEventListener(type,listener){order.push('listener:'+type);listeners.set(type,listener);},
  setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms,due:now+ms});return id;},clearTimeout(id){timers.delete(id);}};
const open={type:'ocu:office-open',chat_id:chat,file_id:file,generation:7};
function dispatch(data, changes={}){listeners.get('message')?.({source:hostWindow.parent,origin:hostWindow.location.origin,data,...changes});}
const fetch=async(url,init={})=>{
  const method=init.method||'GET';const headers=Object.fromEntries(new Headers(init.headers));
  const body=init.body?JSON.parse(init.body):null;
  calls.push({url:String(url),method,headers,body});order.push('fetch:'+method);
  if(method==='POST'&&String(url).endsWith('/save')){
    if(options.rejectSave)return response(502,{reason:'documentserver_unavailable'});
    currentStatus={...currentStatus,state:'saving',save_seq:options.nextSaveSequence??currentStatus.save_seq+1,reason:null};
    if(options.autoCommit)currentStatus={...currentStatus,state:'editing',last_committed_seq:currentStatus.save_seq,
      last_published_seq:options.equalPublished?currentStatus.save_seq:currentStatus.last_published_seq};
    const reply=response(202,{session_id:'session-id',save_seq:currentStatus.save_seq,intent:body.intent});
    if(options.deferSave)return new Promise(resolve=>{resolveSave=()=>resolve(reply);});
    return reply;
  }
  if(method==='POST'){
    if(options.transportError)throw new Error('network failed');
    if(options.deferCreate)return new Promise(resolve=>{resolveCreate=resolve;});
    if(options.refusal)return response(options.refusal.status,{reason:options.refusal.reason});
    return response(options.joined?200:201,{session_id:'session-id',file_id:file,document_key:'stable-key',state:'opening',joined:Boolean(options.joined),editor_config:options.noConfig?null:clone(signed)});
  }
  if(options.statusError)return response(500,{reason:'state_corrupt'});
  statusReads++; activeStatuses++; maxActiveStatuses=Math.max(maxActiveStatuses,activeStatuses);
  const reply=response(200,currentStatus);
  if(options.holdStatusAfter!==undefined&&statusReads>options.holdStatusAfter){
    return new Promise(resolve=>heldStatuses.push(()=>{activeStatuses--;resolve(reply);}));
  }
  activeStatuses--;
  return reply;
};
const context=vm.createContext({window:hostWindow,document:hostDocument,URL,Headers,fetch,console,
  setTimeout:hostWindow.setTimeout,clearTimeout:hostWindow.clearTimeout,queueMicrotask});
const modules=new Map();
async function load(url){
  if(modules.has(url))return modules.get(url);
  const module=new vm.SourceTextModule(fs.readFileSync(path.join(directory,new URL(url).pathname.split('/').pop()),'utf8'),{
    context,identifier:url,initializeImportMeta(meta){meta.url=url;}
  });modules.set(url,module);
  await module.link((specifier,parent)=>load(new URL(specifier,parent.identifier).href));
  return module;
}
const module=await load('https://webui.test/tools/ocu/static/office-editor.js');await module.evaluate();
const host=module.namespace.createOfficeEditorHost({chatId:chat,docserverOrigin:options.origin||'https://office.test:8443',container,window:hostWindow,document:hostDocument});
const idle=()=>new Promise(resolve=>setImmediate(resolve));
async function advance(milliseconds){
  const end=now+milliseconds;
  for(let count=0;count<10000;count++){
    const next=[...timers].filter(([,timer])=>timer.due<=end).sort((a,b)=>a[1].due-b[1].due||a[0]-b[0])[0];
    if(!next){now=end;return;}
    const [id,timer]=next;now=timer.due;timers.delete(id);timer.fn();await idle();
  }
  throw new Error('Timer did not make progress');
}
if(options.messages){for(const row of options.messages){const data=row.data;const changes={};if(row.source)changes.source={parent:row.source==='nested'?hostWindow:hostWindow.parent};if(row.origin)changes.origin=row.origin;dispatch(data,changes);}await idle();}
if(options.open)dispatch(open);
await idle();
if(resolveCreate&&options.releaseCreate){resolveCreate(response(201,{session_id:'session-id',file_id:file,document_key:'stable-key',state:'opening',joined:false,editor_config:clone(signed)}));await idle();}
if(options.expire)await advance(10000);
const beforeLate=options.lateLoad?clone({calls,messages}):null;
if(options.lateLoad){hostWindow.DocsAPI={DocEditor:class{constructor(){attempts++;}}};pendingLoad?.();await idle();}
if(options.modify&&configs.length){for(const value of options.modify){configs[0].events.onDocumentStateChange({data:value});await idle();}}
if(options.error&&configs.length){configs[0].events.onError({data:{errorCode:-18,errorDescription:'connection lost'}});await idle();}
if(options.laterStatus){for(const status of options.laterStatus){host.applyStatus({...initial,...status});await idle();}}
if(options.after){for(const data of options.after)dispatch(data);await idle();}
for(const action of options.actions||[]){
  if(action.kind==='modify')configs.at(-1).events.onDocumentStateChange({data:action.value??true});
  else if(action.kind==='command')dispatch({type:'ocu:office-command',chat_id:chat,generation:7,command:action.command});
  else if(action.kind==='snapshot'){currentStatus={...currentStatus,...action.status};host.applyStatus(clone(currentStatus));}
  else if(action.kind==='releaseSave')resolveSave();
  else if(action.kind==='status')currentStatus={...currentStatus,...action.status};
  else if(action.kind==='tick')await advance(action.ms);
  else if(action.kind==='releaseStatus')heldStatuses.shift()();
  else throw new Error('Unknown test action: '+action.kind);
  await idle();
  actionStates.push(clone(messages.at(-1).data));
}
const dom=node=>({tag:node.tagName,text:node.textContent,hidden:node.hidden,attributes:node.attributes,children:node.children.map(dom)});
process.stdout.write(JSON.stringify({calls,messages,scripts,configs:configs.map(config=>clone(config)),signed,order,attempts,timers:timers.size,actionStates,
  activeStatuses,maxActiveStatuses,beforeLate,timerDelays:[...timers.values()].map(timer=>timer.ms),dom:dom(container)}));
"""


def _run(tmp_path, **options):
    harness = tmp_path / "office-editor-scenario.mjs"
    harness.write_text(_HARNESS)
    result = subprocess.run(
        [NODE, "--experimental-vm-modules", str(harness), str(SERVER_DIR / "static"),
         json.dumps({"chat": CHAT, **options})],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_ready_reply_opens_once_with_signed_configuration(tmp_path):
    file_id = "opaque/file ?文档"
    result = _run(tmp_path, file=file_id, replyOnReady=True, duplicateOnReady=True)
    assert result["order"][:2] == ["listener:message", "message:ocu:office-ready"]
    assert result["messages"][0] == {
        "data": {"type": "ocu:office-ready", "chat_id": CHAT}, "target": "https://webui.test",
    }
    assert [(row["method"], row["url"]) for row in result["calls"]] == [
        ("POST", f"/tools/ocu/api/office/{CHAT}/documents/{quote(file_id, safe='')}/sessions"),
        ("GET", f"/tools/ocu/api/office/{CHAT}/sessions/session-id"),
    ]
    assert all(row["headers"]["x-requested-with"] == "ocu-workspace" for row in result["calls"])
    assert result["scripts"] == ["https://office.test:8443/web-apps/apps/api/documents/api.js"]
    config = result["configs"][0]
    assert {key: config[key] for key in result["signed"]} == result["signed"]
    states = [row["data"] for row in result["messages"] if row["data"]["type"] == "ocu:office-state"]
    assert states[0] == {"type": "ocu:office-state", "chat_id": CHAT, "file_id": file_id,
                         "generation": 7, "session_id": None, "state": "opening", "dirty": False,
                         "workspace_changed": False, "reason": None}
    assert states[-1] == {**states[0], "session_id": "session-id", "state": "editing"}
    assert result["attempts"] == 1


def _open(**changes):
    return {"type": "ocu:office-open", "chat_id": CHAT, "file_id": "document-id", "generation": 7, **changes}


def _states(result):
    return [row["data"] for row in result["messages"] if row["data"]["type"] == "ocu:office-state"]


@pytest.mark.parametrize("message", [
    {"data": _open(), "source": "sibling"},
    {"data": _open(), "source": "nested"},
    {"data": _open(), "origin": "https://other.test"},
    {"data": _open(chat_id="another-chat")},
    {"data": _open(extra=True)},
    {"data": {"type": "ocu:office-open", "chat_id": CHAT, "generation": 7}},
    {"data": _open(generation=1.5)},
    {"data": _open(generation=-1)},
    {"data": _open(generation=2**53)},
    {"data": _open(generation=True)},
    {"data": _open(generation="7")},
    {"data": _open(file_id="")},
    {"data": _open(file_id=1)},
    {"data": _open(type="unknown")},
    {"data": _open(type="ocu:preview-select")},
    {"data": None},
    {"data": []},
    {"data": {"type": "ocu:office-command", "chat_id": CHAT, "generation": 7, "command": "unknown"}},
])
def test_rejected_message_has_no_effect(tmp_path, message):
    result = _run(tmp_path, messages=[message])
    assert result["calls"] == []
    assert result["scripts"] == []
    assert result["configs"] == []
    assert result["messages"] == [{
        "data": {"type": "ocu:office-ready", "chat_id": CHAT}, "target": "https://webui.test",
    }]


def test_pending_creation_latches_identity_and_rejects_later_opens(tmp_path):
    result = _run(tmp_path, replyOnReady=True, deferCreate=True, releaseCreate=True,
                  messages=[{"data": _open(generation=6)}, {"data": _open(generation=8, file_id="other-file")}],
                  after=[_open(generation=9),
                         {"type": "ocu:office-command", "chat_id": CHAT, "generation": 6, "command": "save"},
                         {"type": "ocu:office-command", "chat_id": CHAT, "generation": 7, "command": "unknown"}])
    assert [row["method"] for row in result["calls"]] == ["POST", "GET"]
    assert result["attempts"] == 1
    assert all(row["file_id"] == "document-id" and row["generation"] == 7 for row in _states(result))


@pytest.mark.parametrize(("state", "reason", "committed", "published", "dirty"), [
    ("editing", None, 2, 2, False),
    ("saving", None, 2, 1, True),
    ("closing", None, 2, 1, True),
    ("closed", None, 2, 1, False),
    ("conflict", "workspace_changed", 2, 1, True),
    ("error", "save_failed", 2, 1, True),
    ("orphaned", "editor_state_lost", 2, 1, True),
])
def test_initial_snapshot_reports_persisted_state(tmp_path, state, reason, committed, published, dirty):
    result = _run(tmp_path, open=True, joined=True, noConfig=state == "conflict",
                  status={"state": state, "reason": reason, "last_committed_seq": committed,
                          "last_published_seq": published, "workspace_changed": True})
    assert _states(result)[-1] == {
        "type": "ocu:office-state", "chat_id": CHAT, "file_id": "document-id", "generation": 7,
        "session_id": "session-id", "state": state, "dirty": dirty, "workspace_changed": True, "reason": reason,
    }
    if state in {"closed", "conflict", "error", "orphaned"}:
        assert result["scripts"] == []
        assert result["configs"] == []


def test_modification_acknowledgement_cannot_clear_dirty(tmp_path):
    result = _run(tmp_path, open=True, modify=[True, False, True], laterStatus=[
        {"file_id": "saved-as-destination", "workspace_changed": True},
        {"file_id": "saved-as-destination", "workspace_changed": True},
    ])
    states = _states(result)
    assert [row["dirty"] for row in states] == [False, False, False, True, True]
    assert states[-1]["workspace_changed"] is True
    assert all(row["file_id"] == "document-id" for row in states)
    assert [row["method"] for row in result["calls"]] == ["POST", "GET"]


def test_resolved_conflict_uses_publication_state_not_sticky_dirty(tmp_path):
    result = _run(tmp_path, open=True, noConfig=True,
                  status={"state": "conflict", "reason": "workspace_changed", "last_published_seq": 1},
                  laterStatus=[{"state": "editing", "reason": None, "last_published_seq": 2}])
    assert [(row["state"], row["dirty"]) for row in _states(result)[-2:]] == [
        ("conflict", True), ("editing", False),
    ]
    assert result["configs"] == []
    assert result["scripts"] == []


@pytest.mark.parametrize(("status", "reason"), [
    (404, "unknown_file"), (415, "unsupported_type"), (413, "file_too_large"),
    (422, "unsafe_path"), (422, "corrupt_document"), (503, "storage_low"),
    (409, "unpublished_version"),
])
def test_admission_refusal_is_final_without_a_session(tmp_path, status, reason):
    result = _run(tmp_path, open=True, refusal={"status": status, "reason": reason},
                  after=[_open(generation=9),
                         {"type": "ocu:office-command", "chat_id": CHAT, "generation": 7, "command": "save"},
                         {"type": "ocu:office-command", "chat_id": CHAT, "generation": 7, "command": "close"}],
                  laterStatus=[{"state": "editing"}])
    assert [row["method"] for row in result["calls"]] == ["POST"]
    assert _states(result)[-1] == {
        "type": "ocu:office-state", "chat_id": CHAT, "file_id": "document-id", "generation": 7,
        "session_id": None, "state": "refused", "dirty": False, "workspace_changed": False, "reason": reason,
    }
    assert result["scripts"] == result["configs"] == []


@pytest.mark.parametrize(("status", "reason"), [
    (502, "documentserver_unavailable"), (503, "publish_pending"), (500, "state_corrupt"),
    (401, "unauthorized"), (403, "forbidden"), (500, "storage_low"),
])
def test_non_admission_failure_is_not_refused(tmp_path, status, reason):
    result = _run(tmp_path, open=True, refusal={"status": status, "reason": reason},
                  after=[_open(generation=9)])
    state = _states(result)[-1]
    assert (state["state"], state["session_id"], state["reason"]) == ("error", None, reason)
    assert [row["method"] for row in result["calls"]] == ["POST"]
    assert result["configs"] == result["scripts"] == []


@pytest.mark.parametrize(("options", "session", "reason"), [
    ({"transportError": True}, None, None),
    ({"statusError": True}, "session-id", "state_corrupt"),
    ({"api": "error"}, "session-id", None),
    ({"api": "missing"}, "session-id", None),
    ({"api": "timeout", "expire": True}, "session-id", None),
    ({"api": "throw"}, "session-id", None),
])
def test_failed_creation_status_or_api_is_visible_error(tmp_path, options, session, reason):
    result = _run(tmp_path, open=True, **options)
    state = _states(result)[-1]
    assert (state["state"], state["session_id"]) == ("error", session)
    assert isinstance(state["reason"], str) and state["reason"]
    if reason is not None:
        assert state["reason"] == reason
    assert result["configs"] == []
    alert = result["dom"]["children"][0]
    assert alert["attributes"]["role"] == "alert"
    assert alert["hidden"] is False
    assert state["reason"] in alert["text"]
    assert result["timers"] == 0


@pytest.mark.parametrize("synchronous", [False, True])
def test_joined_editor_connection_loss_never_closes_session(tmp_path, synchronous):
    result = _run(tmp_path, open=True, joined=True, syncError=synchronous, error=not synchronous)
    state = _states(result)[-1]
    assert (state["state"], state["session_id"]) == ("error", "session-id")
    assert isinstance(state["reason"], str) and state["reason"]
    assert [row["method"] for row in result["calls"]] == ["POST", "GET"]
    assert all(row["state"] != "refused" for row in _states(result))
    assert result["dom"]["children"][0]["hidden"] is False


def test_constructor_modification_survives_return(tmp_path):
    result = _run(tmp_path, open=True, syncModified=True)
    assert _states(result)[-1]["dirty"] is True
    assert result["attempts"] == 1


def test_late_api_completion_cannot_resurrect_a_timed_out_open(tmp_path):
    result = _run(tmp_path, open=True, api="timeout", expire=True, lateLoad=True,
                  after=[_open(generation=9)])
    states = _states(result)
    assert states[-1]["state"] == "error"
    assert [row["state"] for row in states].count("error") == 1
    assert result["attempts"] == 0
    assert result["calls"] == result["beforeLate"]["calls"]
    assert result["messages"] == result["beforeLate"]["messages"]
    assert result["timers"] == 0


def test_parent_publish_command_does_not_acknowledge_uncommitted_edits(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], after=[
        {"type": "ocu:office-command", "chat_id": CHAT, "generation": 7, "command": "save"},
    ])
    saves = [row for row in result["calls"] if row["url"].endswith("/save")]
    assert len(saves) == 1
    assert saves[0]["method"] == "POST"
    assert saves[0]["body"] == {"intent": "publish"}
    assert _states(result)[-1]["dirty"] is True
    assert _states(result)[-1]["state"] == "saving"


@pytest.mark.parametrize("later_edit", [False, True])
@pytest.mark.parametrize("commit_before_reply", [False, True])
def test_committed_save_covers_only_its_dispatch_generation(tmp_path, later_edit, commit_before_reply):
    actions = [{"kind": "command", "command": "save"}]
    if later_edit:
        actions.append({"kind": "modify"})
    committed = {"kind": "snapshot", "status": {
        "state": "editing", "save_seq": 9, "last_committed_seq": 9, "last_published_seq": 9,
    }}
    actions += ([committed, {"kind": "releaseSave"}] if commit_before_reply
                else [{"kind": "releaseSave"}, committed])
    result = _run(tmp_path, open=True, modify=[True], deferSave=True,
                  nextSaveSequence=9, actions=actions)
    assert result["actionStates"][0]["dirty"] is True
    assert result["actionStates"][-1]["dirty"] is later_edit
    assert _states(result)[-1]["state"] == "editing"
    assert len([row for row in result["calls"] if row["url"].endswith("/save")]) == 1


def test_another_tabs_sequence_does_not_acknowledge_local_edits(tmp_path):
    result = _run(tmp_path, open=True, modify=[True], actions=[
        {"kind": "snapshot", "status": {"save_seq": 9, "last_committed_seq": 9, "last_published_seq": 9}},
    ])
    assert _states(result)[-1]["dirty"] is True
    assert all(not row["url"].endswith("/save") for row in result["calls"])


@pytest.mark.parametrize(("mode", "save_count", "dirty"), [
    ("commit", 1, True), ("equal", 1, False), ("reject", 2, True),
])
def test_editing_polls_do_not_starve_autosave_or_repeat_covered_edits(tmp_path, mode, save_count, dirty):
    result = _run(tmp_path, open=True, modify=[True], autoCommit=mode != "reject",
                  equalPublished=mode == "equal", rejectSave=mode == "reject", actions=[
                      {"kind": "tick", "ms": 300000}, {"kind": "tick", "ms": 300000},
                  ])
    saves = [row for row in result["calls"] if row["url"].endswith("/save")]
    assert len(saves) == save_count
    assert all(row["body"] == {"intent": "persist"} for row in saves)
    assert _states(result)[-1]["dirty"] is dirty
    assert result["timerDelays"].count(300000) == 1
    assert result["maxActiveStatuses"] == 1
    if mode == "reject":
        assert _states(result)[-1]["state"] == "editing"
        assert _states(result)[-1]["reason"] == "documentserver_unavailable"


def test_status_poll_does_not_overlap_a_held_request(tmp_path):
    result = _run(tmp_path, open=True, holdStatusAfter=1, actions=[
        {"kind": "tick", "ms": 1000}, {"kind": "tick", "ms": 5000},
    ])
    assert len([row for row in result["calls"] if row["method"] == "GET"]) == 2
    assert result["activeStatuses"] == result["maxActiveStatuses"] == 1
